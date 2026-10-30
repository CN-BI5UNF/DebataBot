"""Small, bounded Docker CLI runner.

We intentionally use the Docker CLI instead of a Python Docker SDK:
- no extra dependency and the same path on Linux/Windows Docker Desktop;
- commands are argument lists, never host shell strings;
- stdout/stderr are streamed with a hard byte cap so a noisy container cannot
  grow the host process memory without bound.

The runner does not decide security policy; ContainerService builds the fixed
container arguments and validates every path before calling it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_READ_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False


class DockerCLI:
    """Run Docker commands with bounded output and no shell interpolation."""

    def __init__(self, binary: str | None = None) -> None:
        configured = (
            binary
            or os.environ.get("KAREN_DOCKER_BIN")
            or (
                shutil.which("docker.exe" if os.name == "nt" else "docker")
            )
        )
        self.binary = configured or ("docker.exe" if os.name == "nt" else "docker")

    def available(self) -> bool:
        return bool(
            shutil.which(self.binary)
            or shutil.which("docker.exe" if os.name == "nt" else "docker")
        )

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float = 30.0,
        max_output_bytes: int = 256 * 1024,
        terminate_on_truncate: bool = True,
    ) -> CommandResult:
        if not args:
            raise ValueError("docker args must not be empty")
        cap = max(1024, int(max_output_bytes))
        stdin = asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            *args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def feed() -> None:
            if input_bytes is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(input_bytes)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        async def collect(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
            if stream is None:
                return b"", False
            buffer = bytearray()
            truncated = False
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    break
                remaining = cap - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    if terminate_on_truncate:
                        # Stop a runaway output producer promptly; the process
                        # may still be waited for below so exit status is
                        # deterministic. Pull/install commands opt out because
                        # their progress output is expected to be large.
                        with contextlib.suppress(ProcessLookupError):
                            proc.terminate()
            return bytes(buffer), truncated

        feed_task = asyncio.create_task(feed())
        out_task = asyncio.create_task(collect(proc.stdout))
        err_task = asyncio.create_task(collect(proc.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        except asyncio.CancelledError:
            # Runtime shutdown may cancel a background image pull. Do not leave
            # the Docker CLI child (and its network download) running detached.
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        finally:
            with contextlib.suppress(Exception):
                await feed_task
            try:
                stdout, out_truncated = await out_task
            except Exception:  # noqa: BLE001
                stdout, out_truncated = b"", True
            try:
                stderr, err_truncated = await err_task
            except Exception:  # noqa: BLE001
                stderr, err_truncated = b"", True
        return CommandResult(
            returncode=int(proc.returncode if proc.returncode is not None else -1),
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
            truncated=out_truncated or err_truncated,
            timed_out=timed_out,
        )


__all__ = ["CommandResult", "DockerCLI"]
