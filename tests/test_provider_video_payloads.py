"""OpenAI/Anthropic 视频 payload 与日志脱敏。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.runner import AgentRunner
from providers.base import ProviderUnsupportedError, safe_provider_error_text
from providers.protocols.anthropic_proto import AnthropicProvider
from providers.protocols.openai_compat import (
    OpenAICompatProvider,
    _raw_prompt_dump_payload,
)
from utils.video_contracts import (
    VIDEO_TRANSFORM_VERSION,
    VideoLimits,
    video_asset_relative_path,
)


def _video_url() -> str:
    return "data:video/mp4;base64," + ("V" * 4096)


def _message() -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "看完整视频"},
            {
                "type": "video_url",
                "video_url": {"url": _video_url()},
            },
        ],
        "metadata": {"video_assets": [{"sha256": "a" * 64}]},
    }


@pytest.mark.asyncio
async def test_openai_compat_passes_video_url_block_to_sdk():
    captured: dict = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="ok",
                            tool_calls=None,
                            reasoning_content="",
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
                model="fake-model",
            )

    provider = OpenAICompatProvider(
        "fake_openai",
        base_url="https://example.com/v1",
        api_key="test",
    )
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    message = _message()
    original = json.loads(json.dumps(message))

    await provider.chat_completion([message], model="fake-model", stream=False)

    sent = captured["messages"][0]
    assert sent["content"][1]["type"] == "video_url"
    assert sent["content"][1]["video_url"]["url"] == _video_url()
    assert "metadata" not in sent
    assert message == original


def test_raw_prompt_dump_redacts_video_data_url():
    payload = _raw_prompt_dump_payload(
        provider="fake",
        kwargs={"model": "fake-model", "messages": [_message()]},
    )
    dumped = json.dumps(payload, ensure_ascii=False)

    assert "V" * 128 not in dumped
    assert "data:video" not in dumped
    assert "<video-data:mime=video/mp4;sha256=" in dumped


def test_safe_provider_error_text_removes_video_payload():
    safe = safe_provider_error_text(
        "400 bad video " + _video_url() + " https://example.com/a.mp4?token=SECRET"
    )
    assert "V" * 100 not in safe
    assert "data:video" not in safe
    assert "SECRET" not in safe
    assert "<video-data>" in safe


def test_runner_media_guard_rejects_video_over_limit():
    url = _video_url()
    refs = []
    blocks = []
    for index in range(2):
        sha = f"{index + 1:064x}"
        refs.append(
            {
                "sha256": sha,
                "mime": "video/mp4",
                "byte_size": 1024,
                "path": video_asset_relative_path(sha, "video/mp4"),
                "message_id": "m1",
                "index": index,
                "transform_version": VIDEO_TRANSFORM_VERSION,
            }
        )
        blocks.append({"type": "video_url", "video_url": {"url": url}})
    message = {
        "role": "user",
        "content": [{"type": "text", "text": "x"}, *blocks],
        "metadata": {
            "messages": [{"message_id": "m1"}],
            "video_assets": refs,
        },
    }

    with pytest.raises(ProviderUnsupportedError, match="视频资源超出限制"):
        AgentRunner._guard_outgoing_media(
            [message],
            None,
            None,
            VideoLimits(max_videos_per_request=1),
        )


def test_anthropic_rejects_video_input_explicitly():
    provider = AnthropicProvider(
        "fake_anthropic",
        base_url="https://example.com",
        api_key="test",
    )
    with pytest.raises(ProviderUnsupportedError, match="不支持视频输入"):
        provider._convert_messages([_message()])
