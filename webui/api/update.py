"""GitHub Releases 代码更新 API。

更新器只操作代码仓库；用户数据目录通过 AppPaths 与项目根分离。
check 需要用户确认后才 apply；apply 前再次核对 token、目标 commit、工作树。
所有版本来自远端 tag（与 GitHub Releases 使用同一组 tag），更新日志来自
``releases/<tag>.md``。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from core.updater import (
    UPDATE_REF,
    UPDATE_REPO_URL,
    UpdateError,
    UpdateService,
    systemd_restart_callback,
)

from .deps import require_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/update", tags=["update"])


class UpdateCheckPayload(BaseModel):
    target_tag: str | None = Field(default=None, max_length=200)

    @field_validator("target_tag")
    @classmethod
    def _strip_tag(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None


class UpdateApplyPayload(BaseModel):
    confirmation_token: str = Field(min_length=8, max_length=256)
    target_sha: str = Field(min_length=7, max_length=64)
    confirm_branch_switch: bool = False
    confirm_rollback: bool = False


class UpdateNotificationPayload(BaseModel):
    suppress_badge: bool | None = None


def build_service(host: Any, rt: Any | None = None) -> UpdateService | None:
    """从 RuntimeHost 构造/复用更新服务。

    Scheduler 和 HTTP 路由共用同一缓存，避免自动检查与页面检查各自持有一份
    状态锁、互相看不到对方写入的 update_state.json。
    """
    runtime = rt if rt is not None else getattr(host, "rt", None)
    if runtime is None:
        return None
    config = getattr(runtime, "config", None)
    paths = getattr(runtime, "paths", None)
    if config is None or paths is None:
        return None
    update_cfg = getattr(config, "update", None)
    mirror = str(getattr(update_cfg, "mirror", "") or "")
    timeout = int(getattr(update_cfg, "check_timeout_seconds", 60) or 60)
    interval = int(
        getattr(update_cfg, "check_interval_days", 9) or 9
    )
    key = (
        str(getattr(runtime, "project_root", host.project_root).resolve()),
        str(paths.DATA_DIR.resolve()),
        mirror,
        timeout,
        interval,
    )
    cached = getattr(host, "_update_service_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    service = UpdateService(
        getattr(runtime, "project_root", host.project_root),
        paths.DATA_DIR,
        mirror=mirror,
        timeout_seconds=timeout,
        check_interval_days=interval,
        restart_callback=systemd_restart_callback,
    )
    host._update_service_cache = (key, service)
    return service


def service_for_host(host: Any) -> UpdateService | None:
    """后台 watchdog 用；Runtime 未就绪时返回 None。"""
    runtime = getattr(host, "rt", None)
    if runtime is None:
        return None
    return build_service(host, runtime)


def _service(request: Request) -> UpdateService:
    rt = require_runtime(request)
    host = request.app.state.host
    service = build_service(host, rt)
    if service is None:
        raise HTTPException(status_code=503, detail="更新服务尚未就绪")
    return service


def _http_error(exc: UpdateError) -> HTTPException:
    network_codes = {"git_timeout", "git_failed", "git_missing"}
    status = 409 if exc.code not in network_codes else 502
    if exc.code in {"release_invalid", "mirror_scheme", "mirror_credentials",
                    "mirror_host", "mirror_private", "mirror_repo_mismatch",
                    "mirror_query"}:
        status = 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/version")
async def version(request: Request) -> dict[str, Any]:
    """右下角版本占位；不要求 Runtime 已启动。"""
    host = request.app.state.host
    paths = host.build_paths()
    service = UpdateService(host.project_root, paths.DATA_DIR)
    return await service.version_info()


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    try:
        service = _service(request)
        data = await service.status()
        notification = await service.available_snapshot()
    except UpdateError as exc:
        raise _http_error(exc) from exc
    return {
        "repo": UPDATE_REPO_URL,
        "target_ref": UPDATE_REF,
        **data,
        "notification": notification,
    }


@router.get("/available")
async def available(request: Request) -> dict[str, Any]:
    """只读缓存：总览绿标只轮询这里，不触发网络。"""
    try:
        return await _service(request).available_snapshot()
    except UpdateError as exc:
        raise _http_error(exc) from exc


@router.get("/releases")
async def releases(
    request: Request,
    force: bool = Query(False, description="忽略 5 分钟缓存，强制刷新远端 tag"),
) -> dict[str, Any]:
    try:
        return await _service(request).releases(force=force)
    except UpdateError as exc:
        raise _http_error(exc) from exc


@router.post("/check")
async def check(
    request: Request,
    payload: Annotated[UpdateCheckPayload | None, Body()] = None,
) -> dict[str, Any]:
    target_tag = payload.target_tag if payload else None
    try:
        return await _service(request).check(target_tag=target_tag)
    except UpdateError as exc:
        raise _http_error(exc) from exc


@router.post("/apply")
async def apply(
    request: Request,
    payload: Annotated[UpdateApplyPayload, Body()],
) -> dict[str, Any]:
    try:
        return await _service(request).apply(
            confirmation_token=payload.confirmation_token,
            target_sha=payload.target_sha,
            confirm_branch_switch=payload.confirm_branch_switch,
            confirm_rollback=payload.confirm_rollback,
        )
    except UpdateError as exc:
        raise _http_error(exc) from exc


@router.patch("/preferences")
async def preferences(
    request: Request,
    payload: Annotated[UpdateNotificationPayload, Body()],
) -> dict[str, Any]:
    """更新提醒偏好：勾选“之后不再提示”后由前端写 suppress_badge。"""
    service = _service(request)
    if payload.suppress_badge is not None:
        return await service.set_suppress_badge(payload.suppress_badge)
    return await service.available_snapshot()


__all__ = ["build_service", "router", "service_for_host"]
