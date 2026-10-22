import unittest
from pathlib import Path

from recovery_service.services.doris_encryption import (
    _build_insert_sql,
    _normalize_batch_tables,
    _validate_partial_output_lengths,
)
from recovery_service.services.doris_sm4_function import (
    BuiltSm4Jar,
    _java_source,
    _refresh_function_in_database,
    sm4_encrypt_to_base64,
    sm4_partial_decrypt,
    sm4_partial_encrypt,
)
from recovery_service.services.file_decryption import decrypt_file_content


class Sm4PartialEncryptionTests(unittest.TestCase):
    key_seed = "partial-test-key"

    def test_supported_rules_round_trip(self) -> None:
        samples = {
            "ID_KEEP_6_4": "110101199001011234",
            "NAME_HEAD_TAIL": "张小三",
            "PHONE_KEEP_3_4": "13812345678",
            "ADDRESS_KEEP_6_0": "北京市朝阳区建国路88号",
        }
        for rule_code, plaintext in samples.items():
            with self.subTest(rule_code=rule_code):
                encrypted = sm4_partial_encrypt(plaintext, self.key_seed, rule_code)
                self.assertNotEqual(encrypted, plaintext)
                self.assertEqual(sm4_partial_decrypt(encrypted, self.key_seed, rule_code), plaintext)

    def test_visible_prefix_and_suffix_are_preserved(self) -> None:
        encrypted = sm4_partial_encrypt("110101199001011234", self.key_seed, "ID_KEEP_6_4")
        self.assertTrue(encrypted.startswith("110101"))
        self.assertTrue(encrypted.endswith("1234"))

        encrypted = sm4_partial_encrypt("张三", self.key_seed, "NAME_HEAD_TAIL")
        self.assertTrue(encrypted.startswith("张"))
        self.assertFalse(encrypted.endswith("三"))

    def test_java_udf_sources_keep_partial_and_like_logic_in_the_keyed_jar(self) -> None:
        partial = _java_source("Partial", self.key_seed, mode="partial_encrypt")
        decrypt = _java_source("PartialDecrypt", self.key_seed, mode="partial_decrypt")
        like = _java_source("Like", self.key_seed, mode="like")
        self.assertIn("partialEncryptValue(input, ruleCode)", partial)
        self.assertIn("partialDecryptValue(input, ruleCode)", decrypt)
        self.assertIn("matchesLike(plain, pattern)", like)
        self.assertIn('"ID_KEEP_6_4".equals(rule)', like)
        self.assertIn("return false;", like)
        self.assertIn(self.key_seed, like)

    def test_short_values_use_reversible_full_field_fallback(self) -> None:
        for rule_code, plaintext in (
            ("ID_KEEP_6_4", "123"),
            ("NAME_HEAD_TAIL", "张"),
            ("PHONE_KEEP_3_4", "12345"),
            ("ADDRESS_KEEP_6_0", "重庆市"),
        ):
            with self.subTest(rule_code=rule_code):
                encrypted = sm4_partial_encrypt(plaintext, self.key_seed, rule_code)
                self.assertTrue(encrypted.startswith("~CQF~"))
                self.assertEqual(sm4_partial_decrypt(encrypted, self.key_seed, rule_code), plaintext)

    def test_empty_value_is_preserved_and_unknown_rule_is_rejected(self) -> None:
        self.assertEqual(sm4_partial_encrypt("", self.key_seed, "ID_KEEP_6_4"), "")
        self.assertEqual(sm4_partial_decrypt("", self.key_seed, "ID_KEEP_6_4"), "")
        with self.assertRaisesRegex(ValueError, "不支持"):
            sm4_partial_encrypt("value", self.key_seed, "UNKNOWN")

    def test_task_table_rules_are_normalized_without_changing_legacy_tables(self) -> None:
        legacy = _normalize_batch_tables([{"table_name": "customer", "columns": ["name"]}])
        self.assertEqual(legacy, [{
            "table_name": "customer",
            "columns": ["name"],
            "target_database": None,
            "target_table": None,
        }])

        partial = _normalize_batch_tables([{
            "table_name": "customer",
            "columns": ["name", "phone"],
            "column_rules": {"name": "NAME_HEAD_TAIL", "phone": "PHONE_KEEP_3_4"},
        }])
        self.assertEqual(partial[0]["column_rules"]["name"], "NAME_HEAD_TAIL")
        with self.assertRaisesRegex(ValueError, "不支持"):
            _normalize_batch_tables([{
                "table_name": "customer",
                "columns": ["name"],
                "column_rules": {"name": "UNKNOWN"},
            }])

    def test_insert_sql_uses_new_function_only_for_mapped_columns(self) -> None:
        sql = _build_insert_sql(
            "ODS",
            "ODS",
            "customer_sm4",
            "customer",
            ["id", "name", "phone"],
            ["name", "phone"],
            {"name": "NAME_HEAD_TAIL"},
        )
        self.assertIn("CQ_SM4_PARTIAL_ENCRYPT(`name`, 'NAME_HEAD_TAIL')", sql)
        self.assertIn("CQ_SM4_ENCRYPT(`phone`)", sql)
        self.assertNotIn("CQ_SM4_PARTIAL_ENCRYPT(`phone`", sql)

    def test_partial_output_length_is_checked_before_table_write(self) -> None:
        class Cursor:
            def execute(self, sql) -> None:
                self.sql = sql

            def fetchone(self):
                return {"max_0": 2001}

        cursor = Cursor()
        with self.assertRaisesRegex(ValueError, r"varchar\(2000\)"):
            _validate_partial_output_lengths(
                cursor,
                database="ODS",
                table_name="customer",
                column_rules={"address": "ADDRESS_KEEP_6_0"},
            )
        self.assertIn("CQ_SM4_PARTIAL_ENCRYPT(`address`, 'ADDRESS_KEEP_6_0')", cursor.sql)

    def test_file_decryption_supports_mixed_full_and_partial_columns(self) -> None:
        partial = sm4_partial_encrypt("张小三", self.key_seed, "NAME_HEAD_TAIL")
        full = sm4_encrypt_to_base64("完整秘密", self.key_seed)
        content = f"name,secret\n{partial},{full}\n".encode()
        result = decrypt_file_content(
            filename="sample.csv",
            content=content,
            decrypt_columns=["name", "secret"],
            column_rules={"name": "NAME_HEAD_TAIL"},
            sm4_key=self.key_seed,
        )
        self.assertEqual(result.decrypted_count, 2)
        self.assertIn("张小三", result.content)
        self.assertIn("完整秘密", result.content)

    def test_function_refresh_creates_and_verifies_partial_and_like_java_udfs(self) -> None:
        plaintext = "oracle-recovery-sm4-verify-test"
        ciphertext = sm4_encrypt_to_base64(plaintext, self.key_seed)
        partial_plaintext = "110101199001011234"
        partial_ciphertext = sm4_partial_encrypt(
            partial_plaintext,
            self.key_seed,
            "ID_KEEP_6_4",
        )
        jar = BuiltSm4Jar(
            filename="cq-sm4-encrypt-test-v6.jar",
            path=Path("cq-sm4-encrypt-test-v6.jar"),
            url="http://api/cq-sm4-encrypt-test-v6.jar",
            symbol="Encrypt",
            decrypt_symbol="Decrypt",
            key_seed=self.key_seed,
            key_fingerprint="test",
            verification_plaintext=plaintext,
            verification_ciphertext=ciphertext,
        )

        class Cursor:
            def __init__(self) -> None:
                self.sql: list[str] = []
                self.row = {"value": ""}

            def execute(self, sql, _params=None) -> None:
                self.sql.append(sql)
                if sql.startswith("SELECT CQ_SM4_ENCRYPT"):
                    self.row = {"value": ciphertext}
                elif sql.startswith("SELECT CQ_SM4_DECRYPT"):
                    self.row = {"value": plaintext}
                elif sql.startswith("SELECT CQ_SM4_PARTIAL_ENCRYPT"):
                    self.row = {"value": partial_ciphertext}
                elif sql.startswith("SELECT CQ_SM4_PARTIAL_DECRYPT"):
                    self.row = {"value": partial_plaintext}
                elif "CQ_SM4_LIKE" in sql:
                    self.row = {"value": "ok"}

            def fetchone(self):
                return self.row

        cursor = Cursor()
        result = _refresh_function_in_database(
            cursor,
            "ODS",
            "CQ_SM4_ENCRYPT",
            "CQ_SM4_DECRYPT",
            jar,
            like_enabled=True,
        )
        self.assertEqual(result.state, "success")
        combined = "\n".join(cursor.sql)
        self.assertIn("CREATE FUNCTION CQ_SM4_PARTIAL_ENCRYPT(STRING, STRING) RETURNS STRING", combined)
        self.assertIn("CREATE FUNCTION CQ_SM4_PARTIAL_DECRYPT(STRING, STRING) RETURNS STRING", combined)
        self.assertIn("CREATE FUNCTION CQ_SM4_LIKE(STRING, STRING, STRING) RETURNS BOOLEAN", combined)
        self.assertIn("CqSm4Like_test", combined)
        self.assertIn("SELECT IF(CQ_SM4_LIKE", combined)

    def test_ui_preserves_per_column_rules_in_task_and_file_flows(self) -> None:
        ui = (Path(__file__).parents[1] / "src/recovery_service/static/ui.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('data-doris-encrypt-rule=', ui)
        self.assertIn('column_rules: Object.fromEntries(', ui)
        self.assertIn('form.append("column_rules"', ui)
        self.assertIn('data-sm4-capability="like"', ui)
        self.assertIn("like_enabled:", ui)


if __name__ == "__main__":
    unittest.main()
