"""联网搜索：免费引擎、官方回退顺序与官方通道适配。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from features.web_search.engines import (
    BaiduEngine,
    BingRssEngine,
    ProbeResult,
    SearchEngineError,
    SearchItem,
    build_free_engine,
)
from features.web_search.official import (
    OfficialSearchClient,
    OfficialSearchError,
    build_official_search_client,
)
from features.web_search.search_service import WebSearchService
from providers.presets_loader import ModelInfo, ProviderPreset


class FakeFreeEngine:
    name = "fake-free"

    def __init__(
        self,
        *,
        probe_ok: bool = True,
        items: list[SearchItem] | None = None,
        search_error: bool = False,
    ) -> None:
        self.probe_ok = probe_ok
        self.items = items or []
        self.search_error = search_error
        self.probe_calls = 0
        self.search_calls = 0

    async def probe(self, *, timeout_seconds: float | None = None) -> ProbeResult:
        self.probe_calls += 1
        return ProbeResult(
            ok=self.probe_ok,
            engine=self.name,
            latency_ms=1,
            error="" if self.probe_ok else "offline",
            error_code="" if self.probe_ok else "network_error",
        )

    async def search(self, query: str, max_results: int) -> list[SearchItem]:
        self.search_calls += 1
        if self.search_error:
            raise SearchEngineError("network_error", "free offline")
        return self.items


class FakeOfficialClient:
    def __init__(self, result: str = "official-answer", error: bool = False) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def search(
        self, query: str, *, max_results: int, timeout_seconds: float
    ) -> str:
        self.calls += 1
        if self.error:
            raise RuntimeError("official offline")
        return self.result

    def status(self) -> dict:
        return {"configured": True, "channel": "fake"}


def _service(*, free: FakeFreeEngine, official: FakeOfficialClient | None = None,
             free_enabled: bool = True, official_enabled: bool | None = None):
    return WebSearchService(
        free_engine=free,
        official_client=official,
        free_enabled=free_enabled,
        official_enabled=(
            official is not None if official_enabled is None else official_enabled
        ),
        max_results=3,
    )


@pytest.mark.asyncio
async def test_free_preferred_when_all_healthy():
    free = FakeFreeEngine(
        items=[SearchItem("标题", "https://example.com", "摘要")]
    )
    official = FakeOfficialClient()
    service = _service(free=free, official=official)
    result = await service.search("问题")
    assert "标题" in result and "https://example.com" in result
    assert official.calls == 0


@pytest.mark.asyncio
async def test_official_only_when_free_probe_fails():
    free = FakeFreeEngine(probe_ok=False)
    official = FakeOfficialClient()
    service = _service(free=free, official=official)
    result = await service.search("问题")
    assert result.startswith("（免费搜索测速失败")
    assert "official-answer" in result
    assert official.calls == 1
    assert free.search_calls == 0


@pytest.mark.asyncio
async def test_official_fallback_when_free_request_fails_after_probe():
    free = FakeFreeEngine(search_error=True)
    official = FakeOfficialClient()
    service = _service(free=free, official=official)
    result = await service.search("问题")
    assert result.startswith("（免费搜索请求失败")
    assert "official-answer" in result
    assert official.calls == 1


@pytest.mark.asyncio
async def test_only_free_never_calls_official():
    free = FakeFreeEngine(search_error=True)
    official = FakeOfficialClient()
    service = _service(free=free, official=official, official_enabled=False)
    result = await service.search("问题")
    assert result.startswith("（免费搜索失败")
    assert official.calls == 0


@pytest.mark.asyncio
async def test_only_official_never_probes_free():
    free = FakeFreeEngine()
    official = FakeOfficialClient()
    service = _service(free=free, official=official, free_enabled=False)
    result = await service.search("问题")
    assert result == "official-answer"
    assert free.probe_calls == 0
    assert free.search_calls == 0


@pytest.mark.asyncio
async def test_official_only_does_not_build_invalid_free_engine():
    official = FakeOfficialClient()
    service = WebSearchService(
        free_enabled=False,
        free_provider="searxng",
        searxng_base_url="",
        official_enabled=True,
        official_client=official,
    )
    assert await service.search("问题") == "official-answer"
    assert official.calls == 1


@pytest.mark.asyncio
async def test_no_channel_returns_clear_error():
    service = WebSearchService(free_enabled=False, official_enabled=False)
    assert "未启用任何搜索通道" in await service.search("问题")


@pytest.mark.asyncio
async def test_probe_free_is_cached_and_forceable():
    free = FakeFreeEngine()
    service = _service(free=free)
    first = await service.probe_free()
    second = await service.probe_free()
    assert first == second
    assert free.probe_calls == 1
    await service.probe_free(force=True)
    assert free.probe_calls == 2


def test_bing_rss_parsing_filters_ads_and_html():
    body = """<?xml version="1.0" encoding="utf-8" ?>
    <rss><channel>
      <item><title>正常结果</title><link>https://example.com/a</link>
        <description>&lt;b&gt;摘要&lt;/b&gt; 内容</description></item>
      <item><title>广告</title><link>https://www.bing.com/aclick?x=1</link>
        <description>ad</description></item>
    </channel></rss>""".encode()
    engine = BingRssEngine()
    items = engine._parse(body, 5)
    assert len(items) == 1
    assert items[0].title == "正常结果"
    assert items[0].snippet == "摘要 内容"


def test_baidu_parsing_filters_ads_cards_and_extracts_snippet():
    body = """
    <div  class="c-result result" tpl='www_index'
      data-log='{"mu":"https://example.com/a","logCommonInfo":{"exposureInfo":{"con_type":"official_web"}}}'>
      <div class="c-result-content"><h3 class="cosc-title">
        <span><!--s-text-->正常 <em>结果</em><!--/s-text--></span></h3>
        <div data-sanssr-cmpt="card/www-summary">
          <!--s-data:{"summaryData":{"generalLines":[{"data":[{"text":"摘要 <em>内容</em>"}]}]}}-->
        </div>
      </div>
    </div>
    <div  class="c-result result" data-log='{"mu":"https://ads.example.com/x"}'>
      <span class="ec-tuiguang">广告</span><h3>广告结果</h3>
    </div>
    <div  class="c-result result"
      data-log='{"mu":"http://34689.recommend_list.baidu.com/x"}'>
      <h3>推荐卡片</h3>
    </div>
    <div  class="c-result result" data-log='{"mu":"https://example.com/no-title"}'>
      <p>无标题</p>
    </div>
    """
    items = BaiduEngine()._parse(body, 5)
    assert len(items) == 1
    assert items[0].title == "正常 结果"
    assert items[0].snippet == "摘要 内容"
    assert items[0].url == "https://example.com/a"


@pytest.mark.asyncio
async def test_baidu_search_rejects_security_verification(monkeypatch):
    engine = BaiduEngine()

    async def fake_fetch(query: str, max_results: int) -> str:
        return "<html><title>百度安全验证</title></html>"

    monkeypatch.setattr(engine, "_fetch", fake_fetch)
    with pytest.raises(SearchEngineError) as exc:
        await engine.search("测试", 5)
    assert exc.value.code == "blocked"


def test_build_free_engine_supports_baidu():
    assert isinstance(build_free_engine("baidu"), BaiduEngine)


@pytest.mark.asyncio
async def test_deepseek_tools_search_parses_results(monkeypatch):
    client = OfficialSearchClient(
        provider_id="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key="k",
        model="",
        channel="deepseek_tools_search",
        payload={"request": {"need_content": True}},
    )

    async def fake_post(url, payload, *, timeout, headers):
        assert url.endswith("/tools/search")
        assert payload["query"] == "DeepSeek"
        assert payload["need_content"] is True
        return {
            "results": [
                {
                    "title": "结果一",
                    "url": "https://example.com/1",
                    "snippet": "短摘要",
                    "summary": "长摘要一",
                    "content": "正文",
                    "site_name": "示例站",
                    "date": "2026-10-20T10:00:00+00:00",
                    "rerank_score": 1.8,
                    "authority": 3,
                }
            ]
        }

    monkeypatch.setattr(client, "_post", fake_post)
    result = await client.search("DeepSeek", max_results=5, timeout_seconds=10)
    assert "结果一" in result and "长摘要一" in result
    assert "短摘要" not in result
    assert "示例站" in result and "2026-10-20" in result
    assert "相关度 1.8" in result and "权威度 3" in result
    assert "https://example.com/1" in result


@pytest.mark.asyncio
async def test_deepseek_tools_search_clips_query_and_fills_ori_question(monkeypatch):
    client = OfficialSearchClient(
        provider_id="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key="k",
        model="",
        channel="deepseek_tools_search",
        payload={
            "query_max_chars": 128,
            "request": {"ori_question": "{query}"},
        },
    )
    long_query = "热梗" * 100

    async def fake_post(url, payload, *, timeout, headers):
        assert len(payload["query"]) == 128
        assert payload["ori_question"] == payload["query"]
        return {"results": []}

    monkeypatch.setattr(client, "_post", fake_post)
    result = await client.search(long_query, max_results=5, timeout_seconds=10)
    assert "无相关结果" in result


@pytest.mark.asyncio
async def test_mimo_chat_search_parses_annotations(monkeypatch):
    client = OfficialSearchClient(
        provider_id="mimo",
        base_url="https://api.xiaomimimo.com/v1",
        api_key="k",
        model="mimo-v2.6-pro",
        channel="mimo_chat_web_search",
        payload={"tools": [{"type": "web_search"}], "tool_choice": "auto"},
    )

    async def fake_post(url, payload, *, timeout, headers):
        assert url.endswith("/chat/completions")
        assert payload["tools"][0]["type"] == "web_search"
        return {
            "choices": [{
                "message": {
                    "content": "回答",
                    "annotations": [{
                        "type": "url_citation",
                        "title": "来源",
                        "url": "https://example.com/s",
                    }],
                }
            }]
        }

    monkeypatch.setattr(client, "_post", fake_post)
    result = await client.search("天气", max_results=5, timeout_seconds=10)
    assert "回答" in result
    assert "来源" in result and "https://example.com/s" in result


@pytest.mark.asyncio
async def test_official_search_records_v2_usage(monkeypatch):
    recorded = []

    async def recorder(usage, metadata):
        recorded.append((usage, metadata))

    client = OfficialSearchClient(
        provider_id="mimo",
        base_url="https://api.xiaomimimo.com/v1",
        api_key="k",
        model="mimo-v2.6-pro",
        channel="mimo_chat_web_search",
        payload={},
        usage_recorder=recorder,
    )

    async def fake_post(url, payload, *, timeout, headers):
        return {
            "choices": [{"message": {"content": "回答"}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
        }

    monkeypatch.setattr(client, "_post", fake_post)
    await client.search("天气", max_results=5, timeout_seconds=10)

    assert len(recorded) == 1
    usage, metadata = recorded[0]
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 30
    assert usage.cached_tokens == 20
    assert metadata["operation"] == "web_search_official"
    assert metadata["provider"] == "mimo"
    assert metadata["model"] == "mimo-v2.6-pro"


def test_extract_usage_anthropic_adds_cache_to_prompt():
    from features.web_search.official import _extract_usage

    usage = _extract_usage(
        {
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 180,
            }
        },
        "anthropic",
    )
    assert usage is not None
    assert usage.prompt_tokens == 170
    assert usage.cached_tokens == 50
    assert usage.cache_creation_tokens == 20
    assert usage.completion_tokens == 10


def test_extract_usage_gemini_maps_thought_tokens():
    from features.web_search.official import _extract_usage

    usage = _extract_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 90,
                "candidatesTokenCount": 15,
                "thoughtsTokenCount": 5,
                "cachedContentTokenCount": 30,
                "totalTokenCount": 110,
            }
        },
        "gemini",
    )
    assert usage is not None
    assert usage.prompt_tokens == 90
    assert usage.completion_tokens == 20
    assert usage.reasoning_tokens == 5
    assert usage.cached_tokens == 30
    assert usage.total_tokens == 110


@pytest.mark.asyncio
async def test_deepseek_fetch_parses_title_and_markdown(monkeypatch):
    client = OfficialSearchClient(
        provider_id="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key="k",
        model="",
        channel="deepseek_tools_search",
        payload={"fetch_path": "/tools/fetch"},
    )

    async def fake_post(url, payload, *, timeout, headers):
        assert url.endswith("/v1/tools/fetch")
        assert payload == {"url": "https://example.com/page"}
        return {"title": "示例页面", "content": "# 标题\n\n正文内容"}

    monkeypatch.setattr(client, "_post", fake_post)
    result = await client.fetch("https://example.com/page")
    assert result["title"] == "示例页面"
    assert "正文内容" in result["content"]
    assert result["url"] == "https://example.com/page"
    assert client.supports_fetch() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://192.168.1.2/x",
        "https://user:pass@example.com/x",
        "https://example.com/a b",
    ],
)
async def test_fetch_rejects_unsafe_urls(url):
    client = OfficialSearchClient(
        provider_id="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key="k",
        model="",
        channel="deepseek_tools_search",
        payload={"fetch_path": "/tools/fetch"},
    )
    with pytest.raises(OfficialSearchError):
        await client.fetch(url)


def _fake_registry(preset: ProviderPreset):
    return SimpleNamespace(presets={preset.id: preset})


class _Secrets:
    def get(self, key_id: str):
        return "secret-key"


def _provider():
    return SimpleNamespace(
        preset="demo",
        base_url="https://api.example.com/v1",
        api_key_id="demo_key",
        extra_headers={},
    )


def test_build_provider_level_search_client():
    preset = ProviderPreset(
        id="demo",
        display_name="Demo",
        protocol="openai_compat",
        base_url="https://api.example.com/v1",
        search_channel="deepseek_tools_search",
        search_payload={"request": {"need_content": True}},
    )
    client = build_official_search_client(
        provider_id="demo",
        provider_config=_provider(),
        model="",
        secrets=_Secrets(),
        provider_registry=_fake_registry(preset),
    )
    assert client.channel == "deepseek_tools_search"


def test_build_model_level_search_client_and_reject_unmarked_model():
    preset = ProviderPreset(
        id="demo",
        display_name="Demo",
        protocol="openai_compat",
        base_url="https://api.example.com/v1",
        models=[
            ModelInfo(
                id="search-model",
                capabilities=["chat", "web_search"],
                search_channel="mimo_chat_web_search",
                search_payload={"tools": [{"type": "web_search"}]},
            ),
            ModelInfo(id="plain-model", capabilities=["chat"]),
        ],
    )
    client = build_official_search_client(
        provider_id="demo",
        provider_config=_provider(),
        model="search-model",
        secrets=_Secrets(),
        provider_registry=_fake_registry(preset),
    )
    assert client.channel == "mimo_chat_web_search"
    with pytest.raises(OfficialSearchError) as exc:
        build_official_search_client(
            provider_id="demo",
            provider_config=_provider(),
            model="plain-model",
            secrets=_Secrets(),
            provider_registry=_fake_registry(preset),
        )
    assert exc.value.code == "search_unsupported"
