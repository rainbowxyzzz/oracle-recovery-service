from __future__ import annotations

import re

_REPLICATION_PROPERTY_RE = re.compile(
    r"(?i)(?:[\"`]?replication_(?:allocation|num)[\"`]?\s*=\s*)(?P<quote>[\"'])(?:[^\"']*)(?P=quote)"
)
_PROPERTIES_OPEN_RE = re.compile(r"(?i)\bPROPERTIES\s*\(")
_ALLOCATION_ITEM_RE = re.compile(r"^tag\.location\.[A-Za-z0-9_.-]+\s*:\s*[1-9][0-9]*$")


def configured_encryption_replication_allocation() -> str:
    from recovery_service.settings import get_settings

    return normalize_replication_allocation(get_settings().doris_encryption_replication_allocation)


def normalize_replication_allocation(value: str) -> str:
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    if not items or any(not _ALLOCATION_ITEM_RE.fullmatch(item) for item in items):
        raise ValueError(
            "DORIS_ENCRYPTION_REPLICATION_ALLOCATION 格式无效，"
            "示例：tag.location.default: 1"
        )
    return ", ".join(
        f"{key.strip()}: {int(count.strip())}"
        for key, count in (item.split(":", 1) for item in items)
    )


def rewrite_table_replication_allocation(ddl: str, allocation: str | None = None) -> str:
    target = normalize_replication_allocation(
        allocation if allocation is not None else configured_encryption_replication_allocation()
    )
    replacement = f'"replication_allocation" = "{target}"'
    updated, count = _REPLICATION_PROPERTY_RE.subn(replacement, ddl)
    if count:
        return updated

    properties_match = _PROPERTIES_OPEN_RE.search(ddl)
    if properties_match:
        body_start = properties_match.end()
        next_non_space = re.search(r"\S", ddl[body_start:])
        has_existing_property = bool(next_non_space and ddl[body_start + next_non_space.start()] != ")")
        comma = "," if has_existing_property else ""
        return ddl[:body_start] + f'\n  {replacement}{comma}' + ddl[body_start:]

    stripped = ddl.rstrip()
    has_semicolon = stripped.endswith(";")
    if has_semicolon:
        stripped = stripped[:-1].rstrip()
    suffix = ";" if has_semicolon else ""
    return f"{stripped}\nPROPERTIES (\n  {replacement}\n){suffix}"
