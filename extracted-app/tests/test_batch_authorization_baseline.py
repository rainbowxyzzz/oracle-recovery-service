import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    Base,
    BatchAuthGrantBatch,
    BatchAuthGrantTable,
    BatchAuthGrantUser,
    BatchAuthPrivilegeLease,
)
from recovery_service.services.batch_authorization import _offline_tables_with_session


class BatchAuthorizationLeaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(
            self.engine,
            tables=[
                BatchAuthGrantBatch.__table__,
                BatchAuthGrantTable.__table__,
                BatchAuthPrivilegeLease.__table__,
                BatchAuthGrantUser.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.connection_id = uuid.uuid4()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _lease(self, ownership_state="system"):
        lease = BatchAuthPrivilegeLease(
            lease_key_hash="a" * 64,
            connection_id=self.connection_id,
            db_user_identity="'codex_auth_lease'@'%'",
            source_database="CODEX_AUTH_LEASE",
            source_table="SOURCE_TABLE",
            privilege_type="SELECT",
            baseline_existed_before_system=ownership_state == "external",
            owned_by_system=ownership_state == "system",
            ownership_state=ownership_state,
            state="active",
        )
        self.session.add(lease)
        self.session.flush()
        return lease

    def _batch(self, lease=None, *, name="lease-test", created_permission=False):
        batch = BatchAuthGrantBatch(
            connection_id=self.connection_id,
            department_id=uuid.uuid4(),
            department_name="Codex权限租约测试处",
            department_database="CODEX_AUTH_LEASE",
            name=name,
            filename="tables.xlsx",
            state="succeeded",
            starts_at=app_now(),
            expires_at=app_now() + timedelta(days=1),
        )
        self.session.add(batch)
        self.session.flush()
        table = BatchAuthGrantTable(
            batch_id=batch.id,
            source_database="CODEX_AUTH_LEASE",
            source_table="SOURCE_TABLE",
            target_database="CODEX_AUTH_LEASE",
            target_object="SOURCE_TABLE",
            target_object_type="source_table",
            state="succeeded",
        )
        self.session.add(table)
        self.session.flush()
        grant_user = BatchAuthGrantUser(
            batch_id=batch.id,
            table_id=table.id,
            lease_id=lease.id if lease else None,
            db_username="codex_auth_lease",
            db_user_identity="'codex_auth_lease'@'%'",
            privilege_type="SELECT",
            grant_state="succeeded" if created_permission else "skipped",
            revoke_state="pending",
            privilege_existed_before=not created_permission,
            granted_by_this_batch=created_permission,
        )
        self.session.add(grant_user)
        self.session.flush()
        return batch, table, grant_user

    def test_external_privilege_is_never_revoked(self):
        lease = self._lease("external")
        batch, table, grant_user = self._batch(lease)

        with patch("recovery_service.services.batch_authorization._revoke_source_table") as revoke:
            _offline_tables_with_session(self.session, object(), batch, [table])

        revoke.assert_not_called()
        self.assertEqual(grant_user.revoke_state, "skipped")
        self.assertEqual(grant_user.revoke_decision, "skip_existing")
        self.assertEqual(lease.state, "active")

    def test_single_system_owned_privilege_is_revoked(self):
        lease = self._lease("system")
        batch, table, grant_user = self._batch(lease, created_permission=True)

        with patch("recovery_service.services.batch_authorization._revoke_source_table") as revoke:
            _offline_tables_with_session(self.session, object(), batch, [table])

        revoke.assert_called_once_with(
            unittest.mock.ANY,
            "CODEX_AUTH_LEASE",
            "SOURCE_TABLE",
            "codex_auth_lease",
        )
        self.assertEqual(grant_user.revoke_decision, "revoke")
        self.assertEqual(lease.state, "revoked")

    def test_overlapping_batches_keep_then_revoke_system_permission(self):
        lease = self._lease("system")
        batch_a, table_a, user_a = self._batch(
            lease,
            name="batch-a",
            created_permission=True,
        )
        batch_b, table_b, user_b = self._batch(lease, name="batch-b")

        with patch("recovery_service.services.batch_authorization._revoke_source_table") as revoke:
            _offline_tables_with_session(self.session, object(), batch_a, [table_a])
            revoke.assert_not_called()
            self.assertEqual(user_a.revoke_decision, "skip_referenced")
            self.assertEqual(lease.state, "active")

            _offline_tables_with_session(self.session, object(), batch_b, [table_b])

        revoke.assert_called_once()
        self.assertEqual(user_b.revoke_decision, "revoke")
        self.assertEqual(lease.state, "revoked")

    def test_reverse_offline_order_has_same_final_result(self):
        lease = self._lease("system")
        batch_a, table_a, user_a = self._batch(
            lease,
            name="batch-a",
            created_permission=True,
        )
        batch_b, table_b, user_b = self._batch(lease, name="batch-b")

        with patch("recovery_service.services.batch_authorization._revoke_source_table") as revoke:
            _offline_tables_with_session(self.session, object(), batch_b, [table_b])
            revoke.assert_not_called()
            self.assertEqual(user_b.revoke_decision, "skip_referenced")

            _offline_tables_with_session(self.session, object(), batch_a, [table_a])

        revoke.assert_called_once()
        self.assertEqual(user_a.revoke_decision, "revoke")
        self.assertEqual(lease.state, "revoked")

    def test_legacy_record_without_lease_uses_unknown_ownership(self):
        batch, table, grant_user = self._batch(None, created_permission=True)

        with patch("recovery_service.services.batch_authorization._revoke_source_table") as revoke:
            _offline_tables_with_session(self.session, object(), batch, [table])

        revoke.assert_not_called()
        self.assertIsNotNone(grant_user.lease_id)
        self.assertEqual(grant_user.revoke_decision, "skip_ownership_unknown")
        lease = self.session.get(BatchAuthPrivilegeLease, grant_user.lease_id)
        self.assertEqual(lease.ownership_state, "unknown")

    def test_lease_business_key_is_unique(self):
        self._lease("system")
        self.session.commit()
        duplicate = BatchAuthPrivilegeLease(
            lease_key_hash="a" * 64,
            connection_id=self.connection_id,
            db_user_identity="'codex_auth_lease'@'%'",
            source_database="CODEX_AUTH_LEASE",
            source_table="SOURCE_TABLE",
            privilege_type="SELECT",
        )
        self.session.add(duplicate)

        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

    def test_manual_and_scheduled_paths_share_lease_plan(self):
        source = Path("src/recovery_service/services/batch_authorization.py").read_text(
            encoding="utf-8"
        )

        self.assertGreaterEqual(source.count("_privilege_lease_revoke_plan("), 3)
        self.assertNotIn("if not grant_user.granted_by_this_batch", source)


if __name__ == "__main__":
    unittest.main()
