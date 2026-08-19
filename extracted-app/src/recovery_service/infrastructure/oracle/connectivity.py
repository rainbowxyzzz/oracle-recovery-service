"""Target database connectivity checks via oracledb thin driver."""

import oracledb

from recovery_service.core.domain import TargetDatabase


def check_connection(target: TargetDatabase) -> bool:
    try:
        conn = oracledb.connect(
            user=target.admin_user,
            password=target.admin_password,
            dsn=target.connection_string,
        )
        conn.close()
        return True
    except oracledb.Error:
        return False


def user_exists(target: TargetDatabase, username: str) -> bool:
    sql = "SELECT 1 FROM dba_users WHERE username = :u"
    return _scalar_exists(target, sql, {"u": username.upper()})


def tablespace_exists(target: TargetDatabase, tablespace: str) -> bool:
    sql = "SELECT 1 FROM dba_tablespaces WHERE tablespace_name = :t"
    return _scalar_exists(target, sql, {"t": tablespace.upper()})


def _scalar_exists(target: TargetDatabase, sql: str, binds: dict) -> bool:
    conn = oracledb.connect(
        user=target.admin_user,
        password=target.admin_password,
        dsn=target.connection_string,
    )
    try:
        cur = conn.cursor()
        cur.execute(sql, binds)
        row = cur.fetchone()
        return row is not None
    finally:
        conn.close()
