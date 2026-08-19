import re
from dataclasses import dataclass, field

from recovery_service.core.enums import ImportMode


@dataclass(frozen=True)
class OracleImportMetadata:
    export_mode: ImportMode = ImportMode.UNKNOWN
    source_schemas: list[str] = field(default_factory=list)
    source_tablespaces: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    dumpfiles_from_text: list[str] = field(default_factory=list)
    source_version: str | None = None
    transportable: bool = False
    evidence: list[str] = field(default_factory=list)


_IDENT = r"[A-Za-z][A-Za-z0-9_$#]*"


def analyze_oracle_metadata(*texts: str) -> OracleImportMetadata:
    text = "\n".join(t for t in texts if t)
    upper = text.upper()
    evidence: list[str] = []
    mode = ImportMode.UNKNOWN

    if re.search(r"\bTRANSPORTABLE\s*=\s*(Y|ALWAYS)\b", upper) or "TRANSPORTABLE TABLESPACE" in upper:
        mode = ImportMode.TRANSPORTABLE
        evidence.append("transportable marker")
    elif re.search(r"\bTABLESPACES?\s*=", upper):
        mode = ImportMode.TABLESPACE
        evidence.append("TABLESPACES parameter")
    elif re.search(r"\bFULL\s*=\s*Y\b", upper) or "FULL DATABASE" in upper:
        mode = ImportMode.FULL
        evidence.append("FULL=Y marker")
    elif re.search(r"\bSCHEMAS?\s*=", upper) or "EXPORT SCHEMA" in upper:
        mode = ImportMode.SCHEMA
        evidence.append("SCHEMAS marker")
    elif re.search(r"\bTABLES?\s*=", upper) or "CREATE TABLE" in upper:
        mode = ImportMode.TABLE
        evidence.append("TABLES/CREATE TABLE marker")

    schemas = _unique_upper(
        [
            *_split_csv_params(text, "SCHEMAS?"),
            *_regex_group(text, rf'CREATE\s+USER\s+"?({_IDENT})"?'),
            *_regex_group(text, rf'ALTER\s+SESSION\s+SET\s+CURRENT_SCHEMA\s*=\s*"?({_IDENT})"?'),
            *_regex_group(text, rf'REMAP_SCHEMA\s*=\s*"?({_IDENT})"?\s*:'),
            *_regex_group(text, rf'FROMUSER\s*=\s*"?({_IDENT})"?'),
            *_regex_group(text, rf'CONNECT\s+"?({_IDENT})"?/'),
            *_regex_group(text, rf'Processing\s+object\s+type\s+SCHEMA_EXPORT/[^/\s]+/"?({_IDENT})"?'),
        ]
    )
    tablespaces = _unique_upper(
        [
            *_split_csv_params(text, "TABLESPACES?"),
            *_regex_group(text, rf'\bTABLESPACE\s+"?({_IDENT})"?'),
            *_regex_group(text, rf'DEFAULT\s+TABLESPACE\s+"?({_IDENT})"?'),
            *_regex_group(text, rf'REMAP_TABLESPACE\s*=\s*"?({_IDENT})"?\s*:'),
        ]
    )
    tables = _unique_preserve(
        [
            *_split_csv_params(text, "TABLES?"),
            *_regex_group(text, rf'CREATE\s+TABLE\s+(?:"?{_IDENT}"?\.)?"?({_IDENT})"?'),
        ]
    )
    dumpfiles = _unique_preserve(
        [
            *_split_csv_params(text, "DUMPFILE"),
            *_regex_group(text, r"(?:dump file|dumpfile)\s*[:=]\s*['\"]?([^'\"\s,;]+\.dmp)", flags=re.I),
        ]
    )
    source_version = _first_match(
        text,
        [
            r"Export:\s+Release\s+([0-9.]+)",
            r"Import:\s+Release\s+([0-9.]+)",
            r"Version\s+([0-9.]+)",
        ],
    )

    if mode == ImportMode.UNKNOWN and schemas:
        mode = ImportMode.SCHEMA
        evidence.append("schema names discovered")
    if mode == ImportMode.UNKNOWN and tables:
        mode = ImportMode.TABLE
        evidence.append("table names discovered")

    return OracleImportMetadata(
        export_mode=mode,
        source_schemas=schemas,
        source_tablespaces=tablespaces,
        tables=tables,
        dumpfiles_from_text=dumpfiles,
        source_version=source_version,
        transportable=mode == ImportMode.TRANSPORTABLE,
        evidence=evidence,
    )


def _split_csv_params(text: str, key_pattern: str) -> list[str]:
    values: list[str] = []
    pattern = re.compile(rf"\b{key_pattern}\s*=\s*([^\n\r]+)", re.I)
    for match in pattern.finditer(text):
        raw = match.group(1).strip().strip("'\"")
        raw = re.split(r"\s+(?:[A-Z_]+\s*=|$)", raw, maxsplit=1)[0]
        values.extend(v.strip().strip("'\"") for v in raw.split(",") if v.strip())
    return values


def _regex_group(text: str, pattern: str, *, flags: int = re.I) -> list[str]:
    return [m.group(1).strip().strip('"') for m in re.finditer(pattern, text, flags)]


def _first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return None


def _unique_upper(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _clean_identifier(value)
        if not normalized or normalized in seen or normalized.startswith("SYS_"):
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _unique_preserve(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip().strip('"')
        key = normalized.upper()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _clean_identifier(value: str) -> str:
    value = value.strip().strip('"').rstrip(";")
    if "." in value:
        value = value.split(".", 1)[0]
    return value.upper()
