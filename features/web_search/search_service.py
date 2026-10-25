"""联网搜索编排：免费搜索优先，失败时才回退到官方搜索通道。

免费后端默认 Bing RSS，也支持百度移动页和自建 SearXNG；
DuckDuckGo 仅保留后端实现，不再作为设置选项。
官方搜索由模型 provider 的原生搜索通道实现，调用可能产生额外 API 费用。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from .engines import (
    BaiduEngine,
    BingRssEngine,
    DuckDuckGoEngine,
    ProbeResult,
    SearchEngineError,
    SearchItem,
    SearxngEngine,
    build_free_engine,
)

logger = logging.getLogger(__name__)


class OfficialSearchClient(Protocol):
    """官方搜索通道的统一接口。"""

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_seconds: float,
    ) -> str: ...

    def status(self) -> dict[str, Any]: ...


class WebSearchService:
    """组合免费搜索与可选的官方搜索通道。"""

    def __init__(
        self,
        *,
        max_results: int = 5,
        timeout_seconds: float = 10.0,
        free_enabled: bool = True,
        free_engine: (
            BaiduEngine
            | BingRssEngine
            | SearxngEngine
            | DuckDuckGoEngine
            | None
        ) = None,
        free_provider: str = "bing_rss",
        searxng_base_url: str = "",
        official_enabled: bool = False,
        official_client: OfficialSearchClient | None = None,
        probe_cache_seconds: float = 60.0,
    ) -> None:
        self.max_results = max(1, int(max_results))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.free_enabled = bool(free_enabled)
        self.free_provider = (free_provider or "bing_rss").strip().lower()
        self.searxng_base_url = (searxng_base_url or "").strip()
        self.free_engine = free_engine
        if self.free_engine is None and self.free_enabled:
            # 只开官方搜索时不应因为免费引擎配置不完整而拖垮整个服务。
            self.free_engine = build_free_engine(
                self.free_provider,
                searxng_base_url=self.searxng_base_url,
                timeout_seconds=self.timeout_seconds,
            )
        self.official_enabled = bool(official_enabled)
        self.official_client = official_client
        self.probe_cache_seconds = max(5.0, float(probe_cache_seconds))
        self._free_probe: tuple[float, ProbeResult] | None = None

    async def search(self, query: str) -> str:
        """按配置执行搜索，返回可直接交给模型的文本。"""
        if not query.strip():
            return "（搜索词为空）"
        if not self.free_enabled and not self.official_enabled:
            return "（未启用任何搜索通道）"

        if self.free_enabled and self.official_enabled:
            # 两者都开：先确认免费通道可用；只有测速失败才走官方 API。
            probe = await self.probe_free()
            if not probe["ok"]:
                return await self._official_search(
                    query,
                    fallback_reason=(
                        "免费搜索测速失败："
                        f"{probe.get('error') or probe.get('error_code')}"
                    ),
                )
            try:
                return await self._free_search(query)
            except SearchEngineError as exc:
                self._free_probe = (
                    time.monotonic(),
                    ProbeResult(
                        ok=False,
                        engine=getattr(
                            self.free_engine, "name", self.free_provider
                        ),
                        latency_ms=int(probe.get("latency_ms") or 0),
                        error=exc.message,
                        error_code=exc.code,
                    ),
                )
                return await self._official_search(
                    query,
                    fallback_reason=f"免费搜索请求失败：{exc.message}",
                )

        if self.free_enabled:
            # 只开免费：即使探针失败也按用户选择执行，不偷偷调用付费通道。
            try:
                return await self._free_search(query)
            except SearchEngineError as exc:
                return f"（免费搜索失败：{exc.message}）"
        return await self._official_search(query)

    async def probe_free(self, *, force: bool = False) -> dict[str, Any]:
        """测试免费搜索通道；结果缓存一小段时间，避免每次搜索都多一次请求。"""
        now = time.monotonic()
        if (
            not force
            and self._free_probe is not None
            and now - self._free_probe[0] < self.probe_cache_seconds
        ):
            return self._free_probe[1].as_dict()
        if not self.free_enabled or self.free_engine is None:
            return ProbeResult(
                ok=False,
                engine=self.free_provider,
                latency_ms=0,
                error="免费搜索未启用",
                error_code="disabled",
            ).as_dict()
        try:
            result = await self.free_engine.probe(
                timeout_seconds=min(self.timeout_seconds, 8.0)
            )
        except Exception as exc:  # noqa: BLE001 - 探针失败必须转成状态
            logger.warning("免费搜索探针失败: %s", exc)
            result = ProbeResult(
                ok=False,
                engine=getattr(self.free_engine, "name", self.free_provider),
                latency_ms=0,
                error=f"{type(exc).__name__}: {exc}",
                error_code="probe_failed",
            )
        self._free_probe = (now, result)
        return result.as_dict()

    def probe_official(self) -> dict[str, Any]:
        """只返回通道配置状态；绝不为测速而调用付费搜索。"""
        if not self.official_enabled or self.official_client is None:
            return {
                "enabled": bool(self.official_enabled),
                "configured": False,
                "message": "未配置官方搜索通道",
            }
        try:
            status = self.official_client.status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取官方搜索状态失败: %s", exc)
            return {
                "enabled": True,
                "configured": False,
                "message": f"官方搜索配置不可用：{exc}",
            }
        return {"enabled": True, **status}

    async def _free_search(self, query: str) -> str:
        if self.free_engine is None:
            raise SearchEngineError("disabled", "免费搜索未启用")
        items = await self.free_engine.search(query, self.max_results)
        if not items:
            return f"无相关结果：{query}"
        return self._format_items(items)

    async def _official_search(
        self,
        query: str,
        *,
        fallback_reason: str = "",
    ) -> str:
        if not self.official_enabled or self.official_client is None:
            return "（官方搜索通道未配置）"
        try:
            result = await self.official_client.search(
                query,
                max_results=self.max_results,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 官方通道错误要回给工具
            logger.warning("官方搜索失败: %s", exc)
            message = str(exc) or type(exc).__name__
            if fallback_reason:
                return (
                    f"（免费搜索不可用：{fallback_reason}；"
                    f"官方搜索也失败：{message}）"
                )
            return f"（官方搜索失败：{message}）"
        prefix = (
            f"（{fallback_reason}；已回退到官方搜索）\n\n"
            if fallback_reason
            else ""
        )
        return prefix + result

    def _format_items(self, items: list[SearchItem]) -> str:
        lines: list[str] = []
        for index, item in enumerate(items, 1):
            lines.append(
                f"{index}. {item.title}\n   {item.snippet}\n   {item.url}"
            )
        return "\n\n".join(lines)


__all__ = ["OfficialSearchClient", "WebSearchService"]
