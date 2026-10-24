"""用户数据一键备份 API。

备份 job 在后台线程执行；HTTP 只负责启动、轮询和下载。备份包含加密密钥库与
RSA 私钥，所有接口都走 WebUI 鉴权，下载响应禁止缓存。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core.backup import (
    BackupCoordinator,
    BackupError,
    BackupService,
    resolve_backup_directory,
)

from .deps import require_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backup", tags=["backup"])


class BackupStartPayload(BaseModel):
    include_media: bool | None = None


class BackupVerifyPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    deep_sqlite: bool = True


def build_manager(host: Any, rt: Any | None = None) -> BackupService:
    runtime = rt if rt is not None else getattr(host, "rt", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Runtime 尚未就绪")
    config = getattr(runtime, "config", None)
    paths = getattr(runtime, "paths", None)
    if config is None or paths is None:
        raise HTTPException(status_code=503, detail="Runtime 尚未就绪")
    backup_cfg = getattr(config, "backup", None)
    directory = resolve_backup_directory(getattr(backup_cfg, "directory", ""))
    include_media = bool(getattr(backup_cfg, "include_media", True))
    key = (
        str(getattr(runtime, "project_root", host.project_root)),
        str(paths.DATA_DIR),
        str(directory),
        include_media,
        id(runtime),
        id(getattr(runtime, "secrets", None)),
    )
    cached = getattr(host, "_backup_service_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    root = str(getattr(runtime, "project_root", host.project_root).resolve())
    coordinators = getattr(host, "_backup_coordinators", None)
    if coordinators is None:
        coordinators = {}
        host._backup_coordinators = coordinators
    coord_key = (root, str(paths.DATA_DIR.resolve()))
    coordinator = coordinators.get(coord_key)
    if coordinator is None:
        coordinator = BackupCoordinator(
            getattr(runtime, "project_root", host.project_root), paths
        )
        coordinators[coord_key] = coordinator
    manager = BackupService(
        getattr(runtime, "project_root", host.project_root),
        paths,
        backup_dir=directory,
        include_media=include_media,
        secrets_manager=getattr(runtime, "secrets", None),
        coordinator=coordinator,
    )
    host._backup_service_cache = (key, manager)
    return manager


def _manager(request: Request) -> BackupService:
    rt = require_runtime(request)
    return build_manager(request.app.state.host, rt)


def _http_error(exc: BackupError) -> HTTPException:
    if exc.code == "not_found":
        status = 404
    elif exc.code in {
        "invalid_name", "invalid_directory", "unsafe_directory",
        "not_cancellable",
    }:
        status = 400
    else:
        # backup_running / insufficient_space / space_check_failed / lock
        status = 409
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return _manager(request).status()


@router.post("")
async def start(
    request: Request,
    payload: Annotated[BackupStartPayload | None, Body()] = None,
) -> dict[str, Any]:
    manager = _manager(request)
    include_media = payload.include_media if payload else None
    try:
        return await manager.start(include_media=include_media)
    except BackupError as exc:
        raise _http_error(exc) from exc


@router.get("/jobs")
async def jobs(request: Request) -> dict[str, Any]:
    data = _manager(request).status()
    return {"jobs": data["jobs"], "running": data["running"]}


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _manager(request).get_job(job_id)
    except BackupError as exc:
        raise _http_error(exc) from exc


@router.post("/jobs/{job_id}/cancel")
async def cancel(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return await _manager(request).cancel(job_id)
    except BackupError as exc:
        raise _http_error(exc) from exc


@router.post("/verify")
async def verify(request: Request, payload: BackupVerifyPayload) -> JSONResponse:
    from core.restore import verify_backup

    manager = _manager(request)
    try:
        path = manager.resolve_download(payload.filename)
    except BackupError as exc:
        raise _http_error(exc) from exc
    report = await asyncio.to_thread(
        verify_backup, path, deep_sqlite=payload.deep_sqlite
    )
    return JSONResponse(content=report, headers={"Cache-Control": "no-store"})


@router.get("/files/{name:path}")
async def download(request: Request, name: str) -> FileResponse:
    manager = _manager(request)
    try:
        path = manager.resolve_download(name)
    except BackupError as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["build_manager", "router"]
