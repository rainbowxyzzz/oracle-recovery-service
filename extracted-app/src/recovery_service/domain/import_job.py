from dataclasses import dataclass

from recovery_service.core.domain import RemoteHost, TargetDatabase


@dataclass(frozen=True)
class SourceServerConfig:
    host: RemoteHost
    directory: str


@dataclass(frozen=True)
class OracleDockerTargetConfig:
    host: RemoteHost
    container: str
    dmp_host_path: str
    dmp_container_path: str
    tablespace_container_path: str
    oracle_directory: str | None = None
    oracle_home_in_container: str | None = None
    docker_bin: str = "docker"
    chmod_mode: str = "777"
    sudo_password: str = ""


@dataclass(frozen=True)
class ImportJobConfig:
    source: SourceServerConfig
    oracle_docker: OracleDockerTargetConfig
    target: TargetDatabase
    generated_user_password: str
    task_id: str | None = None


@dataclass(frozen=True)
class ResolvedImportTarget:
    schema: str
    tablespace: str
    directory: str
