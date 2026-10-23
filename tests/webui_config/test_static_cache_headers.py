"""无构建 WebUI 的静态缓存契约。

前端文件名没有内容哈希；如果浏览器长期缓存旧 app.css，就会出现“按钮类名
切换了但样式没变”的假故障。这里确认 index 与 /static 都要求每次重新验证。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from webui.server import _install_static_cache_headers

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_index_and_static_are_revalidated(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "app.css").write_text("body {}\n", encoding="utf-8")

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def index() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/api/plain")
    async def plain() -> PlainTextResponse:
        return PlainTextResponse("api")

    _install_static_cache_headers(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/", "/static/app.css"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache, must-revalidate"
        # API 不应被套上静态缓存策略。
        response = await client.get("/api/plain")
        assert response.status_code == 200
        assert "cache-control" not in response.headers


def test_index_uses_versioned_asset_urls():
    html = (ROOT / "webui" / "static" / "index.html").read_text(encoding="utf-8")
    assert "/static/app.css?v=" in html
    assert "/static/js/app.js?v=" in html
