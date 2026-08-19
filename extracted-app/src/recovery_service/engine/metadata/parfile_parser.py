import re

from recovery_service.core.domain import ExportMetadata
from recovery_service.core.enums import ImportMode


def parse_parfile(text: str) -> ExportMetadata | None:
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    meta = ExportMetadata(discovery_source="parfile", confidence=0.8, raw_parfile_lines=lines)
    kv = _parse_key_values(lines)

    if kv.get("FULL", "").upper() == "Y":
        meta.export_mode = ImportMode.FULL
    if "SCHEMAS" in kv:
        meta.schemas = [s.strip().upper() for s in kv["SCHEMAS"].split(",")]
        meta.export_mode = ImportMode.SCHEMA
    if "TABLES" in kv:
        meta.tables = [t.strip() for t in kv["TABLES"].split(",")]
        meta.export_mode = ImportMode.TABLE
    if "DUMPFILE" in kv:
        meta.dumpfile_param = kv["DUMPFILE"]
    if "LOGFILE" in kv:
        meta.logfile_param = kv["LOGFILE"]
    if "VERSION" in kv:
        meta.source_version = kv["VERSION"]

    if meta.export_mode != ImportMode.UNKNOWN or meta.dumpfile_param:
        return meta
    return None


def _parse_key_values(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip().upper()] = val.strip()
    return result


def extract_tablespaces_from_par(text: str) -> list[str]:
    spaces = re.findall(r"TABLESPACE\s+(\w+)", text, re.I)
    return list({s.upper() for s in spaces})
