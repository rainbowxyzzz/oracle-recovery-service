import shlex
from dataclasses import dataclass
from importlib import import_module
from pathlib import PurePosixPath

from recovery_service.common.logging import get_logger
from recovery_service.core.domain import DumpArtifact, DumpVolumeGroup, RemoteHost, TargetDatabase
from recovery_service.core.enums import DumpVolumeType, TaskState
from recovery_service.core.exceptions import RemoteAccessError
from recovery_service.application.task_config_mapper import TaskConfigMapper
from recovery_service.engine.discovery.remote_scanner import RemoteScanner
from recovery_service.engine.import_result.classifier import (
    ImportResultClassification,
    classify_import_result,
)
from recovery_service.engine.oracle.dump_detector import (
    OracleDumpDecision,
    detect_from_text,
    detect_with_probe,
    enrich_with_metadata_probe,
)
from recovery_service.engine.oracle.error_decision_tree import decide_oracle_repairs
from recovery_service.engine.oracle.export_log_parser import (
    ExportLogBinding,
    OracleExportLogManifest,
    bind_export_log,
    parse_oracle_export_log,
)
from recovery_service.engine.oracle.metadata_analyzer import analyze_oracle_metadata
from recovery_service.engine.oracle.result_validator import validate_oracle_import
from recovery_service.engine.remap.schema_remap import (
    build_schema_remap_plan,
    extract_source_schemas,
    format_remap_schemas,
    merge_remap_schemas,
)
from recovery_service.infrastructure.docker.oracle_import_executor import (
    OracleDockerImportExecutor,
    OracleDockerRuntime,
)
from recovery_service.infrastructure.oracle.admin import (
    derive_identifier,
    ensure_recovery_prerequisites,
    grow_tablespace_for_import,
    reset_recovery_target,
)
from recovery_service.infrastructure.ssh.file_transfer import (
    chmod_remote_tree,
    copy_artifacts_between_hosts,
    ensure_remote_directory,
)
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command
from recovery_service.infrastructure.ssh.sync_client import list_remote_artifacts, read_remote_text
from recovery_service.orchestrator.oracle_auto_import_runner import OracleAutoImportRunner
from recovery_service.services.task_events import record_task_event, update_oracle_runtime_control
from recovery_service.settings import get_settings

logger = get_logger(__name__)

MAX_EXPORT_LOG_BYTES = 20_000_000


@dataclass
class MatchedExportLog:
    artifact: DumpArtifact
    manifest: OracleExportLogManifest
    binding: ExportLogBinding

import_plan = import_module("recovery_service.engine.import.import_plan")
should_retry_with_impdp = import_plan.should_retry_with_impdp
ImportPlan = import_plan.ImportPlan


class ProfessionalRecoveryPipeline:
    def __init__(self):
        self.scanner = RemoteScanner()

    def run(self, config: dict, *, volume_group_index: int = 0) -> dict:
        settings = get_settings()
        job = TaskConfigMapper(settings.credential_encryption_key).from_professional_config(config)
        task_id = job.task_id

        source = config["source"]
        oracle_docker = config["oracle_docker"]
        target_config = config["target"]
        import_source = config.get("import_source") or {}
        import_source_mode = (import_source.get("mode") or "copy").lower()
        manual_dumpfile = (import_source.get("manual_dumpfile") or "").strip()
        impdp_options = config.get("impdp") or {}
        impdp_options = _normalize_impdp_options(impdp_options)
        direct_import = import_source_mode == "direct"
        execute_import = bool(config.get("auto_confirm", True))
        accept_export_log_gaps = bool(import_source.get("accept_export_log_gaps", False))
        matched_export_log: MatchedExportLog | None = None
        export_log_candidates: list[dict] = []
        record_task_event(
            task_id,
            event_type="prepare",
            title="读取任务配置",
            status="succeeded",
            message="已读取源服务器、Oracle Docker 和目标库配置。",
            payload={
                "source_host": source["host"],
                "source_directory": source["directory"],
                "import_source_mode": import_source_mode,
                "manual_dumpfile": manual_dumpfile,
                "oracle_host": oracle_docker["host"],
                "oracle_container": oracle_docker["container"],
                "target_connection": target_config["connection"],
            },
        )

        source_host = job.source.host
        oracle_host = job.oracle_docker.host
        target = job.target

        source_directory = job.source.directory
        if direct_import:
            record_task_event(
                task_id,
                event_type="discover",
                title="校验 Oracle DMP 直读目录",
                status="running",
                message=(
                    f"直接使用 Oracle 宿主机目录 {job.oracle_docker.dmp_host_path}，"
                    f"DUMPFILE={manual_dumpfile}，本次不会从源服务器复制文件。"
                ),
                payload={
                    "dmp_host_path": job.oracle_docker.dmp_host_path,
                    "dmp_container_path": job.oracle_docker.dmp_container_path,
                    "manual_dumpfile": manual_dumpfile,
                },
            )
            group, flat_artifacts, direct_payload = _build_direct_dump_group(
                oracle_host,
                dmp_host_path=job.oracle_docker.dmp_host_path,
                manual_dumpfile=manual_dumpfile,
            )
            matched_export_log, export_log_candidates = _select_export_log(
                oracle_host,
                directory=job.oracle_docker.dmp_host_path,
                dump_files=group.dump_files,
            )
            if matched_export_log:
                group.log_files = [matched_export_log.artifact]
                flat_artifacts = [*flat_artifacts, matched_export_log.artifact]
                direct_payload["export_log"] = matched_export_log.artifact.filename
                direct_payload["export_log_binding"] = matched_export_log.binding.to_dict()
            record_task_event(
                task_id,
                event_type="discover",
                title="Oracle DMP 直读目录校验完成",
                status="succeeded",
                message=f"匹配到 {len(group.dump_files)} 个 DMP 文件并绑定导出日志，后续直接执行导入。",
                payload=direct_payload,
            )
            related_text = ""
        else:
            record_task_event(
                task_id,
                event_type="discover",
                title="扫描 DMP 目录",
                status="running",
                message=f"开始扫描源目录：{source_directory}",
            )
            all_artifacts = self.scanner.scan(source_host, source_directory)
            if volume_group_index >= len(all_artifacts):
                raise IndexError(f"volume_group_index {volume_group_index} out of range")

            group = all_artifacts[volume_group_index]
            matched_export_log, export_log_candidates = _select_export_log(
                source_host,
                directory=source_directory,
                dump_files=group.dump_files,
                log_files=group.log_files,
            )
            if matched_export_log:
                group.log_files = [matched_export_log.artifact]
            flat_artifacts = _flatten_groups(all_artifacts)
            record_task_event(
                task_id,
                event_type="discover",
                title="扫描 DMP 目录完成",
                status="succeeded",
                message=f"发现 {len(all_artifacts)} 个导入分组，本次选择第 {volume_group_index} 个。",
                payload={
                    "group_id": group.group_id,
                    "dump_files": [a.filename for a in group.dump_files],
                    "log_files": [a.filename for a in group.log_files],
                    "par_files": [a.filename for a in group.par_files],
                    "all_file_count": len(flat_artifacts),
                },
            )
            related_text = "" if matched_export_log else _read_related_text(source_host, group)

        export_log_manifest = matched_export_log.manifest if matched_export_log else None
        if export_log_candidates:
            record_task_event(
                task_id,
                event_type="oracle_export_log",
                title=("Oracle 导出日志专项解析完成" if matched_export_log else "Oracle 导出日志未进入专项流程"),
                status="succeeded" if matched_export_log else "succeeded_with_warnings",
                message=(
                    f"已精确绑定导出日志 {matched_export_log.artifact.filename}；"
                    f"源导出状态={matched_export_log.manifest.source_status}。"
                    if matched_export_log
                    else "目录中存在日志，但没有唯一、完整且与当前 DMP 精确匹配的 Oracle 导出日志；继续使用标准 DMP 探测。"
                ),
                payload={
                    "assisted_import": bool(matched_export_log),
                    "selected": matched_export_log.artifact.filename if matched_export_log else "",
                    "binding": matched_export_log.binding.to_dict() if matched_export_log else {},
                    "manifest": (
                        matched_export_log.manifest.to_dict(include_table_details=False)
                        if matched_export_log
                        else {}
                    ),
                    "candidates": export_log_candidates,
                },
            )

        if (
            export_log_manifest
            and export_log_manifest.has_source_gaps
            and execute_import
            and not accept_export_log_gaps
        ):
            record_task_event(
                task_id,
                event_type="oracle_export_log_gate",
                title="源导出缺口需要确认",
                status="failed",
                message=(
                    f"导出日志显示 {len(export_log_manifest.missing_objects)} 个对象未进入备份，"
                    "当前任务未授权接受源导出缺口。"
                ),
                payload={
                    "source_status": export_log_manifest.source_status,
                    "missing_objects": export_log_manifest.missing_objects,
                    "completion_error_count": export_log_manifest.completion_error_count,
                    "required_option": "accept_export_log_gaps",
                },
            )
            raise RemoteAccessError(
                "匹配的 Oracle 导出日志显示源备份不完整。请查看缺失对象，确认后勾选"
                "“允许源导出存在缺失对象”并重新提交。"
            )

        username = derive_identifier(_group_name(group), prefix="U")
        tablespace_name = derive_identifier(username, prefix="TS")
        directory_name = _directory_name(job.oracle_docker.oracle_directory, username)
        user_password = job.generated_user_password
        schema_remap = build_schema_remap_plan(related_text, target_schema=username)
        source_schemas = export_log_manifest.schemas if export_log_manifest else schema_remap.source_schemas
        remap_schemas = (
            [(source_schemas[0], username)]
            if export_log_manifest and len(source_schemas) == 1
            else ([] if export_log_manifest else schema_remap.remap_schemas)
        )
        record_task_event(
            task_id,
            event_type="metadata",
            title="生成目标库对象名称",
            status="succeeded",
            message=f"目标 schema={username}，表空间={tablespace_name}，DIRECTORY={directory_name}。",
            payload={
                "schema": username,
                "source_schemas": source_schemas,
                "remap_schemas": format_remap_schemas(remap_schemas),
                "tablespace": tablespace_name,
                "oracle_directory": directory_name,
                "target_connection": target.connection_string,
            },
            stdout=related_text if related_text else None,
        )

        record_task_event(
            task_id,
            event_type="copy",
            title="准备 Oracle DMP 外部目录",
            status="running",
            message=f"创建并授权宿主机目录：{job.oracle_docker.dmp_host_path}",
        )
        ensure_remote_directory(
            oracle_host,
            job.oracle_docker.dmp_host_path,
            mode=job.oracle_docker.chmod_mode,
            sudo_password=job.oracle_docker.sudo_password,
        )
        record_task_event(
            task_id,
            event_type="copy",
            title="Oracle DMP 外部目录准备完成",
            status="succeeded",
            message=f"宿主机目录已创建并授权：{job.oracle_docker.dmp_host_path}",
            payload={
                "dmp_host_path": job.oracle_docker.dmp_host_path,
                "chmod_mode": job.oracle_docker.chmod_mode,
            },
        )
        if direct_import:
            prepared_files = [artifact.remote_path for artifact in flat_artifacts]
            copied = []
            record_task_event(
                task_id,
                event_type="copy",
                title="跳过 DMP 文件复制",
                status="succeeded",
                message="本次选择直接使用 Oracle DMP 目录，系统不会重复复制大文件。",
                payload={
                    "dmp_host_path": job.oracle_docker.dmp_host_path,
                    "manual_dumpfile": manual_dumpfile,
                    "file_count": len(flat_artifacts),
                    "total_bytes": sum(a.size_bytes for a in flat_artifacts),
                    "files": [a.filename for a in flat_artifacts],
                },
            )
        else:
            record_task_event(
                task_id,
                event_type="copy",
                title="开始复制 DMP/log/par 文件",
                status="running",
                message=f"准备复制 {len(flat_artifacts)} 个文件到 Oracle DMP 外部目录。",
                payload={
                    "target_directory": job.oracle_docker.dmp_host_path,
                    "file_count": len(flat_artifacts),
                    "total_bytes": sum(a.size_bytes for a in flat_artifacts),
                    "files": [a.filename for a in flat_artifacts],
                },
            )
            copied = copy_artifacts_between_hosts(
                source_host,
                oracle_host,
                flat_artifacts,
                job.oracle_docker.dmp_host_path,
            )
            prepared_files = copied
            record_task_event(
                task_id,
                event_type="copy",
                title="复制 DMP/log/par 文件完成",
                status="succeeded",
                message=f"已复制 {len(copied)} 个文件到 Oracle 导入目录。",
                payload={"copied_files": copied},
            )
            chmod_remote_tree(
                oracle_host,
                job.oracle_docker.dmp_host_path,
                mode=job.oracle_docker.chmod_mode,
                sudo_password=job.oracle_docker.sudo_password,
            )
        record_task_event(
            task_id,
            event_type="copy",
            title="验证 DMP 文件在 Oracle 容器内可见",
            status="running",
            message=f"检查宿主机目录 {job.oracle_docker.dmp_host_path} 是否已挂载到容器目录 {job.oracle_docker.dmp_container_path}",
            payload={
                "container": job.oracle_docker.container,
                "dmp_host_path": job.oracle_docker.dmp_host_path,
                "dmp_container_path": job.oracle_docker.dmp_container_path,
                "sample_files": [PurePosixPath(p).name for p in prepared_files[:10]],
            },
        )
        visibility = _verify_dmp_files_visible_in_container(
            oracle_host,
            container=job.oracle_docker.container,
            docker_bin=job.oracle_docker.docker_bin,
            dmp_host_path=job.oracle_docker.dmp_host_path,
            dmp_container_path=job.oracle_docker.dmp_container_path,
            copied_files=prepared_files,
        )
        record_task_event(
            task_id,
            event_type="copy",
            title="DMP 文件容器内可见性验证完成",
            status="succeeded",
            message="Oracle 容器内可以看到本次复制的 DMP/log/par 文件。",
            payload=visibility,
        )

        record_task_event(
            task_id,
            event_type="oracle_auto_import",
            title="启动 Oracle 自动探测还原引擎",
            status="running",
            message=(
                "开始按新流程执行：探测 DMP 类型、生成计划、"
                "清理冲突对象、创建表空间和用户，并在确认后执行导入。"
            ),
            payload={
                "execute": execute_import,
                "container": job.oracle_docker.container,
                "dmp_host_path": job.oracle_docker.dmp_host_path,
                "dmp_container_path": job.oracle_docker.dmp_container_path,
                "tablespace_container_path": job.oracle_docker.tablespace_container_path,
                "target_connection": target.connection_string,
            },
        )
        def record_oracle_stream_event(timeline_item: dict) -> None:
            event_type = str(timeline_item.get("event_type") or "oracle_event")
            event_name = str(timeline_item.get("name") or "unknown")
            event_status = str(timeline_item.get("status") or "info")
            event_detail = timeline_item.get("detail") or {}
            title_prefix = "Oracle 导入阶段" if event_type == "stage" else "Oracle 容器命令"
            record_task_event(
                task_id,
                event_type=f"oracle_auto_import_{event_type}",
                title=f"{title_prefix}：{event_name}",
                status=event_status,
                message=str(event_detail.get("message") or ""),
                payload={
                    "name": event_name,
                    "timestamp": timeline_item.get("timestamp"),
                    "detail": event_detail,
                    "full_log": f"/api/v1/tasks/{task_id}/oracle-logs/download",
                },
            )

        def record_oracle_runtime(runtime: dict) -> None:
            update_oracle_runtime_control(
                task_id,
                run_id=str(runtime.get("run_id") or ""),
                run_dir=str(runtime.get("run_dir") or ""),
                job_name=str(runtime.get("job_name") or ""),
                container=str(runtime.get("container") or ""),
            )
            record_task_event(
                task_id,
                event_type="oracle_auto_import_runtime",
                title="Oracle 19c 导入运行控制已建立",
                status="running",
                message=f"Data Pump Job：{runtime.get('job_name') or '-'}",
                payload=runtime,
            )

        auto_result = OracleAutoImportRunner().run(
            task_id=str(task_id),
            oracle_host=oracle_host,
            group=group,
            dmp_host_path=job.oracle_docker.dmp_host_path,
            dmp_container_path=job.oracle_docker.dmp_container_path,
            tablespace_container_path=job.oracle_docker.tablespace_container_path,
            container=job.oracle_docker.container,
            target=target,
            target_user_password=user_password,
            oracle_home_in_container=job.oracle_docker.oracle_home_in_container,
            oracle_directory=job.oracle_docker.oracle_directory if direct_import else None,
            execute=execute_import,
            manual_dumpfile=manual_dumpfile if direct_import else None,
            export_log=(
                {
                    "remote_path": str(
                        PurePosixPath(job.oracle_docker.dmp_host_path)
                        / matched_export_log.artifact.filename
                    ),
                    "filename": matched_export_log.artifact.filename,
                    "manifest": export_log_manifest.expectation_dict(),
                    "binding": matched_export_log.binding.to_dict(),
                    "accept_source_gaps": accept_export_log_gaps,
                }
                if matched_export_log and export_log_manifest
                else None
            ),
            on_event=record_oracle_stream_event,
            on_runtime=record_oracle_runtime,
        )
        for check in auto_result.preflight_checks:
            check_state = str(check.get("state") or "info")
            record_task_event(
                task_id,
                event_type="oracle_auto_import_preflight",
                title=f"Oracle 导入前置检查：{check.get('name') or check.get('code') or 'unknown'}",
                status="succeeded" if check_state in {"passed", "warning"} else "failed",
                message=str(check.get("message") or ""),
                payload=check,
            )
        plan_data = auto_result.plan or {}
        report_data = auto_result.report or {}
        target_users = plan_data.get("target_users") or []
        target_tablespaces = plan_data.get("target_tablespaces") or []
        primary_schema = target_users[0] if target_users else username
        validation_report = None
        if auto_result.success and execute_import and target_users:
            validation_report = validate_oracle_import(
                target,
                schema=primary_schema,
                compile_invalid=True,
            )
            record_task_event(
                task_id,
                event_type="validation",
                title="Oracle 导入后校验完成",
                status="succeeded" if validation_report.ok else "failed",
                message=(
                    f"schema={primary_schema}; tables={validation_report.table_count}; "
                    f"objects={validation_report.object_count}; "
                    f"invalid={len(validation_report.invalid_objects)}"
                ),
                payload=validation_report.to_dict(),
            )
        event_status = "succeeded" if auto_result.success else ("cancelled" if auto_result.state == TaskState.CANCELLED.value else "failed")
        if auto_result.state == TaskState.SUCCEEDED_WITH_WARNINGS.value:
            event_status = "succeeded_with_warnings"
        record_task_event(
            task_id,
            event_type="oracle_auto_import",
            title="Oracle 自动探测还原引擎完成",
            status=event_status,
            message=auto_result.message,
            payload={
                "returncode": auto_result.returncode,
                "run_id": auto_result.run_id,
                "run_dir": auto_result.run_dir,
                "dump_type": plan_data.get("dump_type"),
                "source_schemas": plan_data.get("source_schemas", []),
                "source_tablespaces": plan_data.get("source_tablespaces", []),
                "schema_map": plan_data.get("schema_map", {}),
                "tablespace_map": plan_data.get("tablespace_map", {}),
                "target_users": target_users,
                "target_tablespaces": target_tablespaces,
                "target_datafiles": plan_data.get("target_datafiles", {}),
                "excluded_object_types": plan_data.get("excluded_object_types", []),
                "masked_commands": plan_data.get("masked_commands", []),
                "fallback_commands": plan_data.get("masked_fallback_commands", []),
                "report": report_data,
                "preflight_checks": auto_result.preflight_checks,
                "log_manifest": auto_result.log_manifest,
                "timeline_event_count": len(auto_result.timeline),
                "stdout_is_excerpt": True,
                "full_log_download_url": f"/api/v1/tasks/{task_id}/oracle-logs/download",
            },
            stdout=auto_result.run_log[-12000:] if auto_result.run_log else auto_result.stdout[-12000:],
            stderr=auto_result.stderr[-8000:] if auto_result.stderr else None,
        )
        result_state = auto_result.state if auto_result.success or auto_result.state == TaskState.CANCELLED.value else TaskState.FAILED.value
        result_message = auto_result.message
        if auto_result.success and export_log_manifest and export_log_manifest.has_source_gaps:
            result_state = TaskState.SUCCEEDED_WITH_WARNINGS.value
            result_message = (
                f"{auto_result.message} 源导出日志记录了 {len(export_log_manifest.missing_objects)} 个缺失对象；"
                "DMP 内已有内容已恢复，但不能声明为源备份完整恢复。"
            )
        return {
            "state": result_state,
            "success": auto_result.success,
            "message": result_message,
            "metadata": {
                "professional_flow": True,
                "oracle_auto_import": True,
                "import_source_mode": import_source_mode,
                "manual_dumpfile": manual_dumpfile,
                "oracle_export_log_assisted": bool(matched_export_log),
                "oracle_export_log": (
                    export_log_manifest.to_dict() if export_log_manifest else {}
                ),
                "oracle_export_log_binding": (
                    matched_export_log.binding.to_dict() if matched_export_log else {}
                ),
                "accept_export_log_gaps": accept_export_log_gaps,
                "source_export_status": export_log_manifest.source_status if export_log_manifest else "",
                "source_directory": source_directory,
                "source_files": prepared_files,
                "copied_files": copied,
                "import_tool": plan_data.get("dump_type") or "oracle_auto_import",
                "oracle_auto_import_run_id": auto_result.run_id,
                "oracle_auto_import_run_dir": auto_result.run_dir,
                "oracle_datapump_job_name": plan_data.get("job_name"),
                "oracle_auto_import_log_manifest": auto_result.log_manifest,
                "oracle_auto_import_log_download_url": f"/api/v1/tasks/{task_id}/oracle-logs/download",
                "oracle_auto_import_timeline_event_count": len(auto_result.timeline),
                "oracle_directory": plan_data.get("dump_directory_object") or plan_data.get("directory_object"),
                "oracle_directory_path": (
                    (plan_data.get("run") or {}).get("container_dump_dir")
                    or (plan_data.get("run") or {}).get("container_import_dir")
                ),
                "oracle_work_directory": plan_data.get("directory_object"),
                "oracle_work_directory_path": (plan_data.get("run") or {}).get("container_import_dir"),
                "zero_copy_dump": bool((plan_data.get("run") or {}).get("zero_copy_dump")),
                "tablespace": target_tablespaces[0] if target_tablespaces else tablespace_name,
                "username": primary_schema,
                "schema": primary_schema,
                "target_connection": target.connection_string,
                "source_schemas": plan_data.get("source_schemas", []),
                "source_tablespaces": plan_data.get("source_tablespaces", []),
                "schema_map": plan_data.get("schema_map", {}),
                "tablespace_map": plan_data.get("tablespace_map", {}),
                "target_users": target_users,
                "target_tablespaces": target_tablespaces,
                "target_datafiles": plan_data.get("target_datafiles", {}),
                "excluded_object_types": plan_data.get("excluded_object_types", []),
                "dump_source_files": plan_data.get("dump_source_files", []),
                "dumpfile_arg": plan_data.get("dumpfile_arg"),
                "preflight_checks": auto_result.preflight_checks,
                "validation_report": validation_report.to_dict() if validation_report else {},
                "result_state": result_state,
            },
            "group_id": group.group_id,
            "correction_attempts": 0,
        }

        executor = OracleDockerImportExecutor(
            OracleDockerRuntime(
                host=oracle_host,
                container=job.oracle_docker.container,
                oracle_home=job.oracle_docker.oracle_home_in_container,
                docker_bin=job.oracle_docker.docker_bin,
            )
        )

        record_task_event(
            task_id,
            event_type="oracle_reset",
            title="重置目标 Oracle 用户、表空间和 dbf 文件",
            status="running",
            message=(
                f"开始清理本次任务的目标 schema={username}、"
                f"表空间={tablespace_name} 和残留 dbf 文件。"
            ),
            payload={
                "schema": username,
                "tablespace": tablespace_name,
                "tablespace_path": job.oracle_docker.tablespace_container_path,
                "safe_policy": "only U_* users, TS_U_* tablespaces and ts_u_*.dbf files are removable",
            },
        )
        reset_report = reset_recovery_target(
            target,
            tablespace_name=tablespace_name,
            tablespace_container_path=job.oracle_docker.tablespace_container_path,
            username=username,
        )
        cleanup_result = executor.remove_recovery_datafiles(
            tablespace_container_path=job.oracle_docker.tablespace_container_path,
            tablespace_name=tablespace_name,
        )
        if cleanup_result.returncode != 0:
            raise RemoteAccessError(
                "failed to remove stale Oracle recovery dbf files: "
                + (cleanup_result.stderr or cleanup_result.stdout)
            )
        record_task_event(
            task_id,
            event_type="oracle_reset",
            title="目标 Oracle 对象重置完成",
            status="succeeded",
            message="已完成目标用户、表空间和残留 dbf 文件清理，准备重新创建导入对象。",
            payload={
                **reset_report.to_dict(),
                "removed_dbf_files": [
                    line.strip()
                    for line in (cleanup_result.stdout or "").splitlines()
                    if line.strip()
                ],
                "cleanup_command": cleanup_result.command,
            },
        )

        record_task_event(
            task_id,
            event_type="oracle",
            title="创建 Oracle DIRECTORY、表空间和 schema",
            status="running",
            message="开始连接目标 PDB 并准备导入所需 Oracle 对象。",
            payload={
                "target_connection": target.connection_string,
                "schema": username,
                "tablespace": tablespace_name,
                "directory": directory_name,
                "directory_path": job.oracle_docker.dmp_container_path,
                "tablespace_path": job.oracle_docker.tablespace_container_path,
            },
        )
        prepared = ensure_recovery_prerequisites(
            target,
            directory_name=directory_name,
            directory_path=job.oracle_docker.dmp_container_path,
            tablespace_name=tablespace_name,
            tablespace_container_path=job.oracle_docker.tablespace_container_path,
            username=username,
            user_password=user_password,
            bigfile=settings.oracle_tablespace_bigfile,
            initial_size=settings.oracle_tablespace_initial_size,
            next_size=settings.oracle_tablespace_next_size,
            max_size=settings.oracle_tablespace_max_size,
        )
        record_task_event(
            task_id,
            event_type="oracle",
            title="Oracle 对象准备完成",
            status="succeeded",
            message=f"schema={prepared.username}，表空间={prepared.tablespace_name} 已准备完成。",
            payload={
                "schema": prepared.username,
                "tablespace": prepared.tablespace_name,
                "datafile": prepared.datafile_path,
                "oracle_directory": prepared.directory_name,
                "oracle_directory_path": prepared.directory_path,
                "bigfile": prepared.bigfile,
                "initial_size": prepared.initial_size,
                "next_size": prepared.next_size,
                "max_size": prepared.max_size,
            },
        )

        dump_decision = self._decide_dump_import(
            executor,
            target,
            prepared,
            group,
            related_text,
        )
        plan = _plan_from_dump_decision(dump_decision)
        oracle_metadata = analyze_oracle_metadata(related_text, dump_decision.probe_output)
        remap_schemas = merge_remap_schemas(
            remap_schemas,
            [
                *oracle_metadata.source_schemas,
                *extract_source_schemas(dump_decision.probe_output, target_schema=username),
            ],
            target_schema=username,
        )
        source_schemas = [source for source, _ in remap_schemas]
        record_task_event(
            task_id,
            event_type="metadata_probe",
            title="Oracle 导入前元数据探测完成",
            status="succeeded",
            message=(
                f"探测工具={plan.tool}; 导出模式={oracle_metadata.export_mode.value}; "
                f"源 schema={','.join(source_schemas) or '未识别'}; "
                f"目标 schema={prepared.username}; 目标表空间={prepared.tablespace_name}"
            ),
            payload={
                "tool": plan.tool,
                "export_mode": oracle_metadata.export_mode.value,
                "source_schemas": source_schemas,
                "source_tablespaces": oracle_metadata.source_tablespaces,
                "tables": oracle_metadata.tables[:100],
                "remap_schemas": format_remap_schemas(remap_schemas),
                "remap_tablespace": f"%:{prepared.tablespace_name}",
                "probe_evidence": dump_decision.evidence,
            },
            stdout=dump_decision.metadata_probe[-12000:] if dump_decision.metadata_probe else None,
        )
        record_task_event(
            task_id,
            event_type="plan",
            title="选择导入工具",
            status="succeeded",
            message=f"选择 {plan.tool}：{plan.reason}",
            payload={
                "tool": plan.tool,
                "dumpfiles": plan.dumpfiles,
                "logfile": plan.logfile,
                "use_percent_u": plan.use_percent_u,
                "confidence": dump_decision.confidence,
                "evidence": dump_decision.evidence,
                "export_mode": oracle_metadata.export_mode.value,
                "detected_source_schemas": oracle_metadata.source_schemas,
                "detected_tablespaces": oracle_metadata.source_tablespaces,
                "detected_tables": oracle_metadata.tables[:100],
                "metadata_evidence": oracle_metadata.evidence,
                "remap_schemas": format_remap_schemas(remap_schemas),
                "remap_tablespace": f"%:{prepared.tablespace_name}",
            },
        )
        import_result = self._execute_plan(
            executor,
            target,
            prepared,
            plan,
            task_id=task_id,
            oracle_host=oracle_host,
            dmp_host_path=job.oracle_docker.dmp_host_path,
            remap_schemas=remap_schemas,
            impdp_options=impdp_options,
        )
        success = import_result["success"]
        validation_report = None
        if success:
            validation_report = validate_oracle_import(
                target,
                schema=prepared.username,
                compile_invalid=True,
            )
            record_task_event(
                task_id,
                event_type="validation",
                title="Oracle 导入后校验完成",
                status="succeeded" if validation_report.ok else "failed",
                message=(
                    f"schema={prepared.username}; tables={validation_report.table_count}; "
                    f"objects={validation_report.object_count}; "
                    f"invalid={len(validation_report.invalid_objects)}"
                ),
                payload=validation_report.to_dict(),
            )
        result_state = import_result.get(
            "result_state",
            TaskState.SUCCEEDED.value if success else TaskState.FAILED.value,
        )
        return {
            "state": result_state if success else TaskState.FAILED.value,
            "success": success,
            "message": import_result["message"],
            "metadata": {
                "professional_flow": True,
                "source_directory": source_directory,
                "copied_files": copied,
                "import_tool": import_result["tool"],
                "import_reason": plan.reason,
                "oracle_directory": prepared.directory_name,
                "oracle_directory_path": prepared.directory_path,
                "tablespace": prepared.tablespace_name,
                "datafile": prepared.datafile_path,
                "username": prepared.username,
                "schema": prepared.username,
                "target_connection": target.connection_string,
                "import_logfile": plan.logfile,
                "remap_tablespace": f"%:{prepared.tablespace_name}",
                "source_schemas": source_schemas,
                "remap_schemas": format_remap_schemas(remap_schemas),
                "detected_export_mode": oracle_metadata.export_mode.value,
                "detected_source_schemas": oracle_metadata.source_schemas,
                "detected_source_tablespaces": oracle_metadata.source_tablespaces,
                "detected_tables": oracle_metadata.tables[:100],
                "dump_decision_confidence": dump_decision.confidence,
                "dump_decision_evidence": dump_decision.evidence,
                "validation_report": validation_report.to_dict() if validation_report else {},
                "result_state": result_state,
                "warning_only": import_result.get("warning_only", False),
                "warning_errors": import_result.get("warning_errors", []),
                "fatal_errors": import_result.get("fatal_errors", []),
                "unknown_errors": import_result.get("unknown_errors", []),
            },
            "group_id": group.group_id,
            "correction_attempts": import_result.get("correction_attempts", 0),
        }

    def _decide_dump_import(
        self,
        executor,
        target: TargetDatabase,
        prepared,
        group: DumpVolumeGroup,
        related_text: str,
    ) -> OracleDumpDecision:
        settings = get_settings()

        def probe_impdp_sqlfile(dumpfile: str):
            return executor.run_impdp_sqlfile(
                target,
                directory=prepared.directory_name,
                dumpfile=dumpfile,
                logfile=f"{prepared.username.lower()}_probe_impdp.log",
                sqlfile=f"{prepared.username.lower()}_probe.sql",
                timeout=settings.oracle_import_operation_timeout_seconds,
            )

        def probe_imp_show(dumpfile: str):
            return executor.run_imp_show(
                target,
                username=prepared.username,
                password=prepared.password,
                dumpfile=dumpfile,
                logfile=f"{prepared.username.lower()}_probe_imp.log",
                timeout=settings.oracle_import_operation_timeout_seconds,
            )

        if related_text.strip():
            return enrich_with_metadata_probe(
                detect_from_text(group, related_text),
                probe_impdp_sqlfile=probe_impdp_sqlfile,
                probe_imp_show=probe_imp_show,
            )

        return detect_with_probe(
            group,
            related_text,
            probe_impdp_sqlfile=probe_impdp_sqlfile,
            probe_imp_show=probe_imp_show,
        )

    def _execute_plan(
        self,
        executor,
        target: TargetDatabase,
        prepared,
        plan,
        *,
        task_id,
        oracle_host: RemoteHost,
        dmp_host_path: str,
        remap_schemas: list[tuple[str, str]],
        impdp_options: dict,
    ) -> dict:
        if plan.tool == "imp":
            outputs = []
            for dumpfile in plan.dumpfiles:
                record_task_event(
                    task_id,
                    event_type="import",
                    title=f"开始 imp 导入 {dumpfile}",
                    status="running",
                    payload={"tool": "imp", "dumpfile": dumpfile, "logfile": plan.logfile},
                )
                result = executor.run_imp(
                    target,
                    username=prepared.username,
                    password=prepared.password,
                    dumpfile=dumpfile,
                    logfile=plan.logfile,
                    timeout=get_settings().oracle_import_operation_timeout_seconds,
                )
                combined = (result.stdout or "") + (result.stderr or "")
                outputs.append(combined)
                oracle_log = _read_oracle_import_log(oracle_host, dmp_host_path, plan.logfile)
                diagnosis = _diagnose_import_failure(combined)
                classification = classify_import_result(
                    "oracle",
                    result.returncode,
                    "\n".join([combined, oracle_log]),
                )
                repair_decisions = _repair_decision_payload(
                    decide_oracle_repairs("\n".join([combined, oracle_log]))
                )
                record_task_event(
                    task_id,
                    event_type="import",
                    title=f"imp 导入 {dumpfile} 完成",
                    status="succeeded" if classification.success else "failed",
                    message=_format_import_event_message("imp", result.returncode, diagnosis),
                    payload={
                        "tool": "imp",
                        "dumpfile": dumpfile,
                        "logfile": plan.logfile,
                        "returncode": result.returncode,
                        "executed_command": _format_command(result.command),
                        "diagnosis": diagnosis,
                        "repair_decisions": repair_decisions,
                        **_classification_payload(classification),
                        **_oracle_log_paths(prepared.directory_path, dmp_host_path, plan.logfile),
                    },
                    stdout=_format_import_stdout(result.stdout, oracle_log),
                    stderr=result.stderr,
                )
                if result.returncode != 0:
                    if should_retry_with_impdp(combined):
                        record_task_event(
                            task_id,
                            event_type="retry",
                            title="imp 失败，准备重试 impdp",
                            status="running",
                            message=combined[-4000:],
                            stdout=combined,
                        )
                        return self._execute_impdp(
                            executor,
                            target,
                            prepared,
                            plan,
                            reason=combined,
                            task_id=task_id,
                            oracle_host=oracle_host,
                            dmp_host_path=dmp_host_path,
                            remap_schemas=remap_schemas,
                            impdp_options=impdp_options,
                        )
                    if classification.success:
                        return _accepted_warning_result(
                            "imp",
                            classification,
                            "\n".join([combined, oracle_log]),
                        )
                    return {
                        "success": False,
                        "tool": "imp",
                        "message": _format_failure_message(
                            combined,
                            fallback=f"imp failed with code {result.returncode}",
                        ),
                    }
            return {"success": True, "tool": "imp", "message": "\n".join(outputs)[-4000:]}
        return self._execute_impdp(
            executor,
            target,
            prepared,
            plan,
            task_id=task_id,
            oracle_host=oracle_host,
            dmp_host_path=dmp_host_path,
            remap_schemas=remap_schemas,
            impdp_options=impdp_options,
        )

    def _execute_impdp(
        self,
        executor,
        target: TargetDatabase,
        prepared,
        plan,
        reason: str = "",
        *,
        task_id=None,
        oracle_host: RemoteHost,
        dmp_host_path: str,
        remap_schemas: list[tuple[str, str]],
        impdp_options: dict,
    ) -> dict:
        outputs = []
        attempted_remap_schemas = list(remap_schemas)
        for dumpfile in plan.dumpfiles:
            settings = get_settings()
            record_task_event(
                task_id,
                event_type="import",
                title=f"开始 impdp 导入 {dumpfile}",
                status="running",
                payload={
                    "tool": "impdp",
                    "dumpfile": dumpfile,
                    "logfile": plan.logfile,
                    "directory": prepared.directory_name,
                    "schema": prepared.username,
                    "remap_schemas": format_remap_schemas(attempted_remap_schemas),
                    "remap_tablespace": f"%:{prepared.tablespace_name}",
                    "impdp_options": impdp_options,
                },
            )
            result = self._run_impdp_with_realtime_log(
                executor,
                target,
                prepared,
                directory=prepared.directory_name,
                dumpfile=dumpfile,
                logfile=plan.logfile,
                username=prepared.username,
                timeout=settings.oracle_import_operation_timeout_seconds,
                remap_schemas=attempted_remap_schemas,
                remap_tablespace=prepared.tablespace_name,
                table_exists_action=impdp_options["table_exists_action"],
                parallel=impdp_options["parallel"],
                metrics=impdp_options["metrics"],
                logtime=impdp_options["logtime"],
                access_method=impdp_options.get("access_method"),
                disable_archive_logging=impdp_options["disable_archive_logging"],
                exclude_indexes=impdp_options["index_mode"] == "exclude",
                task_id=task_id,
            )
            combined = (result.stdout or "") + (result.stderr or "")
            outputs.append(combined)
            oracle_log = _read_oracle_import_log(oracle_host, dmp_host_path, plan.logfile)
            diagnosis = _diagnose_import_failure(combined)
            classification = classify_import_result(
                "oracle",
                result.returncode,
                "\n".join([reason, combined, oracle_log]),
            )
            repair_decisions = _repair_decision_payload(
                decide_oracle_repairs("\n".join([reason, combined, oracle_log]))
            )
            record_task_event(
                task_id,
                event_type="import",
                title=f"impdp 导入 {dumpfile} 完成",
                status="succeeded" if classification.success else "failed",
                message=_format_import_event_message("impdp", result.returncode, diagnosis),
                payload={
                    "tool": "impdp",
                    "dumpfile": dumpfile,
                    "logfile": plan.logfile,
                    "returncode": result.returncode,
                    "directory": prepared.directory_name,
                    "schema": prepared.username,
                    "remap_schemas": format_remap_schemas(attempted_remap_schemas),
                    "remap_tablespace": f"%:{prepared.tablespace_name}",
                    "executed_command": _format_command(result.command),
                    "diagnosis": diagnosis,
                    "repair_decisions": repair_decisions,
                    **_classification_payload(classification),
                    **_oracle_log_paths(prepared.directory_path, dmp_host_path, plan.logfile),
                },
                stdout=_format_import_stdout(result.stdout, oracle_log),
                stderr=result.stderr,
            )
            if result.returncode != 0:
                retry_remap_schemas = merge_remap_schemas(
                    attempted_remap_schemas,
                    extract_source_schemas(
                        "\n".join([reason, combined, oracle_log]),
                        target_schema=prepared.username,
                    ),
                    target_schema=prepared.username,
                )
                if retry_remap_schemas != attempted_remap_schemas:
                    record_task_event(
                        task_id,
                        event_type="retry",
                        title="识别到源 schema，准备使用 REMAP_SCHEMA 重试 impdp",
                        status="running",
                        message="impdp 输出中识别到新的源 schema，追加 REMAP_SCHEMA 后重试。",
                        payload={
                            "old_remap_schemas": format_remap_schemas(attempted_remap_schemas),
                            "new_remap_schemas": format_remap_schemas(retry_remap_schemas),
                            "remap_tablespace": f"%:{prepared.tablespace_name}",
                        },
                    )
                    attempted_remap_schemas = retry_remap_schemas
                    result = self._run_impdp_with_realtime_log(
                        executor,
                        target,
                        prepared,
                        directory=prepared.directory_name,
                        dumpfile=dumpfile,
                        logfile=plan.logfile,
                        username=prepared.username,
                        timeout=settings.oracle_import_operation_timeout_seconds,
                        remap_schemas=attempted_remap_schemas,
                        remap_tablespace=prepared.tablespace_name,
                        table_exists_action=impdp_options["table_exists_action"],
                        parallel=impdp_options["parallel"],
                        metrics=impdp_options["metrics"],
                        logtime=impdp_options["logtime"],
                        access_method=impdp_options.get("access_method"),
                        disable_archive_logging=impdp_options["disable_archive_logging"],
                        exclude_indexes=impdp_options["index_mode"] == "exclude",
                        task_id=task_id,
                    )
                    combined = (result.stdout or "") + (result.stderr or "")
                    outputs.append(combined)
                    oracle_log = _read_oracle_import_log(oracle_host, dmp_host_path, plan.logfile)
                    diagnosis = _diagnose_import_failure(combined)
                    classification = classify_import_result(
                        "oracle",
                        result.returncode,
                        "\n".join([reason, combined, oracle_log]),
                    )
                    repair_decisions = _repair_decision_payload(
                        decide_oracle_repairs("\n".join([reason, combined, oracle_log]))
                    )
                    record_task_event(
                        task_id,
                        event_type="import",
                        title=f"impdp 使用 REMAP_SCHEMA 重试 {dumpfile} 完成",
                        status="succeeded" if classification.success else "failed",
                        message=_format_import_event_message(
                            "impdp retry",
                            result.returncode,
                            diagnosis,
                        ),
                        payload={
                            "tool": "impdp",
                            "dumpfile": dumpfile,
                            "logfile": plan.logfile,
                            "returncode": result.returncode,
                            "directory": prepared.directory_name,
                            "schema": prepared.username,
                            "remap_schemas": format_remap_schemas(attempted_remap_schemas),
                            "remap_tablespace": f"%:{prepared.tablespace_name}",
                            "executed_command": _format_command(result.command),
                            "diagnosis": diagnosis,
                            "repair_decisions": repair_decisions,
                            **_classification_payload(classification),
                            **_oracle_log_paths(
                                prepared.directory_path,
                                dmp_host_path,
                                plan.logfile,
                            ),
                        },
                        stdout=_format_import_stdout(result.stdout, oracle_log),
                        stderr=result.stderr,
                    )
                    if result.returncode == 0:
                        continue
                if classification.success:
                    return _accepted_warning_result(
                        "impdp",
                        classification,
                        "\n".join([reason, combined, oracle_log]),
                    )
                return {
                    "success": False,
                    "tool": "impdp",
                    "message": _format_failure_message(
                        reason + "\n" + combined,
                        fallback=f"impdp failed with code {result.returncode}",
                    ),
                }
        return {"success": True, "tool": "impdp", "message": "\n".join(outputs)[-4000:]}

    def _run_impdp_with_realtime_log(
        self,
        executor,
        target: TargetDatabase,
        prepared,
        *,
        directory: str,
        dumpfile: str,
        logfile: str,
        username: str,
        timeout: int,
        remap_schemas: list[tuple[str, str]],
        remap_tablespace: str,
        table_exists_action: str = "REPLACE",
        parallel: int | None = None,
        metrics: bool | None = None,
        logtime: str | None = None,
        access_method: str | None = None,
        disable_archive_logging: bool = False,
        exclude_indexes: bool = False,
        task_id=None,
    ):
        settings = get_settings()
        stdout_buffer: list[str] = []
        stderr_buffer: list[str] = []
        autogrow_done = False

        def flush(kind: str, force: bool = False) -> None:
            buffer = stdout_buffer if kind == "stdout" else stderr_buffer
            if not buffer:
                return
            text = "".join(buffer)
            if not force and len(text) < 4096 and "ORA-" not in text.upper():
                return
            buffer.clear()
            record_task_event(
                task_id,
                event_type=f"import_{kind}",
                title=f"实时 {kind}：{dumpfile}",
                status="running",
                message=text[-1000:],
                stdout=text if kind == "stdout" else None,
                stderr=text if kind == "stderr" else None,
                payload={
                    "tool": "impdp",
                    "dumpfile": dumpfile,
                    "logfile": logfile,
                    "schema": prepared.username,
                    "tablespace": prepared.tablespace_name,
                },
            )

        def maybe_autogrow(text: str) -> None:
            nonlocal autogrow_done
            upper = text.upper()
            if autogrow_done or "ORA-01653" not in upper:
                return
            if not settings.oracle_tablespace_auto_grow_on_ora_01653:
                return
            autogrow_done = True
            record_task_event(
                task_id,
                event_type="tablespace_autogrow",
                title="检测到 ORA-01653，准备自动扩容表空间",
                status="running",
                message=f"目标表空间 {prepared.tablespace_name} 无法继续扩展，开始自动处理。",
                stdout=text[-4000:],
                payload={
                    "tablespace": prepared.tablespace_name,
                    "datafile_path": prepared.datafile_path,
                    "next_size": settings.oracle_tablespace_next_size,
                    "add_datafile_size": settings.oracle_tablespace_auto_grow_add_datafile_size,
                },
            )
            try:
                result = grow_tablespace_for_import(
                    target,
                    tablespace_name=prepared.tablespace_name,
                    tablespace_container_path=str(PurePosixPath(prepared.datafile_path).parent),
                    next_size=settings.oracle_tablespace_next_size,
                    add_datafile_size=settings.oracle_tablespace_auto_grow_add_datafile_size,
                    max_size=settings.oracle_tablespace_max_size,
                )
                record_task_event(
                    task_id,
                    event_type="tablespace_autogrow",
                    title="表空间自动扩容完成",
                    status="succeeded",
                    message=f"已处理表空间 {prepared.tablespace_name}，等待 impdp 自动继续。",
                    payload=result,
                )
            except Exception as exc:
                record_task_event(
                    task_id,
                    event_type="tablespace_autogrow",
                    title="表空间自动扩容失败",
                    status="failed",
                    message=str(exc),
                    payload={"tablespace": prepared.tablespace_name},
                )

        def on_stdout(text: str) -> None:
            stdout_buffer.append(text)
            maybe_autogrow(text)
            flush("stdout")

        def on_stderr(text: str) -> None:
            stderr_buffer.append(text)
            maybe_autogrow(text)
            flush("stderr")

        result = executor.run_impdp_stream(
            target,
            directory=directory,
            dumpfile=dumpfile,
            logfile=logfile,
            username=username,
            timeout=timeout,
            remap_schemas=remap_schemas,
            remap_tablespace=remap_tablespace,
            table_exists_action=table_exists_action,
            parallel=parallel,
            metrics=metrics,
            logtime=logtime,
            access_method=access_method,
            disable_archive_logging=disable_archive_logging,
            exclude_indexes=exclude_indexes,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        combined = f"{result.stdout or ''}\n{result.stderr or ''}".upper()
        if result.returncode != 0 and parallel and parallel > 1 and "ORA-39094" in combined:
            record_task_event(
                task_id,
                event_type="impdp_parallel_fallback",
                title="impdp 并行导入自动降级",
                status="running",
                message=(
                    f"目标 Oracle 版本不支持 Data Pump 并行执行，已从 PARALLEL={parallel} "
                    "自动降级为 PARALLEL=1 后重试。"
                ),
                payload={
                    "dumpfile": dumpfile,
                    "logfile": logfile,
                    "original_parallel": parallel,
                    "fallback_parallel": 1,
                    "reason": "ORA-39094",
                },
            )
            stdout_buffer.clear()
            stderr_buffer.clear()
            result = executor.run_impdp_stream(
                target,
                directory=directory,
                dumpfile=dumpfile,
                logfile=logfile,
                username=username,
                timeout=timeout,
                remap_schemas=remap_schemas,
                remap_tablespace=remap_tablespace,
                table_exists_action=table_exists_action,
                parallel=1,
                metrics=metrics,
                logtime=logtime,
                access_method=access_method,
                disable_archive_logging=disable_archive_logging,
                exclude_indexes=exclude_indexes,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        flush("stdout", force=True)
        flush("stderr", force=True)
        return result


def _normalize_impdp_options(raw: dict) -> dict:
    options = dict(raw or {})
    logtime = str(options.get("logtime") or "ALL").strip().upper()
    if logtime not in {"NONE", "STATUS", "LOGFILE", "ALL"}:
        logtime = "ALL"

    access_method = str(options.get("access_method") or "DIRECT_PATH").strip().upper()
    if access_method not in {"DIRECT_PATH", "EXTERNAL_TABLE", "CONVENTIONAL"}:
        access_method = "DIRECT_PATH"

    table_exists_action = str(options.get("table_exists_action") or "REPLACE").strip().upper()
    if table_exists_action not in {"SKIP", "APPEND", "TRUNCATE", "REPLACE"}:
        table_exists_action = "REPLACE"

    index_mode = str(options.get("index_mode") or "default").strip().lower()
    if index_mode not in {"default", "exclude"}:
        index_mode = "default"

    try:
        parallel = int(options.get("parallel") or 16)
    except (TypeError, ValueError):
        parallel = 16
    parallel = max(1, min(parallel, 128))

    return {
        "parallel": parallel,
        "metrics": bool(options.get("metrics", True)),
        "logtime": logtime,
        "access_method": access_method,
        "disable_archive_logging": bool(options.get("disable_archive_logging", False)),
        "table_exists_action": table_exists_action,
        "index_mode": index_mode,
    }


def _select_export_log(
    host: RemoteHost,
    *,
    directory: str,
    dump_files: list[DumpArtifact],
    log_files: list[DumpArtifact] | None = None,
) -> tuple[MatchedExportLog | None, list[dict]]:
    candidates = log_files
    if candidates is None:
        candidates = [
            artifact
            for artifact in list_remote_artifacts(host, directory)
            if artifact.filename.lower().endswith(".log")
        ]

    actual_dump_files = [artifact.filename for artifact in dump_files]
    matched: list[MatchedExportLog] = []
    reports: list[dict] = []
    for artifact in sorted(candidates, key=lambda item: item.filename.lower()):
        if artifact.size_bytes > MAX_EXPORT_LOG_BYTES:
            reports.append(
                {
                    "filename": artifact.filename,
                    "state": "too_large",
                    "size_bytes": artifact.size_bytes,
                    "max_bytes": MAX_EXPORT_LOG_BYTES,
                }
            )
            continue
        try:
            text = read_remote_text(host, artifact.remote_path, max_bytes=MAX_EXPORT_LOG_BYTES)
            manifest = parse_oracle_export_log(text)
            binding = bind_export_log(
                manifest,
                log_filename=artifact.filename,
                actual_dump_files=actual_dump_files,
            )
            reports.append(
                {
                    "filename": artifact.filename,
                    "size_bytes": artifact.size_bytes,
                    "recognized": manifest.recognized,
                    "source_status": manifest.source_status,
                    "export_mode": manifest.export_mode,
                    "binding": binding.to_dict(),
                    "content_sha256": manifest.content_sha256,
                }
            )
            if binding.exact:
                matched.append(MatchedExportLog(artifact=artifact, manifest=manifest, binding=binding))
        except Exception as exc:
            reports.append(
                {
                    "filename": artifact.filename,
                    "state": "read_or_parse_failed",
                    "message": str(exc),
                }
            )

    if len(matched) != 1:
        if len(matched) > 1:
            for report in reports:
                binding = report.get("binding") or {}
                if binding.get("state") == "exact":
                    report["state"] = "ambiguous_exact_match"
        return None, reports
    return matched[0], reports


def _build_direct_dump_group(
    oracle_host: RemoteHost,
    *,
    dmp_host_path: str,
    manual_dumpfile: str,
) -> tuple[DumpVolumeGroup, list[DumpArtifact], dict]:
    filename = PurePosixPath(manual_dumpfile).name
    if not filename.lower().endswith(".dmp"):
        raise RemoteAccessError("手动填写的 DUMPFILE 必须是 .dmp 文件名或 %U 分片模式。")

    pattern = filename.replace("%U", "*")
    q_dir = shlex.quote(dmp_host_path)
    q_pattern = shlex.quote(pattern)
    command = (
        f"dir={q_dir}; pattern={q_pattern}; "
        "if [ ! -d \"$dir\" ]; then echo \"__DIR_MISSING__:$dir\"; exit 2; fi; "
        "find \"$dir\" -maxdepth 1 -type f -name \"$pattern\" "
        "-exec sh -c 'for f do "
        "size=$(wc -c < \"$f\" 2>/dev/null || printf 0); "
        "base=${f##*/}; "
        "printf \"%s\\t%s\\t%s\\n\" \"$base\" \"$size\" \"$f\"; "
        "done' sh {} + | sort"
    )
    settings = get_settings()
    result = run_ssh_command(
        oracle_host,
        command,
        timeout=settings.oracle_import_operation_timeout_seconds,
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "无法访问 Oracle DMP 目录"
        raise RemoteAccessError(detail)

    artifacts: list[DumpArtifact] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, size_text, path = parts
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        artifacts.append(DumpArtifact(remote_path=path, filename=name, size_bytes=size))

    if not artifacts:
        raise RemoteAccessError(
            f"Oracle DMP 目录 {dmp_host_path} 中没有匹配 {filename} 的文件。"
            "请确认文件名、%U 分片模式和容器挂载目录是否一致。"
        )

    volume_type = DumpVolumeType.MULTI if "%U" in filename or len(artifacts) > 1 else DumpVolumeType.SINGLE
    group = DumpVolumeGroup(
        group_id=f"direct:{filename}",
        dump_files=artifacts,
        volume_type=volume_type,
    )
    payload = {
        "mode": "direct",
        "dmp_host_path": dmp_host_path,
        "manual_dumpfile": filename,
        "matched_pattern": pattern,
        "file_count": len(artifacts),
        "total_bytes": sum(item.size_bytes for item in artifacts),
        "files": [
            {
                "filename": item.filename,
                "remote_path": item.remote_path,
                "size_bytes": item.size_bytes,
            }
            for item in artifacts
        ],
    }
    return group, artifacts, payload


def _flatten_groups(groups: list[DumpVolumeGroup]) -> list[DumpArtifact]:
    seen: dict[str, DumpArtifact] = {}
    for group in groups:
        for artifact in [*group.dump_files, *group.log_files, *group.par_files]:
            seen[artifact.remote_path] = artifact
    return list(seen.values())


def _read_best_log(source_host: RemoteHost, group: DumpVolumeGroup) -> str:
    for artifact in group.log_files:
        try:
            return read_remote_text(source_host, artifact.remote_path)
        except Exception as e:
            logger.warning("professional_log_read_failed", path=artifact.remote_path, error=str(e))
    return ""


def _read_related_text(source_host: RemoteHost, group: DumpVolumeGroup) -> str:
    texts: list[str] = []
    for artifact in [*group.log_files, *group.par_files]:
        try:
            text = read_remote_text(source_host, artifact.remote_path)
            if text:
                texts.append(f"===== {artifact.filename} =====\n{text}")
        except Exception as e:
            logger.warning("professional_related_text_read_failed", path=artifact.remote_path, error=str(e))
    return "\n\n".join(texts)


def _extract_source_schemas(text: str, *, target_schema: str) -> list[str]:
    return extract_source_schemas(text, target_schema=target_schema)


def _merge_remap_schemas(
    current: list[tuple[str, str]],
    discovered_sources: list[str],
    *,
    target_schema: str,
) -> list[tuple[str, str]]:
    return merge_remap_schemas(
        current,
        discovered_sources,
        target_schema=target_schema,
    )


def _format_remap_schemas(remap_schemas: list[tuple[str, str]]) -> list[str]:
    return format_remap_schemas(remap_schemas)


def _plan_from_dump_decision(decision: OracleDumpDecision):
    return ImportPlan(
        tool=decision.tool,
        dumpfiles=decision.dumpfiles,
        logfile=decision.logfile,
        use_percent_u=decision.use_percent_u,
        reason=decision.reason,
        extra={
            "confidence": decision.confidence,
            "evidence": decision.evidence,
        },
    )


def _repair_decision_payload(decisions) -> list[dict]:
    return [
        {
            "code": decision.code,
            "diagnosis": decision.diagnosis,
            "action": decision.action,
            "retry": decision.retry,
            "fatal_if_unfixed": decision.fatal_if_unfixed,
            "evidence": decision.evidence,
        }
        for decision in decisions
    ]


def _read_oracle_import_log(oracle_host: RemoteHost, dmp_host_path: str, logfile: str) -> str:
    try:
        return read_remote_text(oracle_host, str(PurePosixPath(dmp_host_path) / logfile))
    except Exception as e:
        logger.warning("oracle_import_log_read_failed", logfile=logfile, error=str(e))
        return ""


def _format_oracle_log(log_text: str) -> str:
    if not log_text:
        return ""
    return "===== Oracle import log file content =====\n" + log_text


def _format_command_stdout(stdout: str) -> str:
    if not stdout:
        return ""
    return "===== imp/impdp process stdout =====\n" + stdout


def _format_import_stdout(stdout: str, oracle_log: str) -> str | None:
    parts = [
        text
        for text in [
            _format_command_stdout(stdout),
            _format_oracle_log(oracle_log),
        ]
        if text
    ]
    return "\n\n".join(parts) if parts else None


def _format_command(command: list[str]) -> str:
    return command[0] if command else ""


def _oracle_log_paths(directory_path: str, dmp_host_path: str, logfile: str) -> dict[str, str]:
    return {
        "oracle_log_container_path": str(PurePosixPath(directory_path) / logfile),
        "oracle_log_host_path": str(PurePosixPath(dmp_host_path) / logfile),
    }


def _classification_payload(classification: ImportResultClassification) -> dict:
    return {
        "result_state": classification.state,
        "warning_only": classification.warning_only,
        "warning_errors": classification.warning_errors,
        "fatal_errors": classification.fatal_errors,
        "unknown_errors": classification.unknown_errors,
    }


def _accepted_warning_result(
    tool: str,
    classification: ImportResultClassification,
    output: str,
) -> dict:
    return {
        "success": True,
        "tool": tool,
        "message": _classified_oracle_message(classification, output),
        **_classification_payload(classification),
    }


def _classified_oracle_message(
    classification: ImportResultClassification,
    output: str,
) -> str:
    if classification.warning_only:
        warnings = "\n".join(classification.warning_errors)[-3000:]
        return f"{classification.summary}\n\n{warnings}"
    return (output or classification.summary)[-4000:]


def _diagnose_import_failure(output: str) -> str:
    upper = output.upper()
    if "ORA-39070" in upper or "ORA-29283" in upper:
        return (
            "impdp 无法打开或创建导入日志文件。请重点检查 Oracle DIRECTORY 指向的容器内目录是否存在、"
            "oracle 进程用户是否可写、宿主机挂载目录和容器内目录是否一致；当前版本已改为使用独立"
            "导入日志文件名，避免复用源库导出日志。"
        )
    if "ORA-39002" in upper:
        return "impdp 参数无效，请检查 DIRECTORY、DUMPFILE、REMAP_SCHEMA、REMAP_TABLESPACE 等参数。"
    if "ORA-39165" in upper or "ORA-31655" in upper:
        return "Data Pump 没有找到可导入对象，请检查源 schema 识别和 REMAP_SCHEMA 是否正确。"
    if "ORA-01918" in upper or "ORA-01435" in upper:
        return "导入过程中引用的 schema/user 不存在，通常需要补充 REMAP_SCHEMA=源schema:目标schema。"
    if "ORA-00959" in upper:
        return "导入对象引用的表空间不存在，应使用 REMAP_TABLESPACE=%:目标表空间。"
    if "ORA-01653" in upper:
        return (
            "目标表空间无法继续扩展。通常是表空间数据文件所在磁盘空间不足、数据文件达到上限，"
            "或旧的同名表空间/用户被复用导致空间状态不干净。当前版本会在同名目标存在时先删除"
            "用户和表空间再重建，并使用 BIGFILE TABLESPACE；仍失败时请检查宿主机表空间目录磁盘空间。"
        )
    return ""


def _format_import_event_message(tool: str, returncode: int, diagnosis: str) -> str:
    message = f"{tool} returncode={returncode}"
    if diagnosis and returncode != 0:
        return f"{message}；诊断：{diagnosis}"
    return message


def _format_failure_message(output: str, *, fallback: str) -> str:
    diagnosis = _diagnose_import_failure(output)
    raw = output[-4000:] if output else fallback
    if diagnosis:
        return f"诊断：{diagnosis}\n\n{raw}"
    return raw


def _verify_dmp_files_visible_in_container(
    oracle_host: RemoteHost,
    *,
    container: str,
    docker_bin: str,
    dmp_host_path: str,
    dmp_container_path: str,
    copied_files: list[str],
) -> dict:
    sample_files = [PurePosixPath(p).name for p in copied_files[:20]]
    if not sample_files:
        return {
            "container": container,
            "dmp_host_path": dmp_host_path,
            "dmp_container_path": dmp_container_path,
            "checked_files": [],
            "mounts": "",
        }

    inspect_cmd = (
        f"{shlex.quote(docker_bin)} inspect --format "
        f"{shlex.quote('{{range .Mounts}}{{.Source}}|{{.Destination}}{{println}}{{end}}')} "
        f"{shlex.quote(container)}"
    )
    inspect_result = run_ssh_command(
        oracle_host,
        inspect_cmd,
        timeout=get_settings().oracle_ssh_check_timeout_seconds,
    )
    mounts = inspect_result.stdout or inspect_result.stderr or ""
    if inspect_result.returncode != 0:
        raise RemoteAccessError(f"无法检查 Oracle 容器挂载信息：{mounts}")

    expected_line = f"{dmp_host_path}|{dmp_container_path}"
    destination_lines = [
        line
        for line in mounts.splitlines()
        if line.strip().endswith(f"|{dmp_container_path}")
    ]
    if expected_line not in mounts.splitlines():
        actual = destination_lines[0] if destination_lines else "未找到该容器内路径挂载"
        raise RemoteAccessError(
            "Oracle 容器的 DMP 目录挂载不匹配，复制到宿主机后的文件不会在容器内可见。"
            f"期望挂载：{expected_line}；实际挂载：{actual}。"
            "Docker volume 只能在容器创建时确定，请修改 .env 与现有挂载一致，"
            "或停止并删除旧 Oracle 容器后用 start-oracle19c.sh 重新创建。"
        )

    checks = []
    for filename in sample_files:
        path = str(PurePosixPath(dmp_container_path) / filename)
        checks.append(f"test -f {shlex.quote(path)} || echo MISSING:{shlex.quote(filename)}")
    inner = " && ".join(checks)
    exec_cmd = (
        f"{shlex.quote(docker_bin)} exec {shlex.quote(container)} "
        f"bash -lc {shlex.quote(inner)}"
    )
    settings = get_settings()
    result = run_ssh_command(
        oracle_host,
        exec_cmd,
        timeout=settings.oracle_import_operation_timeout_seconds,
    )
    missing = [
        line.removeprefix("MISSING:")
        for line in (result.stdout or "").splitlines()
        if line.startswith("MISSING:")
    ]
    if result.returncode != 0 or missing:
        raise RemoteAccessError(
            "DMP 文件已经复制到宿主机目录，但 Oracle 容器内不可见。"
            f"宿主机目录：{dmp_host_path}；容器目录：{dmp_container_path}；"
            f"缺失文件：{missing or sample_files}；"
            "请检查容器挂载和目录权限。"
        )

    return {
        "container": container,
        "dmp_host_path": dmp_host_path,
        "dmp_container_path": dmp_container_path,
        "checked_files": sample_files,
        "mounts": mounts,
    }


def _group_name(group: DumpVolumeGroup) -> str:
    if group.dump_files:
        return group.dump_files[0].filename.rsplit(".", 1)[0]
    return group.group_id


def _directory_name(configured: str | None, username: str) -> str:
    if configured and configured.upper() != "DATA_PUMP_DIR":
        return derive_identifier(configured)
    return derive_identifier(username, prefix="DIR")
