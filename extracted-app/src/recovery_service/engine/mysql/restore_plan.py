import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from recovery_service.core.domain import DumpArtifact


SUPPORTED_SUFFIXES = (".sql", ".sql.gz", ".sql.zip", ".zip")


@dataclass(frozen=True)
class MySqlFileSet:
    group_id: str
    files: list[DumpArtifact] = field(default_factory=list)


@dataclass(frozen=True)
class MySqlRestorePlan:
    method: str
    database_name: str
    files: list[DumpArtifact]
    reason: str
    drop_existing: bool = True


def group_mysql_files(files: list[DumpArtifact]) -> list[MySqlFileSet]:
    supported = [f for f in files if is_supported_mysql_dump(f.filename)]
    if not supported:
        return []
    return [
        MySqlFileSet(
            group_id=f"mysql:{_base_name(file.filename)}",
            files=[file],
        )
        for file in sorted(supported, key=lambda f: f.filename.lower())
    ]


def choose_restore_plan(
    file_set: MySqlFileSet,
    *,
    target_database: str | None = None,
    drop_existing: bool = True,
) -> MySqlRestorePlan:
    if not file_set.files:
        return MySqlRestorePlan(
            method="unsupported",
            database_name="UNSUPPORTED_MYSQL_FILES",
            files=[],
            reason="No MySQL dump file detected",
            drop_existing=drop_existing,
        )
    file = file_set.files[0]
    method = _method_for(file.filename)
    database_name = normalize_database_name(target_database or _base_name(file.filename))
    return MySqlRestorePlan(
        method=method,
        database_name=database_name,
        files=[file],
        reason=f"MySQL {method} dump detected; import into database {database_name}",
        drop_existing=drop_existing,
    )


def is_supported_mysql_dump(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith(SUPPORTED_SUFFIXES)


def normalize_database_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if not name:
        name = "recovered_mysql"
    if not (name[0].isalpha() or name[0] == "_"):
        name = f"db_{name}"
    return name[:64]


def _method_for(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".sql.gz"):
        return "sql_gzip"
    if lower.endswith(".sql.zip") or lower.endswith(".zip"):
        return "sql_zip"
    return "sql"


def _base_name(filename: str) -> str:
    name = PurePosixPath(filename).name
    for suffix in (".sql.gz", ".sql.zip", ".sql", ".zip"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return PurePosixPath(name).stem
