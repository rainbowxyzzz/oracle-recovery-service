import unittest
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import (
    Base,
    DataAutomationPipeline,
    DataPlatformNode,
    DataPlatformNodeRun,
    DataPlatformWorkflow,
    DataPlatformWorkflowRun,
    DataPlatformWorkflowVersion,
    DatabaseConnectionProfile,
)
from recovery_service.services.auth import AuthContext
from recovery_service.services.data_platform import (
    archive_doris_sql_collection,
    create_doris_sql_collection,
    create_node,
    get_doris_sql_collection,
    list_doris_sql_collections,
    list_node_runs,
    publish_doris_sql_collection,
    run_doris_sql_collection,
    run_queued_workflow,
    update_doris_sql_collection,
    update_node,
)


class DorisSqlCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.factory_patch = patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        )
        self.factory_patch.start()
        self.actor = AuthContext(
            user_id=None,
            username="admin",
            role="admin",
            auth_type="session",
            permissions={"actions": []},
        )
        self.connection_id = uuid.uuid4()
        with self.factory() as session:
            session.add(DatabaseConnectionProfile(
                id=self.connection_id,
                name="Doris 测试连接",
                engine="doris",
                host="127.0.0.1",
                port=9030,
                username="root",
                password_enc="",
            ))
            session.commit()
        self.tasks = [self._create_task(f"SQL-{index}", f"SELECT {index}") for index in range(1, 4)]

    def tearDown(self) -> None:
        self.factory_patch.stop()
        self.engine.dispose()

    def _create_task(self, name: str, sql: str):
        return create_node(
            name=name,
            node_type="doris_sql",
            description=f"{name} 说明",
            config={
                "connection_id": str(self.connection_id),
                "connection_name": "Doris 测试连接",
                "database": "DWD_TEST",
                "sql": sql,
                "limit": 200,
            },
            actor=self.actor,
        )

    def test_collection_create_order_update_and_ungrouped(self) -> None:
        created = create_doris_sql_collection({
            "name": "自然资源 DWD 集合",
            "description": "按顺序加工",
            "business_domain": "自然资源",
            "data_layer": "dwd",
            "tags": ["月度", "正式"],
            "default_connection_id": self.connection_id,
            "default_database": "DWD_TEST",
            "task_ids": [self.tasks[1].node_id, self.tasks[0].node_id],
        }, self.actor)

        self.assertEqual([row["task_id"] for row in created["members"]], [self.tasks[1].node_id, self.tasks[0].node_id])
        self.assertEqual(created["data_layer"], "DWD")
        listed = list_doris_sql_collections()
        self.assertEqual(listed["ungrouped_task_ids"], [self.tasks[2].node_id])

        updated = update_doris_sql_collection(created["collection_id"], {
            "task_ids": [self.tasks[0].node_id, self.tasks[2].node_id, self.tasks[1].node_id],
            "tags": ["月度"],
        }, self.actor)
        self.assertEqual(
            [row["task_id"] for row in updated["members"]],
            [self.tasks[0].node_id, self.tasks[2].node_id, self.tasks[1].node_id],
        )
        self.assertEqual(list_doris_sql_collections()["ungrouped_count"], 0)

        with self.assertRaisesRegex(ValueError, "不能在集合中重复"):
            update_doris_sql_collection(created["collection_id"], {
                "task_ids": [self.tasks[0].node_id, self.tasks[0].node_id],
            }, self.actor)

    def test_publish_freezes_revision_and_exposes_pipeline_reference(self) -> None:
        created = create_doris_sql_collection({
            "name": "生产集合",
            "task_ids": [self.tasks[0].node_id, self.tasks[1].node_id],
        }, self.actor)
        published = publish_doris_sql_collection(created["collection_id"], self.actor)
        online_id = published["online_version_id"]
        self.assertIsNotNone(online_id)

        update_node(self.tasks[0].node_id, {
            "config": {**self.tasks[0].config, "sql": "SELECT 100"},
        }, self.actor)
        with self.factory() as session:
            frozen = session.get(DataPlatformWorkflowVersion, online_id)
            self.assertEqual(frozen.nodes[0]["config"]["task_definition_revision"], 1)
            self.assertEqual(
                frozen.nodes[0]["config"]["task_definition_snapshot"]["config"]["sql"],
                "SELECT 1",
            )
            session.add(DataAutomationPipeline(
                id=uuid.uuid4(),
                name="自动化引用",
                standard_workflow_version_id=online_id,
                status="active",
            ))
            session.commit()

        refreshed = get_doris_sql_collection(created["collection_id"])
        self.assertEqual(refreshed["references"][0]["name"], "自动化引用")
        self.assertTrue(refreshed["references"][0]["current_online"])

        update_doris_sql_collection(created["collection_id"], {
            "task_ids": [self.tasks[0].node_id, self.tasks[1].node_id],
        }, self.actor)
        republished = publish_doris_sql_collection(created["collection_id"], self.actor)
        self.assertNotEqual(republished["online_version_id"], online_id)
        self.assertEqual(republished["online_version_no"], 2)

    def test_collection_run_is_serial_and_archive_keeps_tasks(self) -> None:
        created = create_doris_sql_collection({
            "name": "串行运行集合",
            "task_ids": [task.node_id for task in self.tasks],
        }, self.actor)
        with patch("recovery_service.workers.celery_app.celery_app.send_task") as send_task:
            send_task.return_value.id = "collection-run-1"
            queued = run_doris_sql_collection(created["collection_id"], channel="dev", actor=self.actor)
        calls: list[str] = []

        def execute_sql(_profile, *, database, sql, limit):
            calls.append(sql)
            return SimpleNamespace(
                message="ok", sql_type="SELECT", row_count=1, affected_rows=None,
                duration_ms=1, columns=[], rows=[{"value": 1}],
            )

        with patch("recovery_service.services.data_platform.execute_doris_sql", side_effect=execute_sql):
            run_queued_workflow(queued.run_id)

        self.assertEqual(calls, ["SELECT 1", "SELECT 2", "SELECT 3"])
        with self.factory() as session:
            run = session.get(DataPlatformWorkflowRun, queued.run_id)
            self.assertEqual(run.status, "succeeded")
            node_rows = session.query(DataPlatformNodeRun).filter_by(run_id=queued.run_id).all()
            for index, row in enumerate(node_rows):
                row.created_at = datetime(2026, 9, 4, 12, 0, 3 - index)
            session.commit()

        ordered_logs = list_node_runs(queued.run_id)
        self.assertEqual([row.node_name for row in ordered_logs], ["SQL-1", "SQL-2", "SQL-3"])

        archive_doris_sql_collection(created["collection_id"], self.actor)
        with self.factory() as session:
            workflow = session.get(DataPlatformWorkflow, created["collection_id"])
            self.assertEqual(workflow.status, "archived")
            self.assertEqual(
                session.query(DataPlatformNode).filter(DataPlatformNode.id.in_([task.node_id for task in self.tasks])).count(),
                3,
            )
        with self.assertRaisesRegex(KeyError, "SQL 集合不存在"):
            get_doris_sql_collection(created["collection_id"])


if __name__ == "__main__":
    unittest.main()
