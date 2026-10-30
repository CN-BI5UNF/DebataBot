"""Container CLI dispatch without invoking Docker or sudo."""

from __future__ import annotations

from types import SimpleNamespace

import service_cli
from main import _SERVICE_COMMANDS


class FakeStorage:
    mode = "image_fixed"

    def status(self, *, include_host_paths=True):
        return SimpleNamespace(
            as_dict=lambda **kwargs: {
                "mode": "image_fixed",
                "ready": False,
                "error": "not mounted",
            }
        )

    def setup_commands(self):
        return {
            "note": "admin setup",
            "command": "sudo bash setup.sh",
            "argv": ["sudo", "bash", "setup.sh"],
        }


class FakeService:
    storage = FakeStorage()

    async def status(self, *, public=False):
        return {"enabled": True, "docker": {"available": False}}

    async def ensure_running(self):
        return None

    async def stop(self):
        return None

    async def reset(self):
        return None


def test_main_routes_container_command():
    assert "container" in _SERVICE_COMMANDS


def test_container_storage_status_and_setup_print_only(monkeypatch, capsys):
    monkeypatch.setattr(service_cli, "_container_service", lambda: FakeService())
    called: list[list[str]] = []
    monkeypatch.setattr(
        service_cli.subprocess, "call", lambda argv: called.append(list(argv)) or 0
    )

    assert service_cli.cmd_container(["storage", "status"]) == 0
    output = capsys.readouterr().out
    assert "image_fixed" in output
    assert "setup.sh" in output

    assert service_cli.cmd_container(["storage", "setup"]) == 0
    assert called == []
    assert "admin setup" in capsys.readouterr().out


def test_container_status_dispatches_async(monkeypatch, capsys):
    monkeypatch.setattr(service_cli, "_container_service", lambda: FakeService())
    assert service_cli.cmd_container(["status"]) == 0
    output = capsys.readouterr().out
    assert '"enabled": true' in output


def test_container_reset_requires_yes(monkeypatch, capsys):
    monkeypatch.setattr(service_cli, "_container_service", lambda: FakeService())
    assert service_cli.cmd_container(["reset"]) == 0
    assert "--yes" in capsys.readouterr().out


def test_container_setup_yes_uses_argv_without_shell(monkeypatch):
    monkeypatch.setattr(service_cli, "_container_service", lambda: FakeService())
    called: list[list[str]] = []
    monkeypatch.setattr(
        service_cli.subprocess,
        "call",
        lambda argv: called.append(list(argv)) or 0,
    )
    assert service_cli.cmd_container(["storage", "setup", "--yes"]) == 0
    assert called == [["sudo", "bash", "setup.sh"]]
