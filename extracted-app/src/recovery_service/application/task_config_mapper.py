from recovery_service.common.security import decrypt_secret
from recovery_service.core.domain import RemoteHost, TargetDatabase
from recovery_service.domain.import_job import (
    ImportJobConfig,
    OracleDockerTargetConfig,
    SourceServerConfig,
)


class TaskConfigMapper:
    def __init__(self, encryption_key: str):
        self.encryption_key = encryption_key

    def from_professional_config(self, config: dict) -> ImportJobConfig:
        source = config["source"]
        oracle_docker = config["oracle_docker"]
        target_config = config["target"]

        source_host = RemoteHost(
            host=source["host"],
            port=int(source.get("port", 22)),
            username=source["user"],
            password=self._secret(source["password"]),
        )
        oracle_host = RemoteHost(
            host=oracle_docker["host"],
            port=int(oracle_docker.get("port", 22)),
            username=oracle_docker["user"],
            password=self._secret(oracle_docker["password"]),
        )
        target = TargetDatabase(
            connection_string=target_config["connection"],
            admin_user=target_config["admin_user"],
            admin_password=self._secret(target_config["admin_password"]),
            default_temp_tablespace=target_config.get("default_temp_tablespace", "TEMP"),
        )
        return ImportJobConfig(
            source=SourceServerConfig(host=source_host, directory=source["directory"]),
            oracle_docker=OracleDockerTargetConfig(
                host=oracle_host,
                container=oracle_docker["container"],
                dmp_host_path=oracle_docker["dmp_host_path"],
                dmp_container_path=oracle_docker["dmp_container_path"],
                tablespace_container_path=oracle_docker["tablespace_container_path"],
                oracle_directory=oracle_docker.get("oracle_directory"),
                oracle_home_in_container=oracle_docker.get("oracle_home_in_container") or None,
                docker_bin=oracle_docker.get("docker_bin") or "docker",
                chmod_mode=oracle_docker.get("chmod_mode") or "777",
                sudo_password=self._secret(oracle_docker.get("sudo_password", "")),
            ),
            target=target,
            generated_user_password=self._secret(target_config["generated_user_password"]),
            task_id=config.get("_task_id"),
        )

    def _secret(self, value: str | None) -> str:
        if not value:
            return ""
        return decrypt_secret(value, self.encryption_key)
