"""Launch-time dependency preflight (standard library only).

The service runs from a source tree and may be updated without the user
manually reinstalling packages. Before ``main.py`` imports the application
(and before a missing package can turn into an ImportError), this module checks
the *required* dependencies declared in ``pyproject.toml`` and, when anything is
missing or outside its version range, runs an editable install with the running
interpreter.

Design boundaries:
- Only base ``project.dependencies`` are installed. Optional/dev/local-model
  extras are never pulled in implicitly; they remain on-demand features.
- Commands are argv lists, never shell strings. The project root is trusted;
  no user/model-provided package names enter the command.
- Output is written to a temporary file and only a short sanitized tail is
  logged, so a noisy resolver cannot grow process memory.
- ``KAREN_DISABLE_DEPENDENCY_CHECK=1`` turns the check off;
  ``KAREN_DISABLE_DEPENDENCY_INSTALL=1`` checks and logs but never installs.
- This module must not import app_config, pydantic, yaml, or any other
  third-party package: it is the bootstrap that makes those imports possible.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import importlib.util
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Fallback used when pyproject.toml is unavailable (e.g. a packaged source
# tree that only kept runtime modules). Keep this in sync with
# ``[project].dependencies``; tests assert that it matches the real file.
BASE_DEPENDENCIES: tuple[str, ...] = (
    "websockets>=12.0",
    "httpx>=0.27.0",
    "aiofiles>=23.2.0",
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.27.0",
    "python-multipart>=0.0.9",
    "openai>=1.50.0",
    "anthropic==0.116.0",
    "google-genai>=0.3.0",
    "pydantic>=2.6.0",
    "pydantic-settings>=2.2.0",
    "pyyaml>=6.0.1",
    "orjson>=3.10.0",
    "tiktoken>=0.7.0",
    "cryptography>=42.0.0",
    "keyring>=24.0.0",
    "ddgs>=8.0.0",
    "edge-tts>=7.2.0",
    "platformdirs>=4.2.0",
    "python-dotenv>=1.0.0",
    "pypdf>=4.0.0",
    "pillow>=10.4.0",
)

_INSTALL_TIMEOUT_SECONDS = 600
_OUTPUT_TAIL_BYTES = 4000
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDENTIAL_URL_RE = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*=\s*([^\s&]+)"
)
_REQUIREMENT_SPLIT_RE = re.compile(r"[<>=!~;\s\[]")


@dataclass(frozen=True)
class InstallOutcome:
    returncode: int
    output_tail: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class StartupDependencyResult:
    skipped: bool = False
    requirements: int = 0
    missing_before: tuple[str, ...] = ()
    missing_after: tuple[str, ...] = ()
    attempted: bool = False
    installed: bool = False
    command: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.skipped or not self.missing_after


def _env_truthy(name: str, environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _requirement_name(requirement: str) -> str:
    """Extract a distribution name from a PEP 508 requirement string."""
    text = str(requirement or "").strip()
    if not text or text.startswith("#"):
        return ""
    text = text.split(";", 1)[0].strip()
    text = text.split("[", 1)[0].strip()
    name = _REQUIREMENT_SPLIT_RE.split(text, maxsplit=1)[0].strip()
    return _normalize_name(name)


def load_declared_dependencies(project_root: Path) -> tuple[str, ...]:
    """Read base dependencies from pyproject.toml; fall back to the static list.

    A present pyproject with an explicitly empty dependency list means the
    caller is in a reduced/test source tree; in that case we must not install
    the full real-project dependency set into it.
    """
    pyproject = Path(project_root) / "pyproject.toml"
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - Python 3.10 compatibility
        tomllib = None  # type: ignore[assignment]
    if tomllib is not None:
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project")
            if isinstance(project, dict):
                declared = project.get("dependencies")
                if isinstance(declared, list):
                    return tuple(
                        str(item).strip()
                        for item in declared
                        if str(item).strip()
                    )
                # A minimal pyproject used by tests/embedders should not cause
                # the full app dependency set to be installed into it.
                return ()
        except FileNotFoundError:
            return BASE_DEPENDENCIES
        except (OSError, ValueError, TypeError):
            logger.debug("读取 pyproject.toml 失败，使用内置依赖清单", exc_info=True)
            return BASE_DEPENDENCIES
    return BASE_DEPENDENCIES


def _installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - malformed metadata must not crash startup
        return None


def _requirement_satisfied(raw: str, installed: str) -> bool:
    try:
        from packaging.requirements import Requirement
        from packaging.version import InvalidVersion, Version

        requirement = Requirement(raw)
        if not requirement.specifier:
            return True
        try:
            version = Version(installed)
        except InvalidVersion:
            return True
        return requirement.specifier.contains(version, prereleases=True)
    except Exception:  # noqa: BLE001 - packaging missing/odd metadata
        # Presence is a safe fallback: do not repeatedly install a package
        # whose version cannot be compared locally.
        return True


def find_missing_dependencies(
    requirements: Iterable[str],
) -> tuple[str, ...]:
    """Return requirements that are absent or outside their version range."""
    missing: list[str] = []
    seen: set[str] = set()
    for raw in requirements:
        name = _requirement_name(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        installed = _installed_version(name)
        if installed is None:
            missing.append(str(raw).strip())
            continue
        if not _requirement_satisfied(str(raw), installed):
            missing.append(str(raw).strip())
    return tuple(missing)


def _find_uv(environ: dict[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    candidates = [
        source.get("KAREN_UV_BIN", ""),
        shutil.which("uv", path=source.get("PATH")),
    ]
    home = Path(source.get("HOME") or Path.home())
    candidates.extend(
        [
            str(home / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")),
            str(home / ".cargo" / "bin" / ("uv.exe" if os.name == "nt" else "uv")),
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _pip_available() -> bool:
    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):
        return False


def _install_candidates(
    project_root: Path, environ: dict[str, str] | None = None
) -> list[tuple[str, list[str]]]:
    root = str(Path(project_root).resolve())
    uv = _find_uv(environ)
    candidates: list[tuple[str, list[str]]] = []
    if uv:
        candidates.append(
            (
                "uv",
                [uv, "pip", "install", "--python", sys.executable, "-e", root],
            )
        )
    if _pip_available():
        candidates.append(
            (
                "pip",
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "-e",
                    root,
                ],
            )
        )
    return candidates


def _sanitize_output(text: str) -> str:
    cleaned = _ANSI_RE.sub("", str(text or ""))
    cleaned = _CREDENTIAL_URL_RE.sub(r"\1***@", cleaned)
    cleaned = _SECRET_ASSIGN_RE.sub(r"\1=***", cleaned)
    return cleaned.strip()


def _read_tail(file_obj, limit: int = _OUTPUT_TAIL_BYTES) -> str:
    try:
        file_obj.flush()
        file_obj.seek(0, os.SEEK_END)
        size = file_obj.tell()
        file_obj.seek(max(0, size - limit))
        raw = file_obj.read(limit)
    except OSError:
        return ""
    return _sanitize_output(raw.decode("utf-8", errors="replace"))


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            proc.kill()


def run_install_command(
    command: list[str],
    project_root: Path,
    *,
    timeout: float = _INSTALL_TIMEOUT_SECONDS,
    environ: dict[str, str] | None = None,
) -> InstallOutcome:
    """Run one installer with bounded log storage and no shell."""
    env = dict(os.environ if environ is None else environ)
    env.update(
        {
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONUNBUFFERED": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "UV_NO_PROGRESS": "1",
        }
    )
    env.pop("PIP_REQUIRE_VIRTUALENV", None)
    with tempfile.TemporaryFile(mode="w+b") as log_file:
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                cwd=str(project_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            return InstallOutcome(returncode=-1, output_tail=str(exc))
        timed_out = False
        try:
            proc.wait(timeout=max(1.0, float(timeout)))
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        tail = _read_tail(log_file)
        return InstallOutcome(
            returncode=int(proc.returncode if proc.returncode is not None else -1),
            output_tail=tail,
            timed_out=timed_out,
        )


@contextlib.contextmanager
def _install_lock():
    """Best-effort cross-process lock so two starts do not install at once."""
    path = Path(tempfile.gettempdir()) / "karen-dependency-install.lock"
    handle = None
    try:
        handle = open(path, "a+b")  # noqa: SIM115 - held for the context
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (OSError, ImportError):
        if handle is not None:
            handle.close()
            handle = None
        yield
        return
    try:
        yield
    finally:
        if handle is not None:
            try:
                if os.name == "nt":  # pragma: no cover
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


InstallRunner = Callable[[list[str], Path], InstallOutcome]


def _run_candidates(
    candidates: list[tuple[str, list[str]]],
    project_root: Path,
    runner: InstallRunner,
) -> tuple[bool, str, str]:
    last_error = ""
    for label, command in candidates:
        started = time.monotonic()
        logger.warning("正在安装启动依赖（%s，超时 %ss）", label, _INSTALL_TIMEOUT_SECONDS)
        try:
            outcome = runner(command, project_root)
        except Exception as exc:  # noqa: BLE001 - bootstrap must not crash
            last_error = f"{label}: {exc}"
            logger.warning("启动依赖安装器异常（%s）：%s", label, exc)
            continue
        elapsed = time.monotonic() - started
        if outcome.returncode == 0:
            logger.info("启动依赖安装完成（%s，%.1fs）", label, elapsed)
            return True, " ".join(command), ""
        last_error = (
            f"{label} exit={outcome.returncode}"
            + ("（超时）" if outcome.timed_out else "")
            + (f"：{outcome.output_tail}" if outcome.output_tail else "")
        )
        logger.warning(
            "启动依赖安装失败（%s，exit=%s，%.1fs）：%s",
            label,
            outcome.returncode,
            elapsed,
            outcome.output_tail or "无输出",
        )
        if outcome.timed_out:
            # A timeout is usually network/resolver trouble; trying a second
            # installer immediately would just double the wait.
            break
    return False, "", last_error


def run_startup_dependency_check(
    project_root: Path,
    *,
    install: bool = True,
    runner: InstallRunner | None = None,
    environ: dict[str, str] | None = None,
) -> StartupDependencyResult:
    """Check base dependencies and optionally install them before app imports.

    Never raises for ordinary network/resolver/installer failures; the caller
    continues to import so a partially installed environment still gets the
    normal traceback, while logs explain what failed.
    """
    source_env = dict(os.environ if environ is None else environ)
    if _env_truthy("KAREN_DISABLE_DEPENDENCY_CHECK", source_env):
        logger.debug("启动依赖检查已通过环境变量关闭")
        return StartupDependencyResult(skipped=True)

    requirements = load_declared_dependencies(project_root)
    if not requirements:
        return StartupDependencyResult(skipped=True)
    missing_before = find_missing_dependencies(requirements)
    if not missing_before:
        logger.debug("启动依赖检查通过（%d 项）", len(requirements))
        return StartupDependencyResult(
            requirements=len(requirements),
            missing_before=(),
            missing_after=(),
        )

    logger.warning(
        "启动依赖检查发现缺失或版本不符：%s", ", ".join(missing_before)
    )
    if not install or _env_truthy("KAREN_DISABLE_DEPENDENCY_INSTALL", source_env):
        logger.warning(
            "不会自动安装（已关闭自动安装）；请手动执行 uv pip install -e . "
            "或 python -m pip install -e ."
        )
        return StartupDependencyResult(
            requirements=len(requirements),
            missing_before=missing_before,
            missing_after=missing_before,
        )

    candidates = _install_candidates(project_root, source_env)
    if not candidates:
        logger.warning(
            "当前解释器没有 pip，也未找到 uv；无法自动安装缺失依赖：%s",
            ", ".join(missing_before),
        )
        return StartupDependencyResult(
            requirements=len(requirements),
            missing_before=missing_before,
            missing_after=missing_before,
            error="no_installer",
        )

    with _install_lock():
        # Another process may have finished the install while we waited.
        missing_now = find_missing_dependencies(requirements)
        if not missing_now:
            return StartupDependencyResult(
                requirements=len(requirements),
                missing_before=missing_before,
                missing_after=(),
                installed=True,
            )
        used_runner: InstallRunner = runner or (
            lambda command, root: run_install_command(
                command, root, environ=source_env
            )
        )
        installed, command, error = _run_candidates(
            candidates, Path(project_root), used_runner
        )
        missing_after = find_missing_dependencies(requirements)
        if installed and missing_after:
            error = "安装命令成功，但仍有依赖未满足：" + ", ".join(missing_after)
            logger.warning("%s", error)
        elif installed:
            logger.warning("启动依赖已补齐；本次启动继续使用当前已加载模块，后续无需再装")
        else:
            logger.warning(
                "自动安装依赖失败；应用将继续启动，功能可能缺失。错误：%s", error
            )
        return StartupDependencyResult(
            requirements=len(requirements),
            missing_before=missing_before,
            missing_after=missing_after,
            attempted=True,
            installed=installed,
            command=command,
            error=error,
        )


__all__ = [
    "BASE_DEPENDENCIES",
    "InstallOutcome",
    "StartupDependencyResult",
    "find_missing_dependencies",
    "load_declared_dependencies",
    "run_install_command",
    "run_startup_dependency_check",
]
