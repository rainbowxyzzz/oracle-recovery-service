import shlex
from pathlib import PurePosixPath

from recovery_service.common.security import decrypt_secret
from recovery_service.core.domain import DumpArtifact, RemoteHost
from recovery_service.core.enums import TaskState
from recovery_service.core.exceptions import DiscoveryError, RemoteAccessError
from recovery_service.engine.sqlserver.restore_plan import (
    SqlServerRestorePlan,
    choose_restore_plan,
    group_sqlserver_files,
)
from recovery_service.infrastructure.sqlserver.docker_executor import (
    SqlServerDockerExecutor,
    SqlServerDockerRuntime,
)
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command
from recovery_service.infrastructure.ssh.file_transfer import (
    chmod_remote_tree,
    copy_artifacts_between_hosts,
    ensure_remote_directory,
)
from recovery_service.services.task_events import record_task_event
from recovery_service.settings import get_settings


class SqlServerRecoveryPipeline:
    def run(self, config: dict, *, volume_group_index: int = 0) -> dict:
        settings = get_settings()
        enc = settings.credential_encryption_key
        task_id = config.get("_task_id")
        source = config["source"]
        sqlserver = config["sqlserver_docker"]

        source_host = RemoteHost(
            host=source["host"],
            port=int(source.get("port", 22)),
            username=source["user"],
            password=decrypt_secret(source["password"], enc),
        )
        sql_host = RemoteHost(
            host=sqlserver["host"],
            port=int(sqlserver.get("port", 22)),
            username=sqlserver["user"],
            password=decrypt_secret(sqlserver["password"], enc),
        )
        sudo_password = decrypt_secret(sqlserver.get("sudo_password", ""), enc)

        record_task_event(
            task_id,
            event_type="discover",
            title="扫描 SQL Server 备份目录",
            status="running",
            message=f"扫描源目录：{source['directory']}",
        )
        files = _scan_sqlserver_files(source_host, source["directory"])
        groups = group_sqlserver_files(files)
        if not groups:
            raise DiscoveryError("没有发现 .bak/.mdf/.ndf/.ldf 文件")
        if volume_group_index >= len(groups):
            raise DiscoveryError(f"volume_group_index {volume_group_index} out of range")

        group = groups[volume_group_index]
        plan = choose_restore_plan(group)
        record_task_event(
            task_id,
            event_type="plan",
            title="生成 SQL Server 恢复计划",
            status="succeeded",
            message=plan.reason,
            payload={
                "method": plan.method,
                "database": plan.database_name,
                "files": [f.filename for f in plan.files],
                "all_groups": [
                    {
                        "group_id": g.group_id,
                        "bak": [f.filename for f in g.bak_files],
                        "mdf": [f.filename for f in g.mdf_files],
                        "ndf": [f.filename for f in g.ndf_files],
                        "ldf": [f.filename for f in g.ldf_files],
                    }
                    for g in groups
                ],
            },
        )

        if plan.method.startswith("unsupported"):
            return _result(False, plan, message=plan.reason)

        ensure_remote_directory(
            sql_host,
            sqlserver["file_host_path"],
            mode=sqlserver.get("chmod_mode") or "777",
            sudo_password=sudo_password,
        )
        ensure_remote_directory(
            sql_host,
            sqlserver["data_host_path"],
            mode=sqlserver.get("chmod_mode") or "777",
            sudo_password=sudo_password,
        )
        copied = copy_artifacts_between_hosts(
            source_host,
            sql_host,
            plan.files,
            sqlserver["file_host_path"],
        )
        chmod_remote_tree(
            sql_host,
            sqlserver["file_host_path"],
            mode=sqlserver.get("chmod_mode") or "777",
            sudo_password=sudo_password,
        )
        visibility = _verify_container_files(
            sql_host,
            container=sqlserver["container"],
            docker_bin=sqlserver.get("docker_bin") or "docker",
            container_path=sqlserver["file_container_path"],
            copied_files=copied,
        )
        record_task_event(
            task_id,
            event_type="copy",
            title="SQL Server 文件复制完成",
            status="succeeded",
            message="备份/数据文件已复制并在容器内可见。",
            payload={
                "copied_files": copied,
                "file_host_path": sqlserver["file_host_path"],
                "file_container_path": sqlserver["file_container_path"],
                "visibility": visibility,
            },
        )

        executor = SqlServerDockerExecutor(
            SqlServerDockerRuntime(
                host=sql_host,
                container=sqlserver["container"],
                sa_password=decrypt_secret(sqlserver["sa_password"], enc),
                docker_bin=sqlserver.get("docker_bin") or "docker",
            )
        )
        if plan.method == "bak":
            result = _restore_bak(executor, plan, sqlserver["file_container_path"], sqlserver["data_container_path"])
        elif plan.method == "attach":
            result = _attach_files(executor, plan, sqlserver["file_container_path"], rebuild_log=False)
        else:
            result = _attach_files(executor, plan, sqlserver["file_container_path"], rebuild_log=True)

        record_task_event(
            task_id,
            event_type="sqlserver",
            title="SQL Server 恢复执行完成",
            status="succeeded" if result.returncode == 0 else "failed",
            message=f"sqlcmd returncode={result.returncode}",
            payload={
                "method": plan.method,
                "database": plan.database_name,
                "executed_command": result.command[0] if result.command else "",
            },
            stdout=result.stdout,
            stderr=result.stderr,
        )
        return _result(
            result.returncode == 0,
            plan,
            message=(result.stderr or result.stdout or "SQL Server restore finished")[-4000:],
            copied_files=copied,
        )


def _scan_sqlserver_files(host: RemoteHost, directory: str) -> list[DumpArtifact]:
    q_dir = shlex.quote(directory)
    cmd = (
        f"find {q_dir} -maxdepth 1 -type f "
        "\\( -iname '*.bak' -o -iname '*.mdf' -o -iname '*.ndf' -o -iname '*.ldf' \\) "
        "-printf '%p\\t%f\\t%s\\n'"
    )
    result = run_ssh_command(host, cmd, timeout=300)
    if result.returncode != 0:
        raise DiscoveryError(result.stderr or result.stdout or "SQL Server file scan failed")
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
            f"SQL Server 容器内看不到复制后的文件：{missing or names}；容器目录={container_path}"
        )
    return {"checked_files": names, "container_path": container_path}


def _restore_bak(
    executor: SqlServerDockerExecutor,
    plan: SqlServerRestorePlan,
    file_container_path: str,
    data_container_path: str,
):
    bak_path = str(PurePosixPath(file_container_path) / plan.files[0].filename)
    logicals = _read_filelist(executor, bak_path)
    moves: list[str] = []
    data_index = 0
    log_index = 0
    for logical, file_type in logicals:
        if file_type == "L":
            log_index += 1
            target = str(PurePosixPath(data_container_path) / f"{plan.database_name}_log{log_index}.ldf")
        else:
            data_index += 1
            suffix = ".mdf" if data_index == 1 else f"_{data_index}.ndf"
            target = str(PurePosixPath(data_container_path) / f"{plan.database_name}{suffix}")
        moves.append(f"MOVE N'{_sql_string(logical)}' TO N'{_sql_string(target)}'")
    move_sql = ",\n  ".join(moves)
    sql = (
        f"RESTORE DATABASE [{_bracket(plan.database_name)}]\n"
        f"FROM DISK = N'{_sql_string(bak_path)}'\n"
        f"WITH {move_sql}, REPLACE, RECOVERY;"
    )
    return executor.run_sql(sql)


def _read_filelist(executor: SqlServerDockerExecutor, bak_path: str) -> list[tuple[str, str]]:
    sql = f"""
SET NOCOUNT ON;
DECLARE @f TABLE (
  LogicalName nvarchar(128), PhysicalName nvarchar(260), [Type] char(1),
  FileGroupName nvarchar(128) NULL, Size numeric(20,0), MaxSize numeric(20,0),
  FileId bigint, CreateLSN numeric(25,0) NULL, DropLSN numeric(25,0) NULL,
  UniqueId uniqueidentifier, ReadOnlyLSN numeric(25,0) NULL, ReadWriteLSN numeric(25,0) NULL,
  BackupSizeInBytes bigint, SourceBlockSize int, FileGroupId int NULL,
  LogGroupGUID uniqueidentifier NULL, DifferentialBaseLSN numeric(25,0) NULL,
  DifferentialBaseGUID uniqueidentifier NULL, IsReadOnly bit, IsPresent bit,
  TDEThumbprint varbinary(32) NULL, SnapshotUrl nvarchar(360) NULL
);
INSERT INTO @f EXEC(N'RESTORE FILELISTONLY FROM DISK = N''{_sql_string(bak_path)}''');
SELECT LogicalName + N'|' + [Type] FROM @f;
"""
    result = executor.run_sql(sql, timeout=1200)
    if result.returncode != 0:
        raise RemoteAccessError(result.stderr or result.stdout or "RESTORE FILELISTONLY failed")
    rows: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        logical, file_type = [part.strip() for part in line.split("|", 1)]
        if logical and file_type in {"D", "L", "F", "S"}:
            rows.append((logical, file_type))
    if not rows:
        raise RemoteAccessError("无法解析 RESTORE FILELISTONLY 输出，不能安全生成 WITH MOVE")
    return rows


def _attach_files(
    executor: SqlServerDockerExecutor,
    plan: SqlServerRestorePlan,
    file_container_path: str,
    *,
    rebuild_log: bool,
):
    data_files = [
        f
        for f in plan.files
        if PurePosixPath(f.filename).suffix.lower() in {".mdf", ".ndf"}
    ]
    log_files = [f for f in plan.files if PurePosixPath(f.filename).suffix.lower() == ".ldf"]
    data_sql = ",\n  ".join(
        f"(FILENAME = N'{_sql_string(str(PurePosixPath(file_container_path) / f.filename))}')"
        for f in data_files
    )
    if rebuild_log:
        sql = f"CREATE DATABASE [{_bracket(plan.database_name)}]\nON {data_sql}\nFOR ATTACH_REBUILD_LOG;"
        return executor.run_sql(sql)
    log_sql = ",\n  ".join(
        f"(FILENAME = N'{_sql_string(str(PurePosixPath(file_container_path) / f.filename))}')"
        for f in log_files
    )
    sql = f"CREATE DATABASE [{_bracket(plan.database_name)}]\nON {data_sql}\nLOG ON {log_sql}\nFOR ATTACH;"
    return executor.run_sql(sql)


def _result(success: bool, plan: SqlServerRestorePlan, *, message: str, copied_files=None) -> dict:
    return {
        "state": TaskState.SUCCEEDED.value if success else TaskState.FAILED.value,
        "success": success,
        "message": message,
        "metadata": {
            "engine": "sqlserver",
            "sqlserver_method": plan.method,
            "database": plan.database_name,
            "target_schema": plan.database_name,
            "import_tool": "sqlcmd",
            "files": [f.filename for f in plan.files],
            "copied_files": copied_files or [],
        },
        "group_id": plan.database_name,
        "correction_attempts": 0,
    }


def _sql_string(value: str) -> str:
    return value.replace("'", "''")


def _bracket(value: str) -> str:
    return value.replace("]", "]]")
