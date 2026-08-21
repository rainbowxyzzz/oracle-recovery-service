import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import DorisSm4BatchJob
from recovery_service.services.sm4_runtime_guard import (
    assert_sm4_key_rotation_allowed,
    list_inflight_sm4_batches,
    sm4_database_guard,
)


class Sm4RuntimeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        DorisSm4BatchJob.__table__.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.connection_id = uuid.uuid4()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _job(self, state: str, database: str = "DWD") -> DorisSm4BatchJob:
        now = datetime(2026, 8, 20, 10, 0, 0)
        return DorisSm4BatchJob(
            id=uuid.uuid4(),
            connection_id=self.connection_id,
            database=database,
            tables=[],
            results=[],
            state=state,
            message="",
            created_at=now,
            updated_at=now,
        )

    def test_rotation_is_blocked_by_inflight_states_in_same_database(self) -> None:
        with self.factory() as session:
            session.add_all([
                self._job("queued"), self._job("reserved"), self._job("running"),
                self._job("stopping"), self._job("succeeded"),
                self._job("running", database="OTHER"),
            ])
            session.commit()
        with patch("recovery_service.services.sm4_runtime_guard.get_sync_session_factory", return_value=self.factory):
            jobs = list_inflight_sm4_batches(self.connection_id, "DWD")
            self.assertEqual({job.state for job in jobs}, {"queued", "reserved", "running", "stopping"})
            with self.assertRaisesRegex(ValueError, "等待任务完成"):
                assert_sm4_key_rotation_allowed(self.connection_id, "DWD")
            assert_sm4_key_rotation_allowed(self.connection_id, "EMPTY")

    def test_non_mysql_guard_is_reentrant_for_same_process(self) -> None:
        with patch("recovery_service.services.sm4_runtime_guard.get_sync_session_factory", return_value=self.factory):
            with sm4_database_guard(self.connection_id, "DWD"):
                with sm4_database_guard(self.connection_id, "dwd"):
                    pass


if __name__ == "__main__":
    unittest.main()
