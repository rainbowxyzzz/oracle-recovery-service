import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import DorisSm4FunctionDeployment, DorisSm4KeyVersion
from recovery_service.api.schemas.doris_encryption import DorisSm4FunctionDatabaseResult
from recovery_service.services.doris_sm4_function import BuiltSm4Jar, refresh_sm4_functions
from recovery_service.services.sm4_key_versions import resolve_sm4_key_version_for_batch


class Sm4FunctionDeploymentKeyResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        DorisSm4KeyVersion.__table__.create(self.engine)
        DorisSm4FunctionDeployment.__table__.create(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        self.connection_id = uuid.uuid4()
        self.now = datetime(2026, 7, 15, 17, 0, 0)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _key(self, fingerprint: str, created_at: datetime) -> DorisSm4KeyVersion:
        return DorisSm4KeyVersion(
            id=uuid.uuid4(),
            connection_id=self.connection_id,
            connection_name="Doris",
            name=f"SM4-{fingerprint}",
            key_fingerprint=fingerprint,
            key_seed_enc="encrypted-seed",
            key_mode="random",
            function_name="CQ_SM4_ENCRYPT",
            decrypt_function_name="CQ_SM4_DECRYPT",
            jar_filename=f"cq-sm4-encrypt-{fingerprint}-v6.jar",
            status="active",
            created_at=created_at,
            updated_at=created_at,
        )

    def test_database_uses_its_successfully_deployed_key_not_latest_connection_key(self) -> None:
        deployed_key = self._key("old-key", self.now)
        newer_key = self._key("new-key", self.now + timedelta(minutes=5))
        with self.session_factory() as session:
            session.add_all([deployed_key, newer_key])
            session.add(
                DorisSm4FunctionDeployment(
                    connection_id=self.connection_id,
                    connection_name="Doris",
                    database="DWD_SOCIAL_SECURITY",
                    function_name="CQ_SM4_ENCRYPT",
                    decrypt_function_name="CQ_SM4_DECRYPT",
                    key_version_id=deployed_key.id,
                    key_fingerprint=deployed_key.key_fingerprint,
                    jar_filename=deployed_key.jar_filename,
                    state="success",
                    message="ok",
                    verification_state="success",
                    attempted_at=self.now,
                    last_success_at=self.now,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.commit()

        with patch(
            "recovery_service.services.sm4_key_versions.get_sync_session_factory",
            return_value=self.session_factory,
        ):
            result = resolve_sm4_key_version_for_batch(
                key_id=None,
                connection_id=self.connection_id,
                database="DWD_SOCIAL_SECURITY",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.key_id, deployed_key.id)

    def test_database_with_failed_latest_deployment_is_blocked(self) -> None:
        key = self._key("failed-key", self.now)
        with self.session_factory() as session:
            session.add(key)
            session.add(
                DorisSm4FunctionDeployment(
                    connection_id=self.connection_id,
                    connection_name="Doris",
                    database="DWD_SOCIAL_SECURITY",
                    function_name="CQ_SM4_ENCRYPT",
                    key_version_id=key.id,
                    key_fingerprint=key.key_fingerprint,
                    jar_filename=key.jar_filename,
                    state="failed",
                    message="function verification failed",
                    verification_state="failed",
                    attempted_at=self.now,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.commit()

        with patch(
            "recovery_service.services.sm4_key_versions.get_sync_session_factory",
            return_value=self.session_factory,
        ):
            with self.assertRaisesRegex(KeyError, "重新创建并验证"):
                resolve_sm4_key_version_for_batch(
                    key_id=None,
                    connection_id=self.connection_id,
                    database="DWD_SOCIAL_SECURITY",
                )

    def test_legacy_connection_without_deployment_uses_latest_active_key(self) -> None:
        older_key = self._key("legacy-old", self.now)
        latest_key = self._key("legacy-new", self.now + timedelta(minutes=5))
        with self.session_factory() as session:
            session.add_all([older_key, latest_key])
            session.commit()

        with patch(
            "recovery_service.services.sm4_key_versions.get_sync_session_factory",
            return_value=self.session_factory,
        ):
            result = resolve_sm4_key_version_for_batch(
                key_id=None,
                connection_id=self.connection_id,
                database="DWD_LEGACY",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.key_id, latest_key.id)

    def test_refresh_only_processes_explicitly_selected_databases(self) -> None:
        jar = BuiltSm4Jar(
            filename="cq-sm4-encrypt-selected-v6.jar",
            path=Path("/tmp/cq-sm4-encrypt-selected-v6.jar"),
            url="http://api/function.jar",
            symbol="Encrypt",
            decrypt_symbol="Decrypt",
            key_seed="seed",
            key_fingerprint="selected",
            verification_plaintext="plain",
            verification_ciphertext="cipher",
        )
        processed: list[str] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

        @contextmanager
        def fake_doris_conn(_profile, _database):
            yield Connection()

        def fake_refresh(_cursor, database, *_args):
            processed.append(database)
            return DorisSm4FunctionDatabaseResult(database=database, state="success", message="ok")

        with (
            patch("recovery_service.services.doris_sm4_function.build_sm4_udf_jar", return_value=jar),
            patch("recovery_service.services.doris_sm4_function._doris_conn", side_effect=fake_doris_conn),
            patch("recovery_service.services.doris_sm4_function._refresh_function_in_database", side_effect=fake_refresh),
            patch(
                "recovery_service.services.doris_sm4_function.register_sm4_key_version",
                return_value=SimpleNamespace(key_id=uuid.uuid4()),
            ),
            patch("recovery_service.services.doris_sm4_function._record_sm4_function_deployments") as record,
        ):
            result = refresh_sm4_functions(
                SimpleNamespace(id=self.connection_id, name="Doris"),
                sm4_key="seed",
                public_base_url="http://api",
                databases=["DWD_SOCIAL_SECURITY", "MAP"],
            )

        self.assertEqual(processed, ["DWD_SOCIAL_SECURITY", "MAP"])
        self.assertEqual(result.total_databases, 2)
        self.assertEqual(result.success_count, 2)
        record.assert_called_once()


if __name__ == "__main__":
    unittest.main()
