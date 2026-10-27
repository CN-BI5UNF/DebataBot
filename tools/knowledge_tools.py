"""LLM-facing knowledge notes distilled from the web.

Two ways to write:

* ``save_knowledge_note`` — the main model already read ``web_search`` results and
  passes a short, checked note. This is the normal path for chat.
* ``research_web_to_prompt`` — delegate search + distillation to the cheap
  summarizer model, which saves the note and returns only the distilled result.

Both write to the persona-scoped store. The main model queries it on demand
through ``list_knowledge_notes``; notes are not auto-injected into task_context.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import ToolContext, tool
from .schemas import (
    DeleteKnowledgeNoteArgs,
    ListKnowledgeArgs,
    ResearchWebToPromptArgs,
    SaveKnowledgeNoteArgs,
)

logger = logging.getLogger(__name__)


def _service(ctx: ToolContext) -> Any:
    return getattr(ctx, "knowledge_service", None)


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "status": "failed", "brief": message, "error": message}


@tool(
    name="save_knowledge_note",
    description=(
        "把已经核对过的网络资料/热梗保存为长期知识笔记。"
        "先用 web_search 查，再自己提炼；不要照搬整段网页，也不要保存隐私或未确认的传闻。"
        "写入前先查重：已有相同/相近条目时服务端不会写入，并会返回已有条目。"
        "之后需要详情时用 list_knowledge_notes 查询。"
    ),
    args_model=SaveKnowledgeNoteArgs,
    category="knowledge",
)
async def save_knowledge_note(args: SaveKnowledgeNoteArgs, ctx: ToolContext) -> dict:
    service = _service(ctx)
    if service is None:
        return _error("知识库未就绪")
    try:
        entry = service.add_note(
            kind=args.kind,
            title=args.title,
            content=args.content,
            usage=args.usage,
            tags=args.tags,
            sources=args.sources,
            source="ai",
            confidence=0.8,
        )
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc))
    if entry.get("duplicate"):
        return {
            "ok": True,
            "status": "inline",
            "brief": f"已有相同/相近知识，未重复写入：{entry.get('title', '')}",
            "duplicate": True,
            "entry": entry,
        }
    return {
        "ok": True,
        "status": "inline",
        "brief": f"已保存知识：{entry.get('title', '')}",
        "entry": entry,
    }


@tool(
    name="research_web_to_prompt",
    description=(
        "让后台小模型联网搜索并整理一个主题/热梗，自动写成知识笔记。"
        "适合需要快速了解当前热梗、新闻背景、版本信息，且不想把整页搜索结果塞进上下文的场景。"
        "会先比对已有条目，相同/相近条目不写入，并返回跳过数量。"
        "返回的是整理后的短笔记，不是原始网页；保存后可用 list_knowledge_notes 查询。"
    ),
    args_model=ResearchWebToPromptArgs,
    category="knowledge",
)
async def research_web_to_prompt(args: ResearchWebToPromptArgs, ctx: ToolContext) -> dict:
    service = _service(ctx)
    if service is None:
        return _error("知识库未就绪")
    result = await service.research(
        args.query,
        kind=args.kind,
        max_notes=args.max_notes,
        save=args.save,
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "brief": str(result.get("error") or "联网考究失败"),
            "error": str(result.get("error") or "联网考究失败"),
        }
    notes = [
        {
            "kind": item.get("kind"),
            "title": item.get("title"),
            "content": item.get("content"),
            "usage": item.get("usage"),
            "sources": item.get("sources"),
        }
        for item in result.get("notes") or []
    ]
    saved = int(result.get("saved") or 0)
    duplicates = int(result.get("duplicates") or 0)
    suffix = f"，跳过 {duplicates} 条重复" if duplicates else ""
    return {
        "ok": True,
        "status": "inline",
        "brief": f"考究完成：新增 {saved} 条笔记{suffix}",
        "query": result.get("query"),
        "saved": saved,
        "duplicates": duplicates,
        "duplicate_titles": list(result.get("duplicate_titles") or []),
        "notes": notes,
    }


@tool(
    name="list_knowledge_notes",
    description=(
        "按需查询知识库中的网络热梗/事实笔记；不会自动注入上下文。"
        "回答近期热梗、新闻背景、版本信息且自己不确定时先查询。"
        "可按标题、内容、标签过滤；返回 id、释义、用法和来源。"
    ),
    args_model=ListKnowledgeArgs,
    category="knowledge",
)
async def list_knowledge_notes(args: ListKnowledgeArgs, ctx: ToolContext) -> dict:
    service = _service(ctx)
    if service is None:
        return _error("知识库未就绪")
    result = service.lookup(
        query=args.query,
        kind=args.kind,
        limit=args.limit,
    )
    if not result.get("ok"):
        return _error(str(result.get("error") or "知识库查询失败"))
    returned = int(result.get("returned") or 0)
    brief = (
        f"查到 {returned} 条知识笔记"
        if returned
        else "知识库暂无匹配笔记"
    )
    return {"ok": True, "status": "inline", "brief": brief, **result}


@tool(
    name="delete_knowledge_note",
    description=(
        "删除一条知识笔记。只在用户明确要求，或确认笔记已过时/错误时使用；"
        "先 list_knowledge_notes 找到 id。"
    ),
    args_model=DeleteKnowledgeNoteArgs,
    category="knowledge",
)
async def delete_knowledge_note(args: DeleteKnowledgeNoteArgs, ctx: ToolContext) -> dict:
    service = _service(ctx)
    if service is None:
        return _error("知识库未就绪")
    try:
        deleted = service.delete_entry(args.entry_id)
    except RuntimeError as exc:
        return _error(str(exc))
    if not deleted:
        return _error("没有这个知识条目")
    return {
        "ok": True,
        "status": "inline",
        "brief": "知识笔记已删除",
        "id": args.entry_id,
    }


__all__ = [
    "delete_knowledge_note",
    "list_knowledge_notes",
    "research_web_to_prompt",
    "save_knowledge_note",
]
