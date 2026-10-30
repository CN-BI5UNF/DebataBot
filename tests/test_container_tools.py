"""container tool dispatch and disabled behavior, using a fake service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.tools.helpers import _make_config
from tools import build_default_registry
from tools.container_tools import container as container_tool
from tools.schemas import ContainerArgs


class FakeStorage:
    def status(self, *, include_host_paths: bool = True):
        return SimpleNamespace(
            as_dict=lambda **kwargs: {
                "mode": "volume_soft",
                "ready": True,
                "mountpoint": "hidden" if not include_host_paths else "/host",
            }
        )


class FakeService:
    enabled = True
    storage = FakeStorage()

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def exec(self, command, *, timeout_seconds=None):
        self.calls.append(("exec", (command,), {"timeout": timeout_seconds}))
        return {"ok": True, "stdout": command}

    async def read_file(self, path):
        self.calls.append(("read", (path,), {}))
        return {"ok": True, "path": path, "content": "x"}

    async def write_file(self, path, content):
        self.calls.append(("write", (path, content), {}))
        return {"ok": True, "path": path}

    async def edit_file(self, path, old, new):
        self.calls.append(("edit", (path, old, new), {}))
        return {"ok": True, "path": path}

    async def list_files(self, path):
        self.calls.append(("list", (path,), {}))
        return {"ok": True, "listing": ""}

    async def import_file(
        self, host_path, container_path, *, workspace_dir, overwrite=False
    ):
        self.calls.append(("import", (host_path, container_path, overwrite), {}))
        return {"ok": True}

    async def export_file(self, container_path, host_path, *, workspace_dir, overwrite=False):
        self.calls.append(("export", (container_path, host_path, overwrite), {}))
        return {"ok": True}

    async def reset(self):
        self.calls.append(("reset", (), {}))
        return {"ok": True}

    async def status(self, *, public=True):
        self.calls.append(("status", (public,), {}))
        return {"enabled": True}


@pytest.mark.asyncio
async def test_container_tool_dispatches_actions():
    service = FakeService()
    ctx = SimpleNamespace(container=service, workspace_dir=None)

    result = await container_tool(ContainerArgs(action="exec", command="pwd"), ctx)
    assert result["ok"] is True
    assert service.calls[-1][0] == "exec"

    await container_tool(ContainerArgs(action="read_file", path="/workspace/a"), ctx)
    await container_tool(
        ContainerArgs(action="write_file", path="/workspace/a", content="x"), ctx
    )
    await container_tool(
        ContainerArgs(action="edit_file", path="/workspace/a", old="x", new="y"),
        ctx,
    )
    await container_tool(ContainerArgs(action="list_files"), ctx)
    await container_tool(
        ContainerArgs(
            action="import_file",
            host_path="a.txt",
            container_path="/workspace/a.txt",
        ),
        ctx,
    )
    await container_tool(
        ContainerArgs(
            action="export_file",
            host_path="out.txt",
            container_path="/workspace/a.txt",
        ),
        ctx,
    )
    await container_tool(ContainerArgs(action="reset"), ctx)
    await container_tool(ContainerArgs(action="status"), ctx)
    assert [call[0] for call in service.calls] == [
        "exec",
        "read",
        "write",
        "edit",
        "list",
        "import",
        "export",
        "reset",
        "status",
    ]


def test_container_tool_is_registered_only_when_enabled():
    disabled = build_default_registry(_make_config())
    assert "container" not in disabled

    enabled_cfg = _make_config()
    enabled_cfg.features.container.enabled = True
    enabled = build_default_registry(enabled_cfg)
    assert "container" in enabled


@pytest.mark.asyncio
async def test_container_tool_disabled_and_invalid_arguments():
    ctx = SimpleNamespace(container=None, workspace_dir=None)
    disabled = await container_tool(ContainerArgs(action="exec", command="pwd"), ctx)
    assert disabled["ok"] is False
    assert disabled["code"] == "container_disabled"

    service = FakeService()
    ctx = SimpleNamespace(container=service, workspace_dir=None)
    invalid = await container_tool(ContainerArgs(action="exec"), ctx)
    assert invalid["ok"] is False
    assert invalid["code"] == "container_invalid_argument"
    assert service.calls == []
