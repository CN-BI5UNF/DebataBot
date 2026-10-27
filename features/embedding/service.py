"""Embedding 服务接口 + OpenAI 兼容实现骨架。

Claude 定义 IEmbeddingService 接口和构造签名，DeepSeek 实装具体方法
（见 docs/deepseek_tasks.md 任务 1）。

OpenAI 兼容协议示例端点：
    POST {base_url}/embeddings
    Body: {"model": "text-embedding-v1", "input": "你好" 或 ["a", "b"]}
    Header: Authorization: Bearer {api_key}
返回：
    {"data": [{"embedding": [0.1, ...], "index": 0}, ...]}
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from providers.base import Usage

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """embedding 调用相关异常。"""


class IEmbeddingService(ABC):
    """文本向量化的抽象接口。"""

    async def warmup(self) -> None:
        """预热（加载本地模型 / 建立 HTTP 连接池）。

        默认 no-op：API 实现不需要预热；本地模型实现应 override。
        Runtime 启动后会 fire-and-forget 调一次。
        """
        return None

    @abstractmethod
    async def embed_one(self, text: str) -> list[float]:
        """把一段文本变成一个向量。"""

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量 embed；返回顺序与输入一致。"""

    @abstractmethod
    async def aclose(self) -> None:
        """释放底层连接（httpx client / 本地模型等）。"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """向量维度。第一次成功 embed 前可能返回 0。"""


class OpenAICompatEmbeddingService(IEmbeddingService):
    """走 OpenAI 兼容 /embeddings 端点。

    适配：OpenAI / DeepSeek / 智谱 / 火山方舟 / 硅基流动 / OpenRouter ……
    所有 OpenAI 兼容 LLM 平台几乎都提供 /v1/embeddings。

    实装由 DeepSeek 完成。Claude 只保留接口与构造签名。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        *,
        provider_id: str = "",
        usage_recorder: (
            Callable[[Usage, dict[str, Any]], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.provider_id = provider_id
        self.usage_recorder = usage_recorder
        self._client: httpx.AsyncClient | None = None
        self._dim: int = 0

    def _get_client(self) -> httpx.AsyncClient:
        """懒创建 httpx 客户端。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=min(3.0, self.timeout)),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def _request(self, inputs: list[str]) -> list[dict]:
        """发送 POST /embeddings 请求，返回 data 列表。"""
        client = self._get_client()
        endpoint = "/embeddings"
        payload = {"model": self.model, "input": inputs}
        if _is_volcengine_multimodal_embedding(self.model):
            endpoint = "/embeddings/multimodal"
            payload = {
                "model": self.model,
                "input": [{"type": "text", "text": text} for text in inputs],
            }
        try:
            resp = await client.post(
                endpoint,
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
            raw_data = body.get("data") if isinstance(body, dict) else None
            data = _normalize_embedding_data(raw_data)
            if not data:
                raise EmbeddingError(f"embedding 响应缺少 data 字段：{resp.text[:200]}")
            if isinstance(body, dict):
                await self._record_usage(body.get("usage"))
            # 缓存维度
            if self._dim == 0 and data:
                self._dim = len(data[0]["embedding"])
            return data
        except httpx.HTTPStatusError as e:
            body = e.response.text.strip()[:500]
            raise EmbeddingError(
                f"embedding 请求失败：HTTP {e.response.status_code}，响应：{body or '<empty>'}"
            ) from e
        except (httpx.TimeoutException, httpx.RequestError) as e:
            raise EmbeddingError(f"embedding 请求失败：{e}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise EmbeddingError(f"embedding 响应解析失败：{e}") from e

    async def _record_usage(self, raw: Any) -> None:
        """Embedding endpoints usually report prompt tokens only; count them in V2."""
        if self.usage_recorder is None or not isinstance(raw, dict):
            return
        try:
            prompt = int(
                raw.get("prompt_tokens")
                or raw.get("input_tokens")
                or raw.get("total_tokens")
                or 0
            )
            total = int(raw.get("total_tokens") or prompt)
        except (TypeError, ValueError):
            return
        if prompt <= 0 and total <= 0:
            return
        usage = Usage(
            prompt_tokens=max(0, prompt),
            completion_tokens=0,
            total_tokens=max(0, total or prompt),
        )
        try:
            outcome = self.usage_recorder(
                usage,
                {
                    "provider": self.provider_id,
                    "model": self.model,
                    "agent": "embedding",
                    "operation": "embedding",
                },
            )
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - 统计失败不能拖垮 RAG
            logger.debug("记录 embedding usage 失败", exc_info=True)

    async def embed_one(self, text: str) -> list[float]:
        data = await self._request([text])
        return data[0]["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if _is_volcengine_multimodal_embedding(self.model):
            return [await self.embed_one(text) for text in texts]
        data = await self._request(texts)
        return [item["embedding"] for item in data]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def dimension(self) -> int:
        return self._dim


def _is_volcengine_multimodal_embedding(model: str) -> bool:
    return model.startswith("doubao-embedding-vision-")


def _normalize_embedding_data(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


__all__ = ["EmbeddingError", "IEmbeddingService", "OpenAICompatEmbeddingService"]
