from dataclasses import dataclass, field

from recovery_service.core.domain import TargetDatabase


@dataclass(frozen=True)
class OracleValidationReport:
    schema: str
    table_count: int = 0
    object_count: int = 0
    invalid_objects: list[dict] = field(default_factory=list)
    compile_attempted: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "table_count": self.table_count,
            "object_count": self.object_count,
            "invalid_objects": self.invalid_objects,
            "compile_attempted": self.compile_attempted,
            "errors": self.errors,
            "ok": self.ok,
        }


def validate_oracle_import(
    target: TargetDatabase,
    *,
    schema: str,
    compile_invalid: bool = True,
) -> OracleValidationReport:
    import oracledb

    schema = schema.upper()
    errors: list[str] = []
    compile_attempted = False
    try:
        conn = oracledb.connect(
            user=target.admin_user,
            password=target.admin_password,
            dsn=target.connection_string,
        )
    except oracledb.Error as exc:
        return OracleValidationReport(schema=schema, errors=[str(exc)])

    try:
        cur = conn.cursor()
        table_count = _count(
            cur,
            "SELECT COUNT(*) FROM dba_tables WHERE owner = :owner",
            schema,
        )
        object_count = _count(
            cur,
            "SELECT COUNT(*) FROM dba_objects WHERE owner = :owner",
            schema,
        )
        invalid_objects = _invalid_objects(cur, schema)
        if compile_invalid and invalid_objects:
            compile_attempted = True
            _compile_invalid_objects(cur, schema, invalid_objects)
            conn.commit()
            invalid_objects = _invalid_objects(cur, schema)
        return OracleValidationReport(
            schema=schema,
            table_count=table_count,
            object_count=object_count,
            invalid_objects=invalid_objects,
            compile_attempted=compile_attempted,
            errors=errors,
        )
    except Exception as exc:
        errors.append(str(exc))
        return OracleValidationReport(schema=schema, errors=errors)
    finally:
        conn.close()


def _count(cur, sql: str, owner: str) -> int:
    cur.execute(sql, owner=owner)
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _invalid_objects(cur, owner: str) -> list[dict]:
    cur.execute(
        """
        SELECT object_name, object_type, status
        FROM dba_objects
        WHERE owner = :owner AND status <> 'VALID'
        ORDER BY object_type, object_name
        """,
        owner=owner,
    )
    return [
        {"name": row[0], "type": row[1], "status": row[2]}
        for row in cur.fetchall()
    ]


def _compile_invalid_objects(cur, owner: str, invalid_objects: list[dict]) -> None:
    for obj in invalid_objects:
        name = str(obj["name"]).replace('"', '""')
        obj_type = str(obj["type"]).upper()
        if obj_type == "PACKAGE BODY":
            statement = f'ALTER PACKAGE "{owner}"."{name}" COMPILE BODY'
        elif obj_type == "PACKAGE":
            statement = f'ALTER PACKAGE "{owner}"."{name}" COMPILE'
        elif obj_type in {"FUNCTION", "PROCEDURE", "TRIGGER", "VIEW"}:
            statement = f'ALTER {obj_type} "{owner}"."{name}" COMPILE'
        else:
            continue
        try:
            cur.execute(statement)
        except Exception:
            continue
