import re
from importlib import import_module

from recovery_service.core.domain import ExportMetadata, ImpdpParams, TargetDatabase
from recovery_service.core.enums import ImportMode

ImpdpRunner = import_module("recovery_service.engine.import.impdp_runner").ImpdpRunner

_ORA_RE = re.compile(r"(ORA-\d{5})", re.I)
_SCHEMA_RE = re.compile(r"schema\s+[\"']?(\w+)[\"']?", re.I)
_TABLESPACE_RE = re.compile(r"tablespace\s+[\"']?(\w+)[\"']?", re.I)


def trial_import_and_analyze(
    runner: ImpdpRunner,
    target: TargetDatabase,
    params: ImpdpParams,
    *,
    timeout: int = 900,
) -> tuple[ExportMetadata | None, str, list[str]]:
    trial = ImpdpParams(
        connection=params.connection,
        directory=params.directory,
        dumpfile=params.dumpfile,
        logfile=params.logfile,
        schemas=params.schemas,
        tables=params.tables,
        full=params.full,
        parallel=1,
        content="METADATA_ONLY",
        version=params.version,
        table_exists_action="SKIP",
        remap_schema=params.remap_schema,
        remap_tablespace=params.remap_tablespace,
    )
    result = runner.run_import(trial, timeout=timeout, allow_failure=True)
    output = (result.stdout or "") + (result.stderr or "")
    ora_codes = list(dict.fromkeys(_ORA_RE.findall(output)))

    meta = ExportMetadata(discovery_source="trial_import", confidence=0.7)
    if "FULL" in output.upper():
        meta.export_mode = ImportMode.FULL

    for s in _SCHEMA_RE.findall(output):
        if s.upper() not in meta.schemas:
            meta.schemas.append(s.upper())
    for t in _TABLESPACE_RE.findall(output):
        if t.upper() not in meta.tablespaces:
            meta.tablespaces.append(t.upper())

    if meta.export_mode != ImportMode.UNKNOWN or meta.schemas or ora_codes:
        return meta, output, ora_codes
    return None, output, ora_codes
