import unittest

from recovery_service.services.data_change_trigger import _compare, _evaluate_conditions
from recovery_service.services.data_platform import (
    _execute_node,
    _execution_content_hash,
    _freeze_component_task_nodes,
    _reachable_nodes,
)


class DataChangeArchitectureTests(unittest.TestCase):
    def test_execution_hash_ignores_revision_identity_and_runtime_key(self) -> None:
        base = {
            "nodes": [
                {
                    "key": "sm4",
                    "node_type": "sm4_batch",
                    "x": 10,
                    "y": 20,
                    "config": {
                        "task_definition_id": "task-a",
                        "task_definition_revision": 1,
                        "task_definition_snapshot": {
                            "task_definition_id": "task-a",
                            "revision": 1,
                            "connection_id": "conn-1",
                            "database": "ODS",
                            "tables": [{"table_name": "customer", "columns": ["phone"]}],
                            "sm4_key_fingerprint": "old-key",
                        },
                    },
                }
            ],
            "edges": [],
            "schedule": {"schedule_type": "daily", "run_time": "02:00"},
        }
        changed_identity = {
            **base,
            "nodes": [
                {
                    **base["nodes"][0],
                    "x": 500,
                    "config": {
                        **base["nodes"][0]["config"],
                        "task_definition_id": "task-b",
                        "task_definition_revision": 9,
                        "task_definition_snapshot": {
                            **base["nodes"][0]["config"]["task_definition_snapshot"],
                            "task_definition_id": "task-b",
                            "revision": 9,
                            "sm4_key_fingerprint": "new-key",
                        },
                    },
                }
            ],
        }
        self.assertEqual(_execution_content_hash(base), _execution_content_hash(changed_identity))

    def test_execution_hash_changes_with_business_content(self) -> None:
        left = {"nodes": [{"key": "sql", "node_type": "doris_sql", "config": {"sql": "SELECT 1"}}], "edges": [], "schedule": {}}
        right = {"nodes": [{"key": "sql", "node_type": "doris_sql", "config": {"sql": "SELECT 2"}}], "edges": [], "schedule": {}}
        self.assertNotEqual(_execution_content_hash(left), _execution_content_hash(right))

    def test_condition_comparison_and_combination(self) -> None:
        config = {
            "condition_logic": "AND",
            "conditions": [
                {"id": "rows", "operator": "increase_by", "threshold": 10},
                {"id": "time", "operator": "increased"},
            ],
        }
        matched, results = _evaluate_conditions(config, {"rows": 100, "time": 5}, {"rows": 112, "time": 6})
        self.assertTrue(matched)
        self.assertTrue(all(item["matched"] for item in results))
        self.assertTrue(_compare(100, 120, "increase_percent", 20))

    def test_reachable_nodes_isolates_trigger_branch(self) -> None:
        edges = [
            {"source": "trigger_a", "target": "sync_a"},
            {"source": "trigger_b", "target": "sync_b"},
            {"source": "sync_a", "target": "sm4_a"},
        ]
        self.assertEqual(_reachable_nodes("trigger_a", edges), {"trigger_a", "sync_a", "sm4_a"})

    def test_historical_backfill_keeps_missing_task_reference(self) -> None:
        nodes = [
            {
                "key": "sm4",
                "node_type": "sm4_batch",
                "config": {"task_definition_id": "not-a-uuid"},
            }
        ]
        frozen = _freeze_component_task_nodes(
            None,
            nodes,
            preserve_existing=True,
            tolerate_missing=True,
        )
        self.assertEqual(frozen, nodes)

    def test_execution_hash_changes_with_trigger_policy(self) -> None:
        left = {
            "nodes": [
                {
                    "key": "trigger",
                    "node_type": "change_trigger",
                    "config": {"conditions": [{"id": "rows", "operator": "changed"}], "overlap_policy": "merge"},
                }
            ],
            "edges": [],
            "schedule": {},
        }
        right = {
            **left,
            "nodes": [
                {
                    **left["nodes"][0],
                    "config": {**left["nodes"][0]["config"], "overlap_policy": "queue"},
                }
            ],
        }
        self.assertNotEqual(_execution_content_hash(left), _execution_content_hash(right))

    def test_manual_incremental_sync_keeps_initial_watermark(self) -> None:
        import uuid
        from unittest.mock import patch

        connection_id = uuid.uuid4()
        profile = type("Profile", (), {"id": connection_id})()
        session = type("Session", (), {"get": lambda self, model, key: profile})()
        spec = {
            "node_type": "data_sync",
            "config": {
                "source_connection_id": str(connection_id),
                "target_connection_id": str(connection_id),
                "watermark_column": "id",
                "initial_watermark_value": 10,
            },
        }
        with patch("recovery_service.services.data_platform.execute_data_sync") as execute:
            execute.return_value = {"message": "ok"}
            _execute_node(session, spec, run=None)
        runtime_config = execute.call_args.args[2]
        self.assertEqual(runtime_config["initial_watermark_value"], 10)
        self.assertNotIn("watermark_value", runtime_config)


if __name__ == "__main__":
    unittest.main()
