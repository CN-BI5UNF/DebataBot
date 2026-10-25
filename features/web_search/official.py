"""模型官方搜索通道。

只实现预设中已确认存在官方搜索 API 的通道；没有 ``search_channel`` 的模型
不会出现在设置里，也不会被猜测调用。官方搜索会产生模型 API 费用。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from providers.base import headers_with_neutral_user_agent

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "请联网搜索并回答下面的问题。只保留可核实的事实，并给出实际来源 URL；"
    "不要编造来源，也不要复述本提示。\n\n问题：{query}"
)
_MAX_QUERY_CHARS = 2000


class OfficialSearchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OfficialSearchClient:
    """单个 provider/model 的官方搜索通道。"""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str,
        channel: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.provider_id = provider_id
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.channel = channel
        self.payload = dict(payload or {})
        self.extra_headers = dict(extra_headers or {})
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    def status(self) -> dict[str, Any]:
        configured = bool(self.api_key and self.base_url and self.channel)
        return {
            "configured": configured,
            "provider": self.provider_id,
            "model": self.model,
            "channel": self.channel,
            "message": (
                "官方搜索已配置（调用时按模型/搜索 API 计费）"
                if configured
                else "官方搜索缺少 API Key、base_url 或搜索通道"
            ),
        }

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        timeout_seconds: float | None = None,
    ) -> str:
        if not self.api_key:
            raise OfficialSearchError("missing_key", "官方搜索缺少 API Key")
        query = query.strip()[:_MAX_QUERY_CHARS]
        if not query:
            return "（搜索词为空）"
        timeout = max(5.0, float(timeout_seconds or self.timeout_seconds))
        if self.channel == "openai_responses":
            answer, citations = await self._openai_responses(query, timeout, max_results)
        elif self.channel == "anthropic_messages":
            answer, citations = await self._anthropic_messages(query, timeout, max_results)
        elif self.channel == "gemini_generate_content":
            answer, citations = await self._gemini_generate_content(
                query, timeout, max_results
            )
        elif self.channel in {
            "openai_chat",
            "mimo_chat_web_search",
            "qwen_enable_search",
            "openrouter_web_plugin",
            "xai_responses_web_search",
        }:
            if self.channel == "xai_responses_web_search":
                answer, citations = await self._openai_responses(
                    query, timeout, max_results
                )
            else:
                answer, citations = await self._openai_chat(
                    query, timeout, max_results
                )
        elif self.channel == "deepseek_tools_search":
            return await self._tools_search(
                query, timeout, max_results,
                default_path="/tools/search",
                result_key="results",
            )
        elif self.channel == "kimi_tools_search":
            return await self._tools_search(
                query, timeout, max_results,
                default_path="/tools/search",
                result_key="search_results",
            )
        elif self.channel == "glm_standalone_web_search":
            return await self._glm_standalone(query, timeout, max_results)
        else:
            raise OfficialSearchError(
                "unsupported_channel",
                f"不支持的官方搜索通道：{self.channel}",
            )
        return self._format(answer, citations)

    # -- protocol adapters -------------------------------------

    def _headers(self, *, anthropic: bool = False, gemini: bool = False) -> dict[str, str]:
        headers = headers_with_neutral_user_agent(self.extra_headers)
        if anthropic:
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = str(
                self.payload.get("anthropic_version") or "2023-06-01"
            )
        elif gemini:
            headers["x-goog-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10.0),
                follow_redirects=False,
                headers=headers,
            ) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise OfficialSearchError("timeout", f"官方搜索超时：{exc}") from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300]
            raise OfficialSearchError(
                "http_error",
                f"官方搜索 HTTP {exc.response.status_code}: {detail}",
            ) from exc
        except httpx.HTTPError as exc:
            raise OfficialSearchError("network_error", f"官方搜索连接失败：{exc}") from exc
        except ValueError as exc:
            raise OfficialSearchError("parse_failed", f"官方搜索响应解析失败：{exc}") from exc
        if not isinstance(data, dict):
            raise OfficialSearchError("parse_failed", "官方搜索响应不是 JSON 对象")
        return data

    async def _openai_responses(
        self, query: str, timeout: float, max_results: int
    ) -> tuple[str, list[tuple[str, str]]]:
        url = self.base_url + "/responses"
        tool_type = str(
            self.payload.get("tool_type") or "web_search_preview"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "input": _DEFAULT_PROMPT.format(query=query),
            "max_output_tokens": int(self.payload.get("max_output_tokens") or 1024),
            "tools": [{"type": tool_type}],
        }
        data = await self._post(
            url, payload, timeout=timeout, headers=self._headers()
        )
        texts: list[str] = []
        citations: list[tuple[str, str]] = []
        top_text = data.get("output_text")
        if isinstance(top_text, str) and top_text.strip():
            texts.append(top_text.strip())
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
                for annotation in block.get("annotations") or []:
                    if not isinstance(annotation, dict):
                        continue
                    if annotation.get("type") in {
                        "url_citation", "web_search_result_location",
                    }:
                        citations.append(
                            (
                                str(annotation.get("title") or ""),
                                str(annotation.get("url") or ""),
                            )
                        )
        return _dedupe_texts(texts), _dedupe_citations(
            citations, max_results
        )

    async def _anthropic_messages(
        self, query: str, timeout: float, max_results: int
    ) -> tuple[str, list[tuple[str, str]]]:
        base = self.base_url
        url = base + "/messages" if base.endswith("/v1") else base + "/v1/messages"
        tool_type = str(
            self.payload.get("tool_type") or "web_search_20250305"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(self.payload.get("max_tokens") or 1024),
            "messages": [
                {"role": "user", "content": _DEFAULT_PROMPT.format(query=query)}
            ],
            "tools": [
                {
                    "type": tool_type,
                    "name": "web_search",
                    "max_uses": int(self.payload.get("max_uses") or 3),
                }
            ],
        }
        data = await self._post(
            url, payload, timeout=timeout, headers=self._headers(anthropic=True)
        )
        texts: list[str] = []
        citations: list[tuple[str, str]] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
                for citation in block.get("citations") or []:
                    if isinstance(citation, dict):
                        citations.append(
                            (
                                str(citation.get("title") or ""),
                                str(citation.get("url") or ""),
                            )
                        )
            elif block.get("type") == "web_search_tool_result":
                for result in block.get("content") or []:
                    if isinstance(result, dict):
                        citations.append(
                            (
                                str(result.get("title") or ""),
                                str(result.get("url") or ""),
                            )
                        )
        return _dedupe_texts(texts), _dedupe_citations(
            citations, max_results
        )

    async def _gemini_generate_content(
        self, query: str, timeout: float, max_results: int
    ) -> tuple[str, list[tuple[str, str]]]:
        model = self.model.removeprefix("models/")
        base_url = str(self.payload.get("base_url") or self.base_url).rstrip("/")
        url = f"{base_url}/models/{model}:generateContent"
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": _DEFAULT_PROMPT.format(query=query)}],
                }
            ],
            "tools": [{"google_search": {}}],
            "generationConfig": {
                "temperature": float(self.payload.get("temperature") or 0.2),
                "maxOutputTokens": int(
                    self.payload.get("max_output_tokens") or 1024
                ),
            },
        }
        data = await self._post(
            url, payload, timeout=timeout, headers=self._headers(gemini=True)
        )
        texts: list[str] = []
        citations: list[tuple[str, str]] = []
        for candidate in data.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
            metadata = candidate.get("groundingMetadata") or {}
            for chunk in metadata.get("groundingChunks") or []:
                web = chunk.get("web") if isinstance(chunk, dict) else None
                if isinstance(web, dict):
                    citations.append(
                        (
                            str(web.get("title") or ""),
                            str(web.get("uri") or ""),
                        )
                    )
        return _dedupe_texts(texts), _dedupe_citations(
            citations, max_results
        )

    async def _openai_chat(
        self, query: str, timeout: float, max_results: int
    ) -> tuple[str, list[tuple[str, str]]]:
        url = self.base_url + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": _DEFAULT_PROMPT.format(query=query)}
            ],
            "max_tokens": int(self.payload.get("max_tokens") or 1024),
            "temperature": float(self.payload.get("temperature") or 0.2),
        }
        # 预设声明的是官方 API 的通道字段；只透传已确认的字段，不猜。
        for key, value in self.payload.items():
            if key not in {
                "max_tokens", "temperature", "tool_type", "max_output_tokens",
            }:
                payload[key] = value
        data = await self._post(
            url, payload, timeout=timeout, headers=self._headers()
        )
        texts: list[str] = []
        citations: list[tuple[str, str]] = []
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"].strip())
            for annotation in message.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                payload = annotation.get("url_citation")
                citation = payload if isinstance(payload, dict) else annotation
                citations.append(
                    (
                        str(citation.get("title") or ""),
                        str(citation.get("url") or ""),
                    )
                )
            for citation in message.get("citations") or []:
                if isinstance(citation, dict):
                    citations.append(
                        (
                            str(citation.get("title") or ""),
                            str(citation.get("url") or ""),
                        )
                    )
            search_info = message.get("search_info")
            if isinstance(search_info, dict):
                for result in search_info.get("search_results") or []:
                    if isinstance(result, dict):
                        citations.append(
                            (
                                str(result.get("title") or ""),
                                str(result.get("url") or ""),
                            )
                        )
        search_info = data.get("output")
        if isinstance(search_info, dict):
            search_info = search_info.get("search_info")
        if isinstance(search_info, dict):
            for result in search_info.get("search_results") or []:
                if isinstance(result, dict):
                    citations.append(
                        (
                            str(result.get("title") or ""),
                            str(result.get("url") or ""),
                        )
                    )
        return _dedupe_texts(texts), _dedupe_citations(
            citations, max_results
        )

    async def _tools_search(
        self,
        query: str,
        timeout: float,
        max_results: int,
        *,
        default_path: str,
        result_key: str,
    ) -> str:
        """DeepSeek / Kimi 等独立 Search API 的通用解析。"""
        path = str(self.payload.get("path") or default_path)
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        request = self.payload.get("request")
        query_field = str(self.payload.get("query_field") or "query")
        body: dict[str, Any] = {query_field: query}
        if bool(self.payload.get("include_count", True)):
            body["count"] = max_results
        if isinstance(request, dict):
            body.update(request)
        data = await self._post(
            url, body, timeout=timeout, headers=self._headers()
        )
        raw_items = data.get(result_key)
        if not isinstance(raw_items, list):
            raw_items = data.get("results") or data.get("search_results") or []
        items: list[tuple[str, str, str]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            link = str(raw.get("url") or raw.get("link") or "").strip()
            snippet = str(
                raw.get("snippet")
                or raw.get("summary")
                or raw.get("content")
                or ""
            ).strip()
            if not snippet and isinstance(raw.get("chunks"), list):
                snippet = " ".join(
                    str(chunk.get("text") or "")
                    for chunk in raw["chunks"]
                    if isinstance(chunk, dict)
                ).strip()
            if title and link:
                items.append((title, link, snippet))
            if len(items) >= max_results:
                break
        return self._format_result_items(items, query)

    async def _glm_standalone(
        self, query: str, timeout: float, max_results: int
    ) -> str:
        path = str(self.payload.get("path") or "/web_search")
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        request = self.payload.get("request")
        body: dict[str, Any] = {
            "search_query": query,
            "search_engine": str(
                self.payload.get("search_engine") or "search_std"
            ),
            "search_intent": False,
            "count": max_results,
        }
        if isinstance(request, dict):
            body.update(request)
        data = await self._post(
            url, body, timeout=timeout, headers=self._headers()
        )
        raw_items = (
            data.get("search_result")
            or data.get("search_results")
            or data.get("results")
            or []
        )
        items: list[tuple[str, str, str]] = []
        if isinstance(raw_items, list):
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("title") or "").strip()
                link = str(raw.get("link") or raw.get("url") or "").strip()
                snippet = str(raw.get("content") or raw.get("snippet") or "").strip()
                if title and link:
                    items.append((title, link, snippet))
                if len(items) >= max_results:
                    break
        return self._format_result_items(items, query)

    @staticmethod
    def _format_result_items(
        items: list[tuple[str, str, str]], query: str
    ) -> str:
        if not items:
            return f"无相关结果：{query}"
        lines = [
            f"{index}. {title}\n   {snippet}\n   {url}"
            for index, (title, url, snippet) in enumerate(items, 1)
        ]
        return "\n\n".join(lines)

    @staticmethod
    def _format(answer: str, citations: list[tuple[str, str]]) -> str:
        text = answer.strip() or "（官方搜索未返回摘要）"
        if citations:
            lines = [f"{index}. {title or url}\n   {url}" for index, (title, url) in enumerate(citations, 1) if url]
            if lines:
                text += "\n\n来源：\n" + "\n".join(lines)
        return text


def _dedupe_texts(texts: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return "\n".join(out)


def _dedupe_citations(
    citations: list[tuple[str, str]], max_results: int
) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for title, url in citations:
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append((title.strip(), url))
        if len(out) >= max(1, max_results):
            break
    return out


def build_official_search_client(
    *,
    provider_id: str,
    provider_config: Any,
    model: str,
    secrets: Any,
    provider_registry: Any,
    timeout_seconds: float = 45.0,
) -> OfficialSearchClient:
    """按预设元数据构造官方搜索客户端；无确认通道时抛错。"""
    from providers.presets_loader import ProviderPreset

    if provider_config is None:
        raise OfficialSearchError("provider_missing", f"provider 不存在：{provider_id}")
    preset_id = str(getattr(provider_config, "preset", "") or "").lower()
    presets = getattr(provider_registry, "presets", {}) or {}
    preset = presets.get(preset_id) if preset_id else None
    if not isinstance(preset, ProviderPreset):
        raise OfficialSearchError(
            "preset_missing",
            "官方搜索只支持带内置预设的 provider；自定义 provider 未登记搜索通道",
        )
    preset_channel = str(getattr(preset, "search_channel", "") or "")
    preset_payload = dict(getattr(preset, "search_payload", {}) or {})
    if preset_channel:
        channel = preset_channel
        channel_payload = preset_payload
        model_value = model
    else:
        model_info = next(
            (item for item in preset.models if item.id == model), None
        )
        if model_info is None:
            raise OfficialSearchError(
                "model_missing", f"预设 {preset_id} 没有模型 {model}"
            )
        if (
            not model_info.search_channel
            or "web_search" not in model_info.capabilities
        ):
            raise OfficialSearchError(
                "search_unsupported",
                f"{preset_id}/{model} 没有已确认的官方搜索 API",
            )
        channel = model_info.search_channel
        channel_payload = dict(model_info.search_payload)
        model_value = model
    key_id = str(getattr(provider_config, "api_key_id", "") or "")
    api_key = ""
    if key_id:
        try:
            api_key = str(secrets.get(key_id) or "")
        except Exception as exc:  # noqa: BLE001
            raise OfficialSearchError(
                "key_unreadable", f"读取 provider 密钥失败：{exc}"
            ) from exc
    return OfficialSearchClient(
        provider_id=provider_id,
        base_url=str(
            getattr(provider_config, "base_url", "") or preset.base_url
        ),
        api_key=api_key,
        model=model_value,
        channel=channel,
        payload=channel_payload,
        extra_headers=dict(getattr(provider_config, "extra_headers", {}) or {}),
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "OfficialSearchClient",
    "OfficialSearchError",
    "build_official_search_client",
]
