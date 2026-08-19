from recovery_service.core.domain import ImpdpParams, TargetDatabase
from recovery_service.core.enums import CorrectionActionType
from recovery_service.engine.correction.ora_dictionary import OraMatch
from recovery_service.engine.correction.remapper import apply_correction
from recovery_service.infrastructure.oracle import catalog_queries


def resolve_with_catalog(
    params: ImpdpParams,
    target: TargetDatabase,
    match: OraMatch,
    stderr: str,
    options: dict,
) -> ImpdpParams:
    action = match.action

    if action.type == CorrectionActionType.CREATE_USER_FROM_REMAP:
        for mapping in params.remap_schema:
            src, _, dest = mapping.partition(":")
            catalog_queries.ensure_user_stub(target, dest or src, target.default_tablespace)
        return params

    if action.type == CorrectionActionType.CREATE_TABLESPACE_STUB:
        for mapping in params.remap_tablespace:
            src, _, dest = mapping.partition(":")
            catalog_queries.ensure_tablespace_stub(
                target,
                dest or src,
                options.get("datafile_dir", "+DATA"),
            )
        return params

    if action.type == CorrectionActionType.ENSURE_ORACLE_DIRECTORY:
        # DIRECTORY must exist in DB; document for ops
        params.directory = options.get("oracle_directory", params.directory or "DATA_PUMP_DIR")
        return params

    return apply_correction(params, target, action, stderr, options)
