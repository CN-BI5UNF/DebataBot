"""群聊黑话 / 网络热梗管理 API。

学习候选与确认状态由 Runtime 的 SlangService 管理；WebUI 只做管理端操作。
模型侧只会看到 confirmed + meaning 的词条，evidence/sources 仅管理端可见。
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from .deps import require_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/slang", tags=["slang"])

_GROUP_RE = re.compile(r"^group:\d+$")


def _service(request: Request) -> Any:
    rt = require_runtime(request)
    service = getattr(rt, "slang_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="黑话服务尚未就绪")
    return service


def _conversation(value: str) -> str:
    conversation_id = str(value or "").strip()
    if not _GROUP_RE.match(conversation_id):
        raise HTTPException(
            status_code=400, detail="会话 ID 必须是 group:<群号>（黑话只在群聊学习）"
        )
    return conversation_id


def _fail(error: Exception, *, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(error))


@router.get("")
async def list_slang(
    request: Request,
    conversation_id: str = "",
    status: str = "",
) -> dict[str, Any]:
    service = _service(request)
    if conversation_id:
        _conversation(conversation_id)
    entries = service.list_entries(
        conversation_id=conversation_id,
        status=status,
        limit=1000,
    )
    stats = service.stats()
    conversations = service.conversations()
    buffers = {
        item: service.buffer_size(item)
        for item in conversations
        if item.startswith("group:")
    }
    config = service.config
    return {
        "ok": True,
        "persona_id": service.persona_id,
        "entries": entries,
        "stats": stats,
        "conversations": conversations,
        "buffers": buffers,
        "writable": bool(service.store.writable),
        "settings": {
            "enabled": bool(config.enabled),
            "auto_learn": bool(config.auto_learn),
            "extract_min_messages": int(config.extract_min_messages),
            "extract_cooldown_seconds": int(config.extract_cooldown_seconds),
            "inject_max": int(config.inject_max),
            "max_prompt_chars": int(config.max_prompt_chars),
        },
    }


@router.post("")
async def create_slang(request: Request, payload: Annotated[dict, Body()]) -> dict[str, Any]:
    service = _service(request)
    try:
        entry = service.store.add_manual(
            persona_id=service.persona_id,
            conversation_id=_conversation(payload.get("conversation_id", "")),
            content=str(payload.get("content") or ""),
            meaning=str(payload.get("meaning") or ""),
            usage=str(payload.get("usage") or ""),
            example=str(payload.get("example") or ""),
            risk=str(payload.get("risk") or ""),
            status=str(payload.get("status") or "confirmed"),
        )
    except (ValueError, RuntimeError) as exc:
        raise _fail(exc) from exc
    return {"ok": True, "entry": entry, "message": "词条已保存"}


@router.post("/learn")
async def learn_now(request: Request, payload: Annotated[dict, Body()]) -> dict[str, Any]:
    service = _service(request)
    conversation_id = _conversation(payload.get("conversation_id", ""))
    result = await service.learn_now(conversation_id, force=True)
    return result


@router.patch("/{entry_id}")
async def update_slang(
    request: Request,
    entry_id: str,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    service = _service(request)
    allowed = {"content", "meaning", "usage", "example", "risk", "sources", "status"}
    try:
        entry = service.store.update_entry(
            entry_id,
            {key: payload[key] for key in allowed if key in payload},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="词条不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise _fail(exc) from exc
    return {"ok": True, "entry": entry, "message": "词条已更新"}


@router.delete("/{entry_id}")
async def delete_slang(request: Request, entry_id: str) -> dict[str, Any]:
    service = _service(request)
    try:
        deleted = service.store.delete_entry(entry_id)
    except RuntimeError as exc:
        raise _fail(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="词条不存在")
    return {"ok": True, "message": "词条已删除"}


__all__ = ["router"]
