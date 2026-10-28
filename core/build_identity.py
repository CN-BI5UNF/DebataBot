"""构建身份：区分源码树、测试进程与线上已加载代码。

WebUI 是无构建步骤的原生 ES module。升级/打补丁后，静态文件可能已经来自
新磁盘，而 Python 进程仍在运行旧模块；只看 ``git HEAD`` 会误导。这里在进程
启动时捕获一次身份，之后 API 返回的就是**该进程实际加载时**的源码状态。

边界：只读取源码/文档/测试的路径与内容，不读取数据目录、日志、密钥或聊天。
"""

from __future__ import annotations

import hashlib
import logging
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".css", ".html", ".htm",
    ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".cfg", ".ini",
    ".sh", ".service", ".in", ".example",
}
# 明确的源码/测试/文档/部署目录白名单；不靠黑名单扫描整个项目。
_SOURCE_ROOTS = {
    "adapters", "agents", "app_config", "core", "karen", "deploy",
    "docs", "features", "mind", "plugins", "providers", "releases",
    "scripts", "tests", "tools", "utils", "webui",
}
_ROOT_FILES = {
    "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE",
    "README.md", "README_EN.md", "compatibility.json", "main.py",
    "main_cli.py", "pyproject.toml", "service_cli.py", "uv.lock",
    "webui_server.py",
}
_EXCLUDED_DIR_PARTS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "data", "logs",
    "cache", "tmp", "models", "workspace", "memory", "vector",
    "config_backups", "maintenance", "instance", "instances",
    "personas", "build",
}
# 不进入源码指纹的敏感/本地文件；manifest 本身也不列这些路径。
_EXCLUDED_NAMES = {
    ".env", "webui_token.txt", "secrets.enc", "secrets.meta",
    "rsa_private.pem", "rsa_public.pem", "update_state.json",
    ".env.local", ".env.production",
}
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_FILES = 20_000
_CHUNK = 256 * 1024

_loaded: dict[str, Any] | None = None
_loaded_lock = threading.Lock()


def _run_git(project_root: Path, *args: str, timeout: float = 8.0) -> str:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(project_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _is_source_path(relative: Path) -> bool:
    if not relative.parts or relative.is_absolute():
        return False
    if any(part in _EXCLUDED_DIR_PARTS or part.startswith(".") for part in relative.parts[:-1]):
        return False
    if relative.name in _EXCLUDED_NAMES or relative.name.startswith(".env"):
        return False
    if len(relative.parts) == 1:
        return relative.name in _ROOT_FILES
    if relative.parts[0] not in _SOURCE_ROOTS:
        return False
    return relative.suffix.lower() in _SOURCE_SUFFIXES


def _source_files(project_root: Path) -> list[Path]:
    """优先使用 git 清单（含未跟踪但未被 ignore 的源码）；非 git 时扫描。"""
    listed = _run_git(
        project_root,
        "ls-files", "-z", "--cached", "--others", "--exclude-standard",
    )
    if listed:
        candidates = [Path(item) for item in listed.split("\0") if item]
    else:
        candidates = []
        try:
            for path in project_root.rglob("*"):
                if path.is_file():
                    candidates.append(path.relative_to(project_root))
        except OSError:
            return []
    result: list[Path] = []
    seen: set[Path] = set()
    root = project_root.resolve()
    for relative in candidates:
        if relative in seen:
            continue
        seen.add(relative)
        if not _is_source_path(relative):
            continue
        path = root / relative
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        result.append(relative)
        if len(result) >= _MAX_FILES:
            break
    return sorted(result, key=lambda item: item.as_posix())


def _read_source_file(path: Path) -> bytes:
    """仅在源码白名单内调用；测试可替换此桩验证没有越界读取。"""
    return path.read_bytes()


def _fingerprint_files(project_root: Path, files: list[Path]) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    total = 0
    root = project_root.resolve()
    for relative in files:
        path = root / relative
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SOURCE_BYTES:
                continue
            # 再次确认没有 symlink/root 逃逸；读之前做，避免打开被替换的路径。
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        try:
            data = _read_source_file(path)
        except OSError:
            continue
        if len(data) > _MAX_SOURCE_BYTES:
            continue
        digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        count += 1
        total += len(data)
    return {
        "source_bundle_sha256": digest.hexdigest(),
        "source_file_count": count,
        "source_bytes": total,
    }


def _wip_paths(project_root: Path) -> list[str]:
    """只返回受控源码的 WIP 相对路径，不输出数据文件路径。"""
    status = _run_git(
        project_root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    paths: list[str] = []
    root = project_root.resolve()
    for chunk in status.split("\0"):
        if not chunk or len(chunk) < 4:
            continue
        raw = chunk[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        try:
            relative = Path(raw)
        except (OSError, ValueError):
            continue
        if not (_is_source_path(relative) or relative.name == "compatibility.json"):
            continue
        path = root / relative
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        paths.append(relative.as_posix())
    return sorted(set(paths))


def compute_build_identity(project_root: Path) -> dict[str, Any]:
    """计算源码身份；不把未跟踪文件正文或生产数据放进返回值。"""
    root = Path(project_root).resolve()
    files = _source_files(root)
    fingerprint = _fingerprint_files(root, files)
    head = _run_git(root, "rev-parse", "HEAD").strip()
    branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    status = _run_git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    wip_paths = _wip_paths(root)
    try:
        from app_config.versioning import read_pyproject_version

        app_version = read_pyproject_version(root)
    except Exception:  # noqa: BLE001
        app_version = ""
    return {
        "identity_kind": "startup_source_snapshot",
        "semantics": (
            "启动时磁盘源码/测试/文档快照；不是所有已加载模块字节的证明，"
            "也不是代码签名或可信来源认证"
        ),
        "base_commit": head,
        "branch": branch,
        "dirty": bool(status),
        "wip_file_count": len(wip_paths),
        "wip_paths": wip_paths,
        "worktree_sha256": hashlib.sha256(
            status.encode("utf-8", "surrogateescape")
        ).hexdigest(),
        **fingerprint,
        "app_version": app_version,
        "captured_at": time.time(),
        "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def capture_build_identity(project_root: Path) -> dict[str, Any]:
    """进程启动时调用一次；后续 API 始终返回该快照。"""
    global _loaded
    with _loaded_lock:
        if _loaded is None:
            _loaded = compute_build_identity(project_root)
            logger.info(
                "loaded build %s%s (%s files, dirty=%s)",
                (_loaded.get("base_commit") or "unknown")[:12],
                " +WIP" if _loaded.get("dirty") else "",
                _loaded.get("source_file_count", 0),
                bool(_loaded.get("dirty")),
            )
        return dict(_loaded)


def loaded_build_identity() -> dict[str, Any] | None:
    with _loaded_lock:
        return dict(_loaded) if _loaded is not None else None


def reset_build_identity_for_tests() -> None:
    global _loaded
    with _loaded_lock:
        _loaded = None


__all__ = [
    "capture_build_identity",
    "compute_build_identity",
    "loaded_build_identity",
    "reset_build_identity_for_tests",
]
