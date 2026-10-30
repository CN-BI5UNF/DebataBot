"""OpenAI 实际载荷与 request dump 的图片安全测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from providers.base import safe_provider_error_text
from providers.protocols.openai_compat import (
    OpenAICompatProvider,
    _raw_prompt_dump_payload,
)


def _data_url() -> str:
    return "data:image/png;base64," + ("A" * 4096)


def _message() -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "原消息文字"},
            {
                "type": "image_url",
                "image_url": {"url": _data_url(), "detail": "auto"},
            },
        ],
        "metadata": {
            "image_assets": [
                {
                    "sha256": "a" * 64,
                    "transform_version": "pillow-first-frame-max2048-v2",
                    "detail": "auto",
                    "byte_size": 1024,
                }
            ]
        },
        "_prompt_section": "volatile_tail",
    }


@pytest.mark.asyncio
async def test_openai_sdk_receives_real_blocks_without_internal_fields():
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

    result = await provider.chat_completion(
        [message],
        model="fake-model",
        stream=False,
    )

    sent = captured["messages"][0]
    assert result.content == "ok"
    assert sent["content"][0] == {"type": "text", "text": "原消息文字"}
    assert sent["content"][1]["type"] == "image_url"
    assert sent["content"][1]["image_url"]["url"] == _data_url()
    assert "metadata" not in sent
    assert "_prompt_section" not in sent
    assert message == original


def test_raw_prompt_dump_redacts_complete_gif_data_url():
    gif_url = "data:image/gif;base64," + ("G" * 4096)
    payload = _raw_prompt_dump_payload(
        provider="fake",
        kwargs={
            "model": "fake-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "动图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": gif_url, "detail": "auto"},
                        },
                    ],
                }
            ],
        },
    )
    dumped = json.dumps(payload, ensure_ascii=False)

    assert "G" * 128 not in dumped
    assert "data:image/gif" not in dumped
    assert "<image-data:mime=image/gif;sha256=" in dumped


def test_raw_prompt_dump_redacts_images_and_does_not_mutate_kwargs():
    message = _message()
    original = json.loads(json.dumps(message))
    text_with_token = (
        "访问 https://user:pass@example.com/image.png?token=SECRET#frag 查看"
    )
    kwargs = {
        "model": "fake-model",
        "messages": [
            message,
            {"role": "user", "content": text_with_token},
        ],
        "extra_body": {"image": {"url": _data_url()}},
    }

    payload = _raw_prompt_dump_payload(provider="fake", kwargs=kwargs)
    dumped = json.dumps(payload, ensure_ascii=False)

    assert "A" * 128 not in dumped
    assert "data:image" not in dumped
    assert "<image-data:mime=image/png;sha256=" in dumped
    assert "SECRET" not in dumped
    assert "user:pass" not in dumped
    assert payload["request"]["messages"][0]["content"][1]["image_url"][
        "url"
    ].startswith("<image-data:mime=image/png;sha256=")
    assert kwargs["messages"][0] == original
    assert kwargs["messages"][0]["content"][1]["image_url"]["url"] == _data_url()


def test_safe_provider_error_text_removes_payload_and_signed_url():
    text = (
        "400 bad image data:image/png;base64,"
        + ("A" * 5000)
        + " from https://user:pass@example.com/a.png?token=SECRET#frag"
    )
    safe = safe_provider_error_text(text)

    assert "A" * 100 not in safe
    assert "data:image" not in safe
    assert "SECRET" not in safe
    assert "user:pass" not in safe
    assert "example.com" in safe
    assert len(safe) <= 300
