"""整进程重启后端：systemd 用户/系统服务或内置后台模式。

systemd 判定必须看当前进程 cgroup 是否真的属于 Karen 的 unit。从桌面、
VNC 或其他 systemd 服务里启动时，环境里可能残留 ``INVOCATION_ID``、
``JOURNAL_STREAM`` 等变量（它们属于外层会话服务）；只看环境变量会误以为
当前进程受 systemd 管理，进而执行一个并不存在的
``systemctl --user restart karenbot.service``，结果静默失败。

没有 systemd 管理时，用独立 session 启动 ``main.py restart-helper``，
由它等待旧进程退出后再拉起新进程；helper 不受旧进程退出影响。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SYSTEMD_UNIT_NAME = "karenbot.service"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_self_cgroup() -> str:
    """读取当前进程 cgroup（测试可替换）。"""
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""


def systemd_scope() -> str | None:
    """当前进程属于 Karen systemd unit 时返回 ``"user"`` / ``"system"``。

    仅当 cgroup 路径里真的出现 ``karenbot.service`` 才算受管理；
    外层会话服务遗留下来的 systemd 环境变量不会被误判。
    """
    if not sys.platform.startswith("linux"):
        return None
    if shutil.which("systemctl") is None:
        return None
    for line in _read_self_cgroup().splitlines():
        # cgroup v1: 1:name=systemd:/user.slice/.../karenbot.service
        # cgroup v2: 0::/user.slice/.../karenbot.service
        path = line.rsplit(":", 1)[-1]
        parts = [part for part in path.split("/") if part]
        if SYSTEMD_UNIT_NAME not in parts:
            continue
        return "system" if "system.slice" in parts else "user"
    return None


def systemd_managed() -> bool:
    return systemd_scope() is not None


def _systemd_restart_command(scope: str) -> list[str]:
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    # --no-block: 先把 restart job 交给 systemd，避免客户端随当前 unit 一起被杀。
    command.extend(["--no-block", "restart", SYSTEMD_UNIT_NAME])
    return command


def spawn_fallback_restart_helper() -> None:
    """启动独立 helper：等旧进程退出后按当前启动参数拉起新后端。"""
    env = os.environ.copy()
    env["KAREN_SERVICE_ARGS"] = shlex.join(sys.argv[1:])
    console_log = PROJECT_ROOT / ".karen-run" / "console.log"
    console_log.parent.mkdir(parents=True, exist_ok=True)
    with open(console_log, "ab") as output:
        subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "main.py"),
                "restart-helper",
                str(os.getpid()),
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def request_backend_restart() -> str:
    """安排一次整进程重启，返回 ``"systemd"`` 或 ``"fallback"``。"""
    scope = systemd_scope()
    if scope is not None:
        subprocess.Popen(
            _systemd_restart_command(scope),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return "systemd"
    spawn_fallback_restart_helper()
    return "fallback"


async def restart_backend_callback() -> None:
    """更新器/接口共用的异步重启回调。

    systemd 模式交给 systemctl；fallback 模式下 helper 已在等待，随后
    向自身发 SIGTERM，让 uvicorn 走正常的优雅退出流程。
    """
    await asyncio.sleep(0.2)
    mode = request_backend_restart()
    if mode == "fallback":
        os.kill(os.getpid(), signal.SIGTERM)


__all__ = [
    "PROJECT_ROOT",
    "SYSTEMD_UNIT_NAME",
    "request_backend_restart",
    "restart_backend_callback",
    "spawn_fallback_restart_helper",
    "systemd_managed",
    "systemd_scope",
]
