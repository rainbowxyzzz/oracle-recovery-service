import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import Base, DataPlatformWorkflowRun, DataPlatformWorkflowVersion
from recovery_service.services.data_platform import (
    archive_folder,
    copy_workflow,
    create_folder,
    create_workflow,
    list_schedules,
    run_version,
    update_folder,
    update_version,
    update_workflow,
)
from recovery_service.services.auth import AuthContext


class DataPlatformTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.factory_patch = patch(
            "recovery_service.services.data_platform.get_sync_session_factory",
            return_value=self.factory,
        )
        self.factory_patch.start()

    def tearDown(self) -> None:
        self.factory_patch.stop()
        self.engine.dispose()

    def test_folder_cycle_is_rejected_and_workflow_can_move_to_root(self) -> None:
        parent = create_folder(name="父目录", parent_id=None, actor=None)
        child = create_folder(name="子目录", parent_id=parent.folder_id, actor=None)
        workflow = create_workflow(name="测试任务", description=None, folder_id=child.folder_id, actor=None)

        with self.assertRaises(ValueError):
            update_folder(parent.folder_id, {"parent_id": child.folder_id}, actor=None)

        moved = update_workflow(workflow.workflow_id, {"folder_id": None}, actor=None)
        self.assertIsNone(moved.folder_id)

    def test_nonempty_folder_cannot_be_deleted(self) -> None:
        folder = create_folder(name="业务目录", parent_id=None, actor=None)
        create_workflow(name="业务任务", description=None, folder_id=folder.folder_id, actor=None)

        with self.assertRaises(ValueError):
            archive_folder(folder.folder_id, actor=None)

    def test_copy_creates_draft_without_schedule_or_production_snapshot(self) -> None:
        folder = create_folder(name="复制目标", parent_id=None, actor=None)
        workflow = create_workflow(name="原任务", description="说明", folder_id=folder.folder_id, actor=None)
        with self.factory() as session:
            session.add(
                DataPlatformWorkflowVersion(
                    id=uuid.uuid4(),
                    workflow_id=workflow.workflow_id,
                    version_no=2,
                    channel="dev",
                    status="draft",
                    nodes=[
                        {
                            "key": "sm4-1",
                            "node_type": "sm4_batch",
                            "config": {
                                "task_definition_id": str(uuid.uuid4()),
                                "task_definition_revision": 3,
                                "task_definition_snapshot": {"revision": 3},
                            },
                        }
                    ],
                    edges=[],
                    schedule_enabled=True,
                    schedule_type="daily",
                    run_time="22:00",
                )
            )
            session.commit()

        copied = copy_workflow(
            workflow.workflow_id,
            name="原任务副本",
            folder_id=folder.folder_id,
            actor=None,
        )

        self.assertEqual(copied.name, "原任务副本")
        self.assertIsNotNone(copied.latest_dev_version_id)
        with self.factory() as session:
            version = session.get(DataPlatformWorkflowVersion, copied.latest_dev_version_id)
            self.assertFalse(version.schedule_enabled)
            config = version.nodes[0]["config"]
            self.assertNotIn("task_definition_snapshot", config)
            self.assertNotIn("task_definition_revision", config)

    def test_schedule_is_visible_before_first_run_and_after_disable(self) -> None:
        folder = create_folder(name="schedule-folder", parent_id=None, actor=None)
        workflow = create_workflow(name="future-schedule", description=None, folder_id=folder.folder_id, actor=None)
        version_id = uuid.uuid4()
        next_run_at = datetime.now() + timedelta(hours=6)
        with self.factory() as session:
            session.add(
                DataPlatformWorkflowVersion(
                    id=version_id,
                    workflow_id=workflow.workflow_id,
                    version_no=1,
                    channel="prod",
                    status="online",
                    nodes=[{"key": "manual-1", "node_type": "manual", "config": {}}],
                    edges=[],
                    schedule_enabled=True,
                    schedule_type="daily",
                    run_time="22:00",
                    next_run_at=next_run_at,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )
            session.commit()

        schedules = list_schedules(include_disabled=False)
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].workflow_name, "future-schedule")
        self.assertEqual(schedules[0].folder_path, "schedule-folder")
        self.assertEqual(schedules[0].schedule_state, "waiting")
        self.assertEqual(schedules[0].next_run_at, next_run_at)

        with self.factory() as session:
            session.add(
                DataPlatformWorkflowRun(
                    id=uuid.uuid4(),
                    workflow_id=workflow.workflow_id,
                    version_id=version_id,
                    version_no=1,
                    channel="prod",
                    trigger_type="schedule",
                    status="queued",
                    total_count=1,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )
            session.commit()
        self.assertEqual(list_schedules(include_disabled=False)[0].schedule_state, "queued")

        with self.factory() as session:
            version = session.get(DataPlatformWorkflowVersion, version_id)
            version.schedule_enabled = False
            version.next_run_at = None
            session.commit()
        self.assertEqual(list_schedules(include_disabled=False), [])
        disabled = list_schedules(include_disabled=True)
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0].schedule_state, "disabled")

    def test_online_version_schedule_rule_can_be_updated_without_changing_design(self) -> None:
        workflow = create_workflow(name="online-schedule", description=None, folder_id=None, actor=None)
        version_id = uuid.uuid4()
        original_nodes = [{"key": "manual-1", "node_type": "manual", "config": {}}]
        with self.factory() as session:
            session.add(
                DataPlatformWorkflowVersion(
                    id=version_id,
                    workflow_id=workflow.workflow_id,
                    version_no=1,
                    channel="prod",
                    status="online",
                    nodes=original_nodes,
                    edges=[],
                    schedule_enabled=False,
                    schedule_type="daily",
                    run_time="02:00",
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )
            session.commit()

        actor = AuthContext(
            user_id=None,
            username="admin",
            role="admin",
            auth_type="session",
            permissions={"actions": []},
        )
        updated = update_version(
            version_id,
            {
                "schedule_enabled": True,
                "schedule_type": "monthly",
                "run_time": "22:30",
                "day_of_month": 18,
                "day_of_week": 1,
                "interval_minutes": None,
            },
            actor,
        )

        self.assertTrue(updated.schedule_enabled)
        self.assertEqual(updated.schedule_type, "monthly")
        self.assertEqual(updated.run_time, "22:30")
        self.assertEqual(updated.day_of_month, 18)
        self.assertIsNotNone(updated.next_run_at)
        with self.factory() as session:
            stored = session.get(DataPlatformWorkflowVersion, version_id)
            self.assertEqual(stored.nodes, original_nodes)
            self.assertEqual(stored.release_snapshot["schedule"]["schedule_type"], "monthly")

        with self.assertRaises(ValueError):
            update_version(version_id, {"nodes": []}, actor)

    def test_workflow_run_is_submitted_to_data_platform_queue(self) -> None:
        workflow = create_workflow(name="queued-workflow", description=None, folder_id=None, actor=None)
        version_id = uuid.uuid4()
        with self.factory() as session:
            session.add(
                DataPlatformWorkflowVersion(
                    id=version_id,
                    workflow_id=workflow.workflow_id,
                    version_no=1,
                    channel="dev",
                    status="draft",
                    nodes=[{"key": "manual-1", "node_type": "manual", "config": {}}],
                    edges=[],
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )
            session.commit()

        with patch("recovery_service.workers.celery_app.celery_app.send_task") as send_task:
            send_task.return_value.id = "workflow-celery-1"
            result = run_version(version_id)

        self.assertEqual(result.status, "queued")
        send_task.assert_called_once_with(
            "data_platform.workflow_run",
            args=[str(result.run_id)],
            queue="data_platform",
        )
        with self.factory() as session:
            stored = session.get(DataPlatformWorkflowRun, result.run_id)
            self.assertEqual(stored.status, "queued")

    def test_workflow_queue_submission_failure_is_recorded(self) -> None:
        workflow = create_workflow(name="queue-failure", description=None, folder_id=None, actor=None)
        version_id = uuid.uuid4()
        with self.factory() as session:
            session.add(
                DataPlatformWorkflowVersion(
                    id=version_id,
                    workflow_id=workflow.workflow_id,
                    version_no=1,
                    channel="dev",
                    status="draft",
                    nodes=[{"key": "manual-1", "node_type": "manual", "config": {}}],
                    edges=[],
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )
            session.commit()

        with patch(
            "recovery_service.workers.celery_app.celery_app.send_task",
            side_effect=RuntimeError("redis unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "redis unavailable"):
                run_version(version_id)

        with self.factory() as session:
            stored = session.query(DataPlatformWorkflowRun).filter_by(version_id=version_id).one()
            self.assertEqual(stored.status, "failed")
            self.assertIn("redis unavailable", stored.message)


if __name__ == "__main__":
    unittest.main()
