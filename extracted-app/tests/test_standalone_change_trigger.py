import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import DataPlatformNode
from recovery_service.services.data_change_trigger import _is_standalone_trigger_reference
from recovery_service.services.data_platform import (
    _build_standalone_trigger_snapshot,
    _normalize_component_task_config,
    _validate_change_trigger_graph,
    _validate_workflow_graph,
)


class StandaloneChangeTriggerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        DataPlatformNode.__table__.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.connection_id = uuid.uuid4()

    def tearDown(self):
        self.engine.dispose()

    def test_snapshot_freezes_action_task_and_connects_monitor(self):
        sql_task = DataPlatformNode(
            id=uuid.uuid4(),
            name="SQL action",
            revision=3,
            node_type="doris_sql",
            config={
                "connection_id": str(self.connection_id),
                "database": "DWH_TEST",
                "sql": "SELECT 1",
                "limit": 200,
            },
            status="active",
        )
        trigger = DataPlatformNode(
            id=uuid.uuid4(),
            name="Customer change",
            revision=2,
            node_type="change_trigger",
            config={
                "source_type": "direct",
                "connection_id": str(self.connection_id),
                "database": "ODS",
                "conditions": [
                    {
                        "id": "rows",
                        "metric_type": "row_count",
                        "table": "customer",
                        "operator": "changed",
                    }
                ],
                "action_nodes": [
                    {
                        "key": "sql_1",
                        "name": "SQL action",
                        "node_type": "doris_sql",
                        "config": {"task_definition_id": str(sql_task.id)},
                    }
                ],
                "action_edges": [{"source": "monitor", "target": "sql_1"}],
                "graph_schema_version": 2,
            },
            status="active",
        )
        with self.factory() as session:
            session.add_all([sql_task, trigger])
            session.commit()
            snapshot = _build_standalone_trigger_snapshot(session, trigger)

        self.assertEqual([item["key"] for item in snapshot["nodes"]], ["monitor", "sql_1"])
        self.assertEqual(snapshot["edges"], [{"source": "monitor", "target": "sql_1"}])
        action = snapshot["nodes"][1]
        self.assertEqual(action["config"]["task_definition_revision"], 3)
        self.assertEqual(action["config"]["sql"], "SELECT 1")
        self.assertTrue(snapshot["nodes"][0]["config"]["standalone_deployment_monitor"])

    def test_version_two_rejects_orphan_action_node(self):
        trigger = DataPlatformNode(
            id=uuid.uuid4(),
            name="Orphan",
            revision=1,
            node_type="change_trigger",
            config={
                "source_type": "direct",
                "connection_id": str(self.connection_id),
                "database": "ODS",
                "conditions": [{"id": "rows", "metric_type": "row_count", "table": "t"}],
                "action_nodes": [
                    {"key": "sql_1", "name": "SQL 1", "node_type": "doris_sql", "config": {}},
                    {"key": "sql_2", "name": "SQL 2", "node_type": "doris_sql", "config": {}},
                ],
                "action_edges": [{"source": "monitor", "target": "sql_1"}],
                "graph_schema_version": 2,
            },
            status="active",
        )
        with self.factory() as session:
            session.add(trigger)
            session.commit()
            with self.assertRaisesRegex(ValueError, "游离节点.*SQL 2"):
                _build_standalone_trigger_snapshot(session, trigger)

    def test_version_two_rejects_edge_into_monitor(self):
        trigger = DataPlatformNode(
            id=uuid.uuid4(),
            name="Invalid monitor",
            revision=1,
            node_type="change_trigger",
            config={
                "source_type": "direct",
                "connection_id": str(self.connection_id),
                "database": "ODS",
                "conditions": [{"id": "rows", "metric_type": "row_count", "table": "t"}],
                "action_nodes": [{"key": "sql", "name": "SQL", "node_type": "doris_sql", "config": {}}],
                "action_edges": [{"source": "sql", "target": "monitor"}],
                "graph_schema_version": 2,
            },
            status="active",
        )
        with self.factory() as session:
            session.add(trigger)
            session.commit()
            with self.assertRaisesRegex(ValueError, "监控节点必须作为流程首节点"):
                _build_standalone_trigger_snapshot(session, trigger)

    def test_version_two_rejects_cycle_when_saving(self):
        config = {
            "source_type": "direct",
            "connection_id": str(self.connection_id),
            "database": "ODS",
            "conditions": [{"id": "rows", "metric_type": "row_count", "table": "t"}],
            "action_nodes": [
                {"key": "a", "name": "A", "node_type": "doris_sql", "config": {}},
                {"key": "b", "name": "B", "node_type": "doris_sql", "config": {}},
            ],
            "action_edges": [
                {"source": "monitor", "target": "a"},
                {"source": "a", "target": "b"},
                {"source": "b", "target": "a"},
            ],
            "graph_schema_version": 2,
        }
        with self.assertRaisesRegex(ValueError, "不能形成环路"):
            _normalize_component_task_config("change_trigger", config)

    def test_trigger_condition_normalization_clears_hidden_values(self):
        config = {
            "source_type": "direct",
            "connection_id": str(self.connection_id),
            "database": "ODS",
            "conditions": [{
                "id": "rows",
                "metric_type": "row_count",
                "table": "CUSTOMER",
                "column": "STALE_COLUMN",
                "operator": "changed",
                "threshold": "100",
                "sql": "SELECT 1",
            }],
        }
        normalized = _normalize_component_task_config("change_trigger", config)
        condition = normalized["conditions"][0]
        self.assertIsNone(condition["column"])
        self.assertIsNone(condition["threshold"])
        self.assertIsNone(condition["sql"])

    def test_max_metric_requires_column(self):
        config = {
            "source_type": "direct",
            "connection_id": str(self.connection_id),
            "database": "ODS",
            "conditions": [{
                "metric_type": "max",
                "table": "CUSTOMER",
                "operator": "changed",
            }],
        }
        with self.assertRaisesRegex(ValueError, "必须选择指标字段"):
            _normalize_component_task_config("change_trigger", config)

    def test_threshold_operator_requires_threshold(self):
        config = {
            "source_type": "direct",
            "connection_id": str(self.connection_id),
            "database": "ODS",
            "conditions": [{
                "metric_type": "row_count",
                "table": "CUSTOMER",
                "operator": "increase_by",
            }],
        }
        with self.assertRaisesRegex(ValueError, "必须填写阈值"):
            _normalize_component_task_config("change_trigger", config)

    def test_metric_rejects_unrelated_operator(self):
        config = {
            "source_type": "direct",
            "connection_id": str(self.connection_id),
            "database": "ODS",
            "conditions": [{
                "metric_type": "schema_signature",
                "table": "CUSTOMER",
                "operator": "greater_than",
                "threshold": "1",
            }],
        }
        with self.assertRaisesRegex(ValueError, "不支持比较规则"):
            _normalize_component_task_config("change_trigger", config)

    def test_scalar_sql_requires_query_and_drops_table_fields(self):
        config = {
            "source_type": "direct",
            "connection_id": str(self.connection_id),
            "database": "ODS",
            "conditions": [{
                "metric_type": "scalar_sql",
                "table": "OPTIONAL_TABLE",
                "column": "STALE_COLUMN",
                "operator": "became_true",
                "sql": "SELECT COUNT(*) > 0 FROM CUSTOMER",
            }],
        }
        normalized = _normalize_component_task_config("change_trigger", config)
        condition = normalized["conditions"][0]
        self.assertIsNone(condition["column"])
        self.assertIsNone(condition["threshold"])
        self.assertEqual(condition["sql"], "SELECT COUNT(*) > 0 FROM CUSTOMER")

    def test_legacy_graph_still_connects_root_actions(self):
        sql_task = DataPlatformNode(
            id=uuid.uuid4(),
            name="Legacy SQL",
            revision=1,
            node_type="doris_sql",
            config={"connection_id": str(self.connection_id), "sql": "SELECT 1"},
            status="active",
        )
        trigger = DataPlatformNode(
            id=uuid.uuid4(),
            name="Legacy trigger",
            revision=1,
            node_type="change_trigger",
            config={
                "source_type": "direct",
                "connection_id": str(self.connection_id),
                "database": "ODS",
                "conditions": [{"id": "rows", "metric_type": "row_count", "table": "t"}],
                "action_nodes": [
                    {
                        "key": "sql",
                        "name": "Legacy SQL",
                        "node_type": "doris_sql",
                        "config": {"task_definition_id": str(sql_task.id)},
                    }
                ],
                "action_edges": [],
            },
            status="active",
        )
        with self.factory() as session:
            session.add_all([sql_task, trigger])
            session.commit()
            snapshot = _build_standalone_trigger_snapshot(session, trigger)
        self.assertEqual(snapshot["edges"], [{"source": "monitor", "target": "sql"}])

    def test_offline_workflow_rejects_invalid_edges(self):
        nodes = [{"key": "a"}, {"key": "b"}]
        with self.assertRaisesRegex(ValueError, "不存在的节点"):
            _validate_workflow_graph(nodes, [{"source": "a", "target": "missing"}])
        with self.assertRaisesRegex(ValueError, "重复连线"):
            _validate_workflow_graph(nodes, [{"source": "a", "target": "b"}, {"source": "a", "target": "b"}])
        with self.assertRaisesRegex(ValueError, "不能形成环路"):
            _validate_workflow_graph(nodes, [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}])
        with self.assertRaisesRegex(ValueError, "游离节点.*C"):
            _validate_workflow_graph(
                [{"key": "a", "name": "A"}, {"key": "b", "name": "B"}, {"key": "c", "name": "C"}],
                [{"source": "a", "target": "b"}],
            )
        _validate_workflow_graph([{"key": "only", "name": "单节点"}], [])
        _validate_workflow_graph(
            [
                {"key": "a", "name": "A"},
                {"key": "b", "name": "B"},
                {"key": "trigger", "name": "触发器", "node_type": "change_trigger", "config": {"standalone_trigger": True}},
            ],
            [{"source": "a", "target": "b"}],
        )

    def test_empty_action_graph_cannot_publish(self):
        trigger = DataPlatformNode(
            id=uuid.uuid4(),
            name="Empty",
            revision=1,
            node_type="change_trigger",
            config={
                "source_type": "direct",
                "connection_id": str(self.connection_id),
                "database": "ODS",
                "conditions": [{"id": "rows", "metric_type": "row_count", "table": "t"}],
                "action_nodes": [],
            },
            status="active",
        )
        with self.factory() as session:
            with self.assertRaisesRegex(ValueError, "至少需要一个执行节点"):
                _build_standalone_trigger_snapshot(session, trigger)

    def test_nested_trigger_action_is_rejected(self):
        nested = DataPlatformNode(
            id=uuid.uuid4(),
            name="Nested",
            revision=1,
            node_type="change_trigger",
            config={},
            status="active",
        )
        trigger = DataPlatformNode(
            id=uuid.uuid4(),
            name="Parent",
            revision=1,
            node_type="change_trigger",
            config={
                "source_type": "direct",
                "connection_id": str(self.connection_id),
                "database": "ODS",
                "conditions": [{"id": "rows", "metric_type": "row_count", "table": "t"}],
                "action_nodes": [
                    {
                        "key": "nested",
                        "name": "Nested",
                        "node_type": "change_trigger",
                        "config": {"task_definition_id": str(nested.id)},
                    }
                ],
            },
            status="active",
        )
        with self.factory() as session:
            session.add_all([nested, trigger])
            session.commit()
            with self.assertRaisesRegex(ValueError, "不支持以下节点类型"):
                _build_standalone_trigger_snapshot(session, trigger)

    def test_offline_standalone_reference_cannot_have_external_edges(self):
        nodes = [
            {
                "key": "trigger",
                "node_type": "change_trigger",
                "config": {"standalone_trigger": True},
            },
            {"key": "sql", "node_type": "doris_sql", "config": {}},
        ]
        with self.assertRaisesRegex(ValueError, "不能在离线画布连接外部"):
            _validate_change_trigger_graph(nodes, [{"source": "trigger", "target": "sql"}])

    def test_deployment_monitor_is_not_treated_as_offline_reference(self):
        node = {
            "node_type": "change_trigger",
            "config": {
                "standalone_trigger": True,
                "standalone_deployment_monitor": True,
            },
        }
        self.assertFalse(_is_standalone_trigger_reference(node))


if __name__ == "__main__":
    unittest.main()
