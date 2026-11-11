"""SQLite 连接与建表。

设计上只追加（append-only）：revisions / attribution_segments 两张表均不做
UPDATE/DELETE，任一历史修订的可见数据因此永久不变。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 允许通过环境变量指定数据库文件路径（测试与 Docker 都会用到）。
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "attribution.db"


def get_db_path() -> Path:
    return Path(os.environ.get("ATTRIBUTION_DB_PATH", str(DEFAULT_DB_PATH)))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发；外键约束开启。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接：正常结束提交，异常回滚并关闭。"""
    conn = _connect(get_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS revisions (
    revision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    entry_count      INTEGER NOT NULL,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS attribution_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id     INTEGER NOT NULL REFERENCES revisions(revision_id),
    employee_id     TEXT    NOT NULL,
    cost_center     TEXT    NOT NULL,
    effective_from  TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间左端点（含）
    effective_to    TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间右端点（不含）
    CHECK (effective_from < effective_to)
);

-- 查询路径：按员工 + 日期点 + 修订号过滤后取最大修订号
CREATE INDEX IF NOT EXISTS idx_segments_emp_dates_rev
    ON attribution_segments (employee_id, effective_from, effective_to, revision_id);

CREATE INDEX IF NOT EXISTS idx_segments_revision
    ON attribution_segments (revision_id);
"""


def init_db() -> None:
    """创建表与索引（幂等）。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
