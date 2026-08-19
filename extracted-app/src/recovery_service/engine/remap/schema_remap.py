import re
from dataclasses import dataclass, field


SYSTEM_SCHEMAS = {
    "SYS",
    "SYSTEM",
    "XDB",
    "WMSYS",
    "CTXSYS",
    "MDSYS",
    "ORDSYS",
    "DBSNMP",
    "OUTLN",
    "AUDSYS",
    "GSMADMIN_INTERNAL",
    "ORDDATA",
    "OLAPSYS",
    "LBACSYS",
    "DVSYS",
    "APPQOSSYS",
}


@dataclass(frozen=True)
class SchemaRemapPlan:
    target_schema: str
    source_schemas: list[str] = field(default_factory=list)

    @property
    def remap_schemas(self) -> list[tuple[str, str]]:
        return [(schema, self.target_schema) for schema in self.source_schemas]

    def formatted(self) -> list[str]:
        return format_remap_schemas(self.remap_schemas)


def build_schema_remap_plan(text: str, *, target_schema: str) -> SchemaRemapPlan:
    return SchemaRemapPlan(
        target_schema=target_schema,
        source_schemas=extract_source_schemas(text, target_schema=target_schema),
    )


def extract_source_schemas(text: str, *, target_schema: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(re.findall(r'"([A-Za-z][A-Za-z0-9_$#]{0,127})"\s*\.', text))
    for match in re.findall(r"(?im)^\s*schemas?\s*=\s*([^\r\n]+)", text):
        candidates.extend(re.split(r"[\s,]+", match.strip()))
    for match in re.findall(r"(?i)SCHEMA(?:S)?[:=]\s*([A-Za-z][A-Za-z0-9_$#]{0,127})", text):
        candidates.append(match)

    target = target_schema.upper()
    result: list[str] = []
    for candidate in candidates:
        schema = candidate.strip().strip('"').upper()
        if not schema or schema == target or schema in SYSTEM_SCHEMAS:
            continue
        if schema not in result:
            result.append(schema)
    return result


def merge_remap_schemas(
    current: list[tuple[str, str]],
    discovered_sources: list[str],
    *,
    target_schema: str,
) -> list[tuple[str, str]]:
    merged = list(current)
    known = {source for source, _ in merged}
    for source in discovered_sources:
        schema = source.upper()
        if schema not in known:
            merged.append((schema, target_schema))
            known.add(schema)
    return merged


def format_remap_schemas(remap_schemas: list[tuple[str, str]]) -> list[str]:
    return [f"{source}:{target}" for source, target in remap_schemas]
