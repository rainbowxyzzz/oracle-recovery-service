import re
import shlex
from dataclasses import dataclass

from recovery_service.core.domain import RemoteHost
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command
from recovery_service.infrastructure.subprocess.safe_runner import ProcessResult


@dataclass(frozen=True)
class SqlServerDockerRuntime:
    host: RemoteHost
    container: str
    sa_password: str
    docker_bin: str = "docker"


class SqlServerDockerExecutor:
    def __init__(self, runtime: SqlServerDockerRuntime):
        self.runtime = runtime

    def run_sql(self, sql: str, *, timeout: int = 14400) -> ProcessResult:
        sqlcmd = (
            "SQLCMD=$(command -v sqlcmd || command -v /opt/mssql-tools18/bin/sqlcmd "
            "|| command -v /opt/mssql-tools/bin/sqlcmd); "
            'if [ -z "$SQLCMD" ]; then echo "sqlcmd not found" >&2; exit 127; fi; '
        )
        inner = (
            sqlcmd
            + '"$SQLCMD" -S 127.0.0.1 -U SA '
            + f"-P {shlex.quote(self.runtime.sa_password)} -C -b -Q {shlex.quote(sql)}"
            + " -h -1 -W -s '|'"
        )
        remote_cmd = self._docker_exec(inner)
        result = run_ssh_command(self.runtime.host, remote_cmd, timeout=timeout)
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[_mask_sa_password(remote_cmd)],
        )

    def list_container_dir(self, path: str, *, timeout: int = 300) -> ProcessResult:
        remote_cmd = self._docker_exec(f"ls -la {shlex.quote(path)}")
        result = run_ssh_command(self.runtime.host, remote_cmd, timeout=timeout)
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[remote_cmd],
        )

    def _docker_exec(self, inner: str) -> str:
        return (
            f"{shlex.quote(self.runtime.docker_bin)} exec -i "
            f"{shlex.quote(self.runtime.container)} bash -lc {shlex.quote(inner)}"
        )


def _mask_sa_password(command: str) -> str:
    return re.sub(r"(-P\s+)(\S+)", r"\1***", command)
