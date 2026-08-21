import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pymysql

from recovery_service.services.data_sync import (
    _align_target_schema_with_source,
    _create_target_table,
    execute_data_sync,
    _execute_one_table,
    _execute_insert_select_table,
    _is_retryable_stream_load_error,
    _build_table_mapping,
    _list_mysql_columns,
    _list_mysql_tables,
    _list_oracle_columns,
    _mapping_select_items,
    _normalize_config,
    _q,
    _resolve_sync_method,
    _source_column_to_doris_type,
    _stream_load,
    _stream_load_http_put,
    _switch_catalog,
    _table_exists,
)


class _FakeHttpResponse:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


class _FakeCursor:
    def __init__(self):
        self.executed: list[str] = []
        self.rowcount = 3

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if sql.startswith("INSERT INTO"):
            return self.rowcount
        return 0


class _FakeDb:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj


class _FakeRowsCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return len(self.rows)

    def fetchall(self):
        return self.rows


class _FakeRowsDb:
    def __init__(self, rows):
        self.cursor_obj = _FakeRowsCursor(rows)

    def cursor(self):
        return self.cursor_obj


class _FakeStreamingCursor:
    def __init__(self, batches):
        self.batches = list(batches)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if sql.startswith("SWITCH"):
            raise AssertionError(f"MySQL source must not execute {sql}")

    def fetchmany(self, size):
        if not self.batches:
            return []
        return self.batches.pop(0)


class _FakeStreamingDb:
    def __init__(self, batches):
        self.cursor_obj = _FakeStreamingCursor(batches)

    def cursor(self):
        return self.cursor_obj


class _FakeConnectionContext:
    def __enter__(self):
        return _FakeDb()

    def __exit__(self, exc_type, exc, tb):
        return False


class DataSyncTests(unittest.TestCase):
    def test_switch_catalog_does_not_mask_closed_connection(self) -> None:
        class ClosedCursor:
            def __init__(self):
                self.calls = 0

            def execute(self, sql):
                self.calls += 1
                raise pymysql.err.InterfaceError(0, "")

        cursor = ClosedCursor()
        with self.assertRaises(pymysql.err.InterfaceError) as raised:
            _switch_catalog(cursor, "external_catalog")

        self.assertEqual(raised.exception.args, (0, ""))
        self.assertEqual(cursor.calls, 1)

    def test_switch_catalog_only_retries_explicit_syntax_error(self) -> None:
        class SyntaxFallbackCursor:
            def __init__(self):
                self.executed = []

            def execute(self, sql):
                self.executed.append(sql)
                if len(self.executed) == 1:
                    raise pymysql.err.OperationalError(1064, "syntax error")

        cursor = SyntaxFallbackCursor()
        _switch_catalog(cursor, "external_catalog")

        self.assertEqual(cursor.executed, ["SWITCH `external_catalog`", "SWITCH external_catalog"])

    def test_table_exists_propagates_metadata_connection_error(self) -> None:
        with patch(
            "recovery_service.services.data_sync._list_tables",
            side_effect=pymysql.err.OperationalError(2013, "Lost connection"),
        ):
            with self.assertRaisesRegex(pymysql.err.OperationalError, "Lost connection"):
                _table_exists(_FakeDb(), "internal", "DWD", "CUSTOMER")

    def test_source_table_and_column_accept_existing_special_names(self) -> None:
        normalized = _normalize_config(
            {
                "source_connection_id": "source",
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "table_mappings": [
                    {
                        "id": "m1",
                        "source_schema": "ODS",
                        "source_table": "BIN$A1.Table Name",
                        "target_table": "CUSTOMER_SYNC",
                        "source_columns": [{"name": "AMOUNT$OLD"}],
                        "column_mappings": [
                            {
                                "target_name": "AMOUNT_OLD",
                                "source_name": "AMOUNT$OLD",
                                "enabled": True,
                            }
                        ],
                    }
                ],
            }
        )

        self.assertEqual(normalized["table_mappings"][0]["source_table"], "BIN$A1.Table Name")
        select_items, target_columns = _mapping_select_items(normalized["table_mappings"][0])
        self.assertEqual(select_items, ["`AMOUNT$OLD` AS `AMOUNT_OLD`"])
        self.assertEqual(target_columns, ["AMOUNT_OLD"])
        self.assertEqual(_q("BIN`A1"), "`BIN``A1`")

    def test_sync_method_defaults_to_auto_and_resolves_catalog(self) -> None:
        normalized = _normalize_config(
            {
                "source_connection_id": "source",
                "source_catalog": "hive_catalog",
                "source_schema": "ODS",
                "target_database": "DWD",
                "write_mode": "append",
                "table_mappings": [
                    {
                        "id": "m1",
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER",
                    }
                ],
            }
        )

        self.assertEqual(normalized["sync_method"], "auto")
        self.assertEqual(_resolve_sync_method("auto", "hive_catalog"), "insert_select")
        self.assertEqual(_resolve_sync_method("auto", "internal"), "insert_select")
        self.assertEqual(_resolve_sync_method("auto", "local_mysql", "mysql"), "stream_load")
        self.assertEqual(_resolve_sync_method("auto", "local_oracle", "oracle"), "stream_load")
        self.assertEqual(_resolve_sync_method("stream_load", "hive_catalog"), "stream_load")

    def test_mysql_source_forces_stream_load_and_rejects_insert_select(self) -> None:
        source = SimpleNamespace(engine="mysql")
        target = SimpleNamespace(engine="doris")
        config = {
            "source_connection_id": "source",
            "source_catalog": "local_mysql",
            "source_schema": "ODS",
            "target_connection_id": "target",
            "target_database": "DWD",
            "sync_method": "auto",
            "table_mappings": [
                {
                    "id": "m1",
                    "source_schema": "ODS",
                    "source_table": "CUSTOMER",
                    "target_table": "CUSTOMER",
                }
            ],
        }

        with patch("recovery_service.services.data_sync._source_conn", return_value=_FakeConnectionContext()), patch(
            "recovery_service.services.data_sync._doris_conn", return_value=_FakeConnectionContext()
        ), patch(
            "recovery_service.services.data_sync._execute_one_table",
            return_value={"loaded_rows": 1, "logs": [], "message": "ok"},
        ):
            result = execute_data_sync(source, target, config)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["table_results"][0]["loaded_rows"], 1)

        config["sync_method"] = "insert_select"
        with self.assertRaisesRegex(ValueError, "不能使用 Catalog"):
            execute_data_sync(source, target, config)

    def test_mysql_metadata_helpers_read_information_schema(self) -> None:
        table_db = _FakeRowsDb(
            [
                {
                    "TABLE_NAME": "CUSTOMER",
                    "TABLE_TYPE": "BASE TABLE",
                    "ENGINE": "InnoDB",
                    "TABLE_ROWS": 3,
                }
            ]
        )
        column_db = _FakeRowsDb(
            [
                {
                    "COLUMN_NAME": "ID",
                    "COLUMN_TYPE": "bigint",
                    "DATA_TYPE": "bigint",
                    "ORDINAL_POSITION": 1,
                    "IS_NULLABLE": "NO",
                },
                {
                    "COLUMN_NAME": "NAME",
                    "COLUMN_TYPE": "varchar(50)",
                    "DATA_TYPE": "varchar",
                    "ORDINAL_POSITION": 2,
                    "IS_NULLABLE": "YES",
                },
            ]
        )

        self.assertEqual(_list_mysql_tables(table_db, "ODS")[0]["name"], "CUSTOMER")
        columns = _list_mysql_columns(column_db, "ODS", "CUSTOMER")
        self.assertEqual([item["name"] for item in columns], ["ID", "NAME"])
        self.assertFalse(columns[0]["nullable"])
        self.assertEqual(_source_column_to_doris_type(columns[1]), "VARCHAR(100)")

    def test_oracle_metadata_columns_are_normalized_for_doris(self) -> None:
        db = _FakeRowsDb(
            [
                {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER", "DATA_LENGTH": 22, "DATA_PRECISION": 18, "DATA_SCALE": 0, "NULLABLE": "N", "COLUMN_ID": 1},
                {"COLUMN_NAME": "PHONE", "DATA_TYPE": "VARCHAR2", "DATA_LENGTH": 32, "DATA_PRECISION": None, "DATA_SCALE": None, "NULLABLE": "Y", "COLUMN_ID": 2},
            ]
        )

        columns = _list_oracle_columns(db, "RESTORED_SCHEMA", "CUSTOMER_SOURCE")

        self.assertEqual(columns[0], {"name": "ID", "type": "NUMBER(18,0)", "nullable": False, "ordinal": 1})
        self.assertEqual(columns[1], {"name": "PHONE", "type": "VARCHAR2(32)", "nullable": True, "ordinal": 2})

    def test_mysql_stream_load_source_query_does_not_switch_catalog(self) -> None:
        source = SimpleNamespace(engine="mysql")
        target = SimpleNamespace(engine="doris")
        source_db = _FakeStreamingDb([[{"id": 1}]])
        config = {
            "source_catalog": "local_mysql",
            "source_schema": "ceshi",
            "target_database": "CESHI",
            "write_mode": "append",
            "sync_method": "stream_load",
            "schema_policy": "target",
            "batch_size": 1000,
            "field_delimiter": ",",
            "line_delimiter": "\\n",
        }
        mapping = {
            "id": "m1",
            "source_schema": "ceshi",
            "source_table": "亚马逊订单应收核对-transaction报表",
            "target_database": "CESHI",
            "target_table": "亚马逊订单应收核对-transaction报表",
            "source_columns": [{"name": "id"}],
            "column_mappings": [{"target_name": "id", "source_name": "id", "enabled": True}],
        }

        with patch("recovery_service.services.data_sync._table_exists", return_value=True), patch(
            "recovery_service.services.data_sync._stream_load",
            return_value={"status": "Success", "number_loaded_rows": 1},
        ):
            result = _execute_one_table(source_db, _FakeDb(), source, target, config, mapping)

        self.assertEqual(result["loaded_rows"], 1)
        self.assertEqual(source_db.cursor_obj.executed, [
            "SELECT `id` AS `id` FROM `ceshi`.`亚马逊订单应收核对-transaction报表`"
        ])

    def test_stream_load_http_put_uses_raw_transport_for_chinese_columns(self) -> None:
        with patch(
            "recovery_service.services.data_sync._stream_load_raw_http_put",
            return_value=(200, {}, b'{"Status":"Success","NumberLoadedRows":1}'),
        ) as raw_put, patch("recovery_service.services.data_sync.urlrequest.urlopen") as urlopen:
            status, headers, body = _stream_load_http_put(
                "http://doris-fe:8030/api/CESHI/%E4%B8%AD%E6%96%87/_stream_load",
                b"payload",
                {"columns": "结算月份,账号", "format": "csv"},
                30,
            )

        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"Status":"Success","NumberLoadedRows":1}')
        raw_put.assert_called_once()
        urlopen.assert_not_called()

    def test_stream_load_quotes_columns_with_special_characters(self) -> None:
        profile = SimpleNamespace(host="doris.local", username="root", password_enc="")
        captured_headers = {}

        def fake_put(url, payload, headers, timeout):
            captured_headers.update(headers)
            return 200, {}, b'{"Status":"Success","NumberLoadedRows":1}'

        with patch("recovery_service.services.data_sync.decrypt_secret", return_value="pwd"), patch(
            "recovery_service.services.data_sync._stream_load_http_put",
            side_effect=fake_put,
        ):
            result = _stream_load(
                profile,
                "CESHI",
                "亚马逊订单应收核对-仓配报表数据",
                ["订单总金额(包含客户运费、平台补贴)(原币种)", "结算月份"],
                b"1,2026-08\n",
                {"stream_load_http_port": 8030},
                1,
            )

        self.assertEqual(result["status"], "Success")
        self.assertEqual(
            captured_headers["columns"],
            "`订单总金额(包含客户运费、平台补贴)(原币种)`,`结算月份`",
        )

    def test_invalid_sync_method_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _normalize_config(
                {
                    "source_connection_id": "source",
                    "source_catalog": "internal",
                    "source_schema": "ODS",
                    "target_database": "DWD",
                    "sync_method": "copy",
                    "table_mappings": [{"source_table": "CUSTOMER"}],
                }
            )

    def test_target_table_with_dot_is_allowed_as_quoted_object_name(self) -> None:
        normalized = _normalize_config(
            {
                "source_connection_id": "source",
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "table_mappings": [
                    {
                        "id": "m1",
                        "source_schema": "ODS",
                        "source_table": "CUSTOMER",
                        "target_table": "CUSTOMER.ARCHIVE",
                    }
                ],
            }
        )

        self.assertEqual(normalized["table_mappings"][0]["target_table"], "CUSTOMER.ARCHIVE")

    def test_disabled_invalid_mapping_does_not_block_selected_run(self) -> None:
        profile = SimpleNamespace(engine="doris")
        config = {
            "source_connection_id": "source",
            "source_catalog": "internal",
            "source_schema": "ODS",
            "target_database": "DWD",
            "selected_tables": ["good"],
            "table_mappings": [
                {
                    "id": "good",
                    "enabled": True,
                    "source_schema": "ODS",
                    "source_table": "CUSTOMER",
                    "target_table": "CUSTOMER",
                },
                {
                    "id": "bad",
                    "enabled": False,
                    "source_schema": "ODS",
                    "source_table": "CUSTOMER_BAD",
                    "target_table": "BAD\nTABLE",
                },
            ],
        }

        with patch("recovery_service.services.data_sync._doris_conn", return_value=_FakeConnectionContext()):
            with patch(
                "recovery_service.services.data_sync._execute_one_table",
                return_value={"loaded_rows": 2, "logs": [], "message": "ok"},
            ):
                result = execute_data_sync(profile, profile, config)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(len(result["config_patch"]["table_mappings"]), 2)

    def test_selected_invalid_mapping_error_includes_context(self) -> None:
        profile = SimpleNamespace(engine="doris")
        config = {
            "source_connection_id": "source",
            "source_catalog": "internal",
            "source_schema": "ODS",
            "target_database": "DWD",
            "selected_tables": ["bad"],
            "table_mappings": [
                {
                    "id": "bad",
                    "enabled": True,
                    "source_schema": "ODS",
                    "source_table": "CUSTOMER_BAD",
                    "target_table": "BAD\nTABLE",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "映射ID=bad.*BAD.*尚未生成 SQL"):
            execute_data_sync(profile, profile, config)

    def test_prewrite_connection_failure_recovers_and_retries_current_table(self) -> None:
        profile = SimpleNamespace(engine="doris")
        config = {
            "source_connection_id": "source",
            "source_catalog": "internal",
            "source_schema": "ODS",
            "target_database": "DWD",
            "connection_retry_attempts": 2,
            "connection_recovery_attempts": 1,
            "connection_recovery_interval_seconds": 0,
            "table_mappings": [
                {
                    "id": "m1",
                    "source_schema": "ODS",
                    "source_table": "CUSTOMER",
                    "target_table": "CUSTOMER",
                }
            ],
        }
        calls = 0

        def execute_one_table(*args, **kwargs):
            nonlocal calls
            calls += 1
            mapping = args[-1]
            if calls == 1:
                mapping["_runtime_stage"] = "source_metadata"
                raise pymysql.err.InterfaceError(0, "")
            return {"loaded_rows": 2, "logs": list(mapping["_runtime_logs"]), "message": "ok"}

        with patch("recovery_service.services.data_sync._doris_conn", return_value=_FakeConnectionContext()), patch(
            "recovery_service.services.data_sync._execute_one_table",
            side_effect=execute_one_table,
        ), patch("recovery_service.services.data_sync._probe_data_sync_connections") as probe:
            result = execute_data_sync(profile, profile, config)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(calls, 2)
        probe.assert_called_once()
        stages = [item["stage"] for item in result["table_results"][0]["logs"]]
        self.assertIn("connection_interrupted", stages)
        self.assertIn("connection_recovered", stages)
        self.assertIn("connection_retry", stages)

    def test_postwrite_connection_failure_is_not_retried(self) -> None:
        profile = SimpleNamespace(engine="doris")
        config = {
            "source_connection_id": "source",
            "source_catalog": "external_catalog",
            "source_schema": "ODS",
            "target_database": "DWD",
            "connection_retry_attempts": 3,
            "connection_recovery_attempts": 1,
            "connection_recovery_interval_seconds": 0,
            "table_mappings": [
                {
                    "id": "m1",
                    "source_schema": "ODS",
                    "source_table": "CUSTOMER",
                    "target_table": "CUSTOMER",
                }
            ],
        }
        calls = 0

        def execute_one_table(*args, **kwargs):
            nonlocal calls
            calls += 1
            mapping = args[-1]
            mapping["_runtime_stage"] = "insert_select"
            mapping["_runtime_last_sql"] = "INSERT INTO `DWD`.`CUSTOMER` SELECT * FROM source"
            mapping["_runtime_write_started"] = True
            raise pymysql.err.OperationalError(2013, "Lost connection during query")

        with patch("recovery_service.services.data_sync._doris_conn", return_value=_FakeConnectionContext()), patch(
            "recovery_service.services.data_sync._execute_one_table",
            side_effect=execute_one_table,
        ), patch("recovery_service.services.data_sync._probe_data_sync_connections") as probe:
            result = execute_data_sync(profile, profile, config)

        self.assertEqual(calls, 1)
        probe.assert_called_once()
        table = result["table_results"][0]
        self.assertEqual(table["status"], "failed")
        self.assertTrue(table["write_started"])
        self.assertIn("结果未知", table["message"])
        self.assertIn("禁止自动重试", table["message"])

    def test_recovery_exhaustion_skips_unstarted_tables(self) -> None:
        profile = SimpleNamespace(engine="doris")
        config = {
            "source_connection_id": "source",
            "source_catalog": "external_catalog",
            "source_schema": "ODS",
            "target_database": "DWD",
            "table_parallelism": 1,
            "connection_retry_attempts": 3,
            "connection_recovery_attempts": 1,
            "connection_recovery_interval_seconds": 0,
            "continue_on_error": True,
            "table_mappings": [
                {
                    "id": f"m{index}",
                    "source_schema": "ODS",
                    "source_table": f"CUSTOMER_{index}",
                    "target_table": f"CUSTOMER_{index}",
                }
                for index in range(1, 4)
            ],
        }

        def execute_one_table(*args, **kwargs):
            mapping = args[-1]
            mapping["_runtime_stage"] = "target_metadata"
            raise pymysql.err.InterfaceError(0, "")

        with patch("recovery_service.services.data_sync._doris_conn", return_value=_FakeConnectionContext()), patch(
            "recovery_service.services.data_sync._execute_one_table",
            side_effect=execute_one_table,
        ), patch(
            "recovery_service.services.data_sync._probe_data_sync_connections",
            side_effect=pymysql.err.OperationalError(2003, "connection refused"),
        ):
            result = execute_data_sync(profile, profile, config)

        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual([item["status"] for item in result["table_results"]], ["failed", "skipped", "skipped"])
        self.assertTrue(result["table_results"][0]["stop_remaining"])
        self.assertTrue(all(item.get("failure_type") == "infrastructure_unavailable" for item in result["table_results"][1:]))

    def test_parallel_connection_failures_share_one_recovery_probe(self) -> None:
        profile = SimpleNamespace(engine="doris")
        config = {
            "source_connection_id": "source",
            "source_catalog": "external_catalog",
            "source_schema": "ODS",
            "target_database": "DWD",
            "table_parallelism": 2,
            "connection_retry_attempts": 2,
            "connection_recovery_attempts": 1,
            "connection_recovery_interval_seconds": 0,
            "table_mappings": [
                {
                    "id": f"m{index}",
                    "source_schema": "ODS",
                    "source_table": f"CUSTOMER_{index}",
                    "target_table": f"CUSTOMER_{index}",
                }
                for index in range(1, 3)
            ],
        }
        first_attempt_barrier = threading.Barrier(2)
        calls: dict[str, int] = {}
        calls_lock = threading.Lock()

        def execute_one_table(*args, **kwargs):
            mapping = args[-1]
            mapping_id = mapping["id"]
            with calls_lock:
                calls[mapping_id] = calls.get(mapping_id, 0) + 1
                current = calls[mapping_id]
            if current == 1:
                mapping["_runtime_stage"] = "source_metadata"
                first_attempt_barrier.wait(timeout=2)
                raise pymysql.err.InterfaceError(0, "")
            return {"loaded_rows": 1, "logs": list(mapping["_runtime_logs"]), "message": "ok"}

        def probe_connections(*args, **kwargs):
            time.sleep(0.05)

        with patch("recovery_service.services.data_sync._doris_conn", return_value=_FakeConnectionContext()), patch(
            "recovery_service.services.data_sync._execute_one_table",
            side_effect=execute_one_table,
        ), patch(
            "recovery_service.services.data_sync._probe_data_sync_connections",
            side_effect=probe_connections,
        ) as probe:
            result = execute_data_sync(profile, profile, config)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(calls, {"m1": 2, "m2": 2})
        probe.assert_called_once()

    def test_schema_policy_defaults_to_target_for_existing_configs(self) -> None:
        normalized = _normalize_config(
            {
                "source_connection_id": "source",
                "source_catalog": "internal",
                "source_schema": "ODS",
                "target_database": "DWD",
                "table_mappings": [{"source_table": "CUSTOMER"}],
            }
        )

        self.assertEqual(normalized["schema_policy"], "target")
        self.assertEqual(normalized["table_mappings"][0]["schema_policy"], "target")

    def test_source_schema_policy_mapping_adds_source_columns_before_save(self) -> None:
        mapping = _build_table_mapping(
            source_schema="ODS",
            source_table="CUSTOMER",
            target_database="DWD",
            target_table="CUSTOMER",
            target_exists=True,
            source_columns=[
                {"name": "ID", "type": "NUMBER(18,0)"},
                {"name": "NAME", "type": "VARCHAR2(50)"},
            ],
            target_columns=[
                {"name": "ID", "type": "BIGINT"},
            ],
            schema_policy="source",
        )

        self.assertEqual(mapping["schema_policy"], "source")
        self.assertEqual([item["target_name"] for item in mapping["column_mappings"]], ["ID", "NAME"])
        self.assertEqual([item["source_name"] for item in mapping["column_mappings"]], ["ID", "NAME"])
        self.assertEqual(mapping["target_columns"][1]["type"], "VARCHAR(100)")

    def test_all_null_enabled_column_mapping_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "没有有效字段映射"):
            _mapping_select_items(
                {
                    "source_table": "TEST01",
                    "source_columns": [{"name": "ID"}],
                    "column_mappings": [
                        {
                            "enabled": True,
                            "target_name": "NAM22222E",
                            "source_name": "",
                            "fixed_value": "",
                        }
                    ],
                }
            )

    def test_source_column_type_maps_to_doris_common_types(self) -> None:
        self.assertEqual(_source_column_to_doris_type({"type": "VARCHAR2(100)"}), "VARCHAR(200)")
        self.assertEqual(_source_column_to_doris_type({"type": "CHAR(200)"}), "CHAR(255)")
        self.assertEqual(_source_column_to_doris_type({"type": "NUMBER(8,0)"}), "INT")
        self.assertEqual(_source_column_to_doris_type({"type": "NUMBER(18,0)"}), "BIGINT")
        self.assertEqual(_source_column_to_doris_type({"type": "NUMBER(30,0)"}), "LARGEINT")
        self.assertEqual(_source_column_to_doris_type({"type": "DECIMAL(12,2)"}), "DECIMAL(12,2)")
        self.assertEqual(_source_column_to_doris_type({"type": "CLOB"}), "STRING")
        self.assertEqual(_source_column_to_doris_type({"type": "TIMESTAMP"}), "DATETIME")

    def test_create_target_table_uses_mapped_source_types(self) -> None:
        db = _FakeDb()

        _create_target_table(
            db,
            "DWD",
            "CUSTOMER",
            [
                {"name": "ID", "type": "NUMBER(18,0)"},
                {"name": "NAME", "type": "VARCHAR2(50)"},
                {"name": "AMOUNT", "type": "DECIMAL(12,2)"},
            ],
        )

        create_sql = next(sql for sql in db.cursor_obj.executed if sql.startswith("CREATE TABLE"))
        self.assertIn("`ID` BIGINT NULL", create_sql)
        self.assertIn("`NAME` VARCHAR(100) NULL", create_sql)
        self.assertIn("`AMOUNT` DECIMAL(12,2) NULL", create_sql)
        self.assertNotIn("VARCHAR(65533)", create_sql)

    def test_source_schema_policy_adds_missing_target_columns(self) -> None:
        db = _FakeDb()
        logs: list[dict] = []

        target_columns, added = _align_target_schema_with_source(
            db,
            "DWD",
            "CUSTOMER",
            [
                {"name": "B", "type": "INT"},
                {"name": "C", "type": "VARCHAR(20)"},
                {"name": "D", "type": "VARCHAR(10)"},
            ],
            [
                {"name": "B", "type": "INT"},
                {"name": "C", "type": "VARCHAR(40)"},
            ],
            logs,
        )

        self.assertEqual(added, ["D"])
        self.assertEqual(target_columns[-1]["type"], "VARCHAR(20)")
        self.assertIn("ALTER TABLE `DWD`.`CUSTOMER` ADD COLUMN `D` VARCHAR(20) NULL", db.cursor_obj.executed)
        self.assertEqual(logs[0]["stage"], "schema_align")

    def test_insert_select_executes_on_target_doris_with_catalog_source(self) -> None:
        db = _FakeDb()
        logs: list[dict] = []

        result = _execute_insert_select_table(
            db,
            "hive_catalog",
            "ODS",
            "CUSTOMER",
            "DWD",
            "CUSTOMER_SYNC",
            ["ID", "NAME"],
            ["`ID` AS `ID`", "`NAME` AS `NAME`"],
            logs,
        )

        self.assertEqual(result["loaded_rows"], 3)
        self.assertEqual(result["status"], "submitted")
        self.assertIn("SWITCH `internal`", db.cursor_obj.executed)
        self.assertIn(
            "INSERT INTO `DWD`.`CUSTOMER_SYNC` (`ID`, `NAME`) SELECT `ID` AS `ID`, `NAME` AS `NAME` FROM `hive_catalog`.`ODS`.`CUSTOMER`",
            db.cursor_obj.executed,
        )
        self.assertEqual(logs[0]["stage"], "insert_select")

    def test_stream_load_retries_fe_master_heartbeat_error(self) -> None:
        profile = SimpleNamespace(host="doris.local", username="root", password_enc="")
        responses = [
            _FakeHttpResponse(
                {
                    "Status": "Fail",
                    "Message": "[E-240] Have not get FE Master heartbeat yet",
                }
            ),
            _FakeHttpResponse(
                {
                    "Status": "Success",
                    "NumberTotalRows": 1,
                    "NumberLoadedRows": 1,
                    "LoadBytes": 4,
                }
            ),
        ]
        requested_urls: list[str] = []

        def fake_urlopen(req, timeout):
            requested_urls.append(req.full_url)
            return responses.pop(0)

        with patch("recovery_service.services.data_sync.decrypt_secret", return_value="pwd"), patch(
            "recovery_service.services.data_sync.urlrequest.urlopen",
            side_effect=fake_urlopen,
        ), patch("recovery_service.services.data_sync.time.sleep") as sleep:
            result = _stream_load(
                profile,
                "DWD",
                "A1 Target",
                ["ID"],
                b"1\n",
                {
                    "stream_load_http_port": 8030,
                    "stream_load_retry_attempts": 2,
                    "stream_load_retry_sleep_seconds": 0,
                },
                1,
            )

        self.assertEqual(result["status"], "Success")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(requested_urls), 2)
        self.assertIn("/api/DWD/A1%20Target/_stream_load", requested_urls[0])
        sleep.assert_called_once()

    def test_retryable_stream_load_error_detects_e240_message(self) -> None:
        self.assertTrue(_is_retryable_stream_load_error("[E-240] Have not get FE Master heartbeat yet"))
        self.assertFalse(_is_retryable_stream_load_error("too many filtered rows"))


if __name__ == "__main__":
    unittest.main()
