from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from recovery_service.services import data_automation
from recovery_service.settings import get_settings


def _base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _api_url() -> str:
    settings = get_settings()
    configured = _base_url(settings.openmetadata_api_url)
    if configured:
        return configured
    ui_url = _base_url(settings.openmetadata_url)
    return f"{ui_url}/api" if ui_url else ""


def status() -> dict[str, Any]:
    settings = get_settings()
    ui_url = _base_url(settings.openmetadata_url)
    api_url = _api_url()
    configured = bool(ui_url and api_url)
    return {
        "configured": configured,
        "enabled": bool(settings.openmetadata_sync_enabled),
        "sync_ready": configured and bool(settings.openmetadata_sync_enabled),
        "ui_url": ui_url or None,
        "api_url": api_url or None,
        "producer": settings.openmetadata_producer,
    }


def _headers() -> dict[str, str]:
    token = get_settings().openmetadata_api_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _custom_properties(entity: dict[str, Any]) -> list[dict[str, str]]:
    values = entity.get("customProperties") or {}
    return [
        {"name": str(key), "value": str(value)}
        for key, value in values.items()
        if value is not None and str(value).strip()
    ]


def _table_payload(entity: dict[str, Any]) -> dict[str, Any]:
    native_fqn = entity.get("openMetadataFqn") or entity.get("fullyQualifiedName")
    payload: dict[str, Any] = {
        "name": entity.get("name"),
        "displayName": entity.get("displayName") or entity.get("name"),
        "fullyQualifiedName": native_fqn,
        "service": entity.get("serviceName"),
        "serviceType": entity.get("serviceType"),
        "databaseSchema": entity.get("openMetadataDatabaseSchema") or entity.get("databaseSchema"),
        "tableType": "Regular",
        "columns": entity.get("columns") or [],
        "tags": entity.get("tags") or [],
    }
    properties = _custom_properties(entity)
    if properties:
        payload["customProperties"] = properties
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _lineage_payload(
    relationship: dict[str, Any],
    source_id: str,
    target_id: str,
) -> dict[str, Any]:
    details = relationship.get("lineageDetails") or {}
    fields = details.get("fields") or []
    lineage_details: dict[str, Any] = {}
    columns_lineage = []
    for field in fields:
        source_columns = field.get("fromColumns") or ([field.get("sourceField")] if field.get("sourceField") else [])
        target_column = field.get("toColumn") or field.get("targetField")
        if source_columns and target_column:
            transformer = field.get("transformer") or field.get("expression") or field.get("transformationType") or "direct"
            columns_lineage.append({
                "fromColumns": source_columns,
                "toColumn": target_column,
                "transformer": transformer if isinstance(transformer, dict) else {"type": "SQL", "code": str(transformer)},
            })
    if columns_lineage:
        lineage_details["columnsLineage"] = columns_lineage
    edge: dict[str, Any] = {
        "fromEntity": {"id": source_id, "type": "table"},
        "toEntity": {"id": target_id, "type": "table"},
    }
    if lineage_details:
        edge["lineageDetails"] = lineage_details
    return {"edge": edge}


def _response_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("message") if isinstance(body, dict) else None
        if message:
            return str(message)[:300]
    except ValueError:
        pass
    return response.text[:300]


def sync_snapshot(
    *,
    search: str | None = None,
    layer: str | None = None,
    database_key: str | None = None,
    batch_id: Any = None,
    limit: int = 500,
) -> dict[str, Any]:
    connection = status()
    if not connection["enabled"]:
        return {"status": "disabled", "message": "OpenMetadata 同步未启用", "summary": {}}
    if not connection["configured"]:
        return {"status": "not_configured", "message": "未配置 OpenMetadata 地址", "summary": {}}

    projection = data_automation.openmetadata_lineage_projection(
        search=search,
        layer=layer,
        database_key=database_key,
        batch_id=batch_id,
        limit=limit,
    )
    entities = projection.get("entities") or []
    relationships = projection.get("relationships") or []
    api_url = str(connection["api_url"])
    settings = get_settings()
    summary = {"entities": len(entities), "lineage": 0, "failed": 0}
    failures: list[dict[str, Any]] = []
    entity_ids: dict[str, str] = {}

    with httpx.Client(
        base_url=api_url,
        headers={"Content-Type": "application/json", **_headers()},
        timeout=settings.openmetadata_request_timeout_seconds,
    ) as client:
        for entity in entities:
            try:
                response = client.put("/v1/tables", json=_table_payload(entity))
                response.raise_for_status()
                result = response.json()
                entity_ids[entity.get("openMetadataFqn") or entity["fullyQualifiedName"]] = str(result.get("id"))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                summary["failed"] += 1
                failures.append({"kind": "table", "name": entity.get("name"), "error": str(exc)[:300]})

        for relationship in relationships:
            source = relationship.get("fromEntity") or {}
            target = relationship.get("toEntity") or {}
            source_fqn = source.get("openMetadataFqn") or source.get("fullyQualifiedName")
            target_fqn = target.get("openMetadataFqn") or target.get("fullyQualifiedName")
            try:
                source_id = entity_ids.get(source_fqn)
                target_id = entity_ids.get(target_fqn)
                if not source_id or not target_id:
                    for fqn in (source_fqn, target_fqn):
                        if fqn and fqn not in entity_ids:
                            lookup = client.get(f"/v1/tables/name/{quote(str(fqn), safe='')}")
                            lookup.raise_for_status()
                            entity_ids[fqn] = str(lookup.json()["id"])
                    source_id = entity_ids.get(source_fqn)
                    target_id = entity_ids.get(target_fqn)
                if not source_id or not target_id:
                    raise ValueError("未找到血缘两端的 OpenMetadata table id")
                response = client.put(
                    "/v1/lineage",
                    json=_lineage_payload(relationship, source_id, target_id),
                )
                response.raise_for_status()
                summary["lineage"] += 1
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                summary["failed"] += 1
                failures.append({"kind": "lineage", "error": str(exc)[:300]})

    return {
        "status": "success" if not failures else "partial_success",
        "summary": summary,
        "failures": failures[:50],
        "projection": {
            "truncated": projection.get("truncated", False),
            "model": projection.get("model"),
        },
    }
