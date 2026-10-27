"""LLM-facing group slang lookup.

Slang is no longer injected into task_context. The model calls ``lookup_slang``
only when it encounters an unfamiliar abbreviation, homophone, or group meme.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import ToolContext, tool
from .schemas import LookupSlangArgs

logger = logging.getLogger(__name__)


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "status": "failed", "brief": message, "error": message}


@tool(
    name="lookup_slang",
    description=(
        "查询当前群已确认的黑话/网络用语/缩写/群梗。"
        "遇到看不懂的词、谐音、缩写或明显需要群内语境才能理解的发言时调用；"
        "留空 query 可查看本群常用词条。黑话不会自动注入上下文，需要时主动查。"
    ),
    args_model=LookupSlangArgs,
    category="slang",
)
async def lookup_slang(args: LookupSlangArgs, ctx: ToolContext) -> dict:
    service = getattr(ctx, "slang_service", None)
    if service is None:
        return _error("黑话库未就绪")
    conversation_id = str(ctx.conversation_id or "").strip()
    if not conversation_id:
        return _error("当前会话未知，无法查询黑话")
    try:
        result = service.lookup(
            conversation_id,
            query=args.query,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 - 查询失败返回给模型
        logger.warning("lookup_slang 失败: %s", exc)
        return _error(f"黑话查询失败：{exc}")
    if not result.get("ok"):
        return _error(str(result.get("error") or "黑话查询失败"))
    returned = int(result.get("returned") or 0)
    brief = (
        f"查到 {returned} 条本群黑话"
        if returned
        else "本群暂无匹配的黑话词条"
    )
    return {"ok": True, "status": "inline", "brief": brief, **result}


__all__ = ["lookup_slang"]
