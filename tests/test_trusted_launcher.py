"""U01-L：独立 launcher 的合成兼容检查；不部署 systemd。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "karen_trusted_launcher", ROOT / "deploy" / "trusted_launcher.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    return {
        "manifest_format": 1,
        "minimum_launcher_protocol": 1,
        "data_contract_epoch": 0,
        "migration_set_id": "",
        "irreversible": False,
        "planes": {
            "root_config": {
                "supported_read": 2, "supported_write": 2, "target_version": 2,
            },
            "persona_db": {
                "supported_read": 1, "supported_write": 1, "target_version": 1,
            },
            "maintenance": {
                "supported_read": 1, "supported_write": 1, "target_version": 1,
            },
            "rag": {
                "supported_read": 0, "supported_write": 0, "target_version": 0,
            },
            "projection": {
                "supported_read": 0, "supported_write": 0, "target_version": 0,
            },
        },
        "required_features": [],
        "optional_features": [],
    }


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    data = tmp_path / "data"
    project.mkdir()
    data.mkdir()
    (project / "compatibility.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )
    (data / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    (data / "memory" / "demo").mkdir(parents=True)
    with sqlite3.connect(data / "memory" / "demo" / "chat.db") as conn:
        conn.execute("PRAGMA user_version=1")
    with sqlite3.connect(data / "maintenance.sqlite3") as conn:
        conn.execute("PRAGMA user_version=1")
    return project, data


def test_launcher_accepts_current_contract(tmp_path):
    launcher = _load_launcher()
    project, data = _seed(tmp_path)
    result = launcher.check(project, data)
    assert result["ok"] is True, result
    assert result["local"]["planes"]["persona_db"] == 1
    assert result["local"]["planes"]["maintenance"] == 1


def test_launcher_blocks_migration_and_phase(tmp_path):
    launcher = _load_launcher()
    project, data = _seed(tmp_path)
    manifest = _manifest()
    manifest["planes"]["persona_db"].update(
        supported_read=2, supported_write=2, target_version=2
    )
    (project / "compatibility.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = launcher.check(project, data)
    assert result["ok"] is False
    assert "migration_required" in result["codes"]

    manifest = _manifest()
    (project / "compatibility.json").write_text(json.dumps(manifest), encoding="utf-8")
    (data / "compatibility_state.json").write_text(
        json.dumps({"state_format": 1, "phase": "preparing"}), encoding="utf-8"
    )
    result = launcher.check(project, data)
    assert result["ok"] is False
    assert "migration_in_progress" in result["codes"]


def test_launcher_allows_first_run_but_not_protocol_too_new(tmp_path):
    launcher = _load_launcher()
    project = tmp_path / "project"
    data = tmp_path / "data"
    project.mkdir()
    data.mkdir()
    (project / "compatibility.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )
    result = launcher.check(project, data)
    assert result["ok"] is False
    assert result["code"] == "setup_required"
    assert result["setup_allowed"] is True
    assert result["local"]["first_run"] is True

    manifest = _manifest()
    manifest["minimum_launcher_protocol"] = 2
    (project / "compatibility.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = launcher.check(project, data)
    assert result["ok"] is False
    assert "manifest_invalid" in result["codes"]


def test_launcher_binds_project_data_and_command(tmp_path):
    launcher = _load_launcher()
    project, data = _seed(tmp_path)
    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (project / "main.py").write_text("# entry\n", encoding="utf-8")
    command = [str(python), str(project / "main.py"), "--host", "127.0.0.1"]
    assert launcher._validate_target_command(
        project, data, command, config_path=None
    ) is None
    assert launcher._validate_target_command(
        project, data, [str(python), str(project / "webui_server.py")],
        config_path=None,
    )
    assert launcher._validate_target_command(
        project, data, ["/usr/bin/python3", str(project / "main.py")],
        config_path=None,
    )
    assert launcher._validate_target_command(
        project, data,
        [str(python), str(project / "main.py"), "--config",
         str(tmp_path / "other.yaml")],
        config_path=None,
    )
    assert launcher._validate_target_command(
        project, data,
        [str(python), str(project / "main.py"), "--config",
         str(data / "config.yaml")],
        config_path=None,
    ) is None


def test_launcher_rejects_manifest_outside_project(tmp_path):
    launcher = _load_launcher()
    project, data = _seed(tmp_path)
    outside = tmp_path / "compatibility.json"
    outside.write_text((project / "compatibility.json").read_text(), encoding="utf-8")
    result = launcher.check(project, data, manifest_path=outside)
    assert result["ok"] is False
    assert result["code"] == "manifest_path_escape"


def test_launcher_obeys_maintenance_state(tmp_path):
    launcher = _load_launcher()
    project, data = _seed(tmp_path)
    state_dir = data / "maintenance"
    state_dir.mkdir()
    (state_dir / "controller_state.json").write_text(
        json.dumps({"schema": 1, "phase": "snapshotting", "generation": 3}),
        encoding="utf-8",
    )
    result = launcher.check(project, data)
    assert result["ok"] is False
    assert result["code"] == "maintenance_blocked"

    (state_dir / "controller_state.json").write_text("{bad", encoding="utf-8")
    result = launcher.check(project, data)
    assert result["ok"] is False
    assert result["code"] == "maintenance_state_invalid"


def test_launcher_does_not_execute_on_incompatible(tmp_path):
    launcher = _load_launcher()
    project, data = _seed(tmp_path)
    manifest = _manifest()
    manifest["planes"]["maintenance"].update(
        supported_read=2, supported_write=2, target_version=2
    )
    (project / "compatibility.json").write_text(json.dumps(manifest), encoding="utf-8")
    marker = tmp_path / "executed"
    command = f"from pathlib import Path; Path(r'{marker}').write_text('ran')"
    import sys

    code = launcher.main([
        "--project-root", str(project),
        "--data-dir", str(data),
        "--json",
        "--", sys.executable, "-c", command,
    ])
    assert code == launcher.EXIT_INCOMPATIBLE
    assert not marker.exists()
