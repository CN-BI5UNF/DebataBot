"""模型官方搜索通道。

只实现预设中已确认存在官方搜索 API 的通道；没有 ``search_channel`` 的模型
不会出现在设置里，也不会被猜测调用。官方搜索会产生模型 API 费用。
"""

from __future__ import annotations

import inspect
import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from providers.base import Usage, headers_with_neutral_user_agent

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
        usage_recorder: (
            Callable[[Usage, dict[str, Any]], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self.provider_id = provider_id
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.channel = channel
        self.payload = dict(payload or {})
        self.extra_headers = dict(extra_headers or {})
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.usage_recorder = usage_recorder

    def supports_fetch(self) -> bool:
        return bool(str(self.payload.get("fetch_path") or "").strip())

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
        query_limit = _int_value(
            self.payload.get("query_max_chars") or _MAX_QUERY_CHARS
        )
        query_limit = max(1, min(8192, query_limit))
        query = query.strip()[:query_limit]
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

    async def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, str]:
        """Fetch one page through the provider's official Fetch API.

        Only providers whose preset declares ``fetch_path`` support this
        (currently DeepSeek). The URL is validated locally before sending; the
        actual network fetch happens on the provider side.
        """
        fetch_path = str(self.payload.get("fetch_path") or "").strip()
        if not fetch_path:
            raise OfficialSearchError(
                "fetch_unsupported",
                "当前官方搜索 provider 没有配置 Fetch 通道",
            )
        if not self.api_key:
            raise OfficialSearchError("missing_key", "官方抓取缺少 API Key")
        clean_url = _validate_fetch_url(url)
        timeout = max(5.0, float(timeout_seconds or self.timeout_seconds))
        path = fetch_path if fetch_path.startswith("/") else "/" + fetch_path
        data = await self._post(
            self.base_url + path,
            {"url": clean_url},
            timeout=timeout,
            headers=self._headers(),
        )
        title = str(data.get("title") or "").strip()
        content = str(data.get("content") or "").strip()
        if not content:
            raise OfficialSearchError("fetch_empty", "官方抓取没有返回正文")
        return {"title": title, "url": clean_url, "content": content}

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

    async def _record_usage(self, data: dict[str, Any], protocol: str) -> None:
        """Record official-search model tokens in the same V2 usage stat store."""
        if self.usage_recorder is None:
            return
        usage = _extract_usage(data, protocol)
        if usage is None:
            return
        try:
            outcome = self.usage_recorder(
                usage,
                {
                    "provider": self.provider_id,
                    "model": self.model,
                    "agent": "web_search",
                    "operation": "web_search_official",
                },
            )
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - 统计失败不能拖垮搜索
            logger.debug("记录官方搜索 usage 失败", exc_info=True)

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
        await self._record_usage(data, "openai_responses")
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
        await self._record_usage(data, "anthropic")
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
        await self._record_usage(data, "gemini")
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
        await self._record_usage(data, "openai_chat")
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
            # DeepSeek/Kimi tools-search 的 count 上限是 50。
            body["count"] = max(1, min(50, int(max_results)))
        if isinstance(request, dict):
            body.update(request)
        # 预设可以用 "{query}" 把实际搜索词填进 ori_question 等字段。
        for key, value in list(body.items()):
            if isinstance(value, str) and "{query}" in value:
                body[key] = value.replace("{query}", query)
        data = await self._post(
            url, body, timeout=timeout, headers=self._headers()
        )
        await self._record_usage(data, "generic")
        raw_items = data.get(result_key)
        if not isinstance(raw_items, list):
            raw_items = data.get("results") or data.get("search_results") or []
        items: list[tuple[str, str, str]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            link = str(raw.get("url") or raw.get("link") or "").strip()
            # summary（约 600–800 字）比 snippet（约 100–200 字）更适合给模型读；
            # content 只有 need_content=true 时才有，作为最后的兜底。
            snippet = str(
                raw.get("summary")
                or raw.get("snippet")
                or raw.get("content")
                or ""
            ).strip()
            if not snippet and isinstance(raw.get("chunks"), list):
                snippet = " ".join(
                    str(chunk.get("text") or "")
                    for chunk in raw["chunks"]
                    if isinstance(chunk, dict)
                ).strip()
            meta_parts: list[str] = []
            site = str(raw.get("site_name") or "").strip()
            date = str(raw.get("date") or "").strip()
            if site:
                meta_parts.append(site)
            if date:
                meta_parts.append(date[:10])
            try:
                score = float(raw.get("rerank_score"))
            except (TypeError, ValueError):
                score = 0.0
            if score:
                meta_parts.append(f"相关度 {score:g}")
            try:
                authority = int(raw.get("authority"))
            except (TypeError, ValueError):
                authority = -1
            if authority >= 0:
                meta_parts.append(f"权威度 {authority}")
            if meta_parts:
                snippet = f"[{' · '.join(meta_parts)}] {snippet}"
            snippet = snippet[:1600]
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
        await self._record_usage(data, "generic")
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


def _validate_fetch_url(url: str) -> str:
    """Reject credentials/control chars/private literals before the provider fetch."""
    text = str(url or "").strip()
    if not text or len(text) > 8192:
        raise OfficialSearchError(
            "fetch_invalid_url", "网页地址为空或超过 8192 字符"
        )
    if any(ord(char) < 33 or ord(char) == 127 for char in text) or "\\" in text:
        raise OfficialSearchError("fetch_invalid_url", "网页地址包含无效字符")
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError as exc:
        raise OfficialSearchError("fetch_invalid_url", "网页地址无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "%" in host
    ):
        raise OfficialSearchError(
            "fetch_invalid_url",
            "只支持不含账号密码的 http(s) 网页地址",
        )
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise OfficialSearchError("fetch_invalid_url", "不允许抓取本机或 .local 地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    ):
        raise OfficialSearchError("fetch_invalid_url", "不允许抓取内网或回环地址")
    return text


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _detail_int(usage: dict[str, Any], name: str) -> int:
    for outer in (
        "prompt_tokens_details",
        "input_tokens_details",
        "completion_tokens_details",
        "output_tokens_details",
    ):
        details = usage.get(outer)
        if isinstance(details, dict) and name in details:
            return _int_value(details.get(name))
    return 0


def _extract_usage(data: dict[str, Any], protocol: str) -> Usage | None:
    """Map the official-search response usage into the shared ``Usage`` shape.

    OpenAI-compatible APIs already report cached tokens as a subset of prompt
    tokens. Anthropic reports uncached input separately, so add cache read/write
    back into ``prompt_tokens`` for the project's V2 semantics.
    """
    raw = data.get("usageMetadata") if protocol == "gemini" else data.get("usage")
    if not isinstance(raw, dict):
        raw = data.get("usage")
    if not isinstance(raw, dict):
        return None

    if protocol == "anthropic":
        cached = _int_value(raw.get("cache_read_input_tokens"))
        creation = _int_value(raw.get("cache_creation_input_tokens"))
        prompt = (
            _int_value(raw.get("input_tokens"))
            + cached
            + creation
        )
        completion = _int_value(raw.get("output_tokens"))
        reasoning = _int_value(raw.get("reasoning_tokens"))
        total = _int_value(raw.get("total_tokens")) or (prompt + completion)
        cache_miss = _int_value(raw.get("cache_miss_tokens"))
    elif protocol == "gemini":
        cached = _int_value(raw.get("cachedContentTokenCount"))
        prompt = _int_value(raw.get("promptTokenCount"))
        reasoning = _int_value(raw.get("thoughtsTokenCount"))
        completion = _int_value(raw.get("candidatesTokenCount")) + reasoning
        total = _int_value(raw.get("totalTokenCount")) or (prompt + completion)
        creation = 0
        cache_miss = 0
    else:
        prompt = _int_value(raw.get("prompt_tokens") or raw.get("input_tokens"))
        completion = _int_value(
            raw.get("completion_tokens") or raw.get("output_tokens")
        )
        cached = _detail_int(raw, "cached_tokens")
        reasoning = _detail_int(raw, "reasoning_tokens")
        creation = _int_value(
            raw.get("cache_creation_input_tokens")
            or raw.get("cache_creation_tokens")
        )
        total = _int_value(raw.get("total_tokens")) or (prompt + completion)
        cache_miss = _int_value(raw.get("cache_miss_tokens"))

    if prompt <= 0 and completion <= 0 and total <= 0:
        return None
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=min(reasoning, completion) if completion else reasoning,
        cached_tokens=min(cached, prompt),
        cache_creation_tokens=min(creation, prompt),
        cache_miss_tokens=cache_miss,
        total_tokens=total or (prompt + completion),
    )


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
    usage_recorder: (
        Callable[[Usage, dict[str, Any]], Awaitable[None]] | None
    ) = None,
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
        usage_recorder=usage_recorder,
    )


__all__ = [
    "OfficialSearchClient",
    "OfficialSearchError",
    "build_official_search_client",
]
