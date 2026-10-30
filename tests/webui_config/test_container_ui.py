"""Container WebUI/API wiring contracts."""

from __future__ import annotations

from pathlib import Path

from webui.api import ALL_ROUTERS
from webui.settings_spec import field_map

ROOT = Path(__file__).resolve().parents[2]


def test_container_settings_paths_exist_in_declaration_table():
    fields = field_map()
    for path in (
        "features.container.enabled",
        "features.container.storage_mode",
        "features.container.image",
        "features.container.workspace_size_mb",
        "features.container.memory_mb",
        "features.container.cpus",
        "features.container.network",
        "features.container.auto_pull_image",
        "features.container.image_mirror",
        "features.container.image_fixed_mountpoint",
    ):
        spec = fields[path]
        assert spec.get("help")
        assert spec.get("needs_restart") is True
    assert fields["features.container.auto_pull_image"]["default"] is True


def test_container_page_nav_api_and_router_are_registered():
    app_js = (ROOT / "webui/static/js/app.js").read_text(encoding="utf-8")
    page_js = (ROOT / "webui/static/js/pages/container.js").read_text(
        encoding="utf-8"
    )
    api_js = (ROOT / "webui/static/js/api.js").read_text(encoding="utf-8")
    assert "pages/container.js" in app_js
    assert "key: 'container'" in app_js
    assert "endpoints.containerStatus" in page_js
    assert "endpoints.containerTest" in page_js
    assert "containerStatus:" in api_js
    assert "/api/container/status" in api_js
    prefixes = {getattr(router, "prefix", "") for router in ALL_ROUTERS}
    assert "/api/container" in prefixes
