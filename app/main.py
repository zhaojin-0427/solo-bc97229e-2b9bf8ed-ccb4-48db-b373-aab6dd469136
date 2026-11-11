"""FastAPI 入口：归属修订 API 与离线备份恢复 API 的路由与应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from . import recovery
from .database import init_db
from .errors import AppError
from .recovery import MAX_BACKUP_BYTES
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    BackupStagedOut,
    BackupStatusOut,
    RecoveryConfirmRequest,
    RevisionOut,
    RevisionSubmitRequest,
    SegmentsQueryRequest,
)
from .service import attribute_hours, list_revisions, list_segments, submit_revision


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保表结构存在（幂等），并对账可能因进程中断停在切换途中的恢复。
    init_db()
    recovery.reconcile_on_startup()
    yield


app = FastAPI(
    title="工时归属修订与备份恢复 API",
    description=(
        "人事调岗补录导致的工时归属修正服务，并支持从离线 SQLite backup 整体恢复归属库。\n\n"
        "- 每批成功提交返回递增修订号，批次内校验失败则整批拒绝；\n"
        "- 生效日期区间为左闭右开 `[effective_from, effective_to)`，允许空档；\n"
        "- 按修订号查询时只可见该修订号及之前的数据，后续跨段补录不改变旧修订查询结果；\n"
        "- 备份恢复分「上传校验暂存」与「凭据确认」两步：非法备份拒绝且不改现库，"
        "确认时若准备后已有补录或其他恢复成功则拒绝，切换为单事务原子替换。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                **({"details": exc.details} if exc.details else {}),
            }
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 原有接口（保持不变）
# --------------------------------------------------------------------------- #

@app.post(
    "/api/revisions",
    response_model=RevisionOut,
    status_code=201,
    tags=["归属修订"],
    summary="提交一批员工-成本中心归属区间",
    description=(
        "整批校验通过后写入，返回递增修订号。\n\n"
        "拒绝条件（整批回滚，数据与修订号均不变）：\n"
        "1. 起日不早于止日（`effective_from >= effective_to`）；\n"
        "2. 批内同一员工的区间相互重叠（首尾相接不算重叠）。"
    ),
)
def create_revision(req: RevisionSubmitRequest) -> RevisionOut:
    return RevisionOut(**submit_revision(req))


@app.get("/api/revisions", tags=["归属修订"], summary="列出全部修订号")
def get_revisions() -> list[RevisionOut]:
    return [RevisionOut(**row) for row in list_revisions()]


@app.post(
    "/api/attribution/query",
    response_model=list[AttributionResult],
    tags=["归属查询"],
    summary="按指定修订号的口径逐条查询工时归属",
    description=(
        "对每条工时记录返回成本中心或未归属。只可见 `revision_id` 及之前的补录；"
        "同日期点存在多个历史区间时取修订号最大者。`revision_id=0` 表示空口径。\n\n"
        "整次查询固定在同一个 WAL 快照上：与恢复切换并发时，单次查询只会读到"
        "恢复前或恢复后的数据，不会混合。"
    ),
)
def query_attribution(req: AttributionQueryRequest) -> list[AttributionResult]:
    return attribute_hours(req)


@app.post(
    "/api/revisions/{revision_id}/segments",
    tags=["归属修订"],
    summary="查看某个修订号下提交的原始区间",
)
def get_revision_segments(revision_id: int, req: SegmentsQueryRequest | None = None) -> list[dict]:
    employee_id = req.employee_id if req else None
    return list_segments(revision_id, employee_id)


# --------------------------------------------------------------------------- #
# 备份恢复（新增）
# --------------------------------------------------------------------------- #

@app.post(
    "/api/recovery/backups",
    response_model=BackupStagedOut,
    status_code=201,
    tags=["备份恢复"],
    summary="上传 SQLite backup 备份：暂存并校验（不修改现库）",
    description=(
        "上传由 SQLite Online Backup API 生成的备份文件（含 revisions 与 "
        "attribution_segments 两张表）。服务端将其写入暂存区并依次校验：\n\n"
        "1. SQLite 文件头与 `PRAGMA integrity_check` 完整性；\n"
        "2. 表结构：仅两张业务表（允许内部 sqlite_sequence）、列定义/主键/CHECK/外键声明、必需索引；\n"
        "3. `PRAGMA foreign_key_check` 外键数据一致性；\n"
        "4. 每个修订号的区间行数与 `revisions.entry_count` 一致；\n"
        "5. 修订号从 1 起连续无缺口；\n"
        "6. 区间规则：ISO 日期、起日严格早于止日、同修订同员工区间不重叠。\n\n"
        "**任何一项不通过都会拒绝（400）并删除暂存文件，现库完全不变；"
        "校验期间补录与查询可正常使用。** 校验通过返回修订范围、记录数与一次性恢复凭据。"
    ),
)
async def upload_backup(
    file: UploadFile = File(..., description="SQLite backup 生成的 .db 备份文件"),
    note: str | None = Form(default=None, description="可选备注，如离线备份批次号"),
) -> BackupStagedOut:
    # 流式读取并强制大小上限，避免恶意超大文件占满磁盘。
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BACKUP_BYTES:
            raise AppError(
                code="BACKUP_TOO_LARGE",
                message=f"备份文件超过 {MAX_BACKUP_BYTES // (1024 * 1024)} MiB 上限",
                status_code=413,
                details={"max_bytes": MAX_BACKUP_BYTES},
            )
        chunks.append(chunk)
    payload = b"".join(chunks)
    result = recovery.stage_backup(payload, file.filename, note)
    return BackupStagedOut(**result)


@app.post(
    "/api/recovery/backups/{backup_id}/confirm",
    response_model=BackupStatusOut,
    tags=["备份恢复"],
    summary="凭恢复凭据确认恢复：整体替换归属数据",
    description=(
        "确认语义：\n\n"
        "- 凭据错误返回 403；备份不存在返回 404；\n"
        "- 若备份准备完成后现库已有补录或其他恢复成功（内容指纹变化），返回 409 且保持现库；\n"
        "- 否则在单个事务内整体替换两张表，后续修订号从备份最大号继续递增；\n"
        "- 切换失败或进程中断，磁盘上只可能是完整旧库或完整备份；\n"
        "- 成功后对同一备份重复请求返回原结果（幂等），不会再次恢复。"
    ),
)
def confirm_backup(backup_id: str, req: RecoveryConfirmRequest) -> BackupStatusOut:
    return BackupStatusOut(**recovery.confirm_backup(backup_id, req.recovery_token))


@app.get(
    "/api/recovery/backups",
    response_model=list[BackupStatusOut],
    tags=["备份恢复"],
    summary="列出全部暂存/已完成的备份",
)
def list_backups() -> list[BackupStatusOut]:
    return [BackupStatusOut(**e) for e in recovery.list_backups()]


@app.get(
    "/api/recovery/backups/{backup_id}",
    response_model=BackupStatusOut,
    tags=["备份恢复"],
    summary="查询单个备份的暂存状态或恢复结果（重启后仍可查询）",
)
def get_backup(backup_id: str) -> BackupStatusOut:
    return BackupStatusOut(**recovery.get_backup(backup_id))
