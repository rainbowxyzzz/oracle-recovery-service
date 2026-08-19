from __future__ import annotations

import re
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import oracledb
import pymysql
from pymysql.cursors import DictCursor
from sqlalchemy import desc, select

from recovery_service.api.schemas.doris_sql_etl import (
    DorisSqlEtlColumnMapping,
    DorisSqlEtlRunStatus,
    DorisSqlEtlTaskStatus,
    DorisSqlDdlResponse,
    DorisSqlExecuteResponse,
    DorisSqlObjectItem,
    DorisSqlObjectListResponse,
    SqlColumn,
    SqlPreviewResponse,
)
from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import DatabaseConnectionProfile, DorisSqlEtlRun, DorisSqlEtlTaskDefinition
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.settings import get_settings

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")
_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE | re.DOTALL)


def preview_oracle_query(profile: DatabaseConnectionProfile, *, sql: str, limit: int = 100) -> SqlPreviewResponse:
    _ensure_oracle_profile(profile)
    clean_sql = _clean_select_sql(sql)
    limited_sql = f"SELECT * FROM ({clean_sql}) WHERE ROWNUM <= :__preview_limit"
    with _oracle_conn(profile) as db:
        cur = db.cursor()
        cur.execute(limited_sql, {"__preview_limit": int(limit)})
        columns = _cursor_columns(cur)
        rows = [_row_to_dict(columns, row) for row in cur.fetchall()]
    return SqlPreviewResponse(columns=[SqlColumn(name=item["name"], type=item.get("type")) for item in columns], rows=rows, row_count=len(rows), message=f"Oracle 查询预览完成，共返回 {len(rows)} 行。")


def execute_doris_sql(profile: DatabaseConnectionProfile, *, database: str | None, sql: str, limit: int = 200, confirm_dangerous: bool = False) -> DorisSqlExecuteResponse:
    _ensure_doris_profile(profile)
    clean_sql = _normalize_single_statement(sql)
    if not clean_sql:
        raise ValueError("请填写 Doris SQL。")
    sql_type = _sql_type(clean_sql)
    # Retained only for backward compatibility with historical API requests and task snapshots.
    _ = confirm_dangerous
    started = time.monotonic()
    with _doris_conn(profile, database) as db:
        with db.cursor() as cur:
            cur.execute(clean_sql)
            affected = cur.rowcount
            rows: list[dict[str, Any]] = []
            columns: list[SqlColumn] = []
            if cur.description:
                names = [item[0] for item in cur.description]
                columns = [SqlColumn(name=str(name), type=None) for name in names]
                for row in cur.fetchmany(int(limit)):
                    rows.append({str(key): _jsonable(value) for key, value in row.items()})
                affected = None
    duration_ms = int((time.monotonic() - started) * 1000)
    return DorisSqlExecuteResponse(sql_type=sql_type, columns=columns, rows=rows, row_count=len(rows), affected_rows=affected, duration_ms=duration_ms, message="Doris SQL 执行完成。")


def list_doris_catalogs(profile: DatabaseConnectionProfile) -> DorisSqlObjectListResponse:
    _ensure_doris_profile(profile)
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            cur.execute("SHOW CATALOGS")
            rows = cur.fetchall()
    items = []
    for row in rows:
        name = _first_value(row, preferred=("Catalog", "CatalogName", "Name"))
        if name:
            items.append(DorisSqlObjectItem(name=str(name), type="catalog", extra=_jsonable_row(row)))
    return DorisSqlObjectListResponse(items=items, message=f"已读取 {len(items)} 个 Catalog。")


def list_doris_databases(profile: DatabaseConnectionProfile, *, catalog: str | None = None) -> DorisSqlObjectListResponse:
    _ensure_doris_profile(profile)
    clean_catalog = _clean_optional_identifier(catalog, "Catalog")
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            _switch_catalog(cur, clean_catalog)
            cur.execute("SHOW DATABASES")
            rows = cur.fetchall()
    items = []
    for row in rows:
        name = _first_value(row, preferred=("Database", "DatabaseName", "Name"))
        if name:
            items.append(DorisSqlObjectItem(name=str(name), type="database", extra=_jsonable_row(row)))
    return DorisSqlObjectListResponse(items=items, message=f"已读取 {len(items)} 个数据库。")


def list_doris_tables(profile: DatabaseConnectionProfile, *, catalog: str | None, database: str) -> DorisSqlObjectListResponse:
    _ensure_doris_profile(profile)
    clean_catalog = _clean_optional_identifier(catalog, "Catalog")
    clean_database = _clean_identifier(database, "数据库")
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            _switch_catalog(cur, clean_catalog)
            cur.execute(f"USE {_q(clean_database)}")
            cur.execute("SHOW FULL TABLES")
            rows = cur.fetchall()
    items = []
    for row in rows:
        name = _first_value(row, preferred=("Table", "Tables_in_" + clean_database, "Name"))
        table_type = _first_value(row, preferred=("Table_type", "Table Type", "Type")) or "table"
        if name:
            items.append(DorisSqlObjectItem(name=str(name), type=str(table_type), extra=_jsonable_row(row)))
    return DorisSqlObjectListResponse(items=items, message=f"已读取 {len(items)} 张表。")


def list_doris_columns(profile: DatabaseConnectionProfile, *, catalog: str | None, database: str, table: str) -> DorisSqlObjectListResponse:
    _ensure_doris_profile(profile)
    clean_catalog = _clean_optional_identifier(catalog, "Catalog")
    clean_database = _clean_identifier(database, "数据库")
    clean_table = _clean_identifier(table, "表")
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            _switch_catalog(cur, clean_catalog)
            cur.execute(f"USE {_q(clean_database)}")
            cur.execute(f"DESC {_q(clean_table)}")
            rows = cur.fetchall()
    items = []
    for row in rows:
        name = _first_value(row, preferred=("Field", "Column", "Name"))
        column_type = _first_value(row, preferred=("Type", "DataType"))
        if name:
            items.append(DorisSqlObjectItem(name=str(name), type=str(column_type or ""), extra=_jsonable_row(row)))
    return DorisSqlObjectListResponse(items=items, message=f"已读取 {len(items)} 个字段。")


def get_doris_table_ddl(profile: DatabaseConnectionProfile, *, catalog: str | None, database: str, table: str) -> DorisSqlDdlResponse:
    _ensure_doris_profile(profile)
    clean_catalog = _clean_optional_identifier(catalog, "Catalog")
    clean_database = _clean_identifier(database, "数据库")
    clean_table = _clean_identifier(table, "表")
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            _switch_catalog(cur, clean_catalog)
            cur.execute(f"USE {_q(clean_database)}")
            cur.execute(f"SHOW CREATE TABLE {_q(clean_table)}")
            rows = cur.fetchall()
    ddl = ""
    if rows:
        row = rows[0]
        ddl = str(_first_value(row, preferred=("Create Table", "Create View", "Create Table Statement")) or list(row.values())[-1])
    return DorisSqlDdlResponse(ddl=ddl, message="DDL 读取完成。")


def preview_doris_table(profile: DatabaseConnectionProfile, *, catalog: str | None, database: str, table: str, limit: int = 100) -> DorisSqlExecuteResponse:
    _ensure_doris_profile(profile)
    clean_catalog = _clean_optional_identifier(catalog, "Catalog")
    clean_database = _clean_identifier(database, "数据库")
    clean_table = _clean_identifier(table, "表")
    clean_limit = max(1, min(1000, int(limit or 100)))
    started = time.monotonic()
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            _switch_catalog(cur, clean_catalog)
            cur.execute(f"USE {_q(clean_database)}")
            cur.execute(f"SELECT * FROM {_q(clean_table)} LIMIT {clean_limit}")
            columns = [SqlColumn(name=str(item[0]), type=None) for item in (cur.description or [])]
            rows = [{str(key): _jsonable(value) for key, value in row.items()} for row in cur.fetchall()]
    duration_ms = int((time.monotonic() - started) * 1000)
    return DorisSqlExecuteResponse(sql_type="SELECT", columns=columns, rows=rows, row_count=len(rows), affected_rows=None, duration_ms=duration_ms, message=f"已预览 {len(rows)} 行。")


def create_doris_sql_etl_task(
    *,
    name: str,
    description: str | None,
    source_profile: DatabaseConnectionProfile,
    target_profile: DatabaseConnectionProfile,
    source_sql: str,
    target_database: str,
    target_table: str,
    write_mode: str,
    batch_size: int,
    column_mapping: list[dict[str, Any]] | None,
    actor: AuthContext | None = None,
) -> DorisSqlEtlTaskStatus:
    _ensure_oracle_profile(source_profile)
    _ensure_doris_profile(target_profile)
    clean_sql = _clean_select_sql(source_sql)
    clean_target_database = _clean_identifier(target_database, "目标库")
    clean_target_table = _clean_identifier(target_table, "目标表")
    clean_mapping = _normalize_column_mapping(column_mapping)
    if not clean_mapping:
        preview = preview_oracle_query(source_profile, sql=clean_sql, limit=1)
        clean_mapping = [
            {"source_name": column.name, "target_name": column.name, "target_type": _default_target_type(index), "enabled": True}
            for index, column in enumerate(preview.columns)
        ]
    now = app_now()
    task = DorisSqlEtlTaskDefinition(
        id=uuid.uuid4(),
        name=name.strip()[:128],
        description=(description or "").strip() or None,
        source_connection_id=source_profile.id,
        source_connection_name=source_profile.name,
        target_connection_id=target_profile.id,
        target_connection_name=target_profile.name,
        source_sql=clean_sql,
        target_database=clean_target_database,
        target_table=clean_target_table,
        write_mode=_clean_write_mode(write_mode),
        batch_size=max(1, min(20000, int(batch_size or 1000))),
        column_mapping=clean_mapping,
        state="active",
        created_by_user_id=uuid.UUID(actor.user_id) if actor and actor.user_id else None,
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "api-key",
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        session.add(task)
        session.commit()
        session.refresh(task)
        return _task_to_status(task)
    finally:
        session.close()


def list_doris_sql_etl_tasks(*, limit: int = 100) -> list[DorisSqlEtlTaskStatus]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(select(DorisSqlEtlTaskDefinition).order_by(desc(DorisSqlEtlTaskDefinition.created_at)).limit(limit)).scalars().all()
        return [_task_to_status(row) for row in rows]
    finally:
        session.close()


def delete_doris_sql_etl_task(task_id: uuid.UUID) -> DorisSqlEtlTaskStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSqlEtlTaskDefinition, task_id)
        if not task:
            raise KeyError("Doris SQL/ETL 任务不存在。")
        status = _task_to_status(task)
        session.delete(task)
        session.commit()
        return status
    finally:
        session.close()


def submit_doris_sql_etl_run(task_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSqlEtlRunStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSqlEtlTaskDefinition, task_id)
        if not task:
            raise KeyError("Doris SQL/ETL 任务不存在。")
        run = DorisSqlEtlRun(
            id=uuid.uuid4(),
            task_id=task.id,
            task_name=task.name,
            state="queued",
            message="Doris SQL/ETL 任务已进入队列。",
            source_connection_id=task.source_connection_id,
            target_connection_id=task.target_connection_id,
            target_database=task.target_database,
            target_table=task.target_table,
            write_mode=task.write_mode,
            config_snapshot=_task_config_snapshot(task),
            created_by_user_id=uuid.UUID(actor.user_id) if actor and actor.user_id else None,
            created_by_username=actor.username if actor else None,
            created_by_auth_type=actor.auth_type if actor else "api-key",
            created_at=app_now(),
            updated_at=app_now(),
        )
        session.add(run)
        session.commit()
        celery_task_id = _enqueue_etl_worker(run.id)
        run.celery_task_id = celery_task_id
        session.commit()
        session.refresh(run)
        return _run_to_status(run)
    finally:
        session.close()


def list_doris_sql_etl_runs(*, limit: int = 100) -> list[DorisSqlEtlRunStatus]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(select(DorisSqlEtlRun).order_by(desc(DorisSqlEtlRun.created_at)).limit(limit)).scalars().all()
        return [_run_to_status(row) for row in rows]
    finally:
        session.close()


def get_doris_sql_etl_run(run_id: uuid.UUID) -> DorisSqlEtlRunStatus:
    session = get_sync_session_factory()()
    try:
        run = session.get(DorisSqlEtlRun, run_id)
        if not run:
            raise KeyError("Doris SQL/ETL 运行记录不存在。")
        return _run_to_status(run)
    finally:
        session.close()


def run_doris_sql_etl(run_id: uuid.UUID) -> dict[str, Any]:
    run_uuid = uuid.UUID(str(run_id))
    session = get_sync_session_factory()()
    try:
        run = session.get(DorisSqlEtlRun, run_uuid)
        if not run:
            return {"state": "failed", "message": "Run not found."}
        task = session.get(DorisSqlEtlTaskDefinition, run.task_id) if run.task_id else None
        if not task:
            run.state = "failed"
            run.message = "ETL 任务定义不存在。"
            run.error_message = run.message
            run.finished_at = app_now()
            session.commit()
            return {"state": run.state, "message": run.message}
        source = session.get(DatabaseConnectionProfile, task.source_connection_id)
        target = session.get(DatabaseConnectionProfile, task.target_connection_id)
        if not source or not target:
            run.state = "failed"
            run.message = "源连接或目标连接不存在。"
            run.error_message = run.message
            run.finished_at = app_now()
            session.commit()
            return {"state": run.state, "message": run.message}
        run.state = "running"
        run.started_at = app_now()
        run.updated_at = app_now()
        run.message = "ETL 正在执行。"
        run.logs = _append_log(run.logs, "INFO", "start", run.message)
        session.commit()
    finally:
        session.close()

    try:
        source_rows, target_rows, batch_count, logs = _execute_oracle_to_doris(source, target, task)
        session = get_sync_session_factory()()
        try:
            run = session.get(DorisSqlEtlRun, run_uuid)
            if run:
                run.state = "succeeded"
                run.message = f"ETL 执行完成：读取 {source_rows} 行，写入 {target_rows} 行。"
                run.source_rows = source_rows
                run.target_rows = target_rows
                run.batch_count = batch_count
                run.logs = list(run.logs or []) + logs + [_log("INFO", "finish", run.message)]
                run.finished_at = app_now()
                run.updated_at = app_now()
                session.commit()
        finally:
            session.close()
        return {"state": "succeeded", "message": "ETL succeeded."}
    except Exception as exc:
        session = get_sync_session_factory()()
        try:
            run = session.get(DorisSqlEtlRun, run_uuid)
            if run:
                run.state = "failed"
                run.message = f"ETL 执行失败：{exc}"
                run.error_message = str(exc)
                run.logs = _append_log(run.logs, "ERROR", "failed", str(exc))
                run.finished_at = app_now()
                run.updated_at = app_now()
                session.commit()
        finally:
            session.close()
        return {"state": "failed", "message": str(exc)}


def _execute_oracle_to_doris(source: DatabaseConnectionProfile, target: DatabaseConnectionProfile, task: DorisSqlEtlTaskDefinition) -> tuple[int, int, int, list[dict[str, Any]]]:
    mapping = [item for item in (task.column_mapping or []) if item.get("enabled", True)]
    if not mapping:
        raise ValueError("字段映射为空，无法写入 Doris。")
    source_cols = [item["source_name"] for item in mapping]
    target_cols = [item["target_name"] for item in mapping]
    logs = [_log("INFO", "prepare", f"写入策略：{task.write_mode}")]
    source_rows = 0
    target_rows = 0
    batch_count = 0
    with _oracle_conn(source) as oracle_db, _doris_conn(target, task.target_database) as doris_db:
        oracle_cur = oracle_db.cursor()
        oracle_cur.arraysize = task.batch_size or 1000
        oracle_cur.execute(_clean_select_sql(task.source_sql))
        oracle_columns = [str(item[0]) for item in (oracle_cur.description or [])]
        with doris_db.cursor() as doris_cur:
            _prepare_doris_target(doris_cur, task, mapping)
            insert_sql = _build_insert_sql(task.target_database, task.target_table, target_cols)
            while True:
                rows = oracle_cur.fetchmany(task.batch_size or 1000)
                if not rows:
                    break
                batch_values = []
                for row in rows:
                    row_dict = {oracle_columns[index]: _jsonable(row[index]) for index in range(min(len(oracle_columns), len(row)))}
                    batch_values.append([row_dict.get(col) for col in source_cols])
                if batch_values:
                    doris_cur.executemany(insert_sql, batch_values)
                    source_rows += len(batch_values)
                    target_rows += len(batch_values)
                    batch_count += 1
                    logs.append(_log("INFO", "batch", f"第 {batch_count} 批写入 {len(batch_values)} 行。", {"rows": len(batch_values)}))
    return source_rows, target_rows, batch_count, logs


def _prepare_doris_target(cur, task: DorisSqlEtlTaskDefinition, mapping: list[dict[str, Any]]) -> None:
    if task.write_mode == "drop_create_insert":
        cur.execute(f"DROP TABLE IF EXISTS {_q(task.target_database)}.{_q(task.target_table)}")
        _create_target_table(cur, task, mapping, if_not_exists=False)
        return
    if task.write_mode in {"truncate_insert", "create_if_not_exists_insert"}:
        _create_target_table(cur, task, mapping, if_not_exists=True)
    if task.write_mode == "truncate_insert":
        cur.execute(f"TRUNCATE TABLE {_q(task.target_database)}.{_q(task.target_table)}")


def _create_target_table(cur, task: DorisSqlEtlTaskDefinition, mapping: list[dict[str, Any]], *, if_not_exists: bool) -> None:
    columns = []
    for index, item in enumerate(mapping):
        target_name = _clean_identifier(item["target_name"], "目标字段")
        target_type = str(item.get("target_type") or _default_target_type(index)).upper()
        columns.append(f"{_q(target_name)} {target_type} NULL")
    first_col = _clean_identifier(mapping[0]["target_name"], "目标字段")
    ine = "IF NOT EXISTS " if if_not_exists else ""
    sql = (
        f"CREATE TABLE {ine}{_q(task.target_database)}.{_q(task.target_table)} ("
        + ", ".join(columns)
        + f") DUPLICATE KEY({_q(first_col)}) DISTRIBUTED BY HASH({_q(first_col)}) BUCKETS 10 PROPERTIES (\"replication_num\" = \"1\")"
    )
    cur.execute(sql)


def _build_insert_sql(database: str, table: str, columns: list[str]) -> str:
    targets = ", ".join(_q(_clean_identifier(item, "目标字段")) for item in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO {_q(database)}.{_q(table)} ({targets}) VALUES ({placeholders})"


def _enqueue_etl_worker(run_id: uuid.UUID) -> str | None:
    from recovery_service.workers.celery_app import celery_app

    settings = get_settings()
    result = celery_app.send_task("doris.sql_etl_run", args=[str(run_id)], queue=settings.celery_sql_queue)
    return result.id


def _oracle_conn(profile: DatabaseConnectionProfile):
    password = decrypt_secret(profile.password_enc, get_settings().credential_encryption_key)
    if profile.dsn:
        return oracledb.connect(user=profile.username, password=password, dsn=profile.dsn)
    service = profile.service_name or profile.database
    dsn = oracledb.makedsn(profile.host, profile.port or 1521, service_name=service)
    return oracledb.connect(user=profile.username, password=password, dsn=dsn)


def _doris_conn(profile: DatabaseConnectionProfile, database: str | None):
    return pymysql.connect(
        host=profile.host,
        port=profile.port or 9030,
        user=profile.username,
        password=decrypt_secret(profile.password_enc, get_settings().credential_encryption_key),
        database=database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _ensure_oracle_profile(profile: DatabaseConnectionProfile) -> None:
    if profile.engine != "oracle":
        raise ValueError("请选择 Oracle 类型的数据连接。")


def _ensure_doris_profile(profile: DatabaseConnectionProfile) -> None:
    if profile.engine != "doris":
        raise ValueError("请选择 Doris 类型的数据连接。")


def _clean_select_sql(sql: str) -> str:
    clean = _strip_sql(sql)
    if not _SELECT_RE.match(clean):
        raise ValueError("Oracle 查询 SQL 首版只允许 SELECT / WITH。")
    return clean


def _normalize_single_statement(sql: str) -> str:
    text = str(sql or "").strip()
    if not text:
        return ""
    in_single = False
    in_double = False
    in_backtick = False
    escape = False
    semicolon_at: int | None = None
    for index, char in enumerate(text):
        if escape:
            escape = False
            continue
        if char == "\\" and (in_single or in_double):
            escape = True
            continue
        if char == "'" and not in_double and not in_backtick:
            in_single = not in_single
            continue
        if char == '"' and not in_single and not in_backtick:
            in_double = not in_double
            continue
        if char == "`" and not in_single and not in_double:
            in_backtick = not in_backtick
            continue
        if char == ";" and not in_single and not in_double and not in_backtick:
            semicolon_at = index
            break
    if semicolon_at is None:
        return text
    tail = text[semicolon_at + 1 :].strip()
    if tail:
        raise ValueError("Doris SQL 工作台只允许执行单条 SQL，不能提交多语句。")
    return text[:semicolon_at].strip()


def _strip_sql(sql: str) -> str:
    return str(sql or "").strip().rstrip(";")


def _sql_type(sql: str) -> str:
    clean = _strip_leading_sql_comments(sql)
    match = re.match(r"^\s*([A-Za-z]+)", clean or "")
    return match.group(1).upper() if match else "UNKNOWN"


def _strip_leading_sql_comments(sql: str) -> str:
    clean = str(sql or "").lstrip()
    while True:
        if clean.startswith("--"):
            nl = clean.find("\n")
            clean = "" if nl < 0 else clean[nl + 1 :].lstrip()
            continue
        if clean.startswith("/*"):
            end = clean.find("*/")
            clean = "" if end < 0 else clean[end + 2 :].lstrip()
            continue
        return clean


def _cursor_columns(cur) -> list[dict[str, Any]]:
    result = []
    for item in cur.description or []:
        result.append({"name": str(item[0]), "type": str(item[1]) if len(item) > 1 else None})
    return result


def _row_to_dict(columns: list[dict[str, Any]], row) -> dict[str, Any]:
    return {column["name"]: _jsonable(row[index]) for index, column in enumerate(columns)}


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable(value) for key, value in row.items()}


def _first_value(row: dict[str, Any], *, preferred: tuple[str, ...]) -> Any:
    if not row:
        return None
    lowered = {str(key).lower(): key for key in row.keys()}
    for name in preferred:
        key = lowered.get(name.lower())
        if key is not None:
            return row.get(key)
    for value in row.values():
        if value is not None:
            return value
    return None


def _clean_identifier(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean or not _IDENT_RE.match(clean):
        raise ValueError(f"{label}只能包含中文、字母、数字和下划线。")
    return clean


def _clean_optional_identifier(value: str | None, label: str) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    return _clean_identifier(clean, label)


def _q(value: str) -> str:
    return f"`{str(value).replace('`', '``')}`"


def _switch_catalog(cur, catalog: str | None) -> None:
    if catalog:
        try:
            cur.execute(f"SWITCH {_q(catalog)}")
        except Exception:
            cur.execute(f"SWITCH {catalog}")


def _clean_write_mode(value: str) -> str:
    if value in {"append", "truncate_insert", "drop_create_insert", "create_if_not_exists_insert"}:
        return value
    return "truncate_insert"


def _normalize_column_mapping(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    result = []
    for index, item in enumerate(items or []):
        source_name = str(item.get("source_name") or "").strip()
        target_name = str(item.get("target_name") or source_name).strip()
        if not source_name or not target_name:
            continue
        result.append(
            {
                "source_name": source_name,
                "target_name": _clean_identifier(target_name, "目标字段"),
                "target_type": str(item.get("target_type") or _default_target_type(index)).strip() or _default_target_type(index),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return result


def _default_target_type(index: int) -> str:
    return "VARCHAR(500)" if index == 0 else "VARCHAR(2000)"


def _task_config_snapshot(task: DorisSqlEtlTaskDefinition) -> dict[str, Any]:
    return {
        "source_connection_id": str(task.source_connection_id),
        "target_connection_id": str(task.target_connection_id),
        "source_sql": task.source_sql,
        "target_database": task.target_database,
        "target_table": task.target_table,
        "write_mode": task.write_mode,
        "batch_size": task.batch_size,
        "column_mapping": task.column_mapping or [],
    }


def _task_to_status(task: DorisSqlEtlTaskDefinition) -> DorisSqlEtlTaskStatus:
    return DorisSqlEtlTaskStatus(
        task_id=task.id,
        name=task.name,
        description=task.description,
        source_connection_id=task.source_connection_id,
        source_connection_name=task.source_connection_name,
        target_connection_id=task.target_connection_id,
        target_connection_name=task.target_connection_name,
        target_database=task.target_database,
        target_table=task.target_table,
        write_mode=task.write_mode,  # type: ignore[arg-type]
        batch_size=task.batch_size,
        column_mapping=[DorisSqlEtlColumnMapping.model_validate(item) for item in (task.column_mapping or [])],
        state=task.state,
        created_by_username=task.created_by_username,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _run_to_status(run: DorisSqlEtlRun) -> DorisSqlEtlRunStatus:
    return DorisSqlEtlRunStatus(
        run_id=run.id,
        task_id=run.task_id,
        task_name=run.task_name,
        state=run.state,  # type: ignore[arg-type]
        message=run.message,
        target_database=run.target_database,
        target_table=run.target_table,
        write_mode=run.write_mode,
        source_rows=run.source_rows,
        target_rows=run.target_rows,
        batch_count=run.batch_count,
        logs=list(run.logs or []),
        error_message=run.error_message,
        created_by_username=run.created_by_username,
        created_at=run.created_at,
        started_at=run.started_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
    )


def _log(level: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"time": app_now().isoformat(), "level": level, "stage": stage, "message": message, "payload": payload or {}}


def _append_log(logs: list | None, level: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return list(logs or []) + [_log(level, stage, message, payload)]
