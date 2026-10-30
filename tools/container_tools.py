"""LLM-facing multiplexed tool for the built-in Docker container."""

from __future__ import annotations

import logging
from typing import Any

from features.container import ContainerError

from .base import ToolContext, tool
from .schemas import ContainerArgs

logger = logging.getLogger(__name__)


def _error(message: str, *, code: str = "container_error") -> dict[str, Any]:
    return {"ok": False, "status": "failed", "code": code, "error": message, "brief": message}


def _service(ctx: ToolContext) -> Any:
    service = ctx.container
    if service is None:
        raise ContainerError("容器功能未启用", code="container_disabled")
    if not getattr(service, "enabled", True):
        raise ContainerError("容器功能未启用", code="container_disabled")
    return service


@tool(
    name="container",
    description=(
        "在 Karen 的受控 Docker 容器里运行命令和读写 /workspace 文件。"
        "action=exec 执行命令并返回 stdout/stderr/exit_code；"
        "read_file/write_file/edit_file/list_files 操作 /workspace；"
        "import_file/export_file 在宿主 AI workspace 与容器之间传输；"
        "status/storage 查看状态；reset 只重建容器，不删除持久数据。"
        "容器默认无网络、无特权、非 root，资源有限。"
    ),
    args_model=ContainerArgs,
    category="container",
    risk_level="high",
    search_tags=["container", "docker", "shell", "workspace", "exec"],
    short_description="在受控 Docker 容器内执行命令/读写文件。",
)
async def container(args: ContainerArgs, ctx: ToolContext) -> dict[str, Any]:
    try:
        service = _service(ctx)
        action = args.action
        if action == "exec":
            if not args.command.strip():
                raise ContainerError(
                    "exec 需要 command", code="container_invalid_argument"
                )
            return await service.exec(
                args.command, timeout_seconds=args.timeout_seconds
            )
        if action == "read_file":
            if not args.path.strip():
                raise ContainerError(
                    "read_file 需要 path", code="container_invalid_argument"
                )
            return await service.read_file(args.path)
        if action == "write_file":
            if not args.path.strip():
                raise ContainerError(
                    "write_file 需要 path", code="container_invalid_argument"
                )
            return await service.write_file(args.path, args.content)
        if action == "edit_file":
            if not args.path.strip():
                raise ContainerError(
                    "edit_file 需要 path", code="container_invalid_argument"
                )
            return await service.edit_file(args.path, args.old, args.new)
        if action == "list_files":
            return await service.list_files(args.path or "/workspace")
        if action == "import_file":
            if not args.host_path.strip() or not args.container_path.strip():
                raise ContainerError(
                    "import_file 需要 host_path 和 container_path",
                    code="container_invalid_argument",
                )
            return await service.import_file(
                args.host_path,
                args.container_path,
                workspace_dir=ctx.workspace_dir,
                overwrite=args.overwrite,
            )
        if action == "export_file":
            if not args.container_path.strip() or not args.host_path.strip():
                raise ContainerError(
                    "export_file 需要 container_path 和 host_path",
                    code="container_invalid_argument",
                )
            return await service.export_file(
                args.container_path,
                args.host_path,
                workspace_dir=ctx.workspace_dir,
                overwrite=args.overwrite,
            )
        if action == "reset":
            return await service.reset()
        if action == "status":
            return {"ok": True, "status": await service.status(public=True)}
        if action == "storage":
            storage = service.storage.status(include_host_paths=False)
            return {"ok": True, "storage": storage.as_dict(include_host_paths=False)}
        raise ContainerError("未知容器操作", code="container_invalid_argument")
    except ContainerError as exc:
        return _error(exc.safe_message(), code=exc.code)
    except Exception:  # noqa: BLE001
        logger.exception("容器工具执行失败")
        return _error("容器操作失败", code="container_error")


__all__ = ["container"]
