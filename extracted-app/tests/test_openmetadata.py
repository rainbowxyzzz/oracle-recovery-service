from types import SimpleNamespace
from unittest.mock import patch

from recovery_service.services import openmetadata


def _settings(**overrides):
    values = {
        "openmetadata_url": "http://openmetadata.test:8585",
        "openmetadata_api_url": "",
        "openmetadata_api_token": "",
        "openmetadata_sync_enabled": True,
        "openmetadata_request_timeout_seconds": 20,
        "openmetadata_producer": "oracle-recovery-service",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_status_derives_api_url_from_native_ui_url() -> None:
    with patch.object(openmetadata, "get_settings", return_value=_settings()):
        result = openmetadata.status()
    assert result["configured"] is True
    assert result["sync_ready"] is True
    assert result["api_url"] == "http://openmetadata.test:8585/api"


def test_table_payload_uses_native_fqn_and_keeps_task_context_as_properties() -> None:
    payload = openmetadata._table_payload({
        "name": "customer",
        "displayName": "customer",
        "openMetadataFqn": "doris.hive.dwd.customer",
        "serviceName": "doris",
        "serviceType": "Doris",
        "openMetadataDatabaseSchema": "doris.hive.dwd",
        "columns": [{"name": "id", "dataType": "BIGINT"}],
        "tags": [],
        "customProperties": {"pipeline": "p-1", "layer": "standard"},
    })
    assert payload["fullyQualifiedName"] == "doris.hive.dwd.customer"
    assert payload["databaseSchema"] == "doris.hive.dwd"
    assert {item["name"] for item in payload["customProperties"]} == {"pipeline", "layer"}


def test_sync_snapshot_upserts_tables_and_lineage_without_persisting_credentials() -> None:
    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

    class Client:
        last_calls = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            Client.last_calls = self.calls

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def put(self, path, json):
            self.calls.append(("PUT", path, json))
            return Response({"id": json.get("fullyQualifiedName", "lineage-id")})

        def get(self, path):
            self.calls.append(("GET", path, None))
            return Response({"id": path.rsplit("/", 1)[-1]})

    entities = [
        {"name": "source", "fullyQualifiedName": "legacy.source", "openMetadataFqn": "doris.dwd.source", "columns": [], "customProperties": {}},
        {"name": "target", "fullyQualifiedName": "legacy.target", "openMetadataFqn": "doris.dwd.target", "columns": [], "customProperties": {}},
    ]
    projection = {
        "entities": entities,
        "relationships": [{"fromEntity": entities[0], "toEntity": entities[1], "lineageDetails": {"fields": [{"fromColumns": ["id"], "toColumn": "id", "transformer": "direct"}]}}],
        "truncated": False,
        "model": "openmetadata-table-lineage-v1",
    }
    with (
        patch.object(openmetadata, "get_settings", return_value=_settings(openmetadata_api_token="secret")),
        patch.object(openmetadata.httpx, "Client", Client),
        patch.object(openmetadata.data_automation, "openmetadata_lineage_projection", return_value=projection),
    ):
        result = openmetadata.sync_snapshot(limit=10)
    assert result["status"] == "success"
    assert result["summary"] == {"entities": 2, "lineage": 1, "failed": 0}
    assert Client.last_calls[0][1] == "/v1/tables"
    assert Client.last_calls[-1][1] == "/v1/lineage"
    assert Client.last_calls[-1][2]["edge"]["lineageDetails"]["columnsLineage"][0]["toColumn"] == "id"
    assert "secret" not in str(Client.last_calls)
