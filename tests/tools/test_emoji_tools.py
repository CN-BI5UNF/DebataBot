"""LLM-facing emoji folder tools."""

from __future__ import annotations

import pytest

from tools import ToolContext, build_default_registry, get_default_specs
from tools.emoji_tools import (
    delete_emoji,
    list_emoji,
    rename_emoji,
    save_emoji_from_url,
    save_emoji_from_workspace,
)
from tools.schemas import (
    DeleteEmojiArgs,
    ListEmojiArgs,
    RenameEmojiArgs,
    SaveEmojiFromUrlArgs,
    SaveEmojiFromWorkspaceArgs,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64


def _ctx(tmp_path) -> ToolContext:
    emoji = tmp_path / "emoji"
    workspace = tmp_path / "workspace"
    emoji.mkdir()
    workspace.mkdir()
    return ToolContext(emoji_dir=emoji, workspace_dir=workspace)


@pytest.mark.asyncio
async def test_list_save_from_workspace_rename_delete(tmp_path):
    ctx = _ctx(tmp_path)
    empty = await list_emoji(ListEmojiArgs(), ctx)
    assert empty["ok"] is True
    assert empty["total"] == 0

    source = ctx.workspace_dir / "incoming" / "group-image.png"
    source.parent.mkdir()
    source.write_bytes(PNG)
    saved = await save_emoji_from_workspace(
        SaveEmojiFromWorkspaceArgs(name="猫猫震惊", path="incoming/group-image.png"),
        ctx,
    )
    assert saved["ok"] is True, saved
    assert saved["file"] == "猫猫震惊.png"
    assert (ctx.emoji_dir / "猫猫震惊.png").read_bytes() == PNG

    listed = await list_emoji(ListEmojiArgs(query="猫"), ctx)
    assert listed["names"] == ["猫猫震惊"]

    renamed = await rename_emoji(
        RenameEmojiArgs(name="猫猫震惊", new_name="猫猫"),
        ctx,
    )
    assert renamed["ok"] is True
    assert (ctx.emoji_dir / "猫猫.png").is_file()

    deleted = await delete_emoji(DeleteEmojiArgs(name="猫猫"), ctx)
    assert deleted["ok"] is True
    assert deleted["deleted"] == "猫猫.png"
    assert list(ctx.emoji_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_save_from_workspace_rejects_traversal_and_non_image(tmp_path):
    ctx = _ctx(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    traversal = await save_emoji_from_workspace(
        SaveEmojiFromWorkspaceArgs(name="x", path="../outside.png"),
        ctx,
    )
    assert traversal["ok"] is False

    text = ctx.workspace_dir / "notes.txt"
    text.write_text("not an image", encoding="utf-8")
    bad = await save_emoji_from_workspace(
        SaveEmojiFromWorkspaceArgs(name="x", path="notes.txt"),
        ctx,
    )
    assert bad["ok"] is False
    assert "png/jpg/gif" in bad["error"]


@pytest.mark.asyncio
async def test_save_from_url_uses_guarded_download(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls: list[str] = []

    async def fake_download(url: str, *, max_bytes: int = 0) -> bytes:
        calls.append(url)
        assert max_bytes > 0
        return GIF

    monkeypatch.setattr(
        "tools.emoji_tools.download_remote_image_bytes",
        fake_download,
    )
    saved = await save_emoji_from_url(
        SaveEmojiFromUrlArgs(name="摇头", url="https://example.com/a.gif"),
        ctx,
    )
    assert saved["ok"] is True, saved
    assert saved["file"] == "摇头.gif"
    assert calls == ["https://example.com/a.gif"]


@pytest.mark.asyncio
async def test_overwrite_replaces_same_name(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    first = ctx.emoji_dir / "旧图.png"
    first.write_bytes(PNG)

    async def fake_download(url: str, *, max_bytes: int = 0) -> bytes:
        return PNG + b"new"

    monkeypatch.setattr(
        "tools.emoji_tools.download_remote_image_bytes",
        fake_download,
    )
    saved = await save_emoji_from_url(
        SaveEmojiFromUrlArgs(
            name="旧图",
            url="https://example.com/new.png",
            overwrite=True,
        ),
        ctx,
    )
    assert saved["ok"] is True, saved
    assert saved["file"] == "旧图.png"
    assert saved["deduped"] is False
    assert first.read_bytes().endswith(b"new")


def test_emoji_tools_are_registered_as_low_frequency_stubs(tmp_path):
    from tests.tools.helpers import _make_config

    names = {spec.name for spec in get_default_specs()}
    assert {
        "list_emoji",
        "save_emoji_from_url",
        "save_emoji_from_workspace",
        "rename_emoji",
        "delete_emoji",
    } <= names

    registry = build_default_registry(_make_config())
    executor = registry.get_executor(ToolContext(emoji_dir=tmp_path / "emoji"))
    assert executor is not None
