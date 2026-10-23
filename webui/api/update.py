"""GitHub 代码更新 API。

更新器只操作代码仓库；用户数据目录通过 AppPaths 与项目根分离。
check 需要用户确认后才 apply；apply 前再次核对 token、目标 commit、工作树。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

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


class UpdateApplyPayload(BaseModel):
    confirmation_token: str = Field(min_length=8, max_length=256)
    target_sha: str = Field(min_length=7, max_length=64)
    confirm_branch_switch: bool = False


def _service(request: Request) -> UpdateService:
    rt = require_runtime(request)
    host = request.app.state.host
    config = rt.config
    update_cfg = getattr(config, "update", None)
    mirror = str(getattr(update_cfg, "mirror", "") or "")
    timeout = int(getattr(update_cfg, "check_timeout_seconds", 60) or 60)
    key = (str(rt.project_root.resolve()), str(rt.paths.DATA_DIR.resolve()), mirror, timeout)
    cached = getattr(host, "_update_service_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    service = UpdateService(
        rt.project_root,
        rt.paths.DATA_DIR,
        mirror=mirror,
        timeout_seconds=timeout,
        restart_callback=systemd_restart_callback,
    )
    host._update_service_cache = (key, service)
    return service


def _http_error(exc: UpdateError) -> HTTPException:
    status = 409 if exc.code in {
        "worktree_dirty",
        "target_diverged",
        "head_changed",
        "target_changed",
        "check_expired",
        "confirmation_token_invalid",
        "branch_switch_not_confirmed",
        "dependency_change_requires_install",
        "up_to_date",
        "data_dir_inside_project",
    } else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    try:
        data = await _service(request).status()
    except UpdateError as exc:
        raise _http_error(exc) from exc
    return {
        "repo": UPDATE_REPO_URL,
        "target_ref": UPDATE_REF,
        **data,
    }


@router.post("/check")
async def check(request: Request) -> dict[str, Any]:
    try:
        return await _service(request).check()
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
        )
    except UpdateError as exc:
        raise _http_error(exc) from exc


__all__ = ["router"]
