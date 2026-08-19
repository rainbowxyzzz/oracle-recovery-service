"""远程 Docker 内 Oracle 执行环境配置（恢复服务本机无需安装 Oracle）。"""

from dataclasses import dataclass
from typing import Any


@dataclass
class RemoteDockerOracle:
    """
    DMP 文件所在 Linux 主机 + 其上的 Oracle Docker 容器。

    - dmp_host_path: SSH 可见的宿主机目录（与 remote_directory 通常一致）
    - dmp_container_path: 容器内与 Oracle DIRECTORY 对应的文件系统路径
    - docker_container: 容器名或 ID（docker exec 用）
    """

    docker_container: str
    dmp_host_path: str
    dmp_container_path: str
    oracle_directory: str = "DATA_PUMP_DIR"
    oracle_home: str | None = None
    docker_bin: str = "docker"
    # 可选：docker compose 项目目录（在 SSH 上执行 compose exec 时用）
    docker_compose_service: str | None = None
    docker_compose_dir: str | None = None

    @classmethod
    def from_options(cls, options: dict[str, Any], remote_directory: str) -> "RemoteDockerOracle | None":
        """从 task.options['execution'] 或扁平字段解析。"""
        ex = options.get("execution") or options.get("remote_docker") or {}
        if not ex and not options.get("docker_container"):
            return None

        def _get(key: str, default: str | None = None):
            return ex.get(key) or options.get(key) or default

        container = _get("docker_container")
        if not container:
            return None

        host_path = _get("dmp_host_path") or remote_directory
        container_path = _get("dmp_container_path")
        if not container_path:
            raise ValueError("缺少 dmp_container_path（容器内 DMP 目录，与 DIRECTORY 挂载一致）")

        return cls(
            docker_container=container,
            dmp_host_path=host_path,
            dmp_container_path=container_path.rstrip("/"),
            oracle_directory=_get("oracle_directory", "DATA_PUMP_DIR") or "DATA_PUMP_DIR",
            oracle_home=_get("oracle_home_in_container") or _get("oracle_home"),
            docker_bin=_get("docker_bin", "docker") or "docker",
            docker_compose_service=_get("docker_compose_service"),
            docker_compose_dir=_get("docker_compose_dir"),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.docker_container:
            errors.append("docker_container 不能为空")
        if not self.dmp_host_path:
            errors.append("dmp_host_path 不能为空")
        if not self.dmp_container_path:
            errors.append("dmp_container_path 不能为空")
        return errors
