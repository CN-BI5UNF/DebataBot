"""直接写角色提示词的 WebUI 契约。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "webui/static/js"


def test_personas_page_offers_direct_prompt_mode():
    page = (JS / "pages/personas.js").read_text(encoding="utf-8")
    dialogs = (JS / "persona_dialogs.js").read_text(encoding="utf-8")
    assert "直接写提示词" in page
    assert "直接编辑" in page
    assert "openPersonaDirectEditor" in page
    assert "export async function openPersonaDirectEditor" in dialogs
    assert "raw: true" in dialogs
    assert "20000" in dialogs
    assert "endpoints.personaSource" in dialogs
    assert "PERSONA_PROMPT" in dialogs


def test_creator_links_to_direct_mode():
    dialogs = (JS / "persona_dialogs.js").read_text(encoding="utf-8")
    assert "不用 AI，直接写提示词" in dialogs
    assert "openPersonaDirectEditor(ctx, null" in dialogs
