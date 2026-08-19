import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from recovery_service.core.domain import DumpArtifact


@dataclass(frozen=True)
class SqlServerFileSet:
    group_id: str
    bak_files: list[DumpArtifact] = field(default_factory=list)
    mdf_files: list[DumpArtifact] = field(default_factory=list)
    ndf_files: list[DumpArtifact] = field(default_factory=list)
    ldf_files: list[DumpArtifact] = field(default_factory=list)


@dataclass(frozen=True)
class SqlServerRestorePlan:
    method: str
    database_name: str
    files: list[DumpArtifact]
    reason: str


def group_sqlserver_files(files: list[DumpArtifact]) -> list[SqlServerFileSet]:
    bak_files = [f for f in files if _suffix(f.filename) == ".bak"]
    data_files = [f for f in files if _suffix(f.filename) in {".mdf", ".ndf", ".ldf"}]
    groups: list[SqlServerFileSet] = []
    for bak in sorted(bak_files, key=lambda f: f.filename.lower()):
        groups.append(
            SqlServerFileSet(
                group_id=f"bak:{_stem(bak.filename)}",
                bak_files=[bak],
            )
        )

    by_base: dict[str, list[DumpArtifact]] = {}
    for file in data_files:
        by_base.setdefault(_base_name(file.filename), []).append(file)
    for base, grouped in sorted(by_base.items()):
        groups.append(
            SqlServerFileSet(
                group_id=f"files:{base}",
                mdf_files=[f for f in grouped if _suffix(f.filename) == ".mdf"],
                ndf_files=[f for f in grouped if _suffix(f.filename) == ".ndf"],
                ldf_files=[f for f in grouped if _suffix(f.filename) == ".ldf"],
            )
        )
    return groups


def choose_restore_plan(file_set: SqlServerFileSet) -> SqlServerRestorePlan:
    if file_set.bak_files:
        bak = sorted(file_set.bak_files, key=lambda f: f.filename.lower())[0]
        return SqlServerRestorePlan(
            method="bak",
            database_name=derive_database_name(bak.filename),
            files=[bak],
            reason="BAK backup file detected; use RESTORE DATABASE",
        )
    attach_files = [*file_set.mdf_files, *file_set.ndf_files, *file_set.ldf_files]
    if file_set.mdf_files and file_set.ldf_files:
        return SqlServerRestorePlan(
            method="attach",
            database_name=derive_database_name(file_set.mdf_files[0].filename),
            files=attach_files,
            reason="MDF and LDF files detected; use CREATE DATABASE FOR ATTACH",
        )
    if file_set.mdf_files:
        return SqlServerRestorePlan(
            method="attach_rebuild_log",
            database_name=derive_database_name(file_set.mdf_files[0].filename),
            files=attach_files,
            reason="MDF detected without LDF; use FOR ATTACH_REBUILD_LOG",
        )
    if file_set.ldf_files:
        return SqlServerRestorePlan(
            method="unsupported_ldf_only",
            database_name=derive_database_name(file_set.ldf_files[0].filename),
            files=file_set.ldf_files,
            reason="Only LDF transaction log files were found; normal database recovery requires BAK or MDF",
        )
    return SqlServerRestorePlan(
        method="unsupported",
        database_name="UNSUPPORTED_SQLSERVER_FILES",
        files=[],
        reason="No BAK/MDF/LDF files detected",
    )


def derive_database_name(filename: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", _stem(filename)).strip("_")
    if not value:
        value = "RecoveredDb"
    if not value[0].isalpha():
        value = f"DB_{value}"
    return value[:120]


def _suffix(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def _stem(filename: str) -> str:
    return PurePosixPath(filename).name.rsplit(".", 1)[0]


def _base_name(filename: str) -> str:
    stem = _stem(filename).lower()
    for token in ("_log", "-log", ".log", "log"):
        if stem.endswith(token):
            stem = stem[: -len(token)]
            break
    return stem.strip("_-.") or _stem(filename).lower()
