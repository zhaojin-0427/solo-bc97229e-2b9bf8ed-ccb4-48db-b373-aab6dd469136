"""备份恢复端到端测试：暂存校验、原子切换、幂等、并发与重启持久化。

运行：ATTRIBUTION_DB_PATH 指向临时文件后 pytest（由 fixture 自动设置）。
"""
from __future__ import annotations

import importlib
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

SCHEMA_SQL = """
CREATE TABLE revisions (
    revision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    entry_count      INTEGER NOT NULL,
    note             TEXT
);
CREATE TABLE attribution_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id     INTEGER NOT NULL REFERENCES revisions(revision_id),
    employee_id     TEXT    NOT NULL,
    cost_center     TEXT    NOT NULL,
    effective_from  TEXT    NOT NULL,
    effective_to    TEXT    NOT NULL,
    CHECK (effective_from < effective_to)
);
CREATE INDEX idx_segments_emp_dates_rev
    ON attribution_segments (employee_id, effective_from, effective_to, revision_id);
CREATE INDEX idx_segments_revision ON attribution_segments (revision_id);
"""


def _new_db(path):
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA_SQL)
    return conn


def make_backup(path, revisions, segments, *, with_backup_api=True):
    """构造受信任备份：先建源库，再用 SQLite Online Backup API 拷出。"""
    source = path.with_suffix(".source.db")
    conn = _new_db(source)
    try:
        conn.executemany(
            "INSERT INTO revisions (revision_id, created_at, entry_count, note) "
            "VALUES (?, ?, ?, ?)",
            revisions,
        )
        conn.executemany(
            "INSERT INTO attribution_segments "
            "(id, revision_id, employee_id, cost_center, effective_from, effective_to) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            segments,
        )
        conn.commit()
    finally:
        conn.close()

    path.unlink(missing_ok=True)
    if with_backup_api:
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(str(path))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        source.unlink(missing_ok=True)
    else:
        source.rename(path)
    return path


GOOD_REVISIONS = [
    (1, "2026-01-05T09:00:00.000Z", 2, "HR 初始归属 Q1"),
    (2, "2026-04-03T09:00:00.000Z", 1, "HR 调岗补录"),
]
GOOD_SEGMENTS = [
    (1, 1, "E001", "CC-SH-01", "2026-01-01", "2026-04-01"),
    (2, 1, "E002", "CC-BJ-01", "2026-01-01", "2026-04-01"),
    (3, 2, "E001", "CC-GZ-02", "2026-04-01", "2026-07-01"),
]


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))

    import app.database as database
    import app.recovery as recovery
    import app.service as service
    import app.main as main

    importlib.reload(database)
    importlib.reload(recovery)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield {
            "client": c,
            "tmp": tmp_path,
            "db_file": db_file,
            "database": database,
            "recovery": recovery,
            "main": main,
        }


@pytest.fixture()
def client(ctx):
    return ctx["client"]


def _upload(client, path, **kwargs):
    with path.open("rb") as fh:
        return client.post(
            "/api/recovery/backups",
            files={"file": (path.name, fh, "application/octet-stream")},
            data=kwargs,
        )


def _seed_live_revisions(client, n=1):
    """在现库制造 n 个修订（默认 1 个），返回最大修订号。"""
    for i in range(n):
        r = client.post("/api/revisions", json={
            "note": f"live-{i}",
            "entries": [{
                "employee_id": "E900", "cost_center": "CC-LIVE",
                "effective_from": "2026-01-01", "effective_to": "2026-02-01",
            }],
        })
        assert r.status_code == 201
    return n


# --------------------------------------------------------------------------- #
# 上传与校验
# --------------------------------------------------------------------------- #

def test_upload_valid_backup_staged_with_token(client, ctx):
    backup = ctx["tmp"] / "good.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)

    r = _upload(client, backup, note="离线备份批次 B1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "staged"
    assert body["backup_id"].startswith("bk-")
    assert len(body["recovery_token"]) > 20
    assert body["revision_count"] == 2
    assert body["segment_count"] == 3
    assert body["min_revision_id"] == 1 and body["max_revision_id"] == 2
    assert body["prepared_against_max_revision"] == 0

    # 暂存不修改现库
    assert client.get("/api/revisions").json() == []
    # 列表/详情可查
    listing = client.get("/api/recovery/backups").json()
    assert len(listing) == 1 and listing[0]["status"] == "staged"
    assert "recovery_token" not in listing[0]  # 列表不泄露凭据
    detail = client.get(f"/api/recovery/backups/{body['backup_id']}").json()
    assert detail["sha256"] == body["sha256"]


def test_upload_empty_file_rejected(client, ctx):
    p = ctx["tmp"] / "empty.db"
    p.write_bytes(b"")
    r = _upload(client, p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_EMPTY"


def test_upload_non_sqlite_rejected(client, ctx):
    p = ctx["tmp"] / "fake.db"
    p.write_bytes(b"this is definitely not a sqlite database file" * 10)
    r = _upload(client, p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_NOT_SQLITE"
    # 非法备份不留暂存目录
    assert not list((ctx["tmp"] / "recovery").glob("bk-*"))


def test_upload_truncated_db_integrity_failed(client, ctx):
    backup = ctx["tmp"] / "trunc.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    data = backup.read_bytes()
    backup.write_bytes(data[: len(data) // 2])
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_INTEGRITY_FAILED"


def test_upload_extra_table_rejected(client, ctx):
    backup = ctx["tmp"] / "extra.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    conn = sqlite3.connect(str(backup))
    conn.execute("CREATE TABLE evil (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_SCHEMA_MISMATCH"


def test_upload_missing_fk_declaration_rejected(client, ctx):
    backup = ctx["tmp"] / "nofk.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    conn = sqlite3.connect(str(backup))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        CREATE TABLE seg_nofk (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER NOT NULL,
            employee_id TEXT NOT NULL, cost_center TEXT NOT NULL,
            effective_from TEXT NOT NULL, effective_to TEXT NOT NULL,
            CHECK (effective_from < effective_to)
        );
        INSERT INTO seg_nofk SELECT * FROM attribution_segments;
        DROP TABLE attribution_segments;
        ALTER TABLE seg_nofk RENAME TO attribution_segments;
        """
    )
    conn.commit()
    conn.close()
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_SCHEMA_MISMATCH"


def test_upload_orphan_foreign_key_rejected(client, ctx):
    backup = ctx["tmp"] / "orphan.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    conn = sqlite3.connect(str(backup))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO attribution_segments VALUES (99, 99, 'E1', 'CC', '2026-01-01', '2026-02-01')"
    )
    conn.commit()
    conn.close()
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_FOREIGN_KEY_VIOLATION"


def test_upload_row_count_mismatch_rejected(client, ctx):
    backup = ctx["tmp"] / "count.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    conn = sqlite3.connect(str(backup))
    conn.execute("UPDATE revisions SET entry_count = 99 WHERE revision_id = 1")
    conn.commit()
    conn.close()
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_ROW_COUNT_MISMATCH"


def test_upload_revision_gap_rejected(client, ctx):
    backup = ctx["tmp"] / "gap.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    conn = sqlite3.connect(str(backup))
    conn.execute("DELETE FROM attribution_segments WHERE revision_id = 1")
    conn.execute("DELETE FROM revisions WHERE revision_id = 1")
    conn.commit()
    conn.close()
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_REVISION_GAP"


def test_upload_bad_date_range_rejected(client, ctx):
    backup = ctx["tmp"] / "badrange.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    conn = sqlite3.connect(str(backup))
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute("INSERT INTO revisions VALUES (3, '2026-05-01T00:00:00.000Z', 1, 'x')")
    conn.execute(
        "INSERT INTO attribution_segments VALUES (10, 3, 'E9', 'CC', '2026-05-01', '2026-05-01')"
    )
    conn.commit()
    conn.close()
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_INVALID_DATE_RANGE"


def test_upload_overlapping_segments_rejected(client, ctx):
    backup = ctx["tmp"] / "overlap.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    conn = sqlite3.connect(str(backup))
    conn.execute("INSERT INTO revisions VALUES (3, '2026-05-01T00:00:00.000Z', 2, 'x')")
    conn.executemany(
        "INSERT INTO attribution_segments VALUES (?, 3, 'E9', ?, ?, ?)",
        [
            (11, "CC-A", "2026-05-01", "2026-08-01"),
            (12, "CC-B", "2026-07-01", "2026-09-01"),
        ],
    )
    conn.commit()
    conn.close()
    r = _upload(client, backup)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_OVERLAPPING_SEGMENTS"


def test_invalid_upload_does_not_touch_live_db(client, ctx):
    # 现库先有一个修订
    _seed_live_revisions(client)
    before = client.get("/api/revisions").json()

    p = ctx["tmp"] / "fake.db"
    p.write_bytes(b"X" * 100)
    assert _upload(client, p).status_code == 400
    # 现库完全不变
    assert client.get("/api/revisions").json() == before


def test_empty_backup_accepted(client, ctx):
    # 只含两张空表的备份也是合法备份（恢复为空归属库）
    backup = ctx["tmp"] / "empty-valid.db"
    make_backup(backup, [], [])
    r = _upload(client, backup)
    assert r.status_code == 201
    body = r.json()
    assert body["revision_count"] == 0 and body["segment_count"] == 0
    assert body["min_revision_id"] is None and body["max_revision_id"] is None


# --------------------------------------------------------------------------- #
# 确认恢复
# --------------------------------------------------------------------------- #

def _stage_good(client, ctx):
    backup = ctx["tmp"] / "good.db"
    make_backup(backup, GOOD_REVISIONS, GOOD_SEGMENTS)
    r = _upload(client, backup)
    assert r.status_code == 201, r.text
    return r.json()


def test_confirm_replaces_data_and_continues_sequence(client, ctx):
    _seed_live_revisions(client)  # 现库修订 1（E900）
    staged = _stage_good(client, ctx)

    r = client.post(
        f"/api/recovery/backups/{staged['backup_id']}/confirm",
        json={"recovery_token": staged["recovery_token"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["recovery"]["restored_revision_count"] == 2
    assert body["recovery"]["restored_max_revision_id"] == 2
    assert body["recovery"]["next_revision_id"] == 3

    # 现库已整体替换：E900 消失，备份数据生效
    revisions = client.get("/api/revisions").json()
    assert [x["revision_id"] for x in revisions] == [1, 2]
    q = client.post("/api/attribution/query", json={
        "revision_id": 2,
        "records": [
            {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
            {"employee_id": "E900", "work_date": "2026-01-15", "hours": 8},
        ],
    }).json()
    assert q[0]["cost_center"] == "CC-GZ-02" and q[0]["matched_revision_id"] == 2
    assert q[1]["attributed"] is False  # 旧库数据已不存在

    # 后续修订从备份最大号 +1 继续递增
    nxt = client.post("/api/revisions", json={
        "entries": [{
            "employee_id": "E003", "cost_center": "CC-SZ",
            "effective_from": "2026-08-01", "effective_to": "2026-09-01",
        }]
    })
    assert nxt.status_code == 201
    assert nxt.json()["revision_id"] == 3


def test_confirm_wrong_token_403_and_live_unchanged(client, ctx):
    _seed_live_revisions(client)
    staged = _stage_good(client, ctx)
    r = client.post(
        f"/api/recovery/backups/{staged['backup_id']}/confirm",
        json={"recovery_token": "wrong-token"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "RECOVERY_TOKEN_INVALID"
    # 现库未变
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [1]
    detail = client.get(f"/api/recovery/backups/{staged['backup_id']}").json()
    assert detail["status"] == "staged"


def test_confirm_unknown_backup_404(client):
    r = client.post(
        "/api/recovery/backups/bk-doesnotexist/confirm",
        json={"recovery_token": "x" * 20},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BACKUP_NOT_FOUND"


def test_confirm_stale_after_backfill_rejected(client, ctx):
    staged = _stage_good(client, ctx)
    # 准备之后发生补录
    _seed_live_revisions(client)
    r = client.post(
        f"/api/recovery/backups/{staged['backup_id']}/confirm",
        json={"recovery_token": staged["recovery_token"]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RECOVERY_STALE"
    # 现库保持补录后的状态，备份仍可查（staged）
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [1]
    detail = client.get(f"/api/recovery/backups/{staged['backup_id']}").json()
    assert detail["status"] == "staged"


def test_confirm_stale_after_another_recovery_rejected(client, ctx):
    # 第一个备份（2 修订）准备
    first = _stage_good(client, ctx)

    # 第二个备份（1 修订）准备并先确认成功
    backup2 = ctx["tmp"] / "good2.db"
    make_backup(
        backup2,
        [(1, "2026-01-01T00:00:00.000Z", 1, "only one")],
        [(1, 1, "E777", "CC-777", "2026-01-01", "2026-03-01")],
    )
    second = _upload(client, backup2).json()
    ok = client.post(
        f"/api/recovery/backups/{second['backup_id']}/confirm",
        json={"recovery_token": second["recovery_token"]},
    )
    assert ok.status_code == 200

    # 第一个备份此时已过期
    r = client.post(
        f"/api/recovery/backups/{first['backup_id']}/confirm",
        json={"recovery_token": first["recovery_token"]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RECOVERY_STALE"
    # 现库是第二次恢复的内容
    q = client.post("/api/attribution/query", json={
        "revision_id": 1,
        "records": [{"employee_id": "E777", "work_date": "2026-02-01", "hours": 8}],
    }).json()
    assert q[0]["cost_center"] == "CC-777"


def test_confirm_is_idempotent_returns_original_result(client, ctx):
    staged = _stage_good(client, ctx)
    url = f"/api/recovery/backups/{staged['backup_id']}/confirm"
    payload = {"recovery_token": staged["recovery_token"]}

    first = client.post(url, json=payload)
    assert first.status_code == 200
    confirmed_at = first.json()["confirmed_at"]

    # 成功后再补录，重复确认也必须返回原结果而不是再次恢复
    _seed_live_revisions(client)
    second = client.post(url, json=payload)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["confirmed_at"] == confirmed_at
    # 补录的修订 3 仍然存在（没有被第二次确认抹掉）
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [1, 2, 3]


def test_confirm_idempotent_wrong_token_still_403(client, ctx):
    staged = _stage_good(client, ctx)
    url = f"/api/recovery/backups/{staged['backup_id']}/confirm"
    assert client.post(url, json={"recovery_token": staged["recovery_token"]}).status_code == 200
    r = client.post(url, json={"recovery_token": "nope"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# 重启持久化与崩溃对账
# --------------------------------------------------------------------------- #

def test_staged_backup_survives_restart(client, ctx):
    staged = _stage_good(client, ctx)
    backup_id = staged["backup_id"]

    # 模拟重启：重新加载模块并执行启动对账
    import app.database as database
    import app.recovery as recovery
    import app.service as service
    import app.main as main
    importlib.reload(database)
    importlib.reload(recovery)
    importlib.reload(service)
    importlib.reload(main)
    recovery.reconcile_on_startup()
    with TestClient(main.app) as c2:
        detail = c2.get(f"/api/recovery/backups/{backup_id}").json()
        assert detail["status"] == "staged"
        # 重启后仍可凭原凭据确认
        r = c2.post(
            f"/api/recovery/backups/{backup_id}/confirm",
            json={"recovery_token": staged["recovery_token"]},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "confirmed"


def test_confirmed_result_survives_restart(client, ctx):
    staged = _stage_good(client, ctx)
    url = f"/api/recovery/backups/{staged['backup_id']}/confirm"
    client.post(url, json={"recovery_token": staged["recovery_token"]})

    import app.database as database
    import app.recovery as recovery
    import app.service as service
    import app.main as main
    importlib.reload(database)
    importlib.reload(recovery)
    importlib.reload(service)
    importlib.reload(main)
    recovery.reconcile_on_startup()
    with TestClient(main.app) as c2:
        detail = c2.get(f"/api/recovery/backups/{staged['backup_id']}").json()
        assert detail["status"] == "confirmed"
        assert detail["recovery"]["next_revision_id"] == 3
        # 幂等仍成立
        again = c2.post(url, json={"recovery_token": staged["recovery_token"]})
        assert again.status_code == 200
        assert again.json()["status"] == "confirmed"


def test_switching_crash_after_commit_reconciles_as_confirmed(client, ctx):
    staged = _stage_good(client, ctx)
    recovery = ctx["recovery"]

    # 手动把状态置为 switching，并让现库内容 == 备份（模拟提交后崩溃）
    with recovery._index_cursor() as data:
        data["backups"][staged["backup_id"]]["status"] = "switching"

    import sqlite3 as _sq
    bk = ctx["tmp"] / "recovery" / staged["backup_id"] / "backup.db"
    live = ctx["db_file"]
    # 用 backup API 把备份内容复制到现库（等同切换已提交后的磁盘状态）
    src = _sq.connect(f"file:{bk.as_posix()}?mode=ro", uri=True)
    dst = _sq.connect(str(live))
    try:
        with dst:
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.execute("DELETE FROM attribution_segments")
            dst.execute("DELETE FROM revisions")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    import app.database as database
    import app.recovery as recovery2
    import app.service as service
    import app.main as main
    importlib.reload(database)
    importlib.reload(recovery2)
    importlib.reload(service)
    importlib.reload(main)
    recovery2.reconcile_on_startup()
    with TestClient(main.app) as c2:
        detail = c2.get(f"/api/recovery/backups/{staged['backup_id']}").json()
        assert detail["status"] == "confirmed"
        assert detail["recovery"]["restored_revision_count"] == 2


def test_switching_crash_before_commit_reconciles_back_to_staged(client, ctx):
    # 准备时现库为空；模拟切换事务未提交即崩溃：状态停在 switching，磁盘仍是旧空库。
    staged = _stage_good(client, ctx)
    recovery = ctx["recovery"]
    with recovery._index_cursor() as data:
        data["backups"][staged["backup_id"]]["status"] = "switching"

    import app.database as database
    import app.recovery as recovery2
    import app.service as service
    import app.main as main
    importlib.reload(database)
    importlib.reload(recovery2)
    importlib.reload(service)
    importlib.reload(main)
    recovery2.reconcile_on_startup()
    with TestClient(main.app) as c2:
        detail = c2.get(f"/api/recovery/backups/{staged['backup_id']}").json()
        assert detail["status"] == "staged"
        # 旧数据完整（仍为空库）
        assert c2.get("/api/revisions").json() == []
        # 可以重新确认（指纹未变，确认成功）
        r = c2.post(
            f"/api/recovery/backups/{staged['backup_id']}/confirm",
            json={"recovery_token": staged["recovery_token"]},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "confirmed"
        assert [x["revision_id"] for x in c2.get("/api/revisions").json()] == [1, 2]


# --------------------------------------------------------------------------- #
# 并发：快照隔离与写互斥
# --------------------------------------------------------------------------- #

def test_query_snapshot_never_mixes_generations(client, ctx):
    # 现库放修订 1
    _seed_live_revisions(client)
    staged = _stage_good(client, ctx)
    url = f"/api/recovery/backups/{staged['backup_id']}/confirm"
    token = staged["recovery_token"]

    barrier = threading.Barrier(2)
    seen_generations = []
    errors = []

    def query_loop():
        try:
            for _ in range(30):
                barrier.wait()
                body = {
                    "revision_id": 1,
                    "records": [
                        {"employee_id": "E900", "work_date": "2026-01-15", "hours": 8},
                        {"employee_id": "E001", "work_date": "2026-02-15", "hours": 8},
                    ],
                }
                r = client.post("/api/attribution/query", json=body).json()
                e900 = r[0]["attributed"]   # 仅旧库有
                e001 = r[1]["attributed"]   # 仅备份有
                # 每次整查询必须是同一代：二者不可能同时归属
                assert not (e900 and e001)
                seen_generations.append(("old" if e900 else "new"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=query_loop)
    t.start()
    for _ in range(30):
        barrier.wait()
    # 恢复切换
    r = client.post(url, json={"recovery_token": token})
    assert r.status_code == 200
    t.join()
    assert not errors
    assert "old" in seen_generations or "new" in seen_generations
    # 切换后稳定为新一代
    after = client.post("/api/attribution/query", json={
        "revision_id": 2,
        "records": [{"employee_id": "E001", "work_date": "2026-02-15", "hours": 8}],
    }).json()
    assert after[0]["cost_center"] == "CC-SH-01"


def test_backfill_concurrent_with_confirm_one_wins(client, ctx):
    staged = _stage_good(client, ctx)
    url = f"/api/recovery/backups/{staged['backup_id']}/confirm"
    token = staged["recovery_token"]
    start = threading.Event()
    outcomes = []

    def do_backfill():
        start.wait()
        r = client.post("/api/revisions", json={
            "entries": [{
                "employee_id": "E555", "cost_center": "CC-555",
                "effective_from": "2026-01-01", "effective_to": "2026-02-01",
            }]
        })
        outcomes.append(("backfill", r.status_code))

    def do_confirm():
        start.wait()
        r = client.post(url, json={"recovery_token": token})
        outcomes.append(("confirm", r.status_code))

    threads = [threading.Thread(target=do_backfill), threading.Thread(target=do_confirm)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    statuses = dict(outcomes)
    assert statuses["backfill"] == 201
    # 补录先拿锁 -> 确认 409；确认先拿锁 -> 确认 200
    assert statuses["confirm"] in (200, 409)
    if statuses["confirm"] == 409:
        # 现库保留补录数据，备份仍 staged，可重新准备
        assert client.get(f"/api/recovery/backups/{staged['backup_id']}").json()["status"] == "staged"
        revs = client.get("/api/revisions").json()
        assert revs[-1]["revision_id"] == 1
    else:
        # 确认成功：备份数据完整，补录被整体替换掉
        q = client.post("/api/attribution/query", json={
            "revision_id": 2,
            "records": [{"employee_id": "E555", "work_date": "2026-01-15", "hours": 8}],
        }).json()
        assert q[0]["attributed"] is False


def test_upload_validation_runs_while_api_still_serving(client, ctx):
    # 校验期间查询与补录继续可用：先制造大备份，上传同时跑查询
    revs = [(i, f"2026-01-0{i}T00:00:00.000Z", 1, f"r{i}") for i in range(1, 11)]
    segs = [
        (i, i, f"E{i:03d}", f"CC-{i:03d}", "2026-01-01", "2026-02-01")
        for i in range(1, 11)
    ]
    backup = ctx["tmp"] / "ten.db"
    make_backup(backup, revs, segs)

    done = threading.Event()
    query_ok = []

    def chatter():
        while not done.is_set():
            r = client.post("/api/attribution/query", json={
                "revision_id": 0,
                "records": [{"employee_id": "E001", "work_date": "2026-01-15", "hours": 8}],
            })
            query_ok.append(r.status_code)

    t = threading.Thread(target=chatter)
    t.start()
    r = _upload(client, backup)
    done.set()
    t.join()
    assert r.status_code == 201
    assert query_ok and all(code == 200 for code in query_ok)


def test_confirm_rejects_tampered_staged_file(client, ctx):
    staged = _stage_good(client, ctx)
    bk_path = ctx["tmp"] / "recovery" / staged["backup_id"] / "backup.db"
    # 模拟暂存文件在确认前被掉包为损坏内容
    data = bk_path.read_bytes()
    bk_path.write_bytes(data[: len(data) // 2])

    r = client.post(
        f"/api/recovery/backups/{staged['backup_id']}/confirm",
        json={"recovery_token": staged["recovery_token"]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BACKUP_INTEGRITY_FAILED"
    # 现库未被触碰
    assert client.get("/api/revisions").json() == []


def test_restored_data_keeps_half_open_and_time_travel_semantics(client, ctx):
    staged = _stage_good(client, ctx)
    r = client.post(
        f"/api/recovery/backups/{staged['backup_id']}/confirm",
        json={"recovery_token": staged["recovery_token"]},
    )
    assert r.status_code == 200

    # 备份：E001 修订1 [01-01,04-01)->CC-SH-01，修订2 [04-01,07-01)->CC-GZ-02
    q1 = client.post("/api/attribution/query", json={
        "revision_id": 1,
        "records": [
            {"employee_id": "E001", "work_date": "2026-03-31", "hours": 8},
            {"employee_id": "E001", "work_date": "2026-04-01", "hours": 8},
        ],
    }).json()
    assert q1[0]["cost_center"] == "CC-SH-01"
    assert q1[1]["attributed"] is False  # 修订1 口径下 4 月为空档

    q2 = client.post("/api/attribution/query", json={
        "revision_id": 2,
        "records": [
            {"employee_id": "E001", "work_date": "2026-04-01", "hours": 8},  # 左端点含
            {"employee_id": "E001", "work_date": "2026-07-01", "hours": 8},  # 右端点不含
        ],
    }).json()
    assert q2[0]["cost_center"] == "CC-GZ-02" and q2[0]["matched_revision_id"] == 2
    assert q2[1]["attributed"] is False


def test_restore_then_backfill_then_second_restore_resets_sequence_again(client, ctx):
    # 第一次恢复（备份最大号 2）
    first = _stage_good(client, ctx)
    client.post(
        f"/api/recovery/backups/{first['backup_id']}/confirm",
        json={"recovery_token": first["recovery_token"]},
    )
    # 恢复后补录到修订 4
    for _ in range(2):
        assert client.post("/api/revisions", json={
            "entries": [{
                "employee_id": "E8", "cost_center": "CC-8",
                "effective_from": "2026-09-01", "effective_to": "2026-10-01",
            }]
        }).status_code == 201
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [1, 2, 3, 4]

    # 第二次恢复同一备份：序号再次回到 2，下一个修订号仍是 3
    second = _stage_good(client, ctx)
    r = client.post(
        f"/api/recovery/backups/{second['backup_id']}/confirm",
        json={"recovery_token": second["recovery_token"]},
    )
    assert r.status_code == 200
    assert r.json()["recovery"]["next_revision_id"] == 3
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [1, 2]
    nxt = client.post("/api/revisions", json={
        "entries": [{
            "employee_id": "E9", "cost_center": "CC-9",
            "effective_from": "2026-11-01", "effective_to": "2026-12-01",
        }]
    })
    assert nxt.json()["revision_id"] == 3
