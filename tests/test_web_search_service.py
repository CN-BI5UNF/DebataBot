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
                    "snippet": "摘要一",
                    "content": "正文",
                }
            ]
        }

    monkeypatch.setattr(client, "_post", fake_post)
    result = await client.search("DeepSeek", max_results=5, timeout_seconds=10)
    assert "结果一" in result and "摘要一" in result
    assert "https://example.com/1" in result


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
