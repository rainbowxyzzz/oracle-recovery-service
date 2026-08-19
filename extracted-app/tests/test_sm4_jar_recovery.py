import hashlib
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from recovery_service.services.data_platform import _node_error_result
from recovery_service.services.doris_encryption import _verify_sm4_batch_key_binding
from recovery_service.services.doris_sm4_function import BuiltSm4Jar, sm4_encrypt_to_base64, sm4_jar_path


class Sm4JarRecoveryTests(unittest.TestCase):
    def test_failed_offline_node_keeps_batch_link(self) -> None:
        result = _node_error_result({"batch_id": "batch-1", "state": "queued"}, "jar missing")

        self.assertEqual(result["batch_id"], "batch-1")
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["error"], "jar missing")

    def test_missing_jar_is_rebuilt_from_active_key_metadata(self) -> None:
        key_seed = "recovery-test-key"
        fingerprint = hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:16]
        filename = f"cq-sm4-encrypt-{fingerprint}-v6.jar"
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(
                doris_sm4_udf_jar_dir=temp_dir,
                doris_sm4_udf_public_base_url="",
            )

            def fake_build(*, sm4_key, public_base_url):
                path = Path(temp_dir) / filename
                path.write_bytes(b"java-archive")
                return BuiltSm4Jar(
                    filename=filename,
                    path=path,
                    url=f"{public_base_url}/{filename}",
                    symbol=f"CqSm4Encrypt_{fingerprint}",
                    decrypt_symbol=f"CqSm4Decrypt_{fingerprint}",
                    key_seed=sm4_key,
                    key_fingerprint=fingerprint,
                    verification_plaintext="plain",
                    verification_ciphertext="cipher",
                )

            with (
                patch("recovery_service.services.doris_sm4_function.get_settings", return_value=settings),
                patch(
                    "recovery_service.services.doris_sm4_function.get_active_sm4_key_seed_for_jar",
                    return_value=(key_seed, uuid.uuid4(), fingerprint),
                ),
                patch("recovery_service.services.doris_sm4_function.build_sm4_udf_jar", side_effect=fake_build),
            ):
                recovered = sm4_jar_path(filename)

            self.assertEqual(recovered.name, filename)
            self.assertEqual(recovered.read_bytes(), b"java-archive")

    def test_batch_preflight_validates_bound_key_before_ddl(self) -> None:
        key_seed = "batch-bound-key"
        fingerprint = hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:16]
        expected = sm4_encrypt_to_base64(f"oracle-recovery-sm4-batch-check-{fingerprint}", key_seed)

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params):
                self.sql = sql
                self.params = params

            def fetchone(self):
                return {"result": expected}

        class Connection:
            def cursor(self):
                return Cursor()

        @contextmanager
        def fake_doris_conn(_profile, _database):
            yield Connection()

        job = SimpleNamespace(id=uuid.uuid4(), database="DWD_TEST", connection_id=uuid.uuid4())
        with (
            patch(
                "recovery_service.services.doris_encryption.get_sm4_key_seed_for_batch",
                return_value=(key_seed, uuid.uuid4(), fingerprint),
            ),
            patch("recovery_service.services.doris_encryption._doris_conn", side_effect=fake_doris_conn),
            patch("recovery_service.services.doris_encryption._add_sm4_log"),
        ):
            _verify_sm4_batch_key_binding(SimpleNamespace(), job)


if __name__ == "__main__":
    unittest.main()
