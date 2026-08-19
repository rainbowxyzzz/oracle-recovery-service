import re
from dataclasses import dataclass, field


IGNORABLE_OBJECT_TYPES = {
    "FUNCTION",
    "PROCEDURE",
    "PACKAGE",
    "PACKAGE_BODY",
    "TRIGGER",
    "VIEW",
    "SYNONYM",
    "GRANT",
    "OBJECT_GRANT",
    "SYSTEM_GRANT",
    "ROLE_GRANT",
    "STATISTICS",
    "INDEX_STATISTICS",
    "JOB",
    "SCHEDULER_JOB",
    "DB_LINK",
    "MATERIALIZED_VIEW",
}

FATAL_OBJECT_TYPES = {
    "TABLE",
    "TABLE_DATA",
    "INDEX",
    "CONSTRAINT",
    "REF_CONSTRAINT",
    "LOB",
    "PARTITION",
    "TABLESPACE",
    "USER",
}

MYSQL_FATAL_MARKERS = [
    "CREATE TABLE",
    "INSERT INTO",
    "LOAD DATA",
    "ALTER TABLE",
    "TABLE ",
    "DUPLICATE ENTRY",
    "DATA TOO LONG",
    "INCORRECT ",
    "FOREIGN KEY",
    "CANNOT ADD OR UPDATE",
    "CAN'T CREATE TABLE",
    "ROW SIZE TOO LARGE",
    "KEY TOO LONG",
    "UNKNOWN COLUMN",
    "DOESN'T HAVE A DEFAULT VALUE",
]

MYSQL_IGNORABLE_MARKERS = [
    "FUNCTION",
    "PROCEDURE",
    "TRIGGER",
    "EVENT",
    "VIEW",
    "DEFINER",
    "GRANT",
    "ROUTINE",
    "LOG_BIN_TRUST_FUNCTION_CREATORS",
]

ORACLE_FATAL_MARKERS = [
    "ORA-01652",
    "ORA-01653",
    "ORA-01654",
    "ORA-01119",
    "ORA-27038",
    "ORA-01950",
    "ORA-00955",
    "ORA-00959",
    "ORA-12899",
    "ORA-01400",
    "ORA-02291",
    "ORA-02292",
    "ORA-00001",
    "IMP-00019",
]


@dataclass(frozen=True)
class ImportResultClassification:
    success: bool
    warning_only: bool = False
    fatal_errors: list[str] = field(default_factory=list)
    warning_errors: list[str] = field(default_factory=list)
    unknown_errors: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.success and self.warning_only:
            return "succeeded_with_warnings"
        return "succeeded" if self.success else "failed"

    @property
    def summary(self) -> str:
        if self.success and not self.warning_only:
            return "import completed without classified errors"
        if self.warning_only:
            return "table/data import accepted; non-table object errors were recorded as warnings"
        if self.fatal_errors:
            return "table/data related import errors detected"
        return "unclassified import errors detected"


def classify_import_result(engine: str, returncode: int, output: str) -> ImportResultClassification:
    if returncode == 0:
        return ImportResultClassification(success=True)
    if engine == "mysql":
        return _classify_mysql(output)
    if engine == "oracle":
        return _classify_oracle(output)
    return ImportResultClassification(success=False, unknown_errors=_error_lines(output))


def _classify_mysql(output: str) -> ImportResultClassification:
    fatal: list[str] = []
    warning: list[str] = []
    unknown: list[str] = []
    for line in _error_lines(output):
        upper = line.upper()
        if any(marker in upper for marker in MYSQL_FATAL_MARKERS):
            fatal.append(line)
        elif any(marker in upper for marker in MYSQL_IGNORABLE_MARKERS):
            warning.append(line)
        else:
            unknown.append(line)
    return _finish(fatal, warning, unknown)


def _classify_oracle(output: str) -> ImportResultClassification:
    fatal: list[str] = []
    warning: list[str] = []
    unknown: list[str] = []
    blocks, consumed = _oracle_object_blocks(output)
    for object_type, block in blocks:
        if object_type in FATAL_OBJECT_TYPES:
            fatal.append(block)
        elif object_type in IGNORABLE_OBJECT_TYPES:
            warning.append(block)
        else:
            unknown.append(block)

    for index, line in _error_lines_with_indexes(output):
        if index in consumed:
            continue
        upper = line.upper()
        if any(marker in upper for marker in ORACLE_FATAL_MARKERS):
            fatal.append(line)
        elif "ORA-" in upper or "IMP-" in upper or "UDI-" in upper:
            unknown.append(line)

    return _finish(fatal, warning, unknown)


def _oracle_object_blocks(output: str) -> tuple[list[tuple[str, str]], set[int]]:
    lines = output.splitlines()
    blocks: list[tuple[str, str]] = []
    consumed: set[int] = set()
    index = 0
    while index < len(lines):
        match = re.search(r"ORA-39083:.*Object type\s+([A-Z_]+)\b", lines[index], re.IGNORECASE)
        if not match:
            index += 1
            continue
        object_type = match.group(1).upper()
        start = index
        index += 1
        while index < len(lines):
            if re.search(r"ORA-39083:.*Object type\s+([A-Z_]+)\b", lines[index], re.IGNORECASE):
                break
            if re.search(r"^\s*(Processing object type|Job .* completed)", lines[index], re.IGNORECASE):
                break
            index += 1
        consumed.update(range(start, index))
        blocks.append((object_type, "\n".join(lines[start:index]).strip()))
    return blocks, consumed


def _finish(
    fatal: list[str],
    warning: list[str],
    unknown: list[str],
) -> ImportResultClassification:
    if fatal or unknown:
        return ImportResultClassification(
            success=False,
            fatal_errors=fatal,
            warning_errors=warning,
            unknown_errors=unknown,
        )
    if warning:
        return ImportResultClassification(
            success=True,
            warning_only=True,
            warning_errors=warning,
        )
    return ImportResultClassification(success=False, unknown_errors=["import failed without parsable errors"])


def _error_lines(output: str) -> list[str]:
    return [line for _, line in _error_lines_with_indexes(output)]


def _error_lines_with_indexes(output: str) -> list[tuple[int, str]]:
    lines: list[str] = []
    for index, line in enumerate((output or "").splitlines()):
        upper = line.upper()
        if "ERROR " in upper or "ORA-" in upper or "IMP-" in upper or "UDI-" in upper:
            lines.append((index, line.strip()))
    return lines
