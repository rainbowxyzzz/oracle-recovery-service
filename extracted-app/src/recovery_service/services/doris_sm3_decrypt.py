from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from recovery_service.api.schemas.doris_sm3_decrypt import (
    DorisSm3DecryptItem,
    DorisSm3DecryptRequest,
    DorisSm3DecryptResponse,
    DorisSm3DecryptResult,
    DorisSm3MappingSource,
)
from recovery_service.common.security import decrypt_secret
from recovery_service.core.models.task import DatabaseConnectionProfile
from recovery_service.settings import get_settings

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")
_DEFAULT_FIELD_MAPPING_TABLE = "doris_mask_field_mappings"
_DEFAULT_ORIGINAL_COLUMN = "original_value"
_DEFAULT_ENCRYPTED_COLUMN = "sm3_value"
_MAX_BATCH_SIZE = 1000
_QUERY_CHUNK_SIZE = 200

_FIELD_CATEGORY_ALIASES = {
    "\u59d3\u540d": [
        "\u59d3\u540d",
        "\u540d\u5b57",
        "\u540d\u79f0",
        "name",
        "user_name",
        "username",
        "real_name",
        "full_name",
    ],
    "\u624b\u673a\u53f7": [
        "\u624b\u673a\u53f7",
        "\u624b\u673a\u53f7\u7801",
        "\u624b\u673a",
        "mobile",
        "mobile_phone",
        "phone",
        "phone_number",
        "tel",
    ],
    "\u7535\u8bdd": ["\u7535\u8bdd", "\u8054\u7cfb\u7535\u8bdd", "phone", "telephone", "tel"],
    "\u8bc1\u4ef6\u53f7": [
        "\u8bc1\u4ef6\u53f7",
        "\u8eab\u4efd\u8bc1",
        "\u8eab\u4efd\u8bc1\u53f7",
        "\u8bc1\u4ef6\u53f7\u7801",
        "id_card",
        "idcard",
        "card_no",
        "cert_no",
        "certificate_no",
    ],
    "\u4f4f\u5740": ["\u4f4f\u5740", "\u5730\u5740", "address", "addr", "home_address"],
    "\u5730\u5740": ["\u5730\u5740", "\u4f4f\u5740", "address", "addr", "home_address"],
}


@dataclass(frozen=True)
class _MappingSource:
    mapping_database: str
    mapping_table: str
    original_column: str = _DEFAULT_ORIGINAL_COLUMN
    encrypted_column: str = _DEFAULT_ENCRYPTED_COLUMN
    source_database: str | None = None
    source_table: str | None = None
    source_column: str | None = None
    masked_database: str | None = None
    masked_table: str | None = None
    masked_column: str | None = None
    updated_at: str | None = None


def decrypt_sm3_by_mapping(
    profile: DatabaseConnectionProfile,
    body: DorisSm3DecryptRequest,
) -> DorisSm3DecryptResponse:
    if profile.engine != "doris":
        raise ValueError("connection_id must point to a Doris connection")

    items = _normalize_items(body)
    if len(items) > _MAX_BATCH_SIZE:
        raise ValueError(f"batch size cannot exceed {_MAX_BATCH_SIZE}")

    with _doris_conn(profile, None) as conn:
        with conn.cursor() as cur:
            sources = _resolve_mapping_sources(cur, profile, body)
            lookup = _load_mapping_values(cur, sources, [item.encrypted_value for item in items])

    results: list[DorisSm3DecryptResult] = []
    found_count = 0
    ambiguous_count = 0
    for index, item in enumerate(items):
        matches = lookup.get(item.encrypted_value, [])
        if not matches:
            results.append(
                DorisSm3DecryptResult(
                    index=index,
                    encrypted_value=item.encrypted_value,
                    client_ref=item.client_ref,
                    found=False,
                )
            )
            continue

        unique_originals = sorted({str(match["original_value"]) for match in matches})
        source = matches[0]["source"]
        ambiguous = len(unique_originals) > 1
        if ambiguous:
            ambiguous_count += 1
        else:
            found_count += 1
        results.append(
            DorisSm3DecryptResult(
                index=index,
                encrypted_value=item.encrypted_value,
                original_value=unique_originals[0] if not ambiguous else None,
                found=not ambiguous,
                ambiguous=ambiguous,
                client_ref=item.client_ref,
                mapping_database=source.mapping_database,
                mapping_table=source.mapping_table,
                error="multiple original values matched" if ambiguous else None,
            )
        )

    warnings = []
    if len(sources) > 1:
        warnings.append("Multiple mapping tables matched the field category; values were searched across all matched tables.")
    return DorisSm3DecryptResponse(
        field_category=body.field_category,
        total=len(items),
        found=found_count,
        not_found=len(items) - found_count - ambiguous_count,
        ambiguous=ambiguous_count,
        mapping_sources=[_source_response(source) for source in sources],
        results=results,
        warnings=warnings,
        metadata={
            "mode": "direct_mapping_table" if body.mapping_table else "field_mapping_lookup",
            "algorithm": "SM3",
        },
    )


def _normalize_items(body: DorisSm3DecryptRequest) -> list[DorisSm3DecryptItem]:
    if body.items:
        return body.items
    return [DorisSm3DecryptItem(encrypted_value=value) for value in body.encrypted_values]


def _resolve_mapping_sources(cur, profile: DatabaseConnectionProfile, body: DorisSm3DecryptRequest) -> list[_MappingSource]:
    if body.mapping_table:
        mapping_database = _clean_identifier(body.mapping_database or profile.database, "mapping_database")
        mapping_table = _clean_identifier(body.mapping_table, "mapping_table")
        _ensure_table_exists(cur, mapping_database, mapping_table)
        return [_MappingSource(mapping_database=mapping_database, mapping_table=mapping_table)]

    field_mapping_database = _clean_identifier(
        body.field_mapping_database or body.mapping_database or profile.database,
        "field_mapping_database",
    )
    field_mapping_table = _clean_identifier(body.field_mapping_table or _DEFAULT_FIELD_MAPPING_TABLE, "field_mapping_table")
    _ensure_table_exists(cur, field_mapping_database, field_mapping_table)

    aliases = _field_aliases(body.field_category, body.field_aliases)
    where = [
        "`algorithm` = 'SM3'",
        "`mapping_database` IS NOT NULL",
        "`mapping_table_name` IS NOT NULL",
        "`mapping_table_name` <> ''",
        "(`source_column_name` IN ({aliases}) OR `masked_column_name` IN ({aliases}) OR `mapping_table_name` IN ({aliases}))",
    ]
    params: list[Any] = []
    alias_placeholders = ", ".join(["%s"] * len(aliases))
    where[-1] = where[-1].format(aliases=alias_placeholders)
    params.extend(aliases)
    params.extend(aliases)
    params.extend(aliases)

    _add_optional_filter(where, params, "source_database", body.source_database)
    _add_optional_filter(where, params, "source_table_name", body.source_table)
    _add_optional_filter(where, params, "masked_database", body.masked_database)
    _add_optional_filter(where, params, "masked_table_name", body.masked_table)

    sql = f"""
SELECT DISTINCT
  `mapping_database`,
  `mapping_table_name`,
  COALESCE(NULLIF(`mapping_original_column`, ''), %s) AS mapping_original_column,
  COALESCE(NULLIF(`mapping_masked_column`, ''), %s) AS mapping_masked_column,
  `source_database`,
  `source_table_name`,
  `source_column_name`,
  `masked_database`,
  `masked_table_name`,
  `masked_column_name`,
  CAST(`updated_at` AS CHAR) AS updated_at
FROM {_q(field_mapping_database)}.{_q(field_mapping_table)}
WHERE {" AND ".join(where)}
ORDER BY `updated_at` DESC
""".strip()
    cur.execute(sql, [_DEFAULT_ORIGINAL_COLUMN, _DEFAULT_ENCRYPTED_COLUMN, *params])
    sources = []
    seen = set()
    for row in cur.fetchall():
        source = _MappingSource(
            mapping_database=str(row["mapping_database"]),
            mapping_table=str(row["mapping_table_name"]),
            original_column=str(row["mapping_original_column"] or _DEFAULT_ORIGINAL_COLUMN),
            encrypted_column=str(row["mapping_masked_column"] or _DEFAULT_ENCRYPTED_COLUMN),
            source_database=_optional_str(row.get("source_database")),
            source_table=_optional_str(row.get("source_table_name")),
            source_column=_optional_str(row.get("source_column_name")),
            masked_database=_optional_str(row.get("masked_database")),
            masked_table=_optional_str(row.get("masked_table_name")),
            masked_column=_optional_str(row.get("masked_column_name")),
            updated_at=_optional_str(row.get("updated_at")),
        )
        key = (source.mapping_database, source.mapping_table, source.original_column, source.encrypted_column)
        if key in seen:
            continue
        seen.add(key)
        _validate_mapping_source(source)
        _ensure_table_exists(cur, source.mapping_database, source.mapping_table)
        sources.append(source)

    if not sources:
        raise ValueError("No SM3 mapping table matched the requested field category. Specify mapping_database and mapping_table directly, or check the field mapping table.")
    return sources


def _load_mapping_values(cur, sources: list[_MappingSource], encrypted_values: list[str]) -> dict[str, list[dict[str, Any]]]:
    unique_values = list(dict.fromkeys(encrypted_values))
    lookup: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        for chunk in _chunks(unique_values, _QUERY_CHUNK_SIZE):
            placeholders = ", ".join(["%s"] * len(chunk))
            sql = (
                f"SELECT CAST({_q(source.encrypted_column)} AS CHAR) AS encrypted_value, "
                f"CAST({_q(source.original_column)} AS CHAR) AS original_value "
                f"FROM {_q(source.mapping_database)}.{_q(source.mapping_table)} "
                f"WHERE {_q(source.encrypted_column)} IN ({placeholders})"
            )
            cur.execute(sql, chunk)
            for row in cur.fetchall():
                encrypted_value = str(row["encrypted_value"])
                original_value = row.get("original_value")
                lookup.setdefault(encrypted_value, []).append({"original_value": original_value, "source": source})
    return lookup


def _doris_conn(profile: DatabaseConnectionProfile, database: str | None):
    return pymysql.connect(
        host=profile.host,
        port=profile.port or 9030,
        user=profile.username,
        password=decrypt_secret(profile.password_enc, get_settings().credential_encryption_key),
        database=database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _field_aliases(field_category: str, request_aliases: list[str]) -> list[str]:
    values = [field_category.strip()]
    values.extend(_FIELD_CATEGORY_ALIASES.get(field_category.strip(), []))
    values.extend(alias.strip() for alias in request_aliases)
    return [value for value in dict.fromkeys(values) if value]


def _add_optional_filter(where: list[str], params: list[Any], column: str, value: str | None) -> None:
    if value:
        where.append(f"`{column}` = %s")
        params.append(value.strip())


def _clean_identifier(value: str | None, label: str) -> str:
    clean = (value or "").strip()
    if not clean:
        raise ValueError(f"{label} is required")
    if not _IDENT_RE.match(clean):
        raise ValueError(f"{label} contains unsupported characters")
    return clean


def _validate_mapping_source(source: _MappingSource) -> None:
    _clean_identifier(source.mapping_database, "mapping_database")
    _clean_identifier(source.mapping_table, "mapping_table")
    _clean_identifier(source.original_column, "mapping_original_column")
    _clean_identifier(source.encrypted_column, "mapping_masked_column")


def _ensure_table_exists(cur, database: str, table_name: str) -> None:
    cur.execute(
        """
        SELECT COUNT(*) AS total
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (database, table_name),
    )
    row = cur.fetchone() or {}
    if int(row.get("total") or 0) <= 0:
        raise ValueError(f"Doris table does not exist: {database}.{table_name}")


def _source_response(source: _MappingSource) -> DorisSm3MappingSource:
    return DorisSm3MappingSource(
        mapping_database=source.mapping_database,
        mapping_table=source.mapping_table,
        original_column=source.original_column,
        encrypted_column=source.encrypted_column,
        source_database=source.source_database,
        source_table=source.source_table,
        source_column=source.source_column,
        masked_database=source.masked_database,
        masked_table=source.masked_table,
        masked_column=source.masked_column,
        updated_at=source.updated_at,
    )


def _chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _q(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"
