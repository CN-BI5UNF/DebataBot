"""L00-PARITY：core 与独立 launcher 对同一状态给出一致 code。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from core.compat import (
    CompatError,
    evaluate,
    parse_manifest,
    read_local_state,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (ROOT / "compatibility.json").read_text(encoding="utf-8")


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "karen_trusted_launcher_parity",
        ROOT / "deploy" / "trusted_launcher.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(mutate=None) -> dict:
    data = json.loads(MANIFEST)
    if mutate:
        mutate(data)
    return data


def _data(tmp_path: Path, *, config: bool = True, memory: dict[str, int] | None = None,
          maintenance: int | None = None, vector: int | None = None,
          state: dict | None = None) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True)
    if config:
        (data / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    for persona, version in (memory or {}).items():
        root = data / "memory" / persona
        root.mkdir(parents=True)
        with sqlite3.connect(root / "chat.db") as conn:
            conn.execute(f"PRAGMA user_version={version}")
    if vector is not None:
        root = data / "vector" / "rag"
        root.mkdir(parents=True)
        with sqlite3.connect(root / "rag.sqlite3") as conn:
            conn.execute(f"PRAGMA user_version={vector}")
    if maintenance is not None:
        with sqlite3.connect(data / "maintenance.sqlite3") as conn:
            conn.execute(f"PRAGMA user_version={maintenance}")
    if state is not None:
        (data / "compatibility_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
    return data


def _codes(manifest: dict, data: Path, launcher) -> tuple[str, str]:
    try:
        core_manifest = parse_manifest(json.dumps(manifest))
    except CompatError as exc:
        core_code = exc.code
    else:
        core_code = evaluate(
            core_manifest, read_local_state(data)
        )["code"]
    validated, error = launcher._validate_manifest(manifest)
    assert validated is not None and error is None
    launcher_result = launcher.evaluate(
        validated,
        launcher.read_local_state(data),
    )
    return core_code, launcher_result["code"]


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(memory={"one": 1}, maintenance=1, vector=0), "compatible"),
        (dict(memory={"one": 0}, maintenance=1), "migration_required"),
        (dict(memory={"one": 3}, maintenance=1), "unsupported_data_contract"),
        (dict(config=False, memory={"one": 1}), "recovery_required"),
        (dict(config=False), "setup_required"),
        (dict(memory={"one": 1}, maintenance=1,
              state={"state_format": 1, "phase": "running"}), "migration_in_progress"),
        (dict(memory={"one": 1}, maintenance=1,
              state={"state_format": 1, "data_contract_epoch": 1}),
         "data_contract_too_new"),
        (dict(memory={"one": 1}, maintenance=1,
              state={"state_format": 1, "features": []}), "compatible"),
    ],
)
def test_core_and_launcher_have_same_code(tmp_path, kwargs, expected):
    launcher = _launcher()
    data = _data(tmp_path, **kwargs)
    manifest = _manifest()
    core_code, launcher_code = _codes(manifest, data, launcher)
    assert core_code == expected
    assert launcher_code == expected


def test_target_above_write_is_manifest_invalid_on_both(tmp_path):
    launcher = _launcher()
    manifest = _manifest(
        lambda item: item["planes"]["persona_db"].update(
            supported_write=1, target_version=2
        )
    )
    with pytest.raises(CompatError) as exc:
        parse_manifest(json.dumps(manifest))
    assert exc.value.code == "manifest_invalid"
    validated, error = launcher._validate_manifest(manifest)
    assert validated is None
    assert "target_version" in error


def test_migration_set_and_features_align(tmp_path):
    launcher = _launcher()
    data = _data(tmp_path, memory={"one": 1}, maintenance=1)
    manifest = _manifest(lambda item: item.update(required_features=["stable_seq"]))
    assert _codes(manifest, data, launcher) == ("feature_missing", "feature_missing")

    manifest = _manifest(lambda item: item.update(migration_set_id="M001"))
    assert _codes(manifest, data, launcher) == (
        "migration_required", "migration_required"
    )

    data2 = _data(tmp_path / "second", memory={"one": 1}, maintenance=1,
                  state={"state_format": 1, "migration_set_id": "M001"})
    assert _codes(manifest, data2, launcher) == ("compatible", "compatible")
