"""离线备份恢复：暂存校验、原子切换、恢复凭据与状态持久化。

流程：

1. ``stage_backup`` 把上传的 SQLite backup 文件写入暂存目录
   ``<数据目录>/recovery/<backup_id>/backup.db``，以**只读**方式完成全部校验
   （完整性 / 表结构 / 外键 / 每修订行数 / 修订号连续 / 区间规则）。
   校验全程不打开、不修改现库，非法备份直接拒绝并删除暂存文件。
2. ``confirm_backup`` 用恢复凭据（一次性 token）确认：
   重新核对现库内容指纹——准备之后若发生过补录或其他恢复成功，返回 409；
   随后在**单个事务**内通过 ATTACH 暂存库整体替换两张表的数据，
   事务原子性保证切换失败/进程崩溃后磁盘上只可能是完整旧库或完整备份。
3. 所有恢复状态持久化在 ``recovery/index.json``（原子写），
   重启后待确认备份与已完成恢复结果仍然可用；
   崩溃在切换途中的 ``switching`` 记录在启动时按内容指纹对账修复。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .database import get_conn, get_db_path, open_readonly, snapshot
from .errors import AppError

# 上传体积上限：备份仅含两张归属表，50 MiB 足够并防止恶意超大文件。
MAX_BACKUP_BYTES = 50 * 1024 * 1024

# 写路径全局互斥：补录提交与恢复切换互斥，保证「指纹校验 -> 切换」原子。
write_lock = threading.RLock()


def recovery_dir() -> Path:
    return get_db_path().parent / "recovery"


def _index_path() -> Path:
    return recovery_dir() / "index.json"


# index.json 的进程内互斥（与 write_lock 独立：上传校验不阻塞查询）。
_index_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 状态持久化
# --------------------------------------------------------------------------- #

def _read_index() -> dict:
    path = _index_path()
    if not path.exists():
        return {"backups": {}}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise AppError(
            code="RECOVERY_STATE_CORRUPT",
            message=f"恢复状态文件无法读取：{exc}",
            status_code=500,
        )
    if not isinstance(data, dict) or not isinstance(data.get("backups"), dict):
        raise AppError(
            code="RECOVERY_STATE_CORRUPT",
            message="恢复状态文件内容格式不正确",
            status_code=500,
        )
    return data


def _write_index(data: dict) -> None:
    """原子写 index.json：临时文件 + fsync + rename，崩溃不会留下半截状态。"""
    recovery_dir().mkdir(parents=True, exist_ok=True)
    path = _index_path()
    tmp = path.with_name(f".index.json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@contextmanager
def _index_cursor():
    with _index_lock:
        data = _read_index()
        yield data
        _write_index(data)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 内容指纹：用于「准备后是否被改动」判定与启动对账
# --------------------------------------------------------------------------- #

def _fingerprint_conn(conn: sqlite3.Connection) -> str:
    """对两张表的全部业务行计算稳定指纹（按主键排序后哈希）。"""
    digest = hashlib.sha256()
    rev_rows = conn.execute(
        "SELECT revision_id, created_at, entry_count, note "
        "FROM revisions ORDER BY revision_id"
    ).fetchall()
    seg_rows = conn.execute(
        "SELECT id, revision_id, employee_id, cost_center, effective_from, effective_to "
        "FROM attribution_segments ORDER BY id"
    ).fetchall()
    for row in rev_rows:
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    digest.update(b"---\n")
    for row in seg_rows:
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def live_fingerprint() -> str:
    with get_conn() as conn:
        with snapshot(conn):
            return _fingerprint_conn(conn)


def _live_max_revision() -> int:
    with get_conn() as conn:
        with snapshot(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions"
            ).fetchone()
            return int(row["r"])


# --------------------------------------------------------------------------- #
# 备份校验（只读，不触碰现库）
# --------------------------------------------------------------------------- #

_EXPECTED_REVISION_COLS = {
    # name: (type, notnull, pk)
    "revision_id": ("INTEGER", 0, 1),
    "created_at": ("TEXT", 1, 0),
    "entry_count": ("INTEGER", 1, 0),
    "note": ("TEXT", 0, 0),
}
_EXPECTED_SEGMENT_COLS = {
    "id": ("INTEGER", 0, 1),
    "revision_id": ("INTEGER", 1, 0),
    "employee_id": ("TEXT", 1, 0),
    "cost_center": ("TEXT", 1, 0),
    "effective_from": ("TEXT", 1, 0),
    "effective_to": ("TEXT", 1, 0),
}
_REQUIRED_INDEXES = {
    "idx_segments_emp_dates_rev",
    "idx_segments_revision",
}
_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"


class BackupRejected(AppError):
    """备份未通过校验（400），携带失败原因。"""

    def __init__(self, code: str, message: str, details=None):
        super().__init__(code=code, message=message, status_code=400, details=details)


def _reject(code: str, message: str, details=None):
    raise BackupRejected(code=code, message=message, details=details)


def _check_columns(conn: sqlite3.Connection, table: str, expected: dict) -> None:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    actual = {
        c["name"]: (
            (c["type"] or "").upper(),
            int(c["notnull"]),
            int(c["pk"]),
        )
        for c in cols
    }
    if set(actual.keys()) != set(expected.keys()):
        _reject(
            "BACKUP_SCHEMA_MISMATCH",
            f"表 {table} 列结构不匹配：期望 {sorted(expected)}，实际 {sorted(actual)}",
            {"table": table, "expected": sorted(expected), "actual": sorted(actual)},
        )
    for name, want in expected.items():
        if actual[name] != want:
            _reject(
                "BACKUP_SCHEMA_MISMATCH",
                f"表 {table} 列 {name} 定义不匹配：期望 {want}，实际 {actual[name]}",
                {"table": table, "column": name, "expected": list(want), "actual": list(actual[name])},
            )


def validate_backup_file(path: Path) -> dict:
    """对暂存备份执行全部校验，通过则返回备份摘要，否则抛 BackupRejected。

    校验项：
      1. SQLite 文件头与 ``PRAGMA integrity_check`` 完整性；
      2. 仅含 revisions / attribution_segments 两张业务表（及内部 sqlite_sequence），
         无额外表/视图/触发器，必需索引存在，列定义、主键、CHECK 与外键声明一致；
      3. ``PRAGMA foreign_key_check`` 外键数据一致性；
      4. 每个修订号的区间行数与 revisions.entry_count 一致；
      5. 修订号从 1 起连续无缺口，区间 id 正向唯一；
      6. 区间规则：ISO 日期、起日严格早于止日、同修订同员工区间不重叠。
    """
    # 规则 1a：SQLite 文件头魔数
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        _reject("BACKUP_UNREADABLE", f"暂存备份无法读取：{exc}")
    if header != b"SQLite format 3\x00":
        _reject(
            "BACKUP_NOT_SQLITE",
            "文件不是有效的 SQLite 数据库（缺少 'SQLite format 3' 文件头）",
        )

    try:
        with open_readonly(path) as conn:
            # 规则 1b：完整性检查
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                _reject(
                    "BACKUP_INTEGRITY_FAILED",
                    "备份数据库完整性检查未通过",
                    {"integrity_check": integrity[:10]},
                )

            # 规则 2a：对象清单——只允许两张业务表 + sqlite_sequence 内部表
            objects = conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            by_type: dict[str, set[str]] = {}
            for obj in objects:
                by_type.setdefault(obj["type"], set()).add(obj["name"])
            tables = by_type.get("table", set())
            if tables != {"revisions", "attribution_segments"}:
                _reject(
                    "BACKUP_SCHEMA_MISMATCH",
                    f"备份必须且只能包含 revisions、attribution_segments 两张表，实际：{sorted(tables)}",
                    {"tables": sorted(tables)},
                )
            extra_nontables = {
                (t, n)
                for t, names in by_type.items()
                if t in ("view", "trigger")
                for n in names
            }
            if extra_nontables:
                _reject(
                    "BACKUP_SCHEMA_MISMATCH",
                    f"备份中不允许存在视图/触发器等额外对象：{sorted(extra_nontables)}",
                    {"objects": [list(x) for x in sorted(extra_nontables)]},
                )

            # 规则 2b：列定义、主键
            _check_columns(conn, "revisions", _EXPECTED_REVISION_COLS)
            _check_columns(conn, "attribution_segments", _EXPECTED_SEGMENT_COLS)

            # 规则 2c：外键声明（segments.revision_id -> revisions.revision_id）
            fks = conn.execute(
                "PRAGMA foreign_key_list(attribution_segments)"
            ).fetchall()
            fk_ok = any(
                (f["table"] == "revisions" and f["from"] == "revision_id"
                 and f["to"] == "revision_id")
                for f in fks
            )
            if not fk_ok:
                _reject(
                    "BACKUP_SCHEMA_MISMATCH",
                    "attribution_segments 缺少指向 revisions(revision_id) 的外键声明",
                )

            # 规则 2d：CHECK(effective_from < effective_to) 约束声明
            seg_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='attribution_segments'"
            ).fetchone()["sql"]
            if "check" not in seg_sql.lower() or "effective_from" not in seg_sql \
                    or "effective_to" not in seg_sql:
                _reject(
                    "BACKUP_SCHEMA_MISMATCH",
                    "attribution_segments 缺少 effective_from < effective_to 的 CHECK 约束",
                )

            # 规则 2e：必需索引
            indexes = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
                )
            }
            missing = _REQUIRED_INDEXES - indexes
            if missing:
                _reject(
                    "BACKUP_SCHEMA_MISMATCH",
                    f"备份缺少必需索引：{sorted(missing)}",
                    {"missing_indexes": sorted(missing)},
                )

            # 规则 3：外键数据一致性（孤儿行等）
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                _reject(
                    "BACKUP_FOREIGN_KEY_VIOLATION",
                    "备份存在外键不一致数据（存在不指向任何修订的区间行）",
                    {
                        "violations": [
                            {"table": v[0], "rowid": v[1], "parent": v[2], "fkid": v[3]}
                            for v in fk_violations[:20]
                        ]
                    },
                )

            rev_count = int(
                conn.execute("SELECT COUNT(*) AS c FROM revisions").fetchone()["c"]
            )
            seg_count = int(
                conn.execute("SELECT COUNT(*) AS c FROM attribution_segments").fetchone()["c"]
            )

            # 规则 4：每修订行数与 entry_count 一致
            bad_counts = conn.execute(
                """
                SELECT r.revision_id, r.entry_count,
                       (SELECT COUNT(*) FROM attribution_segments s
                         WHERE s.revision_id = r.revision_id) AS actual_count
                FROM revisions r
                WHERE r.entry_count <> (
                    SELECT COUNT(*) FROM attribution_segments s
                    WHERE s.revision_id = r.revision_id
                )
                """
            ).fetchall()
            if bad_counts:
                _reject(
                    "BACKUP_ROW_COUNT_MISMATCH",
                    "存在修订的 entry_count 与实际区间行数不一致",
                    {
                        "mismatches": [
                            {
                                "revision_id": r["revision_id"],
                                "declared": r["entry_count"],
                                "actual": r["actual_count"],
                            }
                            for r in bad_counts
                        ]
                    },
                )
            neg_entry = conn.execute(
                "SELECT revision_id FROM revisions WHERE entry_count < 1 LIMIT 1"
            ).fetchall()
            if neg_entry:
                _reject(
                    "BACKUP_ROW_COUNT_MISMATCH",
                    "revisions.entry_count 必须为正整数",
                    {"revision_id": neg_entry[0]["revision_id"]},
                )
            orphan_count = conn.execute(
                """
                SELECT COUNT(*) AS c FROM attribution_segments s
                WHERE NOT EXISTS (
                    SELECT 1 FROM revisions r WHERE r.revision_id = s.revision_id
                )
                """
            ).fetchone()["c"]
            if orphan_count:
                _reject(
                    "BACKUP_FOREIGN_KEY_VIOLATION",
                    f"存在 {orphan_count} 行区间不指向任何修订",
                )

            # 规则 5a：修订号从 1 起连续无缺口
            rev_ids = [
                r["revision_id"]
                for r in conn.execute(
                    "SELECT revision_id FROM revisions ORDER BY revision_id"
                )
            ]
            if rev_ids != list(range(1, len(rev_ids) + 1)):
                _reject(
                    "BACKUP_REVISION_GAP",
                    "备份修订号必须从 1 开始且连续无缺口",
                    {"revision_ids": rev_ids},
                )

            # 规则 5b：区间 id 正向唯一（主键已保证唯一，再排除非正值）
            bad_id = conn.execute(
                "SELECT COUNT(*) AS c FROM attribution_segments WHERE id IS NULL OR id <= 0"
            ).fetchone()["c"]
            if bad_id:
                _reject(
                    "BACKUP_SCHEMA_MISMATCH",
                    "attribution_segments 存在非正 id",
                )

            # 规则 6a：日期格式（ISO yyyy-mm-dd）
            bad_dates = conn.execute(
                """
                SELECT id, effective_from, effective_to FROM attribution_segments
                WHERE effective_from NOT GLOB ? OR effective_to NOT GLOB ?
                LIMIT 20
                """,
                (_DATE_GLOB, _DATE_GLOB),
            ).fetchall()
            if bad_dates:
                _reject(
                    "BACKUP_INVALID_DATE_RANGE",
                    "存在非 ISO 8601(yyyy-mm-dd) 格式的日期",
                    {
                        "rows": [
                            {
                                "id": r["id"],
                                "effective_from": r["effective_from"],
                                "effective_to": r["effective_to"],
                            }
                            for r in bad_dates
                        ]
                    },
                )

            # 规则 6b：起日严格早于止日（CHECK 已约束，再做带上下文的显式校验）
            bad_range = conn.execute(
                """
                SELECT id, revision_id, employee_id, effective_from, effective_to
                FROM attribution_segments
                WHERE effective_from >= effective_to
                LIMIT 20
                """
            ).fetchall()
            if bad_range:
                _reject(
                    "BACKUP_INVALID_DATE_RANGE",
                    "存在起日不早于止日的区间（左闭右开要求起日严格早于止日）",
                    {
                        "rows": [dict(r) for r in bad_range],
                    },
                )

            # 规则 6c：同一修订内同一员工的区间不得重叠（首尾相接允许）
            overlaps = conn.execute(
                """
                SELECT a.id AS id_a, b.id AS id_b, a.revision_id, a.employee_id,
                       a.effective_from AS a_from, a.effective_to AS a_to,
                       b.effective_from AS b_from, b.effective_to AS b_to
                FROM attribution_segments a
                JOIN attribution_segments b
                  ON a.revision_id = b.revision_id
                 AND a.employee_id = b.employee_id
                 AND a.id < b.id
                 AND a.effective_from < b.effective_to
                 AND b.effective_from < a.effective_to
                LIMIT 20
                """
            ).fetchall()
            if overlaps:
                first = overlaps[0]
                _reject(
                    "BACKUP_OVERLAPPING_SEGMENTS",
                    (
                        f"备份中员工 {first['employee_id']} 在修订 {first['revision_id']} "
                        f"存在重叠区间：id={first['id_a']} "
                        f"[{first['a_from']}, {first['a_to']}) 与 id={first['id_b']} "
                        f"[{first['b_from']}, {first['b_to']})"
                    ),
                    {"rows": [dict(r) for r in overlaps]},
                )

            # 文本列 NULL/空串兜底（NOT NULL 约束已挡住 NULL，这里再防空串）
            blank = conn.execute(
                """
                SELECT COUNT(*) AS c FROM attribution_segments
                WHERE trim(employee_id) = '' OR trim(cost_center) = ''
                   OR trim(effective_from) = '' OR trim(effective_to) = ''
                """
            ).fetchone()["c"]
            if blank:
                _reject(
                    "BACKUP_SCHEMA_MISMATCH",
                    "attribution_segments 存在空字符串字段",
                )

            min_rev = rev_ids[0] if rev_ids else None
            max_rev = rev_ids[-1] if rev_ids else None
            fp = _fingerprint_conn(conn)

            return {
                "revision_count": rev_count,
                "segment_count": seg_count,
                "min_revision_id": min_rev,
                "max_revision_id": max_rev,
                "content_fingerprint": fp,
            }
    except BackupRejected:
        raise
    except sqlite3.DatabaseError as exc:
        _reject("BACKUP_INTEGRITY_FAILED", f"备份数据库无法打开或已损坏：{exc}")


# --------------------------------------------------------------------------- #
# 上传暂存
# --------------------------------------------------------------------------- #

def stage_backup(file_bytes: bytes, filename: str | None, note: str | None) -> dict:
    """落盘暂存并校验；合法返回备份信息（含一次性恢复凭据），非法拒绝且不留文件。"""
    if len(file_bytes) == 0:
        raise AppError(
            code="BACKUP_EMPTY",
            message="上传内容为空，不是有效的 SQLite 备份文件",
            status_code=400,
        )
    if len(file_bytes) > MAX_BACKUP_BYTES:
        raise AppError(
            code="BACKUP_TOO_LARGE",
            message=f"备份文件超过 {MAX_BACKUP_BYTES // (1024 * 1024)} MiB 上限",
            status_code=413,
            details={"size_bytes": len(file_bytes), "max_bytes": MAX_BACKUP_BYTES},
        )

    backup_id = f"bk-{uuid.uuid4().hex}"
    bdir = recovery_dir() / backup_id
    bdir.mkdir(parents=True, exist_ok=False)
    backup_path = bdir / "backup.db"
    try:
        # 先写临时文件再 rename，保证目录里不会出现半截备份。
        tmp_path = bdir / ".upload.tmp"
        with tmp_path.open("wb") as fh:
            fh.write(file_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, backup_path)

        summary = validate_backup_file(backup_path)
    except Exception:
        # 非法备份：整个暂存目录移除，现库从未被触碰。
        shutil.rmtree(bdir, ignore_errors=True)
        raise

    token = secrets.token_urlsafe(32)
    prepared_fp = live_fingerprint()
    entry = {
        "backup_id": backup_id,
        "status": "staged",
        "filename": filename or "backup.db",
        "note": note,
        "size_bytes": len(file_bytes),
        "sha256": hashlib.sha256(file_bytes).hexdigest(),
        "created_at": _utcnow(),
        "prepared_at": _utcnow(),
        "prepared_against_fingerprint": prepared_fp,
        "prepared_against_max_revision": _live_max_revision(),
        "token_hash": _hash_token(token),
        "recovery": None,  # 确认成功后填充
        **summary,
    }
    with _index_cursor() as data:
        data["backups"][backup_id] = entry

    return {
        "backup_id": backup_id,
        "status": "staged",
        "recovery_token": token,  # 仅在本次响应中明文返回一次
        "filename": entry["filename"],
        "note": note,
        "size_bytes": entry["size_bytes"],
        "sha256": entry["sha256"],
        "created_at": entry["created_at"],
        "prepared_at": entry["prepared_at"],
        "revision_count": summary["revision_count"],
        "segment_count": summary["segment_count"],
        "min_revision_id": summary["min_revision_id"],
        "max_revision_id": summary["max_revision_id"],
        "prepared_against_max_revision": entry["prepared_against_max_revision"],
    }


# --------------------------------------------------------------------------- #
# 确认与原子切换
# --------------------------------------------------------------------------- #

def _get_entry(backup_id: str) -> dict:
    data = _read_index()
    entry = data["backups"].get(backup_id)
    if entry is None:
        raise AppError(
            code="BACKUP_NOT_FOUND",
            message=f"备份 {backup_id} 不存在（可能未通过校验或已被清理）",
            status_code=404,
        )
    return entry


def confirm_backup(backup_id: str, token: str) -> dict:
    """凭恢复凭据确认恢复，整体替换现库两张表的数据。"""
    with write_lock:
        entry = _get_entry(backup_id)

        # 幂等：同一备份重复确认，直接返回原结果，不再次恢复。
        if entry["status"] == "confirmed":
            if not hmac.compare_digest(
                entry.get("token_hash", ""), _hash_token(token)
            ):
                raise AppError(
                    code="RECOVERY_TOKEN_INVALID",
                    message="恢复凭据无效",
                    status_code=403,
                )
            return _public_entry(entry)

        if entry["status"] != "staged":
            raise AppError(
                code="RECOVERY_CONFLICT",
                message=f"备份当前状态为 {entry['status']}，无法确认恢复",
                status_code=409,
                details={"backup_id": backup_id, "status": entry["status"]},
            )
        if not hmac.compare_digest(entry.get("token_hash", ""), _hash_token(token)):
            raise AppError(
                code="RECOVERY_TOKEN_INVALID",
                message="恢复凭据无效",
                status_code=403,
            )

        backup_path = recovery_dir() / backup_id / "backup.db"
        if not backup_path.exists():
            raise AppError(
                code="BACKUP_NOT_FOUND",
                message="暂存备份文件已丢失，无法恢复",
                status_code=404,
            )

        # 新鲜度：准备之后若有补录或其他恢复成功，现库指纹必然变化。
        current_fp = live_fingerprint()
        if current_fp != entry["prepared_against_fingerprint"]:
            raise AppError(
                code="RECOVERY_STALE",
                message=(
                    "备份准备完成后现库已发生变化（存在新的补录或其他恢复已成功），"
                    "为避免覆盖新数据，本次确认被拒绝，现库保持不变"
                ),
                status_code=409,
                details={
                    "backup_id": backup_id,
                    "prepared_against_max_revision": entry["prepared_against_max_revision"],
                    "current_max_revision": _live_max_revision(),
                },
            )

        # 切换前重新校验一次暂存文件，防止磁盘上的备份被掉包/损坏。
        summary = validate_backup_file(backup_path)
        if summary["content_fingerprint"] != entry["content_fingerprint"]:
            raise AppError(
                code="BACKUP_INTEGRITY_FAILED",
                message="暂存备份内容与上传时不一致，拒绝恢复",
                status_code=400,
            )

        # 标记 switching 并持久化：崩溃后启动对账据此判断。
        entry["status"] = "switching"
        entry["switch_started_at"] = _utcnow()
        with _index_cursor() as data:
            data["backups"][backup_id] = entry

        try:
            result = _atomic_switch(backup_path, entry, summary)
        except Exception:
            # 切换事务回滚后现库仍是完整旧数据；状态退回 staged 允许重试。
            with _index_cursor() as data:
                cur = data["backups"].get(backup_id)
                if cur is not None and cur["status"] == "switching":
                    cur["status"] = "staged"
                    cur.pop("switch_started_at", None)
            raise

        with _index_cursor() as data:
            data["backups"][backup_id] = result
        return _public_entry(result)


def _atomic_switch(backup_path: Path, entry: dict, summary: dict) -> dict:
    """在单个事务内用暂存备份整体替换现库归属数据。

    事务要么完整提交（现库 == 备份），要么完整回滚（现库 == 旧数据），
    SQLite 的事务原子性保证进程崩溃时同样只有这两种结局。
    """
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attached = False
        try:
            conn.execute("ATTACH DATABASE ? AS backup", (str(backup_path),))
            attached = True
            conn.execute("DELETE FROM attribution_segments")
            conn.execute("DELETE FROM revisions")
            conn.execute(
                """
                INSERT INTO revisions
                    (revision_id, created_at, entry_count, note)
                SELECT revision_id, created_at, entry_count, note
                FROM backup.revisions
                """
            )
            conn.execute(
                """
                INSERT INTO attribution_segments
                    (id, revision_id, employee_id, cost_center,
                     effective_from, effective_to)
                SELECT id, revision_id, employee_id, cost_center,
                       effective_from, effective_to
                FROM backup.attribution_segments
                """
            )
            # 重置 AUTOINCREMENT 序列：后续修订从备份最大号继续递增。
            # sqlite_sequence 仅在出现过 AUTOINCREMENT 行时才存在，需先判断。
            has_seq = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()
            if has_seq:
                # sqlite_sequence 为系统表，不支持 ON CONFLICT 语法，先删后插。
                conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('revisions', 'attribution_segments')")
                conn.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES('revisions', ?)",
                    (summary["max_revision_id"] or 0,),
                )
                seg_max = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM attribution_segments"
                ).fetchone()["m"]
                conn.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES('attribution_segments', ?)",
                    (seg_max,),
                )

            # 提交前在同一事务内核验结果与备份完全一致。
            new_fp = _fingerprint_conn(conn)
            if new_fp != summary["content_fingerprint"]:
                raise AppError(
                    code="RECOVERY_SWITCH_FAILED",
                    message="切换后内容指纹与备份不一致，回滚",
                    status_code=500,
                )
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM revisions) AS r, "
                "(SELECT COUNT(*) FROM attribution_segments) AS s"
            ).fetchone()
            if counts["r"] != summary["revision_count"] or \
                    counts["s"] != summary["segment_count"]:
                raise AppError(
                    code="RECOVERY_SWITCH_FAILED",
                    message="切换后行数与备份不一致，回滚",
                    status_code=500,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            # 事务内 DETACH 可能因备份库仍被事务引用而失败；
            # 放到提交/回滚之后摘除，连接随即关闭，残留也无影响。
            if attached:
                try:
                    conn.execute("DETACH DATABASE backup")
                except sqlite3.DatabaseError:
                    pass

        # 提交成功后尽量把 WAL 落盘（失败不影响正确性，下次连接仍可恢复）。
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass

    now = _utcnow()
    finished = dict(entry)
    finished.update(
        {
            "status": "confirmed",
            "confirmed_at": now,
            "switch_completed_at": now,
            "recovery": {
                "restored_revision_count": summary["revision_count"],
                "restored_segment_count": summary["segment_count"],
                "restored_min_revision_id": summary["min_revision_id"],
                "restored_max_revision_id": summary["max_revision_id"],
                "next_revision_id": (summary["max_revision_id"] or 0) + 1,
                "content_fingerprint": summary["content_fingerprint"],
            },
        }
    )
    finished.pop("switch_started_at", None)
    return finished


# --------------------------------------------------------------------------- #
# 查询与启动对账
# --------------------------------------------------------------------------- #

def get_backup(backup_id: str) -> dict:
    return _public_entry(_get_entry(backup_id))


def list_backups() -> list[dict]:
    data = _read_index()
    return [
        _public_entry(data["backups"][bid])
        for bid in sorted(data["backups"].keys())
    ]


def _public_entry(entry: dict) -> dict:
    """对外视图：去掉 token_hash 等内部字段。"""
    out = {k: v for k, v in entry.items() if k != "token_hash"}
    return out


def reconcile_on_startup() -> None:
    """启动时修复崩溃在切换途中的记录。

    - switching 且现库指纹 == 备份指纹：切换实际已提交，记为 confirmed；
    - switching 且指纹不一致：事务已回滚/未提交，退回 staged 等待重新确认。
    """
    if not _index_path().exists():
        return
    with _index_lock:
        data = _read_index()
        changed = False
        for backup_id, entry in list(data["backups"].items()):
            if entry.get("status") != "switching":
                continue
            backup_path = recovery_dir() / backup_id / "backup.db"
            try:
                if backup_path.exists():
                    summary = validate_backup_file(backup_path)
                    fp_match = summary["content_fingerprint"] == live_fingerprint()
                else:
                    fp_match = False
                if fp_match:
                    entry["status"] = "confirmed"
                    now = _utcnow()
                    entry.setdefault("confirmed_at", now)
                    entry["switch_completed_at"] = now
                    entry["recovery"] = {
                        "restored_revision_count": summary["revision_count"],
                        "restored_segment_count": summary["segment_count"],
                        "restored_min_revision_id": summary["min_revision_id"],
                        "restored_max_revision_id": summary["max_revision_id"],
                        "next_revision_id": (summary["max_revision_id"] or 0) + 1,
                        "content_fingerprint": summary["content_fingerprint"],
                    }
                    entry.pop("switch_started_at", None)
                else:
                    entry["status"] = "staged"
                    entry.pop("switch_started_at", None)
            except BackupRejected:
                # 备份本身已损坏：退回 staged，确认时会再次报错，现库不动。
                entry["status"] = "staged"
                entry.pop("switch_started_at", None)
            changed = True
        if changed:
            _write_index(data)

def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
