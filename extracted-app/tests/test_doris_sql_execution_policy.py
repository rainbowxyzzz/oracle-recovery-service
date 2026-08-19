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


if __name__ == "__main__":
    unittest.main()
