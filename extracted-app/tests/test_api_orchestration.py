import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from recovery_service.api.schemas.api_orchestration import ConnectorPayload, SqlApiPayload
from recovery_service.services.api_orchestration import (
    _execute_node,
    _load_context,
    _node_request_summary,
    _render,
    _render_mapping,
    _resolve_ready_nodes,
    _selected_outgoing_edges,
    _store_context,
    _ensure_read_only_sql,
    _validate_graph,
    _validate_params,
    invoke_connector,
    invoke_sql_api,
)


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HttpClient:
    response = _Response()
    request_args = None

    def __init__(self, **kwargs):
        self.options = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, *args, **kwargs):
        type(self).request_args = (args, kwargs)
        return type(self).response


class _Cursor:
    description = (("id",), ("name",))
    rowcount = 1

    def __init__(self):
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.executed = (sql, params)

    def fetchmany(self, limit):
        return [{"id": 7, "name": "测试"}][:limit]


class _Connection:
    def __init__(self):
        self.cursor_value = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_value


class ApiOrchestrationTests(unittest.TestCase):
    def connector(self, **overrides):
        values = {
            "enabled": True,
            "headers": {},
            "query": {},
            "body_template": {"name": "{{ input.name }}"},
            "auth_type": "none",
            "auth_name": None,
            "auth_secret_enc": "",
            "method": "POST",
            "url": "http://mock.local/execute",
            "timeout_seconds": 5,
            "success_statuses": [200],
            "success_path": None,
            "success_value": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_connector_payload_rejects_unknown_method(self):
        with self.assertRaises(ValidationError):
            ConnectorPayload(name="bad", method="TRACE", url="http://mock.local")

    def test_connector_payload_accepts_dynamic_bearer_without_persisted_secret(self):
        payload = ConnectorPayload(
            name="dynamic",
            method="GET",
            url="http://mock.local/items",
            auth_type="dynamic_bearer",
            auth_name="session.token",
        )
        self.assertEqual(payload.auth_type, "dynamic_bearer")
        self.assertEqual(payload.auth_name, "session.token")
        self.assertIsNone(payload.auth_secret)

    def test_sql_payload_rejects_invalid_slug(self):
        with self.assertRaises(ValidationError):
            SqlApiPayload(name="bad", slug="A B", connection_id=uuid.uuid4(), sql_text="SELECT 1")

    def test_template_mapping_preserves_typed_full_expression(self):
        context = {"input": {"count": 3}, "nodes": {"first": {"body": {"id": 8}}}}
        rendered = _render({"count": "{{ input.count }}", "id": "R-{{ nodes.first.body.id }}"}, context)
        self.assertEqual(rendered, {"count": 3, "id": "R-8"})

    def test_mapping_node_extracts_renames_and_composes_typed_values(self):
        context = {
            "input": {"user": {"id": 7, "active": True, "nickname": None}},
            "nodes": {"http-1": {"body": {"items": [{"id": 1}, {"id": 2}]}}},
        }
        node = {
            "id": "mapping-1",
            "type": "mapping",
            "config": {
                "missing_policy": "error",
                "template": {
                    "renamed_id": "{{ input.user.id }}",
                    "profile": {
                        "enabled": "{{ input.user.active }}",
                        "nickname": "{{ input.user.nickname }}",
                    },
                    "combined": ["fixed", "{{ nodes.http-1.body.items }}"],
                    "label": "user-{{ input.user.id }}-{{ input.user.active }}",
                },
            },
        }
        output = _execute_node(None, node, context)
        self.assertEqual(output["renamed_id"], 7)
        self.assertIs(output["profile"]["enabled"], True)
        self.assertIsNone(output["profile"]["nickname"])
        self.assertEqual(output["combined"], ["fixed", [{"id": 1}, {"id": 2}]])
        self.assertEqual(output["label"], "user-7-true")

        context["nodes"]["mapping-1"] = output
        self.assertEqual(_render({"id": "{{ nodes.mapping-1.renamed_id }}"}, context), {"id": 7})
        summary = _node_request_summary(node, context)
        self.assertEqual(summary["missing_policy"], "error")
        self.assertEqual(summary["referenced_values"]["input.user.id"], 7)

    def test_mapping_node_distinguishes_missing_path_from_present_null(self):
        context = {"input": {"present": None}, "nodes": {}}
        self.assertIsNone(_render_mapping("{{ input.present }}", context))
        with self.assertRaisesRegex(ValueError, "input.missing"):
            _render_mapping({"value": "{{ input.missing }}"}, context)
        self.assertEqual(
            _render_mapping(
                {"value": "{{ input.missing }}", "text": "before-{{ input.missing }}-after"},
                context,
                missing_policy="null",
            ),
            {"value": None, "text": "before--after"},
        )

    def test_mapping_node_configuration_is_validated_with_graph(self):
        def graph(mapping_config):
            nodes = [
                {"id": "start", "type": "start"},
                {"id": "mapping", "name": "字段整理", "type": "mapping", "config": mapping_config},
                {"id": "end", "type": "end"},
            ]
            edges = [{"source": "start", "target": "mapping"}, {"source": "mapping", "target": "end"}]
            return nodes, edges

        _validate_graph(*graph({"template": {"id": "{{ input.id }}"}, "missing_policy": "error"}))
        invalid = [
            {"template": "{{ input.id }}"},
            {"template": {}, "missing_policy": "ignore"},
            {"template": {"id": "{{ unknown.id }}"}},
            {"template": {"id": "{{ input.id | upper }}"}},
        ]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                _validate_graph(*graph(config))

    def test_mapping_node_is_available_in_workflow_designer(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")
        self.assertIn('data-orchestration-add-node="mapping"', ui)
        self.assertIn('data-api-node-json="template"', ui)
        self.assertIn('data-api-node-field="missing_policy"', ui)
        self.assertIn('data-api-workflow-edge=', ui)
        self.assertIn('data-api-workflow-delete-edge', ui)
        self.assertIn('await selectApiRun(apiOrchestrationSelectedRunId)', ui)
        self.assertIn('const businessIndex = apiOrchestrationDraft.nodes.filter', ui)
        self.assertIn('id="apiWorkflowNodeLibrary"', ui)
        self.assertIn('id="apiWorkflowNodeSearch"', ui)
        self.assertIn('id="apiWorkflowSearch"', ui)
        self.assertIn('id="apiWorkflowZoomOutBtn"', ui)
        self.assertIn('id="apiWorkflowZoomInBtn"', ui)
        self.assertIn('id="apiWorkflowFitBtn"', ui)
        self.assertIn('id="apiWorkflowInspectorPanel"', ui)
        self.assertIn('/ apiOrchestrationCanvasScale', ui)
        self.assertIn('height: min(680px, calc(100vh - 340px));\n      min-height: 420px;', ui)

    def test_connector_uses_request_workbench_without_changing_api_contract(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")
        self.assertIn('class="connector-workbench"', ui)
        self.assertIn('id="apiConnectorSearch"', ui)
        self.assertIn('data-api-connector-editor-tab="params"', ui)
        self.assertIn('data-api-connector-editor-tab="auth"', ui)
        self.assertIn('data-api-connector-editor-tab="headers"', ui)
        self.assertIn('data-api-connector-editor-tab="body"', ui)
        self.assertIn('data-api-connector-editor-tab="input"', ui)
        self.assertIn('data-api-connector-editor-tab="success"', ui)
        self.assertIn('id="apiConnectorResponseMeta"', ui)
        self.assertIn('grid-template-rows: auto auto 39px minmax(96px, 1fr) minmax(130px, 1fr) auto;', ui)
        self.assertIn('$("apiConnectorTestBtn").disabled = !apiOrchestrationSelectedConnectorId', ui)
        self.assertIn('button.textContent = "发送中"', ui)
        self.assertIn('/api/v1/api-orchestration/connectors/${apiOrchestrationSelectedConnectorId}/test', ui)
        self.assertIn('headers: readApiConnectorKeyValueEditor("headers")', ui)
        self.assertIn('query: readApiConnectorKeyValueEditor("query")', ui)
        self.assertIn('body_template: parseApiOrchestrationJson("apiConnectorBody", null)', ui)
        self.assertIn('setApiConnectorJsonEditorValue("apiConnectorBody", item.body_template === null || item.body_template === undefined ? "null"', ui)
        self.assertIn('class="connector-kv-head"><span>Key</span><span>Value</span><span>Description</span>', ui)
        self.assertIn('data-connector-kv-bulk-toggle="query"', ui)
        self.assertIn('data-connector-kv-bulk-toggle="headers"', ui)
        self.assertIn('function toggleApiConnectorBulkEditor(kind)', ui)
        self.assertIn('存在重复 Key', ui)
        self.assertIn('const separator = item.line.indexOf(":")', ui)
        self.assertIn('data-connector-json-beautify="apiConnectorBody"', ui)
        self.assertIn('data-connector-json-beautify="apiConnectorTestInput"', ui)
        self.assertIn('function syncApiConnectorJsonEditor(id)', ui)
        self.assertIn('function beautifyApiConnectorJson(id)', ui)
        self.assertIn('class="connector-raw-pane" data-api-connector-editor-pane="body"', ui)
        self.assertIn('.connector-raw-pane .connector-code-editor', ui)

    def test_workflow_and_sql_api_use_dedicated_workbenches(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")
        self.assertIn('id="apiWorkflowRunInputBtn"', ui)
        self.assertIn('id="apiWorkflowRunInputPanel"', ui)
        self.assertIn('function setApiWorkflowRunInput(open)', ui)
        self.assertIn('id="apiWorkflowAutoLayoutBtn"', ui)
        self.assertIn('function autoLayoutApiWorkflow()', ui)
        self.assertIn('function beginApiWorkflowLink(event, sourceId)', ui)
        self.assertIn('function moveApiWorkflowLink(event)', ui)
        self.assertIn('function endApiWorkflowLink()', ui)
        self.assertIn('function apiWorkflowCanConnect(sourceId, targetId)', ui)
        self.assertIn('.orchestration-edge.preview', ui)
        self.assertIn('.orchestration-node.connect-target', ui)
        self.assertIn('markApiWorkflowDirty()', ui)
        self.assertIn('id="apiWorkflowListMeta"', ui)
        self.assertIn('class="orchestration-node-summary"', ui)
        self.assertIn('未绑定连接器', ui)
        self.assertIn('未绑定 SQL API', ui)
        self.assertIn('class="api-sql-workbench"', ui)
        self.assertIn('id="apiSqlSearch"', ui)
        self.assertIn('class="api-sql-script-tabs"', ui)
        self.assertIn('class="api-sql-bottom-grid"', ui)
        self.assertIn('id="apiSqlScriptTabName"', ui)
        self.assertIn('id="apiSqlScriptTabMeta"', ui)
        self.assertIn('apiSqlExpandedConnections', ui)
        self.assertIn('data-api-sql-connection=', ui)
        self.assertIn('function updateApiSqlWorkspaceIdentity()', ui)
        self.assertIn('id="apiSqlRouteSlug"', ui)
        self.assertIn('data-api-sql-editor-tab="sql"', ui)
        self.assertIn('data-api-sql-editor-tab="schema"', ui)
        self.assertIn('id="apiSqlTextLineNumbers"', ui)
        self.assertIn('id="apiSqlSchemaLineNumbers"', ui)
        self.assertIn('function showApiSqlEditorTab(tab)', ui)
        self.assertIn('function setApiSqlResponseMeta(text, state = "")', ui)
        self.assertIn('event.key === "Enter" && (event.ctrlKey || event.metaKey)', ui)
        self.assertIn('title="运行已保存的 SQL API（Ctrl+Enter）"', ui)
        self.assertIn('button.textContent = "运行中"', ui)
        self.assertNotIn('apiSqlFormTitle', ui)

    def test_workflow_node_click_and_drag_are_separated(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")
        pointerdown_start = ui.index('$("apiWorkflowCanvas")?.addEventListener("pointerdown"')
        pointerdown_end = ui.index('document.addEventListener("pointermove"', pointerdown_start)
        pointerdown_handler = ui[pointerdown_start:pointerdown_end]

        self.assertNotIn("apiOrchestrationInspectorOpen = true", pointerdown_handler)
        self.assertIn("const apiWorkflowNodeDragThreshold = 5", ui)
        self.assertIn("Math.hypot(deltaX, deltaY) < apiWorkflowNodeDragThreshold", ui)
        self.assertIn('apiOrchestrationSuppressNodeClick = { id: apiOrchestrationDrag.id, until: Date.now() + 500 }', ui)
        self.assertIn("openApiWorkflowInspector(); renderApiWorkflowCanvas();", ui)

    def test_workflow_surfaces_and_messages_have_explicit_lifecycle(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")

        self.assertIn('class="message orchestration-workflow-message"', ui)
        self.assertNotIn('el.className = `message ${type}`', ui)
        self.assertIn('el.classList.remove("ok", "error")', ui)
        self.assertIn('if (text && type === "ok")', ui)
        self.assertIn('}, 4000)', ui)
        self.assertIn("function openApiWorkflowInspector()", ui)
        self.assertIn("setApiWorkflowNodeLibrary(false);", ui)
        self.assertIn("setApiWorkflowRunInput(false);", ui)
        self.assertIn('apiOrchestrationSelectedNodeId = ""', ui)
        self.assertIn("apiOrchestrationInspectorOpen = false", ui)
        self.assertIn("window.requestAnimationFrame(fitApiWorkflowCanvas)", ui)

    def test_offline_canvases_use_five_pixel_drag_thresholds(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")

        self.assertIn("const dataPlatformNodeDragThreshold = 5", ui)
        self.assertIn("Math.hypot(deltaX, deltaY) < dataPlatformNodeDragThreshold", ui)
        self.assertIn("const dataChangeNodeDragThreshold = 5", ui)
        self.assertIn("Math.hypot(dx, dy) < dataChangeNodeDragThreshold", ui)

    def test_workflow_designer_has_visual_data_mapping_and_safe_node_test(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")
        self.assertIn('id="apiWorkflowDataPicker"', ui)
        self.assertIn('id="apiWorkflowDataSourceList"', ui)
        self.assertIn('data-api-inspector-mode="visual"', ui)
        self.assertIn('data-api-open-picker="map"', ui)
        self.assertIn('data-api-test-node', ui)
        self.assertIn('function apiWorkflowDataSources(nodeId)', ui)
        self.assertIn('function apiWorkflowResolveForTest(value, context)', ui)
        self.assertIn('function testApiWorkflowNode()', ui)
        self.assertIn('node.config.output_schema = apiWorkflowInferSchema(result)', ui)
        self.assertIn('apiOrchestrationNodeSamples[String(node.id)] = result', ui)
        self.assertIn('apiWorkflowRedact(result)', ui)
        self.assertIn('<option value="dynamic_bearer">Bearer Token（流程输入）</option>', ui)

    def test_workflow_node_editor_uses_n8n_calibrated_three_pane_mapping(self):
        ui = Path("src/recovery_service/static/ui.html").read_text(encoding="utf-8")
        lucide = Path("src/recovery_service/static/vendor/lucide.min.js")
        license_text = Path("src/recovery_service/static/vendor/LUCIDE_LICENSE.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn('src="/static/vendor/lucide.min.js"', ui)
        self.assertTrue(lucide.is_file())
        self.assertGreater(lucide.stat().st_size, 100_000)
        self.assertIn("ISC License", license_text)
        self.assertIn('class="workflow-node-workbench"', ui)
        self.assertIn('data-api-input-mode="schema"', ui)
        self.assertIn('data-api-output-mode="table"', ui)
        self.assertIn('data-api-output-mode="json"', ui)
        self.assertIn('data-api-output-mode="schema"', ui)
        self.assertIn('data-api-value-mode="fixed"', ui)
        self.assertIn('data-api-value-mode="expression"', ui)
        self.assertIn('draggable="true" class="workflow-source-field', ui)
        self.assertIn('application/x-workflow-path', ui)
        self.assertIn('function applyApiWorkflowSourceToMap(sourcePath, mapPath)', ui)
        self.assertIn('function apiWorkflowResolvePreview(value)', ui)
        self.assertIn('id="apiWorkflowInspectorBackBtn"', ui)
        self.assertIn('id="apiWorkflowInspectorTestBtn"', ui)
        self.assertIn('apiWorkflowRedact(sample)', ui)

    def test_http_connector_supports_bearer_and_business_success(self):
        _HttpClient.response = _Response(200, {"code": 0, "result": {"id": 9}})
        item = self.connector(auth_type="bearer", auth_secret_enc="secret-token", success_path="code", success_value=0)
        with patch("recovery_service.services.api_orchestration.httpx.Client", _HttpClient):
            result = invoke_connector(item, {"name": "张三"})
        self.assertTrue(result["success"])
        self.assertEqual(_HttpClient.request_args[1]["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(_HttpClient.request_args[1]["json"], {"name": "张三"})

    def test_http_connector_supports_dynamic_bearer_from_flow_input(self):
        _HttpClient.response = _Response(200, {"items": [1]})
        item = self.connector(auth_type="dynamic_bearer", auth_name="session.token")
        with patch("recovery_service.services.api_orchestration.httpx.Client", _HttpClient):
            result = invoke_connector(item, {"name": "张三", "session": {"token": "runtime-token"}})
        self.assertTrue(result["success"])
        self.assertEqual(_HttpClient.request_args[1]["headers"]["Authorization"], "Bearer runtime-token")
        self.assertNotIn("runtime-token", item.auth_secret_enc)

        with patch("recovery_service.services.api_orchestration.httpx.Client", _HttpClient):
            with self.assertRaisesRegex(ValueError, "session.token"):
                invoke_connector(item, {"name": "张三"})

    def test_dynamic_url_cannot_change_target_host(self):
        _HttpClient.response = _Response(200, {"ok": True})
        with patch("recovery_service.services.api_orchestration.httpx.Client", _HttpClient):
            with self.assertRaisesRegex(ValueError, "目标主机"):
                invoke_connector(self.connector(url="http://{{ input.host }}/private"), {"host": "other.local"})

    def test_http_connector_surfaces_http_and_invalid_json_failures(self):
        _HttpClient.response = _Response(500, ValueError("invalid json"), "upstream failed")
        with patch("recovery_service.services.api_orchestration.httpx.Client", _HttpClient):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                invoke_connector(self.connector(), {})

    def test_connector_payload_rejects_plaintext_authorization_header(self):
        with self.assertRaises(ValidationError):
            ConnectorPayload(name="bad", method="GET", url="http://mock.local", headers={"Authorization": "secret"})

    def test_graph_rejects_cycle_duplicate_self_loop_and_unreachable_node(self):
        nodes = [
            {"id": "start", "type": "start"},
            {"id": "task", "type": "http"},
            {"id": "end", "type": "end"},
        ]
        _validate_graph(nodes, [{"source": "start", "target": "task"}, {"source": "task", "target": "end"}])
        invalid_edges = [
            [{"source": "start", "target": "start"}],
            [{"source": "start", "target": "task"}, {"source": "start", "target": "task"}],
            [{"source": "start", "target": "task"}, {"source": "task", "target": "start"}],
            [{"source": "start", "target": "end"}],
        ]
        for edges in invalid_edges:
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                _validate_graph(nodes, edges)

    def test_parallel_join_waits_for_every_resolved_incoming_edge(self):
        nodes = {key: {"id": key, "type": "start" if key == "start" else "end"} for key in ("start", "left", "right", "join")}
        edges = [
            {"source": "start", "target": "left"}, {"source": "start", "target": "right"},
            {"source": "left", "target": "join"}, {"source": "right", "target": "join"},
        ]
        incoming = {key: [] for key in nodes}; outgoing = {key: [] for key in nodes}
        for index, edge in enumerate(edges):
            incoming[edge["target"]].append(index); outgoing[edge["source"]].append(index)
        state = {}; pending = set(nodes)
        ready, skipped = _resolve_ready_nodes(nodes, incoming, outgoing, state, pending)
        self.assertEqual((ready, skipped), (["start"], []))
        state.update({0: True, 1: True})
        ready, _ = _resolve_ready_nodes(nodes, incoming, outgoing, state, pending)
        self.assertEqual(ready, ["left", "right"])
        state[2] = True
        ready, _ = _resolve_ready_nodes(nodes, incoming, outgoing, state, pending)
        self.assertEqual(ready, [])
        state[3] = True
        ready, _ = _resolve_ready_nodes(nodes, incoming, outgoing, state, pending)
        self.assertEqual(ready, ["join"])

    def test_condition_uses_matching_branch_and_falls_back_to_default(self):
        edges = [
            {"source": "condition", "target": "yes", "branch": "true"},
            {"source": "condition", "target": "no", "branch": "false"},
            {"source": "condition", "target": "fallback", "branch": "default"},
        ]
        node = {"type": "condition"}
        self.assertEqual(_selected_outgoing_edges(node, edges, [0, 1, 2], {"matched": True}), {0})
        self.assertEqual(_selected_outgoing_edges(node, edges[1:], [0, 1], {"matched": True}), {1})

    def test_context_is_encrypted_and_sensitive_values_are_not_plain_json_fields(self):
        context = {"input": {"token": "secret", "name": "张三"}, "nodes": {}}
        with patch("recovery_service.services.api_orchestration.get_settings", return_value=SimpleNamespace(credential_encryption_key="unit-test-key")):
            stored = _store_context(context)
            self.assertEqual(set(stored), {"_encrypted"})
            self.assertNotIn("secret", stored["_encrypted"])
            self.assertEqual(_load_context(stored), context)

    def test_sql_parameters_are_bound_and_rows_are_limited(self):
        profile = SimpleNamespace(engine="doris", host="127.0.0.1", port=9030, username="u", password_enc="p", database="db")
        item = SimpleNamespace(enabled=True, mode="read", input_schema={"properties": {"id": {"type": "integer"}}, "required": ["id"]}, connection_id=uuid.uuid4(), database="db", sql_text="SELECT id,name FROM t WHERE id=:id", max_rows=1, timeout_seconds=5)
        session = SimpleNamespace(get=lambda model, item_id: profile)
        connection = _Connection()
        with patch("recovery_service.services.api_orchestration.pymysql.connect", return_value=connection):
            result = invoke_sql_api(session, item, {"id": 7})
        self.assertEqual(connection.cursor_value.executed, ("SELECT id,name FROM t WHERE id=%(id)s", {"id": 7}))
        self.assertEqual(result["rows"], [{"id": 7, "name": "测试"}])

    def test_sql_validation_rejects_missing_unknown_and_wrong_type(self):
        schema = {"properties": {"id": {"type": "integer"}}, "required": ["id"]}
        for params in ({}, {"id": "7"}, {"id": 7, "extra": "x"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                _validate_params(schema, params)

    def test_write_sql_requires_explicit_permission(self):
        item = SimpleNamespace(enabled=True, mode="write")
        with self.assertRaises(PermissionError):
            invoke_sql_api(SimpleNamespace(), item, {}, allow_write=False)

    def test_read_sql_rejects_dml_hidden_behind_with(self):
        _ensure_read_only_sql("WITH data AS (SELECT 1) SELECT * FROM data")
        for sql in (
            "UPDATE users SET name='x'",
            "WITH ids AS (SELECT 1) DELETE FROM users",
            "SELECT * FROM users INTO OUTFILE '/tmp/users.csv'",
            "SELECT * FROM users FOR UPDATE",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                _ensure_read_only_sql(sql)


if __name__ == "__main__":
    unittest.main()
