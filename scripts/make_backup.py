#!/usr/bin/env python3
"""离线备份工具（受信任侧使用）：用 SQLite Online Backup API 生成/损坏备份样例。

本脚本运行在运维/人事侧，不读取任何上传数据，仅用于：

1. ``--sample``      构造一个带示例修订的库并 backup 成备份文件（演示与测试用）；
2. ``--from-db PATH`` 对现有归属库执行 SQLite ``Connection.backup()`` 生成离线备份；
3. ``--tamper MODE``  在样例备份基础上制造非法变体，演示服务端的拒绝校验。

用法示例：
    python scripts/make_backup.py --sample /tmp/backup.db
    python scripts/make_backup.py --from-db ./data/attribution.db /tmp/backup.db
    python scripts/make_backup.py --sample /tmp/bad.db --tamper corrupt-header
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

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

SAMPLE_REVISIONS = [
    (1, "2026-01-05T09:00:00.000Z", 2, "HR 初始归属 Q1"),
    (2, "2026-04-03T09:00:00.000Z", 1, "HR 调岗补录：E001 四月起转广州成本中心"),
]
SAMPLE_SEGMENTS = [
    # revision 1：两名员工 Q1 归属
    (1, 1, "E001", "CC-SH-01", "2026-01-01", "2026-04-01"),
    (2, 1, "E002", "CC-BJ-01", "2026-01-01", "2026-04-01"),
    # revision 2：E001 调岗补录 4~6 月
    (3, 2, "E001", "CC-GZ-02", "2026-04-01", "2026-07-01"),
]


def build_sample_source(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    src = sqlite3.connect(str(path))
    try:
        src.execute("PRAGMA journal_mode=DELETE")
        src.executescript(SCHEMA_SQL)
        src.executemany(
            "INSERT INTO revisions (revision_id, created_at, entry_count, note) "
            "VALUES (?, ?, ?, ?)",
            SAMPLE_REVISIONS,
        )
        src.executemany(
            "INSERT INTO attribution_segments "
            "(id, revision_id, employee_id, cost_center, effective_from, effective_to) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            SAMPLE_SEGMENTS,
        )
        src.commit()
    finally:
        src.close()


def sqlite_backup(src_path: Path, dest_path: Path) -> None:
    """用 SQLite Online Backup API 把 src 热备份到 dest（可在库有连接时使用）。"""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()
    source = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest_path))
    try:
        # 分页备份，模拟真实大库的 online backup 调用方式。
        source.backup(target, pages=1024)
        target.execute("PRAGMA journal_mode=DELETE")
        target.commit()
    finally:
        target.close()
        source.close()


def tamper(path: Path, mode: str) -> None:
    if mode == "corrupt-header":
        data = path.read_bytes()
        path.write_bytes(b"NOT-A-SQLITE-DATABASE!!" + data[20:])
    elif mode == "integrity":
        # 截断尾部页面，制造完整性校验失败。
        data = path.read_bytes()
        path.write_bytes(data[: max(1024, len(data) // 2)])
    elif mode == "extra-table":
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE evil (id INTEGER PRIMARY KEY, payload TEXT)")
            conn.execute("INSERT INTO evil VALUES (1, 'unexpected table')")
            conn.commit()
        finally:
            conn.close()
    elif mode == "missing-fk":
        conn = sqlite3.connect(str(path))
        try:
            # 重建无外键声明的 segments 表
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(
                """
                CREATE TABLE attribution_segments_nofk (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision_id INTEGER NOT NULL,
                    employee_id TEXT NOT NULL,
                    cost_center TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT NOT NULL,
                    CHECK (effective_from < effective_to)
                );
                INSERT INTO attribution_segments_nofk
                  SELECT * FROM attribution_segments;
                DROP TABLE attribution_segments;
                ALTER TABLE attribution_segments_nofk RENAME TO attribution_segments;
                """
            )
            conn.commit()
        finally:
            conn.close()
    elif mode == "orphan-fk":
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "INSERT INTO attribution_segments "
                "(id, revision_id, employee_id, cost_center, effective_from, effective_to) "
                "VALUES (999, 999, 'E001', '-CC-', '2026-01-01', '2026-02-01')"
            )
            conn.commit()
        finally:
            conn.close()
    elif mode == "row-count":
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("UPDATE revisions SET entry_count = 99 WHERE revision_id = 1")
            conn.commit()
        finally:
            conn.close()
    elif mode == "revision-gap":
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("DELETE FROM revisions WHERE revision_id = 1")
            conn.execute("DELETE FROM attribution_segments WHERE revision_id = 1")
            conn.commit()
        finally:
            conn.close()
    elif mode == "bad-range":
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(
                "INSERT INTO revisions (revision_id, entry_count, note) VALUES (3, 1, 'bad')"
            )
            conn.execute(
                "INSERT INTO attribution_segments "
                "(id, revision_id, employee_id, cost_center, effective_from, effective_to) "
                "VALUES (100, 3, 'E009', 'CC-X', '2026-05-01', '2026-05-01')"
            )
            conn.commit()
        finally:
            conn.close()
    elif mode == "overlap":
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "INSERT INTO revisions (revision_id, entry_count, note) VALUES (3, 2, 'overlap')"
            )
            conn.executemany(
                "INSERT INTO attribution_segments "
                "(id, revision_id, employee_id, cost_center, effective_from, effective_to) "
                "VALUES (?, 3, 'E009', ?, ?, ?)",
                [
                    (101, "CC-A", "2026-05-01", "2026-08-01"),
                    (102, "CC-B", "2026-07-01", "2026-09-01"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
    else:
        raise SystemExit(f"未知 tamper 模式：{mode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite backup 生成/损坏样例工具")
    parser.add_argument("output", type=Path, help="输出备份文件路径")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sample", action="store_true", help="生成内置示例库的备份")
    group.add_argument("--from-db", type=Path, help="对现有归属库执行 online backup")
    parser.add_argument(
        "--tamper",
        choices=[
            "corrupt-header", "integrity", "extra-table", "missing-fk",
            "orphan-fk", "row-count", "revision-gap", "bad-range", "overlap",
        ],
        help="在生成的备份上制造非法变体（与 --sample 配合）",
    )
    args = parser.parse_args()

    if args.sample:
        source_path = args.output.with_suffix(".source.db")
        build_sample_source(source_path)
        sqlite_backup(source_path, args.output)
        source_path.unlink(missing_ok=True)
    else:
        if not args.from_db.exists():
            raise SystemExit(f"源数据库不存在：{args.from_db}")
        sqlite_backup(args.from_db, args.output)

    if args.tamper:
        tamper(args.output, args.tamper)

    print(f"已生成备份：{args.output}（{args.output.stat().st_size} 字节）")
    if args.tamper:
        print(f"已施加损坏模式：{args.tamper}（上传后应被服务端拒绝）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
