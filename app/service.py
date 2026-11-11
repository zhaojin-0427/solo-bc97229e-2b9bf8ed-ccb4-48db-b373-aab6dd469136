"""核心业务逻辑：批次校验、修订提交、按修订口径归属查询。"""
from __future__ import annotations

from collections import defaultdict
from sqlite3 import Connection

from .database import get_conn
from .errors import AppError
from .schemas import AttributionQueryRequest, AttributionResult, RevisionSubmitRequest, SegmentIn


def validate_batch(entries: list[SegmentIn]) -> None:
    """整批写入前的全部校验。任何一条不通过即抛 AppError，不触碰数据库。

    规则：
      1. 起日必须严格早于止日（左闭右开 [from, to)）；
      2. 同一员工在本批内的区间不得重叠（首尾相接允许：a.to == b.from 不重叠）。
    """
    # 规则 1：起日不早于止日 -> 拒绝（from >= to 非法）
    for idx, e in enumerate(entries):
        if e.effective_from >= e.effective_to:
            raise AppError(
                code="INVALID_DATE_RANGE",
                message=(
                    f"第 {idx + 1} 条记录起日 {e.effective_from.isoformat()} "
                    f"不早于止日 {e.effective_to.isoformat()}，左闭右开区间要求起日严格早于止日"
                ),
                details={
                    "index": idx,
                    "employee_id": e.employee_id,
                    "effective_from": e.effective_from.isoformat(),
                    "effective_to": e.effective_to.isoformat(),
                },
            )

    # 规则 2：批内同员工区间重叠检查（半开区间重叠 <=> a.from < b.to and b.from < a.to）
    by_employee: dict[str, list[tuple[int, SegmentIn]]] = defaultdict(list)
    for idx, e in enumerate(entries):
        by_employee[e.employee_id].append((idx, e))

    for employee_id, items in by_employee.items():
        ordered = sorted(items, key=lambda pair: (pair[1].effective_from, pair[1].effective_to))
        for (i_a, a), (i_b, b) in zip(ordered, ordered[1:]):
            if a.effective_from < b.effective_to and b.effective_from < a.effective_to:
                raise AppError(
                    code="OVERLAPPING_SEGMENTS",
                    message=(
                        f"员工 {employee_id} 在本批存在重叠区间："
                        f"第 {i_a + 1} 条 [{a.effective_from.isoformat()}, {a.effective_to.isoformat()}) "
                        f"与第 {i_b + 1} 条 [{b.effective_from.isoformat()}, {b.effective_to.isoformat()})"
                    ),
                    details={
                        "employee_id": employee_id,
                        "indices": [i_a, i_b],
                    },
                )


def submit_revision(req: RevisionSubmitRequest) -> dict:
    """提交一批归属区间，成功返回新的递增修订号。

    修订号分配与区间插入在同一个 BEGIN IMMEDIATE 事务内完成：
    校验失败/插入失败都会回滚，修订号与数据均保持不变。
    """
    validate_batch(req.entries)

    with get_conn() as conn:
        # 立即拿写锁，避免两个并发批次同时分配到同一个修订号。
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO revisions (entry_count, note) VALUES (?, ?)",
            (len(req.entries), req.note),
        )
        revision_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO attribution_segments
                (revision_id, employee_id, cost_center, effective_from, effective_to)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    revision_id,
                    e.employee_id,
                    e.cost_center,
                    e.effective_from.isoformat(),
                    e.effective_to.isoformat(),
                )
                for e in req.entries
            ],
        )
        row = conn.execute(
            "SELECT revision_id, entry_count, note, created_at FROM revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    return dict(row)


def _lookup_one(
    conn: Connection, revision_id: int, employee_id: str, work_date_iso: str
) -> tuple[str | None, int | None]:
    """在 revision_id 口径下定位单条工时的成本中心。

    仅可见 revision_id <= 给定修订号 的区间；同日期点命中多个历史区间时，
    取修订号最大者（后补录覆盖先补录），区间外/空档则未归属。
    """
    row = conn.execute(
        """
        SELECT cost_center, revision_id AS matched_revision_id
        FROM attribution_segments
        WHERE employee_id = ?
          AND effective_from <= ?
          AND effective_to   > ?
          AND revision_id <= ?
        ORDER BY revision_id DESC, id DESC
        LIMIT 1
        """,
        (employee_id, work_date_iso, work_date_iso, revision_id),
    ).fetchone()
    if row is None:
        return None, None
    return row["cost_center"], row["matched_revision_id"]


def attribute_hours(req: AttributionQueryRequest) -> list[AttributionResult]:
    """逐条返回工时记录在指定修订口径下的成本中心（或未归属）。"""
    with get_conn() as conn:
        max_row = conn.execute("SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions").fetchone()
        if req.revision_id > max_row["r"]:
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {req.revision_id} 不存在，当前最大修订号为 {max_row['r']}",
                status_code=404,
            )

        results: list[AttributionResult] = []
        for rec in req.records:
            cost_center, matched = _lookup_one(
                conn, req.revision_id, rec.employee_id, rec.work_date.isoformat()
            )
            results.append(
                AttributionResult(
                    employee_id=rec.employee_id,
                    work_date=rec.work_date,
                    hours=rec.hours,
                    revision_id=req.revision_id,
                    cost_center=cost_center,
                    attributed=cost_center is not None,
                    matched_revision_id=matched,
                )
            )
    return results


def list_revisions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT revision_id, entry_count, note, created_at FROM revisions ORDER BY revision_id"
        ).fetchall()
    return [dict(r) for r in rows]


def list_segments(revision_id: int, employee_id: str | None) -> list[dict]:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if exists is None:
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {revision_id} 不存在",
                status_code=404,
            )
        if employee_id:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ? AND employee_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id, employee_id.strip()),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id,),
            ).fetchall()
    return [dict(r) for r in rows]
