from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from recovery_service.common.time import app_now
from recovery_service.core.models.task import OpenMetadataSyncOutbox
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services import data_automation
from recovery_service.settings import get_settings

logger = logging.getLogger(__name__)


_OPENMETADATA_SERVICE_TYPES = {
    "bigquery": "BigQuery",
    "clickhouse": "Clickhouse",
    "doris": "Doris",
    "mysql": "Mysql",
    "oracle": "Oracle",
    "postgres": "Postgres",
    "postgresql": "Postgres",
    "sqlserver": "Mssql",
    "mssql": "Mssql",
}

_OPENMETADATA_DATA_TYPES = {
    "array": "ARRAY",
    "bigint": "BIGINT",
    "binary": "BINARY",
    "bit": "BIT",
    "blob": "BLOB",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "char": "CHAR",
    "clob": "CLOB",
    "date": "DATE",
    "datetime": "DATETIME",
    "datetime64": "DATETIME",
    "datetimev2": "DATETIME",
    "decimal": "DECIMAL",
    "double": "DOUBLE",
    "float": "FLOAT",
    "geometry": "GEOMETRY",
    "int": "INT",
    "int64": "BIGINT",
    "integer": "INT",
    "json": "JSON",
    "long": "LONG",
    "largeint": "BIGINT",
    "map": "MAP",
    "number": "NUMBER",
    "numeric": "NUMERIC",
    "smallint": "SMALLINT",
    "string": "STRING",
    "text": "TEXT",
    "time": "TIME",
    "timestamp": "TIMESTAMP",
    "timestampz": "TIMESTAMPZ",
    "tinyint": "TINYINT",
    "uuid": "UUID",
    "varbinary": "VARBINARY",
    "varchar": "VARCHAR",
}


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
    if configured and settings.openmetadata_sync_enabled:
        message = "OpenMetadata 地址已配置，目录入口和 API Bridge 已就绪。"
    elif configured:
        message = "OpenMetadata 地址已配置，但同步开关未开启。"
    else:
        message = "未部署或未配置 OpenMetadata；请先部署 OpenMetadata Server，再设置 OPENMETADATA_URL 和 OPENMETADATA_API_URL。"
    return {
        "configured": configured,
        "enabled": bool(settings.openmetadata_sync_enabled),
        "sync_ready": configured and bool(settings.openmetadata_sync_enabled),
        "ui_url": ui_url or None,
        "api_url": api_url or None,
        "producer": settings.openmetadata_producer,
        "message": message,
        "configuration": {
            "ui_env": "OPENMETADATA_URL",
            "api_env": "OPENMETADATA_API_URL",
            "token_env": "OPENMETADATA_API_TOKEN",
            "sync_env": "OPENMETADATA_SYNC_ENABLED",
            "default_api_suffix": "/api",
        },
    }


def enqueue_snapshot(
    *,
    search: str | None = None,
    layer: str | None = None,
    database_key: str | None = None,
    batch_id: Any = None,
    limit: int = 500,
) -> dict[str, Any] | None:
    """Persist and dispatch a best-effort snapshot sync after a business commit."""
    if not status()["sync_ready"]:
        return None
    scope = {
        "search": search,
        "layer": layer,
        "database_key": database_key,
        "batch_id": str(batch_id) if batch_id else None,
        "limit": limit,
    }
    scope = {key: value for key, value in scope.items() if value is not None}
    bucket = int(app_now().timestamp() // 300)
    key_material = json.dumps({"scope": scope, "bucket": bucket}, ensure_ascii=False, sort_keys=True)
    idempotency_key = f"snapshot:{hashlib.sha256(key_material.encode('utf-8')).hexdigest()}"
    session = get_sync_session_factory()()
    try:
        row = session.scalar(select(OpenMetadataSyncOutbox).where(OpenMetadataSyncOutbox.idempotency_key == idempotency_key))
        should_dispatch = row is None
        if row is None:
            row = OpenMetadataSyncOutbox(idempotency_key=idempotency_key, scope=scope, status="pending", next_attempt_at=app_now())
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                row = session.scalar(select(OpenMetadataSyncOutbox).where(OpenMetadataSyncOutbox.idempotency_key == idempotency_key))
        if row is None:
            return None
        outbox_id = str(row.id)
        should_dispatch = should_dispatch or row.status in {"failed", "deferred"} or bool(row.last_error)
        try:
            if not should_dispatch:
                return {"outbox_id": outbox_id, "status": row.status}
            from recovery_service.workers.celery_app import celery_app

            celery_app.send_task(
                "openmetadata.sync_outbox",
                args=[outbox_id],
                queue=get_settings().celery_data_platform_queue,
            )
        except Exception as exc:  # noqa: BLE001 - dispatch failure must not block the business transaction
            row.last_error = f"派发 OpenMetadata outbox 失败：{str(exc)[:500]}"
            row.status = "pending"
            session.commit()
            logger.warning("OpenMetadata outbox dispatch failed: %s", exc)
        return {"outbox_id": outbox_id, "status": row.status}
    finally:
        session.close()


def run_outbox_item(outbox_id: Any) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        row = session.get(OpenMetadataSyncOutbox, uuid.UUID(str(outbox_id)))
        if row is None:
            return {"status": "missing", "outbox_id": str(outbox_id), "retry": False}
        if row.status == "succeeded":
            return {"status": "succeeded", "outbox_id": str(row.id), "retry": False}
        row.status = "running"
        row.attempts += 1
        row.last_started_at = app_now()
        row.updated_at = app_now()
        session.commit()
        try:
            result = sync_snapshot(**(row.scope or {}))
        except Exception as exc:  # noqa: BLE001 - external OpenMetadata failures are retried by the outbox
            result = {"status": "failed", "message": str(exc)[:500], "summary": {}}
        if result.get("status") == "success":
            row.status = "succeeded"
            row.completed_at = app_now()
            row.next_attempt_at = None
            row.last_error = None
            retry = False
        elif result.get("status") in {"disabled", "not_configured"}:
            row.status = "deferred"
            row.last_error = str(result.get("message") or "OpenMetadata 尚未就绪")[:500]
            row.next_attempt_at = None
            retry = False
        else:
            retry = row.attempts < 5
            row.status = "pending" if retry else "failed"
            row.last_error = str(result.get("message") or result.get("failures") or "同步存在失败项")[:500]
            row.next_attempt_at = app_now() + timedelta(minutes=min(30, 2 ** min(row.attempts, 4))) if retry else None
        row.updated_at = app_now()
        session.commit()
        return {**result, "outbox_id": str(row.id), "attempts": row.attempts, "retry": retry}
    finally:
        session.close()


def _headers() -> dict[str, str]:
    token = get_settings().openmetadata_api_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _custom_properties(entity: dict[str, Any]) -> dict[str, str]:
    values = entity.get("customProperties") or {}
    return {
        str(key): str(value)
        for key, value in values.items()
        if value is not None and str(value).strip()
    }


def _native_data_type(value: Any) -> str:
    raw = str(value or "UNKNOWN").strip()
    normalized = raw.split("(", 1)[0].strip().casefold()
    if normalized in _OPENMETADATA_DATA_TYPES:
        return _OPENMETADATA_DATA_TYPES[normalized]
    if normalized.startswith("varchar"):
        return "VARCHAR"
    if normalized.startswith("timestamp"):
        return "TIMESTAMP"
    if normalized.startswith("number"):
        return "NUMBER"
    if normalized.startswith("decimal"):
        return "DECIMAL"
    return "STRING"


def _native_hierarchy(entity: dict[str, Any]) -> dict[str, str]:
    schema_fqn = str(entity.get("openMetadataDatabaseSchema") or "").strip().casefold()
    parts = [part for part in schema_fqn.split(".") if part]
    if len(parts) < 2:
        raise ValueError("OpenMetadata database schema FQN 至少需要 service.database.schema")
    service_name = parts[0]
    database_name = parts[1]
    schema_name = ".".join(parts[2:]) or "default"
    return {
        "service": service_name,
        "service_type": _OPENMETADATA_SERVICE_TYPES.get(str(entity.get("serviceType") or "").casefold(), "CustomDatabase"),
        "database": database_name,
        "schema": schema_name,
        "database_fqn": f"{service_name}.{database_name}",
        "schema_fqn": f"{service_name}.{database_name}.{schema_name}",
    }


def _table_payload(entity: dict[str, Any]) -> dict[str, Any]:
    hierarchy = _native_hierarchy(entity)
    columns = []
    for column in entity.get("columns") or []:
        item = {
            "name": column.get("name"),
            "dataType": _native_data_type(column.get("dataType") or column.get("data_type")),
            "dataTypeDisplay": str(column.get("dataType") or column.get("data_type") or "UNKNOWN"),
        }
        type_display = item["dataTypeDisplay"]
        type_match = re.search(r"\((\d+)(?:\s*,\s*(\d+))?\)", type_display)
        if type_match and item["dataType"] in {"CHAR", "VARCHAR", "VARBINARY", "BINARY"}:
            item["dataLength"] = int(type_match.group(1))
        if type_match and item["dataType"] in {"NUMBER", "DECIMAL", "NUMERIC"}:
            item["precision"] = int(type_match.group(1))
            if type_match.group(2):
                item["scale"] = int(type_match.group(2))
        if column.get("constraint"):
            item["constraint"] = column["constraint"]
        elif column.get("required"):
            item["constraint"] = "NOT_NULL"
        columns.append({key: value for key, value in item.items() if value not in (None, "")})
    payload: dict[str, Any] = {
        "name": entity.get("name"),
        "displayName": entity.get("displayName") or entity.get("name"),
        "databaseSchema": hierarchy["schema_fqn"],
        "tableType": "Regular",
        "columns": columns,
        "tags": [
            {
                **tag,
                "labelType": tag.get("labelType") or "Automated",
                "state": tag.get("state") or "Confirmed",
            }
            for tag in entity.get("tags") or []
        ],
    }
    properties = _custom_properties(entity)
    if properties:
        context = "；".join(f"{key}={value}" for key, value in properties.items())
        payload["description"] = f"当前系统血缘资产；{context}"
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _ensure_native_tags(client: httpx.Client, entities: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for entity in entities:
        for tag in entity.get("tags") or []:
            fqn = str(tag.get("tagFQN") or "").strip()
            if not fqn or fqn in seen:
                continue
            seen.add(fqn)
            classification, separator, name = fqn.partition(".")
            if not separator or not name:
                continue
            client.put(
                "/v1/classifications",
                json={
                    "name": classification,
                    "displayName": classification,
                    "description": "由当前系统血缘桥接维护的数据分类",
                },
            ).raise_for_status()
            client.put(
                "/v1/tags",
                json={
                    "name": name,
                    "displayName": name,
                    "description": "由当前系统血缘桥接维护的数据分类标签",
                    "classification": classification,
                },
            ).raise_for_status()


def _ensure_native_hierarchy(client: httpx.Client, entities: list[dict[str, Any]]) -> set[str]:
    ready: set[str] = set()
    seen: set[str] = set()
    for entity in entities:
        hierarchy = _native_hierarchy(entity)
        if hierarchy["schema_fqn"] in seen:
            continue
        seen.add(hierarchy["schema_fqn"])
        client.put(
            "/v1/services/databaseServices",
            json={
                "name": hierarchy["service"],
                "displayName": hierarchy["service"],
                "serviceType": hierarchy["service_type"],
                "description": "由当前系统血缘桥接维护的 OpenMetadata 数据服务",
            },
        ).raise_for_status()
        client.put(
            "/v1/databases",
            json={
                "name": hierarchy["database"],
                "displayName": hierarchy["database"],
                "service": hierarchy["service"],
                "description": "由当前系统血缘桥接维护的 OpenMetadata 数据库",
            },
        ).raise_for_status()
        client.put(
            "/v1/databaseSchemas",
            json={
                "name": hierarchy["schema"],
                "displayName": hierarchy["schema"],
                "database": hierarchy["database_fqn"],
                "description": "由当前系统血缘桥接维护的 OpenMetadata 数据库 Schema",
            },
        ).raise_for_status()
        ready.add(hierarchy["schema_fqn"])
    return ready


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
        try:
            _ensure_native_tags(client, entities)
            ready_schemas = _ensure_native_hierarchy(client, entities)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            summary["failed"] += len(entities)
            failures.append({"kind": "hierarchy", "error": str(exc)[:300]})
            ready_schemas = set()
        for entity in entities:
            try:
                if _native_hierarchy(entity)["schema_fqn"] not in ready_schemas:
                    raise ValueError("未能创建 OpenMetadata service/database/schema 层级")
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
