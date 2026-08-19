import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import (
    DataPlatformComponentRun,
    DataPlatformComponentRunLog,
    DataPlatformComponentRunTable,
    DataPlatformNode,
    DatabaseConnectionProfile,
)
from recovery_service.services.data_platform import (
    _freeze_component_task_nodes,
    _is_mysql_out_of_sort_memory,
    _list_nodes_after_sort_memory_error,
    _normalize_component_task_config,
    _validate_component_task_bindings,
    create_node,
    list_component_runs,
    list_nodes,
    run_queued_component_task,
    run_component_task_once,
    submit_component_task_run,
    update_node,
)


class _FakeMySqlSortMemoryError:
    args = (1038, "Out of sort memory, consider increasing server sort buffer size")


class _FakeScalarResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _FakeListNodesSession:
    def __init__(self, node):
        self.node = node
        self.execute_calls = 0
        self.rollback_calls = 0
        self.closed = False

    def execute(self, _stmt):
        self.execute_calls += 1
        if self.execute_calls == 1:
            raise OperationalError("SELECT", {}, _FakeMySqlSortMemoryError())
        if self.execute_calls == 2:
            return _FakeScalarResult([self.node.id])
        return _FakeScalarResult([self.node])

    def rollback(self):
        self.rollback_calls += 1

    def close(self):
        self.closed = True


class DataPlatformComponentTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        DataPlatformNode.__table__.create(self.engine)
        DataPlatformComponentRun.__table__.create(self.engine)
        DataPlatformComponentRunTable.__table__.create(self.engine)
        DataPlatformComponentRunLog.__table__.create(self.engine)
        DatabaseConnectionProfile.__table__.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.connection_id = uuid.uuid4()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_mysql_sort_memory_error_is_detected(self) -> None:
        exc = OperationalError("SELECT", {}, _FakeMySqlSortMemoryError())

        self.assertTrue(_is_mysql_out_of_sort_memory(exc))

    def test_list_nodes_falls_back_when_ordered_query_exhausts_sort_memory(self) -> None:
        node = DataPlatformNode(
            id=uuid.uuid4(),
            name="Sync task",
            revision=2,
            node_type="data_sync",
            config={"table_mappings": []},
            status="active",
            created_at=datetime(2026, 7, 24, 10, 0, 0),
            updated_at=datetime(2026, 7, 24, 10, 0, 0),
        )
        session = _FakeListNodesSession(node)

        with patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=lambda: session,
        ):
            rows = list_nodes()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].node_id, node.id)
        self.assertEqual(rows[0].name, "Sync task")
        self.assertEqual(session.rollback_calls, 1)
        self.assertEqual(session.execute_calls, 3)
        self.assertTrue(session.closed)

    def test_component_task_update_increments_revision(self) -> None:
        with patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        ):
            created = create_node(
                name="SQL task",
                node_type="doris_sql",
                description=None,
                config={
                    "connection_id": str(self.connection_id),
                    "database": "DWH_TEST",
                    "sql": "SELECT 1",
                },
                actor=None,
            )
            updated = update_node(
                created.node_id,
                {"config": {**created.config, "sql": "SELECT 2"}},
                actor=None,
            )

        self.assertEqual(created.revision, 1)
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.config["sql"], "SELECT 2")

    def test_workflow_freezes_component_task_revision_and_config(self) -> None:
        task = DataPlatformNode(
            id=uuid.uuid4(),
            name="Sync task",
            revision=3,
            node_type="data_sync",
            config={
                "source_connection_id": str(self.connection_id),
                "source_database": "ODS",
                "source_table": "CUSTOMER",
                "target_connection_id": str(uuid.uuid4()),
                "target_database": "DWD",
                "target_table": "CUSTOMER",
                "write_mode": "full_replace",
                "batch_size": 1000,
            },
            status="active",
            created_at=datetime(2026, 7, 16, 10, 0, 0),
            updated_at=datetime(2026, 7, 16, 10, 0, 0),
        )
        with self.factory() as session:
            session.add(task)
            session.commit()
            frozen = _freeze_component_task_nodes(
                session,
                [{
                    "key": "sync_1",
                    "name": "数据同步",
                    "node_type": "data_sync",
                    "config": {"task_definition_id": str(task.id)},
                }],
                preserve_existing=False,
            )
            task.revision = 4
            task.config = {**task.config, "source_table": "CUSTOMER_NEW"}
            session.commit()
            preserved = _freeze_component_task_nodes(session, frozen, preserve_existing=True)

        config = preserved[0]["config"]
        self.assertEqual(config["task_definition_revision"], 3)
        self.assertEqual(config["source_table"], "CUSTOMER")
        self.assertEqual(config["task_definition_snapshot"]["revision"], 3)

    def test_unbound_component_placeholder_cannot_execute(self) -> None:
        with self.assertRaisesRegex(ValueError, "尚未选择已保存任务"):
            _validate_component_task_bindings([
                {
                    "key": "sync_1",
                    "name": "数据同步",
                    "node_type": "data_sync",
                    "config": {"note": "请选择已保存的数据同步任务"},
                }
            ])

    def test_legacy_inline_component_config_remains_compatible(self) -> None:
        _validate_component_task_bindings([
            {
                "key": "sql_1",
                "name": "旧 Doris SQL 节点",
                "node_type": "doris_sql",
                "config": {
                    "connection_id": str(self.connection_id),
                    "database": "DWH_TEST",
                    "sql": "SELECT 1",
                },
            }
        ])

    def test_data_sync_table_mappings_config_is_accepted(self) -> None:
        normalized = _normalize_component_task_config(
            "data_sync",
            {
                "source_connection_id": str(self.connection_id),
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "sync_method": "insert_select",
                "schema_policy": "source",
                "table_mappings": [
                    {
                        "id": "m1",
                        "enabled": True,
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER",
                    }
                ],
            },
        )

        self.assertEqual(normalized["source_schema"], "ODS")
        self.assertEqual(normalized["target_connection_id"], str(self.connection_id))
        self.assertEqual(normalized["sync_method"], "insert_select")
        self.assertEqual(normalized["schema_policy"], "source")
        self.assertEqual(normalized["table_mappings"][0]["source_table"], "CUSTOMER")

    def test_data_sync_mysql_source_requires_explicit_doris_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "目标 Doris 连接"):
            _normalize_component_task_config(
                "data_sync",
                {
                    "source_connection_id": str(self.connection_id),
                    "source_engine": "mysql",
                    "source_catalog": "local_mysql",
                    "source_schema": "ODS",
                    "target_database": "DWD",
                    "write_mode": "append",
                    "sync_method": "auto",
                    "schema_policy": "source",
                    "table_mappings": [
                        {
                            "id": "m1",
                            "enabled": True,
                            "source_schema": "ODS",
                            "source_table": "CUSTOMER",
                            "target_table": "CUSTOMER",
                        }
                    ],
                },
            )

    def test_data_sync_mysql_source_rejects_insert_select_at_save_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能使用 Catalog"):
            _normalize_component_task_config(
                "data_sync",
                {
                    "source_connection_id": str(self.connection_id),
                    "source_engine": "mysql",
                    "target_connection_id": str(uuid.uuid4()),
                    "source_catalog": "local_mysql",
                    "source_schema": "ODS",
                    "target_database": "DWD",
                    "write_mode": "append",
                    "sync_method": "insert_select",
                    "schema_policy": "source",
                    "table_mappings": [
                        {
                            "id": "m1",
                            "enabled": True,
                            "source_schema": "ODS",
                            "source_table": "CUSTOMER",
                            "target_table": "CUSTOMER",
                        }
                    ],
                },
            )

    def test_data_sync_legacy_single_table_config_remains_compatible(self) -> None:
        normalized = _normalize_component_task_config(
            "data_sync",
            {
                "source_connection_id": str(self.connection_id),
                "source_database": "ODS",
                "source_table": "CUSTOMER",
                "target_connection_id": str(uuid.uuid4()),
                "target_database": "DWD",
                "target_table": "CUSTOMER",
                "write_mode": "full_replace",
            },
        )

        self.assertEqual(normalized["source_table"], "CUSTOMER")
        self.assertEqual(normalized["write_mode"], "full_replace")

    def test_data_sync_component_run_is_persisted_on_success(self) -> None:
        profile = DatabaseConnectionProfile(
            id=self.connection_id,
            name="Doris",
            engine="doris",
            host="127.0.0.1",
            port=9030,
            username="root",
            password_enc="",
        )
        node = DataPlatformNode(
            id=uuid.uuid4(),
            name="Sync task",
            revision=2,
            node_type="data_sync",
            config={
                "source_connection_id": str(self.connection_id),
                "target_connection_id": str(self.connection_id),
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "sync_method": "insert_select",
                "schema_policy": "source",
                "table_mappings": [
                    {
                        "id": "m1",
                        "enabled": True,
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER",
                    }
                ],
            },
            status="active",
            created_at=datetime(2026, 7, 25, 9, 0, 0),
            updated_at=datetime(2026, 7, 25, 9, 0, 0),
        )
        with self.factory() as session:
            session.add_all([profile, node])
            session.commit()

        with patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        ), patch(
            "recovery_service.services.data_platform.execute_data_sync",
            return_value={
                "status": "succeeded",
                "message": "done",
                "success_count": 1,
                "failed_count": 0,
                "loaded_rows": 3,
                "table_results": [],
                "logs": [],
                "config_patch": {"last_run_status": "succeeded"},
            },
        ):
            result = run_component_task_once(node.id, {"selected_tables": ["m1"]}, actor=None)
            runs = list_component_runs(node.id)

        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["component_run_id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "succeeded")
        self.assertEqual(runs[0].selected_items, ["m1"])
        self.assertEqual(runs[0].result["loaded_rows"], 3)

    def test_data_sync_component_run_is_persisted_on_failure(self) -> None:
        profile = DatabaseConnectionProfile(
            id=self.connection_id,
            name="Doris",
            engine="doris",
            host="127.0.0.1",
            port=9030,
            username="root",
            password_enc="",
        )
        node = DataPlatformNode(
            id=uuid.uuid4(),
            name="Sync task",
            revision=1,
            node_type="data_sync",
            config={
                "source_connection_id": str(self.connection_id),
                "target_connection_id": str(self.connection_id),
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "sync_method": "insert_select",
                "schema_policy": "source",
                "table_mappings": [
                    {
                        "id": "m1",
                        "enabled": True,
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER",
                    }
                ],
            },
            status="active",
            created_at=datetime(2026, 7, 25, 9, 0, 0),
            updated_at=datetime(2026, 7, 25, 9, 0, 0),
        )
        with self.factory() as session:
            session.add_all([profile, node])
            session.commit()

        with patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        ), patch(
            "recovery_service.services.data_platform.execute_data_sync",
            side_effect=ValueError("字段映射无效"),
        ):
            with self.assertRaisesRegex(ValueError, "字段映射无效"):
                run_component_task_once(node.id, None, actor=None)
            runs = list_component_runs(node.id)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "failed")
        self.assertIn("字段映射无效", runs[0].message)
        self.assertEqual(runs[0].result["failed_count"], 1)

    def test_data_sync_component_run_is_submitted_to_queue(self) -> None:
        node = DataPlatformNode(
            id=uuid.uuid4(),
            name="Sync task",
            revision=1,
            node_type="data_sync",
            config={
                "source_connection_id": str(self.connection_id),
                "target_connection_id": str(self.connection_id),
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "sync_method": "insert_select",
                "schema_policy": "source",
                "table_mappings": [
                    {
                        "id": "m1",
                        "enabled": True,
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER",
                    }
                ],
            },
            status="active",
            created_at=datetime(2026, 7, 27, 10, 0, 0),
            updated_at=datetime(2026, 7, 27, 10, 0, 0),
        )
        with self.factory() as session:
            session.add(node)
            session.commit()

        class _FakeTask:
            id = "celery-data-sync-1"

        with patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        ), patch(
            "recovery_service.workers.celery_app.celery_app.send_task",
            return_value=_FakeTask(),
        ) as send_task:
            result = submit_component_task_run(node.id, {"selected_tables": ["m1"]}, actor=None)
            runs = list_component_runs(node.id)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["celery_task_id"], "celery-data-sync-1")
        send_task.assert_called_once()
        self.assertEqual(send_task.call_args.kwargs["queue"], "data_sync")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "queued")
        self.assertEqual(runs[0].selected_items, ["m1"])

    def test_queued_data_sync_component_run_is_consumed_by_worker(self) -> None:
        profile = DatabaseConnectionProfile(
            id=self.connection_id,
            name="Doris",
            engine="doris",
            host="127.0.0.1",
            port=9030,
            username="root",
            password_enc="",
        )
        node = DataPlatformNode(
            id=uuid.uuid4(),
            name="Sync task",
            revision=1,
            node_type="data_sync",
            config={
                "source_connection_id": str(self.connection_id),
                "target_connection_id": str(self.connection_id),
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "sync_method": "insert_select",
                "schema_policy": "source",
                "table_mappings": [
                    {
                        "id": "m1",
                        "enabled": True,
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER",
                    }
                ],
            },
            status="active",
            created_at=datetime(2026, 7, 27, 10, 0, 0),
            updated_at=datetime(2026, 7, 27, 10, 0, 0),
        )
        run = DataPlatformComponentRun(
            id=uuid.uuid4(),
            node_id=node.id,
            node_type="data_sync",
            node_name=node.name,
            node_revision=1,
            selected_items=["m1"],
            status="queued",
            message="queued",
            result={},
            created_at=datetime(2026, 7, 27, 10, 1, 0),
            updated_at=datetime(2026, 7, 27, 10, 1, 0),
        )
        with self.factory() as session:
            session.add_all([profile, node, run])
            session.commit()

        with patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        ), patch(
            "recovery_service.services.data_platform.execute_data_sync",
            return_value={
                "status": "succeeded",
                "message": "done",
                "success_count": 1,
                "failed_count": 0,
                "loaded_rows": 3,
                "table_results": [],
                "logs": [],
            },
        ):
            result = run_queued_component_task(run.id)
            runs = list_component_runs(node.id)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(runs[0].status, "succeeded")
        self.assertEqual(runs[0].result["loaded_rows"], 3)

    def test_queued_data_sync_component_run_marks_missing_connection_failed(self) -> None:
        node = DataPlatformNode(
            id=uuid.uuid4(),
            name="Sync task",
            revision=1,
            node_type="data_sync",
            config={
                "source_connection_id": str(self.connection_id),
                "target_connection_id": str(self.connection_id),
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "sync_method": "insert_select",
                "schema_policy": "source",
                "table_mappings": [
                    {
                        "id": "m1",
                        "enabled": True,
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER",
                    }
                ],
            },
            status="active",
            created_at=datetime(2026, 7, 27, 10, 0, 0),
            updated_at=datetime(2026, 7, 27, 10, 0, 0),
        )
        run = DataPlatformComponentRun(
            id=uuid.uuid4(),
            node_id=node.id,
            node_type="data_sync",
            node_name=node.name,
            node_revision=1,
            selected_items=["m1"],
            status="queued",
            message="queued",
            result={},
            created_at=datetime(2026, 7, 27, 10, 1, 0),
            updated_at=datetime(2026, 7, 27, 10, 1, 0),
        )
        with self.factory() as session:
            session.add_all([node, run])
            session.commit()

        with patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        ):
            with self.assertRaisesRegex(ValueError, "source or target connection"):
                run_queued_component_task(run.id)
            runs = list_component_runs(node.id)

        self.assertEqual(runs[0].status, "failed")
        self.assertIn("source or target connection", runs[0].message)


if __name__ == "__main__":
    unittest.main()
