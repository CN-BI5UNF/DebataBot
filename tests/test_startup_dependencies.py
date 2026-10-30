"""Launch-time dependency preflight without touching the real environment."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import utils.startup_dependencies as deps


def test_static_fallback_matches_pyproject_base_dependencies():
    root = Path(__file__).resolve().parents[1]
    pyproject = root / "pyproject.toml"
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        pytest.skip("tomllib requires Python 3.11+")

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared = tuple(
        str(item).strip()
        for item in data["project"]["dependencies"]
        if str(item).strip()
    )
    assert deps.BASE_DEPENDENCIES == declared


def test_load_declared_dependencies_reads_pyproject_and_falls_back(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["httpx>=0.27", "PyYAML"]\n',
        encoding="utf-8",
    )
    assert deps.load_declared_dependencies(tmp_path) == ("httpx>=0.27", "PyYAML")

    missing = tmp_path / "empty"
    missing.mkdir()
    assert deps.load_declared_dependencies(missing) == deps.BASE_DEPENDENCIES

    (missing / "pyproject.toml").write_text(
        '[project]\nname = "reduced-test-tree"\n', encoding="utf-8"
    )
    assert deps.load_declared_dependencies(missing) == ()


def test_requirement_parsing_and_missing_detection(monkeypatch):
    assert deps._requirement_name("uvicorn[standard]>=0.27") == "uvicorn"
    assert deps._requirement_name("PyYAML>=6.0.1 ; python_version >= '3.10'") == "pyyaml"

    versions = {"httpx": "0.27.0", "anthropic": "0.115.0", "uvicorn": "0.30.0"}

    def fake_version(name: str) -> str:
        import importlib.metadata

        try:
            return versions[name]
        except KeyError:
            raise importlib.metadata.PackageNotFoundError(name) from None

    monkeypatch.setattr(deps.importlib.metadata, "version", fake_version)
    missing = deps.find_missing_dependencies(
        [
            "httpx>=0.27",
            "anthropic==0.116.0",
            "uvicorn[standard]>=0.27",
            "not-installed>=1",
        ]
    )
    assert missing == ("anthropic==0.116.0", "not-installed>=1")


def test_check_installs_once_and_rechecks(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["httpx>=0.27"]\n', encoding="utf-8"
    )
    missing_calls = iter([("httpx>=0.27",), ("httpx>=0.27",), ()])
    monkeypatch.setattr(
        deps, "find_missing_dependencies", lambda _reqs: next(missing_calls)
    )
    monkeypatch.setattr(
        deps, "_install_candidates", lambda *_args, **_kwargs: [("fake", ["uv", "pip"])]
    )
    calls: list[tuple[list[str], Path]] = []

    def runner(command, root):
        calls.append((list(command), Path(root)))
        return deps.InstallOutcome(returncode=0)

    result = deps.run_startup_dependency_check(
        tmp_path, runner=runner, environ={}
    )
    assert result.attempted is True
    assert result.installed is True
    assert result.missing_before == ("httpx>=0.27",)
    assert result.missing_after == ()
    assert calls == [(["uv", "pip"], tmp_path)]


def test_check_failure_is_reported_without_raising(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["httpx>=0.27"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        deps,
        "find_missing_dependencies",
        lambda _reqs: ("httpx>=0.27",),
    )
    monkeypatch.setattr(
        deps, "_install_candidates", lambda *_args, **_kwargs: [("fake", ["uv"])]
    )

    result = deps.run_startup_dependency_check(
        tmp_path,
        runner=lambda _command, _root: deps.InstallOutcome(
            returncode=1, output_tail="network down"
        ),
        environ={},
    )
    assert result.attempted is True
    assert result.installed is False
    assert result.missing_after == ("httpx>=0.27",)
    assert "network down" in result.error


def test_check_honours_disable_switches(monkeypatch, tmp_path):
    called: list[object] = []
    monkeypatch.setattr(
        deps, "find_missing_dependencies", lambda reqs: called.append(reqs) or ()
    )
    skipped = deps.run_startup_dependency_check(
        tmp_path, environ={"KAREN_DISABLE_DEPENDENCY_CHECK": "1"}
    )
    assert skipped.skipped is True
    assert called == []

    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["httpx>=0.27"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        deps, "find_missing_dependencies", lambda _reqs: ("httpx>=0.27",)
    )
    checked = deps.run_startup_dependency_check(
        tmp_path, environ={"KAREN_DISABLE_DEPENDENCY_INSTALL": "1"}
    )
    assert checked.attempted is False
    assert checked.missing_after == ("httpx>=0.27",)


def test_install_command_uses_current_interpreter_and_is_bounded(tmp_path):
    outcome = deps.run_install_command(
        [sys.executable, "-c", "print('installed ok')"],
        tmp_path,
        timeout=30,
    )
    assert outcome.returncode == 0
    assert "installed ok" in outcome.output_tail


def test_main_runs_preflight_before_heavy_startup(monkeypatch):
    import main as app_main

    calls: list[Path] = []
    monkeypatch.setattr(
        app_main,
        "run_startup_dependency_check",
        lambda root: calls.append(Path(root)),
    )

    class StopParsing(Exception):
        pass

    def stop(_argv=None):
        raise StopParsing

    monkeypatch.setattr(app_main, "parse_args", stop)
    with pytest.raises(StopParsing):
        app_main.main(["--no-gui"])
    assert calls == [Path(app_main.__file__).resolve().parent]


def test_main_service_commands_skip_preflight(monkeypatch):
    import main as app_main
    import service_cli

    calls: list[Path] = []
    monkeypatch.setattr(
        app_main,
        "run_startup_dependency_check",
        lambda root: calls.append(Path(root)),
    )
    monkeypatch.setattr(service_cli, "main", lambda _argv: 0)
    with pytest.raises(SystemExit) as exit_info:
        app_main.main(["container", "status"])
    assert exit_info.value.code == 0
    assert calls == []


def test_no_installer_is_reported(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["httpx>=0.27"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        deps, "find_missing_dependencies", lambda _reqs: ("httpx>=0.27",)
    )
    monkeypatch.setattr(deps, "_install_candidates", lambda *_args, **_kwargs: [])
    result = deps.run_startup_dependency_check(tmp_path, environ={})
    assert result.error == "no_installer"
    assert result.missing_after == ("httpx>=0.27",)
