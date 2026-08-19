import fnmatch
import hashlib
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath


_PARAM_LINE = re.compile(r"^;;;\s+(?:_?parfile):\s+(.*)$", re.IGNORECASE)
_PARAM_START = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*)$")
_COMMAND_PARAM = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_EXPORTED_DATA = re.compile(
    r'exported\s+"(?P<schema>[^"]+)"\."(?P<table>[^"]+)"'
    r'(?P<partition>.*?)\s+(?P<size>[0-9.]+)\s+'
    r'(?P<unit>B|KB|MB|GB|TB)\s+(?P<rows>[0-9]+)\s+rows\b',
    re.IGNORECASE,
)
_DUMP_PATH = re.compile(r"^\s+(.+\.dmp)\s*$", re.IGNORECASE)
_ERROR_LINE = re.compile(r"^(ORA|UDE|LRM)-\d+:", re.IGNORECASE)
_OBJECT_COMPLETION = re.compile(r"Completed\s+(\d+)\s+(.+?)\s+objects\b", re.IGNORECASE)
_JOB_REF = r'"[^"]+"(?:\."[^"]+")?'
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
_SIZE_FACTORS = {
    "B": Decimal(1),
    "KB": Decimal(1024),
    "MB": Decimal(1024) ** 2,
    "GB": Decimal(1024) ** 3,
    "TB": Decimal(1024) ** 4,
}


@dataclass
class ExportedTableSummary:
    table: str
    data_units: int = 0
    rows: int = 0
    bytes: int = 0
    zero_row_units: int = 0


@dataclass
class OracleExportLogManifest:
    recognized: bool = False
    tool: str = "unknown"
    source_status: str = "unrecognized"
    source_release: str = ""
    database_release: str = ""
    edition: str = ""
    job_name: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed: str = ""
    export_mode: str = "unknown"
    parameters: dict[str, str] = field(default_factory=dict)
    dumpfile_pattern: str = ""
    logfile_name: str = ""
    dump_files: list[str] = field(default_factory=list)
    schemas: list[str] = field(default_factory=list)
    requested_tables: list[str] = field(default_factory=list)
    duplicate_tables: list[str] = field(default_factory=list)
    exported_tables: list[str] = field(default_factory=list)
    exported_table_summaries: list[ExportedTableSummary] = field(default_factory=list)
    exported_data_units: int = 0
    exported_rows: int = 0
    exported_bytes: int = 0
    zero_row_units: int = 0
    missing_objects: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    object_counts: dict[str, int] = field(default_factory=dict)
    completion_error_count: int = 0
    confidence: float = 0.0
    content_sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def has_source_gaps(self) -> bool:
        return bool(self.missing_objects or self.completion_error_count)

    @property
    def usable_for_assisted_import(self) -> bool:
        return (
            self.recognized
            and self.tool == "expdp"
            and self.source_status in {"clean_success", "completed_with_errors"}
            and bool(self.dumpfile_pattern or self.dump_files)
        )

    def to_dict(self, *, include_table_details: bool = True) -> dict:
        data = asdict(self)
        if not include_table_details:
            data.pop("requested_tables", None)
            data.pop("exported_tables", None)
            data.pop("exported_table_summaries", None)
        data["requested_table_count"] = len(self.requested_tables)
        data["exported_table_count"] = len(self.exported_tables)
        data["missing_object_count"] = len(self.missing_objects)
        data["dump_file_count"] = len(self.dump_files)
        data["has_source_gaps"] = self.has_source_gaps
        data["usable_for_assisted_import"] = self.usable_for_assisted_import
        return data

    def expectation_dict(self) -> dict:
        return {
            "enabled": True,
            "tool": self.tool,
            "source_status": self.source_status,
            "export_mode": self.export_mode,
            "source_schemas": list(self.schemas),
            "dump_files": list(self.dump_files),
            "dumpfile_pattern": self.dumpfile_pattern,
            "logfile_name": self.logfile_name,
            "content_sha256": self.content_sha256,
            "completion_error_count": self.completion_error_count,
            "missing_object_count": len(self.missing_objects),
            "requested_table_count": len(self.requested_tables),
            "exported_table_count": len(self.exported_tables),
        }


@dataclass(frozen=True)
class ExportLogBinding:
    state: str
    score: int
    reasons: list[str] = field(default_factory=list)
    missing_dump_files: list[str] = field(default_factory=list)
    unexpected_dump_files: list[str] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        return self.state == "exact"

    def to_dict(self) -> dict:
        return asdict(self)


def parse_oracle_export_log(text: str) -> OracleExportLogManifest:
    manifest = OracleExportLogManifest(
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    lines = text.splitlines()
    export_header = _first_match(text, r"(?m)^Export:\s+Release\s+([0-9.]+).*$")
    if not export_header:
        return manifest

    manifest.recognized = True
    manifest.tool = "expdp" if "Parfile values:" in text or "SYS_EXPORT_" in text else "exp"
    manifest.source_release = export_header
    manifest.database_release = _first_match(text, r"(?m)^Version\s+([0-9.]+)\s*$")
    manifest.edition = _first_match(text, r"Connected to:\s+Oracle Database\s+(.+?Edition)\s+Release")
    manifest.started_at = _first_match(text, r"(?m)^Export:\s+Release\s+[0-9.]+\s+-\s+Production\s+on\s+(.+)$")

    params = _parse_parfile_values(lines)
    command_line = next((line for line in lines if line.startswith("Starting \"")), "")
    for key, value in _COMMAND_PARAM.findall(command_line):
        params.setdefault(key.lower(), value.strip().strip("'\""))
    manifest.parameters = params
    manifest.dumpfile_pattern = PurePosixPath(params.get("dumpfile", "")).name
    manifest.logfile_name = PurePosixPath(params.get("logfile", "")).name
    manifest.export_mode = _export_mode(params)

    requested_tables = _split_csv(params.get("tables", ""), qualified=True)
    manifest.requested_tables = _unique(requested_tables)
    manifest.duplicate_tables = sorted(
        name for name in set(requested_tables) if requested_tables.count(name) > 1
    )

    table_summaries: dict[str, ExportedTableSummary] = {}
    dump_section = False
    for line in lines:
        if line.startswith("Starting \""):
            manifest.job_name = _first_match(line, rf"^Starting\s+({_JOB_REF})")
        if line.strip().startswith("Dump file set for "):
            dump_section = True
            continue
        if dump_section:
            dump_match = _DUMP_PATH.match(line)
            if dump_match:
                manifest.dump_files.append(PurePosixPath(dump_match.group(1).strip()).name)
                continue
            if line.startswith("Job \""):
                dump_section = False

        exported = _EXPORTED_DATA.search(line)
        if exported:
            table = _qualified_name(exported.group("schema"), exported.group("table"))
            rows = int(exported.group("rows"))
            size_bytes = _size_to_bytes(exported.group("size"), exported.group("unit"))
            summary = table_summaries.setdefault(table, ExportedTableSummary(table=table))
            summary.data_units += 1
            summary.rows += rows
            summary.bytes += size_bytes
            if rows == 0:
                summary.zero_row_units += 1
            manifest.exported_data_units += 1
            manifest.exported_rows += rows
            manifest.exported_bytes += size_bytes
            if rows == 0:
                manifest.zero_row_units += 1

        if _ERROR_LINE.match(line):
            manifest.errors.append(line.strip())
            missing = re.search(r"ORA-39166:\s+Object\s+([^\s]+)", line, re.IGNORECASE)
            if missing:
                manifest.missing_objects.append(missing.group(1).strip().upper())

        completed = _OBJECT_COMPLETION.search(line)
        if completed:
            object_type = completed.group(2).strip()
            manifest.object_counts[object_type] = (
                manifest.object_counts.get(object_type, 0) + int(completed.group(1))
            )

    manifest.dump_files = _unique(manifest.dump_files)
    manifest.exported_table_summaries = sorted(table_summaries.values(), key=lambda item: item.table)
    manifest.exported_tables = [item.table for item in manifest.exported_table_summaries]
    manifest.missing_objects = _unique(manifest.missing_objects)
    manifest.schemas = _schemas_from_tables(
        [*manifest.requested_tables, *manifest.exported_tables, *manifest.missing_objects]
    )

    clean_footer = re.search(rf"(?m)^Job\s+{_JOB_REF}\s+successfully completed.*$", text, re.IGNORECASE)
    error_footer = re.search(
        rf"(?m)^Job\s+{_JOB_REF}\s+completed with\s+(\d+)\s+error\(s\).*$",
        text,
        re.IGNORECASE,
    )
    failed_footer = re.search(
        rf"(?m)^Job\s+{_JOB_REF}\s+(?:stopped|terminated|failed).*$",
        text,
        re.IGNORECASE,
    )
    footer = clean_footer or error_footer or failed_footer
    if footer:
        manifest.finished_at = _first_match(footer.group(0), r"\bat\s+(.+?)\s+elapsed\b")
        manifest.elapsed = _first_match(footer.group(0), r"\belapsed\s+(.+)$")
    if clean_footer:
        manifest.source_status = "clean_success"
    elif error_footer:
        manifest.source_status = "completed_with_errors"
        manifest.completion_error_count = int(error_footer.group(1))
    elif failed_footer:
        manifest.source_status = "failed"
    else:
        manifest.source_status = "incomplete"
        manifest.warnings.append("Export log has no final Data Pump job completion line.")

    if manifest.duplicate_tables:
        manifest.warnings.append(
            f"Parfile contains {len(manifest.duplicate_tables)} duplicate table entries."
        )
    if manifest.completion_error_count != len(manifest.errors):
        manifest.warnings.append(
            "Completion error count differs from the number of ORA/UDE/LRM lines parsed from the log."
        )
    if manifest.requested_tables and manifest.exported_tables:
        expected_missing = sorted(set(manifest.requested_tables) - set(manifest.exported_tables))
        if expected_missing and set(expected_missing) != set(manifest.missing_objects):
            manifest.warnings.append(
                "Requested/exported table reconciliation differs from explicit missing-object errors."
            )

    confidence = 0.35
    confidence += 0.15 if manifest.parameters else 0
    confidence += 0.15 if manifest.export_mode != "unknown" else 0
    confidence += 0.15 if manifest.dump_files else 0
    confidence += 0.1 if manifest.exported_tables else 0
    confidence += 0.1 if manifest.source_status in {"clean_success", "completed_with_errors"} else 0
    manifest.confidence = min(1.0, confidence)
    return manifest


def bind_export_log(
    manifest: OracleExportLogManifest,
    *,
    log_filename: str,
    actual_dump_files: list[str],
) -> ExportLogBinding:
    if not manifest.usable_for_assisted_import:
        return ExportLogBinding("unusable", 0, [f"source_status={manifest.source_status}"])

    actual = {PurePosixPath(name).name.lower() for name in actual_dump_files}
    declared = {PurePosixPath(name).name.lower() for name in manifest.dump_files}
    reasons: list[str] = []
    score = 0
    missing = sorted(declared - actual)
    unexpected = sorted(actual - declared) if declared else []
    if declared and not missing and not unexpected:
        score += 80
        reasons.append("Dump file set exactly matches the export log footer.")
    elif declared:
        reasons.append("Dump file set differs from the export log footer.")

    if manifest.dumpfile_pattern and actual:
        pattern = manifest.dumpfile_pattern.replace("%U", "*").lower()
        if all(fnmatch.fnmatchcase(name, pattern) for name in actual):
            score += 15
            reasons.append("All selected DMP files match the logged DUMPFILE pattern.")

    if manifest.logfile_name and PurePosixPath(log_filename).name.lower() == manifest.logfile_name.lower():
        score += 5
        reasons.append("Log filename matches the logged LOGFILE parameter.")

    if declared and (missing or unexpected):
        return ExportLogBinding("mismatch", score, reasons, missing, unexpected)
    if score >= 95:
        return ExportLogBinding("exact", score, reasons)
    if score >= 80:
        return ExportLogBinding("strong", score, reasons)
    return ExportLogBinding("weak", score, reasons)


def _parse_parfile_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, list[str]] = {}
    current_key = ""
    for line in lines:
        match = _PARAM_LINE.match(line)
        if not match:
            continue
        content = match.group(1).strip()
        start = _PARAM_START.match(content)
        if start:
            current_key = start.group(1).lower()
            values.setdefault(current_key, []).append(start.group(2).strip())
        elif current_key:
            values[current_key].append(content)
    return {key: "".join(parts).strip() for key, parts in values.items()}


def _export_mode(parameters: dict[str, str]) -> str:
    if parameters.get("transport_tablespaces") or parameters.get("transport_full_check", "").upper() == "Y":
        return "transportable"
    if parameters.get("tables"):
        return "tables"
    if parameters.get("tablespaces"):
        return "tablespaces"
    if parameters.get("schemas"):
        return "schemas"
    if parameters.get("full", "").upper() in {"Y", "YES"}:
        return "full"
    return "unknown"


def _split_csv(value: str, *, qualified: bool = False) -> list[str]:
    result: list[str] = []
    for item in value.split(","):
        item = item.strip().strip("'\"")
        if not item:
            continue
        if qualified and "." in item:
            schema, table = item.split(".", 1)
            if _SAFE_IDENTIFIER.fullmatch(schema) and _SAFE_IDENTIFIER.fullmatch(table):
                result.append(_qualified_name(schema, table))
            continue
        result.append(item.upper())
    return result


def _schemas_from_tables(tables: list[str]) -> list[str]:
    schemas: list[str] = []
    for table in tables:
        if "." not in table:
            continue
        schema = table.split(".", 1)[0].strip('"').upper()
        if _SAFE_IDENTIFIER.fullmatch(schema) and schema not in schemas:
            schemas.append(schema)
    return sorted(schemas)


def _qualified_name(schema: str, table: str) -> str:
    return f"{schema.strip().upper()}.{table.strip().upper()}"


def _size_to_bytes(value: str, unit: str) -> int:
    try:
        return int(Decimal(value) * _SIZE_FACTORS[unit.upper()])
    except (InvalidOperation, KeyError):
        return 0


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.upper()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
