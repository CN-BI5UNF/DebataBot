"""Bounded DockerCLI process runner (no Docker daemon, no stress)."""

from __future__ import annotations

import asyncio
import sys

import pytest

from features.container.docker_cli import DockerCLI


@pytest.mark.asyncio
async def test_output_is_capped_without_unbounded_buffering():
    cli = DockerCLI(binary=sys.executable)
    result = await cli.run(
        ["-c", "import sys; sys.stdout.write('x' * 4096)"],
        timeout=10,
        max_output_bytes=1024,
    )
    assert result.returncode != 0  # terminated after the cap
    assert result.truncated is True
    assert len(result.stdout) <= 1024
    assert result.stdout == "x" * 1024


@pytest.mark.asyncio
async def test_timeout_is_reported_and_process_is_killed():
    cli = DockerCLI(binary=sys.executable)
    result = await cli.run(
        ["-c", "import time; time.sleep(2)"],
        timeout=0.2,
        max_output_bytes=1024,
    )
    assert result.timed_out is True
    assert result.returncode != 0


@pytest.mark.asyncio
async def test_cancellation_kills_the_child_instead_of_leaving_it_detached():
    cli = DockerCLI(binary=sys.executable)
    task = asyncio.create_task(
        cli.run(
            ["-c", "import time; time.sleep(30)"],
            timeout=60,
            max_output_bytes=1024,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_stdin_is_passed_without_a_host_shell():
    cli = DockerCLI(binary=sys.executable)
    result = await cli.run(
        ["-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        input_bytes=b"hello",
        timeout=10,
        max_output_bytes=1024,
    )
    assert result.returncode == 0
    assert result.stdout == "HELLO"
