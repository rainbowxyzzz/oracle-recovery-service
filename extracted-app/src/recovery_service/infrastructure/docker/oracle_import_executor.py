import shlex
import re
from dataclasses import dataclass

from recovery_service.core.domain import RemoteHost, TargetDatabase
from recovery_service.engine.import_command.oracle_commands import (
    ImpCommandSpec,
    ImpdpCommandSpec,
)
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command_stream
from recovery_service.infrastructure.subprocess.safe_runner import ProcessResult
from recovery_service.settings import get_settings


@dataclass(frozen=True)
class OracleDockerRuntime:
    host: RemoteHost
    container: str
    oracle_home: str | None = None
    docker_bin: str = "docker"


class OracleDockerImportExecutor:
    def __init__(self, runtime: OracleDockerRuntime):
        self.runtime = runtime

    def run_imp(
        self,
        target: TargetDatabase,
        *,
        username: str,
        password: str,
        dumpfile: str,
        logfile: str,
        timeout: int,
    ) -> ProcessResult:
        connection = f'{username}/{password}@{target.connection_string}'
        cmd = ImpCommandSpec(
            connection=connection,
            dumpfile=dumpfile,
            logfile=logfile,
        ).to_args()
        return self._run(cmd, timeout=timeout)

    def run_imp_show(
        self,
        target: TargetDatabase,
        *,
        username: str,
        password: str,
        dumpfile: str,
        logfile: str,
        timeout: int,
    ) -> ProcessResult:
        connection = f'{username}/{password}@{target.connection_string}'
        cmd = ImpCommandSpec(
            connection=connection,
            dumpfile=dumpfile,
            logfile=logfile,
            extra_args=["SHOW=Y"],
        ).to_args()
        return self._run(cmd, timeout=timeout)

    def run_impdp(
        self,
        target: TargetDatabase,
        *,
        directory: str,
        dumpfile: str,
        logfile: str,
        username: str,
        timeout: int,
        remap_schemas: list[tuple[str, str]] | None = None,
        remap_tablespace: str | None = None,
        table_exists_action: str = "REPLACE",
        parallel: int | None = None,
        metrics: bool | None = None,
        logtime: str | None = None,
        access_method: str | None = None,
        disable_archive_logging: bool = False,
        exclude_indexes: bool = False,
    ) -> ProcessResult:
        connection = f"{target.admin_user}/{target.admin_password}@{target.connection_string}"
        cmd = ImpdpCommandSpec(
            connection=connection,
            directory=directory,
            dumpfile=dumpfile,
            logfile=logfile,
            remap_schemas=remap_schemas or [],
            remap_tablespace=remap_tablespace,
            table_exists_action=table_exists_action,
            parallel=parallel,
            metrics=metrics,
            logtime=logtime,
            access_method=access_method,
            disable_archive_logging=disable_archive_logging,
            exclude_indexes=exclude_indexes,
        ).to_args()
        return self._run(cmd, timeout=timeout)

    def run_impdp_sqlfile(
        self,
        target: TargetDatabase,
        *,
        directory: str,
        dumpfile: str,
        logfile: str,
        sqlfile: str,
        timeout: int,
    ) -> ProcessResult:
        connection = f"{target.admin_user}/{target.admin_password}@{target.connection_string}"
        cmd = ImpdpCommandSpec(
            connection=connection,
            directory=directory,
            dumpfile=dumpfile,
            logfile=logfile,
            table_exists_action="SKIP",
            extra_args=[f"SQLFILE={sqlfile}"],
        ).to_args()
        return self._run(cmd, timeout=timeout)

    def remove_recovery_datafiles(
        self,
        *,
        tablespace_container_path: str,
        tablespace_name: str,
        timeout: int | None = None,
    ) -> ProcessResult:
        safe_name = _safe_recovery_tablespace_name(tablespace_name)
        safe_dir = _safe_posix_directory(tablespace_container_path)
        pattern = f"{safe_name.lower()}*.dbf"
        inner = (
            self._oracle_env_prefix()
            + "set -eu; "
            + f"dir={shlex.quote(safe_dir)}; "
            + f"pattern={shlex.quote(pattern)}; "
            + "case \"$dir\" in /opt/oracle/recovery_tablespaces|/opt/oracle/recovery_tablespaces/*) ;; "
            + "*) echo \"refusing unsafe dbf cleanup path: $dir\" >&2; exit 64 ;; esac; "
            + "mkdir -p \"$dir\"; "
            + "find \"$dir\" -maxdepth 1 -type f -name \"$pattern\" -print -delete"
        )
        remote_cmd = self._docker_exec(inner)
        result = run_ssh_command(
            self.runtime.host,
            remote_cmd,
            timeout=timeout or get_settings().oracle_import_operation_timeout_seconds,
        )
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[remote_cmd],
        )

    def run_impdp_stream(
        self,
        target: TargetDatabase,
        *,
        directory: str,
        dumpfile: str,
        logfile: str,
        username: str,
        timeout: int,
        on_stdout=None,
        on_stderr=None,
        remap_schemas: list[tuple[str, str]] | None = None,
        remap_tablespace: str | None = None,
        table_exists_action: str = "REPLACE",
        parallel: int | None = None,
        metrics: bool | None = None,
        logtime: str | None = None,
        access_method: str | None = None,
        disable_archive_logging: bool = False,
        exclude_indexes: bool = False,
    ) -> ProcessResult:
        connection = f"{target.admin_user}/{target.admin_password}@{target.connection_string}"
        cmd = ImpdpCommandSpec(
            connection=connection,
            directory=directory,
            dumpfile=dumpfile,
            logfile=logfile,
            remap_schemas=remap_schemas or [],
            remap_tablespace=remap_tablespace,
            table_exists_action=table_exists_action,
            parallel=parallel,
            metrics=metrics,
            logtime=logtime,
            access_method=access_method,
            disable_archive_logging=disable_archive_logging,
            exclude_indexes=exclude_indexes,
        ).to_args()
        return self._run_stream(
            cmd,
            timeout=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )

    def _run(self, command: list[str], *, timeout: int) -> ProcessResult:
        inner = self._oracle_env_prefix() + " ".join(shlex.quote(part) for part in command)
        remote_cmd = self._docker_exec(inner)
        result = run_ssh_command(self.runtime.host, remote_cmd, timeout=timeout)
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[_mask_oracle_connections(remote_cmd)],
        )

    def _run_stream(self, command: list[str], *, timeout: int, on_stdout=None, on_stderr=None) -> ProcessResult:
        inner = self._oracle_env_prefix() + " ".join(shlex.quote(part) for part in command)
        remote_cmd = self._docker_exec(inner)
        result = run_ssh_command_stream(
            self.runtime.host,
            remote_cmd,
            timeout=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[_mask_oracle_connections(remote_cmd)],
        )

    def _docker_exec(self, inner: str) -> str:
        docker = shlex.quote(self.runtime.docker_bin)
        container = shlex.quote(self.runtime.container)
        return f"{docker} exec -i {container} bash -lc {shlex.quote(inner)}"

    def _oracle_env_prefix(self) -> str:
        charset_env = "export NLS_LANG=AMERICAN_AMERICA.AL32UTF8 && export LANG=C.UTF-8 && export LC_ALL=C.UTF-8 && "
        if not self.runtime.oracle_home:
            return charset_env
        oh = shlex.quote(self.runtime.oracle_home)
        return f"export ORACLE_HOME={oh} && export PATH=$ORACLE_HOME/bin:$PATH && {charset_env}"


def _mask_oracle_connections(command: str) -> str:
    return re.sub(
        r"([A-Za-z0-9_$#]+)/([^@\s'\"]+)@",
        r"\1/***@",
        command,
    )


def _safe_recovery_tablespace_name(value: str) -> str:
    upper = value.upper()
    if not re.match(r"^TS_U_[A-Z0-9_$#]{1,25}$", upper):
        raise ValueError(f"Refusing unsafe recovery tablespace cleanup: {value}")
    return upper


def _safe_posix_directory(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError(f"Expected absolute container path: {value}")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("Invalid container path")
    return value.rstrip("/") or "/"
