import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from collections.abc import Callable

from recovery_service.core.domain import RemoteHost
from recovery_service.infrastructure.ssh.command_runner import (
    run_ssh_command,
    run_ssh_command_stream,
)
from recovery_service.infrastructure.subprocess.safe_runner import ProcessResult


@dataclass(frozen=True)
class MySqlDockerRuntime:
    host: RemoteHost
    container: str
    root_password: str
    docker_bin: str = "docker"


class MySqlDockerExecutor:
    def __init__(self, runtime: MySqlDockerRuntime):
        self.runtime = runtime

    def run_sql(self, sql: str, *, database: str | None = None, timeout: int = 3600) -> ProcessResult:
        inner = self._mysql_command(database=database, extra="-e " + shlex.quote(sql))
        result = run_ssh_command(self.runtime.host, self._docker_exec(inner), timeout=timeout)
        command = _mask_mysql_password(result.command, self.runtime.root_password)
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[command],
        )

    def import_dump(
        self,
        dump_path: str,
        *,
        database: str,
        method: str,
        timeout: int = 14400,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        stream = self._dump_stream_command(dump_path, method)
        sanitizer = (
            r"sed -E "
            r"-e '/^CREATE DATABASE /Id' "
            r"-e '/^CREATE SCHEMA /Id' "
            r"-e '/^USE /Id' "
            r"-e '/^SET @@GLOBAL.GTID_PURGED=/d' "
            r"-e 's|DEFINER=`[^`]+`@`[^`]+`||g' "
            r"-e 's|DEFINER=[^* ]+||g'"
        )
        prelude = (
            "printf '%s\\n' "
            "'SET SESSION FOREIGN_KEY_CHECKS=0;' "
            "'SET SESSION UNIQUE_CHECKS=0;' "
            "'SET SESSION SQL_LOG_BIN=0;'"
        )
        epilogue = (
            "printf '%s\\n' "
            "'SET SESSION FOREIGN_KEY_CHECKS=1;' "
            "'SET SESSION UNIQUE_CHECKS=1;'"
        )
        mysql = self._mysql_command(
            database=database,
            extra="--binary-mode=1 --show-warnings --default-character-set=utf8mb4",
        )
        inner = f"set -o pipefail; ( {prelude}; {stream} | {sanitizer}; {epilogue} ) | {mysql}"
        result = run_ssh_command_stream(
            self.runtime.host,
            self._docker_exec(inner),
            timeout=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        command = _mask_mysql_password(result.command, self.runtime.root_password)
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[command],
        )

    def list_container_dir(self, path: str, *, timeout: int = 300) -> ProcessResult:
        result = run_ssh_command(
            self.runtime.host,
            self._docker_exec(f"ls -la {shlex.quote(path)}"),
            timeout=timeout,
        )
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[result.command],
        )

    def _mysql_command(self, *, database: str | None, extra: str = "") -> str:
        database_arg = shlex.quote(database) if database else ""
        return (
            "mysql -h127.0.0.1 -uroot "
            f"-p{shlex.quote(self.runtime.root_password)} "
            f"{extra} {database_arg}"
        ).strip()

    def _dump_stream_command(self, dump_path: str, method: str) -> str:
        q_path = shlex.quote(dump_path)
        if method == "sql_gzip":
            return f"gzip -dc {q_path}"
        if method == "sql_zip":
            return (
                "command -v unzip >/dev/null 2>&1 "
                '|| { echo "unzip not found in MySQL container" >&2; exit 127; }; '
                f"unzip -p {q_path} '*.sql'"
            )
        return f"cat {q_path}"

    def _docker_exec(self, inner: str) -> str:
        return (
            f"{shlex.quote(self.runtime.docker_bin)} exec -i "
            f"{shlex.quote(self.runtime.container)} bash -lc {shlex.quote(inner)}"
        )


def mysql_container_path(directory: str, filename: str) -> str:
    return str(PurePosixPath(directory) / filename)


def _mask_mysql_password(command: str, password: str = "") -> str:
    masked = re.sub(r"(-p)(?:'[^']*'|\"[^\"]*\"|\S+)", r"\1***", command)
    return masked.replace(password, "***") if password else masked
