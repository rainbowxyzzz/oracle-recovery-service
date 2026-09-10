import unittest
from unittest.mock import patch

from recovery_service.core.models.task import DatabaseConnectionProfile
from recovery_service.services.doris_sql_etl import execute_doris_sql


class _FakeCursor:
    description = None
    rowcount = 1

    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql):
        self.executed.append(sql)


class _FakeConnection:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return _FakeCursor(self.executed)


class DorisSqlExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.profile = DatabaseConnectionProfile(
            name="Doris",
            engine="doris",
            host="127.0.0.1",
            port=9030,
            username="tester",
            password_enc="",
        )

    def test_write_ddl_and_permission_sql_are_forwarded_without_confirmation(self):
        statements = [
            "INSERT INTO db.t VALUES (1)",
            "UPDATE db.t SET value = 2",
            "DELETE FROM db.t WHERE id = 1",
            "CREATE TABLE db.t2 (id INT)",
            "ALTER TABLE db.t2 ADD COLUMN value INT",
            "TRUNCATE TABLE db.t2",
            "DROP TABLE db.t2",
            "GRANT SELECT_PRIV ON db.t TO 'reader'@'%'",
            "REVOKE SELECT_PRIV ON db.t FROM 'reader'@'%'",
        ]
        executed = []

        with patch(
            "recovery_service.services.doris_sql_etl._doris_conn",
            side_effect=lambda profile, database: _FakeConnection(executed),
        ):
            for statement in statements:
                result = execute_doris_sql(
                    self.profile,
                    database=None,
                    sql=statement,
                    confirm_dangerous=False,
                )
                self.assertEqual(result.sql_type, statement.split()[0])

        self.assertEqual(executed, statements)

    def test_restricted_execution_uses_security_sql_resolver(self):
        executed = []
        with (
            patch(
                "recovery_service.services.security_access.resolve_security_sql",
                return_value={"sql": "SELECT PHONE FROM ACCESS.CASE_RAW", "mappings": ["mapping"]},
            ) as resolver,
            patch(
                "recovery_service.services.doris_sql_etl._doris_conn",
                side_effect=lambda profile, database: _FakeConnection(executed),
            ),
        ):
            result = execute_doris_sql(
                self.profile,
                database="ODS",
                sql="SELECT PHONE FROM ODS.CASE_RAW",
                security_access_mode="restricted",
            )
        self.assertEqual(result.sql_type, "SELECT")
        resolver.assert_called_once()
        self.assertEqual(executed, ["SELECT PHONE FROM ACCESS.CASE_RAW"])

    def test_protected_etl_execution_uses_frozen_security_context(self):
        executed = []
        security_context = {"field_contracts": [{"contract_hash": "x" * 64}]}
        with (
            patch(
                "recovery_service.services.security_access.resolve_protected_etl_sql",
                return_value={"sql": "INSERT INTO DWD.CASE_STANDARD SELECT * FROM ACCESS.CASE_RAW", "mappings": ["mapping"]},
            ) as resolver,
            patch(
                "recovery_service.services.doris_sql_etl._doris_conn",
                side_effect=lambda profile, database: _FakeConnection(executed),
            ),
        ):
            result = execute_doris_sql(
                self.profile,
                database="ODS",
                sql="INSERT INTO DWD.CASE_STANDARD SELECT * FROM ODS.CASE_RAW",
                security_access_mode="protected_etl",
                security_access_context=security_context,
            )
        self.assertEqual(result.sql_type, "INSERT")
        resolver.assert_called_once_with(
            connection_id=self.profile.id,
            default_database="ODS",
            sql="INSERT INTO DWD.CASE_STANDARD SELECT * FROM ODS.CASE_RAW",
            security_context=security_context,
        )
        self.assertEqual(executed, ["INSERT INTO DWD.CASE_STANDARD SELECT * FROM ACCESS.CASE_RAW"])


if __name__ == "__main__":
    unittest.main()
