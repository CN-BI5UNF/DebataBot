"""安全的代码更新器（Releases / tag 版本管理）。

边界：
    - 只更新 Karen 源码仓库；用户数据目录（persona/聊天/媒体/配置）不在
      git 工作树内，更新过程不读取、不删除、不覆盖它。
    - 版本来自远端 tag（与 GitHub Releases 使用同一组 tag）；每个版本可以在
      仓库的 ``releases/<tag>.md`` 中提供更新日志。
    - 只允许在干净工作树上切换；当前提交先备份到 ``refs/backups/update/*``，
      依赖文件变化拒绝自动应用。
    - 镜像只允许 https（测试可显式放开本地 file remote），且必须指向同一仓库
      路径；不接受账号密码、file:// 或内网地址（生产模式）。
    - 自动检查状态保存在数据目录的 ``update_state.json``（0600），不保存聊天正文。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from core.compat import (
    MANIFEST_FILENAME,
    CompatError,
    evaluate,
    invalid_result,
    legacy_result,
    parse_manifest,
    public_compat,
    read_local_state,
)

logger = logging.getLogger(__name__)

UPDATE_REPO_URL = "https://github.com/CN-BI5UNF/KarenBot.git"
UPDATE_REPO_OWNER = "CN-BI5UNF"
UPDATE_REPO_NAME = "KarenBot"
UPDATE_REF = "refactor/main"
UPDATE_LOCAL_BRANCH = "refactor/main"
UPDATE_TRACKING_REF = "refs/remotes/update-target/refactor-main"
UPDATE_TAGS_REF = "refs/update-target/tags"
UPDATE_CONFIRM_TTL_SECONDS = 300
DEFAULT_CHECK_INTERVAL_DAYS = 9
RETRY_INTERVAL_SECONDS = 6 * 60 * 60
RELEASES_CACHE_SECONDS = 300
MAX_RELEASES = 40
RELEASE_NOTES_DIR = "releases"
RELEASE_NOTES_MAX_CHARS = 20_000
APP_RELEASE_LABEL = "开发版"
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")

# 预设镜像。只做“把 github.com 前缀替换成加速源”的常见代理；它们都是第三方，
# 是否可用/可信取决于用户网络环境，因此 UI 必须保留明确警告。
MIRROR_PRESETS = [
    {
        "id": "official",
        "label": "GitHub 官方（直连）",
        "value": "",
        "note": "不经过第三方；私有仓库需要本机已配置 git 凭据。",
    },
    {
        "id": "ghproxy",
        "label": "gh-proxy.com",
        "value": "https://gh-proxy.com/",
        "note": "第三方 GitHub 加速源；只在你信任它时使用。",
    },
    {
        "id": "ghfast",
        "label": "ghfast.top",
        "value": "https://ghfast.top/",
        "note": "第三方 GitHub 加速源；只在你信任它时使用。",
    },
    {
        "id": "ghproxy_net",
        "label": "ghproxy.net",
        "value": "https://ghproxy.net/",
        "note": "第三方 GitHub 加速源；只在你信任它时使用。",
    },
]


class UpdateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def mirror_presets() -> list[dict[str, str]]:
    """返回镜像预设副本，避免调用方改到模块级常量。"""
    return [dict(item) for item in MIRROR_PRESETS]


def _safe_tag(tag: str) -> bool:
    """tag 会参与 refspec 和文件路径，拒绝可疑字符与路径穿越。"""
    return bool(
        _TAG_RE.match(tag)
        and ".." not in tag
        and not tag.endswith("/")
        and not tag.endswith(".lock")
        and "\\" not in tag
    )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDENTIAL_URL_RE = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*=\s*([^\s&]+)"
)


def _sanitize_install_output(text: str) -> str:
    """Keep install logs useful without leaking credential-bearing URLs."""
    cleaned = _ANSI_RE.sub("", str(text or ""))
    cleaned = _CREDENTIAL_URL_RE.sub(r"\1***@", cleaned)
    cleaned = _SECRET_ASSIGN_RE.sub(r"\1=***", cleaned)
    return cleaned.strip()


def resolve_update_repo_url(mirror: str = "") -> str:
    """解析并校验镜像；只允许同一 KarenBot 仓库。"""
    mirror = str(mirror or "").strip()
    if not mirror:
        url = UPDATE_REPO_URL
    elif "{url}" in mirror:
        url = mirror.replace("{url}", UPDATE_REPO_URL)
    else:
        candidate = mirror.rstrip("/")
        candidate_path = unquote(urlsplit(candidate).path).rstrip("/").lower()
        if candidate_path.endswith(f"/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}".lower()):
            # 用户填的是完整仓库 URL（可省略 .git）；补齐而不是当镜像前缀。
            url = candidate + ".git"
        elif candidate.lower().endswith(".git"):
            url = candidate
        else:
            url = candidate + "/" + UPDATE_REPO_URL
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UpdateError("mirror_scheme", "更新镜像必须使用 https URL")
    if parts.username or parts.password:
        raise UpdateError("mirror_credentials", "更新镜像 URL 不能包含账号或密码")
    if not parts.hostname:
        raise UpdateError("mirror_host", "更新镜像缺少主机名")
    host = parts.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise UpdateError("mirror_private", "更新镜像不能指向本机或 .local 域名")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    ):
        raise UpdateError("mirror_private", "更新镜像不能指向内网/回环地址")
    path = unquote(parts.path).rstrip("/").lower()
    expected = f"/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}.git".lower()
    if not path.endswith(expected):
        raise UpdateError(
            "mirror_repo_mismatch",
            f"更新镜像必须指向 {UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME} 仓库",
        )
    if parts.query or parts.fragment:
        raise UpdateError("mirror_query", "更新镜像 URL 不能包含查询参数或 fragment")
    return url.rstrip("/")


def _state_defaults() -> dict:
    return {
        "suppress_badge": False,
        "suppressed_at": None,
        "last_attempt_at": None,
        "last_check_at": None,
        "last_error": "",
        "next_check_at": None,
        "available": None,
        "checked_commit": None,
        "checked_target": None,
    }


class UpdateService:
    def __init__(
        self,
        project_root: Path,
        data_dir: Path,
        *,
        mirror: str = "",
        timeout_seconds: int = 60,
        check_interval_days: int = DEFAULT_CHECK_INTERVAL_DAYS,
        restart_callback=None,
        allow_local_remote: bool = False,
        auto_install_dependencies: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.mirror = str(mirror or "").strip()
        self.timeout_seconds = max(10, min(300, int(timeout_seconds)))
        self.check_interval_days = max(1, min(365, int(check_interval_days)))
        self.restart_callback = restart_callback
        self.allow_local_remote = bool(allow_local_remote)
        self.auto_install_dependencies = bool(auto_install_dependencies)
        self._lock = asyncio.Lock()
        self._state_io_lock = asyncio.Lock()
        self._plan: dict | None = None
        self._restart_tasks: set[asyncio.Task] = set()
        self._releases_cache: tuple[float, str, str, list[dict]] | None = None

    # ============================================================
    # 远程 / 状态
    # ============================================================

    def _remote_url(self) -> str:
        if self.allow_local_remote and self.mirror.startswith("file://"):
            return self.mirror
        return resolve_update_repo_url(self.mirror)

    def _state_path(self) -> Path:
        return self.data_dir / "update_state.json"

    def _read_state_sync(self) -> dict:
        path = self._state_path()
        state = _state_defaults()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return state
        except (OSError, json.JSONDecodeError):
            logger.warning("更新状态文件损坏，使用默认值：%s", path)
            return state
        if not isinstance(raw, dict):
            return state
        state.update({k: raw.get(k, v) for k, v in state.items()})
        state["suppress_badge"] = bool(state.get("suppress_badge"))
        return state

    async def _read_state(self) -> dict:
        return await asyncio.to_thread(self._read_state_sync)

    def _write_state_sync(self, state: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_path()
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    async def _write_state(self, state: dict) -> None:
        async with self._state_io_lock:
            await asyncio.to_thread(self._write_state_sync, state)

    async def available_snapshot(self, *, include_commit: bool = True) -> dict:
        """只读缓存状态，不访问网络；总览绿标/设置页用它。"""
        state = await self._read_state()
        release = state.get("available")
        current = ""
        if include_commit:
            try:
                current = (await self._git("rev-parse", "HEAD")).strip()
            except UpdateError:
                current = ""
        suppressed = bool(state.get("suppress_badge"))
        return {
            "version_label": APP_RELEASE_LABEL,
            "repo": UPDATE_REPO_URL,
            "target_ref": UPDATE_REF,
            "available": bool(release) and not suppressed,
            "release": release if isinstance(release, dict) else None,
            "suppressed": suppressed,
            "last_attempt_at": state.get("last_attempt_at"),
            "last_check_at": state.get("last_check_at"),
            "next_check_at": state.get("next_check_at"),
            "last_error": state.get("last_error") or "",
            "interval_days": self.check_interval_days,
            "current_commit": current,
            "current_commit_short": current[:10],
        }

    async def version_info(self) -> dict:
        """右下角版本占位：目前不发布正式版本号。"""
        current = ""
        try:
            current = (await self._git("rev-parse", "HEAD")).strip()
        except UpdateError:
            pass
        return {
            "version_label": APP_RELEASE_LABEL,
            "commit": current,
            "commit_short": current[:10],
            "repo": UPDATE_REPO_URL,
            "target_ref": UPDATE_REF,
        }

    async def set_suppress_badge(self, suppress: bool) -> dict:
        state = await self._read_state()
        state["suppress_badge"] = bool(suppress)
        state["suppressed_at"] = time.time() if suppress else None
        await self._write_state(state)
        return await self.available_snapshot()

    async def _record_success(self, result: dict) -> None:
        state = await self._read_state()
        now = time.time()
        available = result.get("available_release")
        state.update(
            {
                "last_attempt_at": now,
                "last_check_at": now,
                "last_error": "",
                "next_check_at": now + self.check_interval_days * 86400,
                "available": available if result.get("available") else None,
                "checked_commit": result.get("current_commit"),
                "checked_target": result.get("target_commit"),
            }
        )
        try:
            await self._write_state(state)
        except OSError:
            logger.warning("写入更新状态失败", exc_info=True)

    async def _record_failure(self, exc: UpdateError) -> None:
        state = await self._read_state()
        now = time.time()
        state.update(
            {
                "last_attempt_at": now,
                "last_error": f"{exc.code}: {exc.message}"[:500],
                "next_check_at": now + RETRY_INTERVAL_SECONDS,
            }
        )
        try:
            await self._write_state(state)
        except OSError:
            logger.warning("写入更新失败状态失败", exc_info=True)

    async def _record_applied(self, new_commit: str) -> None:
        state = await self._read_state()
        now = time.time()
        state.update(
            {
                "available": None,
                "last_attempt_at": now,
                "last_check_at": now,
                "last_error": "",
                "next_check_at": now + self.check_interval_days * 86400,
                "checked_commit": new_commit,
                "checked_target": new_commit,
            }
        )
        try:
            await self._write_state(state)
        except OSError:
            logger.warning("写入更新完成状态失败", exc_info=True)

    # ============================================================
    # 状态 / 版本列表
    # ============================================================

    async def status(self) -> dict:
        async with self._lock:
            return await self._status_unlocked()

    async def _status_unlocked(self) -> dict:
        current_sha = await self._git("rev-parse", "HEAD")
        branch = await self._git("branch", "--show-current")
        status_text = await self._git("status", "--porcelain=v1", "--untracked-files=normal")
        dirty_files = [line for line in status_text.splitlines() if line.strip()]
        data_inside = self._data_dir_inside_project()
        remote_url = ""
        remote_error = ""
        try:
            remote_url = self._remote_url()
        except UpdateError as exc:
            remote_error = exc.message
        local_main = await self._rev_parse(f"refs/heads/{UPDATE_LOCAL_BRANCH}")
        return {
            "repo": UPDATE_REPO_URL,
            "target_ref": UPDATE_REF,
            "target_branch": UPDATE_LOCAL_BRANCH,
            "current_branch": branch.strip() or "(detached)",
            "current_commit": current_sha.strip(),
            "current_commit_short": current_sha.strip()[:10],
            "local_target_commit": local_main,
            "mirror": self.mirror,
            "effective_remote": remote_url,
            "mirror_error": remote_error,
            "worktree_dirty": bool(dirty_files),
            "dirty_file_count": len(dirty_files),
            "data_dir": str(self.data_dir),
            "data_dir_inside_project": data_inside,
            "restart_supported": self.restart_callback is not None,
            "check_interval_days": self.check_interval_days,
            "version_label": APP_RELEASE_LABEL,
            "mirror_presets": mirror_presets(),
        }

    async def probe_mirrors(self, *, timeout_seconds: float = 8.0) -> dict:
        """只读测速：对 GitHub 官方和已配置镜像执行 ``git ls-remote``。

        不获取 tag 内容、不创建 plan、不写 update_state.json，也不切换源码。
        """
        timeout = max(1.0, min(30.0, float(timeout_seconds)))
        candidates = list(mirror_presets())
        if self.mirror and not any(
            str(item.get("value") or "") == self.mirror for item in candidates
        ):
            candidates.append(
                {
                    "id": "custom",
                    "label": "当前自定义镜像",
                    "value": self.mirror,
                    "note": "",
                }
            )

        async def probe(candidate: dict) -> dict:
            started = time.perf_counter()
            value = str(candidate.get("value") or "")
            try:
                if value.startswith("file://") and self.allow_local_remote:
                    url = value
                else:
                    url = resolve_update_repo_url(value)
                await self._git(
                    "ls-remote", "--tags", url, timeout=timeout
                )
                return {
                    "id": str(candidate.get("id") or ""),
                    "label": str(candidate.get("label") or ""),
                    "mirror": value,
                    "url": url,
                    "ok": True,
                    "latency_ms": int(
                        (time.perf_counter() - started) * 1000
                    ),
                    "error": "",
                    "error_code": "",
                }
            except UpdateError as exc:
                return {
                    "id": str(candidate.get("id") or ""),
                    "label": str(candidate.get("label") or ""),
                    "mirror": value,
                    "url": "",
                    "ok": False,
                    "latency_ms": int(
                        (time.perf_counter() - started) * 1000
                    ),
                    "error": exc.message,
                    "error_code": exc.code,
                }
            except Exception as exc:  # noqa: BLE001 - 单目标失败不影响其它目标
                return {
                    "id": str(candidate.get("id") or ""),
                    "label": str(candidate.get("label") or ""),
                    "mirror": value,
                    "url": "",
                    "ok": False,
                    "latency_ms": int(
                        (time.perf_counter() - started) * 1000
                    ),
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_code": "probe_failed",
                }

        results = await asyncio.gather(
            *(probe(candidate) for candidate in candidates)
        )
        return {
            "checked_at": time.time(),
            "timeout_seconds": timeout,
            "results": results,
        }

    async def releases(self, *, force: bool = False) -> dict:
        """列出远端 tag/Release 与更新日志；需要网络。"""
        async with self._lock:
            status = await self._status_unlocked()
            remote_url = self._remote_url()
            releases = await self._fetch_releases_unlocked(remote_url, force=force)
            return {
                "repo": UPDATE_REPO_URL,
                "target_ref": UPDATE_REF,
                "current_commit": status["current_commit"],
                "current_commit_short": status["current_commit_short"],
                "current_branch": status["current_branch"],
                "worktree_dirty": status["worktree_dirty"],
                "mirror": self.mirror,
                "version_label": APP_RELEASE_LABEL,
                "interval_days": self.check_interval_days,
                "releases": releases,
            }

    async def _local_compat_state(self):
        return await asyncio.to_thread(read_local_state, self.data_dir)

    async def _compat_for_target(self, target_sha: str, local_state=None) -> dict:
        """读取目标提交的 compatibility.json 并离线评估；不执行目标代码。"""
        local = local_state if local_state is not None else await self._local_compat_state()
        try:
            raw = await self._git(
                "show", f"{target_sha}:{MANIFEST_FILENAME}"
            )
        except UpdateError:
            return legacy_result(local)
        try:
            manifest = parse_manifest(raw)
        except CompatError as exc:
            return invalid_result(exc, local)
        return evaluate(manifest, local)

    async def _fetch_releases_unlocked(
        self, remote_url: str, *, force: bool = False
    ) -> list[dict]:
        current_sha = (await self._git("rev-parse", "HEAD")).strip()
        local_state = await self._local_compat_state()
        state_key = hashlib.sha256(
            json.dumps(
                local_state.snapshot(), ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        cached = self._releases_cache
        now = time.time()
        if (
            not force
            and cached is not None
            and cached[0] > now
            and cached[1] == current_sha
            and cached[2] == state_key
        ):
            return [dict(item) for item in cached[3]]

        await self._git(
            "fetch",
            "--prune",
            "--no-tags",
            "--force",
            remote_url,
            f"+refs/tags/*:{UPDATE_TAGS_REF}/*",
            timeout=self.timeout_seconds,
        )
        refs_text = await self._git(
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname)%09%(creatordate:iso-strict)",
            UPDATE_TAGS_REF,
        )
        releases: list[dict] = []
        for line in refs_text.splitlines():
            if not line.strip():
                continue
            refname, _, created = line.partition("\t")
            prefix = UPDATE_TAGS_REF + "/"
            if not refname.startswith(prefix):
                continue
            tag = refname[len(prefix):]
            if not tag or not _safe_tag(tag):
                logger.warning("忽略不安全的更新 tag：%r", tag)
                continue
            commit = await self._rev_parse(f"{refname}^{{commit}}")
            if not commit:
                continue
            notes = await self._show_release_notes(refname, tag)
            if not created:
                try:
                    created = (
                        await self._git("log", "-1", "--format=%cI", commit)
                    ).strip()
                except UpdateError:
                    created = ""
            compat_result = await self._compat_for_target(
                commit.strip(), local_state
            )
            releases.append(
                {
                    "tag": tag,
                    "name": _release_name(tag, notes),
                    "commit": commit.strip(),
                    "commit_short": commit.strip()[:10],
                    "published_at": created.strip(),
                    "notes": notes,
                    "relation": await self._relation(current_sha, commit.strip()),
                    "is_current": commit.strip() == current_sha,
                    "compat": public_compat(compat_result),
                }
            )
            if len(releases) >= MAX_RELEASES:
                break

        self._releases_cache = (
            now + RELEASES_CACHE_SECONDS,
            current_sha,
            state_key,
            [dict(item) for item in releases],
        )
        return releases

    async def _show_release_notes(self, refname: str, tag: str) -> str:
        if not _safe_tag(tag):
            return ""
        path = f"{RELEASE_NOTES_DIR}/{tag}.md"
        try:
            text = await self._git("show", f"{refname}:{path}")
        except UpdateError:
            return ""
        text = text.strip()
        if len(text) > RELEASE_NOTES_MAX_CHARS:
            text = text[:RELEASE_NOTES_MAX_CHARS] + "\n\n…（更新日志过长，已截断）"
        return text

    async def _relation(self, current: str, target: str) -> str:
        if current == target:
            return "current"
        if await self._is_ancestor(target, current):
            return "older"
        if await self._is_ancestor(current, target):
            return "newer"
        return "diverged"

    # ============================================================
    # 检查 / 应用
    # ============================================================

    async def check(self, target_tag: str | None = None, *, mode: str = "manual") -> dict:
        """检查更新；target_tag 为空时检查最新 Release，否则检查指定版本。"""
        target_tag = str(target_tag or "").strip() or None
        if target_tag is not None and not _safe_tag(target_tag):
            raise UpdateError("release_invalid", "版本 tag 不合法")
        async with self._lock:
            status = await self._status_unlocked()
            if status["worktree_dirty"]:
                raise UpdateError(
                    "worktree_dirty",
                    "工作树有未提交改动，拒绝自动更新；请先提交或清理",
                )
            if status["data_dir_inside_project"]:
                raise UpdateError(
                    "data_dir_inside_project",
                    "用户数据目录位于代码工作树内，拒绝自动更新",
                )
            remote_url = self._remote_url()
            releases = await self._fetch_releases_unlocked(remote_url, force=True)
            current_sha = status["current_commit"].strip()
            selected: dict | None = None
            if target_tag is not None:
                selected = next((r for r in releases if r["tag"] == target_tag), None)
                if selected is None:
                    raise UpdateError(
                        "release_not_found",
                        f"远端没有名为 {target_tag} 的 Release/tag",
                    )
                target_sha = selected["commit"]
                target_label = selected["tag"]
            elif releases:
                selected = releases[0]
                target_sha = selected["commit"]
                target_label = selected["tag"]
            else:
                await self._git(
                    "fetch",
                    "--no-tags",
                    "--force",
                    remote_url,
                    f"+refs/heads/{UPDATE_REF}:{UPDATE_TRACKING_REF}",
                    timeout=self.timeout_seconds,
                )
                target_sha = (await self._git("rev-parse", UPDATE_TRACKING_REF)).strip()
                target_label = UPDATE_REF

            relation = await self._relation(current_sha, target_sha)
            dep_text = await self._git(
                "diff",
                "--name-only",
                current_sha,
                target_sha,
                "--",
                *DEPENDENCY_FILES,
            )
            dependency_changes = [
                line for line in dep_text.splitlines() if line.strip()
            ]
            local_state = await self._local_compat_state()
            compat_result = await self._compat_for_target(
                target_sha, local_state
            )
            can_apply = True
            reason = relation
            if target_sha == current_sha:
                can_apply = False
                reason = "up_to_date"
            elif dependency_changes and not self.auto_install_dependencies:
                can_apply = False
                reason = "dependency_change_requires_install"
            elif relation == "older" and target_tag is None:
                # 默认检查不主动提示回退到旧版本；用户需要显式选择版本。
                can_apply = False
                reason = "up_to_date"
            elif not compat_result["compatible"]:
                can_apply = False
                reason = compat_result["code"]

            commits_text = await self._git(
                "log",
                "--format=%h%x09%s",
                "-n",
                "12",
                target_sha,
                "--not",
                current_sha,
            )
            commits = [line for line in commits_text.splitlines() if line.strip()]

            warnings: list[str] = []
            if status["current_branch"] != UPDATE_LOCAL_BRANCH:
                warnings.append(
                    f"应用更新会从 {status['current_branch']} 切换到 {UPDATE_LOCAL_BRANCH}；"
                    "当前分支不会被删除，但工作树会加载稳定分支代码。"
                )
            if relation == "older":
                warnings.append(
                    f"{target_label} 比当前提交旧，这是回退操作；旧 HEAD 会先备份。"
                )
            elif relation == "diverged":
                warnings.append(
                    f"{target_label} 与当前提交不在同一条历史线上；会移动 "
                    f"{UPDATE_LOCAL_BRANCH}，旧 HEAD 会先备份。"
                )
            if reason == "up_to_date":
                warnings.append("当前已经是最新版本；没有可应用的更新。")
            if dependency_changes:
                if self.auto_install_dependencies:
                    warnings.append(
                        "目标版本修改了依赖文件（"
                        + "、".join(dependency_changes)
                        + "）；应用更新后将用当前 Python 解释器自动安装/更新依赖，"
                        "安装失败会回滚源码。"
                    )
                else:
                    warnings.append(
                        "目标版本修改了依赖文件（"
                        + "、".join(dependency_changes)
                        + "）；自动安装已关闭，请手动安装依赖后再更新。"
                    )
            if not compat_result["compatible"]:
                warnings.append(compat_result["message"])
            if self.mirror:
                warnings.append(
                    "镜像站会提供将要执行的代码；只使用你信任的 https 镜像。"
                )

            token = secrets.token_urlsafe(32)
            expires_at = time.time() + UPDATE_CONFIRM_TTL_SECONDS
            plan_kind = relation
            self._plan = {
                "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "expires_at": expires_at,
                "target_sha": target_sha,
                "target_tag": selected["tag"] if selected else None,
                "target_label": target_label,
                "kind": plan_kind,
                "old_head": status["current_commit"],
                "mirror": self.mirror,
                "target_full_sha": target_sha,
                "compat_manifest_hash": compat_result.get("manifest_hash", ""),
                "compat_code": compat_result.get("code", ""),
                "data_snapshot": compat_result.get("data_snapshot", {}),
            }
            available_release = None
            if (
                target_tag is None
                and selected is not None
                # 自动提醒只针对可向前推进且通过兼容门禁的 Release；
                # 另一条历史/回退版本仍可从版本列表手动选择，但不主动弹绿标。
                and relation == "newer"
                and compat_result["compatible"]
            ):
                available_release = {
                    "tag": selected["tag"],
                    "name": selected["name"],
                    "commit": selected["commit"],
                    "commit_short": selected["commit_short"],
                    "published_at": selected["published_at"],
                    "relation": relation,
                }
            result = {
                **status,
                "target_commit": target_sha,
                "target_commit_short": target_sha[:10],
                "target_tag": selected["tag"] if selected else None,
                "target_label": target_label,
                "target_name": selected["name"] if selected else target_label,
                "target_notes": selected["notes"] if selected else "",
                "target_relation": relation,
                "is_rollback": relation == "older",
                "is_diverged": relation == "diverged",
                "can_apply": bool(can_apply),
                "reason": reason,
                "dependency_changes": dependency_changes,
                "auto_install_dependencies": self.auto_install_dependencies,
                "dependency_install_required": bool(
                    dependency_changes and self.auto_install_dependencies and can_apply
                ),
                "commits": commits,
                "warnings": warnings,
                "confirmation_token": token,
                "expires_at": expires_at,
                "available": bool(available_release),
                "available_release": available_release,
                "latest_release": _release_public(releases[0]) if releases else None,
                "releases_count": len(releases),
                "compat": compat_result,
                "compat_code": compat_result.get("code", ""),
                "compat_message": compat_result.get("message", ""),
                "compat_manifest_hash": compat_result.get("manifest_hash", ""),
                "mode": mode,
            }
            if target_tag is None:
                await self._record_success(result)
            return result

    async def apply(
        self,
        *,
        confirmation_token: str,
        target_sha: str,
        confirm_branch_switch: bool,
        confirm_rollback: bool = False,
        defer_restart: bool = False,
    ) -> dict:
        async with self._lock:
            plan = self._plan
            if plan is None:
                raise UpdateError("no_check", "请先检查更新")
            if float(plan.get("expires_at") or 0) < time.time():
                self._plan = None
                raise UpdateError("check_expired", "更新检查已过期，请重新检查")
            token_hash = hashlib.sha256(
                str(confirmation_token or "").encode("utf-8")
            ).hexdigest()
            if not secrets.compare_digest(str(plan["token_hash"]), token_hash):
                raise UpdateError("confirmation_token_invalid", "确认令牌不匹配")
            if str(plan.get("target_sha")) != str(target_sha):
                raise UpdateError("target_changed", "目标提交已变化，请重新检查")
            if str(plan.get("old_head")) != (await self._git("rev-parse", "HEAD")).strip():
                raise UpdateError("head_changed", "代码 HEAD 已变化，请重新检查")
            if plan.get("target_tag") and plan.get("kind") in {"older", "diverged"}:
                if not confirm_rollback:
                    raise UpdateError(
                        "rollback_not_confirmed",
                        "更新/回退到该版本会移动分支，请确认后重试",
                    )
            status = await self._status_unlocked()
            if status["worktree_dirty"]:
                raise UpdateError("worktree_dirty", "工作树不干净，拒绝更新")
            if status["data_dir_inside_project"]:
                raise UpdateError(
                    "data_dir_inside_project",
                    "用户数据目录位于代码工作树内，拒绝更新",
                )
            if status["current_branch"] != UPDATE_LOCAL_BRANCH and not confirm_branch_switch:
                raise UpdateError(
                    "branch_switch_not_confirmed",
                    f"应用更新需要切换到 {UPDATE_LOCAL_BRANCH}",
                )
            local_main = await self._rev_parse(f"refs/heads/{UPDATE_LOCAL_BRANCH}")
            old_head = (await self._git("rev-parse", "HEAD")).strip()
            if old_head == target_sha:
                raise UpdateError("up_to_date", "当前工作树已经在该提交")
            dep_text = await self._git(
                "diff",
                "--name-only",
                old_head,
                target_sha,
                "--",
                *DEPENDENCY_FILES,
            )
            dependency_changes = [
                line for line in dep_text.splitlines() if line.strip()
            ]
            if dependency_changes and not self.auto_install_dependencies:
                raise UpdateError(
                    "dependency_change_requires_install",
                    "目标版本修改了依赖文件；自动安装已关闭，请手动安装依赖后再更新",
                )

            # 计划创建后本地数据可能已经变化；在切换源码前重新做兼容预检。
            local_state = await self._local_compat_state()
            compat_result = await self._compat_for_target(
                target_sha, local_state
            )
            planned_snapshot = plan.get("data_snapshot") or {}
            if planned_snapshot and planned_snapshot != compat_result.get("data_snapshot"):
                raise UpdateError(
                    "data_state_changed",
                    "检查后本地数据版本/迁移状态发生变化，请重新检查更新",
                )
            if not compat_result.get("compatible"):
                raise UpdateError(
                    compat_result.get("code", "incompatible"),
                    compat_result.get("message", "目标版本与当前数据不兼容"),
                )
            expected_hash = plan.get("compat_manifest_hash") or ""
            if expected_hash and compat_result.get("manifest_hash") != expected_hash:
                raise UpdateError(
                    "compat_manifest_changed",
                    "目标兼容声明在计划创建后发生变化，请重新检查",
                )

            backup_ref = (
                "refs/backups/update/"
                + time.strftime("%Y%m%d-%H%M%S", time.localtime())
                + "-"
                + secrets.token_hex(2)
            )
            await self._git("update-ref", backup_ref, old_head)
            # 当前不在稳定分支时，本地 refactor/main 的 tip 也可能与 HEAD 不同。
            # 切换前把它一并备份，避免 -C 移动分支时丢掉未检出的提交；如果 tip
            # 已经等于目标，则不会移动它，不需要额外备份。
            backup_branch_ref = None
            if (
                local_main is not None
                and local_main != old_head
                and local_main != target_sha
            ):
                backup_branch_ref = backup_ref + "-branch"
                await self._git("update-ref", backup_branch_ref, local_main)
            # 使用 -C 可以直接把本地稳定分支移动到任意 release（含回退），
            # 旧提交已先写入 backup_ref，工作树已确认干净。
            await self._git(
                "switch",
                "--no-guess",
                "-C",
                UPDATE_LOCAL_BRANCH,
                target_sha,
            )
            new_head = (await self._git("rev-parse", "HEAD")).strip()
            if new_head != target_sha:
                raise UpdateError(
                    "post_apply_mismatch",
                    f"更新后 HEAD={new_head}，预期 {target_sha}",
                )
            install_result = None
            if dependency_changes:
                try:
                    install_result = await self._install_dependencies(
                        dependency_changes
                    )
                except UpdateError as exc:
                    # A half-updated environment is worse than the old code:
                    # put the source tree back and require a fresh check.
                    self._plan = None
                    try:
                        await self._git(
                            "switch",
                            "--no-guess",
                            "-C",
                            UPDATE_LOCAL_BRANCH,
                            old_head,
                        )
                    except UpdateError as rollback_exc:
                        raise UpdateError(
                            "dependency_install_failed_rollback_failed",
                            "依赖安装失败，且源码回滚也失败："
                            f"{exc.message}；回滚错误：{rollback_exc.message}",
                        ) from exc
                    raise UpdateError(
                        "dependency_install_failed",
                        f"依赖安装失败，源码已回滚到 {old_head[:10]}：{exc.message}",
                    ) from exc
            await asyncio.to_thread(
                self._append_history,
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "old_commit": old_head,
                    "new_commit": new_head,
                    "target_tag": plan.get("target_tag"),
                    "kind": plan.get("kind"),
                    "backup_ref": backup_ref,
                    "backup_branch_ref": backup_branch_ref,
                    "mirror": self.mirror,
                    "dependency_changes": dependency_changes,
                    "dependency_install": install_result,
                },
            )
            self._plan = None
            self._releases_cache = None
            await self._record_applied(new_head)
            restart_scheduled = (
                self.restart_callback is not None and not defer_restart
            )
            if restart_scheduled:
                task = asyncio.create_task(self._restart_after_delay())
                self._restart_tasks.add(task)
                task.add_done_callback(self._restart_tasks.discard)
            restart_required = self.restart_callback is not None and defer_restart
            installed_note = "并已安装依赖" if install_result is not None else ""
            if restart_scheduled:
                message = f"更新完成{installed_note}；服务将在约 1 秒后重启"
            elif restart_required:
                message = f"更新完成{installed_note}；请重启后端加载新代码"
            else:
                message = f"更新完成{installed_note}"
            return {
                "ok": True,
                "old_commit": old_head,
                "new_commit": new_head,
                "target_tag": plan.get("target_tag"),
                "kind": plan.get("kind"),
                "backup_ref": backup_ref,
                "backup_branch_ref": backup_branch_ref,
                "restart_scheduled": restart_scheduled,
                "restart_required": restart_required,
                "dependency_changes": dependency_changes,
                "dependency_install": install_result,
                "message": message,
            }

    async def _install_dependencies(
        self,
        dependency_changes: list[str],
    ) -> dict:
        """Install/refresh dependencies with the *running* interpreter.

        Only the repository's own dependency files are consulted. There is no
        shell interpolation: commands are argv lists and run with the project
        root as cwd. A failure is surfaced as ``UpdateError`` so ``apply`` can
        roll the source tree back.
        """
        pip_base = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
        ]
        steps: list[tuple[str, list[str]]] = []
        requirements = self.project_root / "requirements.txt"
        if "requirements.txt" in dependency_changes and requirements.is_file():
            steps.append(("requirements.txt", [*pip_base, "-r", "requirements.txt"]))
        steps.append(("project", [*pip_base, "-e", "."]))

        uv = shutil.which("uv")
        results: list[dict[str, str]] = []
        for label, pip_cmd in steps:
            command = pip_cmd
            output = await self._run_install_command(command)
            if self._pip_missing(output):
                if not uv:
                    raise UpdateError(
                        "dependency_install_failed",
                        "当前解释器没有 pip，也未找到 uv；无法自动安装依赖",
                    )
                uv_command = [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                ]
                if label == "requirements.txt":
                    uv_command.extend(["-r", "requirements.txt"])
                else:
                    uv_command.extend(["-e", "."])
                command = uv_command
                output = await self._run_install_command(command)
            if output.get("returncode") != 0:
                raise UpdateError(
                    "dependency_install_failed",
                    f"{label} 安装失败（exit={output.get('returncode')}）："
                    f"{output.get('output_tail') or '没有输出'}",
                )
            results.append(
                {
                    "step": label,
                    "command": " ".join(command),
                    "output_tail": str(output.get("output_tail") or ""),
                }
            )
        return {
            "ok": True,
            "interpreter": sys.executable,
            "changed_files": list(dependency_changes),
            "steps": results,
        }

    @staticmethod
    def _pip_missing(output: dict) -> bool:
        text = f"{output.get('output_tail') or ''} {output.get('stderr') or ''}".lower()
        return output.get("returncode") != 0 and (
            "no module named pip" in text
            or "pip: not found" in text
            or "no module named 'pip'" in text
        )

    async def _run_install_command(self, command: list[str]) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "PIP_NO_INPUT": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONUNBUFFERED": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        env.pop("PIP_REQUIRE_VIRTUALENV", None)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.project_root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=600,
            )
        except asyncio.TimeoutError as exc:
            if proc is not None:
                try:
                    if os.name == "nt":
                        proc.kill()
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, AttributeError):
                    try:
                        proc.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
            raise UpdateError(
                "dependency_install_timeout",
                "依赖安装超过 600 秒，已终止",
            ) from exc
        except OSError as exc:
            raise UpdateError(
                "dependency_install_failed",
                f"无法启动依赖安装进程：{exc}",
            ) from exc
        text = _sanitize_install_output(
            (stdout or b"").decode("utf-8", errors="replace")
        )
        return {
            "returncode": int(proc.returncode if proc is not None else -1),
            "output_tail": text[-4000:],
        }

    async def auto_check_if_due(self, *, now: float | None = None) -> dict:
        """后台调度入口：只在到期时联网，失败按 6 小时重试。"""
        current_time = time.time() if now is None else float(now)
        state = await self._read_state()
        next_at = state.get("next_check_at")
        if next_at is None:
            last = state.get("last_check_at") or state.get("last_attempt_at")
            if last:
                next_at = float(last) + self.check_interval_days * 86400
        if next_at is not None and current_time < float(next_at):
            return {
                "skipped": True,
                "reason": "not_due",
                "next_check_at": float(next_at),
            }
        try:
            result = await self.check(mode="auto")
            return {
                **result,
                "skipped": False,
            }
        except UpdateError as exc:
            await self._record_failure(exc)
            return {
                "skipped": False,
                "ok": False,
                "error": exc.code,
                "message": exc.message,
                "next_check_at": current_time + RETRY_INTERVAL_SECONDS,
            }

    async def _restart_after_delay(self) -> None:
        await asyncio.sleep(1.0)
        try:
            await self.restart_callback()
        except Exception:
            logger.exception("自动更新后的服务重启失败")

    def _append_history(self, record: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "update_history.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _data_dir_inside_project(self) -> bool:
        try:
            self.data_dir.relative_to(self.project_root)
            return True
        except ValueError:
            return False

    async def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        try:
            await self._git(
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            )
        except UpdateError:
            return False
        return True

    async def _rev_parse(self, ref: str) -> str | None:
        try:
            return (await self._git("rev-parse", "--verify", ref)).strip()
        except UpdateError:
            return None

    async def _git(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> str:
        windows = os.name == "nt"
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if not windows:
            env["GIT_ASKPASS"] = "/bin/false"
        env["LC_ALL"] = "C"
        command = [
            "git",
            "-C",
            str(self.project_root),
            "-c",
            f"core.hooksPath={os.devnull}",
            *args,
        ]

        def kill_process() -> None:
            if windows:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=5,
                    )
                except (OSError, subprocess.SubprocessError):
                    try:
                        proc.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
                return
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # git 会派生 git-remote-https 等子进程；只 kill 父进程时子进程
                # 仍持有管道，communicate() 可能永久挂住并卡死更新锁。
                start_new_session=not windows,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=float(timeout or self.timeout_seconds),
                )
            except asyncio.TimeoutError:
                kill_process()
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("git 超时后仍有子进程占用管道，放弃等待")
                raise UpdateError(
                    "git_timeout",
                    f"git 操作超时（{int(timeout or self.timeout_seconds)} 秒）",
                ) from None
            except asyncio.CancelledError:
                # 服务关闭/任务取消时也要回收 git 子进程，避免 watchdog 取消后
                # 留下仍在 fetch 的 git-remote-https。
                kill_process()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                raise
        except FileNotFoundError:
            raise UpdateError("git_missing", "系统未安装 git") from None
        if proc.returncode != 0:
            message = (stderr or stdout).decode("utf-8", "replace").strip()
            raise UpdateError(
                "git_failed",
                f"git {' '.join(args[:2])} 失败：{message[:500]}",
            )
        return stdout.decode("utf-8", "replace")


def _release_name(tag: str, notes: str) -> str:
    for line in notes.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
        if line:
            break
    return tag


def _release_public(release: dict) -> dict:
    return {
        key: release.get(key)
        for key in (
            "tag",
            "name",
            "commit",
            "commit_short",
            "published_at",
            "relation",
            "is_current",
        )
    }


async def systemd_restart_callback() -> None:
    """兼容旧名字：按当前运行模式（systemd 或内置后台）重启后端。"""
    from .backend_restart import restart_backend_callback

    await restart_backend_callback()


__all__ = [
    "APP_RELEASE_LABEL",
    "DEFAULT_CHECK_INTERVAL_DAYS",
    "MIRROR_PRESETS",
    "UPDATE_REF",
    "UPDATE_REPO_URL",
    "UpdateError",
    "UpdateService",
    "mirror_presets",
    "resolve_update_repo_url",
    "systemd_restart_callback",
]
