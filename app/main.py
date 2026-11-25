"""FastAPI 入口：从 /docs 可完成录入、查询与纠错。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Path, Query
from fastapi.responses import JSONResponse

from . import reconciliation, services
from .database import get_conn, init_db
from .schemas import (
    BulkAttribution,
    BulkDispatch,
    BulkWorkHour,
    CorrectRequest,
    CorrectResponse,
    ErrorResponse,
    ReconAccountListResponse,
    ReconChildrenResponse,
    ReconGenerateResponse,
    ReconWholeResponse,
    SupplyResponse,
)

MonthParam = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="月份，格式 YYYY-MM")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="道路养护工时纠错服务",
    version="1.0.0",
    description=(
        "同月三张表（工时/分派/归科）入 SQLite。工时恰好存在一条完整路径"
        "（工单→项目→预算科目）才可供数；两条及以上路径即使科目相同也按重复扣留。"
        "纠错通过撤除一条分派或归科记录完成，撤除与供数变化在同一事务生效。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(services.AppError)
async def app_error_handler(request, exc: services.AppError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(reconciliation.ReconError)
async def recon_error_handler(request, exc: reconciliation.ReconError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def conn_dep():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 录入
@app.post(
    "/months/{month}/work-hours",
    tags=["录入"],
    summary="录入工时表（工单号+分钟数）",
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def post_work_hours(month: str = MonthParam, payload: BulkWorkHour = ..., conn=Depends(conn_dep)):
    return services.load_work_hours(conn, month, payload)


@app.post(
    "/months/{month}/dispatches",
    tags=["录入"],
    summary="录入分派表（工单→项目）",
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def post_dispatches(month: str = MonthParam, payload: BulkDispatch = ..., conn=Depends(conn_dep)):
    return services.load_dispatches(conn, month, payload)


@app.post(
    "/months/{month}/attributions",
    tags=["录入"],
    summary="录入归科表（项目→预算科目）",
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def post_attributions(month: str = MonthParam, payload: BulkAttribution = ..., conn=Depends(conn_dep)):
    return services.load_attributions(conn, month, payload)


# ---------------------------------------------------------------- 查询
@app.get(
    "/months/{month}/supply",
    tags=["查询"],
    summary="供数视图：可供数工时带科目，扣留行带路径数",
    response_model=SupplyResponse,
)
def get_supply(month: str = MonthParam, conn=Depends(conn_dep)):
    return services.query_supply(conn, month)


@app.get("/months/{month}/audit", tags=["查询"], summary="纠错撤除审计记录")
def get_audit(month: str = MonthParam, conn=Depends(conn_dep)):
    return {"month": month, "audits": services.list_audit(conn, month)}


# ---------------------------------------------------------------- 纠错
@app.post(
    "/months/{month}/corrections",
    tags=["纠错"],
    summary="撤除一条分派/归科记录以纠正重复工时",
    response_model=CorrectResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def post_correction(month: str = MonthParam, payload: CorrectRequest = ..., conn=Depends(conn_dep)):
    return services.correct(
        conn,
        month,
        payload.work_row_id,
        payload.remove,
        payload.remove_row_id,
    )


@app.delete(
    "/months/{month}",
    tags=["管理"],
    summary="（测试辅助）清空某月全部数据",
    responses={409: {"model": ErrorResponse}},
)
def delete_month(month: str = MonthParam, conn=Depends(conn_dep)):
    return services.reset_month(conn, month)


# ---------------------------------------------------------------- 对账
@app.post(
    "/months/{month}/reconciliation",
    tags=["对账"],
    summary="生成月度对账号：返回三表整表指纹（同一已提交快照）",
    response_model=ReconGenerateResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def post_reconciliation(month: str = MonthParam, conn=Depends(conn_dep)):
    return reconciliation.generate_reconciliation(conn, month)


@app.get(
    "/months/{month}/reconciliation",
    tags=["对账"],
    summary="列出某月已生成且仍可查的对账号",
    response_model=ReconAccountListResponse,
)
def list_reconciliation(month: str = MonthParam, conn=Depends(conn_dep)):
    return reconciliation.list_accounts(conn, month)


@app.get(
    "/reconciliation/{account_id}",
    tags=["对账"],
    summary="按对账号取三表整表指纹（level 0）",
    response_model=ReconWholeResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_reconciliation_whole(account_id: str, conn=Depends(conn_dep)):
    return reconciliation.get_whole(conn, account_id)


@app.get(
    "/reconciliation/{account_id}/tables/{table}/segments",
    tags=["对账"],
    summary="查某段下一级的 32 个子段指纹（空段标空，直接读已存子段）",
    response_model=ReconChildrenResponse,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def get_reconciliation_children(
    account_id: str,
    table: str,
    lo: int = Query(..., ge=1, description="父段起始行号（闭区间，须按段宽对齐）"),
    hi: int = Query(..., ge=1, description="父段结束行号（闭区间）"),
    conn=Depends(conn_dep),
):
    return reconciliation.get_children(conn, account_id, table, lo, hi)


@app.get("/health", tags=["管理"], summary="健康检查")
def health():
    return {"status": "ok"}
