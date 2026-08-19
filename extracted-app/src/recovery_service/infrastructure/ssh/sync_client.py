"""Synchronous SSH wrapper for Celery workers."""

from recovery_service.core.domain import DumpArtifact, RemoteHost
from recovery_service.infrastructure.ssh.async_client import AsyncSSHClient


def list_remote_artifacts(host: RemoteHost, remote_dir: str) -> list[DumpArtifact]:
    import asyncio

    async def _run():
        client = AsyncSSHClient(host)
        try:
            await client.connect()
            return await client.list_directory(remote_dir)
        finally:
            await client.close()

    return asyncio.run(_run())


def read_remote_text(host: RemoteHost, remote_path: str, max_bytes: int = 2_000_000) -> str:
    import asyncio

    async def _run():
        client = AsyncSSHClient(host)
        try:
            await client.connect()
            return await client.read_text_file(remote_path, max_bytes=max_bytes)
        finally:
            await client.close()

    return asyncio.run(_run())
