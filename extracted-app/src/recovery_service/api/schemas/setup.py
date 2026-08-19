from typing import Any

from pydantic import BaseModel, Field, SecretStr


class ExecutionDockerConfig(BaseModel):
    """Oracle 跑在 DMP 服务器的 Docker 里时使用。"""

    mode: str = Field("remote_docker", description="固定为 remote_docker")
    docker_container: str = Field(..., description="容器名或 ID，如 oracle19c")
    dmp_host_path: str | None = Field(
        None, description="宿主机 DMP 目录，默认同 remote_directory"
    )
    dmp_container_path: str = Field(
        ..., description="容器内路径，须与 DIRECTORY 挂载一致，如 /opt/oracle/admin/ORCL/dpdump"
    )
    oracle_directory: str = Field("DATA_PUMP_DIR", description="Oracle DIRECTORY 对象名")
    oracle_home_in_container: str | None = Field(
        None, description="容器内 ORACLE_HOME，如 /opt/oracle/product/19c/dbhome_1"
    )
    docker_bin: str = "docker"
    docker_compose_service: str | None = None
    docker_compose_dir: str | None = None


class SetupCheckRequest(BaseModel):
    """分步检测入参 — 按你环境逐项填写。"""

    # SSH / DMP 服务器
    ssh_host: str = Field(..., description="DMP 文件服务器 IP")
    ssh_port: int = 22
    ssh_user: str
    ssh_password: SecretStr | None = None
    ssh_private_key_path: str | None = None

    dmp_host_path: str | None = Field(None, description="宿主机上 DMP 目录")
    remote_directory: str | None = Field(None, description="同 dmp_host_path，二选一")

    execution: ExecutionDockerConfig | None = None

    # 目标库（19c）
    target_connection: str | None = Field(None, description="host:1521/service")
    target_admin_user: str | None = None
    target_admin_password: SecretStr | None = None

    stop_on_first_error: bool = True


class SetupStepResponse(BaseModel):
    step: str
    ok: bool
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class SetupCheckAllResponse(BaseModel):
    results: list[SetupStepResponse]
    all_passed: bool


class ConfigTemplateResponse(BaseModel):
    description: str
    task_example: dict[str, Any]
    env_variables: dict[str, str]
