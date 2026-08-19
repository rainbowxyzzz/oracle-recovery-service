from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # Python < 3.11

    class StrEnum(str, Enum):
        """3.10 兼容：与 enum.StrEnum 行为一致。"""

        pass


class TaskState(StrEnum):
    CREATED = "created"
    DISCOVERING = "discovering"
    POLICY_RUNNING = "policy_running"
    METADATA_READY = "metadata_ready"
    IMPORTING = "importing"
    CORRECTING = "correcting"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PolicyNodeId(StrEnum):
    PARSE_LOG = "parse_log"
    PARSE_PARFILE = "parse_parfile"
    PARSE_FILENAME = "parse_filename"
    SQLFILE_PROBE = "sqlfile_probe"
    TRIAL_IMPORT = "trial_import"
    EXECUTE_IMPORT = "execute_import"
    ORA_CORRECTION_LOOP = "ora_correction_loop"


class ImportMode(StrEnum):
    FULL = "FULL"
    SCHEMA = "SCHEMA"
    TABLE = "TABLE"
    TABLESPACE = "TABLESPACE"
    TRANSPORTABLE = "TRANSPORTABLE"
    UNKNOWN = "UNKNOWN"


class DumpVolumeType(StrEnum):
    SINGLE = "single"
    MULTI = "multi"
    UNKNOWN = "unknown"


class CorrectionActionType(StrEnum):
    REMAP_SCHEMA = "remap_schema"
    REMAP_TABLESPACE = "remap_tablespace"
    SET_TABLE_EXISTS_ACTION = "set_table_exists_action"
    SET_VERSION = "set_version"
    CREATE_USER_FROM_REMAP = "create_user_from_remap"
    CREATE_TABLESPACE_STUB = "create_tablespace_stub"
    ENSURE_ORACLE_DIRECTORY = "ensure_oracle_directory"
    DROP_INVALID_PARAMETER = "drop_invalid_parameter"
    REDUCE_PARALLEL = "reduce_parallel"
    LOG_AND_FAIL = "log_and_fail"
