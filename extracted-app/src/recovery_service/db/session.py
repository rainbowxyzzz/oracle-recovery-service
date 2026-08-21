import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from recovery_service.settings import get_settings

_async_engine: AsyncEngine | None = None
_sync_engine = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def _ensure_engines() -> None:
    global _async_engine, _sync_engine, _async_session_factory, _sync_session_factory
    if _async_engine is not None:
        return
    settings = get_settings()
    _async_engine = create_async_engine(
        settings.database_url,
        echo=settings.app_env == "development",
        pool_pre_ping=True,
        pool_recycle=3600,
    )
    _sync_engine = create_engine(
        settings.database_url_sync,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=3600,
    )
    if settings.mysql_session_time_zone:
        event.listen(_async_engine.sync_engine, "connect", _set_mysql_session_time_zone)
        event.listen(_sync_engine, "connect", _set_mysql_session_time_zone)
    _async_session_factory = async_sessionmaker(_async_engine, expire_on_commit=False)
    _sync_session_factory = sessionmaker(_sync_engine, expire_on_commit=False)


def _set_mysql_session_time_zone(dbapi_connection, _connection_record) -> None:
    settings = get_settings()
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET time_zone = '{settings.mysql_session_time_zone}'")
    finally:
        cursor.close()


def get_async_engine() -> AsyncEngine:
    _ensure_engines()
    assert _async_engine is not None
    return _async_engine


def get_sync_session_factory() -> sessionmaker[Session]:
    _ensure_engines()
    assert _sync_session_factory is not None
    return _sync_session_factory


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    _ensure_engines()
    assert _async_session_factory is not None
    async with _async_session_factory() as session:
        yield session


async def init_db() -> None:
    from recovery_service.core.models.task import Base
    from recovery_service.services.auth import ensure_default_admin

    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_recovery_task_stop_columns(conn)
        await _ensure_user_columns(conn)
        await _ensure_doris_sm3_job_columns(conn)
        await _ensure_doris_sm3_audit_columns(conn)
        await _ensure_doris_sm4_key_and_batch_columns(conn)
        await _ensure_doris_sm4_function_deployment_columns(conn)
        await _ensure_doris_sm4_auto_snapshot_columns(conn)
        await _ensure_doris_sm4_schedule_columns(conn)
        await _ensure_doris_sm4_task_definition_columns(conn)
        await _ensure_doris_sm3_task_definition_columns(conn)
        await _ensure_batch_authorization_columns(conn)
        await _ensure_resource_provisioning_columns(conn)
        await _ensure_resource_permission_columns(conn)
        await _ensure_doris_csv_task_columns(conn)
        await _ensure_data_platform_folder_columns(conn)
        await _ensure_data_platform_workflow_metadata_columns(conn)
        await _ensure_data_platform_node_columns(conn)
        await _ensure_data_platform_version_columns(conn)
        await _ensure_data_platform_indexes(conn)
    async for session in get_async_session():
        await ensure_default_admin(session)
        break
    from recovery_service.services.data_platform import backfill_data_platform_release_metadata

    await asyncio.to_thread(backfill_data_platform_release_metadata)


async def _ensure_user_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    if not columns:
        return
    migrations = {
        "permissions": "ALTER TABLE users ADD COLUMN permissions JSON NULL",
    }
    for column, sql in migrations.items():
        if column not in columns:
            await conn.execute(text(sql))


async def _ensure_resource_provisioning_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'resource_provisioning_batches'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    migrations = {
        "youdata_login_name": (
            "ALTER TABLE resource_provisioning_batches "
            "ADD COLUMN youdata_login_name VARCHAR(255) NULL AFTER api_token_enc"
        ),
        "youdata_password_enc": (
            "ALTER TABLE resource_provisioning_batches "
            "ADD COLUMN youdata_password_enc LONGTEXT NULL AFTER youdata_login_name"
        ),
        "youdata_token_url": (
            "ALTER TABLE resource_provisioning_batches "
            "ADD COLUMN youdata_token_url VARCHAR(1024) NULL AFTER youdata_password_enc"
        ),
    }
    for column, sql in migrations.items():
        if columns and column not in columns:
            await conn.execute(text(sql))


async def _ensure_resource_permission_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    columns = await _table_columns(conn, "resource_permission_rows")
    migrations = {
        "role_id": (
            "ALTER TABLE resource_permission_rows "
            "ADD COLUMN role_id BIGINT NULL AFTER resource_id"
        ),
        "role_delete_state": (
            "ALTER TABLE resource_permission_rows "
            "ADD COLUMN role_delete_state VARCHAR(32) NOT NULL DEFAULT 'not_created' AFTER role_id"
        ),
        "role_delete_message": (
            "ALTER TABLE resource_permission_rows "
            "ADD COLUMN role_delete_message TEXT NULL AFTER role_delete_state"
        ),
        "role_deleted_at": (
            "ALTER TABLE resource_permission_rows "
            "ADD COLUMN role_deleted_at DATETIME NULL AFTER role_delete_message"
        ),
    }
    for column, sql in migrations.items():
        if columns and column not in columns:
            await conn.execute(text(sql))
    indexes = await _table_indexes(conn, "resource_permission_rows")
    for index_name, column_name in (
        ("ix_resource_permission_rows_role_id", "role_id"),
        ("ix_resource_permission_rows_role_delete_state", "role_delete_state"),
    ):
        if columns and index_name not in indexes:
            await conn.execute(
                text(f"CREATE INDEX {index_name} ON resource_permission_rows ({column_name})")
            )


async def _ensure_recovery_task_stop_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    columns = await _table_columns(conn, "recovery_tasks")
    migrations = {
        "stop_requested": "ALTER TABLE recovery_tasks ADD COLUMN stop_requested BOOL NOT NULL DEFAULT 0",
        "force_stop_requested": "ALTER TABLE recovery_tasks ADD COLUMN force_stop_requested BOOL NOT NULL DEFAULT 0",
        "stop_reason": "ALTER TABLE recovery_tasks ADD COLUMN stop_reason TEXT NULL",
        "stop_requested_at": "ALTER TABLE recovery_tasks ADD COLUMN stop_requested_at DATETIME NULL",
        "stopped_at": "ALTER TABLE recovery_tasks ADD COLUMN stopped_at DATETIME NULL",
        "oracle_run_id": "ALTER TABLE recovery_tasks ADD COLUMN oracle_run_id VARCHAR(128) NULL",
        "oracle_run_dir": "ALTER TABLE recovery_tasks ADD COLUMN oracle_run_dir VARCHAR(1024) NULL",
        "oracle_job_name": "ALTER TABLE recovery_tasks ADD COLUMN oracle_job_name VARCHAR(128) NULL",
        "oracle_container": "ALTER TABLE recovery_tasks ADD COLUMN oracle_container VARCHAR(255) NULL",
    }
    for column, sql in migrations.items():
        if columns and column not in columns:
            await conn.execute(text(sql))


async def _ensure_data_platform_folder_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'data_platform_workflows'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    if columns and "folder_id" not in columns:
        await conn.execute(text("ALTER TABLE data_platform_workflows ADD COLUMN folder_id CHAR(32) NULL"))
        await conn.execute(text("CREATE INDEX ix_data_platform_workflows_folder_id ON data_platform_workflows (folder_id)"))


async def _ensure_data_platform_workflow_metadata_columns(conn) -> None:
    if conn.dialect.name not in {"mysql", "mariadb"}:
        return
    workflow_columns = await _table_columns(conn, "data_platform_workflows")
    if workflow_columns and "business_metadata" not in workflow_columns:
        await conn.execute(
            text("ALTER TABLE data_platform_workflows ADD COLUMN business_metadata JSON NULL AFTER description")
        )


async def _ensure_data_platform_node_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    columns = await _table_columns(conn, "data_platform_nodes")
    if columns and "revision" not in columns:
        await conn.execute(
            text("ALTER TABLE data_platform_nodes ADD COLUMN revision INT NOT NULL DEFAULT 1 AFTER name")
        )
        await conn.execute(text("CREATE INDEX ix_data_platform_nodes_revision ON data_platform_nodes (revision)"))


async def _ensure_data_platform_version_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    version_columns = await _table_columns(conn, "data_platform_workflow_versions")
    version_migrations = {
        "business_metadata": "ALTER TABLE data_platform_workflow_versions ADD COLUMN business_metadata JSON NULL AFTER edges",
        "release_snapshot": "ALTER TABLE data_platform_workflow_versions ADD COLUMN release_snapshot JSON NULL AFTER edges",
        "execution_content_hash": "ALTER TABLE data_platform_workflow_versions ADD COLUMN execution_content_hash VARCHAR(64) NULL AFTER release_snapshot",
    }
    for column, sql in version_migrations.items():
        if version_columns and column not in version_columns:
            await conn.execute(text(sql))
    run_columns = await _table_columns(conn, "data_platform_workflow_runs")
    if run_columns and "trigger_context" not in run_columns:
        await conn.execute(text("ALTER TABLE data_platform_workflow_runs ADD COLUMN trigger_context JSON NULL AFTER trigger_type"))


async def _ensure_data_platform_indexes(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    index_specs = (
        ("data_platform_nodes", "ix_data_platform_nodes_updated_at", "updated_at"),
        ("data_platform_workflows", "ix_data_platform_workflows_updated_at", "updated_at"),
        ("data_platform_workflow_versions", "ix_data_platform_workflow_versions_updated_at", "updated_at"),
    )
    for table_name, index_name, column_name in index_specs:
        columns = await _table_columns(conn, table_name)
        if column_name not in columns:
            continue
        indexes = await _table_indexes(conn, table_name)
        if index_name not in indexes:
            await conn.execute(text(f"CREATE INDEX {index_name} ON {table_name} ({column_name})"))


async def _ensure_doris_sm3_task_definition_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    columns = await _table_columns(conn, "doris_sm3_task_definitions")
    if columns and "revision" not in columns:
        await conn.execute(text("ALTER TABLE doris_sm3_task_definitions ADD COLUMN revision INT NOT NULL DEFAULT 1 AFTER name"))


async def _table_columns(conn, table_name: str) -> set[str]:
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {str(row[0]) for row in result.fetchall()}


async def _table_indexes(conn, table_name: str) -> set[str]:
    result = await conn.execute(
        text(
            """
            SELECT INDEX_NAME
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {str(row[0]) for row in result.fetchall()}


async def _ensure_doris_csv_task_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return

    task_columns = await _table_columns(conn, "doris_csv_parse_tasks")
    task_migrations = {
        "import_create_table": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_create_table BOOL NOT NULL DEFAULT 1",
        "import_overwrite": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_overwrite BOOL NOT NULL DEFAULT 0",
        "import_requested_at": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_requested_at DATETIME NULL",
        "import_started_at": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_started_at DATETIME NULL",
        "import_finished_at": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_finished_at DATETIME NULL",
        "import_total_files": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_total_files INT NOT NULL DEFAULT 0",
        "imported_files": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN imported_files INT NOT NULL DEFAULT 0",
        "import_failed_files": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_failed_files INT NOT NULL DEFAULT 0",
        "import_total_rows": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_total_rows INT NOT NULL DEFAULT 0",
        "import_loaded_rows": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_loaded_rows INT NOT NULL DEFAULT 0",
        "import_filtered_rows": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN import_filtered_rows INT NOT NULL DEFAULT 0",
        "result": "ALTER TABLE doris_csv_parse_tasks ADD COLUMN result JSON NULL",
    }
    for column, sql in task_migrations.items():
        if task_columns and column not in task_columns:
            await conn.execute(text(sql))

    file_columns = await _table_columns(conn, "doris_csv_parse_files")
    file_migrations = {
        "processed_bytes": "ALTER TABLE doris_csv_parse_files ADD COLUMN processed_bytes BIGINT NOT NULL DEFAULT 0",
        "preview": "ALTER TABLE doris_csv_parse_files ADD COLUMN preview JSON NULL",
        "warnings": "ALTER TABLE doris_csv_parse_files ADD COLUMN warnings JSON NULL",
    }
    for column, sql in file_migrations.items():
        if file_columns and column not in file_columns:
            await conn.execute(text(sql))

    log_columns = await _table_columns(conn, "doris_csv_task_logs")
    log_migrations = {
        "file_id": "ALTER TABLE doris_csv_task_logs ADD COLUMN file_id CHAR(32) NULL",
        "stage": "ALTER TABLE doris_csv_task_logs ADD COLUMN stage VARCHAR(64) NULL",
        "payload": "ALTER TABLE doris_csv_task_logs ADD COLUMN payload JSON NULL",
    }
    for column, sql in log_migrations.items():
        if log_columns and column not in log_columns:
            await conn.execute(text(sql))


async def _ensure_doris_sm3_job_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'doris_sm3_jobs'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    if not columns:
        return
    migrations = {
        "celery_task_id": "ALTER TABLE doris_sm3_jobs ADD COLUMN celery_task_id VARCHAR(255) NULL",
        "connection_name": "ALTER TABLE doris_sm3_jobs ADD COLUMN connection_name VARCHAR(128) NULL",
        "backup_table_name": "ALTER TABLE doris_sm3_jobs ADD COLUMN backup_table_name VARCHAR(255) NULL",
        "output_table_name": "ALTER TABLE doris_sm3_jobs ADD COLUMN output_table_name VARCHAR(255) NULL",
        "mapping_database": "ALTER TABLE doris_sm3_jobs ADD COLUMN mapping_database VARCHAR(255) NULL",
        "field_mapping_database": "ALTER TABLE doris_sm3_jobs ADD COLUMN field_mapping_database VARCHAR(255) NULL",
        "field_mapping_table": "ALTER TABLE doris_sm3_jobs ADD COLUMN field_mapping_table VARCHAR(255) NULL",
        "created_by_user_id": "ALTER TABLE doris_sm3_jobs ADD COLUMN created_by_user_id CHAR(32) NULL",
        "created_by_username": "ALTER TABLE doris_sm3_jobs ADD COLUMN created_by_username VARCHAR(64) NULL",
        "created_by_auth_type": "ALTER TABLE doris_sm3_jobs ADD COLUMN created_by_auth_type VARCHAR(32) NULL DEFAULT 'api-key'",
        "current_step": "ALTER TABLE doris_sm3_jobs ADD COLUMN current_step VARCHAR(128) NULL",
        "source_rows": "ALTER TABLE doris_sm3_jobs ADD COLUMN source_rows INT NULL",
        "target_rows": "ALTER TABLE doris_sm3_jobs ADD COLUMN target_rows INT NULL",
        "cancel_requested": "ALTER TABLE doris_sm3_jobs ADD COLUMN cancel_requested BOOL DEFAULT 0",
        "active_query_id": "ALTER TABLE doris_sm3_jobs ADD COLUMN active_query_id VARCHAR(255) NULL",
        "error_message": "ALTER TABLE doris_sm3_jobs ADD COLUMN error_message TEXT NULL",
        "started_at": "ALTER TABLE doris_sm3_jobs ADD COLUMN started_at DATETIME NULL",
        "finished_at": "ALTER TABLE doris_sm3_jobs ADD COLUMN finished_at DATETIME NULL",
    }
    for column, sql in migrations.items():
        if column not in columns:
            await conn.execute(text(sql))


async def _ensure_doris_sm3_audit_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'doris_sm3_audits'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    if not columns:
        return
    migrations = {
        "mapping_database": "ALTER TABLE doris_sm3_audits ADD COLUMN mapping_database VARCHAR(255) NULL",
    }
    for column, sql in migrations.items():
        if column not in columns:
            await conn.execute(text(sql))


async def _ensure_doris_sm4_schedule_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'doris_sm4_schedules'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    if not columns:
        return
    migrations = {
        "archived_at": "ALTER TABLE doris_sm4_schedules ADD COLUMN archived_at DATETIME NULL",
        "archived_by_user_id": "ALTER TABLE doris_sm4_schedules ADD COLUMN archived_by_user_id CHAR(32) NULL",
        "archived_by_username": "ALTER TABLE doris_sm4_schedules ADD COLUMN archived_by_username VARCHAR(64) NULL",
        "archived_reason": "ALTER TABLE doris_sm4_schedules ADD COLUMN archived_reason TEXT NULL",
        "deleted_at": "ALTER TABLE doris_sm4_schedules ADD COLUMN deleted_at DATETIME NULL",
        "deleted_by_user_id": "ALTER TABLE doris_sm4_schedules ADD COLUMN deleted_by_user_id CHAR(32) NULL",
        "deleted_by_username": "ALTER TABLE doris_sm4_schedules ADD COLUMN deleted_by_username VARCHAR(64) NULL",
        "delete_reason": "ALTER TABLE doris_sm4_schedules ADD COLUMN delete_reason TEXT NULL",
    }
    for column, sql in migrations.items():
        if column not in columns:
            await conn.execute(text(sql))


async def _ensure_doris_sm4_auto_snapshot_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'doris_sm4_auto_snapshot_tasks'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    migrations = {
        "exclude_tables": "ALTER TABLE doris_sm4_auto_snapshot_tasks ADD COLUMN exclude_tables JSON NULL AFTER exclude_databases",
    }
    for column, sql in migrations.items():
        if columns and column not in columns:
            await conn.execute(text(sql))


async def _ensure_doris_sm4_task_definition_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'doris_sm4_task_definitions'
            """
        )
    )
    columns = {str(row[0]) for row in result.fetchall()}
    if columns and "revision" not in columns:
        await conn.execute(
            text(
                "ALTER TABLE doris_sm4_task_definitions "
                "ADD COLUMN revision INT NOT NULL DEFAULT 1 AFTER name"
            )
        )


async def _ensure_doris_sm4_key_and_batch_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    key_result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'doris_sm4_key_versions'
            """
        )
    )
    key_columns = {str(row[0]) for row in key_result.fetchall()}
    key_migrations = {
        "connection_id": "ALTER TABLE doris_sm4_key_versions ADD COLUMN connection_id CHAR(32) NULL AFTER id",
        "connection_name": "ALTER TABLE doris_sm4_key_versions ADD COLUMN connection_name VARCHAR(128) NULL AFTER connection_id",
    }
    for column, sql in key_migrations.items():
        if key_columns and column not in key_columns:
            await conn.execute(text(sql))

    batch_result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'doris_sm4_batch_jobs'
            """
        )
    )
    batch_columns = {str(row[0]) for row in batch_result.fetchall()}
    batch_migrations = {
        "sm4_key_version_id": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN sm4_key_version_id CHAR(32) NULL AFTER `database`",
        "sm4_key_fingerprint": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN sm4_key_fingerprint VARCHAR(64) NULL AFTER sm4_key_version_id",
        "execution_window_enabled": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN execution_window_enabled BOOL NOT NULL DEFAULT 0 AFTER target_suffix",
        "execution_window_start": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN execution_window_start VARCHAR(16) NULL AFTER execution_window_enabled",
        "execution_window_end": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN execution_window_end VARCHAR(16) NULL AFTER execution_window_start",
        "allow_running_cross_window": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN allow_running_cross_window BOOL NOT NULL DEFAULT 1 AFTER execution_window_end",
        "auto_snapshot": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN auto_snapshot BOOL NOT NULL DEFAULT 0 AFTER allow_running_cross_window",
        "auto_snapshot_config": "ALTER TABLE doris_sm4_batch_jobs ADD COLUMN auto_snapshot_config JSON NULL AFTER auto_snapshot",
    }
    for column, sql in batch_migrations.items():
        if batch_columns and column not in batch_columns:
            await conn.execute(text(sql))


async def _ensure_doris_sm4_function_deployment_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    result = await conn.execute(text("""
        SELECT COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'doris_sm4_function_deployments'
    """))
    columns = {str(row[0]) for row in result.fetchall()}
    migrations = {
        "encrypt_enabled": "ALTER TABLE doris_sm4_function_deployments ADD COLUMN encrypt_enabled BOOL NOT NULL DEFAULT 1 AFTER jar_filename",
        "decrypt_enabled": "ALTER TABLE doris_sm4_function_deployments ADD COLUMN decrypt_enabled BOOL NOT NULL DEFAULT 1 AFTER encrypt_enabled",
    }
    for column, sql in migrations.items():
        if columns and column not in columns:
            await conn.execute(text(sql))


async def _ensure_batch_authorization_columns(conn) -> None:
    dialect = conn.dialect.name
    if dialect not in {"mysql", "mariadb"}:
        return
    table_result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'batch_auth_grant_tables'
            """
        )
    )
    table_columns = {str(row[0]) for row in table_result.fetchall()}
    migrations = {
        "source_object_level": "ALTER TABLE batch_auth_grant_tables ADD COLUMN source_object_level VARCHAR(128) NULL AFTER source_table",
    }
    for column, sql in migrations.items():
        if table_columns and column not in table_columns:
            await conn.execute(text(sql))

    user_result = await conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'batch_auth_grant_users'
            """
        )
    )
    user_columns = {str(row[0]) for row in user_result.fetchall()}
    user_migrations = {
        "lease_id": "ALTER TABLE batch_auth_grant_users ADD COLUMN lease_id CHAR(32) NULL AFTER table_id",
        "privilege_existed_before": "ALTER TABLE batch_auth_grant_users ADD COLUMN privilege_existed_before BOOL NOT NULL DEFAULT 0 AFTER revoke_state",
        "granted_by_this_batch": "ALTER TABLE batch_auth_grant_users ADD COLUMN granted_by_this_batch BOOL NOT NULL DEFAULT 1 AFTER privilege_existed_before",
        "revoke_decision": "ALTER TABLE batch_auth_grant_users ADD COLUMN revoke_decision VARCHAR(64) NULL AFTER granted_by_this_batch",
        "revoke_decision_reason": "ALTER TABLE batch_auth_grant_users ADD COLUMN revoke_decision_reason TEXT NULL AFTER revoke_decision",
        "checked_before_grant_at": "ALTER TABLE batch_auth_grant_users ADD COLUMN checked_before_grant_at DATETIME NULL AFTER revoke_decision_reason",
        "checked_before_revoke_at": "ALTER TABLE batch_auth_grant_users ADD COLUMN checked_before_revoke_at DATETIME NULL AFTER checked_before_grant_at",
    }
    for column, sql in user_migrations.items():
        if user_columns and column not in user_columns:
            await conn.execute(text(sql))

    index_result = await conn.execute(
        text(
            """
            SELECT INDEX_NAME
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'batch_auth_grant_users'
            """
        )
    )
    user_indexes = {str(row[0]) for row in index_result.fetchall()}
    if user_columns and "ix_batch_auth_grant_users_lease_id" not in user_indexes:
        await conn.execute(
            text(
                "CREATE INDEX ix_batch_auth_grant_users_lease_id "
                "ON batch_auth_grant_users (lease_id)"
            )
        )


async def check_mysql_connection() -> tuple[bool, str]:
    try:
        engine = get_async_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "MySQL 连接成功"
    except Exception as e:
        return False, str(e)
