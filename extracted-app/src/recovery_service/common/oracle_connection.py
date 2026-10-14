"""Resolve saved Oracle profiles identically for testing and recovery snapshots."""


def oracle_connection_dsn(connection) -> str:
    dsn = (connection.dsn or "").strip()
    if dsn:
        return dsn
    service = (connection.service_name or connection.database or "").strip()
    return f"{connection.host}:{connection.port or 1521}/{service}"
