"""SQLite 连接与建表。

设计上只追加（append-only）：revisions / attribution_segments 两张表均不做
UPDATE/DELETE，任一历史修订的可见数据因此永久不变。

事务边界全部显式管理（``isolation_level=None`` 即 SQLite autocommit 模式）：

- 写路径（补录提交、恢复切换）使用 ``BEGIN IMMEDIATE``；
- 读路径使用 ``BEGIN`` 开启延迟读事务，把 WAL 快照固定到整次请求结束，
  因此恢复切换提交的瞬间，进行中的查询要么全程读旧快照、要么全程读新快照，
  单次请求不会读到新旧混合的数据。
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


def _connect(db_path: Path, *, uri: str | None = None, readonly: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    target = uri if uri is not None else str(db_path)
    conn = sqlite3.connect(
        target,
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
        # 关闭 Python 层隐式事务，事务语句全部由业务代码显式发出。
        isolation_level=None,
        uri=uri is not None,
    )
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发；外键约束开启。
    # 只读连接不能修改 journal_mode（会触发写操作），仅保留 busy_timeout。
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接：调用方自行管理显式事务；退出时关闭连接。"""
    conn = _connect(get_db_path())
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def open_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """以只读 URI 打开一个不受信任的 SQLite 文件（备份校验专用）。

    mode=ro 保证任何语句都无法写入该文件；trusted_schema=OFF 禁止加载
    文件内可能携带的恶意 schema 触发器/视图逻辑。
    """
    conn = _connect(
        path,
        uri=f"file:{path.as_posix()}?mode=ro",
        readonly=True,
    )
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        yield conn
    finally:
        conn.close()


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """固定一次读请求的 WAL 快照：BEGIN 与 COMMIT 之间所有 SELECT 同属一个读事务。"""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("COMMIT")


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
    """创建表与索引（幂等）。

    SCHEMA_SQL 全部为 CREATE ... IF NOT EXISTS，语句本身幂等；
    注意 ``executescript`` 会隐式提交当前事务，故不额外包裹 BEGIN。
    """
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
