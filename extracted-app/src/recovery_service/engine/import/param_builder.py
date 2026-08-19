from recovery_service.core.domain import DumpVolumeGroup, ExportMetadata, ImpdpParams, TargetDatabase
from recovery_service.core.enums import ImportMode
from recovery_service.engine.metadata.filename_heuristics import infer_from_filenames


def build_impdp_params(
    target: TargetDatabase,
    group: DumpVolumeGroup,
    metadata: ExportMetadata,
    *,
    oracle_directory: str = "DATA_PUMP_DIR",
    options: dict | None = None,
) -> ImpdpParams:
    options = options or {}
    conn = f"{target.admin_user}/{target.admin_password}@{target.connection_string}"

    ex = options.get("execution") or {}
    oracle_directory = ex.get("oracle_directory") or options.get("oracle_directory", "DATA_PUMP_DIR")

    dumpfile = metadata.dumpfile_param
    if not dumpfile and group.dump_files:
        fn_meta = infer_from_filenames(group)
        dumpfile = fn_meta.dumpfile_param if fn_meta else group.dump_files[0].filename

    params = ImpdpParams(
        connection=conn,
        directory=oracle_directory or "DATA_PUMP_DIR",
        dumpfile=dumpfile,
        logfile=options.get("logfile") or f"imp_{group.group_id}.log",
        schemas=metadata.schemas or options.get("schemas", []),
        tables=metadata.tables or options.get("tables", []),
        table_exists_action=options.get("table_exists_action", "SKIP"),
        parallel=int(options.get("parallel", 4)),
        access_method=(
            str(options.get("access_method", "")).strip().upper() or None
        ),
        version=metadata.source_version or options.get("version"),
        remap_schema=options.get("remap_schema", []),
        remap_tablespace=options.get("remap_tablespace", []),
    )

    if metadata.export_mode == ImportMode.FULL:
        params.full = True
    elif metadata.export_mode == ImportMode.SCHEMA and params.schemas:
        pass
    elif metadata.export_mode == ImportMode.TABLE and params.tables:
        pass

    return params
