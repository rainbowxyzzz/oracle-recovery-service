from recovery_service.core.domain import TargetDatabase
from recovery_service.infrastructure.oracle.connectivity import tablespace_exists, user_exists


def ensure_user_stub(target: TargetDatabase, username: str, tablespace: str | None = None) -> None:
    import oracledb

    ts = tablespace or target.default_tablespace
    ddl = (
        f'CREATE USER "{username.upper()}" IDENTIFIED BY "ChangeMe123!" '
        f"DEFAULT TABLESPACE {ts} TEMPORARY TABLESPACE {target.default_temp_tablespace} "
        f"QUOTA UNLIMITED ON {ts}"
    )
    conn = oracledb.connect(
        user=target.admin_user,
        password=target.admin_password,
        dsn=target.connection_string,
    )
    try:
        cur = conn.cursor()
        if not user_exists(target, username):
            cur.execute(ddl)
            cur.execute(f'GRANT CONNECT, RESOURCE TO "{username.upper()}"')
        conn.commit()
    finally:
        conn.close()


def ensure_tablespace_stub(target: TargetDatabase, tablespace: str, datafile_dir: str = "+DATA") -> None:
    import oracledb

    if tablespace_exists(target, tablespace):
        return
    ddl = (
        f"CREATE TABLESPACE {tablespace.upper()} "
        f"DATAFILE '{datafile_dir}/{tablespace.lower()}01.dbf' SIZE 100M AUTOEXTEND ON"
    )
    conn = oracledb.connect(
        user=target.admin_user,
        password=target.admin_password,
        dsn=target.connection_string,
    )
    try:
        cur = conn.cursor()
        cur.execute(ddl)
        conn.commit()
    finally:
        conn.close()
