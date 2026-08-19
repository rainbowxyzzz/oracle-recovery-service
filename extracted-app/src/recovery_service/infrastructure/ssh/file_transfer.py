import asyncio
import shlex
from pathlib import PurePosixPath

import asyncssh

from recovery_service.common.logging import get_logger
from recovery_service.core.domain import DumpArtifact, RemoteHost
from recovery_service.core.exceptions import RemoteAccessError
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command

logger = get_logger(__name__)


def ensure_remote_directory(
    host: RemoteHost,
    path: str,
    *,
    mode: str = "777",
    sudo_password: str = "",
) -> None:
    q_path = shlex.quote(path)
    q_mode = shlex.quote(mode)
    command = f"mkdir -p {q_path} && chmod {q_mode} {q_path}"
    result = run_ssh_command(host, _sudo(command, sudo_password), timeout=300)
    if result.returncode != 0:
        raise RemoteAccessError(result.stderr or result.stdout or "remote directory setup failed")


def chmod_remote_tree(
    host: RemoteHost,
    path: str,
    *,
    mode: str = "777",
    sudo_password: str = "",
) -> None:
    q_path = shlex.quote(path)
    q_mode = shlex.quote(mode)
    result = run_ssh_command(host, _sudo(f"chmod -R {q_mode} {q_path}", sudo_password), timeout=300)
    if result.returncode != 0:
        raise RemoteAccessError(result.stderr or result.stdout or "remote chmod failed")


def copy_artifacts_between_hosts(
    source_host: RemoteHost,
    target_host: RemoteHost,
    artifacts: list[DumpArtifact],
    target_directory: str,
    *,
    chunk_size: int = 1024 * 1024,
) -> list[str]:
    if _same_ssh_endpoint(source_host, target_host):
        return copy_artifacts_on_same_host(
            target_host,
            artifacts,
            target_directory,
        )

    logger.info(
        "copy_artifacts_sftp_started",
        source_host=source_host.host,
        target_host=target_host.host,
        target_directory=target_directory,
        file_count=len(artifacts),
        total_bytes=sum(a.size_bytes for a in artifacts),
    )
    return asyncio.run(
        _copy_artifacts_between_hosts(
            source_host,
            target_host,
            artifacts,
            target_directory,
            chunk_size=chunk_size,
        )
    )


def copy_artifacts_on_same_host(
    host: RemoteHost,
    artifacts: list[DumpArtifact],
    target_directory: str,
) -> list[str]:
    copied: list[str] = []
    for artifact in artifacts:
        dest = str(PurePosixPath(target_directory) / artifact.filename)
        if artifact.remote_path == dest:
            copied.append(dest)
            continue

        logger.info(
            "copy_artifact_remote_cp_started",
            host=host.host,
            source=artifact.remote_path,
            dest=dest,
            bytes=artifact.size_bytes,
        )
        cmd = f"cp -f -- {shlex.quote(artifact.remote_path)} {shlex.quote(dest)}"
        result = run_ssh_command(host, cmd, timeout=14400)
        if result.returncode != 0:
            raise RemoteAccessError(result.stderr or result.stdout or f"copy failed: {artifact.filename}")
        copied.append(dest)

    logger.info(
        "copy_artifacts_remote_cp_finished",
        host=host.host,
        target_directory=target_directory,
        file_count=len(copied),
    )
    return copied


async def _copy_artifacts_between_hosts(
    source_host: RemoteHost,
    target_host: RemoteHost,
    artifacts: list[DumpArtifact],
    target_directory: str,
    *,
    chunk_size: int,
) -> list[str]:
    source_conn = await _connect(source_host)
    target_conn = await _connect(target_host)
    copied: list[str] = []
    try:
        source_sftp = await source_conn.start_sftp_client()
        target_sftp = await target_conn.start_sftp_client()
        for artifact in artifacts:
            dest = str(PurePosixPath(target_directory) / artifact.filename)
            await _copy_one(source_sftp, target_sftp, artifact.remote_path, dest, chunk_size)
            copied.append(dest)
            logger.info(
                "copy_artifact_sftp_finished",
                source=artifact.remote_path,
                dest=dest,
                bytes=artifact.size_bytes,
            )
        logger.info(
            "copy_artifacts_sftp_finished",
            target_directory=target_directory,
            file_count=len(copied),
        )
        return copied
    finally:
        source_conn.close()
        target_conn.close()
        await source_conn.wait_closed()
        await target_conn.wait_closed()


async def _copy_one(source_sftp, target_sftp, source_path: str, dest_path: str, chunk_size: int) -> None:
    async with source_sftp.open(source_path, "rb") as src:
        async with target_sftp.open(dest_path, "wb") as dst:
            while True:
                chunk = await src.read(chunk_size)
                if not chunk:
                    break
                await dst.write(chunk)


async def _connect(host: RemoteHost):
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
        return await asyncssh.connect(**kwargs)
    except Exception as e:
        raise RemoteAccessError(f"SFTP connect failed: {e}") from e


def _sudo(command: str, sudo_password: str) -> str:
    if not sudo_password:
        return command
    quoted_pw = shlex.quote(sudo_password)
    return f"printf %s {quoted_pw} | sudo -S sh -lc {shlex.quote(command)}"


def _same_ssh_endpoint(left: RemoteHost, right: RemoteHost) -> bool:
    return (
        left.host == right.host
        and int(left.port) == int(right.port)
        and left.username == right.username
    )
