"""Pydantic 请求/响应模型。

日期统一使用 ISO 8601（yyyy-mm-dd），SQLite 内以 TEXT 存储；
ISO 日期字符串的字典序与时间序一致，可直接做半开区间比较。
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


class SegmentIn(BaseModel):
    """人事导出的一条员工-成本中心生效区间（左闭右开）。"""

    employee_id: str = Field(..., min_length=1, description="员工编号", examples=["E001"])
    cost_center: str = Field(..., min_length=1, description="成本中心编号", examples=["CC-SH-01"])
    effective_from: date = Field(..., description="生效起日（含）", examples=["2026-01-01"])
    effective_to: date = Field(..., description="生效止日（不含）", examples=["2026-04-01"])

    @field_validator("employee_id", "cost_center")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class RevisionSubmitRequest(BaseModel):
    """一批补录/归属区间。整批要么全部成功，要么整批拒绝。"""

    entries: list[SegmentIn] = Field(..., min_length=1, description="本批归属区间（至少一条）")
    note: str | None = Field(default=None, description="备注，如人事调岗补录批次号", examples=["HR 调岗补录 2026-11"])


class RevisionOut(BaseModel):
    revision_id: int
    entry_count: int
    note: str | None
    created_at: str


class WorkHourRecord(BaseModel):
    """工时系统导出的一条工时记录。"""

    employee_id: str = Field(..., min_length=1, examples=["E001"])
    work_date: date = Field(..., description="工作日期", examples=["2026-02-15"])
    hours: float = Field(..., description="小时数（原样回显，不参与归属判断）", examples=[8.0])

    @field_validator("employee_id")
    @classmethod
    def _strip_employee(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class AttributionQueryRequest(BaseModel):
    """按指定修订号的口径查询工时归属。"""

    revision_id: int = Field(..., ge=0, description="月结口径修订号；0 表示尚无任何补录的空口径")
    records: list[WorkHourRecord] = Field(..., min_length=1, description="待归属的工时记录")


class AttributionResult(BaseModel):
    employee_id: str
    work_date: date
    hours: float
    revision_id: int
    cost_center: str | None = Field(..., description="成本中心；None 表示未归属")
    attributed: bool
    matched_revision_id: int | None = Field(..., description="命中区间所属的修订号，便于审计")


class SegmentsQueryRequest(BaseModel):
    employee_id: str | None = Field(default=None, description="可选，按员工过滤")


# --------------------------------------------------------------------------- #
# 备份恢复
# --------------------------------------------------------------------------- #

class RecoveryResult(BaseModel):
    """一次成功恢复的结果摘要。"""

    restored_revision_count: int = Field(..., description="恢复后的修订总数")
    restored_segment_count: int = Field(..., description="恢复后的区间总行数")
    restored_min_revision_id: int | None = Field(..., description="备份内最小修订号；空库备份为 null")
    restored_max_revision_id: int | None = Field(..., description="备份内最大修订号；空库备份为 null")
    next_revision_id: int = Field(..., description="恢复后下一次补录将使用的修订号（备份最大号 + 1）")
    content_fingerprint: str = Field(..., description="恢复内容的 SHA-256 指纹")


class BackupStagedOut(BaseModel):
    """上传校验通过后的暂存响应（恢复凭据仅此一次明文返回）。"""

    backup_id: str
    status: str = Field(..., description="staged / confirmed / switching")
    recovery_token: str = Field(..., description="确认恢复所需的一次性凭据，请妥善保存")
    filename: str
    note: str | None
    size_bytes: int
    sha256: str
    created_at: str
    prepared_at: str
    revision_count: int = Field(..., description="备份内 revisions 行数")
    segment_count: int = Field(..., description="备份内 attribution_segments 行数")
    min_revision_id: int | None = Field(..., description="备份修订范围下限；空库备份为 null")
    max_revision_id: int | None = Field(..., description="备份修订范围上限；空库备份为 null")
    prepared_against_max_revision: int = Field(
        ..., description="暂存时现库的最大修订号，用于确认时新鲜度判定"
    )


class BackupStatusOut(BaseModel):
    """备份状态/恢复结果查询响应（不含凭据）。"""

    backup_id: str
    status: str
    filename: str
    note: str | None
    size_bytes: int
    sha256: str
    created_at: str
    prepared_at: str
    prepared_against_fingerprint: str
    prepared_against_max_revision: int
    revision_count: int
    segment_count: int
    min_revision_id: int | None
    max_revision_id: int | None
    content_fingerprint: str
    confirmed_at: str | None = None
    switch_completed_at: str | None = None
    switch_started_at: str | None = None
    recovery: RecoveryResult | None = None


class RecoveryConfirmRequest(BaseModel):
    """确认恢复请求体。"""

    recovery_token: str = Field(..., min_length=1, description="上传时返回的一次性恢复凭据")
