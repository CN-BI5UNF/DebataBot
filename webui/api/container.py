"""WebUI API for the built-in Docker container feature."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from features.container import ContainerError
from features.container.admin import admin_capabilities

from .deps import require_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/container", tags=["container"])


def _service(request: Request) -> Any:
    rt = require_runtime(request)
    service = getattr(rt, "container", None)
    if service is None:
        raise HTTPException(status_code=503, detail="容器服务尚未就绪")
    return service


def _http_error(exc: ContainerError) -> HTTPException:
    detail: dict[str, Any] = {
        "code": exc.code,
        "message": exc.safe_message(),
    }
    if exc.details:
        detail["details"] = exc.details
    return HTTPException(status_code=400, detail=detail)


@router.get("/status")
async def container_status(request: Request) -> dict[str, Any]:
    service = _service(request)
    try:
        status = await service.status(public=False)
    except ContainerError as exc:
        raise _http_error(exc) from exc
    return {"ok": True, "status": status}


@router.get("/storage")
async def container_storage(request: Request) -> dict[str, Any]:
    service = _service(request)
    status = service.storage.status(include_host_paths=True)
    return {
        "ok": True,
        "storage": status.as_dict(include_host_paths=True),
        "setup": service.storage.setup_commands(),
        "admin": admin_capabilities(service.platform_name),
    }


@router.post("/storage/setup")
async def container_storage_setup(
    request: Request,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """One-time C storage setup.

    The request may contain ``password`` for Linux sudo. It is consumed in
    memory, sent only to ``sudo -S`` stdin, and never logged or returned.
    Windows ignores it and uses the UAC prompt instead.
    """
    service = _service(request)
    data = payload or {}
    password = data.get("password")
    if password is not None and not isinstance(password, str):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "container_admin_password_invalid",
                "message": "密码格式无效",
            },
        )
    try:
        return await service.setup_storage(password=password)
    except ContainerError as exc:
        raise _http_error(exc) from exc


@router.post("/test")
async def container_test(request: Request) -> dict[str, Any]:
    service = _service(request)
    try:
        result = await service.exec("printf karen-container-ok", timeout_seconds=15)
    except ContainerError as exc:
        raise _http_error(exc) from exc
    return {"ok": bool(result.get("ok")), "result": result}


@router.post("/reset")
async def container_reset(request: Request) -> dict[str, Any]:
    service = _service(request)
    try:
        return await service.reset()
    except ContainerError as exc:
        raise _http_error(exc) from exc
