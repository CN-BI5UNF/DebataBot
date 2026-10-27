"""直接提示词 API：raw 模式不经过 AI，且能保留元数据。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from webui.api.personas import read_prompt_source, save_persona


@pytest.fixture
def persona_root(tmp_path):
    (tmp_path / "personas").mkdir()
    return SimpleNamespace(PERSONAS_DIR=tmp_path / "personas", DATA_DIR=tmp_path)


def _request(persona_root):
    rt = SimpleNamespace(paths=persona_root)
    host = SimpleNamespace(rt=rt, state="running", last_error=None)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host=host)))


@pytest.mark.asyncio
async def test_save_raw_prompt_without_ai_or_personality_field(persona_root):
    request = _request(persona_root)
    content = "你是谁\n\n直接写，没有 AI。\n含 \\ 反斜杠和 ''' 三引号。"
    result = await save_persona(
        request,
        {
            "name": "小晚",
            "xml": content,
            "raw": True,
            "display_name": "小晚",
            "gender": "female",
            "age": 24,
            "admins": [{"qq": 12345, "name": "老王"}],
        },
    )
    assert result["ok"] is True
    assert result["created"] is True
    assert result["raw"] is True
    assert result["size"] == len(content)
    source = await read_prompt_source(request, "小晚")
    assert source["xml"] == content
    assert source["vars"]["name"] == "小晚"
    assert source["vars"]["age"] == 24
    assert source["vars"]["admins"] == [{"qq": 12345, "name": "老王", "role": "owner"}]


@pytest.mark.asyncio
async def test_legacy_ai_payload_still_works(persona_root):
    request = _request(persona_root)
    result = await save_persona(
        request,
        {
            "name": "linwan",
            "xml": "<persona>old mode</persona>",
            "personality": "安静",
        },
    )
    assert result["created"] is True
    source = await read_prompt_source(request, "linwan")
    assert "old mode" in source["xml"]


@pytest.mark.asyncio
async def test_ai_overwrite_without_brief_preserves_admin_vars(persona_root):
    request = _request(persona_root)
    await save_persona(
        request,
        {
            "name": "小晚",
            "xml": "第一版",
            "raw": True,
            "admins": [{"qq": 42, "name": "管理员"}],
            "gender": "female",
        },
    )
    await save_persona(
        request,
        {
            "name": "小晚",
            "xml": "AI 改写版",
            "overwrite": True,
        },
    )
    source = await read_prompt_source(request, "小晚")
    assert source["xml"] == "AI 改写版"
    assert source["vars"]["admins"] == [{"qq": 42, "name": "管理员", "role": "owner"}]
    assert source["vars"]["gender"] == "female"
