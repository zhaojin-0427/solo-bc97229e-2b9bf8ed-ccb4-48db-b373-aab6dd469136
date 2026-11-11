"""FastAPI 入口：归属修订 API 路由与应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .database import init_db
from .errors import AppError
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    RevisionOut,
    RevisionSubmitRequest,
    SegmentsQueryRequest,
)
from .service import attribute_hours, list_revisions, list_segments, submit_revision


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保表结构存在（幂等）。
    init_db()
    yield


app = FastAPI(
    title="工时归属修订 API",
    description=(
        "人事调岗补录导致的工时归属修正服务。\n\n"
        "- 每批成功提交返回递增修订号，批次内校验失败则整批拒绝；\n"
        "- 生效日期区间为左闭右开 `[effective_from, effective_to)`，允许空档；\n"
        "- 按修订号查询时只可见该修订号及之前的数据，后续跨段补录不改变旧修订查询结果。"
    ),
    version="1.0.0",
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
        "同日期点存在多个历史区间时取修订号最大者。`revision_id=0` 表示空口径。"
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
