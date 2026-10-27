"""整进程重启后端的接口契约。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core import backend_restart
from webui.api import system as system_api


def _request(server=None, host=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                server=server,
                host=host or SimpleNamespace(),
            )
        )
    )


def _platform_linux(monkeypatch):
    monkeypatch.setattr(backend_restart.sys, "platform", "linux")
    monkeypatch.setattr(backend_restart.shutil, "which", lambda name: "/usr/bin/systemctl")


def test_backend_restart_supported_allows_linux_fallback(monkeypatch):
    monkeypatch.setattr(system_api.sys, "platform", "linux")
    monkeypatch.setattr(
        backend_restart, "_read_self_cgroup",
        lambda: "0::/system.slice/system-vncserver.slice/vncserver@1.service",
    )
    assert system_api.backend_restart_supported() is True
    monkeypatch.setattr(system_api.sys, "platform", "win32")
    assert system_api.backend_restart_supported() is False


def test_systemd_scope_ignores_inherited_outer_service_env(monkeypatch):
    """从 VNC/桌面服务里启动时，残留的 INVOCATION_ID 不能当成受管理。"""
    _platform_linux(monkeypatch)
    monkeypatch.setenv("INVOCATION_ID", "outer-session")
    monkeypatch.setattr(
        backend_restart, "_read_self_cgroup",
        lambda: "0::/system.slice/system-vncserver.slice/vncserver@1.service",
    )
    assert backend_restart.systemd_scope() is None
    assert backend_restart.systemd_managed() is False


def test_systemd_scope_detects_user_and_system_units(monkeypatch):
    _platform_linux(monkeypatch)
    cgroup = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/debata-agent.service"
    monkeypatch.setattr(backend_restart, "_read_self_cgroup", lambda: cgroup)
    assert backend_restart.systemd_scope() == "user"

    monkeypatch.setattr(
        backend_restart, "_read_self_cgroup",
        lambda: "0::/system.slice/debata-agent.service",
    )
    assert backend_restart.systemd_scope() == "system"


def test_systemd_restart_commands_keep_scope():
    assert backend_restart._systemd_restart_command("user") == [
        "systemctl", "--user", "--no-block", "restart", "debata-agent.service",
    ]
    assert backend_restart._systemd_restart_command("system") == [
        "systemctl", "--no-block", "restart", "debata-agent.service",
    ]


def test_request_backend_restart_uses_fallback_helper(monkeypatch):
    called = []
    monkeypatch.setattr(backend_restart, "systemd_scope", lambda: None)
    monkeypatch.setattr(
        backend_restart, "spawn_fallback_restart_helper", lambda: called.append(True)
    )
    assert backend_restart.request_backend_restart() == "fallback"
    assert called == [True]


def test_request_backend_restart_uses_systemctl(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))

    monkeypatch.setattr(backend_restart, "systemd_scope", lambda: "user")
    monkeypatch.setattr(backend_restart.subprocess, "Popen", FakePopen)
    assert backend_restart.request_backend_restart() == "systemd"
    command, kwargs = calls[0]
    assert command[:2] == ["systemctl", "--user"]
    assert kwargs["start_new_session"] is True


@pytest.mark.asyncio
async def test_restart_backend_rejects_unmanaged_process(monkeypatch):
    monkeypatch.setattr(system_api, "backend_restart_supported", lambda: False)
    with pytest.raises(HTTPException) as exc:
        await system_api.restart_backend(_request())
    assert exc.value.status_code == 400
    assert "不支持" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_restart_backend_schedules_restart_after_response(monkeypatch):
    called = asyncio.Event()

    async def fake_restart(**kwargs):
        called.set()

    monkeypatch.setattr(system_api, "backend_restart_supported", lambda: True)
    monkeypatch.setattr(system_api, "_restart_backend_after_delay", fake_restart)
    result = await system_api.restart_backend(_request())
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert result["boot_id"] == system_api.BOOT_ID
    await asyncio.wait_for(called.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_auth_state_exposes_boot_id(monkeypatch):
    class FakeAuth:
        enabled = False

        @staticmethod
        def request_authorized(request):
            return True

    monkeypatch.setattr(system_api, "get_auth", lambda request: FakeAuth())
    result = await system_api.auth_state(SimpleNamespace())
    assert result["boot_id"] == system_api.BOOT_ID


def test_restart_ui_waits_for_a_changed_boot_id():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[2] / "webui/static/js/releases.js"
    ).read_text(encoding="utf-8")
    assert "previousBootId" in js
    assert "bootId !== previousBootId" in js
    assert "restartResponse && restartResponse.boot_id" in js


def test_spawn_fallback_restart_helper_passes_current_args(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))

    monkeypatch.setattr(backend_restart.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(backend_restart.os, "getpid", lambda: 12345)
    monkeypatch.setattr(
        backend_restart.sys, "argv", ["main.py", "--host", "0.0.0.0", "--port", "8000"]
    )

    backend_restart.spawn_fallback_restart_helper()

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[-2:] == ["restart-helper", "12345"]
    assert command[1].endswith("main.py")
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["DEBATA_SERVICE_ARGS"] == "--host 0.0.0.0 --port 8000"


@pytest.mark.asyncio
async def test_restart_after_delay_stops_fallback_server(monkeypatch):
    calls = []

    class FakeHost:
        def request_stop(self):
            calls.append("host_stop")

    class FakeServer:
        should_exit = False

    server = FakeServer()
    monkeypatch.setattr(system_api, "BACKEND_RESTART_DELAY_SECONDS", 0)
    monkeypatch.setattr(system_api, "request_backend_restart", lambda: "fallback")

    await system_api._restart_backend_after_delay(server=server, host=FakeHost())

    assert calls == ["host_stop"]
    assert server.should_exit is True


@pytest.mark.asyncio
async def test_restart_callback_terminates_fallback_process(monkeypatch):
    kills = []
    monkeypatch.setattr(backend_restart, "request_backend_restart", lambda: "fallback")
    monkeypatch.setattr(
        backend_restart.os, "kill",
        lambda pid, sig: kills.append((pid, sig)),
    )
    monkeypatch.setattr(backend_restart.asyncio, "sleep", _no_sleep)

    await backend_restart.restart_backend_callback()

    assert kills == [(backend_restart.os.getpid(), backend_restart.signal.SIGTERM)]


async def _no_sleep(_seconds):
    return None


def test_restart_backend_route_is_registered():
    paths = {route.path for route in system_api.router.routes}
    assert "/api/system/restart-backend" in paths
