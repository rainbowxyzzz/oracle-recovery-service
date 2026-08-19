import re
from dataclasses import dataclass

import oracledb

from recovery_service.core.domain import TargetDatabase
from recovery_service.core.exceptions import TargetDatabaseConnectionError

_IDENT_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,29}$")
_SAFE_SCHEMA_PREFIX = "U_"
_SAFE_TABLESPACE_PREFIX = "TS_U_"
_SYSTEM_SCHEMAS = {
    "SYS",
    "SYSTEM",
    "SYSAUX",
    "DBSNMP",
    "OUTLN",
    "XDB",
    "WMSYS",
    "ORDDATA",
    "MDSYS",
    "CTXSYS",
    "APEX_PUBLIC_USER",
    "GSMADMIN_INTERNAL",
    "GSMCATUSER",
    "GSMUSER",
}


@dataclass(frozen=True)
class PreparedOracleTarget:
    directory_name: str
    directory_path: str
    tablespace_name: str
    datafile_path: str
    username: str
    password: str
    bigfile: bool = True
    initial_size: str = "10G"
    next_size: str = "1G"
    max_size: str = "UNLIMITED"


@dataclass(frozen=True)
class OracleTargetResetReport:
    username: str
    tablespace_name: str
    datafile_path: str
    existing_datafiles: list[str]
    killed_sessions: list[str]
    dropped_user: bool
    dropped_tablespace: bool

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "tablespace": self.tablespace_name,
            "datafile_path": self.datafile_path,
            "existing_datafiles": self.existing_datafiles,
            "killed_sessions": self.killed_sessions,
            "dropped_user": self.dropped_user,
            "dropped_tablespace": self.dropped_tablespace,
        }


def derive_identifier(raw: str, *, prefix: str = "") -> str:
    value = re.sub(r"[^A-Za-z0-9_$#]+", "_", raw).strip("_").upper()
    if not value:
        value = "IMPORT"
    if not value[0].isalpha():
        value = f"{prefix or 'U'}_{value}"
    elif prefix:
        value = f"{prefix}_{value}"
    return value[:30]


def reset_recovery_target(
    target: TargetDatabase,
    *,
    tablespace_name: str,
    tablespace_container_path: str,
    username: str,
) -> OracleTargetResetReport:
    tablespace_name = _validate_identifier(tablespace_name)
    username = _validate_identifier(username)
    _validate_safe_recovery_target(username, tablespace_name)
    datafile_path = f"{tablespace_container_path.rstrip('/')}/{tablespace_name.lower()}01.dbf"

    try:
        conn = oracledb.connect(
            user=target.admin_user,
            password=target.admin_password,
            dsn=target.connection_string,
        )
    except oracledb.Error as exc:
        raise TargetDatabaseConnectionError(
            _format_oracle_connect_error(exc, target)
        ) from exc
    try:
        cur = conn.cursor()
        existing_datafiles = _tablespace_datafiles(cur, tablespace_name)
        killed_sessions = _kill_user_sessions(cur, username)
        dropped_user = _drop_user_if_exists(cur, username)
        dropped_tablespace = _drop_tablespace_if_exists(cur, tablespace_name)
        conn.commit()
    finally:
        conn.close()

    return OracleTargetResetReport(
        username=username,
        tablespace_name=tablespace_name,
        datafile_path=datafile_path,
        existing_datafiles=existing_datafiles,
        killed_sessions=killed_sessions,
        dropped_user=dropped_user,
        dropped_tablespace=dropped_tablespace,
    )


def ensure_recovery_prerequisites(
    target: TargetDatabase,
    *,
    directory_name: str,
    directory_path: str,
    tablespace_name: str,
    tablespace_container_path: str,
    username: str,
    user_password: str,
    bigfile: bool = True,
    initial_size: str = "10G",
    next_size: str = "1G",
    max_size: str = "UNLIMITED",
) -> PreparedOracleTarget:
    directory_name = _validate_identifier(directory_name)
    tablespace_name = _validate_identifier(tablespace_name)
    username = _validate_identifier(username)
    datafile_path = f"{tablespace_container_path.rstrip('/')}/{tablespace_name.lower()}01.dbf"

    try:
        conn = oracledb.connect(
            user=target.admin_user,
            password=target.admin_password,
            dsn=target.connection_string,
        )
    except oracledb.Error as exc:
        raise TargetDatabaseConnectionError(
            _format_oracle_connect_error(exc, target)
        ) from exc
    try:
        cur = conn.cursor()
        cur.execute(
            f"CREATE OR REPLACE DIRECTORY {directory_name} AS {_sql_literal(directory_path)}"
        )
        _drop_user_if_exists(cur, username)
        _drop_tablespace_if_exists(cur, tablespace_name)
        tablespace_kind = "BIGFILE " if bigfile else ""
        cur.execute(
            f"CREATE {tablespace_kind}TABLESPACE {tablespace_name} "
            f"DATAFILE {_sql_literal(datafile_path)} "
            f"SIZE {_validate_size(initial_size)} AUTOEXTEND ON "
            f"NEXT {_validate_size(next_size)} MAXSIZE {_validate_max_size(max_size)}"
        )
        cur.execute(
            f'CREATE USER "{username}" IDENTIFIED BY "{_escape_password(user_password)}" '
            f"DEFAULT TABLESPACE {tablespace_name} "
            f"TEMPORARY TABLESPACE {target.default_temp_tablespace} "
            f"QUOTA UNLIMITED ON {tablespace_name}"
        )

        cur.execute(f'GRANT CONNECT, RESOURCE, DBA TO "{username}"')
        cur.execute(f'GRANT READ, WRITE ON DIRECTORY {directory_name} TO "{username}"')
        conn.commit()
    finally:
        conn.close()

    return PreparedOracleTarget(
        directory_name=directory_name,
        directory_path=directory_path,
        tablespace_name=tablespace_name,
        datafile_path=datafile_path,
        username=username,
        password=user_password,
        bigfile=bigfile,
        initial_size=initial_size,
        next_size=next_size,
        max_size=max_size,
    )


def grow_tablespace_for_import(
    target: TargetDatabase,
    *,
    tablespace_name: str,
    tablespace_container_path: str,
    next_size: str = "1G",
    add_datafile_size: str = "10G",
    max_size: str = "UNLIMITED",
) -> dict:
    tablespace_name = _validate_identifier(tablespace_name)
    next_size = _validate_size(next_size)
    add_datafile_size = _validate_size(add_datafile_size)
    max_size = _validate_max_size(max_size)
    try:
        conn = oracledb.connect(
            user=target.admin_user,
            password=target.admin_password,
            dsn=target.connection_string,
        )
    except oracledb.Error as exc:
        raise TargetDatabaseConnectionError(
            _format_oracle_connect_error(exc, target)
        ) from exc
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT bigfile
            FROM dba_tablespaces
            WHERE tablespace_name = :v
            """,
            v=tablespace_name,
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Tablespace does not exist: {tablespace_name}")
        is_bigfile = str(row[0]).upper() == "YES"
        cur.execute(
            """
            SELECT file_name
            FROM dba_data_files
            WHERE tablespace_name = :v
            ORDER BY file_id
            """,
            v=tablespace_name,
        )
        files = [r[0] for r in cur.fetchall()]
        if is_bigfile and files:
            cur.execute(
                f"ALTER DATABASE DATAFILE {_sql_literal(files[0])} "
                f"AUTOEXTEND ON NEXT {next_size} MAXSIZE {max_size}"
            )
            action = "alter_bigfile_autoextend"
            datafile_path = files[0]
        else:
            index = len(files) + 1
            datafile_path = (
                f"{tablespace_container_path.rstrip('/')}/"
                f"{tablespace_name.lower()}{index:02d}.dbf"
            )
            cur.execute(
                f"ALTER TABLESPACE {tablespace_name} "
                f"ADD DATAFILE {_sql_literal(datafile_path)} "
                f"SIZE {add_datafile_size} AUTOEXTEND ON NEXT {next_size} MAXSIZE {max_size}"
            )
            action = "add_datafile"
        conn.commit()
        return {
            "tablespace": tablespace_name,
            "bigfile": is_bigfile,
            "action": action,
            "datafile": datafile_path,
            "next_size": next_size,
            "max_size": max_size,
        }
    finally:
        conn.close()


def _exists(cur, sql: str, value: str) -> bool:
    cur.execute(sql, v=value)
    return cur.fetchone() is not None


def _tablespace_datafiles(cur, tablespace_name: str) -> list[str]:
    cur.execute(
        """
        SELECT file_name
        FROM dba_data_files
        WHERE tablespace_name = :v
        ORDER BY file_id
        """,
        v=tablespace_name,
    )
    return [str(row[0]) for row in cur.fetchall()]


def _kill_user_sessions(cur, username: str) -> list[str]:
    cur.execute(
        """
        SELECT sid, serial#
        FROM v$session
        WHERE username = :v
        """,
        v=username,
    )
    sessions = [f"{row[0]},{row[1]}" for row in cur.fetchall()]
    for session in sessions:
        cur.execute(f"ALTER SYSTEM KILL SESSION {_sql_literal(session)} IMMEDIATE")
    return sessions


def _drop_user_if_exists(cur, username: str) -> bool:
    if _exists(cur, "SELECT 1 FROM dba_users WHERE username = :v", username):
        cur.execute(f'DROP USER "{username}" CASCADE')
        return True
    return False


def _drop_tablespace_if_exists(cur, tablespace_name: str) -> bool:
    if _exists(cur, "SELECT 1 FROM dba_tablespaces WHERE tablespace_name = :v", tablespace_name):
        cur.execute(f"DROP TABLESPACE {tablespace_name} INCLUDING CONTENTS AND DATAFILES")
        return True
    return False


def _validate_safe_recovery_target(username: str, tablespace_name: str) -> None:
    if username in _SYSTEM_SCHEMAS or username.startswith("APEX_") or username.startswith("GSM"):
        raise ValueError(f"Refusing to drop protected Oracle user: {username}")
    if not username.startswith(_SAFE_SCHEMA_PREFIX):
        raise ValueError(f"Refusing to drop non-recovery Oracle user: {username}")
    if not tablespace_name.startswith(_SAFE_TABLESPACE_PREFIX):
        raise ValueError(f"Refusing to drop non-recovery Oracle tablespace: {tablespace_name}")


def _validate_identifier(value: str) -> str:
    upper = value.upper()
    if not _IDENT_RE.match(upper):
        raise ValueError(f"Invalid Oracle identifier: {value}")
    return upper


def _validate_size(value: str) -> str:
    upper = value.strip().upper()
    if not re.match(r"^\d+[KMGTP]?$", upper):
        raise ValueError(f"Invalid Oracle size: {value}")
    return upper


def _validate_max_size(value: str) -> str:
    upper = value.strip().upper()
    if upper == "UNLIMITED":
        return upper
    return _validate_size(upper)


def _escape_password(value: str) -> str:
    return value.replace('"', '""')


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _format_oracle_connect_error(exc: oracledb.Error, target: TargetDatabase) -> str:
    text = str(exc)
    if "ORA-01017" in text:
        return (
            "Oracle 管理员账号登录失败：ORA-01017 invalid username/password; logon denied。"
            f"请检查请求体 target.connection={target.connection_string!r}、"
            f"target.admin_user={target.admin_user!r}、target.admin_password 是否正确，"
            "以及该账号是否能连接到指定 service/PDB。建议优先使用 SYSTEM 等具备 DBA 权限的普通管理员账号。"
        )
    if "ORA-12154" in text or "DPY-4027" in text:
        return (
            "Oracle 连接串无法解析或无法连接。"
            f"请检查 target.connection={target.connection_string!r}，格式通常为 host:1521/service_name。"
            f"原始错误：{text}"
        )
    return (
        "连接 Oracle 管理员账号失败。"
        f"请检查 target.connection={target.connection_string!r}、"
        f"target.admin_user={target.admin_user!r} 和密码。原始错误：{text}"
    )
