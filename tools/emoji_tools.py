"""LLM-facing sticker folder tools.

The model already sends stickers by name through ``send_*``; these tools let it
*read and curate* the folder as well:

    list_emoji               list usable names
    save_emoji_from_url      download a public image into data/emoji
    save_emoji_from_workspace  promote an inbound group-chat image
    rename_emoji             rename an existing sticker
    delete_emoji             remove a sticker

All paths go through :mod:`utils.emoji_store`, which rejects path traversal,
symlinks, non-image content and oversized files. Remote downloads reuse the
vision pipeline's SSRF/redirect/size guards.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from features.vision.image_assets import ImageAssetError, download_remote_image_bytes
from utils import emoji_store
from utils.emoji_store import EmojiError

from .base import ToolContext, tool
from .schemas import (
    DeleteEmojiArgs,
    ListEmojiArgs,
    RenameEmojiArgs,
    SaveEmojiFromUrlArgs,
    SaveEmojiFromWorkspaceArgs,
)
from .workspace import WorkspaceError, resolve_in_workspace

logger = logging.getLogger(__name__)

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "status": "failed", "brief": message, "error": message}


def _emoji_dir(ctx: ToolContext) -> Path:
    if ctx.emoji_dir is None:
        raise EmojiError("表情包目录未配置，表情包工具被禁用")
    return Path(ctx.emoji_dir)


def _save_bytes(
    emoji_dir: Path,
    *,
    name: str,
    data: bytes,
    overwrite: bool,
) -> dict[str, Any]:
    """Validate and persist one image. Runs in a worker thread."""
    if len(data) > emoji_store.MAX_UPLOAD_BYTES:
        raise EmojiError(
            f"图片超过 {emoji_store.MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"
        )
    if not data:
        raise EmojiError("图片内容为空")
    mime = emoji_store.sniff_image(data[:16])
    ext = _EXT_BY_MIME.get(mime or "")
    if ext is None:
        raise EmojiError("只支持 png/jpg/gif 表情包")

    if overwrite:
        try:
            existing = emoji_store.find_by_stem(emoji_dir, name)
        except EmojiError as exc:
            # "不存在" is the normal case; a malformed name must still fail.
            if "不存在" not in str(exc) and "目录不存在" not in str(exc):
                raise
        else:
            existing.unlink()

    result = emoji_store.save_one(
        emoji_dir,
        f"{name}{ext}",
        data,
        stem=name,
    )
    result["size"] = len(data)
    return result


async def _persist(
    ctx: ToolContext,
    *,
    name: str,
    data: bytes,
    overwrite: bool,
) -> dict[str, Any]:
    emoji_dir = _emoji_dir(ctx)
    return await asyncio.to_thread(
        _save_bytes,
        emoji_dir,
        name=name,
        data=data,
        overwrite=overwrite,
    )


@tool(
    name="list_emoji",
    description=(
        "列出当前可发送的表情包名称。发图前先查名称，或者看看图库里已有哪些图。"
        "可按名称片段过滤；只返回名称和文件信息，不读取图片内容。"
    ),
    args_model=ListEmojiArgs,
    category="emoji",
)
async def list_emoji(args: ListEmojiArgs, ctx: ToolContext) -> dict:
    try:
        emoji_dir = _emoji_dir(ctx)
        items = await asyncio.to_thread(emoji_store.list_items, emoji_dir)
    except EmojiError as exc:
        return _error(str(exc))

    query = (args.query or "").strip().casefold()
    if query:
        items = [item for item in items if query in str(item.get("name", "")).casefold()]
    total = len(items)
    items = items[: args.limit]
    names = [str(item.get("name", "")) for item in items]
    return {
        "ok": True,
        "status": "inline",
        "brief": f"表情包 {total} 个" + (f"，返回前 {len(items)} 个" if total > len(items) else ""),
        "total": total,
        "returned": len(items),
        "names": names,
        "items": items,
    }


@tool(
    name="save_emoji_from_url",
    description=(
        "把网上的公开图片保存为表情包。先用 web_search 或用户给的链接找到图片直链，"
        "再调用本工具下载到表情包目录。只支持 png/jpg/gif 和公开 http(s) 地址；"
        "同名默认自动加 _1，除非 overwrite=true。"
    ),
    args_model=SaveEmojiFromUrlArgs,
    category="emoji",
)
async def save_emoji_from_url(args: SaveEmojiFromUrlArgs, ctx: ToolContext) -> dict:
    if ctx.emoji_dir is None:
        return _error("表情包目录未配置，表情包工具被禁用")
    try:
        data = await download_remote_image_bytes(
            args.url,
            max_bytes=emoji_store.MAX_UPLOAD_BYTES,
        )
    except ImageAssetError as exc:
        return _error(f"下载图片失败：{exc.safe_message()}")
    try:
        saved = await _persist(
            ctx,
            name=args.name,
            data=data,
            overwrite=args.overwrite,
        )
    except EmojiError as exc:
        return _error(str(exc))
    except OSError as exc:
        logger.warning("保存网络表情包失败: %s", exc)
        return _error(f"保存失败：{exc}")
    return {
        "ok": True,
        "status": "inline",
        "brief": f"已保存表情包：{saved['file']}",
        **saved,
    }


@tool(
    name="save_emoji_from_workspace",
    description=(
        "把本轮消息里的图片保存为表情包。消息中的图片会带 "
        "[图片 workspace=...] 路径，拿这个路径调用本工具即可；也可用于把 "
        "workspace 里已有的图片收进表情包目录。"
    ),
    args_model=SaveEmojiFromWorkspaceArgs,
    category="emoji",
)
async def save_emoji_from_workspace(
    args: SaveEmojiFromWorkspaceArgs,
    ctx: ToolContext,
) -> dict:
    if ctx.emoji_dir is None:
        return _error("表情包目录未配置，表情包工具被禁用")
    try:
        source = resolve_in_workspace(args.path, ctx.workspace_dir)
    except WorkspaceError as exc:
        return _error(str(exc))
    if not source.is_file():
        return _error(f"workspace 里没有这个文件：{args.path}")
    try:
        size = source.stat().st_size
    except OSError as exc:
        return _error(f"读取失败：{exc}")
    if size > emoji_store.MAX_UPLOAD_BYTES:
        return _error(
            f"图片超过 {emoji_store.MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"
        )
    try:
        data = await asyncio.to_thread(source.read_bytes)
        saved = await _persist(
            ctx,
            name=args.name,
            data=data,
            overwrite=args.overwrite,
        )
    except (OSError, EmojiError) as exc:
        return _error(str(exc))
    return {
        "ok": True,
        "status": "inline",
        "brief": f"已从 workspace 保存表情包：{saved['file']}",
        **saved,
    }


@tool(
    name="rename_emoji",
    description="给已有表情包改名。名称不带扩展名；新名称不能与现有名称重复。",
    args_model=RenameEmojiArgs,
    category="emoji",
)
async def rename_emoji(args: RenameEmojiArgs, ctx: ToolContext) -> dict:
    try:
        emoji_dir = _emoji_dir(ctx)
        result = await asyncio.to_thread(
            emoji_store.rename,
            emoji_dir,
            args.name,
            args.new_name,
        )
    except EmojiError as exc:
        return _error(str(exc))
    return {
        "ok": True,
        "status": "inline",
        "brief": f"已改名：{result['from']} → {result['to']}",
        **result,
    }


@tool(
    name="delete_emoji",
    description=(
        "删除一个表情包。删除不可恢复；只在确认是重复图、坏图或用户明确要求时使用。"
        "名称不带扩展名。"
    ),
    args_model=DeleteEmojiArgs,
    category="emoji",
)
async def delete_emoji(args: DeleteEmojiArgs, ctx: ToolContext) -> dict:
    try:
        emoji_dir = _emoji_dir(ctx)
        deleted = await asyncio.to_thread(emoji_store.delete, emoji_dir, args.name)
    except EmojiError as exc:
        return _error(str(exc))
    return {
        "ok": True,
        "status": "inline",
        "brief": f"已删除表情包：{deleted}",
        "deleted": deleted,
    }


__all__ = [
    "delete_emoji",
    "list_emoji",
    "rename_emoji",
    "save_emoji_from_url",
    "save_emoji_from_workspace",
]
