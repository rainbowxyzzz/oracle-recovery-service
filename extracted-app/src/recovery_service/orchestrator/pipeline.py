from dataclasses import asdict

from recovery_service.common.logging import get_logger
from recovery_service.common.security import decrypt_secret
from recovery_service.core.docker_oracle import RemoteDockerOracle
from recovery_service.core.domain import RemoteHost, TargetDatabase
from recovery_service.core.exceptions import DiscoveryError
from recovery_service.engine.discovery.remote_scanner import RemoteScanner
from recovery_service.engine.policy_tree import PolicyContext, PolicyTreeEngine
from recovery_service.orchestrator.mysql_pipeline import MySqlRecoveryPipeline
from recovery_service.orchestrator.professional_pipeline import ProfessionalRecoveryPipeline
from recovery_service.orchestrator.sqlserver_pipeline import SqlServerRecoveryPipeline
from recovery_service.settings import get_settings

logger = get_logger(__name__)


class RecoveryPipeline:
    def __init__(self):
        self.scanner = RemoteScanner()
        self.engine = PolicyTreeEngine()

    def run_task(
        self,
        *,
        remote_host: str,
        remote_port: int,
        remote_user: str,
        remote_password: str,
        remote_directory: str,
        target_connection: str,
        target_admin_user: str,
        target_admin_password: str,
        options: dict | None = None,
        volume_group_index: int = 0,
    ) -> dict:
        options = options or {}
        if options.get("professional_flow"):
            professional_config = dict(options["professional_flow"])
            if options.get("_task_id"):
                professional_config["_task_id"] = options["_task_id"]
            if "auto_confirm" in options:
                professional_config["auto_confirm"] = options["auto_confirm"]
            return ProfessionalRecoveryPipeline().run(
                professional_config,
                volume_group_index=volume_group_index,
            )
        if options.get("sqlserver_flow"):
            sqlserver_config = dict(options["sqlserver_flow"])
            if options.get("_task_id"):
                sqlserver_config["_task_id"] = options["_task_id"]
            return SqlServerRecoveryPipeline().run(
                sqlserver_config,
                volume_group_index=volume_group_index,
            )
        if options.get("mysql_flow"):
            mysql_config = dict(options["mysql_flow"])
            if options.get("_task_id"):
                mysql_config["_task_id"] = options["_task_id"]
            return MySqlRecoveryPipeline().run(
                mysql_config,
                volume_group_index=volume_group_index,
            )

        settings = get_settings()
        enc_key = settings.credential_encryption_key
        host = RemoteHost(
            host=remote_host,
            port=remote_port,
            username=remote_user,
            password=decrypt_secret(remote_password, enc_key),
        )
        target = TargetDatabase(
            connection_string=target_connection,
            admin_user=target_admin_user,
            admin_password=decrypt_secret(target_admin_password, enc_key),
            default_tablespace=options.get("default_tablespace", "USERS") if options else "USERS",
        )
        scan_path = remote_directory
        docker_cfg = RemoteDockerOracle.from_options(options, remote_directory)
        if docker_cfg:
            scan_path = docker_cfg.dmp_host_path
            if not options.get("execution"):
                options["execution"] = {
                    "mode": "remote_docker",
                    "docker_container": docker_cfg.docker_container,
                    "dmp_host_path": docker_cfg.dmp_host_path,
                    "dmp_container_path": docker_cfg.dmp_container_path,
                    "oracle_directory": docker_cfg.oracle_directory,
                    "oracle_home_in_container": docker_cfg.oracle_home,
                }

        groups = self.scanner.scan(host, scan_path)
        if volume_group_index >= len(groups):
            raise DiscoveryError(f"volume_group_index {volume_group_index} out of range")

        group = groups[volume_group_index]
        ctx = PolicyContext(
            host=host, target=target, group=group, options=options, remote_directory=scan_path
        )
        result = self.engine.run(ctx)

        return {
            "state": result.state.value,
            "success": result.success,
            "message": result.message,
            "metadata": _metadata_to_dict(result.metadata),
            "group_id": group.group_id,
            "correction_attempts": ctx.correction_attempts,
        }


def _metadata_to_dict(meta) -> dict | None:
    if not meta:
        return None
    d = asdict(meta)
    d["export_mode"] = meta.export_mode.value
    return d
