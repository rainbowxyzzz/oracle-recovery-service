from __future__ import annotations

import asyncio
import shlex
from typing import Any

import oracledb
import pymysql
from pymysql.cursors import DictCursor

from recovery_service.api.schemas.database_cleanup import (
    CleanupBatchExecutionResult,
    CleanupBatchPlan,
    CatalogObject,
    CleanupCatalog,
    CleanupConnection,
    CleanupExecutionResult,
    CleanupPlan,
    CleanupPlanStep,
    CleanupStatus,
)
from recovery_service.api.schemas.doris_csv_import import DorisFtpConnection
from recovery_service.core.domain import RemoteHost
from recovery_service.infrastructure.sqlserver.docker_executor import (
    SqlServerDockerExecutor,
    SqlServerDockerRuntime,
)
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command
from recovery_service.services.doris_csv_import import list_ftp_directory
from recovery_service.settings import get_settings

MYSQL_SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
SQLSERVER_SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}
DORIS_SYSTEM_DATABASES = {"information_schema", "__internal_schema", "mysql"}
ORACLE_SYSTEM_USERS = {
    "SYS",
    "SYSTEM",
    "DBSNMP",
    "OUTLN",
    "XDB",
    "WMSYS",
    "ORDSYS",
    "ORDDATA",
    "MDSYS",
    "CTXSYS",
    "OLAPSYS",
    "SYSMAN",
    "GSMADMIN_INTERNAL",
    "LBACSYS",
    "DVSYS",
    "DVF",
    "AUDSYS",
    "APPQOSSYS",
    "ORACLE_OCM",
}
ORACLE_SYSTEM_TABLESPACES = {"SYSTEM", "SYSAUX", "TEMP", "UNDOTBS1", "UNDO"}


def cleanup_defaults() -> dict[str, Any]:
    settings = get_settings()
    mysql_host = settings.mysql_restore_target_host or settings.mysql_restore_container_name
    mysql_port = settings.mysql_restore_host_port if settings.mysql_restore_target_host else 3306
    oracle_host = settings.oracle_target_host or settings.oracle_container_name
    oracle_port = settings.oracle_host_port if settings.oracle_target_host else 1521
    sqlserver_host = settings.sqlserver_target_host or settings.sqlserver_container_name
    sqlserver_port = settings.sqlserver_host_port if settings.sqlserver_target_host else 1433
    return {
        "mysql": {
            "engine": "mysql",
            "host": mysql_host,
            "port": mysql_port,
            "username": "root",
            "database": "",
        },
        "oracle": {
            "engine": "oracle",
            "host": oracle_host,
            "port": oracle_port,
            "username": "SYSTEM",
            "service_name": settings.oracle_pdb,
            "database": settings.oracle_pdb,
        },
        "sqlserver": {
            "engine": "sqlserver",
            "host": sqlserver_host,
            "port": sqlserver_port,
            "username": "SA",
            "database": "",
            "ssh_host": settings.sqlserver_docker_host,
            "ssh_port": settings.sqlserver_docker_ssh_port,
            "ssh_user": settings.sqlserver_docker_ssh_user,
            "container_name": settings.sqlserver_container_name,
        },
    }


async def test_connection(conn: CleanupConnection) -> CleanupStatus:
    if conn.engine == "mysql":
        return await asyncio.to_thread(_test_mysql, conn)
    if conn.engine == "oracle":
        return await asyncio.to_thread(_test_oracle, conn)
    if conn.engine == "doris":
        return await asyncio.to_thread(_test_doris, conn)
    if conn.engine == "ftp":
        return await asyncio.to_thread(_test_ftp, conn)
    return await asyncio.to_thread(_test_sqlserver, conn)


async def discover_catalog(conn: CleanupConnection) -> CleanupCatalog:
    if conn.engine == "mysql":
        return await asyncio.to_thread(_catalog_mysql, conn)
    if conn.engine == "oracle":
        return await asyncio.to_thread(_catalog_oracle, conn)
    if conn.engine == "doris":
        return await asyncio.to_thread(_catalog_doris, conn)
    if conn.engine == "ftp":
        return await asyncio.to_thread(_catalog_ftp, conn)
    return await asyncio.to_thread(_catalog_sqlserver, conn)


async def build_cleanup_plan(
    conn: CleanupConnection,
    target_name: str,
    *,
    drop_storage: bool = False,
    cleanup_files: bool = False,
) -> CleanupPlan:
    if conn.engine == "mysql":
        return await asyncio.to_thread(_plan_mysql, conn, target_name, cleanup_files)
    if conn.engine == "oracle":
        return await asyncio.to_thread(_plan_oracle, conn, target_name, drop_storage)
    if conn.engine == "doris":
        return await asyncio.to_thread(_plan_doris, conn, target_name, cleanup_files)
    if conn.engine == "ftp":
        return await asyncio.to_thread(_plan_ftp_blocked, conn, target_name)
    return await asyncio.to_thread(_plan_sqlserver, conn, target_name, cleanup_files)


async def build_cleanup_batch_plan(
    conn: CleanupConnection,
    target_names: list[str],
    *,
    drop_storage: bool = False,
    cleanup_files: bool = False,
) -> CleanupBatchPlan:
    targets = _normalize_batch_targets(target_names)
    plans: list[CleanupPlan] = []
    warnings: list[str] = []
    for target in targets:
        try:
            plans.append(
                await build_cleanup_plan(
                    conn,
                    target,
                    drop_storage=drop_storage,
                    cleanup_files=cleanup_files,
                )
            )
        except Exception as exc:
            warnings.append(f"{target}: {exc}")
    blocked_targets = [plan.target_name for plan in plans if not plan.can_execute]
    can_execute = bool(plans) and not blocked_targets and not warnings
    return CleanupBatchPlan(
        engine=conn.engine,
        target_names=[plan.target_name for plan in plans],
        plans=plans,
        can_execute=can_execute,
        warnings=warnings,
        blocked_targets=blocked_targets,
    )


async def execute_cleanup(
    conn: CleanupConnection,
    target_name: str,
    *,
    confirmation: str,
    drop_storage: bool = False,
    cleanup_files: bool = False,
) -> CleanupExecutionResult:
    if confirmation != target_name:
        plan = await build_cleanup_plan(
            conn, target_name, drop_storage=drop_storage, cleanup_files=cleanup_files
        )
        return CleanupExecutionResult(
            engine=conn.engine,
            target_name=target_name,
            state="blocked",
            plan=plan,
            error="confirmation does not match target name.",
        )
    if conn.engine == "mysql":
        return await asyncio.to_thread(_execute_mysql, conn, target_name, cleanup_files)
    if conn.engine == "oracle":
        return await asyncio.to_thread(_execute_oracle, conn, target_name, drop_storage)
    if conn.engine == "doris":
        return await asyncio.to_thread(_execute_doris, conn, target_name, cleanup_files)
    if conn.engine == "ftp":
        plan = await build_cleanup_plan(conn, target_name)
        return CleanupExecutionResult(
            engine="ftp",
            target_name=target_name,
            state="blocked",
            plan=plan,
            error="FTP connections cannot be cleaned in this module.",
        )
    return await asyncio.to_thread(_execute_sqlserver, conn, target_name, cleanup_files)


async def execute_cleanup_batch(
    conn: CleanupConnection,
    target_names: list[str],
    *,
    acknowledged: bool,
    drop_storage: bool = False,
    cleanup_files: bool = False,
) -> CleanupBatchExecutionResult:
    plan = await build_cleanup_batch_plan(
        conn,
        target_names,
        drop_storage=drop_storage,
        cleanup_files=cleanup_files,
    )
    if not acknowledged:
        return CleanupBatchExecutionResult(
            engine=conn.engine,
            target_names=plan.target_names,
            state="blocked",
            plan=plan,
            blocked_count=len(plan.target_names),
            error="Please confirm irreversible batch cleanup first.",
        )
    if not plan.can_execute:
        return CleanupBatchExecutionResult(
            engine=conn.engine,
            target_names=plan.target_names,
            state="blocked",
            plan=plan,
            blocked_count=len(plan.blocked_targets),
            failed_count=len(plan.warnings),
            error="Batch cleanup plan contains protected or invalid targets.",
        )

    results: list[CleanupExecutionResult] = []
    for target in plan.target_names:
        results.append(
            await execute_cleanup(
                conn,
                target,
                confirmation=target,
                drop_storage=drop_storage,
                cleanup_files=cleanup_files,
            )
        )

    success_count = sum(1 for item in results if item.state == "success")
    blocked_count = sum(1 for item in results if item.state == "blocked")
    failed_count = len(results) - success_count - blocked_count
    if success_count == len(results):
        state = "success"
    elif success_count > 0:
        state = "partial"
    else:
        state = "blocked" if blocked_count else "failed"
    return CleanupBatchExecutionResult(
        engine=conn.engine,
        target_names=plan.target_names,
        state=state,
        plan=plan,
        results=results,
        success_count=success_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
    )


def _normalize_batch_targets(target_names: list[str]) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    for raw in target_names:
        target = raw.strip()
        if not target:
            continue
        key = target.lower()
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)
    if not targets:
        raise ValueError("Please select at least one database/user to clean.")
    return targets


def _test_mysql(conn: CleanupConnection) -> CleanupStatus:
    with _mysql_conn(conn) as db:
        with db.cursor() as cur:
            cur.execute("SELECT VERSION() AS version")
            row = cur.fetchone() or {}
    return CleanupStatus(ok=True, message="MySQL connection succeeded.", details=row)


def _catalog_mysql(conn: CleanupConnection) -> CleanupCatalog:
    targets: list[CatalogObject] = []
    objects: list[CatalogObject] = []
    with _mysql_conn(conn) as db:
        with db.cursor() as cur:
            cur.execute("SHOW DATABASES")
            for row in cur.fetchall():
                name = row.get("Database") or next(iter(row.values()))
                targets.append(
                    CatalogObject(
                        name=name,
                        type="system_database" if name in MYSQL_SYSTEM_DATABASES else "database",
                    )
                )
            cur.execute(
                """
                SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS,
                       DATA_LENGTH + INDEX_LENGTH AS bytes
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys')
                ORDER BY TABLE_SCHEMA, TABLE_NAME
                """
            )
            for row in cur.fetchall():
                objects.append(
                    CatalogObject(
                        name=row["TABLE_NAME"],
                        type=row["TABLE_TYPE"].lower().replace(" ", "_"),
                        parent=row["TABLE_SCHEMA"],
                        details={
                            "engine": row.get("ENGINE"),
                            "rows": row.get("TABLE_ROWS"),
                            "bytes": row.get("bytes"),
                        },
                    )
                )
            for table, obj_type, name_col in [
                ("ROUTINES", "routine", "ROUTINE_NAME"),
                ("TRIGGERS", "trigger", "TRIGGER_NAME"),
                ("EVENTS", "event", "EVENT_NAME"),
            ]:
                cur.execute(
                    f"""
                    SELECT EVENT_OBJECT_SCHEMA AS parent, {name_col} AS name
                    FROM information_schema.{table}
                    WHERE EVENT_OBJECT_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys')
                    """
                    if table == "TRIGGERS"
                    else f"""
                    SELECT ROUTINE_SCHEMA AS parent, {name_col} AS name
                    FROM information_schema.{table}
                    WHERE ROUTINE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys')
                    """
                    if table == "ROUTINES"
                    else f"""
                    SELECT EVENT_SCHEMA AS parent, {name_col} AS name
                    FROM information_schema.{table}
                    WHERE EVENT_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys')
                    """
                )
                for row in cur.fetchall():
                    objects.append(CatalogObject(name=row["name"], type=obj_type, parent=row["parent"]))
    return CleanupCatalog(
        engine="mysql",
        targets=targets,
        objects=objects,
        protected_targets=sorted(MYSQL_SYSTEM_DATABASES),
    )


def _plan_mysql(
    conn: CleanupConnection,
    target_name: str,
    cleanup_files: bool = False,
) -> CleanupPlan:
    target = target_name.strip()
    protected = target.lower() in MYSQL_SYSTEM_DATABASES
    warnings = []
    objects = _mysql_object_counts(conn, target) if not protected else {}
    if protected:
        warnings.append("System database is protected and cannot be dropped.")
    if cleanup_files:
        warnings.append("MySQL physical files are not removed manually by this module.")
    steps = [
        CleanupPlanStep(layer="L0 session", action="kill_sessions", target=target),
        CleanupPlanStep(
            layer="L1 object",
            action="release_schema_objects",
            target=target,
            sql=f"DROP DATABASE {_mysql_ident(target)}",
            notes=[f"{kind}: {count}" for kind, count in objects.items()],
        ),
        CleanupPlanStep(
            layer="L2 container",
            action="drop_database",
            target=target,
            sql=f"DROP DATABASE IF EXISTS {_mysql_ident(target)}",
            danger="high",
        ),
        CleanupPlanStep(
            layer="L3 storage",
            action="verify_mysql_datadir_cleanup",
            target=target,
            required=False,
            notes=["InnoDB storage is managed by MySQL; manual file removal is not performed."],
        ),
    ]
    return CleanupPlan(
        engine="mysql",
        target_name=target,
        protected=protected,
        can_execute=not protected,
        warnings=warnings,
        steps=steps,
        confirmation=target,
    )


def _execute_mysql(
    conn: CleanupConnection,
    target_name: str,
    cleanup_files: bool = False,
) -> CleanupExecutionResult:
    plan = _plan_mysql(conn, target_name, cleanup_files)
    if not plan.can_execute:
        return CleanupExecutionResult(
            engine="mysql", target_name=target_name, state="blocked", plan=plan, error="Target is protected and cannot be dropped."
        )
    executed: list[CleanupPlanStep] = []
    try:
        with _mysql_conn(conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT ID FROM information_schema.PROCESSLIST
                    WHERE DB = %s AND ID <> CONNECTION_ID()
                    """,
                    (target_name,),
                )
                for row in cur.fetchall():
                    cur.execute(f"KILL {int(row['ID'])}")
                executed.append(plan.steps[0])
                cur.execute(f"DROP DATABASE IF EXISTS {_mysql_ident(target_name)}")
                executed.extend(plan.steps[1:3])
                cur.execute(
                    "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
                    (target_name,),
                )
                exists = cur.fetchone() is not None
        verification = {"database_exists": exists, "storage_cleanup": "mysql_owned"}
        return CleanupExecutionResult(
            engine="mysql",
            target_name=target_name,
            state="failed" if exists else "success",
            plan=plan,
            executed_steps=executed,
            verification=verification,
            error="DROP DATABASE completed but database still exists." if exists else None,
        )
    except Exception as e:
        return CleanupExecutionResult(
            engine="mysql",
            target_name=target_name,
            state="failed",
            plan=plan,
            executed_steps=executed,
            error=str(e),
        )


def _test_oracle(conn: CleanupConnection) -> CleanupStatus:
    with _oracle_conn(conn) as db:
        cur = db.cursor()
        cur.execute("SELECT banner FROM v$version WHERE rownum = 1")
        row = cur.fetchone()
    details: dict[str, Any] = {
        "version": row[0] if row else "",
        "database": {"ok": True},
    }
    if _oracle_server_check_requested(conn):
        details.update(_test_oracle_server_access(conn))
        message = "Oracle database and server checks succeeded."
    else:
        details["server"] = {"skipped": True, "reason": "Oracle server SSH fields are empty."}
        message = "Oracle database connection succeeded. Oracle server SSH check was skipped."
    return CleanupStatus(ok=True, message=message, details=details)


def _oracle_server_check_requested(conn: CleanupConnection) -> bool:
    return bool(conn.ssh_host or conn.ssh_user or conn.ssh_password or conn.container_name)


def _test_oracle_server_access(conn: CleanupConnection) -> dict[str, Any]:
    settings = get_settings()
    ssh_host = conn.ssh_host or settings.oracle_docker_host
    ssh_port = conn.ssh_port or settings.oracle_docker_ssh_port
    ssh_user = conn.ssh_user or settings.oracle_docker_ssh_user
    ssh_password = (
        conn.ssh_password.get_secret_value()
        if conn.ssh_password
        else settings.oracle_docker_ssh_password
    )
    if not ssh_host:
        raise RuntimeError("Oracle server SSH host is required for server validation.")
    if not ssh_user:
        raise RuntimeError("Oracle server SSH user is required for server validation.")
    if not ssh_password:
        raise RuntimeError("Oracle server SSH password is required for server validation.")

    remote = RemoteHost(
        host=ssh_host,
        port=ssh_port,
        username=ssh_user,
        password=ssh_password,
    )
    ssh_result = run_ssh_command(remote, "printf oracle-ssh-ok", timeout=30)
    if ssh_result.returncode != 0:
        raise RuntimeError(ssh_result.stderr or ssh_result.stdout or "Oracle server SSH validation failed.")

    details: dict[str, Any] = {
        "server": {
            "ok": True,
            "host": ssh_host,
            "port": ssh_port,
            "user": ssh_user,
        }
    }
    container = conn.container_name or settings.oracle_container_name
    if container:
        inspect_cmd = (
            "docker inspect --format "
            + shlex.quote("{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.Name}}")
            + " "
            + shlex.quote(container)
        )
        inspect_result = run_ssh_command(remote, inspect_cmd, timeout=30)
        if inspect_result.returncode != 0:
            raise RuntimeError(
                f"Oracle container validation failed for {container}: "
                + (inspect_result.stderr or inspect_result.stdout or "docker inspect failed")
            )
        inspect_text = inspect_result.stdout.strip()
        inspect_parts = inspect_text.split()
        running = bool(inspect_parts) and inspect_parts[0].lower() == "true"
        if not running:
            raise RuntimeError(f"Oracle container validation failed for {container}: container is not running ({inspect_text})")
        details["container"] = {
            "ok": True,
            "name": container,
            "running": running,
            "health": inspect_parts[1] if len(inspect_parts) > 2 else "",
            "inspect": inspect_text,
        }
    else:
        details["container"] = {"skipped": True, "reason": "Oracle container name is empty."}
    return details


def _catalog_oracle(conn: CleanupConnection) -> CleanupCatalog:
    targets: list[CatalogObject] = []
    objects: list[CatalogObject] = []
    protected_names: set[str] = set(ORACLE_SYSTEM_USERS)
    with _oracle_conn(conn) as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT username, default_tablespace, oracle_maintained
            FROM dba_users
            ORDER BY username
            """
        )
        for username, default_ts, oracle_maintained in cur.fetchall():
            is_system = username in ORACLE_SYSTEM_USERS or oracle_maintained == "Y"
            if is_system:
                protected_names.add(username)
            targets.append(
                CatalogObject(
                    name=username,
                    type="system_user" if is_system else "schema",
                    details={
                        "default_tablespace": default_ts,
                        "oracle_maintained": oracle_maintained,
                    },
                )
            )
        cur.execute(
            """
            SELECT owner, object_type, COUNT(*)
            FROM dba_objects
            WHERE owner NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS','ORDSYS','ORDDATA',
              'MDSYS','CTXSYS','OLAPSYS','SYSMAN','GSMADMIN_INTERNAL','LBACSYS',
              'DVSYS','DVF','AUDSYS','APPQOSSYS','ORACLE_OCM'
            )
            GROUP BY owner, object_type
            ORDER BY owner, object_type
            """
        )
        for owner, obj_type, count in cur.fetchall():
            objects.append(
                CatalogObject(
                    name=obj_type,
                    type="object_count",
                    parent=owner,
                    details={"count": count},
                )
            )
    return CleanupCatalog(
        engine="oracle",
        targets=targets,
        objects=objects,
        protected_targets=sorted(protected_names),
    )


def _plan_oracle(
    conn: CleanupConnection,
    target_name: str,
    drop_storage: bool = False,
) -> CleanupPlan:
    owner = target_name.strip().upper()
    protected = owner in ORACLE_SYSTEM_USERS or _oracle_is_maintained_user(conn, owner)
    warnings = []
    storage: list[dict[str, Any]] = []
    object_counts: dict[str, int] = {}
    exclusive_tablespaces: list[str] = []
    shared_tablespaces: list[str] = []
    if protected:
        warnings.append("Oracle system user is protected and cannot be dropped.")
    else:
        with _oracle_conn(conn) as db:
            cur = db.cursor()
            object_counts = _oracle_object_counts(cur, owner)
            tablespaces = _oracle_user_tablespaces(cur, owner)
            for ts in tablespaces:
                files = _oracle_datafiles(cur, ts)
                shared_with = _oracle_tablespace_shared_with(cur, ts, owner)
                item = {"tablespace": ts, "datafiles": files, "shared_with": shared_with}
                storage.append(item)
                if ts in ORACLE_SYSTEM_TABLESPACES:
                    shared_tablespaces.append(ts)
                elif shared_with:
                    shared_tablespaces.append(ts)
                else:
                    exclusive_tablespaces.append(ts)
            if shared_tablespaces:
                warnings.append(
                    "Some tablespaces are shared or protected: "
                    + ", ".join(sorted(set(shared_tablespaces)))
                )
            if not drop_storage and exclusive_tablespaces:
                warnings.append(
                    "Oracle storage cleanup is not selected; exclusive tablespaces and datafiles will be kept."
                )
    steps = [
        CleanupPlanStep(layer="L0 session", action="kill_sessions", target=owner),
        CleanupPlanStep(
            layer="L1 object",
            action="drop_user_cascade",
            target=owner,
            sql=f"DROP USER {_oracle_ident(owner)} CASCADE",
            danger="high",
            notes=[f"{kind}: {count}" for kind, count in sorted(object_counts.items())],
        ),
        CleanupPlanStep(
            layer="L1 object",
            action="purge_recyclebin",
            target=owner,
            sql="PURGE DBA_RECYCLEBIN",
            required=False,
        ),
        CleanupPlanStep(
            layer="L2 container",
            action="verify_user_removed",
            target=owner,
        ),
    ]
    for ts in exclusive_tablespaces:
        steps.append(
            CleanupPlanStep(
                layer="L3 storage",
                action="drop_tablespace_with_datafiles" if drop_storage else "skip_tablespace_drop",
                target=ts,
                sql=(
                    f"DROP TABLESPACE {_oracle_ident(ts)} INCLUDING CONTENTS AND DATAFILES CASCADE CONSTRAINTS"
                    if drop_storage
                    else None
                ),
                required=drop_storage,
                danger="critical" if drop_storage else "normal",
                notes=["Drop exclusive schema tablespaces and datafiles."],
            )
        )
    for ts in sorted(set(shared_tablespaces)):
        steps.append(
            CleanupPlanStep(
                layer="L3 storage",
                action="skip_shared_or_protected_tablespace",
                target=ts,
                required=False,
                notes=["Shared or protected tablespace is skipped."],
            )
        )
    return CleanupPlan(
        engine="oracle",
        target_name=owner,
        protected=protected,
        can_execute=not protected,
        warnings=warnings,
        storage=storage,
        steps=steps,
        confirmation=owner,
    )


def _execute_oracle(
    conn: CleanupConnection,
    target_name: str,
    drop_storage: bool = False,
) -> CleanupExecutionResult:
    owner = target_name.strip().upper()
    plan = _plan_oracle(conn, owner, drop_storage)
    if not plan.can_execute:
        return CleanupExecutionResult(
            engine="oracle", target_name=owner, state="blocked", plan=plan, error="Target is protected and cannot be dropped."
        )
    executed: list[CleanupPlanStep] = []
    try:
        with _oracle_conn(conn) as db:
            db.autocommit = True
            cur = db.cursor()
            cur.execute("SELECT sid, serial# FROM v$session WHERE username = :u", {"u": owner})
            sessions = list(cur.fetchall())
            for sid, serial in sessions:
                try:
                    cur.execute(f"ALTER SYSTEM KILL SESSION '{int(sid)},{int(serial)}' IMMEDIATE")
                except Exception:
                    pass
            executed.append(plan.steps[0])
            cur.execute(f"DROP USER {_oracle_ident(owner)} CASCADE")
            executed.append(plan.steps[1])
            try:
                cur.execute("PURGE DBA_RECYCLEBIN")
                executed.append(plan.steps[2])
            except Exception:
                pass
            for step in plan.steps:
                if step.layer == "L3 storage" and step.action == "drop_tablespace_with_datafiles" and step.sql:
                    cur.execute(step.sql)
                    executed.append(step)
            cur.execute("SELECT COUNT(*) FROM dba_users WHERE username = :u", {"u": owner})
            user_exists = bool(cur.fetchone()[0])
            remaining_ts = []
            for item in plan.storage:
                ts = item["tablespace"]
                cur.execute(
                    "SELECT COUNT(*) FROM dba_tablespaces WHERE tablespace_name = :t",
                    {"t": ts},
                )
                if cur.fetchone()[0]:
                    remaining_ts.append(ts)
        verification = {"user_exists": user_exists, "remaining_tablespaces": remaining_ts}
        failed = user_exists or any(
            ts not in [s.target for s in plan.steps if s.action == "skip_shared_or_protected_tablespace"]
            for ts in remaining_ts
            if drop_storage
        )
        return CleanupExecutionResult(
            engine="oracle",
            target_name=owner,
            state="failed" if failed else "success",
            plan=plan,
            executed_steps=executed,
            verification=verification,
            error="Oracle storage cleanup failed." if failed else None,
        )
    except Exception as e:
        return CleanupExecutionResult(
            engine="oracle",
            target_name=owner,
            state="failed",
            plan=plan,
            executed_steps=executed,
            error=str(e),
        )


def _test_sqlserver(conn: CleanupConnection) -> CleanupStatus:
    out = _sqlserver_run(conn, "SELECT @@VERSION")
    return CleanupStatus(ok=True, message="SQL Server connection succeeded.", details={"version": out.strip()})


def _catalog_sqlserver(conn: CleanupConnection) -> CleanupCatalog:
    targets: list[CatalogObject] = []
    objects: list[CatalogObject] = []
    rows = _sqlserver_rows(
        conn,
        "SELECT name, database_id FROM sys.databases ORDER BY database_id",
    )
    for row in rows:
        name = row[0]
        targets.append(
            CatalogObject(
                name=name,
                type="system_database" if name.lower() in SQLSERVER_SYSTEM_DATABASES else "database",
                details={"database_id": row[1] if len(row) > 1 else None},
            )
        )
    for target in targets:
        if target.name.lower() in SQLSERVER_SYSTEM_DATABASES:
            continue
        db = _sqlserver_ident(target.name)
        count_rows = _sqlserver_rows(
            conn,
            f"""
            USE {db};
            SELECT 'table', COUNT(*) FROM sys.tables
            UNION ALL SELECT 'view', COUNT(*) FROM sys.views
            UNION ALL SELECT 'procedure', COUNT(*) FROM sys.procedures
            UNION ALL SELECT 'function', COUNT(*) FROM sys.objects WHERE type IN ('FN','IF','TF')
            """,
        )
        for kind, count in count_rows:
            objects.append(
                CatalogObject(
                    name=str(kind),
                    type="object_count",
                    parent=target.name,
                    details={"count": _safe_int(count)},
                )
            )
    return CleanupCatalog(
        engine="sqlserver",
        targets=targets,
        objects=objects,
        protected_targets=sorted(SQLSERVER_SYSTEM_DATABASES),
    )


def _test_doris(conn: CleanupConnection) -> CleanupStatus:
    with _doris_conn(conn) as db:
        with db.cursor() as cur:
            cur.execute("SELECT VERSION() AS version")
            row = cur.fetchone() or {}
    return CleanupStatus(ok=True, message="Doris connection succeeded.", details=row)


def _catalog_doris(conn: CleanupConnection) -> CleanupCatalog:
    targets: list[CatalogObject] = []
    objects: list[CatalogObject] = []
    with _doris_conn(conn) as db:
        with db.cursor() as cur:
            cur.execute("SHOW DATABASES")
            for row in cur.fetchall():
                name = row.get("Database") or next(iter(row.values()))
                targets.append(
                    CatalogObject(
                        name=name,
                        type="system_database" if str(name).lower() in DORIS_SYSTEM_DATABASES else "database",
                    )
                )
            for target in targets:
                if target.name.lower() in DORIS_SYSTEM_DATABASES:
                    continue
                try:
                    cur.execute(f"SHOW TABLES FROM {_mysql_ident(target.name)}")
                    for row in cur.fetchall():
                        objects.append(
                            CatalogObject(
                                name=next(iter(row.values())),
                                type="table",
                                parent=target.name,
                            )
                        )
                except Exception as exc:
                    objects.append(
                        CatalogObject(
                            name="tables",
                            type="object_count",
                            parent=target.name,
                            details={"count": 0, "warning": str(exc)},
                        )
                    )
    return CleanupCatalog(
        engine="doris",
        targets=targets,
        objects=objects,
        protected_targets=sorted(DORIS_SYSTEM_DATABASES),
    )


def _plan_doris(
    conn: CleanupConnection,
    target_name: str,
    cleanup_files: bool = False,
) -> CleanupPlan:
    target = target_name.strip()
    protected = target.lower() in DORIS_SYSTEM_DATABASES
    warnings = []
    object_counts = _doris_object_counts(conn, target) if not protected else {}
    if protected:
        warnings.append("Doris 系统库受保护，禁止删除。")
    if cleanup_files:
        warnings.append("Doris 存储由集群托管，本模块不会手工删除 BE 物理文件。")
    steps = [
        CleanupPlanStep(
            layer="L1 object",
            action="release_database_objects",
            target=target,
            notes=[f"{kind}: {count}" for kind, count in object_counts.items()]
            or ["DROP DATABASE 会由 Doris 释放库内表、视图等对象。"],
        ),
        CleanupPlanStep(
            layer="L2 container",
            action="drop_database",
            target=target,
            sql=f"DROP DATABASE IF EXISTS {_mysql_ident(target)}",
            danger="critical",
        ),
        CleanupPlanStep(
            layer="L3 storage",
            action="verify_doris_storage_cleanup",
            target=target,
            required=False,
            notes=["Doris 的 Tablet/Replica 文件由 BE 集群生命周期管理，不做 Oracle datafile 式手工物理清理。"],
        ),
    ]
    return CleanupPlan(
        engine="doris",
        target_name=target,
        protected=protected,
        can_execute=not protected,
        warnings=warnings,
        steps=steps,
        confirmation=target,
    )


def _execute_doris(
    conn: CleanupConnection,
    target_name: str,
    cleanup_files: bool = False,
) -> CleanupExecutionResult:
    plan = _plan_doris(conn, target_name, cleanup_files)
    if not plan.can_execute:
        return CleanupExecutionResult(
            engine="doris",
            target_name=target_name,
            state="blocked",
            plan=plan,
            error="目标受保护，禁止删除。",
        )
    executed: list[CleanupPlanStep] = []
    try:
        target = plan.target_name
        with _doris_conn(conn) as db:
            with db.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {_mysql_ident(target)}")
                executed.extend(plan.steps[:2])
                cur.execute("SHOW DATABASES")
                exists = False
                for row in cur.fetchall():
                    name = row.get("Database") or next(iter(row.values()))
                    if str(name).lower() == target.lower():
                        exists = True
                        break
                executed.append(plan.steps[2])
        verification = {"database_exists": exists, "storage_cleanup": "doris_owned"}
        return CleanupExecutionResult(
            engine="doris",
            target_name=target_name,
            state="failed" if exists else "success",
            plan=plan,
            executed_steps=executed,
            verification=verification,
            error="执行 DROP DATABASE 后库仍然存在。" if exists else None,
        )
    except Exception as e:
        return CleanupExecutionResult(
            engine="doris",
            target_name=target_name,
            state="failed",
            plan=plan,
            executed_steps=executed,
            error=str(e),
        )


def _plan_doris_blocked(conn: CleanupConnection, target_name: str) -> CleanupPlan:
    target = target_name.strip()
    return CleanupPlan(
        engine="doris",
        target_name=target,
        protected=True,
        can_execute=False,
        warnings=["Doris cleanup is now handled by the active Doris cleanup plan."],
        steps=[
            CleanupPlanStep(
                layer="L2 container",
                action="drop_database",
                target=target,
                required=False,
                danger="critical",
                notes=["Legacy blocked Doris plan; not used by the active path."],
            )
        ],
        confirmation=target,
    )


def _ftp_schema(conn: CleanupConnection) -> DorisFtpConnection:
    return DorisFtpConnection(
        host=conn.host,
        port=conn.port or 21,
        username=conn.username,
        password=_connection_password(conn),
        directory=conn.database or conn.dsn or "/",
    )


def _test_ftp(conn: CleanupConnection) -> CleanupStatus:
    catalog = list_ftp_directory(_ftp_schema(conn))
    return CleanupStatus(
        ok=True,
        message="FTP connection succeeded.",
        details={"directory": catalog.directory, "items": len(catalog.items)},
    )


def _catalog_ftp(conn: CleanupConnection) -> CleanupCatalog:
    catalog = list_ftp_directory(_ftp_schema(conn))
    targets = [
        CatalogObject(
            name=item.name,
            type=item.type,
            parent=catalog.directory,
            details={"path": item.path, "size": item.size, "modified": item.modified},
        )
        for item in catalog.items
    ]
    return CleanupCatalog(engine="ftp", targets=targets, objects=[], protected_targets=[])


def _plan_ftp_blocked(conn: CleanupConnection, target_name: str) -> CleanupPlan:
    target = target_name.strip()
    return CleanupPlan(
        engine="ftp",
        target_name=target,
        protected=True,
        can_execute=False,
        warnings=["FTP connections are file sources and cannot be deleted by database cleanup."],
        steps=[
            CleanupPlanStep(
                layer="L2 container",
                action="drop_database",
                target=target,
                required=False,
                danger="critical",
                notes=["FTP directories and files are not database cleanup targets."],
            )
        ],
        confirmation=target,
    )


def _plan_sqlserver(
    conn: CleanupConnection,
    target_name: str,
    cleanup_files: bool = False,
) -> CleanupPlan:
    target = target_name.strip()
    protected = target.lower() in SQLSERVER_SYSTEM_DATABASES
    warnings = []
    files: list[dict[str, Any]] = []
    if protected:
        warnings.append("SQL Server system database is protected and cannot be dropped.")
    else:
        for row in _sqlserver_rows(
            conn,
            f"""
            SELECT name, physical_name, type_desc
            FROM sys.master_files
            WHERE database_id = DB_ID(N{_sqlserver_literal(target)})
            ORDER BY type_desc, name
            """,
        ):
            files.append({"name": row[0], "path": row[1], "type": row[2] if len(row) > 2 else ""})
        if cleanup_files:
            warnings.append("Only confirmed residual MDF/LDF files under configured SQL Server paths will be removed.")
    steps = [
        CleanupPlanStep(
            layer="L0 session",
            action="single_user_rollback",
            target=target,
            sql=f"ALTER DATABASE {_sqlserver_ident(target)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE",
            danger="high",
        ),
        CleanupPlanStep(
            layer="L1 object",
            action="release_database_objects",
            target=target,
            notes=["SQL Server releases database objects through DROP DATABASE."],
        ),
        CleanupPlanStep(
            layer="L2 container",
            action="drop_database",
            target=target,
            sql=f"DROP DATABASE {_sqlserver_ident(target)}",
            danger="critical",
        ),
        CleanupPlanStep(
            layer="L3 storage",
            action="verify_database_files",
            target=target,
            required=False,
            notes=[f"{f['type']}: {f['path']}" for f in files],
        ),
    ]
    if cleanup_files:
        steps.append(
            CleanupPlanStep(
                layer="L3 storage",
                action="remove_residual_files",
                target=target,
                required=False,
                danger="critical",
                notes=["Only confirmed residual physical files under configured paths will be removed."],
            )
        )
    return CleanupPlan(
        engine="sqlserver",
        target_name=target,
        protected=protected,
        can_execute=not protected,
        warnings=warnings,
        storage=files,
        steps=steps,
        confirmation=target,
    )


def _execute_sqlserver(
    conn: CleanupConnection,
    target_name: str,
    cleanup_files: bool = False,
) -> CleanupExecutionResult:
    plan = _plan_sqlserver(conn, target_name, cleanup_files)
    if not plan.can_execute:
        return CleanupExecutionResult(
            engine="sqlserver",
            target_name=target_name,
            state="blocked",
            plan=plan,
            error="Target is protected and cannot be dropped.",
        )
    executed: list[CleanupPlanStep] = []
    try:
        target = _sqlserver_ident(target_name)
        sql = (
            f"ALTER DATABASE {target} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
            f"DROP DATABASE {target};"
        )
        _sqlserver_run(conn, sql, timeout=600)
        executed.extend(plan.steps[:3])
        rows = _sqlserver_rows(
            conn,
            f"SELECT name FROM sys.databases WHERE name = N{_sqlserver_literal(target_name)}",
        )
        exists = bool(rows)
        removed_files: list[str] = []
        if cleanup_files and not exists:
            removed_files = _cleanup_sqlserver_files(conn, plan.storage)
            executed.extend([s for s in plan.steps if s.action == "remove_residual_files"])
        verification = {
            "database_exists": exists,
            "tracked_files": plan.storage,
            "removed_residual_files": removed_files,
        }
        return CleanupExecutionResult(
            engine="sqlserver",
            target_name=target_name,
            state="failed" if exists else "success",
            plan=plan,
            executed_steps=executed,
            verification=verification,
            error="DROP DATABASE completed but database still exists." if exists else None,
        )
    except Exception as e:
        return CleanupExecutionResult(
            engine="sqlserver",
            target_name=target_name,
            state="failed",
            plan=plan,
            executed_steps=executed,
            error=str(e),
        )


def _mysql_conn(conn: CleanupConnection):
    return pymysql.connect(
        host=conn.host,
        port=conn.port or 3306,
        user=conn.username,
        password=_connection_password(conn),
        database=conn.database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _doris_conn(conn: CleanupConnection):
    return pymysql.connect(
        host=conn.host,
        port=conn.port or 9030,
        user=conn.username,
        password=_connection_password(conn),
        database=conn.database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _mysql_object_counts(conn: CleanupConnection, target: str) -> dict[str, int]:
    with _mysql_conn(conn) as db:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT 'table' AS kind, COUNT(*) AS count
                FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                UNION ALL SELECT 'view', COUNT(*)
                FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'VIEW'
                UNION ALL SELECT 'routine', COUNT(*)
                FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s
                UNION ALL SELECT 'trigger', COUNT(*)
                FROM information_schema.TRIGGERS WHERE EVENT_OBJECT_SCHEMA = %s
                UNION ALL SELECT 'event', COUNT(*)
                FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s
                """,
                (target, target, target, target, target),
            )
            return {row["kind"]: int(row["count"]) for row in cur.fetchall()}


def _doris_object_counts(conn: CleanupConnection, target: str) -> dict[str, int]:
    with _doris_conn(conn) as db:
        with db.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT TABLE_TYPE AS kind, COUNT(*) AS count
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                    GROUP BY TABLE_TYPE
                    """,
                    (target,),
                )
                return {
                    str(row["kind"]).lower().replace(" ", "_"): int(row["count"])
                    for row in cur.fetchall()
                }
            except Exception:
                cur.execute(f"SHOW TABLES FROM {_mysql_ident(target)}")
                return {"table": len(cur.fetchall())}


def _oracle_conn(conn: CleanupConnection):
    dsn = conn.dsn or f"{conn.host}:{conn.port or 1521}/{conn.service_name or conn.database or ''}"
    return oracledb.connect(
        user=conn.username,
        password=_connection_password(conn),
        dsn=dsn,
    )


def _oracle_object_counts(cur, owner: str) -> dict[str, int]:
    cur.execute(
        """
        SELECT object_type, COUNT(*)
        FROM dba_objects
        WHERE owner = :owner
        GROUP BY object_type
        """,
        {"owner": owner},
    )
    return {kind: int(count) for kind, count in cur.fetchall()}


def _oracle_user_tablespaces(cur, owner: str) -> list[str]:
    names: set[str] = set()
    cur.execute("SELECT default_tablespace FROM dba_users WHERE username = :owner", {"owner": owner})
    row = cur.fetchone()
    if row and row[0]:
        names.add(row[0])
    cur.execute(
        "SELECT DISTINCT tablespace_name FROM dba_segments WHERE owner = :owner AND tablespace_name IS NOT NULL",
        {"owner": owner},
    )
    names.update(row[0] for row in cur.fetchall() if row and row[0])
    return sorted(names)


def _oracle_datafiles(cur, tablespace: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT file_name, bytes, autoextensible
        FROM dba_data_files
        WHERE tablespace_name = :ts
        ORDER BY file_id
        """,
        {"ts": tablespace},
    )
    return [
        {"path": row[0], "bytes": int(row[1] or 0), "autoextensible": row[2]}
        for row in cur.fetchall()
    ]


def _oracle_tablespace_shared_with(cur, tablespace: str, owner: str) -> list[str]:
    users: set[str] = set()
    cur.execute(
        """
        SELECT DISTINCT owner
        FROM dba_segments
        WHERE tablespace_name = :ts AND owner <> :owner
        """,
        {"ts": tablespace, "owner": owner},
    )
    users.update(row[0] for row in cur.fetchall())
    cur.execute(
        """
        SELECT username
        FROM dba_users
        WHERE default_tablespace = :ts AND username <> :owner
        """,
        {"ts": tablespace, "owner": owner},
    )
    users.update(row[0] for row in cur.fetchall())
    return sorted(users)


def _oracle_is_maintained_user(conn: CleanupConnection, owner: str) -> bool:
    with _oracle_conn(conn) as db:
        cur = db.cursor()
        cur.execute(
            "SELECT oracle_maintained FROM dba_users WHERE username = :owner",
            {"owner": owner},
        )
        row = cur.fetchone()
    return bool(row and row[0] == "Y")


def _sqlserver_executor(conn: CleanupConnection) -> SqlServerDockerExecutor:
    settings = get_settings()
    ssh_password = (
        conn.ssh_password.get_secret_value()
        if conn.ssh_password
        else settings.sqlserver_docker_ssh_password
    )
    runtime = SqlServerDockerRuntime(
        host=RemoteHost(
            host=conn.ssh_host or settings.sqlserver_docker_host,
            port=conn.ssh_port or settings.sqlserver_docker_ssh_port,
            username=conn.ssh_user or settings.sqlserver_docker_ssh_user,
            password=ssh_password,
        ),
        container=conn.container_name or settings.sqlserver_container_name,
        sa_password=_connection_password(conn),
    )
    return SqlServerDockerExecutor(runtime)


def _sqlserver_run(conn: CleanupConnection, sql: str, *, timeout: int = 120) -> str:
    result = _sqlserver_executor(conn).run_sql(sql, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "sqlcmd failed")
    return result.stdout


def _sqlserver_rows(conn: CleanupConnection, sql: str) -> list[list[str]]:
    out = _sqlserver_run(conn, sql)
    rows: list[list[str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("(") or set(line) <= {"-"}:
            continue
        rows.append([part.strip() for part in line.split("|")])
    return rows


def _cleanup_sqlserver_files(conn: CleanupConnection, files: list[dict[str, Any]]) -> list[str]:
    settings = get_settings()
    allowed = settings.sqlserver_data_host_path.rstrip("/") + "/"
    removed: list[str] = []
    remote = _sqlserver_executor(conn).runtime.host
    for item in files:
        path = str(item.get("path") or "")
        if not path or not path.startswith(allowed):
            continue
        cmd = f"if [ -f {shlex.quote(path)} ]; then rm -f {shlex.quote(path)} && echo {shlex.quote(path)}; fi"
        result = run_ssh_command(remote, cmd, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            removed.append(path)
    return removed


def _mysql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _oracle_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlserver_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _sqlserver_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _connection_password(conn: CleanupConnection) -> str:
    password = conn.password.get_secret_value()
    if password:
        return password
    settings = get_settings()
    if conn.engine == "mysql":
        return settings.mysql_restore_root_password
    if conn.engine == "oracle":
        return settings.oracle_pwd
    if conn.engine == "sqlserver":
        return settings.sqlserver_sa_password
    return password
