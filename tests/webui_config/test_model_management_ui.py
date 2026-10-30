"""Model management page and collapsible sidebar contracts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "webui/static/js"


def test_settings_no_longer_has_model_tab():
    settings = (JS / "pages/settings.js").read_text(encoding="utf-8")
    assert "key: 'models'" not in settings
    assert "renderModels(" not in settings


def test_model_management_hosts_provider_and_local_panels():
    models = (JS / "pages/models.js").read_text(encoding="utf-8")
    providers = (JS / "providers.js").read_text(encoding="utf-8")
    assert "renderModelsPanel" in models
    assert "providerHost" in models
    assert "本地模型插件" in models
    assert "model_vendor" in providers
    assert "上游模型厂商" in providers
    assert "detectModelVendor" in providers


def test_sidebar_has_icon_only_collapse():
    html = (ROOT / "webui/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "webui/static/app.css").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")
    assert 'id="btn-sidebar"' in html
    assert "sidebar-toggle" in html
    assert ".sidebar.collapsed" in css
    assert "karen-sidebar-collapsed" in app
    assert "applySidebarCollapsed" in app
    assert "aria-expanded" in app
