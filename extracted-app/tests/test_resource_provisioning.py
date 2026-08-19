import json
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recovery_service.core.models.task import (
    Base,
    ResourceProvisioningBatch,
    ResourceProvisioningRow,
    ResourceProvisioningStepLog,
)
from recovery_service.api.schemas.resource_provisioning import ResourceProvisioningBatchCreateRequest
from recovery_service.services.resource_provisioning import (
    ResourceProvisioningStepError,
    _clear_youdata_token_cache,
    _derive_youdata_token_url,
    _execute_step,
    _external_success,
    _is_youdata_auth_failure,
    _refresh_youdata_token,
    _register_external_connection,
    _resolve_youdata_token,
    _quoted_user,
    generate_username,
    preview_file,
)


class ResourceProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        _clear_youdata_token_cache()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        _clear_youdata_token_cache()
        self.engine.dispose()

    @staticmethod
    def _youdata_batch() -> SimpleNamespace:
        return SimpleNamespace(
            api_token_enc="",
            api_url="http://mock.local/api/dash/dataConnection/apiAdd",
            youdata_login_name="zhangsan@example.com",
            youdata_password_enc="youdata-password",
            youdata_token_url="http://mock.local/api/dash/util/genToken",
            project_id=6,
            paths=["测试目录"],
            server="127.0.0.1",
            port=9030,
        )

    @staticmethod
    def _youdata_row(name: str = "测试库_张三") -> SimpleNamespace:
        return SimpleNamespace(database_name=name, db_username="zhangsan18888888888")

    def test_example_generates_expected_username_and_database(self) -> None:
        result = preview_file(
            "people.csv",
            "姓名,部门,手机号\n张三,财政一处,18888888888\n".encode("utf-8"),
        )

        self.assertEqual(result.valid_count, 1)
        self.assertEqual(result.rows[0].db_username, "zhangsan18888888888")
        self.assertEqual(result.rows[0].database_name, "财政一处_张三")

    def test_invalid_mobile_and_duplicate_generated_names_are_blocked(self) -> None:
        result = preview_file(
            "people.csv",
            (
                "姓名,部门,手机号\n"
                "张三,财政一处,123\n"
                "张三,财政一处,123\n"
            ).encode("utf-8"),
        )

        self.assertEqual(result.invalid_count, 2)
        self.assertIn("手机号必须是 11 位中国大陆手机号。", result.rows[0].issues)
        self.assertIn("批次内用户名重复。", result.rows[0].issues)
        self.assertIn("批次内数据库名重复。", result.rows[0].issues)

    def test_missing_required_headers_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "表头必须包含"):
            preview_file("people.csv", "姓名,手机号\n张三,18888888888\n".encode("utf-8"))

    def test_external_request_uses_boolean_skip_test_and_masks_secrets(self) -> None:
        batch = SimpleNamespace(
            api_token_enc="external-token",
            api_url="http://mock.local/api/dash/dataConnection/apiAdd",
            project_id=6,
            paths=["培训项目_数据连接"],
            server="23.47.200.99",
            port=9030,
        )
        row = SimpleNamespace(
            database_name="财政一处_张三",
            db_username="zhangsan18888888888",
        )

        def fake_post(url, *, json, timeout):
            self.assertEqual(url, batch.api_url)
            self.assertIs(json["skipTest"], False)
            self.assertEqual(json["password"], "Doris@default0")
            self.assertEqual(json["token"], "external-token")
            return httpx.Response(200, json={"success": True, "code": 200, "message": "ok"})

        with patch("recovery_service.services.resource_provisioning.httpx.post", side_effect=fake_post):
            state, _, details = _register_external_connection(batch, row, "Doris@default0")

        self.assertEqual(state, "succeeded")
        self.assertIs(details["request_summary"]["skipTest"], False)
        self.assertEqual(details["request_summary"]["password"], "******")
        self.assertEqual(details["request_summary"]["token"], "******")
        self.assertNotIn("Doris@default0", json.dumps(details, ensure_ascii=False))
        self.assertNotIn("external-token", json.dumps(details, ensure_ascii=False))

    def test_youdata_token_url_is_derived_from_api_add_url(self) -> None:
        self.assertEqual(
            _derive_youdata_token_url("https://youdata.example.com/api/dash/dataConnection/apiAdd"),
            "https://youdata.example.com/api/dash/util/genToken",
        )
        with self.assertRaisesRegex(ValueError, "api/dash/dataConnection/apiAdd"):
            _derive_youdata_token_url("https://youdata.example.com/custom/apiAdd")

    def test_youdata_login_is_cached_in_worker_memory(self) -> None:
        batch = self._youdata_batch()
        calls = {"login": 0, "api_add": 0}

        def fake_post(url, *, json, timeout):
            del timeout
            if url.endswith("/genToken"):
                calls["login"] += 1
                self.assertEqual(json["tokenType"], "userPassword")
                self.assertEqual(json["email"], "zhangsan@example.com")
                self.assertEqual(json["password"], "youdata-password")
                return httpx.Response(200, json={"code": 200, "result": "generated-youdata-token"})
            calls["api_add"] += 1
            self.assertEqual(json["token"], "generated-youdata-token")
            return httpx.Response(200, json={"success": True, "code": 200, "message": "ok"})

        with patch("recovery_service.services.resource_provisioning.httpx.post", side_effect=fake_post):
            first = _register_external_connection(batch, self._youdata_row("测试库_A"), "Doris@default0")
            second = _register_external_connection(batch, self._youdata_row("测试库_B"), "Doris@default0")

        self.assertEqual(calls, {"login": 1, "api_add": 2})
        self.assertEqual(first[2]["request_summary"]["youdataAuth"]["tokenSource"], "generated")
        self.assertEqual(second[2]["request_summary"]["youdataAuth"]["tokenSource"], "memory")
        serialized = json.dumps([first, second], ensure_ascii=False)
        self.assertNotIn("generated-youdata-token", serialized)
        self.assertNotIn("youdata-password", serialized)

    def test_parallel_token_lookup_uses_single_login(self) -> None:
        batch = self._youdata_batch()
        login_calls = 0

        def fake_post(url, *, json, timeout):
            nonlocal login_calls
            del json, timeout
            self.assertTrue(url.endswith("/genToken"))
            login_calls += 1
            time.sleep(0.05)
            return httpx.Response(200, json={"code": 200, "result": "shared-token"})

        with patch("recovery_service.services.resource_provisioning.httpx.post", side_effect=fake_post):
            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(lambda _: _resolve_youdata_token(batch), range(5)))

        self.assertEqual(login_calls, 1)
        self.assertEqual({item[0] for item in results}, {"shared-token"})
        self.assertEqual([item[1] for item in results].count("generated"), 1)
        self.assertEqual([item[1] for item in results].count("memory"), 4)

    def test_auth_failure_refreshes_once_and_retries_api_add(self) -> None:
        batch = self._youdata_batch()
        calls = {"login": 0, "api_add": 0}

        def fake_post(url, *, json, timeout):
            del timeout
            if url.endswith("/genToken"):
                calls["login"] += 1
                token = "stale-token" if calls["login"] == 1 else "fresh-token"
                return httpx.Response(200, json={"code": 200, "result": token})
            calls["api_add"] += 1
            if json["token"] == "stale-token":
                return httpx.Response(200, json={"code": 401, "message": "请登录"})
            self.assertEqual(json["token"], "fresh-token")
            return httpx.Response(200, json={"success": True, "code": 200, "message": "ok"})

        with patch("recovery_service.services.resource_provisioning.httpx.post", side_effect=fake_post):
            state, message, details = _register_external_connection(
                batch,
                self._youdata_row(),
                "Doris@default0",
            )

        self.assertEqual(state, "succeeded")
        self.assertIn("失效刷新", message)
        self.assertEqual(calls, {"login": 2, "api_add": 2})
        self.assertTrue(details["response_summary"]["youdataTokenRefreshed"])
        self.assertEqual(details["response_summary"]["initialAuthFailure"]["code"], 401)
        serialized = json.dumps(details, ensure_ascii=False)
        self.assertNotIn("stale-token", serialized)
        self.assertNotIn("fresh-token", serialized)

    def test_parallel_stale_token_refresh_uses_single_login(self) -> None:
        batch = self._youdata_batch()
        login_calls = 0

        def fake_post(url, *, json, timeout):
            nonlocal login_calls
            del json, timeout
            self.assertTrue(url.endswith("/genToken"))
            login_calls += 1
            token = "stale-token" if login_calls == 1 else "fresh-token"
            time.sleep(0.05)
            return httpx.Response(200, json={"code": 200, "result": token})

        with patch("recovery_service.services.resource_provisioning.httpx.post", side_effect=fake_post):
            stale_token, _ = _resolve_youdata_token(batch)
            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(lambda _: _refresh_youdata_token(batch, stale_token), range(5)))

        self.assertEqual(login_calls, 2)
        self.assertEqual({item[0] for item in results}, {"fresh-token"})
        self.assertEqual([item[1] for item in results].count("refreshed"), 1)
        self.assertEqual([item[1] for item in results].count("refreshed_by_peer"), 4)

    def test_non_auth_business_failure_does_not_refresh_token(self) -> None:
        batch = self._youdata_batch()
        calls = {"login": 0, "api_add": 0}

        def fake_post(url, *, json, timeout):
            del json, timeout
            if url.endswith("/genToken"):
                calls["login"] += 1
                return httpx.Response(200, json={"code": 200, "result": "valid-token"})
            calls["api_add"] += 1
            return httpx.Response(500, json={"code": 500, "message": "internal server error"})

        with patch("recovery_service.services.resource_provisioning.httpx.post", side_effect=fake_post):
            with self.assertRaises(ResourceProvisioningStepError):
                _register_external_connection(batch, self._youdata_row(), "Doris@default0")

        self.assertEqual(calls, {"login": 1, "api_add": 1})

    def test_login_failure_masks_result_and_password(self) -> None:
        batch = self._youdata_batch()
        response = httpx.Response(
            200,
            json={"code": 500, "message": "invalid password", "result": "must-not-leak"},
        )
        with patch("recovery_service.services.resource_provisioning.httpx.post", return_value=response):
            with self.assertRaises(ResourceProvisioningStepError) as raised:
                _register_external_connection(batch, self._youdata_row(), "Doris@default0")

        serialized = json.dumps(raised.exception.details, ensure_ascii=False)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("youdata-password", serialized)
        self.assertIn('"result": "******"', serialized)

    def test_only_explicit_auth_failures_request_token_refresh(self) -> None:
        self.assertTrue(_is_youdata_auth_failure(401, {"message": "failed"}))
        self.assertTrue(_is_youdata_auth_failure(200, {"code": 500, "message": "Token 已过期，请登录"}))
        self.assertFalse(_is_youdata_auth_failure(500, {"code": 500, "message": "connection failed"}))

    def test_batch_request_requires_one_auth_strategy(self) -> None:
        base = {
            "filename": "people.csv",
            "connection_id": str(uuid.uuid4()),
            "user_password": "Doris@default0",
            "api_url": "https://youdata.example.com/api/dash/dataConnection/apiAdd",
            "project_id": 6,
            "paths": ["测试目录"],
            "server": "127.0.0.1",
            "port": 9030,
            "parallelism": 2,
            "rows": [{
                "row_no": 2,
                "person_name": "张三",
                "department_name": "财政一处",
                "mobile": "18888888888",
                "db_username": "zhangsan18888888888",
                "database_name": "财政一处_张三",
            }],
        }
        current = ResourceProvisioningBatchCreateRequest(
            **base,
            youdata_login_name="zhangsan@example.com",
            youdata_password="youdata-password",
        )
        self.assertEqual(current.youdata_login_name, "zhangsan@example.com")
        legacy = ResourceProvisioningBatchCreateRequest(**base, api_token="legacy-token")
        self.assertEqual(legacy.api_token.get_secret_value(), "legacy-token")
        with self.assertRaises(ValidationError):
            ResourceProvisioningBatchCreateRequest(**base)

    def test_external_success_requires_explicit_business_success(self) -> None:
        self.assertTrue(_external_success({"success": True}))
        self.assertTrue(_external_success({"code": 0}))
        self.assertTrue(_external_success({"status": "ok"}))
        self.assertFalse(_external_success({"message": "accepted"}))

    def test_external_http_failure_keeps_redacted_request_and_response(self) -> None:
        batch = SimpleNamespace(
            api_token_enc="external-token",
            api_url="http://mock.local/api/dash/dataConnection/apiAdd",
            project_id=6,
            paths=["测试目录"],
            server="127.0.0.1",
            port=9030,
        )
        row = SimpleNamespace(database_name="测试库_张三", db_username="zhangsan18888888888")
        response = httpx.Response(
            500,
            json={"message": "failed", "password": "Doris@default0", "data": {"token": "external-token"}},
        )

        with patch("recovery_service.services.resource_provisioning.httpx.post", return_value=response):
            with self.assertRaises(ResourceProvisioningStepError) as raised:
                _register_external_connection(batch, row, "Doris@default0")

        serialized = json.dumps(raised.exception.details, ensure_ascii=False)
        self.assertIn('"password": "******"', serialized)
        self.assertIn('"token": "******"', serialized)
        self.assertNotIn("Doris@default0", serialized)
        self.assertNotIn("external-token", serialized)

    def test_external_business_failure_keeps_request_and_response_details(self) -> None:
        batch = SimpleNamespace(
            api_token_enc="external-token",
            api_url="http://mock.local/api/dash/dataConnection/apiAdd",
            project_id=6,
            paths=["测试目录"],
            server="127.0.0.1",
            port=9030,
        )
        row = SimpleNamespace(database_name="测试库_张三", db_username="zhangsan18888888888")

        with patch(
            "recovery_service.services.resource_provisioning.httpx.post",
            return_value=httpx.Response(200, json={"success": False, "code": 500, "message": "connection failed"}),
        ):
            with self.assertRaises(ResourceProvisioningStepError) as raised:
                _register_external_connection(batch, row, "Doris@default0")

        self.assertEqual(raised.exception.details["request_summary"]["skipTest"], False)
        self.assertEqual(raised.exception.details["response_summary"]["code"], 500)

    def test_failed_step_details_are_committed_to_step_log(self) -> None:
        batch_id = uuid.uuid4()
        row_id = uuid.uuid4()
        with self.factory() as db:
            db.add(
                ResourceProvisioningBatch(
                    id=batch_id,
                    filename="people.csv",
                    connection_id=uuid.uuid4(),
                    connection_name="test-doris",
                    api_url="http://mock.local/api/dash/dataConnection/apiAdd",
                    api_token_enc="encrypted-token",
                    user_password_enc="encrypted-password",
                    project_id=6,
                    paths=["测试目录"],
                    server="127.0.0.1",
                    port=9030,
                    parallelism=1,
                    total_count=1,
                )
            )
            db.add(
                ResourceProvisioningRow(
                    id=row_id,
                    batch_id=batch_id,
                    row_no=2,
                    person_name="张三",
                    department_name="财政一处",
                    mobile="18888888888",
                    db_username="zhangsan18888888888",
                    database_name="财政一处_张三",
                )
            )
            db.commit()

        failure = ResourceProvisioningStepError(
            "外部接口业务失败",
            {
                "request_summary": {"skipTest": False, "password": "******"},
                "response_summary": {"code": 500, "message": "failed"},
            },
        )
        with (
            patch(
                "recovery_service.services.resource_provisioning.get_sync_session_factory",
                return_value=self.factory,
            ),
            patch(
                "recovery_service.services.resource_provisioning._perform_step",
                side_effect=failure,
            ),
        ):
            with self.assertRaises(ResourceProvisioningStepError):
                _execute_step(row_id, "register_connection")

        with self.factory() as db:
            log = db.scalar(select(ResourceProvisioningStepLog).where(ResourceProvisioningStepLog.row_id == row_id))
            self.assertIsNotNone(log)
            self.assertEqual(log.state, "failed")
            self.assertEqual(log.request_summary["skipTest"], False)
            self.assertEqual(log.request_summary["password"], "******")
            self.assertEqual(log.response_summary["code"], 500)
            self.assertEqual(log.error_message, "外部接口业务失败")

    def test_non_chinese_name_is_normalized(self) -> None:
        self.assertEqual(generate_username("Li Ming", "18888888888"), "liming18888888888")

    def test_create_user_escapes_percent_for_pymysql_parameter_formatting(self) -> None:
        self.assertEqual(_quoted_user("codexrp20260728a"), "'codexrp20260728a'@'%'")
        self.assertEqual(
            _quoted_user("codexrp20260728a", escape_percent=True),
            "'codexrp20260728a'@'%%'",
        )

    def test_preview_edit_validation_does_not_replace_active_inputs(self) -> None:
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")

        self.assertIn('data-resource-provisioning-validation="${index}"', ui)
        self.assertIn("cell.innerHTML = renderResourceProvisioningValidation(item)", ui)
        self.assertNotIn('$("resourceProvisioningPreview").addEventListener("change"', ui)

    def test_ui_uses_youdata_credentials_instead_of_manual_token(self) -> None:
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")

        self.assertIn('id="resourceProvisioningYoudataLoginName"', ui)
        self.assertIn('id="resourceProvisioningYoudataPassword"', ui)
        self.assertIn("youdata_login_name: youdataLoginName", ui)
        self.assertIn("youdata_password: youdataPassword", ui)
        self.assertNotIn('id="resourceProvisioningToken"', ui)
        self.assertNotIn("api_token: apiToken", ui)


if __name__ == "__main__":
    unittest.main()
