"""Async SSH client for remote directory listing — never reads .dmp binary content."""

import stat
from pathlib import PurePosixPath

import asyncssh

from recovery_service.core.domain import DumpArtifact, RemoteHost
from recovery_service.core.exceptions import RemoteAccessError


class AsyncSSHClient:
    def __init__(self, host: RemoteHost):
        self.host = host
        self._conn: asyncssh.SSHClientConnection | None = None
        self._sftp = None

    async def connect(self) -> None:
        kwargs: dict = {
            "host": self.host.host,
            "port": self.host.port,
            "username": self.host.username,
            "known_hosts": None,
        }
        if self.host.private_key_path:
            kwargs["client_keys"] = [self.host.private_key_path]
        else:
            kwargs["password"] = self.host.password
        try:
            self._conn = await asyncssh.connect(**kwargs)
        except Exception as e:
            raise RemoteAccessError(f"SSH connect failed: {e}") from e

    async def close(self) -> None:
        if self._sftp:
            try:
                # asyncssh's SFTPClient.exit() closes synchronously and returns None.
                self._sftp.exit()
            finally:
                self._sftp = None
        if self._conn:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def _get_sftp(self):
        if not self._conn:
            await self.connect()
        assert self._conn
        if self._sftp is None:
            self._sftp = await self._conn.start_sftp_client()
        return self._sftp

    async def list_directory(self, remote_dir: str) -> list[DumpArtifact]:
        if not self._conn:
            await self.connect()
        assert self._conn
        try:
            entries = await self._conn.run(f"ls -la {remote_dir}", check=True)
        except asyncssh.ProcessError as e:
            raise RemoteAccessError(f"ls failed: {e}") from e

        artifacts: list[DumpArtifact] = []
        for line in (entries.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 9 or parts[0].startswith("d"):
                continue
            name = parts[-1]
            if name in (".", ".."):
                continue
            lower = name.lower()
            if not (lower.endswith(".dmp") or lower.endswith(".log") or lower.endswith(".par")):
                continue
            try:
                size = int(parts[4])
            except ValueError:
                size = 0
            remote_path = str(PurePosixPath(remote_dir) / name)
            artifacts.append(DumpArtifact(remote_path=remote_path, filename=name, size_bytes=size))
        return artifacts

    async def read_text_file(self, remote_path: str, max_bytes: int = 2_000_000) -> str:
        """Read log/par text files only."""
        if not self._conn:
            await self.connect()
        assert self._conn
        lower = remote_path.lower()
        if lower.endswith(".dmp"):
            raise RemoteAccessError("Refusing to read binary dump file content")
        try:
            sftp = await self._get_sftp()
            async with sftp.open(remote_path, "rb") as f:
                data = await f.read(max_bytes)
            return _decode_text(data)
        except Exception as e:
            raise RemoteAccessError(f"read failed: {e}") from e

    async def read_binary_file(self, remote_path: str, max_bytes: int = 100_000_000) -> bytes:
        if not self._conn:
            await self.connect()
        assert self._conn
        try:
            sftp = await self._get_sftp()
            attrs = await sftp.stat(remote_path)
            size = int(attrs.size or 0)
            if size > max_bytes:
                raise RemoteAccessError(
                    f"remote file exceeds download limit: {size} > {max_bytes} bytes"
                )
            async with sftp.open(remote_path, "rb") as f:
                data = await f.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise RemoteAccessError(f"remote file exceeds download limit: {max_bytes} bytes")
            return data
        except RemoteAccessError:
            raise
        except Exception as e:
            raise RemoteAccessError(f"read failed: {e}") from e

    async def list_files_recursive(self, remote_dir: str, max_files: int = 1000) -> list[dict]:
        if not self._conn:
            await self.connect()
        assert self._conn
        try:
            sftp = await self._get_sftp()
            root = PurePosixPath(remote_dir)
            pending = [root]
            files: list[dict] = []
            while pending:
                current = pending.pop()
                for name in await sftp.listdir(str(current)):
                    if name in (".", ".."):
                        continue
                    path = current / name
                    attrs = await sftp.lstat(str(path))
                    mode = int(attrs.permissions or 0)
                    if stat.S_ISLNK(mode):
                        continue
                    if stat.S_ISDIR(mode):
                        pending.append(path)
                        continue
                    if not stat.S_ISREG(mode):
                        continue
                    files.append(
                        {
                            "relative_path": path.relative_to(root).as_posix(),
                            "remote_path": str(path),
                            "size_bytes": int(attrs.size or 0),
                            "modified_epoch": float(attrs.mtime or 0),
                        }
                    )
                    if len(files) > max_files:
                        raise RemoteAccessError(
                            f"remote directory contains more than {max_files} files"
                        )
            return sorted(files, key=lambda item: item["relative_path"])
        except RemoteAccessError:
            raise
        except Exception as e:
            raise RemoteAccessError(f"list files failed: {e}") from e


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
