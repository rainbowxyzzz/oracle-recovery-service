import re

from recovery_service.core.domain import ExportMetadata
from recovery_service.core.enums import ImportMode

_VERSION_RE = re.compile(r"Version\s+(\d+\.\d+\.\d+\.\d+)", re.I)
_SCHEMAS_RE = re.compile(r"SCHEMAS?\s*[=:]\s*([\w,\s]+)", re.I)
_TABLES_RE = re.compile(r"TABLES?\s*[=:]\s*([\w\.,\s]+)", re.I)
_FULL_RE = re.compile(r"\bFULL\s*[=:]\s*Y\b", re.I)
_EXPORT_MODE_RE = re.compile(r"Export\s+mode\s*:\s*(\w+)", re.I)


def parse_export_log(text: str) -> ExportMetadata | None:
    if not text or len(text) < 20:
        return None

    meta = ExportMetadata(discovery_source="log", confidence=0.85)
    m = _VERSION_RE.search(text)
    if m:
        meta.source_version = m.group(1)

    if _FULL_RE.search(text):
        meta.export_mode = ImportMode.FULL
    else:
        em = _EXPORT_MODE_RE.search(text)
        if em:
            mode = em.group(1).upper()
            if "SCHEMA" in mode:
                meta.export_mode = ImportMode.SCHEMA
            elif "TABLE" in mode:
                meta.export_mode = ImportMode.TABLE
            elif "FULL" in mode:
                meta.export_mode = ImportMode.FULL

    sm = _SCHEMAS_RE.search(text)
    if sm:
        meta.schemas = [s.strip().upper() for s in sm.group(1).split(",") if s.strip()]
        if meta.export_mode == ImportMode.UNKNOWN:
            meta.export_mode = ImportMode.SCHEMA

    tm = _TABLES_RE.search(text)
    if tm:
        meta.tables = [t.strip() for t in tm.group(1).split(",") if t.strip()]
        if meta.export_mode == ImportMode.UNKNOWN:
            meta.export_mode = ImportMode.TABLE

    if meta.source_version or meta.export_mode != ImportMode.UNKNOWN:
        return meta
    return None
