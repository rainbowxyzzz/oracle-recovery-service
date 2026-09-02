"""在 DMP 服务器的 Docker 容器内执行 impdp（恢复服务部署机无需 Oracle）。"""

import shlex

from recovery_service.core.docker_oracle import RemoteDockerOracle
from recovery_service.core.domain import ImpdpParams, RemoteHost
from recovery_service.core.exceptions import ImpdpError
from recovery_service.infrastructure.ssh.command_runner import SSHCommandResult, run_ssh_command
from recovery_service.infrastructure.subprocess.safe_runner import ProcessResult
from recovery_service.settings import get_settings


class RemoteDockerImpdpExecutor:
    def __init__(self, ssh_host: RemoteHost, docker: RemoteDockerOracle):
        self.ssh_host = ssh_host
        self.docker = docker
        settings = get_settings()
        self.impdp_bin = "impdp"
        self._mask = settings.load_yaml("default.yaml").get("security", {}).get("mask_patterns")

    def _wrap_docker_exec(self, inner_shell: str) -> str:
        d = self.docker
        container = shlex.quote(d.docker_container)
        docker = shlex.quote(d.docker_bin)

        if d.docker_compose_service and d.docker_compose_dir:
            compose_dir = shlex.quote(d.docker_compose_dir)
            svc = shlex.quote(d.docker_compose_service)
            return (
                f"cd {compose_dir} && docker compose exec -T {svc} bash -lc {shlex.quote(inner_shell)}"
            )

        return f"{docker} exec -i {container} bash -lc {shlex.quote(inner_shell)}"

    def _build_inner_impdp(self, params: ImpdpParams) -> str:
        parts = [self.impdp_bin, params.connection]
        parts.extend(params.to_cli_args())
        # log/sql 输出到容器内 DMP 目录
        return " ".join(shlex.quote(p) for p in parts)

    def _oracle_env_prefix(self) -> str:
        charset_env = "export NLS_LANG=AMERICAN_AMERICA.AL32UTF8 && export LANG=C.UTF-8 && export LC_ALL=C.UTF-8 && "
        if self.docker.oracle_home:
            oh = shlex.quote(self.docker.oracle_home)
            return f"export ORACLE_HOME={oh} && export PATH=$ORACLE_HOME/bin:$PATH && {charset_env}"
        return charset_env

    def run_import(
        self,
        params: ImpdpParams,
        *,
        timeout: int | None = None,
        allow_failure: bool = False,
    ) -> ProcessResult:
        if "@" not in params.connection and "/" not in params.connection:
            raise ImpdpError("connection must be user/pass@dsn for impdp")

        inner = self._oracle_env_prefix() + self._build_inner_impdp(params)
        remote_cmd = self._wrap_docker_exec(inner)
        result = run_ssh_command(
            self.ssh_host,
            remote_cmd,
            timeout=timeout or get_settings().oracle_import_operation_timeout_seconds,
        )

        proc = ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=[remote_cmd],
        )
        if proc.returncode != 0 and not allow_failure:
            raise ImpdpError(
                f"remote docker impdp failed: exit {proc.returncode}",
                stderr=proc.stderr,
                return_code=proc.returncode,
            )
        return proc

    def check_container(self) -> SSHCommandResult:
        cmd = self._wrap_docker_exec("echo ORACLE_DOCKER_OK")
        return run_ssh_command(self.ssh_host, cmd, timeout=get_settings().oracle_ssh_check_timeout_seconds)

    def list_container_dmp_dir(self) -> SSHCommandResult:
        path = shlex.quote(self.docker.dmp_container_path)
        cmd = self._wrap_docker_exec(f"ls -la {path}")
        return run_ssh_command(self.ssh_host, cmd, timeout=get_settings().oracle_ssh_check_timeout_seconds)

    def impdp_help(self) -> SSHCommandResult:
        inner = self._oracle_env_prefix() + f"{self.impdp_bin} help=y 2>&1 | head -20"
        return run_ssh_command(
            self.ssh_host,
            self._wrap_docker_exec(inner),
            timeout=get_settings().oracle_ssh_check_timeout_seconds,
        )
