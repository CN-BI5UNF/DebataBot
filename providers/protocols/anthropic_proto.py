"""Anthropic Claude 协议。

与 OpenAI 协议的主要差异：
    - system 不作为 messages 的一员，而是顶层 system 参数
    - tool_use / tool_result 在 content 中作为块（不是顶层 tool_calls 字段）
    - extended thinking 通过 thinking={"type":"enabled","budget_tokens":N}
    - reasoning_content 在 content 中作为 'thinking' 类型块返回

本模块负责双向翻译：
    OpenAI 风格 messages ↔ Anthropic 风格 messages
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Any

import httpx
from anthropic import (
    APIError,
    APITimeoutError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)

from providers.base import (
    CompletionResult,
    IProvider,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnsupportedError,
    ReasoningConfig,
    ToolCall,
    Usage,
    headers_with_neutral_user_agent,
    safe_provider_error_text,
)
from utils.image_contracts import MAX_IMAGE_BYTES
from utils.proxy_env import sanitize_no_proxy_env

logger = logging.getLogger(__name__)


_BUDGET_MAP = {
    "low": 2000,
    "medium": 6000,
    "high": 16000,
}

_ANTHROPIC_IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_MAX_IMAGE_DATA_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 16


class AnthropicProvider(IProvider):
    """Anthropic Claude 适配。"""

    def __init__(
        self,
        name: str,
        *,
        base_url: str = "https://api.anthropic.com",
        api_key: str | None,
        timeout: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name)
        self.base_url = base_url
        self.api_key = api_key or ""
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

        # 同 openai_compat：AsyncAnthropic 构造时展开 NO_PROXY，[::1] 会抛 InvalidURL。
        sanitize_no_proxy_env()

        self._client = AsyncAnthropic(
            api_key=self.api_key,
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            default_headers=headers_with_neutral_user_agent(self.extra_headers),
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        top_p: float = 1.0,
        max_tokens: int = 16384,
        reasoning: ReasoningConfig | None = None,
        stream: bool = True,
        timeout: float = 120.0,
        first_token_timeout: float | None = 30.0,
        extra: dict[str, Any] | None = None,
    ) -> CompletionResult:
        if extra and any(key in extra for key in ("messages", "system", "tools")):
            raise ProviderUnsupportedError(
                f"{self.name}: extra 不能覆盖 messages/system/tools"
            )
        system_text, anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools) if tools else None

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if system_text:
            kwargs["system"] = system_text
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        if reasoning is not None and reasoning.enabled:
            budget = reasoning.max_tokens
            if budget is None and reasoning.budget:
                budget = _BUDGET_MAP.get(reasoning.budget, 4000)
            if budget is None:
                budget = 4000
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

        if extra:
            kwargs.update(extra)

        try:
            if stream:
                return await self._stream_call(kwargs, first_token_timeout)
            return await self._nonstream_call(kwargs)
        except AuthenticationError as e:
            raise ProviderAuthError(f"{self.name}: 认证失败: {e}") from e
        except RateLimitError as e:
            raise ProviderRateLimitError(f"{self.name}: 限流: {e}") from e
        except (APITimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as e:
            raise ProviderTimeoutError(f"{self.name}: 超时: {e}") from e
        except APIError as e:
            raise ProviderError(
                f"{self.name}: API 错误: {safe_provider_error_text(e)}"
            ) from e
        except TypeError as e:
            # 请求还没发出去就炸的编程错误（比如给 SDK 传了不存在的参数）。
            # 不兜住的话它既不是 APIError 也不是 TimeoutError，会一路冒到 AgentRunner
            # 的兜底分支，被记成含糊的「未知错误」，排查时得翻整个 traceback 才知道
            # 其实是本地的调用签名写错了。
            raise ProviderError(f"{self.name}: 内部错误（请求未发出）: {e}") from e

    # ============================================================
    # 协议翻译
    # ============================================================

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """OpenAI 风格 messages → (system_text, anthropic_messages)。

        合并多个 system 消息为单一 system 文本；
        合并连续相同 role 的消息（Anthropic 要求 user/assistant 交替）。
        """
        system_parts: list[str] = []
        result: list[dict[str, Any]] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content", "") or ""

            if role == "system":
                if not isinstance(content, str):
                    raise ProviderUnsupportedError(
                        "Anthropic system content 必须是字符串"
                    )
                if content:
                    system_parts.append(content)
                continue

            if role == "tool":
                # OpenAI 风格 tool 结果 → Anthropic tool_result 块
                tool_call_id = m.get("tool_call_id", "")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise ProviderUnsupportedError(
                        "Anthropic tool 消息缺少 tool_call_id"
                    )
                tool_content = _tool_result_content(content)
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": tool_content,
                    }
                ]
                # tool_result 必须放在 user 角色下
                result.append({"role": "user", "content": blocks})
                continue

            if role == "user":
                result.append(
                    {
                        "role": "user",
                        "content": _convert_user_content(content),
                    }
                )
                continue

            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                blocks.extend(
                    _normalize_reasoning_blocks(m.get("reasoning_blocks"))
                )
                if not blocks and "reasoning_content" in m:
                    blocks.append(
                        {
                            "type": "thinking",
                            "thinking": str(m.get("reasoning_content") or ""),
                        }
                    )
                if content:
                    blocks.extend(_convert_assistant_content(content))
                # 处理 tool_calls
                for tc in m.get("tool_calls", []) or []:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    arguments_str = func.get("arguments", "") or "{}"
                    import json
                    try:
                        arguments = json.loads(arguments_str) if arguments_str else {}
                    except json.JSONDecodeError:
                        arguments = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": name,
                            "input": arguments,
                        }
                    )
                result.append({"role": "assistant", "content": blocks or content})
                continue

        # Anthropic 要求 messages 不为空，且首条不能是 assistant
        if result and result[0]["role"] == "assistant":
            result.insert(0, {"role": "user", "content": "[continue]"})

        return "\n\n".join(system_parts), result

    @staticmethod
    def _convert_tools(openai_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI 风格 tool → Anthropic 风格。

        OpenAI:    {"type":"function","function":{"name","description","parameters"}}
        Anthropic: {"name","description","input_schema"}
        """
        out = []
        for t in openai_tools:
            func = t.get("function", t)
            out.append(
                {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object"}),
                }
            )
        return out

    # ============================================================
    # 流 / 非流调用
    # ============================================================

    async def _nonstream_call(self, kwargs: dict[str, Any]) -> CompletionResult:
        response = await self._client.messages.create(**kwargs)
        return self._build_result_from_response(response, kwargs.get("model", ""))

    async def _stream_call(
        self, kwargs: dict[str, Any], first_token_timeout: float | None
    ) -> CompletionResult:
        # 走的是 client.messages.stream()，流式已经由方法本身决定，
        # 不能再往 kwargs 里塞 stream —— AsyncMessages.stream() 没有这个形参，
        # 塞了会直接 TypeError: unexpected keyword argument 'stream'，
        # 导致 anthropic 协议的每一次调用都在发请求前就崩掉。
        events: list[Any] = []

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                iterator = stream.__aiter__()
                try:
                    if first_token_timeout is not None:
                        first_event = await asyncio.wait_for(
                            anext(iterator), timeout=first_token_timeout
                        )
                    else:
                        first_event = await anext(iterator)
                except StopAsyncIteration:
                    first_event = None

                if first_event is not None:
                    events.append(first_event)
                    async for event in iterator:
                        events.append(event)
        except asyncio.TimeoutError:
            raise ProviderTimeoutError(
                f"{self.name}: 首 token 超时 ({first_token_timeout}s)"
            ) from None

        return self._assemble_from_events(events, kwargs.get("model", ""))

    # ============================================================
    # 响应解析
    # ============================================================

    def _build_result_from_response(self, response: Any, model: str) -> CompletionResult:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_blocks: list[dict[str, Any]] = []
        tool_calls: list[ToolCall] = []
        import json

        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "thinking":
                thinking = getattr(block, "thinking", "") or ""
                reasoning_parts.append(thinking)
                out_block = {"type": "thinking", "thinking": thinking}
                signature = getattr(block, "signature", None)
                if signature:
                    out_block["signature"] = signature
                reasoning_blocks.append(out_block)
            elif btype == "redacted_thinking":
                data = getattr(block, "data", "") or ""
                out_block = {"type": "redacted_thinking", "data": data}
                reasoning_blocks.append(out_block)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", "") or "",
                        name=getattr(block, "name", "") or "",
                        arguments=json.dumps(
                            getattr(block, "input", {}) or {}, ensure_ascii=False
                        ),
                    )
                )

        usage_obj = getattr(response, "usage", None)
        usage = Usage()
        if usage_obj is not None:
            input_tokens = int(getattr(usage_obj, "input_tokens", 0) or 0)
            cached = int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0)
            creation = int(
                getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
            )
            usage.prompt_tokens = input_tokens + cached + creation
            usage.completion_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)
            usage.cached_tokens = cached
            usage.cache_creation_tokens = creation
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

        return CompletionResult(
            content="".join(text_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts),
            reasoning_blocks=reasoning_blocks,
            finish_reason=getattr(response, "stop_reason", "") or "",
            usage=usage,
            model=getattr(response, "model", "") or model,
            raw=response,
        )

    def _assemble_from_events(self, events: list[Any], model: str) -> CompletionResult:
        """从流式事件重建结果。简化处理：等流跑完后取 final_message。"""
        # Anthropic 的 stream 在 __aexit__ 时把 final message 推给 get_final_message()
        # 这里我们没有保留 stream context，所以从 events 中重建
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_blocks_by_index: dict[int, dict[str, Any]] = {}
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        input_tokens = 0
        completion_tokens = 0
        cached_tokens = 0
        cache_creation_tokens = 0

        current_tool_index = 0

        for ev in events:
            ev_type = getattr(ev, "type", "")
            if ev_type == "content_block_start":
                block = getattr(ev, "content_block", None)
                if block is None:
                    continue
                btype = getattr(block, "type", "")
                if btype == "thinking":
                    reasoning_blocks_by_index[current_tool_index] = {
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", "") or "",
                    }
                elif btype == "redacted_thinking":
                    reasoning_blocks_by_index[current_tool_index] = {
                        "type": "redacted_thinking",
                        "data": getattr(block, "data", "") or "",
                    }
                elif btype == "tool_use":
                    tool_calls_by_index[current_tool_index] = {
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "arguments": "",
                    }
            elif ev_type == "content_block_delta":
                delta = getattr(ev, "delta", None)
                if delta is None:
                    continue
                dtype = getattr(delta, "type", "")
                if dtype == "text_delta":
                    text_parts.append(getattr(delta, "text", "") or "")
                elif dtype == "thinking_delta":
                    thinking = getattr(delta, "thinking", "") or ""
                    reasoning_parts.append(thinking)
                    block = reasoning_blocks_by_index.setdefault(
                        current_tool_index,
                        {"type": "thinking", "thinking": ""},
                    )
                    block["thinking"] = str(block.get("thinking", "")) + thinking
                elif dtype == "input_json_delta":
                    if current_tool_index in tool_calls_by_index:
                        tool_calls_by_index[current_tool_index]["arguments"] += (
                            getattr(delta, "partial_json", "") or ""
                        )
                elif dtype == "signature_delta":
                    block = reasoning_blocks_by_index.setdefault(
                        current_tool_index,
                        {"type": "thinking", "thinking": ""},
                    )
                    signature = getattr(delta, "signature", "") or ""
                    if signature:
                        block["signature"] = signature
            elif ev_type == "content_block_stop":
                current_tool_index += 1
            elif ev_type == "message_delta":
                delta = getattr(ev, "delta", None)
                if delta:
                    sr = getattr(delta, "stop_reason", None)
                    if sr:
                        finish_reason = sr
                usage = getattr(ev, "usage", None)
                if usage:
                    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                    cached_tokens = _usage_int(
                        usage, "cache_read_input_tokens", cached_tokens
                    )
                    cache_creation_tokens = _usage_int(
                        usage,
                        "cache_creation_input_tokens",
                        cache_creation_tokens,
                    )
            elif ev_type == "message_start":
                msg = getattr(ev, "message", None)
                if msg:
                    usage = getattr(msg, "usage", None)
                    if usage:
                        input_tokens = _usage_int(usage, "input_tokens", input_tokens)
                        cached_tokens = _usage_int(
                            usage, "cache_read_input_tokens", cached_tokens
                        )
                        cache_creation_tokens = _usage_int(
                            usage,
                            "cache_creation_input_tokens",
                            cache_creation_tokens,
                        )

        return CompletionResult(
            content="".join(text_parts),
            tool_calls=[
                ToolCall(
                    id=v["id"],
                    name=v["name"],
                    arguments=v["arguments"] or "{}",
                )
                for _, v in sorted(tool_calls_by_index.items())
                if v["name"]
            ],
            reasoning_content="".join(reasoning_parts),
            reasoning_blocks=[
                v
                for _, v in sorted(reasoning_blocks_by_index.items())
                if v.get("type") == "redacted_thinking"
                or v.get("thinking")
                or "signature" in v
            ],
            finish_reason=finish_reason,
            usage=Usage(
                prompt_tokens=input_tokens + cached_tokens + cache_creation_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens,
                total_tokens=(
                    input_tokens
                    + cached_tokens
                    + cache_creation_tokens
                    + completion_tokens
                ),
            ),
            model=model,
            raw=None,
        )


def _usage_int(usage: Any, name: str, fallback: int) -> int:
    value = getattr(usage, name, None)
    if value is None:
        return fallback
    return int(value or 0)


def _convert_user_content(content: Any) -> str | list[dict[str, Any]]:
    """OpenAI user content → Anthropic text/image 块；未知结构明确报错。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ProviderUnsupportedError(
            "Anthropic user content 必须是字符串或内容块数组"
        )
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise ProviderUnsupportedError("Anthropic user content 包含非对象块")
        block_type = str(block.get("type") or "")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ProviderUnsupportedError("Anthropic text 块缺少文本")
            blocks.append({"type": "text", "text": text})
            continue
        if block_type == "image_url":
            image_url = block.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if not isinstance(url, str):
                raise ProviderUnsupportedError("Anthropic image_url 块缺少 URL")
            if not url.lower().startswith("data:"):
                # 新 native 链路只发送受管 data URL；不把未知 URL 交给 SDK 下载。
                raise ProviderUnsupportedError(
                    "Anthropic 图片输入仅支持受管 data URL"
                )
            media_type, data = _parse_image_data_url(url)
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
            continue
        if block_type == "image":
            source = block.get("source")
            if not isinstance(source, dict):
                raise ProviderUnsupportedError("Anthropic image 块缺少 source")
            source_type = str(source.get("type") or "")
            if source_type != "base64":
                raise ProviderUnsupportedError(
                    "Anthropic image source 仅支持 base64"
                )
            media_type = str(source.get("media_type") or "")
            data = source.get("data")
            data = _validated_image_base64(data, media_type)
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
            continue
        raise ProviderUnsupportedError(
            f"Anthropic user content 不支持块类型: {block_type or 'unknown'}"
        )
    return blocks


def _convert_assistant_content(content: Any) -> list[dict[str, Any]]:
    """助手历史只接受文本块；未知多模态输出不猜测协议支持。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ProviderUnsupportedError(
            "Anthropic assistant content 必须是字符串或文本块数组"
        )
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or str(block.get("type") or "") != "text":
            raise ProviderUnsupportedError(
                "Anthropic assistant content 仅支持 text 块"
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise ProviderUnsupportedError("Anthropic text 块缺少文本")
        if text:
            blocks.append({"type": "text", "text": text})
    return blocks


def _tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict) or str(block.get("type") or "") != "text":
                raise ProviderUnsupportedError(
                    "Anthropic tool_result 仅支持文本内容"
                )
            text = block.get("text")
            if not isinstance(text, str):
                raise ProviderUnsupportedError("Anthropic text 块缺少文本")
            texts.append(text)
        return "\n".join(texts)
    raise ProviderUnsupportedError(
        "Anthropic tool_result content 必须是字符串或文本块数组"
    )


def _parse_image_data_url(url: str) -> tuple[str, str]:
    header, separator, payload = url.partition(",")
    if not separator or not header.lower().startswith("data:"):
        raise ProviderUnsupportedError("图片 data URL 格式无效")
    parts = [part.strip().lower() for part in header[5:].split(";") if part.strip()]
    if not parts or parts[0] not in _ANTHROPIC_IMAGE_MEDIA_TYPES:
        raise ProviderUnsupportedError("图片 data URL MIME 不受支持")
    if "base64" not in parts[1:]:
        raise ProviderUnsupportedError("图片 data URL 必须是 base64")
    return parts[0], _validated_image_base64(payload, parts[0])


def _validated_image_base64(data: Any, media_type: str) -> str:
    if media_type not in _ANTHROPIC_IMAGE_MEDIA_TYPES:
        raise ProviderUnsupportedError("图片 media_type 不受支持")
    if not isinstance(data, str) or not data or not data.isascii():
        raise ProviderUnsupportedError("图片 base64 数据无效")
    if len(data) > _MAX_IMAGE_DATA_CHARS:
        raise ProviderUnsupportedError("图片 base64 数据超过限制")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ProviderUnsupportedError("图片 base64 数据无效") from None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ProviderUnsupportedError("图片字节数超过限制")
    return data


def _normalize_reasoning_blocks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        btype = item.get("type")
        if btype == "thinking":
            block = {
                "type": "thinking",
                "thinking": str(item.get("thinking") or ""),
            }
            signature = item.get("signature")
            if signature:
                block["signature"] = str(signature)
            blocks.append(block)
        elif btype == "redacted_thinking":
            blocks.append(
                {
                    "type": "redacted_thinking",
                    "data": str(item.get("data") or ""),
                }
            )
    return blocks
