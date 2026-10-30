"""Container storage setup API contracts (no real sudo/UAC)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from features.container import ContainerError
from webui.api import container as container_api

ROOT = Path(__file__).resolve().parents[2]


def _request(service) -> SimpleNamespace:
    host = SimpleNamespace(state="running", rt=SimpleNamespace(container=service))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host=host)))


class FakeStorage:
    def status(self, *, include_host_paths: bool = True):
        return SimpleNamespace(
            as_dict=lambda **kwargs: {
                "mode": "image_fixed",
                "ready": False,
                "error": "not mounted",
            }
        )

    def setup_commands(self):
        return {
            "mode": "image_fixed",
            "command": "sudo bash setup.sh",
            "argv": ["sudo", "bash", "setup.sh"],
            "note": "manual",
        }


class FakeService:
    platform_name = "Linux"
    storage = FakeStorage()

    def __init__(self) -> None:
        self.passwords: list[str | None] = []

    async def setup_storage(self, *, password=None):
        self.passwords.append(password)
        return {
            "ok": True,
            "already_ready": False,
            "admin_method": "sudo_password",
            "storage": {"ready": True},
        }


@pytest.mark.asyncio
async def test_storage_endpoint_exposes_admin_method_without_paths_in_public_status():
    data = await container_api.container_storage(_request(FakeService()))
    assert data["admin"]["method"] == "sudo_password"
    assert data["admin"]["requires_password"] is True
    assert data["setup"]["command"] == "sudo bash setup.sh"


@pytest.mark.asyncio
async def test_setup_endpoint_passes_password_and_does_not_return_it():
    service = FakeService()
    data = await container_api.container_storage_setup(
        _request(service), {"password": "s3cret"}
    )
    assert service.passwords == ["s3cret"]
    assert "s3cret" not in str(data)
    assert data["storage"]["ready"] is True


@pytest.mark.asyncio
async def test_setup_endpoint_rejects_non_string_password():
    with pytest.raises(HTTPException) as error:
        await container_api.container_storage_setup(
            _request(FakeService()), {"password": 123}
        )
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "container_admin_password_invalid"


@pytest.mark.asyncio
async def test_setup_endpoint_returns_safe_details_on_failure():
    class FailingService(FakeService):
        async def setup_storage(self, *, password=None):
            raise ContainerError(
                "sudo 密码不正确或认证失败",
                code="container_admin_auth_failed",
                details={"exit_code": 1, "output_tail": "incorrect password"},
            )

    with pytest.raises(HTTPException) as error:
        await container_api.container_storage_setup(
            _request(FailingService()), {"password": "wrong"}
        )
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "container_admin_auth_failed"
    assert error.value.detail["details"]["exit_code"] == 1


def test_frontend_has_setup_modal_and_first_enable_hook():
    api_js = (ROOT / "webui/static/js/api.js").read_text(encoding="utf-8")
    setup_js = (ROOT / "webui/static/js/container_setup.js").read_text(
        encoding="utf-8"
    )
    settings_js = (ROOT / "webui/static/js/pages/settings.js").read_text(
        encoding="utf-8"
    )
    page_js = (ROOT / "webui/static/js/pages/container.js").read_text(
        encoding="utf-8"
    )
    assert "containerStorageSetup" in api_js
    assert "/api/container/storage/setup" in api_js
    assert "sudo -S" in setup_js
    assert "UAC" in setup_js
    assert "localStorage" not in setup_js
    assert "promptStorageSetup" in settings_js
    assert "features.container.enabled" in settings_js
    assert "promptStorageSetup" in page_js
