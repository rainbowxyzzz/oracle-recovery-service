import re

from recovery_service.core.domain import ImpdpParams, TargetDatabase
from recovery_service.core.enums import CorrectionActionType
from recovery_service.engine.correction.ora_dictionary import CorrectionAction


_SCHEMA_RE = re.compile(r"user\s+[\"']?(\w+)[\"']?\s+does not exist", re.I)
_TABLESPACE_RE = re.compile(r"tablespace\s+[\"']?(\w+)[\"']?\s+does not exist", re.I)


def apply_correction(
    params: ImpdpParams,
    target: TargetDatabase,
    action: CorrectionAction,
    stderr: str,
    options: dict,
) -> ImpdpParams:
    at = action.type

    if at == CorrectionActionType.REMAP_SCHEMA:
        for name in _SCHEMA_RE.findall(stderr):
            dest = options.get("default_remap_schema_dest", name)
            mapping = f"{name}:{dest}"
            if mapping not in params.remap_schema:
                params.remap_schema.append(mapping)

    elif at == CorrectionActionType.REMAP_TABLESPACE:
        for name in _TABLESPACE_RE.findall(stderr):
            dest = options.get("default_remap_tablespace_dest", target.default_tablespace)
            mapping = f"{name}:{dest}"
            if mapping not in params.remap_tablespace:
                params.remap_tablespace.append(mapping)

    elif at == CorrectionActionType.SET_TABLE_EXISTS_ACTION:
        params.table_exists_action = action.params.get("value", "REPLACE")

    elif at == CorrectionActionType.SET_VERSION:
        params.version = action.params.get("value")

    elif at == CorrectionActionType.REDUCE_PARALLEL:
        params.parallel = max(1, params.parallel // 2)

    elif at == CorrectionActionType.DROP_INVALID_PARAMETER:
        key = action.params.get("key")
        if key and key in params.extra:
            del params.extra[key]

    return params
