from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import database
from .models import (
    CollectRequest,
    CollectResponse,
    ProjectOut,
    ReceiptCheckRequest,
    ReceiptCheckResponse,
    RunOut,
)
from .receipt_service import ReceiptCheckError, check_receipt
from .service import ServiceError, query_projects, run_collection


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程投标截止时间采集与回执核对 API",
    description=(
        "提交公告列表页地址，服务抓取全部详情页，将更正公告按引用的原公告编号归并，"
        "按发布时间确定生效的投标截止时间。重复采集不产生重复记录；"
        "任一详情页读取失败时整体报错并保留上次成功采集结果。"
        "另提供回执核对：沿回执目录分页读取上传/撤回回执，"
        "确认手头标书是否仍是平台受理的末份版本。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未入库
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(status="success", **result)


@app.post(
    "/api/receipt-check",
    response_model=ReceiptCheckResponse,
    status_code=200,
    summary="回执核对：确认手头标书是否仍是平台受理版本",
)
def receipt_check_endpoint(req: ReceiptCheckRequest) -> ReceiptCheckResponse:
    try:
        result = check_receipt(
            original_code=req.original_code,
            section_code=req.section_code,
            file_sha256=req.file_sha256,
            receipt_list_url=req.receipt_list_url,
        )
    except ReceiptCheckError as exc:
        # 422 仅用于请求本身不合法（原公告未采集等）；
        # 回执页面抓取失败属于“无法判定”的核对结论，仍返回 200
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ReceiptCheckResponse(**result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects()]


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]
