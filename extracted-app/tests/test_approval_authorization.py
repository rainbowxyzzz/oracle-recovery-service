import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from recovery_service.core.models.task import (
    ApprovalAuthorizationConfig,
    ApprovalAuthorizationRun,
    ApprovalAuthorizationStepLog,
    Base,
    DatabaseConnectionProfile,
)
from recovery_service.services.approval_authorization import _Runtime, _extract_department_name, _normalized_config, _run_full_sync


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
        self.assertEqual(log.request_data["headers"]["token"], "toke***-123")

    def test_failed_table_schema_lookup_keeps_sql_trace(self):
        runtime = self._runtime()

        def fake_query(sql, params):
            runtime._last_sql_text = sql
            runtime._last_sql_params = {"params": list(params)}
            runtime._last_sql_result = {"rows": [], "row_count": 0}
            return []

        with patch.object(runtime, "_query", side_effect=fake_query):
            with self.assertRaises(ValueError):
                runtime.execute_step(
                    "table_schema_lookup",
                    {"data_items": [{"datatitle": "T_MISSING", "dataLevel": "1"}]},
                    "FLOW001",
                )

        log = self.session.query(ApprovalAuthorizationStepLog).one()
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.apply_flow_id, "FLOW001")
        self.assertIn("information_schema.tables", log.sql_text)
        self.assertEqual(log.sql_params["params"], ["DWD_%", "T_MISSING"])
        self.assertEqual(log.sql_result["row_count"], 0)

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

    def test_auto_watch_retries_only_audit_status_after_import_success(self):
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
        calls = []

        def fake_execute(self, step_key, context, apply_flow_id):
            calls.append(step_key)
            if step_key == "login":
                return {"workflow_token": "workflow-token"}
            if step_key == "todo_list":
                return {"apply_flow_ids": ["FLOW001"], "todo_rows": [{"id": "FLOW001", "auditStatus": 0}]}
            if step_key == "audit_status_update":
                return {"audit_status_updated": True}
            raise AssertionError(f"unexpected step {step_key}")

        with patch.object(_Runtime, "execute_step", fake_execute), patch(
            "recovery_service.services.approval_authorization.get_sync_session_factory",
            return_value=lambda: Session(self.engine, expire_on_commit=False),
        ):
            _run_full_sync(self.run_id, self.config_id, {"mode": "auto_watch"})

        self.assertEqual(calls, ["login", "todo_list", "audit_status_update"])
        self.session.expire_all()
        run = self.session.get(ApprovalAuthorizationRun, self.run_id)
        self.assertEqual(run.state, "success")
        self.assertEqual(run.success_count, 1)


if __name__ == "__main__":
    unittest.main()
