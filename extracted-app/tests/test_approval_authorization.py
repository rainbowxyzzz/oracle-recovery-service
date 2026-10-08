import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from recovery_service.core.models.task import (
    ApprovalAuthorizationCase,
    ApprovalAuthorizationConfig,
    ApprovalAuthorizationRun,
    ApprovalAuthorizationStepLog,
    Base,
    DatabaseConnectionProfile,
)
from recovery_service.services.approval_authorization import (
    _extract_department_name,
    _normalized_config,
    _Runtime,
    _validate_import_permission_path,
)


class ApprovalAuthorizationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[
                DatabaseConnectionProfile.__table__,
                ApprovalAuthorizationConfig.__table__,
                ApprovalAuthorizationRun.__table__,
                ApprovalAuthorizationStepLog.__table__,
                ApprovalAuthorizationCase.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.connection_id = uuid.uuid4()
        self.config_id = uuid.uuid4()
        self.run_id = uuid.uuid4()
        self.session.add(
            DatabaseConnectionProfile(
                id=self.connection_id,
                name="Doris 测试连接",
                engine="doris",
                host="127.0.0.1",
                port=9030,
                username="root",
                password_enc="",
                database="TESTS",
            )
        )
        self.session.add(
            ApprovalAuthorizationConfig(
                id=self.config_id,
                name="审批流测试配置",
                doris_connection_id=self.connection_id,
                workflow_base_url="http://workflow.example",
                workflow_username="workflow-user",
                workflow_password_enc="",
                youdata_base_url="http://youdata.example",
                youdata_email="youdata@example.com",
                youdata_password_enc="",
                default_doris_password_enc="",
            )
        )
        self.session.add(
            ApprovalAuthorizationRun(
                id=self.run_id,
                config_id=self.config_id,
                config_name="审批流测试配置",
                state="running",
            )
        )
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _runtime(self):
        return _Runtime(self.session, self.run_id, self.config_id)

    def test_todo_list_filters_audit_status_zero_and_logs_response(self):
        runtime = self._runtime()

        def fake_post(url, body, headers=None):
            return {
                "code": 200,
                "data": {
                    "list": [
                        {"id": "A001", "auditStatus": 0, "createTime": "2020-08-01 18:50:10"},
                        {"id": "A002", "auditStatus": 1},
                        {"id": "A003", "auditStatus": "0"},
                        {"id": "A004", "auditStatus": ""},
                    ]
                },
            }

        with patch.object(runtime, "_post_json", side_effect=fake_post):
            result = runtime.execute_step("todo_list", {"workflow_token": "token-123"}, None)

        self.assertEqual(result["apply_flow_ids"], ["A001", "A003", "A004"])
        self.assertEqual(result["apply_flow_create_times"]["A001"], "2020-08-01 18:50:10")
        log = self.session.query(ApprovalAuthorizationStepLog).one()
        self.assertEqual(log.status, "success")
        self.assertEqual(log.extracted_data["audit_status_ready_count"], 3)
        self.assertEqual(log.extracted_data["audit_status_zero_count"], 2)
        self.assertEqual(log.extracted_data["audit_status_empty_count"], 1)
        self.assertEqual(log.extracted_data["audit_status_counts"], {"0": 2, "1": 1, "空值": 1})
        self.assertEqual(log.request_data["headers"]["token"], "toke***-123")

    def test_table_schema_lookup_skips_missing_table_and_keeps_sql_trace(self):
        runtime = self._runtime()

        def fake_query(sql, params):
            runtime._last_sql_text = sql
            runtime._last_sql_params = {"params": list(params)}
            runtime._last_sql_result = {"rows": [], "row_count": 0}
            return []

        with patch.object(runtime, "_query", side_effect=fake_query):
            result = runtime.execute_step(
                "table_schema_lookup",
                {"data_items": [{"datatitle": "T_MISSING", "dataLevel": "1"}]},
                "FLOW001",
            )

        log = self.session.query(ApprovalAuthorizationStepLog).one()
        self.assertEqual(result["grant_records"], [])
        self.assertEqual(result["skipped_tables"][0]["reason"], "missing")
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.apply_flow_id, "FLOW001")
        self.assertIn("information_schema.tables", log.sql_text)
        self.assertEqual(log.sql_params["titles"], ["T_MISSING"])
        self.assertEqual(log.sql_result["rows"], [])

    def test_audit_status_update_posts_workflow_token_and_id(self):
        runtime = self._runtime()
        captured = {}

        def fake_post(url, body, headers=None):
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = headers
            return {"code": 200, "data": True}

        with patch.object(runtime, "_post_json", side_effect=fake_post):
            result = runtime.execute_step(
                "audit_status_update",
                {"workflow_token": "workflow-token", "apply_flow_id": "FLOW001"},
                "FLOW001",
            )

        self.assertTrue(result["audit_status_updated"])
        self.assertEqual(captured["url"], "http://workflow.example/api/market/dataModelApplyFlow/auditStatus")
        self.assertEqual(captured["headers"], {"token": "workflow-token"})
        self.assertEqual(captured["body"], {"id": "FLOW001"})
        log = self.session.query(ApprovalAuthorizationStepLog).one()
        self.assertEqual(log.step_key, "audit_status_update")
        self.assertEqual(log.status, "success")

    def test_extract_department_name_switches_by_prefix(self):
        self.assertEqual(_extract_department_name("重庆市审计局/某某处"), "某某处")
        self.assertEqual(_extract_department_name("重庆市财政局/预算处"), "重庆市财政局")
        self.assertEqual(_extract_department_name("重庆市审计局"), "重庆市审计局")

    def test_new_config_defaults_use_ai_recovery_and_api_auto_authorization_path(self):
        config = _normalized_config({})

        self.assertEqual(config["mapping_database"], "ai_recovery")
        self.assertEqual(config["auth_info_database"], "ai_recovery")
        self.assertEqual(config["api_add_defaults"]["paths"], ["API自动授权"])
        self.assertEqual(config["import_permissions_defaults"]["path"], ["API授权"])

    def test_import_permission_path_rejects_multiple_directories(self):
        with self.assertRaises(ValueError):
            _validate_import_permission_path(["目录A", "目录B"])

    def test_detail_uses_todo_create_time_for_generated_username_suffix(self):
        self.session.get(ApprovalAuthorizationConfig, self.config_id).config = {"date_suffix": "0817"}
        self.session.commit()
        runtime = self._runtime()

        def fake_post(url, body, headers=None):
            return {
                "data": {
                    "createUserDepartment": "重庆市审计局/数据处",
                    "createUserName": "张三",
                    "createUserMobile": "13800001234",
                    "queryUserList": [],
                }
            }

        with patch.object(runtime, "_post_json", side_effect=fake_post):
            result = runtime.execute_step(
                "detail",
                {"workflow_token": "token-123", "apply_flow_id": "FLOW001", "todo_create_time": "2020-08-01 18:50:10"},
                "FLOW001",
            )

        self.assertEqual(result["date_suffix"], "0801")
        self.assertEqual(result["generated_username"], "张三_1234_0801")

    def test_old_import_log_does_not_mark_case_complete(self):
        self.session.add(
            ApprovalAuthorizationStepLog(
                run_id=uuid.uuid4(),
                config_id=self.config_id,
                apply_flow_id="FLOW001",
                step_key="import_permissions",
                step_name="导入有数人员权限",
                status="success",
            )
        )
        self.session.commit()
        runtime = self._runtime()
        self.assertTrue(runtime.claim_apply_flow("FLOW001"))
        case = self.session.query(ApprovalAuthorizationCase).one()
        self.assertEqual(case.state, "running")
        self.assertFalse(case.audit_status_updated)

    def test_second_run_cannot_claim_same_active_apply_flow(self):
        first = self._runtime()
        self.assertTrue(first.claim_apply_flow("FLOW001"))
        second_run_id = uuid.uuid4()
        self.session.add(
            ApprovalAuthorizationRun(
                id=second_run_id,
                config_id=self.config_id,
                config_name="审批流测试配置",
                state="running",
            )
        )
        self.session.commit()
        second = _Runtime(self.session, second_run_id, self.config_id)
        self.assertFalse(second.claim_apply_flow("FLOW001"))

    def test_api_add_reuses_exact_existing_connection(self):
        runtime = self._runtime()
        with patch.object(runtime, "_query", return_value=[{"resource_id": 88}]), patch.object(
            runtime, "_post_json"
        ) as post:
            result = runtime.execute_step(
                "api_add",
                {
                    "youdata_token": "token",
                    "doris_username": "user_01",
                    "doris_password": "secret",
                    "database_name": "DWD_TEST",
                },
                "FLOW001",
            )

        self.assertEqual(result["resource_id"], 88)
        post.assert_not_called()
        log = self.session.query(ApprovalAuthorizationStepLog).one()
        self.assertEqual(log.extracted_data["connection_action"], "reused")

    def test_import_permissions_skips_identified_missing_user(self):
        runtime = self._runtime()
        calls = []

        def fake_post(url, body, headers=None):
            calls.append(list(body["uniqueIds"]))
            if len(calls) == 1:
                runtime._last_http_response = {"body": {"message": "用户不存在: 13800000001"}}
                raise ValueError("用户不存在")
            return {"code": 200, "result": 9}

        with patch.object(runtime, "_post_json", side_effect=fake_post):
            result = runtime.execute_step(
                "import_permissions",
                {
                    "youdata_token": "token",
                    "unique_ids": ["13800000001", "13800000002"],
                    "query_end_time": "2026-12-31 10:00:00",
                    "resource_id": 88,
                    "api_add_name": "DWD_TEST_user_01",
                },
                "FLOW001",
            )

        self.assertEqual(calls, [["13800000001", "13800000002"], ["13800000002"]])
        self.assertEqual(result["authorized_users"], ["13800000002"])
        self.assertEqual(result["skipped_users"], ["13800000001"])
        self.assertEqual(result["role_id"], 9)

    def test_import_permissions_rejects_non_positive_role_id(self):
        runtime = self._runtime()
        with patch.object(runtime, "_post_json", return_value={"code": 200, "result": 0}):
            with self.assertRaisesRegex(ValueError, "角色 id 不是有效正整数"):
                runtime.execute_step(
                    "import_permissions",
                    {
                        "youdata_token": "token",
                        "unique_ids": ["13800000002"],
                        "query_end_time": "2026-12-31 10:00:00",
                        "resource_id": 88,
                        "api_add_name": "DWD_TEST_user_01",
                    },
                    "FLOW001",
                )

    def test_auth_info_insert_only_inserts_missing_records(self):
        runtime = self._runtime()
        records = [
            {"datatitle": "T_EXISTING", "dataLevel": "1", "schema_name": "DWD_TEST"},
            {"datatitle": "T_NEW", "dataLevel": "2", "schema_name": "DWD_TEST"},
        ]
        with patch.object(runtime, "_query", return_value=[{"datatitle": "T_EXISTING"}]), patch.object(
            runtime, "_execute_many", return_value=1
        ) as execute_many:
            result = runtime.execute_step(
                "auth_info_insert",
                {"apply_flow_id": "FLOW001", "grant_records": records},
                "FLOW001",
            )

        self.assertEqual(result["insert_count"], 1)
        self.assertEqual(result["reused_count"], 1)
        self.assertEqual(execute_many.call_args.args[1][0][1], "T_NEW")

    def test_post_json_rejects_explicit_business_failure(self):
        runtime = self._runtime()

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"code": 200, "success": False, "message": "failed"}

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            @staticmethod
            def post(*_args, **_kwargs):
                return FakeResponse()

        with patch("recovery_service.services.approval_authorization.httpx.Client", FakeClient):
            with self.assertRaisesRegex(ValueError, "业务返回失败"):
                runtime._post_json("http://example/api", {})


if __name__ == "__main__":
    unittest.main()
