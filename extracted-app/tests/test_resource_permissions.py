import json
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recovery_service.api.schemas.resource_provisioning import (
    RESOURCE_PERMISSION_OPTIONS,
    ResourcePermissionBatchCreateRequest,
)
from recovery_service.core.models.task import (
    Base,
    ResourcePermissionBatch,
    ResourcePermissionRow,
    ResourcePermissionStepLog,
)
from recovery_service.services.resource_permissions import (
    ResourcePermissionStepError,
    _delete_role,
    _execute_permission_step,
    _import_data_permissions,
    _lookup_resource_id,
    _permission_payload,
    delete_permission_role,
    derive_permission_api_url,
    derive_role_delete_api_url,
    validate_permission_api_url,
)


class ResourcePermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _batch(**overrides):
        values = {
            "id": uuid.uuid4(),
            "lookup_connection_id": uuid.uuid4(),
            "lookup_connection_name": "resource-doris",
            "lookup_database": "TESTS",
            "lookup_table": "data_connection",
            "lookup_name_column": "name",
            "lookup_id_column": "id",
            "lookup_timeout_seconds": 60,
            "lookup_interval_seconds": 2,
            "permission_api_url": "http://mock.local/api/dash/role/importDataPermissions",
            "api_url": "http://mock.local/api/dash/dataConnection/apiAdd",
            "api_token_enc": "",
            "youdata_login_name": "zhangsan@example.com",
            "youdata_password_enc": "encrypted-password",
            "youdata_token_url": "http://mock.local/api/dash/util/genToken",
            "project_id": 6,
            "paths": ["2026年7月培训项目_数据连接"],
            "expire_at": datetime(2027, 8, 3, 16, 0, 0),
            "permissions": list(RESOURCE_PERMISSION_OPTIONS),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def _row(**overrides):
        values = {
            "mobile": "18888888888",
            "database_name": "财政一处_张三",
            "resource_id": 2487,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_permission_url_is_derived_from_api_add(self) -> None:
        self.assertEqual(
            derive_permission_api_url("https://youdata.example.com/api/dash/dataConnection/apiAdd"),
            "https://youdata.example.com/api/dash/role/importDataPermissions",
        )
        self.assertEqual(
            validate_permission_api_url("https://youdata.example.com/api/dash/role/importDataPermissions/"),
            "https://youdata.example.com/api/dash/role/importDataPermissions",
        )
        with self.assertRaisesRegex(ValueError, "importDataPermissions"):
            validate_permission_api_url("https://youdata.example.com/api/dash/dataConnection/apiAdd")

    def test_role_delete_url_is_derived_from_permission_url(self) -> None:
        self.assertEqual(
            derive_role_delete_api_url("https://youdata.example.com/api/dash/role/importDataPermissions"),
            "https://youdata.example.com/api/dash/role/ext/delete",
        )

    def test_request_contract_rejects_unknown_permission_and_unsafe_identifier(self) -> None:
        base = {
            "source_batch_id": str(uuid.uuid4()),
            "lookup_connection_id": str(uuid.uuid4()),
            "permission_api_url": "https://youdata.example.com/api/dash/role/importDataPermissions",
            "project_id": 6,
            "paths": ["培训目录"],
            "expire_at": datetime.now() + timedelta(days=1),
        }
        request = ResourcePermissionBatchCreateRequest(**base)
        self.assertEqual(request.lookup_database, "TESTS")
        self.assertEqual(request.lookup_table, "data_connection")
        self.assertEqual(request.permissions, list(RESOURCE_PERMISSION_OPTIONS))
        with self.assertRaises(ValidationError):
            ResourcePermissionBatchCreateRequest(**base, lookup_table="data_connection; DROP TABLE x")
        with self.assertRaises(ValidationError):
            ResourcePermissionBatchCreateRequest(**base, permissions=["view", "unknown"])

    def test_unique_resource_name_returns_positive_integer_id(self) -> None:
        batch = self._batch()
        row = self._row(resource_id=None)
        with patch(
            "recovery_service.services.resource_permissions._query_resource_ids",
            return_value=[2487],
        ) as query:
            state, message, details = _lookup_resource_id(batch, row, SimpleNamespace())

        self.assertEqual(state, "succeeded")
        self.assertIn("2487", message)
        self.assertEqual(details["resource_id"], 2487)
        self.assertIn("`TESTS`.`data_connection`", details["sql_text"])
        self.assertIn("WHERE `name` = %s LIMIT 2", details["sql_text"])
        self.assertEqual(query.call_args.args[2], "财政一处_张三")

    def test_duplicate_resource_name_is_conflict(self) -> None:
        with patch(
            "recovery_service.services.resource_permissions._query_resource_ids",
            return_value=[2487, 2488],
        ):
            with self.assertRaises(ResourcePermissionStepError) as raised:
                _lookup_resource_id(self._batch(), self._row(resource_id=None), SimpleNamespace())

        self.assertEqual(raised.exception.state, "conflict")
        self.assertEqual(raised.exception.details["response_summary"]["matchedCount"], 2)

    def test_invalid_resource_id_is_rejected(self) -> None:
        for value in ("not-an-integer", 0, -1):
            with self.subTest(value=value):
                with patch(
                    "recovery_service.services.resource_permissions._query_resource_ids",
                    return_value=[value],
                ):
                    with self.assertRaises(ResourcePermissionStepError):
                        _lookup_resource_id(self._batch(), self._row(resource_id=None), SimpleNamespace())

    def test_empty_lookup_retries_until_timeout(self) -> None:
        batch = self._batch(lookup_timeout_seconds=1, lookup_interval_seconds=1)
        with (
            patch("recovery_service.services.resource_permissions._query_resource_ids", return_value=[]),
            patch("recovery_service.services.resource_permissions.time.monotonic", side_effect=[0.0, 0.5, 1.1]),
            patch("recovery_service.services.resource_permissions.time.sleep") as sleeper,
        ):
            with self.assertRaisesRegex(ResourcePermissionStepError, "1 秒内未查询到") as raised:
                _lookup_resource_id(batch, self._row(resource_id=None), SimpleNamespace())

        self.assertEqual(raised.exception.details["response_summary"]["attempts"], 2)
        sleeper.assert_called_once_with(1)

    def test_permission_payload_matches_external_api_contract(self) -> None:
        payload = _permission_payload(self._batch(), self._row(), "secret-token")

        self.assertEqual(payload["token"], "secret-token")
        self.assertEqual(payload["uniqueId"], "18888888888")
        self.assertEqual(payload["userExpireMap"], {"18888888888": "2027-08-03 16:00:00"})
        self.assertEqual(payload["roleName"], "财政一处_张三")
        self.assertEqual(payload["type"], 0)
        self.assertEqual(payload["importResourceTypes"], ["DATA_CONNECTION"])
        resource = payload["resourcePermissions"][0]
        self.assertEqual(resource["resourceType"], "DATA_CONNECTION")
        self.assertEqual(resource["resourceId"], 2487)
        self.assertEqual(resource["permissions"], list(RESOURCE_PERMISSION_OPTIONS))
        self.assertEqual(resource["isFolder"], 0)

    def test_auth_failure_refreshes_token_once_and_masks_tokens(self) -> None:
        responses = [
            httpx.Response(200, json={"code": 401, "message": "请登录"}),
            httpx.Response(200, json={"code": 200, "message": "ok", "result": 9001}),
        ]
        posted_payloads = []

        def fake_post(api_url, payload, request_summary):
            del api_url, request_summary
            posted_payloads.append(dict(payload))
            return responses.pop(0)

        with (
            patch("recovery_service.services.resource_permissions._resolve_youdata_token", return_value=("stale-token", "memory")),
            patch("recovery_service.services.resource_permissions._refresh_youdata_token", return_value=("fresh-token", "refreshed")) as refresh,
            patch("recovery_service.services.resource_permissions._post_permission_api", side_effect=fake_post),
        ):
            state, message, details = _import_data_permissions(self._batch(), self._row())

        self.assertEqual(state, "succeeded")
        self.assertIn("失效刷新", message)
        refresh.assert_called_once()
        self.assertEqual([item["token"] for item in posted_payloads], ["stale-token", "fresh-token"])
        serialized = json.dumps(details, ensure_ascii=False)
        self.assertNotIn("stale-token", serialized)
        self.assertNotIn("fresh-token", serialized)
        self.assertIn('"token": "******"', serialized)

    def test_business_http_500_does_not_refresh_token(self) -> None:
        with (
            patch("recovery_service.services.resource_permissions._resolve_youdata_token", return_value=("valid-token", "memory")),
            patch("recovery_service.services.resource_permissions._refresh_youdata_token") as refresh,
            patch(
                "recovery_service.services.resource_permissions._post_permission_api",
                return_value=httpx.Response(500, json={"code": 500, "message": "internal server error"}),
            ),
        ):
            with self.assertRaises(ResourcePermissionStepError) as raised:
                _import_data_permissions(self._batch(), self._row())

        refresh.assert_not_called()
        self.assertEqual(raised.exception.details["response_summary"]["code"], 500)
        self.assertNotIn("valid-token", json.dumps(raised.exception.details, ensure_ascii=False))

    def test_import_result_is_saved_as_positive_role_id(self) -> None:
        with (
            patch("recovery_service.services.resource_permissions._resolve_youdata_token", return_value=("valid-token", "memory")),
            patch(
                "recovery_service.services.resource_permissions._post_permission_api",
                return_value=httpx.Response(200, json={"code": 200, "result": "9001"}),
            ),
        ):
            state, message, details = _import_data_permissions(self._batch(), self._row())

        self.assertEqual(state, "succeeded")
        self.assertEqual(details["role_id"], 9001)
        self.assertIn("9001", message)

    def test_import_success_without_valid_role_id_fails(self) -> None:
        for result in (None, "", "abc", True, 1.5, 0, -1):
            with self.subTest(result=result), patch(
                "recovery_service.services.resource_permissions._resolve_youdata_token",
                return_value=("valid-token", "memory"),
            ), patch(
                "recovery_service.services.resource_permissions._post_permission_api",
                return_value=httpx.Response(200, json={"code": 200, "result": result}),
            ):
                with self.assertRaisesRegex(ResourcePermissionStepError, "角色 ID"):
                    _import_data_permissions(self._batch(), self._row())

    def test_delete_role_posts_only_token_and_role_id_and_persists_state(self) -> None:
        batch_id = uuid.uuid4()
        row_id = uuid.uuid4()
        with self.factory() as db:
            db.add(
                ResourcePermissionBatch(
                    id=batch_id,
                    source_batch_id=uuid.uuid4(),
                    source_filename="people.xlsx",
                    lookup_connection_id=uuid.uuid4(),
                    lookup_connection_name="resource-doris",
                    permission_api_url="http://mock.local/api/dash/role/importDataPermissions",
                    youdata_login_name="zhangsan@example.com",
                    youdata_password_enc="encrypted-password",
                    youdata_token_url="http://mock.local/api/dash/util/genToken",
                    project_id=6,
                    paths=["培训目录"],
                    expire_at=datetime(2027, 8, 3, 16, 0, 0),
                    permissions=list(RESOURCE_PERMISSION_OPTIONS),
                    total_count=1,
                )
            )
            db.add(
                ResourcePermissionRow(
                    id=row_id,
                    batch_id=batch_id,
                    source_row_id=uuid.uuid4(),
                    row_no=2,
                    person_name="张三",
                    department_name="财政一处",
                    mobile="18888888888",
                    database_name="财政一处_张三",
                    role_id=9001,
                    role_delete_state="active",
                )
            )
            db.commit()

        payloads = []

        def fake_delete(api_url, payload, request_summary):
            self.assertEqual(api_url, "http://mock.local/api/dash/role/ext/delete")
            self.assertEqual(set(payload), {"token", "roleId"})
            payloads.append(dict(payload))
            self.assertEqual(request_summary["token"], "******")
            return httpx.Response(200, json={"code": 200, "message": "ok"})

        with (
            patch("recovery_service.services.resource_permissions.get_sync_session_factory", return_value=self.factory),
            patch("recovery_service.services.resource_permissions._resolve_youdata_token", return_value=("valid-token", "memory")),
            patch("recovery_service.services.resource_permissions._post_role_delete_api", side_effect=fake_delete),
        ):
            delete_permission_role(batch_id, row_id)

        self.assertEqual(payloads, [{"token": "valid-token", "roleId": 9001}])
        with self.factory() as db:
            row = db.get(ResourcePermissionRow, row_id)
            log = db.scalar(select(ResourcePermissionStepLog).where(ResourcePermissionStepLog.row_id == row_id))
            self.assertEqual(row.role_id, 9001)
            self.assertEqual(row.role_delete_state, "deleted")
            self.assertIsNotNone(row.role_deleted_at)
            self.assertEqual(log.step, "delete_role")
            self.assertEqual(log.state, "succeeded")
            self.assertEqual(log.request_summary["token"], "******")

    def test_delete_role_auth_failure_refreshes_once(self) -> None:
        batch = self._batch()
        responses = [
            httpx.Response(200, json={"code": 401, "message": "请登录"}),
            httpx.Response(200, json={"code": 200, "message": "ok"}),
        ]
        payloads = []

        def fake_delete(api_url, payload, request_summary):
            del api_url, request_summary
            payloads.append(dict(payload))
            return responses.pop(0)

        with (
            patch("recovery_service.services.resource_permissions._resolve_youdata_token", return_value=("stale-token", "memory")),
            patch("recovery_service.services.resource_permissions._refresh_youdata_token", return_value=("fresh-token", "refreshed")) as refresh,
            patch("recovery_service.services.resource_permissions._post_role_delete_api", side_effect=fake_delete),
        ):
            state, message, details = _delete_role(batch, 9001)

        self.assertEqual(state, "succeeded")
        self.assertIn("刷新", message)
        refresh.assert_called_once()
        self.assertEqual([item["token"] for item in payloads], ["stale-token", "fresh-token"])
        self.assertNotIn("stale-token", json.dumps(details, ensure_ascii=False))
        self.assertNotIn("fresh-token", json.dumps(details, ensure_ascii=False))

    def test_delete_role_rejects_in_progress_row(self) -> None:
        batch_id = uuid.uuid4()
        row_id = uuid.uuid4()
        with self.factory() as db:
            db.add(
                ResourcePermissionBatch(
                    id=batch_id,
                    source_batch_id=uuid.uuid4(),
                    source_filename="people.xlsx",
                    lookup_connection_id=uuid.uuid4(),
                    lookup_connection_name="resource-doris",
                    permission_api_url="http://mock.local/api/dash/role/importDataPermissions",
                    project_id=6,
                    paths=["培训目录"],
                    expire_at=datetime(2027, 8, 3, 16, 0, 0),
                    permissions=list(RESOURCE_PERMISSION_OPTIONS),
                    total_count=1,
                )
            )
            db.add(
                ResourcePermissionRow(
                    id=row_id,
                    batch_id=batch_id,
                    source_row_id=uuid.uuid4(),
                    row_no=2,
                    person_name="张三",
                    department_name="财政一处",
                    mobile="18888888888",
                    database_name="财政一处_张三",
                    role_id=9001,
                    role_delete_state="deleting",
                )
            )
            db.commit()

        with patch("recovery_service.services.resource_permissions.get_sync_session_factory", return_value=self.factory):
            with self.assertRaisesRegex(ValueError, "正在删除"):
                delete_permission_role(batch_id, row_id)

    def test_failed_step_details_are_committed_to_independent_log(self) -> None:
        batch_id = uuid.uuid4()
        row_id = uuid.uuid4()
        with self.factory() as db:
            db.add(
                ResourcePermissionBatch(
                    id=batch_id,
                    source_batch_id=uuid.uuid4(),
                    source_filename="people.xlsx",
                    lookup_connection_id=uuid.uuid4(),
                    lookup_connection_name="resource-doris",
                    permission_api_url="http://mock.local/api/dash/role/importDataPermissions",
                    project_id=6,
                    paths=["培训目录"],
                    expire_at=datetime(2027, 8, 3, 16, 0, 0),
                    permissions=list(RESOURCE_PERMISSION_OPTIONS),
                    total_count=1,
                )
            )
            db.add(
                ResourcePermissionRow(
                    id=row_id,
                    batch_id=batch_id,
                    source_row_id=uuid.uuid4(),
                    row_no=2,
                    person_name="张三",
                    department_name="财政一处",
                    mobile="18888888888",
                    database_name="财政一处_张三",
                )
            )
            db.commit()

        failure = ResourcePermissionStepError(
            "资源名称匹配冲突",
            {
                "sql_text": "SELECT `id` FROM `TESTS`.`data_connection` WHERE `name` = %s LIMIT 2",
                "request_summary": {"name": "财政一处_张三"},
                "response_summary": {"matchedCount": 2},
            },
            state="conflict",
        )
        with (
            patch("recovery_service.services.resource_permissions.get_sync_session_factory", return_value=self.factory),
            patch("recovery_service.services.resource_permissions._perform_permission_step", side_effect=failure),
        ):
            with self.assertRaises(ResourcePermissionStepError):
                _execute_permission_step(row_id, "lookup_resource")

        with self.factory() as db:
            log = db.scalar(select(ResourcePermissionStepLog).where(ResourcePermissionStepLog.row_id == row_id))
            self.assertIsNotNone(log)
            self.assertEqual(log.state, "conflict")
            self.assertEqual(log.response_summary["matchedCount"], 2)
            self.assertIn("SELECT `id`", log.sql_text)

    def test_ui_contains_independent_subapp_and_event_wiring(self) -> None:
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")

        self.assertIn('data-resource-provisioning-subapp="permissions"', ui)
        self.assertIn('data-resource-provisioning-pane="permissions"', ui)
        self.assertIn('id="resourcePermissionSourceBatch"', ui)
        self.assertIn('id="resourcePermissionSubmitBtn"', ui)
        self.assertIn('id="resourcePermissionBatchList"', ui)
        self.assertIn("data-resource-permission-delete-role", ui)
        self.assertIn("/delete-role", ui)
        self.assertIn('$("resourceProvisioningSubapps").addEventListener("click"', ui)
        self.assertIn('$("resourcePermissionSubmitBtn").addEventListener("click"', ui)
        self.assertIn('$("resourcePermissionBatchList").addEventListener("click"', ui)
        self.assertIn("/api/v1/resource-provisioning/permission-batches", ui)


if __name__ == "__main__":
    unittest.main()
