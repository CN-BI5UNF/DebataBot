"""兼容 manifest 解析、本地数据快照与 fail-closed 评估。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.compat import (
    DEFAULT_PLANES,
    CompatError,
    LocalDataState,
    evaluate,
    evaluate_manifest_text,
    parse_manifest,
    read_local_state,
)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = (ROOT / "compatibility.json").read_text(encoding="utf-8")


def _manifest_dict() -> dict:
    return json.loads(MANIFEST)


def _local(**planes) -> LocalDataState:
    values = dict(DEFAULT_PLANES)
    values.update(planes)
    return LocalDataState(planes=values)


def test_real_manifest_is_valid_and_compatible_with_baseline():
    manifest = parse_manifest(MANIFEST)
    assert manifest.manifest_format == 1
    assert manifest.minimum_launcher_protocol == 1
    assert manifest.data_contract_epoch == 0
    assert set(manifest.planes) == set(DEFAULT_PLANES)
    result = evaluate(manifest, _local())
    assert result["compatible"] is True
    assert result["code"] == "compatible"
    assert result["manifest_hash"]


def test_real_manifest_hash_is_stable():
    first = parse_manifest(MANIFEST).raw_hash
    second = parse_manifest(MANIFEST).raw_hash
    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(manifest_format=2),
        lambda d: d["planes"].pop("rag"),
        lambda d: d["planes"].update(extra={"supported_read": 1, "supported_write": 1, "target_version": 1}),
        lambda d: d["planes"]["root_config"].update(supported_write=3),
        lambda d: d["planes"]["root_config"].update(target_version=3),
        lambda d: d["planes"]["root_config"].update(target_version="2"),
        lambda d: d.update(required_features="stable_seq"),
    ],
)
def test_bad_manifests_fail_closed(mutate):
    data = _manifest_dict()
    mutate(data)
    with pytest.raises(CompatError):
        parse_manifest(json.dumps(data))


def test_oversized_manifest_is_rejected():
    with pytest.raises(CompatError):
        parse_manifest("{" + " " * (70 * 1024) + "}")


def test_empty_manifest_text_is_invalid():
    result = evaluate_manifest_text("", _local())
    assert result["compatible"] is False
    assert result["code"] == "manifest_invalid"


def test_migration_required_and_too_new_versions():
    manifest = parse_manifest(MANIFEST)
    assert evaluate(manifest, _local(root_config=1))["code"] == "migration_required"
    assert evaluate(manifest, _local(root_config=3))["code"] == "unsupported_data_contract"

    data = _manifest_dict()
    data["planes"]["persona_db"] = {
        "supported_read": 2,
        "supported_write": 2,
        "target_version": 1,
    }
    result = evaluate_manifest_text(json.dumps(data), _local(persona_db=2))
    assert result["code"] == "downgrade_required"


def test_contract_epoch_and_features():
    manifest = parse_manifest(MANIFEST)
    assert evaluate(
        manifest,
        LocalDataState(planes=dict(DEFAULT_PLANES), data_contract_epoch=1),
    )["code"] == "data_contract_too_new"

    data = _manifest_dict()
    data["required_features"] = ["stable_seq"]
    result = evaluate_manifest_text(json.dumps(data), _local())
    assert result["compatible"] is False
    assert result["code"] == "feature_missing"


def test_launcher_protocol_is_checked():
    data = _manifest_dict()
    data["minimum_launcher_protocol"] = 2
    result = evaluate_manifest_text(json.dumps(data), _local())
    assert result["code"] == "launcher_protocol_too_old"


def test_unreadable_local_state_blocks(tmp_path):
    data = tmp_path / "instance"
    data.mkdir()
    (data / "config.yaml").write_text("version: [broken\n", encoding="utf-8")
    state = read_local_state(data)
    assert state.unreadable
    result = evaluate(parse_manifest(MANIFEST), state)
    assert result["compatible"] is False
    assert result["code"] == "data_unreadable"


def test_local_state_reads_versions_without_chat_content(tmp_path):
    data = tmp_path / "instance"
    (data / "memory" / "bot").mkdir(parents=True)
    (data / "vector" / "bot").mkdir(parents=True)
    (data / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    with sqlite3.connect(data / "memory" / "bot" / "bot.db") as conn:
        conn.execute("PRAGMA user_version = 1")
    with sqlite3.connect(data / "vector" / "bot" / "rag.sqlite3") as conn:
        conn.execute("PRAGMA user_version = 0")
    with sqlite3.connect(data / "maintenance.sqlite3") as conn:
        conn.execute("PRAGMA user_version = 1")
    (data / "compatibility_state.json").write_text(
        json.dumps({
            "data_contract_epoch": 0,
            "migration_set_id": "",
            "features": [],
            "planes": {"projection": 0},
        }),
        encoding="utf-8",
    )
    state = read_local_state(data)
    assert not state.unreadable
    assert state.planes["root_config"] == 2
    assert state.planes["persona_db"] == 1
    assert state.planes["maintenance"] == 1
    assert state.planes["rag"] == 0


def test_per_database_versions_are_not_hidden_by_plane_max(tmp_path):
    data = tmp_path / "instance"
    (data / "memory" / "one").mkdir(parents=True)
    (data / "memory" / "two").mkdir(parents=True)
    (data / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    with sqlite3.connect(data / "memory" / "one" / "one.db") as conn:
        conn.execute("PRAGMA user_version = 1")
    with sqlite3.connect(data / "memory" / "two" / "two.db") as conn:
        conn.execute("PRAGMA user_version = 0")
    state = read_local_state(data)
    assert state.planes["persona_db"] == 1
    result = evaluate(parse_manifest(MANIFEST), state)
    assert result["compatible"] is False
    assert result["code"] == "migration_required"
    assert "two.db" in result["message"]


def test_compat_state_cannot_override_measured_versions(tmp_path):
    data = tmp_path / "instance"
    (data / "memory" / "bot").mkdir(parents=True)
    (data / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    with sqlite3.connect(data / "memory" / "bot" / "bot.db") as conn:
        conn.execute("PRAGMA user_version = 1")
    (data / "compatibility_state.json").write_text(
        json.dumps({"state_format": 1, "planes": {"persona_db": 0}}),
        encoding="utf-8",
    )
    state = read_local_state(data)
    assert state.unreadable
    result = evaluate(parse_manifest(MANIFEST), state)
    assert result["code"] == "data_unreadable"


def test_compat_phase_blocks_normal_update(tmp_path):
    data = tmp_path / "instance"
    data.mkdir()
    (data / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    (data / "compatibility_state.json").write_text(
        json.dumps({"state_format": 1, "phase": "running"}),
        encoding="utf-8",
    )
    state = read_local_state(data)
    result = evaluate(parse_manifest(MANIFEST), state)
    assert result["code"] == "migration_in_progress"


def test_sqlite_probe_never_creates_missing_file(tmp_path):
    from core.compat import _sqlite_probe

    data = tmp_path / "instance"
    data.mkdir()
    missing = data / "ghost.db"
    probe = _sqlite_probe(missing, "persona_db", data, [])
    assert probe.error == "missing"
    assert not missing.exists()
