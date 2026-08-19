import asyncio
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.task_create import (
    EmbeddedMySqlTaskCreateRequest,
    EmbeddedOracleTaskCreateRequest,
    EmbeddedSqlServerTaskCreateRequest,
    TaskDetailResponse,
    TaskEventResponse,
    TaskCreateRequest,
    TaskResponse,
    TaskStopRequest,
)
from recovery_service.common.security import decrypt_secret, encrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.domain import RemoteHost
from recovery_service.core.models.task import RecoveryTask
from recovery_service.db.repositories.task_repo import TaskRepository
from recovery_service.orchestrator.oracle_auto_import_runner import OracleAutoImportRunner
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext
from recovery_service.services.database_connections import (
    get_profile,
    profile_to_cleanup_connection,
)
from recovery_service.settings import get_settings
from recovery_service.infrastructure.ssh.async_client import AsyncSSHClient
from recovery_service.services.task_events import record_task_event
from recovery_service.workers.tasks.run_recovery import run_recovery_task

router = APIRouter(prefix="/tasks", tags=["tasks"])

ORACLE_AUTO_IMPORT_RUNS_DIR = PurePosixPath(
    "/opt/oracle-recovery-service-package/oracle-auto-import-runs"
)


def _enqueue_recovery_task(task_id: uuid.UUID | str, *, volume_group_index: int = 0):
    settings = get_settings()
    return run_recovery_task.apply_async(
        args=[str(task_id)],
        kwargs={"volume_group_index": volume_group_index},
        queue=settings.celery_oracle_queue,
    )
ORACLE_LOG_ARCHIVE_LIMIT_BYTES = 500_000_000
ORACLE_LOG_FILE_LIMIT_BYTES = 200_000_000


def _secret_value(secret) -> str:
    return secret.get_secret_value() if secret else ""


async def _target_profile(db: AsyncSession, connection_id, engine: str):
    if not connection_id:
        return None
    try:
        profile = await get_profile(db, connection_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if profile.engine != engine:
        raise HTTPException(status_code=422, detail=f"请选择 {engine} 类型的数据库连接。")
    return profile


def _task_response(task: RecoveryTask, celery_task_id: str | None = None) -> TaskResponse:
    metadata = task.metadata_snapshot or {}
    options = task.options or {}
    engine = str(
        metadata.get("engine")
        or ("oracle" if options.get("embedded_oracle") or (options.get("professional_flow") or {}).get("oracle_docker") else "")
    ) or None
    return TaskResponse(
        id=task.id,
        state=task.state,
        current_policy_node=task.current_policy_node,
        progress_percent=task.progress_percent,
        error_message=task.error_message,
        metadata_snapshot=metadata,
        celery_task_id=celery_task_id,
        remote_host=task.remote_host,
        remote_directory=task.remote_directory,
        target_connection=task.target_connection,
        target_schema=metadata.get("schema") or metadata.get("username") or metadata.get("database"),
        import_tool=metadata.get("import_tool"),
        engine=engine,
        stop_requested=bool(task.stop_requested),
        force_stop_requested=bool(task.force_stop_requested),
        stop_reason=task.stop_reason,
        stop_requested_at=task.stop_requested_at,
        stopped_at=task.stopped_at,
        oracle_run_id=task.oracle_run_id,
        oracle_job_name=task.oracle_job_name,
        oracle_container=task.oracle_container,
        created_at=task.created_at,
        updated_at=task.updated_at,
        finished_at=task.finished_at,
    )


def _oracle_log_access(task: RecoveryTask) -> tuple[RemoteHost, PurePosixPath]:
    metadata = task.metadata_snapshot or {}
    run_dir_text = str(metadata.get("oracle_auto_import_run_dir") or "").strip()
    if not run_dir_text:
        raise HTTPException(status_code=404, detail="Oracle auto import logs are not available.")

    run_dir = PurePosixPath(run_dir_text)
    if run_dir == ORACLE_AUTO_IMPORT_RUNS_DIR or ORACLE_AUTO_IMPORT_RUNS_DIR not in run_dir.parents:
        raise HTTPException(status_code=409, detail="Oracle auto import log path is invalid.")

    professional = (task.options or {}).get("professional_flow") or {}
    oracle = professional.get("oracle_docker") or {}
    if not oracle.get("host") or not oracle.get("user"):
        raise HTTPException(status_code=409, detail="Oracle Docker SSH settings are unavailable.")

    encryption_key = get_settings().credential_encryption_key
    host = RemoteHost(
        host=str(oracle["host"]),
        port=int(oracle.get("port") or 22),
        username=str(oracle["user"]),
        password=decrypt_secret(str(oracle.get("password") or ""), encryption_key),
    )
    return host, run_dir


def _oracle_log_kind(relative_path: str) -> str:
    if relative_path == "run.log":
        return "command_log"
    if relative_path == "timeline.jsonl":
        return "timeline"
    if relative_path.endswith(".json"):
        return "report"
    for prefix in ("probe", "import", "cleanup"):
        if relative_path.startswith(prefix + "/"):
            return prefix
    return "log"


def _archive_iterator(archive):
    try:
        archive.seek(0)
        while True:
            chunk = archive.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        archive.close()


def _sanitize_oracle_log_artifact(relative_path: str, data: bytes) -> bytes:
    text_suffixes = (".json", ".jsonl", ".log", ".sql", ".txt", ".par")
    if not relative_path.lower().endswith(text_suffixes):
        return data
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    text = re.sub(
        r"(?i)(\b[A-Z][A-Z0-9_$#.-]*/)([^@\s'\"]+)(@[A-Z0-9_.:/-]+)",
        r"\1******\3",
        text,
    )
    text = re.sub(
        r"(?i)(IDENTIFIED\s+BY(?:\s+VALUES)?\s+)(\"[^\"]*\"|'[^']*'|\S+)",
        r'\1"******"',
        text,
    )
    text = re.sub(r"(?i)(--password\s+)(\S+)", r"\1******", text)
    text = re.sub(r"(?i)(PASSWORD\s*=\s*)(\S+)", r"\1******", text)
    return text.encode("utf-8")


@router.post("", response_model=TaskResponse, status_code=202)
async def create_task(
    body: TaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:submit")),
):
    settings = get_settings()
    enc = settings.credential_encryption_key
    options = dict(body.options)
    professional = None
    if body.source and body.oracle_docker and body.target:
        professional = {
            "source": {
                "host": body.source.host,
                "port": body.source.port,
                "user": body.source.user,
                "password": encrypt_secret(body.source.password.get_secret_value(), enc),
                "directory": body.source.directory,
            },
            "oracle_docker": {
                "host": body.oracle_docker.host,
                "port": body.oracle_docker.port,
                "user": body.oracle_docker.user,
                "password": encrypt_secret(body.oracle_docker.password.get_secret_value(), enc),
                "sudo_password": encrypt_secret(
                    body.oracle_docker.sudo_password.get_secret_value(), enc
                )
                if body.oracle_docker.sudo_password
                else "",
                "container": body.oracle_docker.container,
                "dmp_host_path": body.oracle_docker.dmp_host_path,
                "dmp_container_path": body.oracle_docker.dmp_container_path,
                "oracle_directory": body.oracle_docker.oracle_directory,
                "tablespace_container_path": body.oracle_docker.tablespace_container_path,
                "oracle_home_in_container": body.oracle_docker.oracle_home_in_container,
                "docker_bin": body.oracle_docker.docker_bin,
                "chmod_mode": body.oracle_docker.chmod_mode,
            },
            "target": {
                "connection": body.target.connection,
                "admin_user": body.target.admin_user,
                "admin_password": encrypt_secret(body.target.admin_password.get_secret_value(), enc),
                "generated_user_password": encrypt_secret(
                    (
                        body.target.generated_user_password
                        or body.target.admin_password
                    ).get_secret_value(),
                    enc,
                ),
                "default_temp_tablespace": body.target.default_temp_tablespace,
            },
        }
        options["professional_flow"] = professional
    if body.execution:
        options["execution"] = body.execution.model_dump()
    if body.auto_confirm is not None:
        options["auto_confirm"] = body.auto_confirm

    remote_host = body.source.host if body.source else body.remote_host
    remote_port = body.source.port if body.source else body.remote_port
    remote_user = body.source.user if body.source else body.remote_user
    remote_password = body.source.password if body.source else body.remote_password
    remote_directory = body.source.directory if body.source else body.remote_directory
    target_connection = body.target.connection if body.target else body.target_connection
    target_admin_user = body.target.admin_user if body.target else body.target_admin_user
    target_admin_password = body.target.admin_password if body.target else body.target_admin_password

    assert remote_host is not None
    assert remote_user is not None
    assert remote_password is not None
    assert remote_directory is not None
    assert target_connection is not None
    assert target_admin_user is not None
    assert target_admin_password is not None

    task = RecoveryTask(
        remote_host=remote_host,
        remote_port=remote_port,
        remote_user=remote_user,
        remote_password_enc=encrypt_secret(remote_password.get_secret_value(), enc),
        remote_directory=remote_directory,
        target_connection=target_connection,
        target_admin_user=target_admin_user,
        target_admin_password_enc=encrypt_secret(
            target_admin_password.get_secret_value(), enc
        ),
        options=options,
        state="created",
    )
    repo = TaskRepository(db)
    task = await repo.create(task)

    async_result = _enqueue_recovery_task(task.id, volume_group_index=body.volume_group_index)

    return _task_response(task, celery_task_id=async_result.id)


@router.post("/embedded-oracle", response_model=TaskResponse, status_code=202)
async def create_embedded_oracle_task(
    body: EmbeddedOracleTaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:submit")),
):
    settings = get_settings()
    enc = settings.credential_encryption_key
    profile = await _target_profile(db, body.target_connection_id, "oracle")
    conn = profile_to_cleanup_connection(profile) if profile else None
    conn_password = _secret_value(conn.password) if conn else ""
    conn_ssh_password = _secret_value(conn.ssh_password) if conn and conn.ssh_password else ""
    oracle_password = (
        body.oracle_password.get_secret_value()
        if body.oracle_password
        else conn_ssh_password or settings.oracle_docker_ssh_password
    )
    if not oracle_password:
        raise HTTPException(
            status_code=422,
            detail=(
                "Missing Oracle Docker host SSH password. "
                "Set ORACLE_DOCKER_SSH_PASSWORD in .env or submit oracle_password."
            ),
        )
    import_source_mode = body.import_source_mode
    direct_dmp_host_path = (body.direct_dmp_host_path or settings.oracle_dmp_host_path).strip()
    manual_dumpfile = (body.manual_dumpfile or "").strip()

    generated_password = (
        body.generated_user_password.get_secret_value()
        if body.generated_user_password
        else conn_password or settings.oracle_pwd
    )
    oracle_target_host = conn.host if conn else settings.oracle_target_host or settings.oracle_container_name
    oracle_target_port = (
        (conn.port or 1521)
        if conn
        else (settings.oracle_host_port if settings.oracle_target_host else 1521)
    )
    oracle_service = (conn.service_name or conn.database) if conn else settings.oracle_pdb
    oracle_admin_user = conn.username if conn else "SYSTEM"
    oracle_admin_password = conn_password or settings.oracle_pwd
    oracle_container = conn.container_name if conn and conn.container_name else settings.oracle_container_name
    source_config = {
        "host": body.source.host,
        "port": body.source.port,
        "user": body.source.user,
        "password": encrypt_secret(body.source.password.get_secret_value(), enc),
        "directory": body.source.directory,
    } if body.source else {
        "host": body.oracle_host or (conn.ssh_host if conn else None) or settings.oracle_docker_host,
        "port": body.oracle_port or (conn.ssh_port if conn else None) or settings.oracle_docker_ssh_port,
        "user": body.oracle_user or (conn.ssh_user if conn else None) or settings.oracle_docker_ssh_user,
        "password": encrypt_secret(oracle_password, enc),
        "directory": direct_dmp_host_path,
    }
    professional = {
        "source": source_config,
        "oracle_docker": {
            "host": body.oracle_host or (conn.ssh_host if conn else None) or settings.oracle_docker_host,
            "port": body.oracle_port or (conn.ssh_port if conn else None) or settings.oracle_docker_ssh_port,
            "user": body.oracle_user or (conn.ssh_user if conn else None) or settings.oracle_docker_ssh_user,
            "password": encrypt_secret(oracle_password, enc),
            "sudo_password": encrypt_secret(
                (
                    body.oracle_sudo_password.get_secret_value()
                    if body.oracle_sudo_password
                    else settings.oracle_docker_sudo_password
                ),
                enc,
            )
            if body.oracle_sudo_password or settings.oracle_docker_sudo_password
            else "",
            "container": oracle_container,
            "dmp_host_path": direct_dmp_host_path if import_source_mode == "direct" else settings.oracle_dmp_host_path,
            "dmp_container_path": settings.oracle_dmp_container_path,
            "oracle_directory": settings.oracle_directory,
            "tablespace_container_path": settings.oracle_tablespace_container_path,
            "oracle_home_in_container": settings.oracle_home_in_container,
            "docker_bin": "docker",
            "chmod_mode": "777",
        },
        "target": {
            "connection": f"{oracle_target_host}:{oracle_target_port}/{oracle_service}",
            "admin_user": oracle_admin_user,
            "admin_password": encrypt_secret(oracle_admin_password, enc),
            "generated_user_password": encrypt_secret(generated_password, enc),
            "default_temp_tablespace": "TEMP",
        },
        "import_source": {
            "mode": import_source_mode,
            "manual_dumpfile": manual_dumpfile,
            "direct_dmp_host_path": direct_dmp_host_path,
            "accept_export_log_gaps": body.accept_export_log_gaps,
        },
        "impdp": {},
    }
    impdp_options = professional["impdp"]
    if body.impdp_parallel:
        impdp_options["parallel"] = body.impdp_parallel
    if body.impdp_metrics is not None:
        impdp_options["metrics"] = body.impdp_metrics
    if body.impdp_logtime:
        impdp_options["logtime"] = body.impdp_logtime.strip().upper()
    if body.impdp_access_method:
        impdp_options["access_method"] = body.impdp_access_method.strip().upper()
    if body.impdp_disable_archive_logging:
        impdp_options["disable_archive_logging"] = True
    if body.impdp_table_exists_action:
        impdp_options["table_exists_action"] = body.impdp_table_exists_action.strip().upper()
    if body.impdp_index_mode:
        impdp_options["index_mode"] = body.impdp_index_mode.strip().lower()
    options = {
        "professional_flow": professional,
        "auto_confirm": body.auto_confirm,
        "embedded_oracle": True,
        "target_connection_profile": str(profile.id) if profile else None,
    }
    task = RecoveryTask(
        remote_host=source_config["host"],
        remote_port=source_config["port"],
        remote_user=source_config["user"],
        remote_password_enc=source_config["password"],
        remote_directory=source_config["directory"],
        target_connection=professional["target"]["connection"],
        target_admin_user=oracle_admin_user,
        target_admin_password_enc=encrypt_secret(oracle_admin_password, enc),
        options=options,
        state="created",
    )
    repo = TaskRepository(db)
    task = await repo.create(task)
    async_result = _enqueue_recovery_task(task.id, volume_group_index=body.volume_group_index)
    return _task_response(task, celery_task_id=async_result.id)


@router.post("/embedded-sqlserver", response_model=TaskResponse, status_code=202)
async def create_embedded_sqlserver_task(
    body: EmbeddedSqlServerTaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:submit")),
):
    settings = get_settings()
    enc = settings.credential_encryption_key
    profile = await _target_profile(db, body.target_connection_id, "sqlserver")
    conn = profile_to_cleanup_connection(profile) if profile else None
    conn_password = _secret_value(conn.password) if conn else ""
    conn_ssh_password = _secret_value(conn.ssh_password) if conn and conn.ssh_password else ""
    sqlserver_password = (
        body.sqlserver_password.get_secret_value()
        if body.sqlserver_password
        else conn_ssh_password or settings.sqlserver_docker_ssh_password
    )
    if not sqlserver_password:
        raise HTTPException(
            status_code=422,
            detail=(
                "Missing SQL Server Docker host SSH password. "
                "Set SQLSERVER_DOCKER_SSH_PASSWORD in .env or submit sqlserver_password."
            ),
        )
    sa_password = body.sa_password.get_secret_value() if body.sa_password else conn_password or settings.sqlserver_sa_password
    sqlserver_target_host = conn.host if conn else settings.sqlserver_target_host or settings.sqlserver_container_name
    sqlserver_target_port = (
        (conn.port or settings.sqlserver_host_port)
        if conn
        else settings.sqlserver_host_port
    )
    sqlserver_container = conn.container_name if conn and conn.container_name else settings.sqlserver_container_name
    sqlserver_flow = {
        "source": {
            "host": body.source.host,
            "port": body.source.port,
            "user": body.source.user,
            "password": encrypt_secret(body.source.password.get_secret_value(), enc),
            "directory": body.source.directory,
        },
        "sqlserver_docker": {
            "host": body.sqlserver_host or (conn.ssh_host if conn else None) or settings.sqlserver_docker_host,
            "port": body.sqlserver_port or (conn.ssh_port if conn else None) or settings.sqlserver_docker_ssh_port,
            "user": body.sqlserver_user or (conn.ssh_user if conn else None) or settings.sqlserver_docker_ssh_user,
            "password": encrypt_secret(sqlserver_password, enc),
            "sudo_password": encrypt_secret(
                (
                    body.sqlserver_sudo_password.get_secret_value()
                    if body.sqlserver_sudo_password
                    else settings.sqlserver_docker_sudo_password
                ),
                enc,
            )
            if body.sqlserver_sudo_password or settings.sqlserver_docker_sudo_password
            else "",
            "container": sqlserver_container,
            "sa_password": encrypt_secret(sa_password, enc),
            "file_host_path": settings.sqlserver_file_host_path,
            "data_host_path": settings.sqlserver_data_host_path,
            "file_container_path": settings.sqlserver_file_container_path,
            "data_container_path": settings.sqlserver_data_container_path,
            "docker_bin": "docker",
            "chmod_mode": "777",
        },
    }
    options = {
        "sqlserver_flow": sqlserver_flow,
        "auto_confirm": body.auto_confirm,
        "embedded_sqlserver": True,
        "target_connection_profile": str(profile.id) if profile else None,
    }
    task = RecoveryTask(
        remote_host=body.source.host,
        remote_port=body.source.port,
        remote_user=body.source.user,
        remote_password_enc=encrypt_secret(body.source.password.get_secret_value(), enc),
        remote_directory=body.source.directory,
        target_connection=f"{sqlserver_target_host}:{sqlserver_target_port}",
        target_admin_user="SA",
        target_admin_password_enc=encrypt_secret(sa_password, enc),
        options=options,
        state="created",
    )
    repo = TaskRepository(db)
    task = await repo.create(task)
    async_result = _enqueue_recovery_task(task.id, volume_group_index=body.volume_group_index)
    return _task_response(task, celery_task_id=async_result.id)


@router.post("/embedded-mysql", response_model=TaskResponse, status_code=202)
async def create_embedded_mysql_task(
    body: EmbeddedMySqlTaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:submit")),
):
    settings = get_settings()
    enc = settings.credential_encryption_key
    profile = await _target_profile(db, body.target_connection_id, "mysql")
    conn = profile_to_cleanup_connection(profile) if profile else None
    conn_password = _secret_value(conn.password) if conn else ""
    conn_ssh_password = _secret_value(conn.ssh_password) if conn and conn.ssh_password else ""
    mysql_password = (
        body.mysql_password.get_secret_value()
        if body.mysql_password
        else conn_ssh_password or settings.mysql_restore_docker_ssh_password
    )
    if not mysql_password:
        raise HTTPException(
            status_code=422,
            detail=(
                "Missing MySQL restore Docker host SSH password. "
                "Set MYSQL_RESTORE_DOCKER_SSH_PASSWORD in .env or submit mysql_password."
            ),
        )
    root_password = (
        body.root_password.get_secret_value()
        if body.root_password
        else conn_password or settings.mysql_restore_root_password
    )
    mysql_target_host = conn.host if conn else settings.mysql_restore_target_host or settings.mysql_restore_container_name
    mysql_target_port = (
        (conn.port or 3306)
        if conn
        else (settings.mysql_restore_host_port if settings.mysql_restore_target_host else 3306)
    )
    mysql_container = conn.container_name if conn and conn.container_name else settings.mysql_restore_container_name
    mysql_flow = {
        "source": {
            "host": body.source.host,
            "port": body.source.port,
            "user": body.source.user,
            "password": encrypt_secret(body.source.password.get_secret_value(), enc),
            "directory": body.source.directory,
        },
        "mysql_docker": {
            "host": body.mysql_host or (conn.ssh_host if conn else None) or settings.mysql_restore_docker_host,
            "port": body.mysql_port or (conn.ssh_port if conn else None) or settings.mysql_restore_docker_ssh_port,
            "user": body.mysql_user or (conn.ssh_user if conn else None) or settings.mysql_restore_docker_ssh_user,
            "password": encrypt_secret(mysql_password, enc),
            "sudo_password": encrypt_secret(
                (
                    body.mysql_sudo_password.get_secret_value()
                    if body.mysql_sudo_password
                    else settings.mysql_restore_docker_sudo_password
                ),
                enc,
            )
            if body.mysql_sudo_password or settings.mysql_restore_docker_sudo_password
            else "",
            "container": mysql_container,
            "root_password": encrypt_secret(root_password, enc),
            "backup_host_path": settings.mysql_restore_backup_host_path,
            "data_host_path": settings.mysql_restore_data_host_path,
            "backup_container_path": settings.mysql_restore_backup_container_path,
            "docker_bin": "docker",
            "chmod_mode": "777",
            "import_timeout_seconds": settings.mysql_restore_import_timeout_seconds,
        },
        "options": {
            "target_database": body.target_database,
            "drop_existing": body.drop_existing,
        },
    }
    options = {
        "mysql_flow": mysql_flow,
        "auto_confirm": body.auto_confirm,
        "embedded_mysql": True,
        "target_connection_profile": str(profile.id) if profile else None,
    }
    task = RecoveryTask(
        remote_host=body.source.host,
        remote_port=body.source.port,
        remote_user=body.source.user,
        remote_password_enc=encrypt_secret(body.source.password.get_secret_value(), enc),
        remote_directory=body.source.directory,
        target_connection=f"{mysql_target_host}:{mysql_target_port}",
        target_admin_user="root",
        target_admin_password_enc=encrypt_secret(root_password, enc),
        options=options,
        state="created",
    )
    repo = TaskRepository(db)
    task = await repo.create(task)
    async_result = _enqueue_recovery_task(task.id, volume_group_index=body.volume_group_index)
    return _task_response(task, celery_task_id=async_result.id)


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:read")),
):
    repo = TaskRepository(db)
    tasks = await repo.list_recent(limit=limit)
    return [_task_response(task) for task in tasks]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:read")),
):
    repo = TaskRepository(db)
    task = await repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(task)


@router.get("/{task_id}/detail", response_model=TaskDetailResponse)
async def get_task_detail(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:read")),
):
    repo = TaskRepository(db)
    task = await repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    events = await repo.list_events(task_id)
    return TaskDetailResponse(
        task=_task_response(task),
        events=[
            TaskEventResponse(
                id=event.id,
                event_type=event.event_type,
                title=event.title,
                status=event.status,
                message=event.message,
                payload=event.payload or {},
                stdout=event.stdout,
                stderr=event.stderr,
                created_at=event.created_at,
            )
            for event in events
        ],
    )


@router.get("/{task_id}/oracle-logs")
async def list_oracle_auto_import_logs(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:read")),
):
    repo = TaskRepository(db)
    task = await repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    host, run_dir = _oracle_log_access(task)
    client = AsyncSSHClient(host)
    try:
        await client.connect()
        files = await client.list_files_recursive(str(run_dir))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not list Oracle import logs: {exc}") from exc
    finally:
        await client.close()

    for item in files:
        item["kind"] = _oracle_log_kind(item["relative_path"])
        item.pop("remote_path", None)
    return {
        "task_id": str(task_id),
        "run_id": (task.metadata_snapshot or {}).get("oracle_auto_import_run_id"),
        "file_count": len(files),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "download_url": f"/api/v1/tasks/{task_id}/oracle-logs/download",
        "files": files,
    }


@router.get("/{task_id}/oracle-logs/download")
async def download_oracle_auto_import_logs(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:read")),
):
    repo = TaskRepository(db)
    task = await repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    host, run_dir = _oracle_log_access(task)
    client = AsyncSSHClient(host)
    archive = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
    try:
        await client.connect()
        files = await client.list_files_recursive(str(run_dir))
        total_size = sum(int(item["size_bytes"]) for item in files)
        if total_size > ORACLE_LOG_ARCHIVE_LIMIT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Oracle import logs exceed archive limit: {total_size} bytes.",
            )
        manifest = {
            "task_id": str(task_id),
            "run_id": (task.metadata_snapshot or {}).get("oracle_auto_import_run_id"),
            "files": [
                {
                    "relative_path": item["relative_path"],
                    "size_bytes": item["size_bytes"],
                    "modified_epoch": item["modified_epoch"],
                    "kind": _oracle_log_kind(item["relative_path"]),
                }
                for item in files
            ],
        }
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for item in files:
                data = await client.read_binary_file(
                    item["remote_path"],
                    max_bytes=ORACLE_LOG_FILE_LIMIT_BYTES,
                )
                bundle.writestr(
                    item["relative_path"],
                    _sanitize_oracle_log_artifact(item["relative_path"], data),
                )
    except HTTPException:
        archive.close()
        raise
    except Exception as exc:
        archive.close()
        raise HTTPException(status_code=502, detail=f"Could not download Oracle import logs: {exc}") from exc
    finally:
        await client.close()

    filename = f"oracle_import_logs_{task_id}.zip"
    return StreamingResponse(
        _archive_iterator(archive),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:cancel")),
):
    repo = TaskRepository(db)
    task = await repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.state in ("succeeded", "succeeded_with_warnings", "failed", "cancelled"):
        return _task_response(task)
    if task.state != "created":
        raise HTTPException(
            status_code=409,
            detail="Only queued tasks in created state can be cancelled safely.",
        )
    await repo.update_state(task, "cancelled", error="Cancelled by user before execution")
    return _task_response(task)


@router.post("/{task_id}/stop", response_model=TaskResponse)
async def stop_oracle_import_task(
    task_id: uuid.UUID,
    body: TaskStopRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("restore:cancel")),
):
    repo = TaskRepository(db)
    task = await repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.state in ("succeeded", "succeeded_with_warnings", "failed", "cancelled"):
        return _task_response(task)
    if task.state == "created":
        await repo.update_state(task, "cancelled", error="Cancelled by user before execution")
        return _task_response(task)
    if not (task.options or {}).get("embedded_oracle"):
        raise HTTPException(status_code=409, detail="运行中停止当前只支持 Oracle 19c 自动导入任务。")
    if not task.oracle_run_dir or not task.oracle_job_name or not task.oracle_container:
        raise HTTPException(status_code=409, detail="Oracle 19c 运行控制信息尚未建立，请稍后重试。")

    professional = (task.options or {}).get("professional_flow") or {}
    oracle = professional.get("oracle_docker") or {}
    target = professional.get("target") or {}
    encryption_key = get_settings().credential_encryption_key
    host = RemoteHost(
        host=str(oracle.get("host") or ""),
        port=int(oracle.get("port") or 22),
        username=str(oracle.get("user") or ""),
        password=decrypt_secret(str(oracle.get("password") or ""), encryption_key),
    )
    username = str(target.get("admin_user") or task.target_admin_user or "SYSTEM")
    password = decrypt_secret(str(target.get("admin_password") or ""), encryption_key)
    connection = str(target.get("connection") or task.target_connection or "")
    pdb = connection.rsplit("/", 1)[-1] if "/" in connection else ""
    reason = body.reason.strip()

    task.stop_requested = True
    task.force_stop_requested = bool(body.force)
    task.stop_reason = reason
    task.stop_requested_at = task.stop_requested_at or app_now()
    task.state = "stopping"
    task.error_message = "正在强制终止 Oracle 19c 导入。" if body.force else "正在停止 Oracle 19c 导入。"
    await db.commit()
    await db.refresh(task)

    record_task_event(
        task.id,
        event_type="oracle_import_stop",
        title="请求强制终止 Oracle 19c 导入" if body.force else "请求停止 Oracle 19c 导入",
        status="running",
        message=reason,
        payload={"job_name": task.oracle_job_name, "run_id": task.oracle_run_id, "force": body.force},
    )
    try:
        result = await asyncio.to_thread(
            OracleAutoImportRunner().stop,
            oracle_host=host,
            run_dir=task.oracle_run_dir,
            container=task.oracle_container,
            username=username,
            password=password,
            pdb=pdb,
            job_name=task.oracle_job_name,
            reason=reason,
            force=body.force,
        )
    except Exception as exc:
        task.error_message = f"停止命令派发失败：{exc}"
        await db.commit()
        record_task_event(
            task.id,
            event_type="oracle_import_stop",
            title="Oracle 19c 停止命令派发失败",
            status="failed",
            message=str(exc),
            payload={"job_name": task.oracle_job_name, "force": body.force},
        )
        raise HTTPException(status_code=502, detail=task.error_message) from exc

    record_task_event(
        task.id,
        event_type="oracle_import_stop",
        title="Oracle 19c 停止命令已派发",
        status="succeeded",
        message="等待 Worker 确认远端进程退出并收口为 cancelled。",
        payload=result,
    )
    await record_audit(
        db,
        actor,
        action="force_stop_oracle_import" if body.force else "stop_oracle_import",
        module="restore",
        target_type="recovery_task",
        target_id=str(task.id),
        target_name=task.oracle_job_name,
        payload={"reason": reason, "force": body.force, "result": result},
        request=request,
    )
    await db.refresh(task)
    return _task_response(task)


@router.post("/{task_id}/retry", response_model=TaskResponse, status_code=202)
async def retry_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:cancel")),
):
    repo = TaskRepository(db)
    task = await repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    retryable_states = {"failed", "cancelled", "succeeded", "succeeded_with_warnings"}
    if task.state not in retryable_states:
        raise HTTPException(
            status_code=409,
            detail=f"Task is {task.state}; only finished tasks can be retried.",
        )

    task.error_message = None
    task.finished_at = None
    task.current_policy_node = None
    task.progress_percent = 0.0
    task.correction_attempts = 0
    task.stop_requested = False
    task.force_stop_requested = False
    task.stop_reason = None
    task.stop_requested_at = None
    task.stopped_at = None
    task.oracle_run_id = None
    task.oracle_run_dir = None
    task.oracle_job_name = None
    task.oracle_container = None
    await repo.update_state(task, "created", error=None)
    async_result = _enqueue_recovery_task(task.id)
    return _task_response(task, celery_task_id=async_result.id)
