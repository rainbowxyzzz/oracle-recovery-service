from importlib import import_module

from recovery_service.core.domain import ExportMetadata, ImpdpParams, TargetDatabase
from recovery_service.core.enums import ImportMode
from recovery_service.settings import get_settings

ImpdpRunner = import_module("recovery_service.engine.import.impdp_runner").ImpdpRunner


def probe_via_sqlfile(
    runner: ImpdpRunner,
    target: TargetDatabase,
    params: ImpdpParams,
    sqlfile_name: str,
) -> tuple[ExportMetadata | None, str]:
    probe_params = ImpdpParams(
        connection=params.connection,
        directory=params.directory,
        dumpfile=params.dumpfile,
        sqlfile=sqlfile_name,
        schemas=params.schemas,
        tables=params.tables,
        full=params.full,
        parallel=1,
        content="METADATA_ONLY",
        version=params.version,
        remap_schema=params.remap_schema,
        remap_tablespace=params.remap_tablespace,
        table_exists_action="SKIP",
    )
    try:
        result = runner.run_import(
            probe_params,
            timeout=get_settings().oracle_import_operation_timeout_seconds,
        )
    except Exception as e:
        return None, str(e)

    combined = (result.stdout or "") + (result.stderr or "")
    meta = ExportMetadata(discovery_source="sqlfile_probe", confidence=0.75)
    upper = combined.upper()
    if "FULL DATABASE" in upper or "FULL=Y" in upper:
        meta.export_mode = ImportMode.FULL
    elif "CREATE TABLE" in upper:
        meta.export_mode = ImportMode.TABLE
    elif "CREATE USER" in upper or "ALTER USER" in upper:
        meta.export_mode = ImportMode.SCHEMA

    if "VERSION" in upper and not meta.source_version:
        for ver in ("19.0.0", "12.2.0", "11.2.0"):
            if ver.replace(".0", "") in upper:
                meta.source_version = ver
                break

    if meta.export_mode != ImportMode.UNKNOWN or meta.source_version:
        return meta, combined
    return None, combined
