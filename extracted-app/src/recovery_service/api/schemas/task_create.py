from typing import Any
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from recovery_service.api.schemas.setup import ExecutionDockerConfig


class SshServerConfig(BaseModel):
    host: str
    port: int = 22
    user: str
    password: SecretStr


class SourceDmpConfig(SshServerConfig):
    directory: str = Field(..., description="DMP/log/par directory visible on source server A")


class OracleDockerConfig(SshServerConfig):
    container: str = Field(..., description="Oracle database container name on server B")
    dmp_host_path: str = Field(..., description="Server B host path receiving copied DMP files")
    dmp_container_path: str = Field(
        ..., description="Oracle container path mapped to dmp_host_path"
    )
    oracle_directory: str = "DATA_PUMP_DIR"
    tablespace_container_path: str = Field(
        ..., description="Oracle container path for per-task tablespace datafiles"
    )
    oracle_home_in_container: str | None = None
    docker_bin: str = "docker"
    chmod_mode: str = "777"
    sudo_password: SecretStr | None = None


class TargetOracleConfig(BaseModel):
    connection: str = Field(..., description="Oracle DSN, e.g. host:1521/service")
    admin_user: str
    admin_password: SecretStr
    generated_user_password: SecretStr | None = None
    default_temp_tablespace: str = "TEMP"


class TaskCreateRequest(BaseModel):
    remote_host: str | None = Field(None, description="Legacy DMP file server hostname or IP")
    remote_port: int = 22
    remote_user: str | None = None
    remote_password: SecretStr | None = None
    remote_directory: str | None = Field(None, description="Legacy remote dmp/log/par dir")

    target_connection: str | None = Field(None, description="Legacy Oracle DSN e.g. host:1521/service")
    target_admin_user: str | None = None
    target_admin_password: SecretStr | None = None

    source: SourceDmpConfig | None = None
    oracle_docker: OracleDockerConfig | None = None
    target: TargetOracleConfig | None = None

    options: dict[str, Any] = Field(default_factory=dict)
    execution: ExecutionDockerConfig | None = Field(
        None,
        description="Oracle 在 DMP 服务器 Docker 内时必填；也可写在 options.execution",
    )
    volume_group_index: int = 0
    auto_confirm: bool | None = Field(
        None, description="If true, run full import; else metadata-only until confirmed"
    )

    @model_validator(mode="after")
    def validate_legacy_or_professional(self) -> "TaskCreateRequest":
        has_professional = self.source is not None or self.oracle_docker is not None or self.target is not None
        if has_professional:
            missing = [
                name
                for name, value in (
                    ("source", self.source),
                    ("oracle_docker", self.oracle_docker),
                    ("target", self.target),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"Missing professional flow config: {', '.join(missing)}")
            return self

        missing_legacy = [
            name
            for name, value in (
                ("remote_host", self.remote_host),
                ("remote_user", self.remote_user),
                ("remote_password", self.remote_password),
                ("remote_directory", self.remote_directory),
                ("target_connection", self.target_connection),
                ("target_admin_user", self.target_admin_user),
                ("target_admin_password", self.target_admin_password),
            )
            if value is None
        ]
        if missing_legacy:
            raise ValueError(f"Missing legacy task config: {', '.join(missing_legacy)}")
        return self


class EmbeddedOracleTaskCreateRequest(BaseModel):
    source: SourceDmpConfig | None = None
    target_connection_id: UUID | None = None
    oracle_host: str | None = Field(
        None,
        description="Oracle Docker host SSH address; defaults to ORACLE_DOCKER_HOST",
    )
    oracle_port: int | None = None
    oracle_user: str | None = None
    oracle_password: SecretStr | None = Field(
        None,
        description="Oracle Docker host SSH password; defaults to ORACLE_DOCKER_SSH_PASSWORD",
    )
    oracle_sudo_password: SecretStr | None = None
    generated_user_password: SecretStr | None = None
    import_source_mode: str = Field(
        "copy",
        description="copy: copy files from source server; direct: use files already in Oracle DMP host path",
    )
    manual_dumpfile: str | None = Field(
        None,
        description="Oracle DUMPFILE pattern/name when import_source_mode=direct, e.g. cqdsj_20260701_180002_%U.dmp",
    )
    direct_dmp_host_path: str | None = Field(
        None,
        description="Oracle Docker host DMP directory to use directly; defaults to ORACLE_DMP_HOST_PATH",
    )
    impdp_access_method: str | None = Field(
        None,
        description="Optional impdp ACCESS_METHOD such as DIRECT_PATH",
    )
    impdp_parallel: int | None = Field(None, ge=1, le=128)
    impdp_metrics: bool | None = None
    impdp_logtime: str | None = Field(None, description="NONE, STATUS, LOGFILE, or ALL")
    impdp_disable_archive_logging: bool = False
    impdp_table_exists_action: str | None = Field(None, description="SKIP, APPEND, TRUNCATE, or REPLACE")
    impdp_index_mode: str = Field("default", description="default or exclude")
    accept_export_log_gaps: bool = Field(
        False,
        description="Allow import when an exactly matched Oracle export log reports source objects missing from the dump set",
    )
    volume_group_index: int = 0
    auto_confirm: bool = True

    @field_validator("import_source_mode")
    @classmethod
    def validate_import_source_mode(cls, value: str) -> str:
        normalized = (value or "copy").strip().lower()
        if normalized not in {"copy", "direct"}:
            raise ValueError("import_source_mode must be copy or direct")
        return normalized

    @model_validator(mode="after")
    def validate_source_mode(self) -> "EmbeddedOracleTaskCreateRequest":
        if self.import_source_mode == "copy" and self.source is None:
            raise ValueError("source is required when import_source_mode=copy")
        if self.import_source_mode == "direct" and not (self.manual_dumpfile or "").strip():
            raise ValueError("manual_dumpfile is required when import_source_mode=direct")
        return self


class EmbeddedSqlServerTaskCreateRequest(BaseModel):
    source: SourceDmpConfig
    target_connection_id: UUID | None = None
    sqlserver_host: str | None = Field(
        None,
        description="SQL Server Docker host SSH address; defaults to SQLSERVER_DOCKER_HOST",
    )
    sqlserver_port: int | None = None
    sqlserver_user: str | None = None
    sqlserver_password: SecretStr | None = Field(
        None,
        description="SQL Server Docker host SSH password; defaults to SQLSERVER_DOCKER_SSH_PASSWORD",
    )
    sqlserver_sudo_password: SecretStr | None = None
    sa_password: SecretStr | None = None
    volume_group_index: int = 0
    auto_confirm: bool = True


class EmbeddedMySqlTaskCreateRequest(BaseModel):
    source: SourceDmpConfig
    target_connection_id: UUID | None = None
    mysql_host: str | None = Field(
        None,
        description="MySQL restore Docker host SSH address; defaults to MYSQL_RESTORE_DOCKER_HOST",
    )
    mysql_port: int | None = None
    mysql_user: str | None = None
    mysql_password: SecretStr | None = Field(
        None,
        description="MySQL restore Docker host SSH password; defaults to MYSQL_RESTORE_DOCKER_SSH_PASSWORD",
    )
    mysql_sudo_password: SecretStr | None = None
    root_password: SecretStr | None = None
    target_database: str | None = None
    drop_existing: bool = True
    volume_group_index: int = 0
    auto_confirm: bool = True


class BatchCreateRequest(BaseModel):
    remote_host: str
    remote_port: int = 22
    remote_user: str
    remote_password: SecretStr
    remote_directory: str
    target_connection: str
    target_admin_user: str
    target_admin_password: SecretStr
    options: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    id: UUID
    state: str
    current_policy_node: str | None = None
    progress_percent: float = 0.0
    error_message: str | None = None
    metadata_snapshot: dict = Field(default_factory=dict)
    celery_task_id: str | None = None
    remote_host: str | None = None
    remote_directory: str | None = None
    target_connection: str | None = None
    target_schema: str | None = None
    import_tool: str | None = None
    engine: str | None = None
    stop_requested: bool = False
    force_stop_requested: bool = False
    stop_reason: str | None = None
    stop_requested_at: datetime | None = None
    stopped_at: datetime | None = None
    oracle_run_id: str | None = None
    oracle_job_name: str | None = None
    oracle_container: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskStopRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
    force: bool = False


class TaskEventResponse(BaseModel):
    id: int
    event_type: str
    title: str
    status: str
    message: str | None = None
    payload: dict = Field(default_factory=dict)
    stdout: str | None = None
    stderr: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskDetailResponse(BaseModel):
    task: TaskResponse
    events: list[TaskEventResponse] = Field(default_factory=list)
