import shlex
from pathlib import PurePosixPath

from recovery_service.common.security import decrypt_secret
from recovery_service.core.domain import DumpArtifact, RemoteHost
from recovery_service.core.enums import TaskState
from recovery_service.core.exceptions import DiscoveryError, RemoteAccessError
from recovery_service.engine.mysql.restore_plan import (
    MySqlRestorePlan,
    choose_restore_plan,
    group_mysql_files,
)
from recovery_service.engine.import_result.classifier import (
    ImportResultClassification,
    classify_import_result,
)
from recovery_service.infrastructure.mysql.docker_executor import (
    MySqlDockerExecutor,
    MySqlDockerRuntime,
    mysql_container_path,
)
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command
from recovery_service.infrastructure.ssh.file_transfer import (
    chmod_remote_tree,
    copy_artifacts_between_hosts,
    ensure_remote_directory,
)
from recovery_service.services.task_events import record_task_event
from recovery_service.settings import get_settings


class MySqlRecoveryPipeline:
    def run(self, config: dict, *, volume_group_index: int = 0) -> dict:
        settings = get_settings()
        enc = settings.credential_encryption_key
        task_id = config.get("_task_id")
        source = config["source"]
        mysql = config["mysql_docker"]
        options = config.get("options") or {}

        source_host = RemoteHost(
            host=source["host"],
            port=int(source.get("port", 22)),
            username=source["user"],
            password=decrypt_secret(source["password"], enc),
        )
        mysql_host = RemoteHost(
            host=mysql["host"],
            port=int(mysql.get("port", 22)),
            username=mysql["user"],
            password=decrypt_secret(mysql["password"], enc),
        )
        sudo_password = decrypt_secret(mysql.get("sudo_password", ""), enc)

        record_task_event(
            task_id,
            event_type="discover",
            title="扫描 MySQL 备份目录",
            status="running",
            message=f"扫描源目录：{source['directory']}",
        )
        files = _scan_mysql_files(source_host, source["directory"])
        groups = group_mysql_files(files)
        if not groups:
            raise DiscoveryError("没有发现 .sql/.sql.gz/.sql.zip/.zip 文件")
        if volume_group_index >= len(groups):
            raise DiscoveryError(f"volume_group_index {volume_group_index} out of range")

        plan = choose_restore_plan(
            groups[volume_group_index],
            target_database=options.get("target_database"),
            drop_existing=bool(options.get("drop_existing", True)),
        )
        record_task_event(
            task_id,
            event_type="plan",
            title="生成 MySQL 还原计划",
            status="succeeded",
            message=plan.reason,
            payload={
                "method": plan.method,
                "database": plan.database_name,
                "drop_existing": plan.drop_existing,
                "files": [f.filename for f in plan.files],
                "all_groups": [
                    {"group_id": g.group_id, "files": [f.filename for f in g.files]}
                    for g in groups
                ],
            },
        )

        if plan.method == "unsupported":
            return _result(False, plan, message=plan.reason)

        ensure_remote_directory(
            mysql_host,
            mysql["backup_host_path"],
            mode=mysql.get("chmod_mode") or "777",
            sudo_password=sudo_password,
        )
        copied = copy_artifacts_between_hosts(
            source_host,
            mysql_host,
            plan.files,
            mysql["backup_host_path"],
        )
        chmod_remote_tree(
            mysql_host,
            mysql["backup_host_path"],
            mode=mysql.get("chmod_mode") or "777",
            sudo_password=sudo_password,
        )
        visibility = _verify_container_files(
            mysql_host,
            container=mysql["container"],
            docker_bin=mysql.get("docker_bin") or "docker",
            container_path=mysql["backup_container_path"],
            copied_files=copied,
        )
        record_task_event(
            task_id,
            event_type="copy",
            title="MySQL 备份文件复制完成",
            status="succeeded",
            message="备份文件已复制，并且目标 MySQL 容器内可见。",
            payload={
                "copied_files": copied,
                "backup_host_path": mysql["backup_host_path"],
                "backup_container_path": mysql["backup_container_path"],
                "visibility": visibility,
            },
        )

        executor = MySqlDockerExecutor(
            MySqlDockerRuntime(
                host=mysql_host,
                container=mysql["container"],
                root_password=decrypt_secret(mysql["root_password"], enc),
                docker_bin=mysql.get("docker_bin") or "docker",
            )
        )
        _prepare_database(executor, plan)
        executor.run_sql("SET GLOBAL log_bin_trust_function_creators = 1;", timeout=600)
        record_task_event(
            task_id,
            event_type="mysql",
            title="MySQL 目标库已准备",
            status="succeeded",
            message=f"目标库：{plan.database_name}",
            payload={"database": plan.database_name, "drop_existing": plan.drop_existing},
        )

        dump_path = mysql_container_path(mysql["backup_container_path"], plan.files[0].filename)

        def on_stderr(line: str) -> None:
            if line.strip():
                record_task_event(
                    task_id,
                    event_type="mysql",
                    title="MySQL 导入日志",
                    status="running",
                    message=line.strip()[:1000],
                    stderr=line,
                )

        result = executor.import_dump(
            dump_path,
            database=plan.database_name,
            method=plan.method,
            timeout=int(mysql.get("import_timeout_seconds", 14400)),
            on_stderr=on_stderr,
        )
        output = "\n".join([result.stdout or "", result.stderr or ""])
        classification = classify_import_result("mysql", result.returncode, output)
        record_task_event(
            task_id,
            event_type="mysql",
            title="MySQL 导入执行完成",
            status="succeeded" if classification.success else "failed",
            message=f"mysql import returncode={result.returncode}; {classification.summary}",
            payload={
                "method": plan.method,
                "database": plan.database_name,
                "executed_command": result.command[0] if result.command else "",
                **_classification_payload(classification),
            },
            stdout=result.stdout,
            stderr=result.stderr,
        )
        return _result(
            classification.success,
            plan,
            message=_classified_message(
                classification,
                result.stderr or result.stdout or "MySQL restore finished",
            ),
            copied_files=copied,
            classification=classification,
        )


def _scan_mysql_files(host: RemoteHost, directory: str) -> list[DumpArtifact]:
    q_dir = shlex.quote(directory)
    cmd = (
        f"find {q_dir} -maxdepth 1 -type f "
        "\\( -iname '*.sql' -o -iname '*.sql.gz' -o -iname '*.sql.zip' -o -iname '*.zip' \\) "
        "-printf '%p\\t%f\\t%s\\n'"
    )
    result = run_ssh_command(host, cmd, timeout=300)
    if result.returncode != 0:
        raise DiscoveryError(result.stderr or result.stdout or "MySQL file scan failed")
    files: list[DumpArtifact] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        files.append(
            DumpArtifact(
                remote_path=parts[0],
                filename=parts[1],
                size_bytes=int(parts[2] or 0),
            )
        )
    return files


def _verify_container_files(
    host: RemoteHost,
    *,
    container: str,
    docker_bin: str,
    container_path: str,
    copied_files: list[str],
) -> dict:
    names = [PurePosixPath(path).name for path in copied_files[:20]]
    checks = [
        f"test -f {shlex.quote(str(PurePosixPath(container_path) / name))} || echo MISSING:{shlex.quote(name)}"
        for name in names
    ]
    command = (
        f"{shlex.quote(docker_bin)} exec {shlex.quote(container)} "
        f"bash -lc {shlex.quote(' && '.join(checks))}"
    )
    result = run_ssh_command(host, command, timeout=300)
    missing = [
        line.removeprefix("MISSING:")
        for line in (result.stdout or "").splitlines()
        if line.startswith("MISSING:")
    ]
    if result.returncode != 0 or missing:
        raise RemoteAccessError(
            f"MySQL 容器内看不到复制后的文件：{missing or names}；容器目录 {container_path}"
        )
    return {"checked_files": names, "container_path": container_path}


def _prepare_database(executor: MySqlDockerExecutor, plan: MySqlRestorePlan) -> None:
    if plan.drop_existing:
        sql = (
            f"DROP DATABASE IF EXISTS `{_identifier(plan.database_name)}`; "
            f"CREATE DATABASE `{_identifier(plan.database_name)}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
        )
    else:
        sql = (
            f"CREATE DATABASE IF NOT EXISTS `{_identifier(plan.database_name)}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
        )
    result = executor.run_sql(sql, timeout=600)
    if result.returncode != 0:
        raise RemoteAccessError(result.stderr or result.stdout or "MySQL database prepare failed")


def _result(
    success: bool,
    plan: MySqlRestorePlan,
    *,
    message: str,
    copied_files: list[str] | None = None,
    classification: ImportResultClassification | None = None,
) -> dict:
    classification = classification or ImportResultClassification(success=success)
    return {
        "state": classification.state if success else TaskState.FAILED.value,
        "success": success,
        "message": message,
        "metadata": {
            "engine": "mysql",
            "mysql_method": plan.method,
            "database": plan.database_name,
            "target_schema": plan.database_name,
            "import_tool": "mysql",
            "files": [f.filename for f in plan.files],
            "copied_files": copied_files or [],
            "drop_existing": plan.drop_existing,
            **_classification_payload(classification),
        },
        "group_id": plan.database_name,
        "correction_attempts": 0,
    }


def _identifier(value: str) -> str:
    return value.replace("`", "``")


def _classification_payload(classification: ImportResultClassification) -> dict:
    return {
        "result_state": classification.state,
        "warning_only": classification.warning_only,
        "warning_errors": classification.warning_errors,
        "fatal_errors": classification.fatal_errors,
        "unknown_errors": classification.unknown_errors,
    }


def _classified_message(classification: ImportResultClassification, output: str) -> str:
    if classification.warning_only:
        warnings = "\n".join(classification.warning_errors)[-3000:]
        return f"{classification.summary}\n\n{warnings}"
    return (output or classification.summary)[-4000:]
