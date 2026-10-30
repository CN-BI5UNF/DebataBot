"""One-time administrator setup for C-mode container storage.

Linux:
    ``sudo -S`` reads the password from stdin. The password is never placed in
    argv, environment variables, logs, files, or the returned payload. There is
    no shell interpolation: the script path and arguments come from the fixed
    ``ContainerStorage.setup_argv()`` contract and are re-validated here.

Windows:
    ``Start-Process -Verb RunAs`` shows the normal UAC consent dialog. No sudo
    password is needed or accepted. The elevated child cannot stream output
    back reliably, so success is verified by re-reading the storage marker and
    mount status after the UAC process exits.

Neither path silently falls back to storage B. If elevation is unavailable or
not interactive, the caller gets an explicit error with the manual command.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ContainerError
from .storage import ContainerStorage

logger = logging.getLogger(__name__)

_MAX_PASSWORD_CHARS = 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_SETUP_TIMEOUT_SECONDS = 300.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDENTIAL_URL_RE = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*=\s*([^\s&]+)"
)
_AUTH_FAILURE_RE = re.compile(
    r"(sorry, try again|incorrect password|authentication failure|"
    r"no password was provided|密码不正确|认证失败|无效的密码)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AdminCommandOutcome:
    returncode: int
    output_tail: str = ""
    timed_out: bool = False
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class AdminSetupResult:
    ok: bool
    code: str = ""
    message: str = ""
    exit_code: int = 0
    output_tail: str = ""
    timed_out: bool = False
    admin_method: str = ""
    storage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "exit_code": self.exit_code,
            "output_tail": self.output_tail,
            "timed_out": self.timed_out,
            "admin_method": self.admin_method,
            "storage": self.storage,
        }


AdminRunner = Callable[
    [list[str], bytes | None, float], Awaitable[AdminCommandOutcome]
]


def _sanitize_output(text: str) -> str:
    cleaned = _ANSI_RE.sub("", str(text or ""))
    cleaned = _CREDENTIAL_URL_RE.sub(r"\1***@", cleaned)
    cleaned = _SECRET_ASSIGN_RE.sub(r"\1=***", cleaned)
    return cleaned.strip()


def _tail(text: str, limit: int = 2000) -> str:
    value = _sanitize_output(text)
    return value[-limit:] if len(value) > limit else value


def _find_sudo(environ: dict[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    override = str(source.get("KAREN_SUDO_BIN", "") or "").strip()
    candidates = [override, shutil.which("sudo", path=source.get("PATH"))]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _find_powershell(environ: dict[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    override = str(source.get("KAREN_POWERSHELL_BIN", "") or "").strip()
    candidates = [
        override,
        shutil.which("powershell", path=source.get("PATH")),
        shutil.which("powershell.exe", path=source.get("PATH")),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def admin_capabilities(
    platform_name: str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    platform_text = str(platform_name or os.name).lower()
    if platform_text.startswith("win"):
        binary = _find_powershell(environ)
        return {
            "method": "uac",
            "available": binary is not None,
            "requires_password": False,
            "binary": Path(binary).name if binary else "",
            "note": (
                "Windows 会弹出 UAC 管理员确认，不需要输入 sudo 密码。"
                "如果服务运行在无桌面的远程会话，UAC 可能无法显示；"
                "请改用 B 模式或以管理员 PowerShell 手动运行脚本。"
            ),
        }
    binary = _find_sudo(environ)
    return {
        "method": "sudo_password",
        "available": binary is not None,
        "requires_password": True,
        "binary": binary or "",
        "note": (
            "密码只通过 stdin 交给本机 sudo，不会保存或写入日志。"
            "若通过局域网 HTTP 打开 WebUI，密码在传输中不受 TLS 保护；"
            "建议在本机浏览器操作。"
        ),
    }


def _validate_password(password: str | None) -> str:
    text = "" if password is None else str(password)
    if not text:
        raise ContainerError(
            "请输入当前用户的 sudo 密码", code="container_admin_password_required"
        )
    if len(text) > _MAX_PASSWORD_CHARS:
        raise ContainerError(
            "sudo 密码过长", code="container_admin_password_invalid"
        )
    if any(ch in text for ch in ("\x00", "\r", "\n")):
        raise ContainerError(
            "sudo 密码不能包含换行或 NUL 字符",
            code="container_admin_password_invalid",
        )
    return text


def _validated_script(storage: ContainerStorage) -> tuple[Path, list[str]]:
    """Return the fixed setup script plus its argument tail, or raise."""
    if storage.mode != "image_fixed":
        raise ContainerError(
            "只有 C 固定大小存储需要管理员初始化",
            code="container_storage_setup_not_required",
        )
    argv = [str(part) for part in storage.setup_argv()]
    if not argv:
        raise ContainerError(
            "无法构造管理员安装命令", code="container_admin_command_invalid"
        )
    if storage.platform_name.startswith("win"):
        if len(argv) < 5 or argv[1].lower() != "-executionpolicy":
            raise ContainerError(
                "Windows 安装命令格式无效", code="container_admin_command_invalid"
            )
        try:
            file_index = argv.index("-File")
        except ValueError:
            raise ContainerError(
                "Windows 安装命令缺少脚本路径",
                code="container_admin_command_invalid",
            ) from None
        script_index = file_index + 1
        if script_index >= len(argv):
            raise ContainerError(
                "Windows 安装命令缺少脚本路径",
                code="container_admin_command_invalid",
            )
    else:
        if len(argv) < 3 or argv[0] != "sudo" or argv[1] != "bash":
            raise ContainerError(
                "Linux 安装命令格式无效", code="container_admin_command_invalid"
            )
        script_index = 2
    script_path = Path(argv[script_index])
    expected_dir = (storage.paths.PROJECT_ROOT / "scripts" / "container").resolve()
    try:
        resolved = script_path.resolve(strict=True)
    except OSError:
        raise ContainerError(
            "管理员安装脚本不存在", code="container_admin_script_missing"
        ) from None
    if not resolved.is_file() or not resolved.is_relative_to(expected_dir):
        raise ContainerError(
            "管理员安装脚本路径不在受信任目录",
            code="container_admin_script_invalid",
        )
    suffix = ".ps1" if storage.platform_name.startswith("win") else ".sh"
    if resolved.suffix.lower() != suffix:
        raise ContainerError(
            "管理员安装脚本类型与平台不一致",
            code="container_admin_script_invalid",
        )
    return resolved, argv[script_index + 1 :]


def linux_setup_argv(
    storage: ContainerStorage,
    *,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Build ``sudo -S ...`` argv; the password itself is never an argument."""
    source_env = os.environ if environ is None else environ
    sudo = _find_sudo(source_env)
    if sudo is None:
        raise ContainerError(
            "未找到 sudo；请安装 sudo 或以 root 手动运行安装脚本",
            code="container_sudo_missing",
        )
    bash = shutil.which("bash", path=source_env.get("PATH")) or "/bin/bash"
    if not Path(bash).is_file():
        raise ContainerError(
            "未找到 bash；无法执行管理员安装脚本",
            code="container_admin_script_invalid",
        )
    script, tail = _validated_script(storage)
    return [sudo, "-S", "-p", "", "--", bash, str(script), *tail]


def windows_setup_argv(
    storage: ContainerStorage,
    *,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Build a PowerShell UAC launcher as an encoded command.

    The launcher only starts the fixed setup script. It needs no password and
    never opens a shell with model-controlled text.
    """
    source_env = os.environ if environ is None else environ
    powershell = _find_powershell(source_env)
    if powershell is None:
        raise ContainerError(
            "未找到 PowerShell；请以管理员身份手动运行 VHDX 安装脚本",
            code="container_powershell_missing",
        )
    script, tail = _validated_script(storage)
    child_args = [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *tail,
    ]
    arg_line = subprocess.list2cmdline(child_args)
    if any(ch in arg_line for ch in ("\x00", "\r", "\n")):
        raise ContainerError(
            "管理员安装参数包含非法字符",
            code="container_admin_command_invalid",
        )
    escaped_arg_line = arg_line.replace("'", "''")
    escaped_powershell = str(powershell).replace("'", "''")
    launcher = (
        "$ErrorActionPreference='Stop';"
        f"$psPath='{escaped_powershell}';"
        f"$argLine='{escaped_arg_line}';"
        "try {"
        "$p=Start-Process -FilePath $psPath -Verb RunAs -Wait -PassThru "
        "-ArgumentList $argLine;"
        "if ($null -eq $p) { exit 1 }; exit $p.ExitCode"
        "} catch { Write-Error $_.Exception.Message; exit 1223 }"
    )
    encoded = base64.b64encode(launcher.encode("utf-16-le")).decode("ascii")
    return [
        str(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


async def _read_tail(stream: asyncio.StreamReader) -> bytes:
    tail = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > _MAX_OUTPUT_BYTES:
            del tail[: len(tail) - _MAX_OUTPUT_BYTES]
    return bytes(tail)


async def _default_runner(
    argv: list[str],
    stdin_bytes: bytes | None,
    timeout: float,
) -> AdminCommandOutcome:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=(
                asyncio.subprocess.PIPE
                if stdin_bytes is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        assert proc.stdout is not None

        async def _write_stdin() -> None:
            if stdin_bytes is None or proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # sudo may have cached credentials and never reads stdin.
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        async def _collect() -> bytes:
            assert proc is not None and proc.stdout is not None
            return await _read_tail(proc.stdout)

        try:
            tail_bytes, _ = await asyncio.wait_for(
                asyncio.gather(_collect(), _write_stdin()),
                timeout=max(1.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            if proc is not None:
                _kill_process(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            return AdminCommandOutcome(
                returncode=-1,
                timed_out=True,
                error_code="container_admin_timeout",
                error_message=f"管理员安装超过 {int(timeout)} 秒，已终止",
            )
        except asyncio.CancelledError:
            # Client disconnect / shutdown: stop the elevation child and its
            # setup script rather than leaving it detached.
            if proc is not None:
                _kill_process(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        await proc.wait()
        return AdminCommandOutcome(
            returncode=int(proc.returncode if proc.returncode is not None else -1),
            output_tail=_tail(tail_bytes.decode("utf-8", errors="replace")),
        )
    except OSError as exc:
        if proc is not None:
            _kill_process(proc)
        return AdminCommandOutcome(
            returncode=-1,
            error_code="container_admin_process_failed",
            error_message=f"无法启动管理员安装进程：{exc}",
        )


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            proc.kill()


def _result_from_outcome(
    outcome: AdminCommandOutcome,
    *,
    storage: ContainerStorage,
    admin_method: str,
) -> AdminSetupResult:
    status = storage.status(include_host_paths=True)
    storage_data = status.as_dict(include_host_paths=True)
    if outcome.timed_out:
        code = outcome.error_code or "container_admin_timeout"
        message = outcome.error_message or "管理员安装超时"
    elif outcome.error_code:
        code = outcome.error_code
        message = outcome.error_message or "管理员安装进程启动失败"
    elif outcome.returncode != 0:
        if admin_method == "uac" and outcome.returncode == 1223:
            code = "container_admin_uac_cancelled"
            message = "UAC 提权已取消；存储未初始化"
        elif _AUTH_FAILURE_RE.search(outcome.output_tail or ""):
            code = "container_admin_auth_failed"
            message = "sudo 密码不正确或认证失败"
        else:
            code = "container_admin_setup_failed"
            message = "管理员存储安装失败"
            if outcome.output_tail:
                message += "：" + _tail(outcome.output_tail, 500)
    elif not status.ready:
        code = status.code or "container_storage_not_ready"
        message = status.error or "安装命令已结束，但存储仍未就绪"
    else:
        return AdminSetupResult(
            ok=True,
            admin_method=admin_method,
            exit_code=outcome.returncode,
            output_tail=outcome.output_tail,
            storage=storage_data,
        )
    return AdminSetupResult(
        ok=False,
        code=code,
        message=message,
        exit_code=outcome.returncode,
        output_tail=outcome.output_tail,
        timed_out=outcome.timed_out,
        admin_method=admin_method,
        storage=storage_data,
    )


async def run_admin_setup(
    storage: ContainerStorage,
    password: str | None = None,
    *,
    platform_name: str | None = None,
    environ: dict[str, str] | None = None,
    runner: AdminRunner | None = None,
    timeout: float = _SETUP_TIMEOUT_SECONDS,
) -> AdminSetupResult:
    """Run the fixed storage setup command and verify the resulting marker."""
    platform_text = str(platform_name or storage.platform_name).lower()
    if storage.mode != "image_fixed":
        return AdminSetupResult(
            ok=True,
            code="container_storage_setup_not_required",
            message="当前为 B 命名卷模式，不需要 C 存储初始化",
            admin_method="none",
            storage=storage.status(include_host_paths=True).as_dict(
                include_host_paths=True
            ),
        )

    use_runner = runner or _default_runner
    if platform_text.startswith("win"):
        argv = windows_setup_argv(storage, environ=environ)
        method = "uac"
        stdin_bytes = None
    else:
        password_text = _validate_password(password)
        argv = linux_setup_argv(storage, environ=environ)
        method = "sudo_password"
        stdin_bytes = password_text.encode("utf-8") + b"\n"
        # Drop the local reference as soon as the bytes are prepared. Python
        # cannot scrub the immutable string, but we avoid keeping extra copies
        # or letting it reach any formatter.
        password_text = ""

    logger.info("开始 C 存储管理员初始化 method=%s", method)
    try:
        outcome = await use_runner(argv, stdin_bytes, timeout)
    except ContainerError:
        raise
    except Exception as exc:  # noqa: BLE001 - API must return a safe error
        logger.warning("管理员存储初始化异常 method=%s: %s", method, exc)
        return AdminSetupResult(
            ok=False,
            code="container_admin_process_failed",
            message="无法执行管理员存储初始化",
            admin_method=method,
            storage=storage.status(include_host_paths=True).as_dict(
                include_host_paths=True
            ),
        )
    finally:
        stdin_bytes = None
    result = _result_from_outcome(outcome, storage=storage, admin_method=method)
    if result.ok:
        logger.info("C 存储管理员初始化完成 method=%s", method)
    else:
        logger.warning(
            "C 存储管理员初始化失败 method=%s code=%s output=%s",
            method,
            result.code,
            _tail(result.output_tail, 500) or "无输出",
        )
    return result


__all__ = [
    "AdminCommandOutcome",
    "AdminSetupResult",
    "admin_capabilities",
    "linux_setup_argv",
    "run_admin_setup",
    "windows_setup_argv",
]
