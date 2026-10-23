"""安全的代码更新器。

边界：
    - 只更新 Debata 源码仓库；用户数据目录（persona/聊天/媒体/配置）不在
      git 工作树内，更新过程不读取、不删除、不覆盖它。
    - 固定更新 refactor/main。当前所在分支不参与「是否有更新」的判断。
    - 只允许 fast-forward 本地 refactor/main；分叉、脏工作树、依赖文件变化
      都拒绝自动应用。
    - 镜像只允许 https（测试可显式放开本地 file remote），且必须指向同一仓库
      路径；不接受账号密码、file:// 或内网地址（生产模式）。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

UPDATE_REPO_URL = "https://github.com/CN-BI5UNF/Debata-Refactor.git"
UPDATE_REF = "refactor/main"
UPDATE_LOCAL_BRANCH = "refactor/main"
UPDATE_TRACKING_REF = "refs/remotes/update-target/refactor-main"
UPDATE_CONFIRM_TTL_SECONDS = 300
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt")


class UpdateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_update_repo_url(mirror: str = "") -> str:
    """解析并校验镜像；只允许同一 Debata-Refactor 仓库。"""
    mirror = str(mirror or "").strip()
    if not mirror:
        url = UPDATE_REPO_URL
    elif "{url}" in mirror:
        url = mirror.replace("{url}", UPDATE_REPO_URL)
    else:
        candidate = mirror.rstrip("/")
        candidate_path = unquote(urlsplit(candidate).path).rstrip("/").lower()
        if candidate_path.endswith("/cn-bi5unf/debata-refactor"):
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
    expected = "/cn-bi5unf/debata-refactor.git"
    if not path.endswith(expected):
        raise UpdateError(
            "mirror_repo_mismatch",
            "更新镜像必须指向 CN-BI5UNF/Debata-Refactor 仓库",
        )
    if parts.query or parts.fragment:
        raise UpdateError("mirror_query", "更新镜像 URL 不能包含查询参数或 fragment")
    return url.rstrip("/")


class UpdateService:
    def __init__(
        self,
        project_root: Path,
        data_dir: Path,
        *,
        mirror: str = "",
        timeout_seconds: int = 60,
        restart_callback=None,
        allow_local_remote: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.mirror = str(mirror or "").strip()
        self.timeout_seconds = max(10, min(300, int(timeout_seconds)))
        self.restart_callback = restart_callback
        self.allow_local_remote = bool(allow_local_remote)
        self._lock = asyncio.Lock()
        self._plan: dict | None = None
        self._restart_tasks: set[asyncio.Task] = set()

    def _remote_url(self) -> str:
        if self.allow_local_remote and self.mirror.startswith("file://"):
            return self.mirror
        return resolve_update_repo_url(self.mirror)

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
            "local_target_commit": local_main,
            "mirror": self.mirror,
            "effective_remote": remote_url,
            "mirror_error": remote_error,
            "worktree_dirty": bool(dirty_files),
            "dirty_file_count": len(dirty_files),
            "data_dir": str(self.data_dir),
            "data_dir_inside_project": data_inside,
            "restart_supported": self.restart_callback is not None,
        }

    async def check(self) -> dict:
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
            await self._git(
                "fetch",
                "--no-tags",
                "--force",
                remote_url,
                f"+refs/heads/{UPDATE_REF}:{UPDATE_TRACKING_REF}",
                timeout=self.timeout_seconds,
            )
            target_sha = (await self._git("rev-parse", UPDATE_TRACKING_REF)).strip()
            local_main = await self._rev_parse(f"refs/heads/{UPDATE_LOCAL_BRANCH}")
            can_fast_forward = True
            reason = ""
            if local_main is None:
                can_fast_forward = True
            elif local_main == target_sha:
                can_fast_forward = False
                reason = "up_to_date"
            else:
                can_fast_forward = await self._is_ancestor(local_main, target_sha)
                if not can_fast_forward:
                    reason = "target_diverged"
            dependency_changes: list[str] = []
            if local_main is not None and local_main != target_sha:
                dep_text = await self._git(
                    "diff",
                    "--name-only",
                    local_main,
                    target_sha,
                    "--",
                    *DEPENDENCY_FILES,
                )
                dependency_changes = [
                    line for line in dep_text.splitlines() if line.strip()
                ]
            if dependency_changes:
                can_fast_forward = False
                reason = "dependency_change_requires_install"
            commits_text = await self._git(
                "log",
                "--format=%h%x09%s",
                "-n",
                "12",
                target_sha,
                "--not",
                local_main or "HEAD",
            )
            commits = [
                line for line in commits_text.splitlines() if line.strip()
            ]
            warnings: list[str] = []
            if status["current_branch"] != UPDATE_LOCAL_BRANCH:
                warnings.append(
                    f"应用更新会从 {status['current_branch']} 切换到 {UPDATE_LOCAL_BRANCH}；"
                    "当前分支不会被删除，但工作树会加载稳定分支代码。"
                )
            if status["current_branch"] == UPDATE_LOCAL_BRANCH and reason == "up_to_date":
                warnings.append("refactor/main 已是最新；没有可应用的更新。")
            if self.mirror:
                warnings.append(
                    "镜像站会提供将要执行的代码；只使用你信任的 https 镜像。"
                )
            token = secrets.token_urlsafe(32)
            expires_at = time.time() + UPDATE_CONFIRM_TTL_SECONDS
            self._plan = {
                "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "expires_at": expires_at,
                "target_sha": target_sha,
                "old_head": status["current_commit"],
                "mirror": self.mirror,
            }
            return {
                **status,
                "target_commit": target_sha,
                "target_commit_short": target_sha[:10],
                "can_apply": bool(can_fast_forward),
                "reason": reason,
                "dependency_changes": dependency_changes,
                "commits": commits,
                "warnings": warnings,
                "confirmation_token": token,
                "expires_at": expires_at,
            }

    async def apply(
        self,
        *,
        confirmation_token: str,
        target_sha: str,
        confirm_branch_switch: bool,
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
            if local_main is not None and local_main == target_sha:
                raise UpdateError("up_to_date", "refactor/main 已是最新")
            if local_main is not None and not await self._is_ancestor(local_main, target_sha):
                raise UpdateError("target_diverged", "refactor/main 无法 fast-forward")
            dep_text = await self._git(
                "diff",
                "--name-only",
                local_main or "HEAD",
                target_sha,
                "--",
                *DEPENDENCY_FILES,
            )
            if [line for line in dep_text.splitlines() if line.strip()]:
                raise UpdateError(
                    "dependency_change_requires_install",
                    "目标版本修改了依赖文件；请手动安装依赖后再更新",
                )

            backup_ref = (
                "refs/backups/update/"
                + time.strftime("%Y%m%d-%H%M%S", time.localtime())
            )
            old_head = (await self._git("rev-parse", "HEAD")).strip()
            await self._git("update-ref", backup_ref, old_head)
            if local_main is None:
                await self._git(
                    "switch",
                    "--no-guess",
                    "-c",
                    UPDATE_LOCAL_BRANCH,
                    target_sha,
                )
            else:
                if status["current_branch"] != UPDATE_LOCAL_BRANCH:
                    await self._git("switch", "--no-guess", UPDATE_LOCAL_BRANCH)
                await self._git("merge", "--ff-only", target_sha)
            new_head = (await self._git("rev-parse", "HEAD")).strip()
            if new_head != target_sha:
                raise UpdateError(
                    "post_apply_mismatch",
                    f"更新后 HEAD={new_head}，预期 {target_sha}",
                )
            await asyncio.to_thread(
                self._append_history,
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "old_commit": old_head,
                    "new_commit": new_head,
                    "backup_ref": backup_ref,
                    "mirror": self.mirror,
                },
            )
            self._plan = None
            if self.restart_callback is not None:
                task = asyncio.create_task(self._restart_after_delay())
                self._restart_tasks.add(task)
                task.add_done_callback(self._restart_tasks.discard)
            return {
                "ok": True,
                "old_commit": old_head,
                "new_commit": new_head,
                "backup_ref": backup_ref,
                "restart_scheduled": self.restart_callback is not None,
                "message": "更新完成；服务将在约 1 秒后重启",
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
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "/bin/false"
        env["LC_ALL"] = "C"
        command = [
            "git",
            "-C",
            str(self.project_root),
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=float(timeout or self.timeout_seconds),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise UpdateError("git_timeout", "git 操作超时") from None
        except FileNotFoundError:
            raise UpdateError("git_missing", "系统未安装 git") from None
        if proc.returncode != 0:
            message = (stderr or stdout).decode("utf-8", "replace").strip()
            raise UpdateError(
                "git_failed",
                f"git {' '.join(args[:2])} 失败：{message[:500]}",
            )
        return stdout.decode("utf-8", "replace")


async def systemd_restart_callback() -> None:
    """在响应返回后重启当前用户服务。"""
    await asyncio.sleep(0.2)
    subprocess.Popen(
        ["systemctl", "--user", "restart", "debata-agent.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


__all__ = [
    "UPDATE_REF",
    "UPDATE_REPO_URL",
    "UpdateError",
    "UpdateService",
    "resolve_update_repo_url",
    "systemd_restart_callback",
]
