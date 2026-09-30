"""批次追溯 HTTP 路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy.orm import Session

from . import service
from .deps import SessionDep
from .errors import AppError
from .models import Batch, Production
from .schemas import (
    BatchOut,
    DescendantsOut,
    LineageOut,
    ProductionIn,
    ProductionOut,
    RawBatchIn,
    ReviewIn,
    ReviewOut,
    RevocationIn,
    RevocationOut,
    ProductionDetailOut,
    SuspectIn,
)

router = APIRouter()


def _batch_out(batch: Batch) -> dict[str, Any]:
    revoked = (
        batch.output_production is not None
        and batch.output_production.revocation is not None
    )
    return {
        "batch_no": batch.batch_no,
        "unit": batch.unit,
        "initial_qty": float(batch.initial_qty),
        "remaining_qty": float(batch.remaining_qty),
        "is_suspect": batch.is_suspect,
        "is_quarantined": batch.is_quarantined,
        "note": batch.note,
        "is_revoked": revoked,
    }


def _production_out(prod: Production, unit: str) -> dict[str, Any]:
    return {
        "output_batch_no": prod.output_no,
        "unit": unit,
        "output_quantity": float(prod.output_qty),
        "inputs": [
            {"batch_no": pi.source_no, "quantity": float(pi.qty)}
            for pi in sorted(prod.inputs, key=lambda x: x.id)
        ],
    }


def _production_detail_out(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "output_batch_no": detail["output_batch_no"],
        "unit": detail["unit"],
        "output_quantity": float(detail["output_quantity"]),
        "inputs": [
            {"batch_no": item["batch_no"], "quantity": float(item["qty"])}
            for item in detail["inputs"]
        ],
        "status": detail["status"],
        "revoke_reason": detail["revoke_reason"],
        "revoked_at": detail["revoked_at"],
    }


def _revocation_out(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "output_batch_no": result["output_batch_no"],
        "status": result["status"],
        "revoke_reason": result["revoke_reason"],
        "revoked_at": result["revoked_at"],
        "returns": [
            {"batch_no": item["batch_no"], "quantity": float(item["quantity"])}
            for item in result["returns"]
        ],
        "output_quantity": float(result["output_quantity"]),
        "unit": result["unit"],
    }


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/raw-batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_raw_batch(
    body: RawBatchIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        batch = service.register_raw_batch(session, body)
        return status.HTTP_201_CREATED, _batch_out(batch)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post("/productions", response_model=ProductionOut, status_code=status.HTTP_201_CREATED)
def create_production(
    body: ProductionIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        prod = service.submit_production(session, body)
        return status.HTTP_201_CREATED, _production_out(prod, body.unit)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/batches/{batch_no}", response_model=BatchOut)
def get_batch(batch_no: str, session: SessionDep) -> dict[str, Any]:
    batch = session.get(Batch, batch_no)
    if batch is None:
        from .errors import batch_not_found

        raise batch_not_found(batch_no)
    return _batch_out(batch)


@router.get("/batches/{batch_no}/lineage", response_model=LineageOut)
def get_lineage(batch_no: str, session: SessionDep) -> dict[str, Any]:
    return service.lineage(session, batch_no)


@router.get("/productions/{output_batch_no}", response_model=ProductionDetailOut)
def get_production(output_batch_no: str, session: SessionDep) -> dict[str, Any]:
    """按产出批次编号查询生产单：原单据 + 撤销状态/原因（已撤销也可查）。"""
    detail = service.get_production_detail(session, output_batch_no)
    return _production_detail_out(detail)


@router.post("/contamination", response_model=DescendantsOut, status_code=status.HTTP_200_OK)
def flag_suspect(
    body: SuspectIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    # 污染标记本身是幂等动作：无键也允许，有键则走标准重放/冲突语义
    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.mark_suspect(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post("/reviews", response_model=ReviewOut, status_code=status.HTTP_200_OK)
def review_suspect(
    body: ReviewIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """质检复核：确认疑似批次为误报，撤销标记并重算隔离。必须携带幂等键。"""
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.review_suspect(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post(
    "/productions/{output_batch_no}/revoke",
    response_model=RevocationOut,
    status_code=status.HTTP_200_OK,
)
def revoke_production(
    output_batch_no: str,
    body: RevocationIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """撤销录入错误的生产单：返还来源投料、产出停用、边退出有效谱系。

    路径产出编号须与请求体一致；必须携带幂等键，沿用同体重放/异体冲突约定。
    """
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )
    if body.output_batch_no != output_batch_no:
        raise AppError(
            "VALIDATION_ERROR",
            "路径产出批次编号与请求体 output_batch_no 不一致",
            422,
            details={
                "path_output_batch_no": output_batch_no,
                "body_output_batch_no": body.output_batch_no,
            },
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, _revocation_out(
            service.revoke_production(session, body)
        )

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result
