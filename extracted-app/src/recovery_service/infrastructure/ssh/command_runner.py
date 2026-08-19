"""通过 SSH 在远程 Linux 上执行命令（用于 docker exec / 列举 DMP 目录）。"""

import asyncio
from dataclasses import dataclass
from collections.abc import Callable

import asyncssh

from recovery_service.core.domain import RemoteHost
from recovery_service.core.exceptions import RemoteAccessError


@dataclass
class SSHCommandResult:
    returncode: int
    stdout: str
    stderr: str
    command: str


async def run_ssh_command_async(
    host: RemoteHost,
    command: str,
    *,
    timeout: int = 3600,
) -> SSHCommandResult:
    kwargs: dict = {
        "host": host.host,
        "port": host.port,
        "username": host.username,
        "known_hosts": None,
    }
    if host.private_key_path:
        kwargs["client_keys"] = [host.private_key_path]
    else:
        kwargs["password"] = host.password

    try:
        async with asyncssh.connect(**kwargs) as conn:
            result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
            return SSHCommandResult(
                returncode=result.exit_status or 0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                command=command,
            )
    except asyncio.TimeoutError as e:
        raise RemoteAccessError(f"SSH command timeout after {timeout}s") from e
    except Exception as e:
        raise RemoteAccessError(f"SSH command failed: {e}") from e


def run_ssh_command(host: RemoteHost, command: str, *, timeout: int = 3600) -> SSHCommandResult:
    return asyncio.run(run_ssh_command_async(host, command, timeout=timeout))


async def run_ssh_command_stream_async(
    host: RemoteHost,
    command: str,
    *,
    timeout: int = 3600,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> SSHCommandResult:
    kwargs: dict = {
        "host": host.host,
        "port": host.port,
        "username": host.username,
        "known_hosts": None,
    }
    if host.private_key_path:
        kwargs["client_keys"] = [host.private_key_path]
    else:
        kwargs["password"] = host.password

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    async def _read_stream(stream, parts: list[str], callback):
        while True:
            line = await stream.readline()
            if not line:
                break
            parts.append(line)
            if callback:
                callback(line)

    try:
        async with asyncssh.connect(**kwargs) as conn:
            proc = await conn.create_process(command)
            await asyncio.wait_for(
                asyncio.gather(
                    _read_stream(proc.stdout, stdout_parts, on_stdout),
                    _read_stream(proc.stderr, stderr_parts, on_stderr),
                    proc.wait(),
                ),
                timeout=timeout,
            )
            return SSHCommandResult(
                returncode=proc.exit_status or 0,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                command=command,
            )
    except asyncio.TimeoutError as e:
        raise RemoteAccessError(f"SSH command timeout after {timeout}s") from e
    except Exception as e:
        raise RemoteAccessError(f"SSH command failed: {e}") from e


def run_ssh_command_stream(
    host: RemoteHost,
    command: str,
    *,
    timeout: int = 3600,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> SSHCommandResult:
    return asyncio.run(
        run_ssh_command_stream_async(
            host,
            command,
            timeout=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
    )
