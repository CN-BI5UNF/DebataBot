"""整进程重启后端的接口契约。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from webui.api import system as system_api


def test_backend_restart_supported_requires_systemd_invocation(monkeypatch):
    monkeypatch.setattr(system_api.sys, "platform", "linux")
    monkeypatch.setattr(system_api.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert system_api.backend_restart_supported() is False
    monkeypatch.setenv("INVOCATION_ID", "test-invocation")
    assert system_api.backend_restart_supported() is True


@pytest.mark.asyncio
async def test_restart_backend_rejects_unmanaged_process(monkeypatch):
    monkeypatch.setattr(system_api, "backend_restart_supported", lambda: False)
    with pytest.raises(HTTPException) as exc:
        await system_api.restart_backend()
    assert exc.value.status_code == 400
    assert "systemd" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_restart_backend_schedules_restart_after_response(monkeypatch):
    called = asyncio.Event()

    async def fake_restart():
        called.set()

    monkeypatch.setattr(system_api, "backend_restart_supported", lambda: True)
    monkeypatch.setattr(system_api, "_restart_backend_after_delay", fake_restart)
    result = await system_api.restart_backend()
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    await asyncio.wait_for(called.wait(), timeout=1.0)


def test_restart_backend_route_is_registered():
    paths = {route.path for route in system_api.router.routes}
    assert "/api/system/restart-backend" in paths
