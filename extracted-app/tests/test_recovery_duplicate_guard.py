import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import RecoveryTask, TaskEvent
from recovery_service.settings import get_settings
from recovery_service.workers.celery_app import celery_app, visibility_timeout
from recovery_service.workers.tasks.run_recovery import run_recovery_task


class RecoveryDuplicateGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        RecoveryTask.__table__.create(self.engine)
        TaskEvent.__table__.create(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _task(self, state: str) -> RecoveryTask:
        task = RecoveryTask(
            id=uuid.uuid4(),
            state=state,
            remote_host="source",
            remote_port=22,
            remote_user="root",
            remote_password_enc="encrypted",
            remote_directory="/backup",
            target_connection="oracle",
            target_admin_user="system",
            target_admin_password_enc="encrypted",
            options={},
        )
        with self.session_factory() as session:
            session.add(task)
            session.commit()
        return task

    def test_delivery_for_running_task_is_acknowledged_without_execution(self) -> None:
        task = self._task("policy_running")
        with (
            patch(
                "recovery_service.workers.tasks.run_recovery.get_sync_session_factory",
                return_value=self.session_factory,
            ),
            patch(
                "recovery_service.services.task_events.get_sync_session_factory",
                return_value=self.session_factory,
            ),
            patch("recovery_service.workers.tasks.run_recovery.RecoveryPipeline.run_task") as pipeline,
        ):
            result = run_recovery_task.run(str(task.id))

        self.assertTrue(result["duplicate_ignored"])
        self.assertEqual(result["state"], "policy_running")
        pipeline.assert_not_called()
        with self.session_factory() as session:
            events = session.query(TaskEvent).filter(TaskEvent.task_id == task.id).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "duplicate_delivery_ignored")

    def test_created_task_is_claimed_once_and_runs_pipeline(self) -> None:
        task = self._task("created")
        pipeline_result = {
            "state": "succeeded",
            "success": True,
            "message": "done",
            "metadata": {},
        }
        with (
            patch(
                "recovery_service.workers.tasks.run_recovery.get_sync_session_factory",
                return_value=self.session_factory,
            ),
            patch(
                "recovery_service.services.task_events.get_sync_session_factory",
                return_value=self.session_factory,
            ),
            patch(
                "recovery_service.workers.tasks.run_recovery.RecoveryPipeline.run_task",
                return_value=pipeline_result,
            ) as pipeline,
        ):
            result = run_recovery_task.run(str(task.id))

        self.assertTrue(result["success"])
        pipeline.assert_called_once()
        with self.session_factory() as session:
            stored = session.get(RecoveryTask, task.id)
        self.assertEqual(stored.state, "succeeded")

    def test_stop_requested_while_pipeline_runs_finishes_as_cancelled(self) -> None:
        task = self._task("created")

        def stopped_pipeline(**_kwargs):
            with self.session_factory() as session:
                stored = session.get(RecoveryTask, task.id)
                stored.stop_requested = True
                stored.stop_reason = "operator stop"
                stored.state = "stopping"
                session.commit()
            return {
                "state": "failed",
                "success": False,
                "message": "remote command exited",
                "metadata": {"oracle_datapump_job_name": "ORS_TASK_RUN"},
            }

        with (
            patch(
                "recovery_service.workers.tasks.run_recovery.get_sync_session_factory",
                return_value=self.session_factory,
            ),
            patch(
                "recovery_service.services.task_events.get_sync_session_factory",
                return_value=self.session_factory,
            ),
            patch(
                "recovery_service.workers.tasks.run_recovery.RecoveryPipeline.run_task",
                side_effect=stopped_pipeline,
            ),
        ):
            result = run_recovery_task.run(str(task.id))

        self.assertFalse(result["success"])
        with self.session_factory() as session:
            stored = session.get(RecoveryTask, task.id)
        self.assertEqual(stored.state, "cancelled")
        self.assertEqual(stored.error_message, "operator stop")
        self.assertIsNotNone(stored.stopped_at)
        self.assertEqual(stored.metadata_snapshot["oracle_datapump_job_name"], "ORS_TASK_RUN")

    def test_redis_visibility_timeout_exceeds_oracle_operation_timeout(self) -> None:
        settings = get_settings()
        self.assertGreaterEqual(visibility_timeout, settings.oracle_import_operation_timeout_seconds + 3600)
        self.assertEqual(
            celery_app.conf.broker_transport_options["visibility_timeout"],
            visibility_timeout,
        )


if __name__ == "__main__":
    unittest.main()
