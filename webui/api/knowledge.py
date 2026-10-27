"""Knowledge / web-research notebook API.

The Runtime owns the persona-scoped store; WebUI only lists, edits, deletes and
triggers research. Raw search results are not persisted here: only the
summarizer model's validated notes are stored.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from .deps import require_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _service(request: Request) -> Any:
    rt = require_runtime(request)
    service = getattr(rt, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务尚未就绪")
    return service


@router.get("")
async def list_knowledge(
    request: Request,
    kind: str = "",
    status: str = "",
    query: str = "",
) -> dict[str, Any]:
    service = _service(request)
    entries = service.list_entries(kind=kind, status=status, query=query, limit=1000)
    config = service.config
    return {
        "ok": True,
        "persona_id": service.persona_id,
        "entries": entries,
        "stats": service.stats(),
        "writable": bool(service.store.writable),
        "state": service.get_state(),
        "settings": {
            "enabled": bool(config.enabled),
            "inject_max": int(config.inject_max),
            "max_prompt_chars": int(config.max_prompt_chars),
            "auto_research_enabled": bool(config.auto_research_enabled),
            "research_interval_hours": int(config.research_interval_hours),
            "research_queries": list(config.research_queries),
            "research_max_notes": int(config.research_max_notes),
        },
    }


@router.post("")
async def create_knowledge(
    request: Request,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    service = _service(request)
    try:
        entry = service.add_manual(
            kind=str(payload.get("kind") or "note"),
            title=str(payload.get("title") or ""),
            content=str(payload.get("content") or ""),
            usage=str(payload.get("usage") or ""),
            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else [],
            sources=payload.get("sources") if isinstance(payload.get("sources"), list) else [],
            status=str(payload.get("status") or "active"),
            shared=bool(payload.get("shared", False)),
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "entry": entry, "message": "知识笔记已保存"}


@router.patch("/{entry_id}")
async def update_knowledge(
    request: Request,
    entry_id: str,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    service = _service(request)
    allowed = {
        "kind",
        "title",
        "content",
        "usage",
        "tags",
        "sources",
        "status",
        "shared",
        "confidence",
    }
    try:
        entry = service.update_entry(
            entry_id,
            {key: payload[key] for key in allowed if key in payload},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="知识条目不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "entry": entry, "message": "知识笔记已更新"}


@router.delete("/{entry_id}")
async def delete_knowledge(request: Request, entry_id: str) -> dict[str, Any]:
    service = _service(request)
    try:
        deleted = service.delete_entry(entry_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="知识条目不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return {"ok": True, "message": "知识笔记已删除"}


@router.post("/research")
async def research(
    request: Request,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    service = _service(request)
    try:
        max_notes = int(payload.get("max_notes") or 8)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="max_notes 必须是整数") from exc
    if not 1 <= max_notes <= 8:
        raise HTTPException(status_code=400, detail="max_notes 需要在 1–8 之间")
    result = await service.research(
        str(payload.get("query") or ""),
        kind=str(payload.get("kind") or "note"),
        max_notes=max_notes,
        save=bool(payload.get("save", True)),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "考究失败"))
    return result


@router.post("/trends")
async def refresh_trends(
    request: Request,
    payload: Annotated[dict, Body()] | None = None,
) -> dict[str, Any]:
    service = _service(request)
    force = True
    if isinstance(payload, dict) and "force" in payload:
        force = bool(payload.get("force"))
    result = await service.run_trend_research(force=force)
    if result.get("skipped"):
        return {"ok": True, **result}
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail="热梗考究失败；请查看日志")
    return result


@router.post("/dedupe")
async def dedupe(
    request: Request,
    payload: Annotated[dict, Body()] | None = None,
) -> dict[str, Any]:
    """合并知识库中的保守重复项；默认只预览，不写盘。"""
    service = _service(request)
    data = payload if isinstance(payload, dict) else {}
    dry_run = bool(data.get("dry_run", True))
    persona_id = str(data.get("persona_id") or "").strip()
    result = service.dedupe(
        persona_id=persona_id or None,
        dry_run=dry_run,
    )
    return {"ok": True, **result}


__all__ = ["router"]
