"""免费网页搜索后端。

默认 Bing RSS：国内通常可直连，RSS 结果不含广告，适合作为免费兜底。
另提供百度移动页解析和自建 SearXNG；DuckDuckGo 仅保留实现，不再暴露为选项。
注意：Microsoft 的 RSS 结果版权声明只允许个人、非商业的 RSS 聚合器展示；
百度和 SearXNG 抓取/自建实例也请自行确认目标站点条款。
商业部署请改用获得授权的搜索 API，或用户自行确认条款。
本模块不绕过任何登录、付费墙或验证码。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

_BING_URL = "https://cn.bing.com/search"
_BAIDU_URL = "https://www.baidu.com/s"
_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 KarenBot"
)
_BAIDU_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 KarenBot"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_MAX_BODY_BYTES = 2 * 1024 * 1024
_BAIDU_MAX_BODY_BYTES = 6 * 1024 * 1024
_AD_HOSTS = {
    "go.microsoft.com",
    "ad.doubleclick.net",
    "googleadservices.com",
    "www.googleadservices.com",
}
_AD_PATH_MARKERS = ("/aclick", "/adclick", "/ads/", "ad_provider")
_BAIDU_RESULT_RE = re.compile(
    r"<div\s+class=\"c-result result\"([^>]*)>", re.IGNORECASE
)
_BAIDU_AD_MARKERS = ("ec-tuiguang", "ecfc-tuiguang", "c-result-ad")
_BAIDU_CARD_HOST_MARKERS = (
    "recommend_list",
    "shopping",
    "map.baidu.com",
    "author.baidu.com",
    "weather",
)


class SearchEngineError(RuntimeError):
    """免费搜索后端的可分类错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SearchItem:
    title: str
    url: str
    snippet: str = ""
    source: str = ""


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    engine: str
    latency_ms: int
    error: str = ""
    error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "error_code": self.error_code,
        }


def _clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _looks_like_ad(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    host = (parts.hostname or "").lower().rstrip(".")
    if host in _AD_HOSTS or any(host.endswith("." + item) for item in _AD_HOSTS):
        return True
    path = (parts.path or "").lower()
    return any(marker in path for marker in _AD_PATH_MARKERS)


def _is_baidu_card_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in {"baidu.com", "www.baidu.com", "m.baidu.com"}:
        return True
    if not host.endswith(".baidu.com"):
        return False
    return any(marker in host for marker in _BAIDU_CARD_HOST_MARKERS)


def _collect_summary_texts(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"text", "content"} and isinstance(child, str):
                out.append(child)
            elif key not in {"url", "link", "pic_url", "image"}:
                _collect_summary_texts(child, out)
    elif isinstance(value, list):
        for child in value:
            _collect_summary_texts(child, out)


def _baidu_snippet(block: str) -> str:
    marker = 'data-sanssr-cmpt="card/www-summary"'
    position = block.find(marker)
    if position < 0:
        return ""
    data_start = block.find("<!--s-data:", position)
    if data_start < 0:
        return ""
    data_start += len("<!--s-data:")
    data_end = block.find("-->", data_start)
    if data_end < 0:
        return ""
    try:
        data = json.loads(html.unescape(block[data_start:data_end]))
    except (TypeError, ValueError):
        return ""
    texts: list[str] = []
    if isinstance(data, dict):
        _collect_summary_texts(data.get("summaryData"), texts)
    else:
        _collect_summary_texts(data, texts)
    seen: set[str] = set()
    parts: list[str] = []
    for text in texts:
        cleaned = _clean_text(text)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            parts.append(cleaned)
    return " ".join(parts)[:600]


class BingRssEngine:
    """Bing RSS 搜索（免费、无广告，但受 Microsoft RSS 使用条款约束）。"""

    name = "bing_rss"

    def __init__(
        self,
        *,
        endpoint: str = _BING_URL,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def search(self, query: str, max_results: int) -> list[SearchItem]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=5.0),
                follow_redirects=True,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                },
            ) as client:
                response = await client.get(
                    self.endpoint,
                    params={
                        "q": query,
                        "format": "rss",
                        "mkt": "zh-CN",
                        "setlang": "zh-Hans",
                    },
                )
                response.raise_for_status()
                body = response.content
        except httpx.TimeoutException as exc:
            raise SearchEngineError(
                "timeout", f"Bing RSS 请求超时：{exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise SearchEngineError(
                "http_error",
                f"Bing RSS HTTP {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchEngineError("network_error", f"Bing RSS 连接失败：{exc}") from exc
        if len(body) > _MAX_BODY_BYTES:
            raise SearchEngineError("response_too_large", "Bing RSS 响应过大")
        return self._parse(body, max_results)

    def _parse(self, body: bytes, max_results: int) -> list[SearchItem]:
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise SearchEngineError("parse_failed", f"Bing RSS 解析失败：{exc}") from exc
        items: list[SearchItem] = []
        for node in root.findall("./channel/item"):
            title = _clean_text(node.findtext("title"))
            url = (node.findtext("link") or "").strip()
            snippet = _clean_text(node.findtext("description"))
            if not title or not url or _looks_like_ad(url):
                continue
            items.append(
                SearchItem(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source=self.name,
                )
            )
            if len(items) >= max_results:
                break
        return items

    async def probe(self, *, timeout_seconds: float | None = None) -> ProbeResult:
        started = time.monotonic()
        engine = BingRssEngine(
            endpoint=self.endpoint,
            timeout_seconds=timeout_seconds or min(self.timeout_seconds, 8.0),
        )
        try:
            await engine.search("KarenBot", 1)
        except SearchEngineError as exc:
            return ProbeResult(
                ok=False,
                engine=self.name,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=exc.message,
                error_code=exc.code,
            )
        return ProbeResult(
            ok=True,
            engine=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class BaiduEngine:
    """百度移动页搜索（国内可直连；会过滤明显广告和百度卡片）。

    百度没有公开的免密钥搜索 API，这里解析其移动端 HTML。页面带有大量
    前端资源，需限制响应体积；出现安全验证页时明确报错，不做绕过。
    """

    name = "baidu"

    def __init__(
        self,
        *,
        endpoint: str = _BAIDU_URL,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def search(self, query: str, max_results: int) -> list[SearchItem]:
        body = await self._fetch(query, max_results)
        if "百度安全验证" in body:
            raise SearchEngineError(
                "blocked", "百度返回安全验证页，请稍后再试或换用 Bing RSS"
            )
        return self._parse(body, max_results)

    async def _fetch(self, query: str, max_results: int) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=5.0),
                follow_redirects=True,
                headers={
                    "User-Agent": _BAIDU_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                },
            ) as client:
                response = await client.get(
                    self.endpoint,
                    params={
                        "wd": query,
                        "ie": "utf-8",
                        "rn": max(10, min(max_results + 5, 20)),
                    },
                )
                response.raise_for_status()
                body = response.content
        except httpx.TimeoutException as exc:
            raise SearchEngineError("timeout", f"百度搜索请求超时：{exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise SearchEngineError(
                "http_error", f"百度搜索 HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchEngineError("network_error", f"百度搜索连接失败：{exc}") from exc
        if len(body) > _BAIDU_MAX_BODY_BYTES:
            raise SearchEngineError("response_too_large", "百度搜索响应过大")
        return body.decode("utf-8", errors="replace")

    def _parse(self, body: str, max_results: int) -> list[SearchItem]:
        starts = [match.start() for match in _BAIDU_RESULT_RE.finditer(body)]
        items: list[SearchItem] = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(body)
            block = body[start:end]
            if any(marker in block for marker in _BAIDU_AD_MARKERS):
                continue
            match = _BAIDU_RESULT_RE.match(body, start)
            if match is None:
                continue
            attrs = html.unescape(match.group(1))
            log_match = re.search(r"data-log='([^']*)'", attrs)
            if log_match is None:
                log_match = re.search(r'data-log="([^"]*)"', attrs)
            if log_match is None:
                continue
            try:
                log = json.loads(html.unescape(log_match.group(1)))
            except (TypeError, ValueError):
                continue
            if not isinstance(log, dict):
                continue
            info = log.get("logCommonInfo") or {}
            exposure = info.get("exposureInfo") if isinstance(info, dict) else None
            con_type = (
                str(exposure.get("con_type") or "").lower()
                if isinstance(exposure, dict)
                else ""
            )
            if "ad" in con_type or "sponsor" in con_type:
                continue
            url = str(log.get("mu") or "").strip()
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
            if _is_baidu_card_host(parts.hostname):
                continue
            title_match = re.search(r"<h3\b[^>]*>(.*?)</h3>", block, re.S | re.I)
            title = _clean_text(title_match.group(1)) if title_match else ""
            if not title:
                continue
            items.append(
                SearchItem(
                    title=title,
                    url=url,
                    snippet=_baidu_snippet(block),
                    source=self.name,
                )
            )
            if len(items) >= max_results:
                break
        return items

    async def probe(self, *, timeout_seconds: float | None = None) -> ProbeResult:
        started = time.monotonic()
        engine = BaiduEngine(
            endpoint=self.endpoint,
            timeout_seconds=timeout_seconds or min(self.timeout_seconds, 8.0),
        )
        try:
            await engine.search("KarenBot", 1)
        except SearchEngineError as exc:
            return ProbeResult(
                ok=False,
                engine=self.name,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=exc.message,
                error_code=exc.code,
            )
        return ProbeResult(
            ok=True,
            engine=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class SearxngEngine:
    """自建/受信任 SearXNG 的 JSON 搜索。

    需要目标实例开启 ``format=json``。默认不跟随跨站重定向，避免把配置的
    地址变成任意 SSRF 跳板。
    """

    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        candidate = (base_url or "").strip().rstrip("/")
        parts = urlsplit(candidate)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("SearXNG 地址必须是 http(s) URL")
        if parts.username or parts.password:
            raise ValueError("SearXNG 地址不能包含账号密码")
        self.base_url = candidate
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def search(self, query: str, max_results: int) -> list[SearchItem]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=5.0),
                follow_redirects=False,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/json",
                },
            ) as client:
                response = await client.get(
                    f"{self.base_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "language": "zh-CN",
                        "safesearch": "1",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as exc:
            raise SearchEngineError("timeout", f"SearXNG 请求超时：{exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise SearchEngineError(
                "http_error",
                f"SearXNG HTTP {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchEngineError("network_error", f"SearXNG 连接失败：{exc}") from exc
        except ValueError as exc:
            raise SearchEngineError("parse_failed", f"SearXNG JSON 解析失败：{exc}") from exc
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            raise SearchEngineError("parse_failed", "SearXNG 响应缺少 results")
        items: list[SearchItem] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            title = _clean_text(str(raw.get("title") or ""))
            url = str(raw.get("url") or "").strip()
            snippet = _clean_text(
                str(raw.get("content") or raw.get("snippet") or "")
            )
            if not title or not url or _looks_like_ad(url):
                continue
            items.append(
                SearchItem(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source=self.name,
                )
            )
            if len(items) >= max_results:
                break
        return items

    async def probe(self, *, timeout_seconds: float | None = None) -> ProbeResult:
        started = time.monotonic()
        engine = SearxngEngine(
            self.base_url,
            timeout_seconds=timeout_seconds or min(self.timeout_seconds, 8.0),
        )
        try:
            await engine.search("KarenBot", 1)
        except (SearchEngineError, ValueError) as exc:
            return ProbeResult(
                ok=False,
                engine=self.name,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=getattr(exc, "message", str(exc)),
                error_code=getattr(exc, "code", "invalid_config"),
            )
        return ProbeResult(
            ok=True,
            engine=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class DuckDuckGoEngine:
    """DuckDuckGo（ddgs）后端实现；国内通常不可用，未暴露为设置选项。"""

    name = "ddg"

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def search(self, query: str, max_results: int) -> list[SearchItem]:
        try:
            raw_results = await asyncio.wait_for(
                asyncio.to_thread(self._sync_search, query, max_results),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise SearchEngineError("timeout", "DuckDuckGo 请求超时") from exc
        except Exception as exc:  # noqa: BLE001 - ddgs 的异常类型不稳定
            raise SearchEngineError(
                "network_error", f"DuckDuckGo 连接失败：{type(exc).__name__}: {exc}"
            ) from exc
        items: list[SearchItem] = []
        for raw in raw_results:
            title = _clean_text(raw.get("title"))
            url = str(raw.get("href") or raw.get("url") or "").strip()
            snippet = _clean_text(raw.get("body") or raw.get("snippet"))
            if not title or not url or _looks_like_ad(url):
                continue
            items.append(
                SearchItem(title=title, url=url, snippet=snippet, source=self.name)
            )
        return items

    def _sync_search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        from ddgs import DDGS

        with DDGS(timeout=int(self.timeout_seconds)) as ddgs:
            return list(
                ddgs.text(
                    query,
                    backend="duckduckgo",
                    max_results=max_results,
                )
            )

    async def probe(self, *, timeout_seconds: float | None = None) -> ProbeResult:
        started = time.monotonic()
        engine = DuckDuckGoEngine(
            timeout_seconds=timeout_seconds or min(self.timeout_seconds, 8.0)
        )
        try:
            await engine.search("KarenBot", 1)
        except SearchEngineError as exc:
            return ProbeResult(
                ok=False,
                engine=self.name,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=exc.message,
                error_code=exc.code,
            )
        return ProbeResult(
            ok=True,
            engine=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def build_free_engine(
    provider: str,
    *,
    searxng_base_url: str = "",
    timeout_seconds: float = 10.0,
) -> BingRssEngine | BaiduEngine | SearxngEngine | DuckDuckGoEngine:
    provider = (provider or "bing_rss").strip().lower()
    if provider == "bing_rss":
        return BingRssEngine(timeout_seconds=timeout_seconds)
    if provider == "baidu":
        return BaiduEngine(timeout_seconds=timeout_seconds)
    if provider == "searxng":
        return SearxngEngine(
            searxng_base_url,
            timeout_seconds=timeout_seconds,
        )
    if provider == "ddg":
        # 保留实现但不再作为设置选项；国内通常不可用。
        return DuckDuckGoEngine(timeout_seconds=timeout_seconds)
    raise ValueError(f"未知免费搜索后端：{provider}")


__all__ = [
    "BaiduEngine",
    "BingRssEngine",
    "DuckDuckGoEngine",
    "ProbeResult",
    "SearchEngineError",
    "SearchItem",
    "SearxngEngine",
    "build_free_engine",
]
