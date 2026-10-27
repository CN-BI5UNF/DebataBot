"""Auxiliary model calls must land in the same V2 usage store as chat."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from features.embedding.service import OpenAICompatEmbeddingService
from features.vision.vision_service import VisionService
from providers.base import Usage


class FakeVisionProvider:
    async def chat_completion(self, messages, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {"summary": "一张图", "description": "图里有一只猫"},
                ensure_ascii=False,
            ),
            usage=Usage(
                prompt_tokens=100,
                completion_tokens=20,
                cached_tokens=40,
                total_tokens=120,
            ),
        )


@pytest.mark.asyncio
async def test_vision_records_usage_as_v2_operation():
    recorded = []

    async def recorder(usage, metadata):
        recorded.append((usage, metadata))

    service = VisionService(
        FakeVisionProvider(),
        model="vision-model",
        provider_id="vision-provider",
        usage_recorder=recorder,
    )
    result = await service.describe("https://example.com/a.png")
    assert result["description"] == "图里有一只猫"
    assert len(recorded) == 1
    usage, metadata = recorded[0]
    assert usage.prompt_tokens == 100
    assert usage.cached_tokens == 40
    assert metadata == {
        "provider": "vision-provider",
        "model": "vision-model",
        "agent": "vision",
        "operation": "describe_image",
    }


@pytest.mark.asyncio
async def test_embedding_records_usage_as_v2_operation():
    recorded = []

    async def recorder(usage, metadata):
        recorded.append((usage, metadata))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 33, "total_tokens": 33},
            },
        )

    service = OpenAICompatEmbeddingService(
        base_url="https://embedding.example.com/v1",
        api_key="key",
        model="embed-model",
        provider_id="embedding-provider",
        usage_recorder=recorder,
    )
    service._client = httpx.AsyncClient(
        base_url=service.base_url,
        transport=httpx.MockTransport(handler),
    )
    vector = await service.embed_one("你好")
    assert vector == [0.1, 0.2]
    assert len(recorded) == 1
    usage, metadata = recorded[0]
    assert usage.prompt_tokens == 33
    assert usage.total_tokens == 33
    assert metadata["operation"] == "embedding"
    assert metadata["provider"] == "embedding-provider"
    await service.aclose()
