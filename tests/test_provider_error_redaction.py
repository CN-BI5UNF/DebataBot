"""Provider SDK 异常统一脱敏测试（不调用真实 API）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from anthropic import (
    APIError as AnthropicAPIError,
)
from anthropic import (
    APITimeoutError as AnthropicAPITimeoutError,
)
from anthropic import (
    AuthenticationError as AnthropicAuthenticationError,
)
from anthropic import (
    RateLimitError as AnthropicRateLimitError,
)
from openai import (
    APIError as OpenAIAPIError,
)
from openai import (
    APITimeoutError as OpenAIAPITimeoutError,
)
from openai import (
    AuthenticationError as OpenAIAuthenticationError,
)
from openai import (
    RateLimitError as OpenAIRateLimitError,
)

from providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    safe_provider_error_text,
)
from providers.protocols.anthropic_proto import AnthropicProvider
from providers.protocols.openai_compat import OpenAICompatProvider

SENTINEL = "SYNTHETIC_SIGNED_TOKEN_DO_NOT_LOG"
SIGNED_URL = (
    "https://user:pass@images.example.invalid/a.png?token=" + SENTINEL
)
IMAGE_DATA = "data:image/png;base64," + ("A" * 512)


def _message() -> str:
    return (
        "request failed at "
        + SIGNED_URL
        + " header Authorization: Bearer "
        + SENTINEL
        + " payload "
        + IMAGE_DATA
    )


def _openai_errors() -> list[tuple[type[BaseException], BaseException, type[BaseException]]]:
    request = httpx2.Request("POST", "https://api.example.invalid/v1/chat")
    response_auth = httpx2.Response(401, request=request)
    response_rate = httpx2.Response(429, request=request)
    return [
        (
            OpenAIAuthenticationError,
            OpenAIAuthenticationError(
                _message(), response=response_auth, body=None
            ),
            ProviderAuthError,
        ),
        (
            OpenAIRateLimitError,
            OpenAIRateLimitError(
                _message(), response=response_rate, body=None
            ),
            ProviderRateLimitError,
        ),
        (
            OpenAIAPITimeoutError,
            OpenAIAPITimeoutError(request),
            ProviderTimeoutError,
        ),
        (
            asyncio.TimeoutError,
            asyncio.TimeoutError(_message()),
            ProviderTimeoutError,
        ),
        (
            OpenAIAPIError,
            OpenAIAPIError(_message(), request, body=None),
            ProviderError,
        ),
        (
            TypeError,
            TypeError(_message()),
            ProviderError,
        ),
    ]


def _anthropic_errors() -> list[tuple[type[BaseException], BaseException, type[BaseException]]]:
    request = httpx.Request("POST", "https://api.example.invalid/v1/messages")
    response_auth = httpx.Response(401, request=request)
    response_rate = httpx.Response(429, request=request)
    return [
        (
            AnthropicAuthenticationError,
            AnthropicAuthenticationError(
                _message(), response=response_auth, body=None
            ),
            ProviderAuthError,
        ),
        (
            AnthropicRateLimitError,
            AnthropicRateLimitError(
                _message(), response=response_rate, body=None
            ),
            ProviderRateLimitError,
        ),
        (
            AnthropicAPITimeoutError,
            AnthropicAPITimeoutError(request),
            ProviderTimeoutError,
        ),
        (
            AnthropicAPIError,
            AnthropicAPIError(_message(), request, body=None),
            ProviderError,
        ),
        (
            TypeError,
            TypeError(_message()),
            ProviderError,
        ),
    ]


def _assert_safe(exc: BaseException, expected_type: type[BaseException]) -> None:
    assert type(exc) is expected_type
    text = str(exc)
    assert SENTINEL not in text
    assert "user:pass" not in text
    assert "data:image" not in text
    assert "Bearer " + SENTINEL not in text
    assert exc.__cause__ is None
    assert getattr(exc, "__suppress_context__", False) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_openai_all_sdk_error_categories_are_redacted(stream):
    for _error_type, error, expected in _openai_errors():
        class BoomCompletions:
            def __init__(self, error):
                self.error = error

            async def create(self, **kwargs):
                raise self.error

        provider = OpenAICompatProvider(
            "fake_openai",
            base_url="https://example.invalid/v1",
            api_key="test",
        )
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=BoomCompletions(error))
        )
        with pytest.raises(BaseException) as excinfo:
            await provider.chat_completion(
                [{"role": "user", "content": "hi"}],
                model="fake",
                stream=stream,
            )
        _assert_safe(excinfo.value, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_anthropic_all_sdk_error_categories_are_redacted(stream):
    for _error_type, error, expected in _anthropic_errors():
        class BoomMessages:
            def __init__(self, error):
                self.error = error

            def create(self, **kwargs):
                raise self.error

            def stream(self, **kwargs):
                raise self.error

        provider = AnthropicProvider(
            "fake_anthropic",
            base_url="https://example.invalid",
            api_key="test",
        )
        provider._client = SimpleNamespace(messages=BoomMessages(error))
        with pytest.raises(BaseException) as excinfo:
            await provider.chat_completion(
                [{"role": "user", "content": "hi"}],
                model="fake",
                stream=stream,
            )
        _assert_safe(excinfo.value, expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "Authorization: Bearer " + SENTINEL,
        "api_key=" + SENTINEL,
        "signature=" + SENTINEL,
        "https://[::1",
        "https://user:pass@host.invalid/a?access_token=" + SENTINEL,
        "prefix " + IMAGE_DATA + " suffix",
        "x" * 5000,
    ],
)
def test_safe_provider_error_text_handles_malformed_and_sensitive_inputs(text):
    safe = safe_provider_error_text(text)
    assert SENTINEL not in safe
    assert "data:image" not in safe
    assert "user:pass" not in safe
    assert len(safe) <= 300
