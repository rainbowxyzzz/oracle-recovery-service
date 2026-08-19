import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OracleRepairDecision:
    code: str
    diagnosis: str
    action: str
    retry: bool
    fatal_if_unfixed: bool = True
    evidence: list[str] = field(default_factory=list)


_DECISIONS: dict[str, tuple[str, str, bool, bool]] = {
    "ORA-00959": ("tablespace does not exist", "remap_or_create_tablespace", True, True),
    "ORA-01917": ("user or role does not exist", "create_user_or_remap_schema", True, True),
    "ORA-01918": ("user does not exist", "create_user_or_remap_schema", True, True),
    "ORA-01435": ("user does not exist", "create_user_or_remap_schema", True, True),
    "ORA-39087": ("directory name is invalid", "create_directory", True, True),
    "ORA-39070": ("unable to open import log file", "fix_directory_permissions", True, True),
    "ORA-29283": ("directory path is not readable or writable", "fix_directory_permissions", True, True),
    "ORA-01119": ("database datafile creation failed", "clean_stale_datafile", True, True),
    "ORA-27038": ("target datafile already exists", "clean_stale_datafile", True, True),
    "ORA-39002": ("invalid impdp operation or parameters", "regenerate_import_command", True, True),
    "ORA-39001": ("invalid impdp argument value", "regenerate_import_command", True, True),
    "ORA-39151": ("object already exists", "apply_table_exists_action", True, False),
    "ORA-01652": ("unable to extend temporary segment", "grow_tablespace", True, True),
    "ORA-01653": ("unable to extend table segment", "grow_tablespace", True, True),
    "ORA-01654": ("unable to extend index segment", "grow_tablespace", True, True),
    "ORA-39083": ("object creation failed", "classify_object_failure", False, False),
    "IMP-00019": ("row rejected during legacy import", "table_data_failure", False, True),
}


def decide_oracle_repairs(output: str) -> list[OracleRepairDecision]:
    decisions: list[OracleRepairDecision] = []
    seen: set[str] = set()
    for code in re.findall(r"\b(?:ORA|IMP)-\d{5}\b", output or "", flags=re.I):
        upper = code.upper()
        if upper in seen:
            continue
        seen.add(upper)
        if upper in _DECISIONS:
            diagnosis, action, retry, fatal = _DECISIONS[upper]
            decisions.append(
                OracleRepairDecision(
                    code=upper,
                    diagnosis=diagnosis,
                    action=action,
                    retry=retry,
                    fatal_if_unfixed=fatal,
                    evidence=_lines_for_code(output, upper),
                )
            )
        else:
            decisions.append(
                OracleRepairDecision(
                    code=upper,
                    diagnosis="unclassified Oracle import error",
                    action="fail_for_manual_review",
                    retry=False,
                    fatal_if_unfixed=True,
                    evidence=_lines_for_code(output, upper),
                )
            )
    return decisions


def retryable_actions(decisions: list[OracleRepairDecision]) -> list[OracleRepairDecision]:
    return [decision for decision in decisions if decision.retry]


def _lines_for_code(output: str, code: str) -> list[str]:
    return [
        line.strip()
        for line in (output or "").splitlines()
        if code in line.upper()
    ][:10]
