"""存储盘点与安全缓存清理 API。

破坏性操作不直接接收任意路径；preview 只接受类别，服务端自行确定候选。
execute 需要 preview_id、确认令牌、幂等键和盘点代次四项同时匹配。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.storage_maintenance import (
    CATEGORY_SPECS,
    StorageMaintenanceError,
    StorageMaintenanceService,
)

from .deps import require_runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"])


class CleanupPreviewPayload(BaseModel):
    categories: list[str] = Field(min_length=1, max_length=16)


class CleanupExecutePayload(BaseModel):
    preview_id: str = Field(min_length=1, max_length=160)
    confirmation_token: str = Field(min_length=8, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_generation: str = Field(min_length=8, max_length=128)


def _service(request: Request) -> StorageMaintenanceService:
    rt = require_runtime(request)
    service = getattr(rt, "_storage_maintenance_service", None)
    if service is None:
        service = StorageMaintenanceService(rt)
        rt._storage_maintenance_service = service
    return service


def _http_error(exc: StorageMaintenanceError) -> HTTPException:
    if exc.code in {"preview_not_found", "job_not_found"}:
        status = 404
    elif exc.code == "preview_expired":
        status = 410
    elif exc.code in {
        "confirmation_token_invalid",
        "generation_conflict",
        "preview_already_used",
        "preview_conflict",
        "job_not_cancellable",
    }:
        status = 409
    elif exc.code in {"unknown_category", "category_not_implemented", "empty_categories"}:
        status = 422
    elif exc.code == "restore_not_supported":
        status = 409
    else:
        status = 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "job_id": job.get("job_id"),
        "preview_id": job.get("preview_id"),
        "state": job.get("state"),
        "categories": list(job.get("categories") or []),
        "created_at": _iso(job.get("created_at")),
        "started_at": _iso(job.get("started_at")),
        "finished_at": _iso(job.get("finished_at")),
        "result": job.get("result"),
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
    }


@router.get("/usage")
async def usage(request: Request) -> dict[str, Any]:
    return await _service(request).usage()


@router.get("/categories")
async def categories() -> dict[str, Any]:
    return {
        "categories": [
            {
                "key": spec.key,
                "label": spec.label,
                "kind": spec.kind,
                "description": spec.description,
                "implemented": spec.implemented,
                "protected": spec.protected,
            }
            for spec in CATEGORY_SPECS.values()
        ]
    }


@router.post("/cleanup/preview")
async def cleanup_preview(
    request: Request,
    payload: Annotated[CleanupPreviewPayload, Body()],
) -> dict[str, Any]:
    try:
        return await _service(request).preview(payload.categories)
    except StorageMaintenanceError as exc:
        raise _http_error(exc) from exc


@router.post("/cleanup/execute")
async def cleanup_execute(
    request: Request,
    payload: Annotated[CleanupExecutePayload, Body()],
) -> JSONResponse:
    try:
        job, created = await _service(request).execute(
            preview_id=payload.preview_id,
            confirmation_token=payload.confirmation_token,
            idempotency_key=payload.idempotency_key,
            expected_generation=payload.expected_generation,
        )
    except StorageMaintenanceError as exc:
        raise _http_error(exc) from exc
    public = _public_job(job)
    if public is not None:
        public["created"] = bool(created)
    return JSONResponse(status_code=202, content=public or {})


@router.get("/jobs")
async def jobs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    rows = await _service(request).list_jobs(limit)
    return {"jobs": [_public_job(row) for row in rows]}


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str) -> dict[str, Any]:
    row = await _service(request).get_job(job_id)
    public = _public_job(row)
    if public is None:
        raise HTTPException(status_code=404, detail="清理任务不存在")
    return public


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        row = await _service(request).cancel(job_id)
    except StorageMaintenanceError as exc:
        raise _http_error(exc) from exc
    return _public_job(row) or {"job_id": job_id}


@router.post("/jobs/{job_id}/restore")
async def restore_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        await _service(request).restore(job_id)
    except StorageMaintenanceError as exc:
        raise _http_error(exc) from exc
    return {"ok": True}


__all__ = ["router"]
