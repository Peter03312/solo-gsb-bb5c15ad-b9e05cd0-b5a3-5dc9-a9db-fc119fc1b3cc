"""FastAPI 应用与路由。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from . import ledger
from .db import init_db
from .ledger import ApiError
from .schemas import (
    CloseIn,
    PaymentIn,
    PriceCorrectionIn,
    RefundIn,
    SaleIn,
    SplitChangeIn,
    WorkIn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时自动初始化数据库
    yield


app = FastAPI(
    title="数字出版版税结算服务",
    version="1.3.0",
    description="阶梯版税计提、作者分成版本、退款冲销、净价更正、关账报表与关账后付款结算",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/", summary="服务信息")
def root():
    return {"service": "数字出版版税结算服务", "version": "1.3.0", "docs": "/docs"}


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 作品


@app.post("/works", status_code=201, summary="录入作品（阶梯费率 + 作者分成）")
def create_work(payload: WorkIn, response: Response):
    body, created = ledger.create_work(payload)
    if not created:  # 同号同内容重试 → 返回原结果
        response.status_code = 200
    return body


@app.get("/works/{work_id}", summary="查询作品")
def get_work(work_id: str):
    return ledger.get_work(work_id)


@app.get("/works/{work_id}/stats", summary="查询作品销量汇总")
def get_work_stats(work_id: str):
    return ledger.work_stats(work_id)


# ---------------------------------------------------------------- 作者分成版本


@app.post(
    "/works/{work_id}/splits",
    status_code=201,
    summary="提交作者分成新版本（变更编号幂等，按提交顺序生效）",
)
def create_split_version(work_id: str, payload: SplitChangeIn, response: Response):
    body, created = ledger.submit_split_change(work_id, payload)
    if not created:  # 同变更编号同内容重试 → 返回原版本
        response.status_code = 200
    return body


@app.get("/works/{work_id}/splits", summary="查询作品当前及历次分成版本")
def list_split_versions(work_id: str):
    return ledger.list_split_versions(work_id)


@app.get("/works/{work_id}/splits/{version}", summary="查询作品指定分成版本")
def get_split_version(work_id: str, version: int):
    return ledger.get_split_version(work_id, version)


# ---------------------------------------------------------------- 销售


@app.post("/sales", status_code=201, summary="登记销售流水（入账即计提版税）")
def create_sale(payload: SaleIn, response: Response):
    body, created = ledger.record_sale(payload)
    if not created:
        response.status_code = 200
    return body


@app.get("/sales/{sale_id}", summary="查询销售流水及计提明细")
def get_sale(sale_id: str):
    return ledger.get_sale(sale_id)


# ---------------------------------------------------------------- 退款


@app.post("/refunds", status_code=201, summary="登记退款流水（从最后一册起冲回）")
def create_refund(payload: RefundIn, response: Response):
    body, created = ledger.record_refund(payload)
    if not created:
        response.status_code = 200
    return body


@app.get("/refunds/{refund_id}", summary="查询退款流水及冲销明细")
def get_refund(refund_id: str):
    return ledger.get_refund(refund_id)


# ---------------------------------------------------------------- 净价更正


@app.post(
    "/price-corrections",
    status_code=201,
    summary="提交销售净价更正（仅对未退册数补提/冲回版税差额）",
)
def create_price_correction(payload: PriceCorrectionIn, response: Response):
    body, created = ledger.submit_price_correction(payload)
    if not created:  # 同更正编号同内容重试 → 返回原结果
        response.status_code = 200
    return body


@app.get("/price-corrections", summary="查询净价更正列表（可按 sale_id 过滤）")
def list_price_corrections(sale_id: str | None = None):
    return ledger.list_price_corrections(sale_id)


@app.get("/price-corrections/{correction_id}", summary="按更正编号查询净价更正")
def get_price_correction(correction_id: str):
    return ledger.get_price_correction(correction_id)


# ---------------------------------------------------------------- 关账


@app.post("/closes", status_code=201, summary="关账：汇总本批流水，生成不可改报表")
def create_close(response: Response, payload: CloseIn | None = None):
    body, created = ledger.close_batch(payload.request_id if payload else None)
    if not created:
        response.status_code = 200
    return body


@app.get("/closes", summary="关账批次列表")
def list_closes():
    return ledger.list_closes()


@app.get("/closes/{close_id}", summary="查询关账报表（不可改）")
def get_close(close_id: int):
    return ledger.get_close(close_id)


# ---------------------------------------------------------------- 付款结算


@app.post("/payments", status_code=201, summary="提交作者付款结算（仅针对当前最新关账批次）")
def create_payment(payload: PaymentIn, response: Response):
    body, created = ledger.submit_payment(payload)
    if not created:  # 同号同内容重试 → 返回原结算结果
        response.status_code = 200
    return body


@app.get("/payments/{payment_id}", summary="查询单笔付款结算")
def get_payment(payment_id: str):
    return ledger.get_payment(payment_id)


@app.get("/authors/{author_id}/settlement", summary="作者结算总览（分批应计/已付/负余额/可付/付款记录）")
def get_author_settlement(author_id: str):
    return ledger.author_settlement(author_id)
