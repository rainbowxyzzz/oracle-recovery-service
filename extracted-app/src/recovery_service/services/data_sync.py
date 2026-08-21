from __future__ import annotations

import base64
import csv
import io
import json
import re
import socket
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlunparse

import pymysql
import oracledb
from pymysql.cursors import DictCursor

from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import DatabaseConnectionProfile
from recovery_service.settings import get_settings

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_BATCH_SIZE = 1000
_DEFAULT_STREAM_LOAD_RETRY_ATTEMPTS = 3
_DEFAULT_STREAM_LOAD_RETRY_SLEEP_SECONDS = 1.0
_DEFAULT_CONNECTION_RETRY_ATTEMPTS = 3
_DEFAULT_CONNECTION_RECOVERY_ATTEMPTS = 30
_DEFAULT_CONNECTION_RECOVERY_INTERVAL_SECONDS = 10.0
_RETRYABLE_CONNECTION_ERROR_CODES = {0, 2006, 2013, 2014, 2055}
_SYNC_METHODS = {"auto", "insert_select", "stream_load"}
_SOURCE_ENGINES = {"doris", "mysql", "oracle"}
_SCHEMA_POLICIES = {"source", "target"}
_DORIS_MAX_VARCHAR_LENGTH = 65533
_DORIS_MAX_CHAR_LENGTH = 255
_DORIS_MAX_DECIMAL_PRECISION = 38
DataSyncEventHook = Callable[[str, dict[str, Any]], None]


class DataSyncInfrastructureUnavailable(RuntimeError):
    pass


class _DataSyncConnectionRecovery:
    def __init__(
        self,
        source_profile: DatabaseConnectionProfile,
        target_profile: DatabaseConnectionProfile,
        config: dict[str, Any],
    ) -> None:
        self.source_profile = source_profile
        self.target_profile = target_profile
        self.config = config
        self._condition = threading.Condition()
        self._recovering = False
        self._generation = 0
        self._failed_generation: int | None = None

    def wait_ready(self) -> tuple[int, bool]:
        with self._condition:
            while self._recovering:
                self._condition.wait()
            return self._generation, self._failed_generation == self._generation

    def recover(
        self,
        failed_generation: int,
        log_sink: Callable[[dict[str, Any]], None],
    ) -> bool:
        with self._condition:
            if self._generation != failed_generation:
                return True
            if self._failed_generation == failed_generation:
                return False
            if self._recovering:
                while self._recovering and self._generation == failed_generation:
                    self._condition.wait()
                return self._generation != failed_generation
            self._recovering = True

        recovered = False
        attempts = _clean_connection_recovery_attempts(self.config.get("connection_recovery_attempts"))
        interval = _clean_connection_recovery_interval_seconds(
            self.config.get("connection_recovery_interval_seconds")
        )
        log_sink(
            _log(
                "WARNING",
                "connection_recovery",
                f"检测到 Doris/Catalog 连接异常，暂停新表并开始健康恢复，最多探测 {attempts} 次。",
                {"max_attempts": attempts, "interval_seconds": interval},
            )
        )
        last_error: Exception | None = None
        try:
            for attempt in range(1, attempts + 1):
                try:
                    _probe_data_sync_connections(self.source_profile, self.target_profile, self.config)
                    recovered = True
                    log_sink(
                        _log(
                            "INFO",
                            "connection_recovered",
                            f"Doris/Catalog 健康探测第 {attempt} 次成功，恢复当前表执行。",
                            {"attempt": attempt},
                        )
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    log_sink(
                        _log(
                            "WARNING" if attempt < attempts else "ERROR",
                            "connection_probe_failed",
                            f"Doris/Catalog 健康探测第 {attempt} 次失败：{_exception_summary(exc)}",
                            {
                                "attempt": attempt,
                                "exception_type": type(exc).__name__,
                                "error_code": _exception_code(exc),
                            },
                        )
                    )
                    if attempt < attempts:
                        time.sleep(interval)
        finally:
            with self._condition:
                if recovered:
                    self._generation += 1
                    self._failed_generation = None
                else:
                    self._failed_generation = failed_generation
                self._recovering = False
                self._condition.notify_all()
        if not recovered and last_error:
            log_sink(
                _log(
                    "ERROR",
                    "connection_recovery_exhausted",
                    f"Doris/Catalog 在恢复窗口内仍不可用：{_exception_summary(last_error)}",
                    {
                        "exception_type": type(last_error).__name__,
                        "error_code": _exception_code(last_error),
                    },
                )
            )
        return recovered


def recognize_data_sync_mappings(
    source_profile: DatabaseConnectionProfile,
    target_profile: DatabaseConnectionProfile,
    *,
    source_catalog: str,
    source_schema: str,
    target_database: str,
    schema_policy: str = "source",
) -> dict[str, Any]:
    """Read Doris metadata and build table/column mappings for a data sync task."""

    _ensure_supported_source_profile(source_profile)
    _ensure_doris_profile(target_profile)
    clean_source_catalog = _clean_source_catalog(source_profile, source_catalog)
    clean_source_schema = _clean_identifier(source_schema, "源 Schema")
    clean_target_database = _clean_identifier(target_database, "目标库")

    clean_schema_policy = _clean_schema_policy(schema_policy)

    with _source_conn(source_profile, None) as source_db, _doris_conn(target_profile, None) as target_db:
        source_tables = _list_source_tables(source_db, source_profile, clean_source_catalog, clean_source_schema)
        target_tables = {
            item["name"]: item
            for item in _list_tables(target_db, "internal", clean_target_database)
        }
        table_mappings: list[dict[str, Any]] = []
        for table in source_tables:
            source_table = table["name"]
            source_columns = _list_source_columns(
                source_db,
                source_profile,
                clean_source_catalog,
                clean_source_schema,
                source_table,
            )
            target_table = source_table
            target_exists = target_table in target_tables
            target_columns = (
                _list_columns(target_db, "internal", clean_target_database, target_table)
                if target_exists
                else [_auto_target_column(column) for column in source_columns]
            )
            table_mappings.append(
                _build_table_mapping(
                    source_schema=clean_source_schema,
                    source_table=source_table,
                    target_database=clean_target_database,
                    target_table=target_table,
                    target_exists=target_exists,
                    source_columns=source_columns,
                    target_columns=target_columns,
                    schema_policy=clean_schema_policy,
                )
            )

    return {
        "source_catalog": clean_source_catalog,
        "source_schema": clean_source_schema,
        "target_database": clean_target_database,
        "schema_policy": clean_schema_policy,
        "table_count": len(table_mappings),
        "matched_count": sum(1 for item in table_mappings if item.get("target_exists")),
        "unmatched_count": sum(1 for item in table_mappings if not item.get("target_exists")),
        "table_mappings": table_mappings,
        "message": f"识别完成：源端 {len(table_mappings)} 张表，同名目标表 {sum(1 for item in table_mappings if item.get('target_exists'))} 张。",
    }


def execute_data_sync(
    source_profile: DatabaseConnectionProfile,
    target_profile: DatabaseConnectionProfile,
    config: dict[str, Any],
    event_hook: DataSyncEventHook | None = None,
) -> dict[str, Any]:
    _ensure_supported_source_profile(source_profile)
    _ensure_doris_profile(target_profile)
    config = dict(config or {})
    raw_mappings = [dict(item or {}) for item in config.get("table_mappings") or []]
    selected = {str(item) for item in config.get("selected_tables") or [] if str(item).strip()}
    if raw_mappings:
        runnable_raw = [
            item
            for item in raw_mappings
            if item.get("enabled", True)
            and (
                not selected
                or str(item.get("id") or item.get("source_table")) in selected
                or str(item.get("source_table")) in selected
            )
        ]
        scoped_config = dict(config)
        scoped_config["table_mappings"] = runnable_raw
        normalized = _normalize_config(scoped_config)
        all_original_mappings = raw_mappings
    else:
        normalized = _normalize_config(config)
        all_original_mappings = list(normalized.get("table_mappings") or [])
    if normalized.get("mode") == "legacy":
        normalized = _legacy_to_batch_config(normalized)
    normalized["source_engine"] = source_profile.engine
    normalized["source_catalog"] = _clean_source_catalog(source_profile, normalized.get("source_catalog"))
    resolved_method = _resolve_sync_method(
        normalized.get("sync_method"),
        str(normalized.get("source_catalog") or "internal"),
        source_profile.engine,
    )
    if source_profile.engine != "doris" and resolved_method == "insert_select":
        raise ValueError("源连接不是 Doris 时不能使用 Catalog 联邦查询，请选择 Stream Load 或自动选择。")

    table_mappings = [dict(item) for item in normalized.get("table_mappings") or []]
    runnable = [
        item
        for item in table_mappings
        if item.get("enabled", True) and (not selected or str(item.get("id") or item.get("source_table")) in selected or str(item.get("source_table")) in selected)
    ]
    if not runnable:
        raise ValueError("数据同步没有可运行的表映射。")

    started = time.monotonic()
    table_results: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = [_log("INFO", "start", f"准备执行 {len(runnable)} 张表的数据同步。")]
    success_count = 0
    failed_count = 0
    skipped_count = 0
    total_rows = 0
    now_text = app_now().isoformat()
    table_parallelism = _clean_table_parallelism(normalized.get("table_parallelism"))
    normalized["table_parallelism"] = table_parallelism
    _emit_event(
        event_hook,
        "run_prepared",
        {
            "table_count": len(runnable),
            "table_parallelism": table_parallelism,
            "mappings": [_table_event_mapping(item, normalized) for item in runnable],
        },
    )
    _emit_event(event_hook, "run_log", {"log": logs[0]})
    connection_recovery = _DataSyncConnectionRecovery(source_profile, target_profile, normalized)

    def execute_mapping(index: int, mapping: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _emit_event(event_hook, "table_started", {"mapping": _table_event_mapping(mapping, normalized)})
        mapping["_runtime_logs"] = []

        def log_sink(log: dict[str, Any]) -> None:
            _emit_event(
                event_hook,
                "table_log",
                {"mapping": _table_event_mapping(mapping, normalized), "log": log},
            )

        def record_runtime_log(log: dict[str, Any]) -> None:
            mapping.setdefault("_runtime_logs", []).append(log)
            log_sink(log)

        max_attempts = _clean_connection_retry_attempts(normalized.get("connection_retry_attempts"))
        final_error: Exception | None = None
        failure_type = "execution"
        stop_remaining = False
        attempts_used = 0
        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            generation, unavailable = connection_recovery.wait_ready()
            if unavailable:
                final_error = DataSyncInfrastructureUnavailable(
                    "Doris/Catalog 在共享恢复窗口内未恢复，当前表未建立新连接。"
                )
                failure_type = "connection"
                stop_remaining = True
                break
            mapping["_runtime_attempt"] = attempt
            mapping["_runtime_stage"] = "connect"
            mapping["_runtime_write_started"] = False
            mapping["_runtime_last_sql"] = None
            try:
                with ExitStack() as stack:
                    mapping["_runtime_stage"] = "connect_source"
                    source_db = stack.enter_context(_source_conn(source_profile, None))
                    mapping["_runtime_stage"] = "connect_target"
                    target_db = stack.enter_context(_doris_conn(target_profile, None))
                    result = _execute_one_table(
                        source_db,
                        target_db,
                        source_profile,
                        target_profile,
                        normalized,
                        mapping,
                        log_sink=log_sink,
                    )
                result["status"] = "succeeded"
                result["last_success_at"] = now_text
                result["connection_attempts"] = attempt
                mapping["last_success_at"] = now_text
                mapping["last_result"] = result
                _emit_event(
                    event_hook,
                    "table_finished",
                    {"mapping": _table_event_mapping(mapping, normalized), "result": _table_result_summary(result)},
                )
                return index, result
            except Exception as exc:
                final_error = exc
                if not _is_retryable_connection_error(exc):
                    break
                failure_type = "connection"
                write_started = bool(mapping.get("_runtime_write_started"))
                record_runtime_log(
                    _log(
                        "ERROR" if write_started else "WARNING",
                        "connection_interrupted",
                        (
                            f"第 {attempt} 次执行在阶段 {mapping.get('_runtime_stage') or 'unknown'} 发生连接中断："
                            f"{_exception_summary(exc)}"
                        ),
                        {
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "stage": mapping.get("_runtime_stage") or "unknown",
                            "exception_type": type(exc).__name__,
                            "error_code": _exception_code(exc),
                            "write_started": write_started,
                            "sql_generated": bool(_latest_runtime_sql(mapping)),
                            "sql": _latest_runtime_sql(mapping),
                        },
                    )
                )
                recovered = connection_recovery.recover(generation, record_runtime_log)
                if not recovered:
                    stop_remaining = True
                    break
                if write_started:
                    break
                if attempt < max_attempts:
                    record_runtime_log(
                        _log(
                            "INFO",
                            "connection_retry",
                            f"连接已恢复，使用全新连接重试当前表，第 {attempt + 1}/{max_attempts} 次。",
                            {"next_attempt": attempt + 1, "max_attempts": max_attempts},
                        )
                    )
                    continue
                break

        assert final_error is not None
        source_table = str(mapping.get("source_table") or "")
        target_table = str(mapping.get("target_table") or "")
        context_message = _mapping_error_message(mapping, final_error)
        runtime_logs = list(mapping.get("_runtime_logs") or [])
        failed_log = _log(
            "ERROR",
            "failed",
            context_message,
            {
                "mapping_id": str(mapping.get("id") or ""),
                "source_schema": str(mapping.get("source_schema") or normalized.get("source_schema") or ""),
                "source_table": source_table,
                "target_database": str(mapping.get("target_database") or normalized.get("target_database") or ""),
                "target_table": target_table,
                "stage": mapping.get("_runtime_stage") or "unknown",
                "exception_type": type(final_error).__name__,
                "error_code": _exception_code(final_error),
                "connection_attempts": attempts_used,
                "write_started": bool(mapping.get("_runtime_write_started")),
                "sql_generated": bool(_latest_runtime_sql(mapping)),
                "sql": _latest_runtime_sql(mapping),
                "failure_type": failure_type,
            },
        )
        runtime_logs.append(failed_log)
        log_sink(failed_log)
        result = {
            "table_id": str(mapping.get("id") or ""),
            "source_catalog": str(normalized.get("source_catalog") or ""),
            "source_schema": str(mapping.get("source_schema") or normalized.get("source_schema") or ""),
            "source_table": source_table,
            "target_database": str(mapping.get("target_database") or normalized.get("target_database") or ""),
            "target_table": target_table,
            "sync_method": _resolve_sync_method(
                normalized.get("sync_method"),
                str(normalized.get("source_catalog") or "internal"),
                source_profile.engine,
            ),
            "write_mode": normalized.get("write_mode"),
            "schema_policy": mapping.get("schema_policy") or normalized.get("schema_policy"),
            "status": "failed",
            "message": context_message,
            "logs": runtime_logs,
            "failure_type": failure_type,
            "connection_attempts": attempts_used,
            "write_started": bool(mapping.get("_runtime_write_started")),
            "stop_remaining": stop_remaining,
        }
        mapping["last_result"] = result
        _emit_event(
            event_hook,
            "table_failed",
            {"mapping": _table_event_mapping(mapping, normalized), "result": _table_result_summary(result)},
        )
        return index, result

    indexed_results: dict[int, dict[str, Any]] = {}
    infrastructure_stop_message: str | None = None
    if table_parallelism <= 1 or len(runnable) <= 1:
        for index, mapping in enumerate(runnable):
            _, result = execute_mapping(index, mapping)
            indexed_results[index] = result
            if result.get("status") == "succeeded":
                success_count += 1
                total_rows += int(result.get("loaded_rows") or 0)
            else:
                failed_count += 1
                if result.get("stop_remaining"):
                    infrastructure_stop_message = result.get("message") or "Doris/Catalog 基础设施未恢复。"
                if infrastructure_stop_message or not normalized.get("continue_on_error", True):
                    for skipped_index in range(index + 1, len(runnable)):
                        skipped = (
                            _infrastructure_skipped_table_result(
                                runnable[skipped_index], normalized, infrastructure_stop_message
                            )
                            if infrastructure_stop_message
                            else _skipped_table_result(runnable[skipped_index], normalized)
                        )
                        indexed_results[skipped_index] = skipped
                        runnable[skipped_index]["last_result"] = skipped
                        skipped_count += 1
                        _emit_event(
                            event_hook,
                            "table_skipped",
                            {
                                "mapping": _table_event_mapping(runnable[skipped_index], normalized),
                                "result": _table_result_summary(skipped),
                            },
                        )
                    break
    else:
        next_index = 0
        stop_submit = False
        with ThreadPoolExecutor(max_workers=table_parallelism) as executor:
            futures = {}

            def submit_next() -> None:
                nonlocal next_index
                if next_index >= len(runnable) or stop_submit:
                    return
                future = executor.submit(execute_mapping, next_index, runnable[next_index])
                futures[future] = next_index
                next_index += 1

            for _ in range(min(table_parallelism, len(runnable))):
                submit_next()
            while futures:
                done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future, None)
                    index, result = future.result()
                    indexed_results[index] = result
                    if result.get("status") == "succeeded":
                        success_count += 1
                        total_rows += int(result.get("loaded_rows") or 0)
                    else:
                        failed_count += 1
                        if result.get("stop_remaining"):
                            infrastructure_stop_message = result.get("message") or "Doris/Catalog 基础设施未恢复。"
                            stop_submit = True
                        elif not normalized.get("continue_on_error", True):
                            stop_submit = True
                    if not stop_submit:
                        submit_next()
            if stop_submit and next_index < len(runnable):
                for skipped_index in range(next_index, len(runnable)):
                    skipped = (
                        _infrastructure_skipped_table_result(
                            runnable[skipped_index], normalized, infrastructure_stop_message
                        )
                        if infrastructure_stop_message
                        else _skipped_table_result(runnable[skipped_index], normalized)
                    )
                    indexed_results[skipped_index] = skipped
                    runnable[skipped_index]["last_result"] = skipped
                    skipped_count += 1
                    _emit_event(
                        event_hook,
                        "table_skipped",
                        {
                            "mapping": _table_event_mapping(runnable[skipped_index], normalized),
                            "result": _table_result_summary(skipped),
                        },
                    )

    for index in range(len(runnable)):
        result = indexed_results.get(index)
        if not result:
            continue
        table_results.append(result)
        logs.extend(result.get("logs") or [])

    status = "succeeded" if failed_count == 0 else "partial" if success_count else "failed"
    updated_mappings = []
    by_id = {str(item.get("id") or item.get("source_table")): item for item in table_mappings}
    for original in all_original_mappings or normalized.get("table_mappings") or []:
        key = str(original.get("id") or original.get("source_table"))
        updated_mappings.append(by_id.get(key, original))

    message = f"数据同步完成：成功 {success_count} 张表，失败 {failed_count} 张表，跳过 {skipped_count} 张表，写入 {total_rows} 行。"
    finish_log = _log("INFO" if failed_count == 0 else "ERROR", "finish", message)
    _emit_event(event_hook, "run_log", {"log": finish_log})
    return {
        "status": status,
        "message": message,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "loaded_rows": total_rows,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "table_parallelism": table_parallelism,
        "table_results": table_results,
        "logs": logs + [finish_log],
        "config_patch": {
            "table_mappings": updated_mappings,
            "last_run_at": app_now().isoformat(),
            "last_run_status": status,
            "last_run_message": message,
            "last_success_at": now_text if failed_count == 0 else normalized.get("last_success_at"),
        },
    }


def _execute_one_table(
    source_db,
    target_db,
    source_profile: DatabaseConnectionProfile,
    target_profile: DatabaseConnectionProfile,
    config: dict[str, Any],
    mapping: dict[str, Any],
    log_sink: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    runtime_logs: list[dict[str, Any]] = mapping.setdefault("_runtime_logs", [])
    mapping["_runtime_stage"] = "prepare"
    mapping["_runtime_write_started"] = False
    mapping["_runtime_last_sql"] = None

    def add_log(level: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        log = _log(level, stage, message, payload)
        runtime_logs.append(log)
        if log_sink:
            log_sink(log)

    source_catalog = _clean_source_catalog(source_profile, config.get("source_catalog"))
    source_schema = _clean_identifier(mapping.get("source_schema") or config.get("source_schema"), "源 Schema")
    source_table = _clean_existing_identifier(mapping.get("source_table"), "源表")
    target_database = _clean_identifier(mapping.get("target_database") or config.get("target_database"), "目标库")
    target_table = _clean_existing_identifier(mapping.get("target_table") or source_table, "目标表")
    write_mode = _clean_write_mode(config.get("write_mode"))
    sync_method = _resolve_sync_method(config.get("sync_method"), source_catalog, source_profile.engine)
    if source_profile.engine != "doris" and sync_method == "insert_select":
        raise ValueError("源连接不是 Doris 时不能使用 Catalog 联邦查询，请选择 Stream Load 或自动选择。")
    schema_policy = _clean_schema_policy(mapping.get("schema_policy") or config.get("schema_policy"))
    batch_size = _clean_batch_size(config.get("batch_size"))
    added_columns: list[str] = []
    add_log(
        "INFO",
        "prepare",
        f"开始同步 {source_catalog}.{source_schema}.{source_table} -> internal.{target_database}.{target_table}。",
        {"connection_attempt": int(mapping.get("_runtime_attempt") or 1)},
    )
    add_log(
        "INFO",
        "sync_method",
        "同步方式：Catalog 联邦查询（INSERT SELECT）。" if sync_method == "insert_select" else "同步方式：Stream Load。",
        {"sync_method": sync_method, "configured_sync_method": config.get("sync_method") or "auto"},
    )
    if source_profile.engine in {"mysql", "oracle"}:
        add_log(
            "INFO",
            "local_source_push",
            f"同步方式：本地 {source_profile.engine.upper()} 推送 Stream Load。源库由当前 Worker 读取，Doris 不通过 Catalog 反向访问源库。",
            {"sync_method": sync_method, "source_engine": source_profile.engine, "local_push": True},
        )
    mapping["_runtime_stage"] = "target_metadata"
    target_exists = _table_exists(target_db, "internal", target_database, target_table)

    if not target_exists:
        if not mapping.get("auto_create", True):
            raise ValueError(f"目标表 {target_database}.{target_table} 不存在，且当前映射未允许自动建表。")
        mapping["_runtime_stage"] = "source_metadata"
        source_columns = mapping.get("source_columns") or _list_source_columns(
            source_db,
            source_profile,
            source_catalog,
            source_schema,
            source_table,
        )
        mapping["_runtime_stage"] = "create_target_table"
        create_sql = _create_target_table(
            target_db,
            target_database,
            target_table,
            source_columns,
            before_execute=lambda sql: _mark_write_started(mapping, "create_target_table", sql),
        )
        add_log(
            "INFO",
            "create_table",
            f"目标表不存在，自动创建 {target_database}.{target_table}，字段数 {len(source_columns)}。",
            {"sql": create_sql, "source_columns": [item.get("name") for item in source_columns]},
        )
        target_exists = True
        mapping["target_exists"] = True
        mapping["target_columns"] = [_auto_target_column(item) for item in source_columns]

    if schema_policy == "source":
        mapping["_runtime_stage"] = "source_metadata"
        source_columns = _list_source_columns(source_db, source_profile, source_catalog, source_schema, source_table)
        mapping["_runtime_stage"] = "target_metadata"
        target_columns = _list_columns(target_db, "internal", target_database, target_table)
        log_start = len(runtime_logs)
        mapping["_runtime_stage"] = "align_target_schema"
        target_columns, added_columns = _align_target_schema_with_source(
            target_db,
            target_database,
            target_table,
            source_columns,
            target_columns,
            runtime_logs,
            before_execute=lambda sql: _mark_write_started(mapping, "align_target_schema", sql),
        )
        _emit_runtime_logs(log_sink, runtime_logs[log_start:])
        mapping["source_columns"] = source_columns
        mapping["target_columns"] = target_columns
        mapping["column_mappings"] = _source_policy_column_mappings(mapping, source_columns, target_columns)

    mapping["_runtime_stage"] = "build_mapping"
    select_items, target_columns = _mapping_select_items(mapping)
    if not target_columns:
        raise ValueError(f"{source_table} 没有可写入的字段映射。")

    if write_mode == "truncate_insert":
        truncate_sql = f"TRUNCATE TABLE {_q(target_database)}.{_q(target_table)}"
        add_log("INFO", "truncate", f"执行 TRUNCATE TABLE {target_database}.{target_table}。", {"sql": truncate_sql})
        _mark_write_started(mapping, "truncate", truncate_sql)
        with target_db.cursor() as cur:
            cur.execute(truncate_sql)

    if sync_method == "insert_select":
        log_start = len(runtime_logs)
        mapping["_runtime_stage"] = "insert_select"
        insert_result = _execute_insert_select_table(
            target_db,
            source_catalog,
            source_schema,
            source_table,
            target_database,
            target_table,
            target_columns,
            select_items,
            runtime_logs,
            before_execute=lambda sql: _mark_write_started(mapping, "insert_select", sql),
        )
        _emit_runtime_logs(log_sink, runtime_logs[log_start:])
        loaded_rows = int(insert_result.get("loaded_rows") or 0)
        add_log("INFO", "finish_table", f"{source_table} Catalog 联邦查询完成，写入 {loaded_rows} 行。")
        return {
            "table_id": str(mapping.get("id") or ""),
            "source_catalog": source_catalog,
            "source_schema": source_schema,
            "source_table": source_table,
            "target_database": target_database,
            "target_table": target_table,
            "target_exists": target_exists,
            "write_mode": write_mode,
            "sync_method": sync_method,
            "schema_policy": schema_policy,
            "schema_added_columns": added_columns,
            "batch_count": 1,
            "loaded_rows": loaded_rows,
            "stream_load": [],
            "insert_select": insert_result,
            "logs": runtime_logs,
            "message": f"{source_schema}.{source_table} -> {target_database}.{target_table} 通过 Catalog 联邦查询写入 {loaded_rows} 行。",
        }

    sql = (
        "SELECT "
        + ", ".join(select_items)
        + f" FROM {_q(source_schema)}.{_q(source_table)}"
    )
    if source_profile.engine == "oracle":
        sql = re.sub(r"`([^`]*)`", lambda match: '"' + match.group(1).replace('"', '""') + '"', sql)
    _mark_runtime_sql(mapping, "query_source", sql)
    add_log("INFO", "query_source", f"读取源表 SQL：{sql}", {"batch_size": batch_size, "target_columns": target_columns})
    loaded_rows = 0
    batch_index = 0
    stream_results: list[dict[str, Any]] = []
    with source_db.cursor() as cur:
        if source_profile.engine == "doris":
            _switch_catalog(cur, source_catalog)
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            batch_index += 1
            payload = _rows_to_csv(rows, target_columns, config)
            add_log("INFO", "stream_load", f"第 {batch_index} 批准备 Stream Load，行数 {len(rows)}，字节数 {len(payload)}。")
            _mark_write_started(
                mapping,
                "stream_load",
                f"STREAM LOAD {target_database}.{target_table} BATCH {batch_index}",
            )
            stream_result = _stream_load(target_profile, target_database, target_table, target_columns, payload, config, batch_index)
            stream_results.append(stream_result)
            loaded_rows += int(stream_result.get("number_loaded_rows") or len(rows))
            add_log("INFO", "stream_load_result", f"第 {batch_index} 批 Stream Load 返回 {stream_result.get('status')}，写入 {stream_result.get('number_loaded_rows')} 行。", stream_result)

    add_log("INFO", "finish_table", f"{source_table} 同步完成，写入 {loaded_rows} 行，批次 {batch_index}。")
    return {
        "table_id": str(mapping.get("id") or ""),
        "source_catalog": source_catalog,
        "source_schema": source_schema,
        "source_table": source_table,
        "target_database": target_database,
        "target_table": target_table,
        "target_exists": target_exists,
        "write_mode": write_mode,
        "sync_method": sync_method,
        "schema_policy": schema_policy,
        "schema_added_columns": added_columns,
        "batch_count": batch_index,
        "loaded_rows": loaded_rows,
        "stream_load": stream_results[-3:],
        "logs": runtime_logs,
        "message": f"{source_schema}.{source_table} -> {target_database}.{target_table} 写入 {loaded_rows} 行。",
    }


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    clean = dict(config or {})
    if clean.get("table_mappings") or clean.get("source_catalog") or clean.get("source_schema"):
        if not clean.get("source_connection_id"):
            raise ValueError("数据同步任务必须选择 Doris 连接。")
        clean["target_connection_id"] = clean.get("target_connection_id") or clean.get("source_connection_id")
        clean["source_catalog"] = _clean_identifier(clean.get("source_catalog") or "internal", "源 Catalog")
        clean["source_schema"] = _clean_identifier(clean.get("source_schema") or clean.get("source_database"), "源 Schema")
        clean["target_database"] = _clean_identifier(clean.get("target_database"), "目标库")
        clean["write_mode"] = _clean_write_mode(clean.get("write_mode"))
        clean["sync_method"] = _clean_sync_method(clean.get("sync_method"))
        clean["schema_policy"] = _clean_schema_policy(clean.get("schema_policy"))
        clean["batch_size"] = _clean_batch_size(clean.get("batch_size"))
        clean["field_delimiter"] = _decode_delimiter(clean.get("field_delimiter") or ",")
        clean["line_delimiter"] = _decode_delimiter(clean.get("line_delimiter") or "\\n")
        clean["strict_mode"] = bool(clean.get("strict_mode", False))
        clean["max_filter_ratio"] = str(clean.get("max_filter_ratio") if clean.get("max_filter_ratio") is not None else "1")
        clean["stream_load_http_port"] = int(clean.get("stream_load_http_port") or 8030)
        clean["continue_on_error"] = bool(clean.get("continue_on_error", True))
        clean["table_parallelism"] = _clean_table_parallelism(clean.get("table_parallelism"))
        clean["table_mappings"] = [_normalize_table_mapping(item, clean) for item in clean.get("table_mappings") or []]
        return clean
    clean["mode"] = "legacy"
    return clean


def _legacy_to_batch_config(config: dict[str, Any]) -> dict[str, Any]:
    for field, label in {
        "source_connection_id": "源连接",
        "source_database": "源库",
        "source_table": "源表",
        "target_connection_id": "目标连接",
        "target_database": "目标库",
        "target_table": "目标表",
    }.items():
        if not str(config.get(field) or "").strip():
            raise ValueError(f"数据同步任务缺少：{label}。")
    write_mode = str(config.get("write_mode") or "truncate_insert")
    if write_mode == "full_replace":
        write_mode = "truncate_insert"
    return _normalize_config(
        {
            "source_connection_id": config["source_connection_id"],
            "target_connection_id": config["target_connection_id"],
            "source_catalog": config.get("source_catalog") or "internal",
            "source_schema": config["source_database"],
            "target_database": config["target_database"],
            "write_mode": write_mode,
            "sync_method": config.get("sync_method") or "auto",
            "schema_policy": config.get("schema_policy") or "target",
            "batch_size": config.get("batch_size") or _DEFAULT_BATCH_SIZE,
            "field_delimiter": config.get("field_delimiter") or ",",
            "line_delimiter": config.get("line_delimiter") or "\\n",
            "strict_mode": config.get("strict_mode", False),
            "max_filter_ratio": config.get("max_filter_ratio", "1"),
            "stream_load_http_port": config.get("stream_load_http_port") or 8030,
            "table_parallelism": config.get("table_parallelism") or 1,
            "table_mappings": [
                {
                    "id": str(uuid.uuid4()),
                    "enabled": True,
                    "source_schema": config["source_database"],
                    "source_table": config["source_table"],
                    "target_database": config["target_database"],
                    "target_table": config["target_table"],
                    "auto_create": True,
                    "column_mappings": config.get("column_mapping") or [],
                }
            ],
        }
    )


def _normalize_table_mapping(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    mapping = dict(item or {})
    mapping["id"] = str(mapping.get("id") or uuid.uuid4())
    mapping["enabled"] = bool(mapping.get("enabled", True))
    try:
        mapping["source_schema"] = _clean_identifier(mapping.get("source_schema") or config.get("source_schema"), "源 Schema")
        mapping["source_table"] = _clean_existing_identifier(mapping.get("source_table"), "源表")
        mapping["target_database"] = _clean_identifier(mapping.get("target_database") or config.get("target_database"), "目标库")
        mapping["target_table"] = _clean_existing_identifier(mapping.get("target_table") or mapping.get("source_table"), "目标表")
    except ValueError as exc:
        raise ValueError(_mapping_error_message(mapping, exc)) from exc
    mapping["schema_policy"] = _clean_schema_policy(mapping.get("schema_policy") or config.get("schema_policy"))
    mapping["auto_create"] = bool(mapping.get("auto_create", True))
    mapping["source_columns"] = list(mapping.get("source_columns") or [])
    mapping["target_columns"] = list(mapping.get("target_columns") or [])
    mapping["column_mappings"] = list(mapping.get("column_mappings") or mapping.get("column_mapping") or [])
    return mapping


def _build_table_mapping(
    *,
    source_schema: str,
    source_table: str,
    target_database: str,
    target_table: str,
    target_exists: bool,
    source_columns: list[dict[str, Any]],
    target_columns: list[dict[str, Any]],
    schema_policy: str = "target",
) -> dict[str, Any]:
    schema_policy = _clean_schema_policy(schema_policy)
    source_by_name = {item["name"].lower(): item for item in source_columns}
    target_by_name = {item["name"].lower(): item for item in target_columns}
    if schema_policy == "source":
        for source in source_columns:
            name = str(source.get("name") or "")
            if name and name.lower() not in target_by_name:
                auto_column = _auto_target_column(source)
                target_columns.append(auto_column)
                target_by_name[auto_column["name"].lower()] = auto_column
    column_mappings = []
    for target in target_columns:
        source = source_by_name.get(str(target["name"]).lower())
        column_mappings.append(
            {
                "target_name": target["name"],
                "target_type": target.get("type") or (_source_column_to_doris_type(source) if source else "STRING"),
                "source_name": source["name"] if source else "",
                "fixed_value": "",
                "enabled": True,
            }
        )
    unmapped_source_columns = [
        item for item in source_columns if str(item["name"]).lower() not in target_by_name
    ]
    return {
        "id": str(uuid.uuid4()),
        "enabled": target_exists,
        "source_schema": source_schema,
        "source_table": source_table,
        "target_database": target_database,
        "target_table": target_table,
        "target_exists": target_exists,
        "schema_policy": schema_policy,
        "auto_create": not target_exists,
        "source_columns": source_columns,
        "target_columns": target_columns,
        "column_mappings": column_mappings,
        "unmapped_source_columns": unmapped_source_columns,
    }


def _mapping_select_items(mapping: dict[str, Any]) -> tuple[list[str], list[str]]:
    source_names = {str(item.get("name")) for item in mapping.get("source_columns") or []}
    select_items: list[str] = []
    target_columns: list[str] = []
    seen_targets: set[str] = set()
    meaningful_items = 0
    mappings = list(mapping.get("column_mappings") or [])
    if not mappings:
        for column in mapping.get("source_columns") or []:
            mappings.append({"target_name": column["name"], "source_name": column["name"], "enabled": True})
    for item in mappings:
        if not item.get("enabled", True):
            continue
        target_name = _clean_existing_identifier(item.get("target_name"), "目标字段")
        if target_name.lower() in seen_targets:
            raise ValueError(f"目标字段 {target_name} 被重复映射。")
        seen_targets.add(target_name.lower())
        source_name = str(item.get("source_name") or "").strip()
        fixed_value = item.get("fixed_value")
        if source_name:
            _clean_existing_identifier(source_name, "源字段")
            if source_names and source_name not in source_names:
                raise ValueError(f"源字段 {source_name} 不存在。")
            select_items.append(f"{_q(source_name)} AS {_q(target_name)}")
            meaningful_items += 1
        elif fixed_value not in (None, ""):
            select_items.append(f"{_sql_literal(fixed_value)} AS {_q(target_name)}")
            meaningful_items += 1
        else:
            select_items.append(f"NULL AS {_q(target_name)}")
        target_columns.append(target_name)
    if target_columns and meaningful_items == 0:
        table_name = str(mapping.get("source_table") or mapping.get("target_table") or "").strip()
        raise ValueError(f"{table_name or '当前表'} 没有有效字段映射：启用字段至少需要选择一个源字段或填写固定值。")
    return select_items, target_columns


def list_data_sync_source_catalogs(profile: DatabaseConnectionProfile) -> dict[str, Any]:
    _ensure_supported_source_profile(profile)
    if profile.engine == "mysql":
        return {
            "items": [{"name": "local_mysql", "type": "mysql", "extra": {"local_push": True}}],
            "message": "MySQL 源连接使用本地推送模式，Catalog 固定为 local_mysql。",
        }
    if profile.engine == "oracle":
        return {
            "items": [{"name": "local_oracle", "type": "oracle", "extra": {"local_push": True}}],
            "message": "Oracle 源连接使用本地推送模式，Catalog 固定为 local_oracle。",
        }
    with _doris_conn(profile, None) as db:
        catalogs = []
        with db.cursor() as cur:
            cur.execute("SHOW CATALOGS")
            rows = cur.fetchall()
        for row in rows:
            name = _first_value(row, ("Catalog", "CatalogName", "Name"))
            if name:
                catalogs.append({"name": str(name), "type": "catalog", "extra": _jsonable_row(row)})
    return {"items": catalogs, "message": f"已读取 {len(catalogs)} 个 Catalog。"}


def refresh_data_sync_source_catalog(profile: DatabaseConnectionProfile, catalog: str) -> None:
    """Refresh a Doris external catalog after an upstream restore creates a new schema."""
    _ensure_supported_source_profile(profile)
    clean_catalog = _clean_source_catalog(profile, catalog)
    if profile.engine != "doris" or clean_catalog == "internal":
        return
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            cur.execute(f"REFRESH CATALOG {_q(clean_catalog)}")


def list_data_sync_source_databases(profile: DatabaseConnectionProfile, *, catalog: str | None = None) -> dict[str, Any]:
    _ensure_supported_source_profile(profile)
    clean_catalog = _clean_source_catalog(profile, catalog or "internal")
    with _source_conn(profile, None) as db:
        items = [
            {"name": item["name"], "type": item.get("type") or "database", "extra": item.get("extra") or {}}
            for item in _list_source_databases(db, profile, clean_catalog)
        ]
    return {"items": items, "message": f"已读取 {len(items)} 个数据库。"}


def _list_source_tables(
    db,
    profile: DatabaseConnectionProfile,
    catalog: str,
    database: str,
) -> list[dict[str, Any]]:
    if profile.engine == "mysql":
        return _list_mysql_tables(db, database)
    if profile.engine == "oracle":
        return _list_oracle_tables(db, database)
    return _list_tables(db, catalog, database)


def _list_source_columns(
    db,
    profile: DatabaseConnectionProfile,
    catalog: str,
    database: str,
    table: str,
) -> list[dict[str, Any]]:
    if profile.engine == "mysql":
        return _list_mysql_columns(db, database, table)
    if profile.engine == "oracle":
        return _list_oracle_columns(db, database, table)
    return _list_columns(db, catalog, database, table)


def _list_source_databases(db, profile: DatabaseConnectionProfile, catalog: str) -> list[dict[str, Any]]:
    if profile.engine == "mysql":
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT SCHEMA_NAME
                FROM information_schema.SCHEMATA
                WHERE SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys')
                ORDER BY SCHEMA_NAME
                """
            )
            rows = cur.fetchall()
        return [{"name": str(row.get("SCHEMA_NAME")), "type": "database", "extra": _jsonable_row(row)} for row in rows]
    if profile.engine == "oracle":
        return [{"name": str(profile.username).upper(), "type": "schema", "extra": {"local_push": True}}]
    return _list_doris_databases(db, catalog)


def _list_mysql_tables(db, database: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE IN ('BASE TABLE','VIEW')
            ORDER BY TABLE_NAME
            """,
            (database,),
        )
        rows = cur.fetchall()
    return [
        {
            "name": str(row.get("TABLE_NAME")),
            "type": str(row.get("TABLE_TYPE") or "table").lower(),
            "extra": _jsonable_row(row),
        }
        for row in rows
        if row.get("TABLE_NAME")
    ]


def _list_mysql_columns(db, database: str, table: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, ORDINAL_POSITION, IS_NULLABLE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
                   COLUMN_DEFAULT, COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (database, table),
        )
        rows = cur.fetchall()
    result = []
    for row in rows:
        name = row.get("COLUMN_NAME")
        if not name:
            continue
        result.append(
            {
                "name": str(name),
                "type": str(row.get("COLUMN_TYPE") or row.get("DATA_TYPE") or ""),
                "ordinal": int(row.get("ORDINAL_POSITION") or len(result) + 1),
                "nullable": str(row.get("IS_NULLABLE") or "").upper() != "NO",
                "extra": _jsonable_row(row),
            }
        )
    return result


def _list_oracle_tables(db, database: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME, 'BASE TABLE' AS TABLE_TYPE FROM ALL_TABLES WHERE OWNER = :owner ORDER BY TABLE_NAME",
            {"owner": database.upper()},
        )
        rows = cur.fetchall()
    return [{"name": str(row.get("TABLE_NAME")), "type": str(row.get("TABLE_TYPE")), "extra": {}} for row in rows]


def _list_oracle_columns(db, database: str, table: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE, COLUMN_ID
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :owner AND TABLE_NAME = :table_name
            ORDER BY COLUMN_ID
            """,
            {"owner": database.upper(), "table_name": table.upper()},
        )
        rows = cur.fetchall()
    result = []
    for row in rows:
        data_type = str(row.get("DATA_TYPE") or "VARCHAR2").upper()
        if data_type == "NUMBER" and row.get("DATA_PRECISION") is not None:
            data_type = f"NUMBER({int(row['DATA_PRECISION'])},{int(row.get('DATA_SCALE') or 0)})"
        elif data_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"}:
            data_type = f"{data_type}({int(row.get('DATA_LENGTH') or 1)})"
        result.append({
            "name": str(row.get("COLUMN_NAME")),
            "type": data_type,
            "nullable": str(row.get("NULLABLE") or "Y").upper() == "Y",
            "ordinal": int(row.get("COLUMN_ID") or len(result) + 1),
        })
    return result


def _list_doris_databases(db, catalog: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        _switch_catalog(cur, catalog)
        cur.execute("SHOW DATABASES")
        rows = cur.fetchall()
    result = []
    for row in rows:
        name = _first_value(row, ("Database", "DatabaseName", "Name"))
        if name:
            result.append({"name": str(name), "type": "database", "extra": _jsonable_row(row)})
    return result


def _list_tables(db, catalog: str, database: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        _switch_catalog(cur, catalog)
        cur.execute(f"USE {_q(database)}")
        cur.execute("SHOW FULL TABLES")
        rows = cur.fetchall()
    result = []
    for row in rows:
        name = _first_value(row, ("Table", "Name", "Tables_in_" + database))
        table_type = _first_value(row, ("Table_type", "Table Type", "Type")) or "table"
        if name:
            result.append({"name": str(name), "type": str(table_type), "extra": _jsonable_row(row)})
    return result


def _list_columns(db, catalog: str, database: str, table: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        _switch_catalog(cur, catalog)
        cur.execute(f"USE {_q(database)}")
        cur.execute(f"DESC {_q(table)}")
        rows = cur.fetchall()
    result = []
    for index, row in enumerate(rows):
        name = _first_value(row, ("Field", "Column", "Name"))
        if not name:
            continue
        result.append(
            {
                "name": str(name),
                "type": str(_first_value(row, ("Type", "DataType")) or ""),
                "ordinal": index + 1,
                "nullable": str(_first_value(row, ("Null", "Nullable")) or "").upper() != "NO",
                "extra": _jsonable_row(row),
            }
        )
    return result


def _table_exists(db, catalog: str, database: str, table: str) -> bool:
    names = {item["name"].lower() for item in _list_tables(db, catalog, database)}
    return table.lower() in names


def _create_target_table(
    db,
    database: str,
    table: str,
    source_columns: list[dict[str, Any]],
    before_execute: Callable[[str], None] | None = None,
) -> str:
    if not source_columns:
        raise ValueError("源表字段为空，无法自动创建目标表。")
    columns = []
    for column in source_columns:
        column_name = _clean_existing_identifier(column.get("name"), "字段名")
        columns.append(f"{_q(column_name)} {_source_column_to_doris_type(column)} NULL")
    first_col = _clean_existing_identifier(source_columns[0].get("name"), "首字段")
    sql = (
        f"CREATE TABLE IF NOT EXISTS {_q(database)}.{_q(table)} ("
        + ", ".join(columns)
        + f") DUPLICATE KEY({_q(first_col)}) DISTRIBUTED BY HASH({_q(first_col)}) BUCKETS 10 "
        + 'PROPERTIES ("replication_num" = "1")'
    )
    if before_execute:
        before_execute(sql)
    with db.cursor() as cur:
        _switch_catalog(cur, "internal")
        cur.execute(sql)
    return sql


def _align_target_schema_with_source(
    target_db,
    database: str,
    table: str,
    source_columns: list[dict[str, Any]],
    target_columns: list[dict[str, Any]],
    runtime_logs: list[dict[str, Any]],
    before_execute: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    target_by_name = {str(item.get("name") or "").lower(): item for item in target_columns}
    added: list[str] = []
    for source_column in source_columns:
        source_name = str(source_column.get("name") or "").strip()
        if not source_name or source_name.lower() in target_by_name:
            continue
        target_name = _clean_existing_identifier(source_name, "目标字段")
        doris_type = _source_column_to_doris_type(source_column)
        sql = f"ALTER TABLE {_q(database)}.{_q(table)} ADD COLUMN {_q(target_name)} {doris_type} NULL"
        runtime_logs.append(
            _log(
                "INFO",
                "schema_align",
                f"目标表缺少源字段 {source_name}，按源表结构补充 Doris 字段 {target_name}。",
                {"sql": sql, "source_type": source_column.get("type"), "target_type": doris_type},
            )
        )
        if before_execute:
            before_execute(sql)
        with target_db.cursor() as cur:
            _switch_catalog(cur, "internal")
            cur.execute(sql)
        new_column = {
            "name": target_name,
            "type": doris_type,
            "ordinal": len(target_columns) + 1,
            "nullable": True,
            "source_type": source_column.get("type") or "",
        }
        target_columns.append(new_column)
        target_by_name[target_name.lower()] = new_column
        added.append(target_name)
    return target_columns, added


def _source_policy_column_mappings(
    mapping: dict[str, Any],
    source_columns: list[dict[str, Any]],
    target_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_by_target = {
        str(item.get("target_name") or "").lower(): item
        for item in mapping.get("column_mappings") or []
        if str(item.get("target_name") or "").strip()
    }
    target_by_name = {str(item.get("name") or "").lower(): item for item in target_columns}
    result: list[dict[str, Any]] = []
    for source in source_columns:
        source_name = str(source.get("name") or "").strip()
        if not source_name:
            continue
        target = target_by_name.get(source_name.lower()) or _auto_target_column(source)
        existing = existing_by_target.get(source_name.lower()) or {}
        result.append(
            {
                "target_name": target["name"],
                "target_type": target.get("type") or _source_column_to_doris_type(source),
                "source_name": existing.get("source_name") or source_name,
                "fixed_value": existing.get("fixed_value", ""),
                "enabled": existing.get("enabled", True),
            }
        )
    return result


def _execute_insert_select_table(
    target_db,
    source_catalog: str,
    source_schema: str,
    source_table: str,
    target_database: str,
    target_table: str,
    target_columns: list[str],
    select_items: list[str],
    runtime_logs: list[dict[str, Any]],
    before_execute: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    target_ref = f"{_q(target_database)}.{_q(target_table)}"
    source_ref = f"{_q(source_catalog)}.{_q(source_schema)}.{_q(source_table)}"
    sql = (
        f"INSERT INTO {target_ref} ("
        + ", ".join(_q(column) for column in target_columns)
        + ") SELECT "
        + ", ".join(select_items)
        + f" FROM {source_ref}"
    )
    runtime_logs.append(
        _log(
            "INFO",
            "insert_select",
            f"执行 Catalog 联邦查询 INSERT SELECT：{source_catalog}.{source_schema}.{source_table}。",
            {"sql": sql, "target_columns": target_columns},
        )
    )
    if before_execute:
        before_execute(sql)
    with target_db.cursor() as cur:
        _switch_catalog(cur, "internal")
        affected_rows = cur.execute(sql)
        if affected_rows is None or int(affected_rows) < 0:
            affected_rows = getattr(cur, "rowcount", 0)
    loaded_rows = max(0, int(affected_rows or 0))
    return {
        "status": "submitted",
        "loaded_rows": loaded_rows,
        "sql": sql,
    }


def _rows_to_csv(rows: list[dict[str, Any]], columns: list[str], config: dict[str, Any]) -> bytes:
    delimiter = str(config.get("field_delimiter") or ",")
    line_delimiter = str(config.get("line_delimiter") or "\n")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator=line_delimiter, quoting=csv.QUOTE_MINIMAL)
    for row in rows:
        writer.writerow([_csv_value(row.get(column)) for column in columns])
    return buffer.getvalue().encode("utf-8")


def _stream_load(
    profile: DatabaseConnectionProfile,
    database: str,
    table: str,
    columns: list[str],
    payload: bytes,
    config: dict[str, Any],
    batch_index: int,
) -> dict[str, Any]:
    password = decrypt_secret(profile.password_enc, get_settings().credential_encryption_key)
    http_port = int(config.get("stream_load_http_port") or 8030)
    url = f"http://{profile.host}:{http_port}/api/{quote(database, safe='')}/{quote(table, safe='')}/_stream_load"
    token = base64.b64encode(f"{profile.username}:{password}".encode("utf-8")).decode("ascii")
    base_headers = {
        "Authorization": f"Basic {token}",
        "Expect": "100-continue",
        "format": "csv",
        "column_separator": _stream_load_header_delimiter(config.get("field_delimiter") or ","),
        "line_delimiter": _stream_load_header_delimiter(config.get("line_delimiter") or "\n"),
        "columns": ",".join(_q(column) for column in columns),
        "strict_mode": "true" if config.get("strict_mode") else "false",
        "max_filter_ratio": str(config.get("max_filter_ratio") or "1"),
    }
    timeout = int(config.get("stream_load_timeout_seconds") or 300)
    retry_attempts = _stream_load_retry_attempts(config)
    retry_sleep_seconds = _stream_load_retry_sleep_seconds(config)
    last_retryable_body = ""
    for load_attempt in range(1, retry_attempts + 1):
        body = ""
        current_url = url
        headers = {
            **base_headers,
            "label": f"data_sync_{uuid.uuid4().hex}_{batch_index}_{load_attempt}",
        }
        for redirect_attempt in range(3):
            try:
                status_code, response_headers, response_body = _stream_load_http_put(current_url, payload, headers, timeout)
                body = response_body.decode("utf-8", errors="replace")
                if status_code in {307, 308} and response_headers.get("Location") and redirect_attempt < 2:
                    current_url = _rewrite_stream_load_redirect(response_headers["Location"], profile.host)
                    continue
                if status_code >= 400:
                    if _is_retryable_stream_load_error(body) and load_attempt < retry_attempts:
                        last_retryable_body = body
                        break
                    if _is_retryable_stream_load_error(body) and retry_attempts > 1:
                        raise ValueError(
                            f"Stream Load 失败：Doris FE 主节点心跳未就绪，已尝试 {retry_attempts} 次仍失败。"
                            f"请检查 FE master/BE 状态或稍后重试。原始返回：{body}"
                        )
                    raise ValueError(f"Stream Load 失败：HTTP {status_code} {body}")
                break
            except HTTPError as exc:
                if exc.code in {307, 308} and exc.headers.get("Location") and redirect_attempt < 2:
                    current_url = _rewrite_stream_load_redirect(exc.headers["Location"], profile.host)
                    continue
                body = exc.read().decode("utf-8", errors="replace")
                if _is_retryable_stream_load_error(body) and load_attempt < retry_attempts:
                    last_retryable_body = body
                    break
                if _is_retryable_stream_load_error(body) and retry_attempts > 1:
                    raise ValueError(
                        f"Stream Load 失败：Doris FE 主节点心跳未就绪，已尝试 {retry_attempts} 次仍失败。"
                        f"请检查 FE master/BE 状态或稍后重试。原始返回：{body}"
                    ) from exc
                raise ValueError(f"Stream Load 失败：HTTP {exc.code} {body}") from exc
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"Status": "Unknown", "Message": body}
        status = str(parsed.get("Status") or parsed.get("status") or "")
        if status.lower() in {"success", "publish timeout"}:
            return {
                "status": status,
                "message": parsed.get("Message") or parsed.get("msg") or "",
                "number_total_rows": int(parsed.get("NumberTotalRows") or parsed.get("numberTotalRows") or 0),
                "number_loaded_rows": int(parsed.get("NumberLoadedRows") or parsed.get("numberLoadedRows") or 0),
                "load_bytes": int(parsed.get("LoadBytes") or parsed.get("loadBytes") or len(payload)),
                "attempts": load_attempt,
            }
        if _is_retryable_stream_load_error(body) and load_attempt < retry_attempts:
            last_retryable_body = body
            time.sleep(retry_sleep_seconds * load_attempt)
            continue
        if _is_retryable_stream_load_error(body) and retry_attempts > 1:
            raise ValueError(
                f"Stream Load 失败：Doris FE 主节点心跳未就绪，已尝试 {retry_attempts} 次仍失败。"
                f"请检查 FE master/BE 状态或稍后重试。原始返回：{body}"
            )
        raise ValueError(f"Stream Load 失败：{body}")
    return {
        "status": "failed",
        "message": last_retryable_body,
        "number_total_rows": 0,
        "number_loaded_rows": 0,
        "load_bytes": len(payload),
        "attempts": retry_attempts,
    }


def _stream_load_http_put(url: str, payload: bytes, headers: dict[str, str], timeout: int) -> tuple[int, dict[str, str], bytes]:
    if not _headers_fit_latin1(headers):
        return _stream_load_raw_http_put(url, payload, headers, timeout)
    req = urlrequest.Request(url, data=payload, headers=headers, method="PUT")
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        status_code = int(resp.getcode()) if hasattr(resp, "getcode") else 200
        response_headers = dict(resp.headers.items()) if hasattr(resp, "headers") else {}
        return status_code, response_headers, resp.read()


def _headers_fit_latin1(headers: dict[str, str]) -> bool:
    for key, value in headers.items():
        try:
            str(key).encode("latin-1")
            str(value).encode("latin-1")
        except UnicodeEncodeError:
            return False
    return True


def _stream_load_raw_http_put(url: str, payload: bytes, headers: dict[str, str], timeout: int) -> tuple[int, dict[str, str], bytes]:
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise ValueError("Stream Load UTF-8 header transport only supports http.")
    host = parsed.hostname
    if not host:
        raise ValueError("Stream Load URL 缺少主机。")
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    request_headers = {str(key): str(value) for key, value in headers.items()}
    request_headers["Host"] = parsed.netloc
    request_headers["Content-Length"] = str(len(payload))
    request_headers["Connection"] = "close"
    for key, value in request_headers.items():
        if _CONTROL_CHAR_RE.search(key) or "\r" in value or "\n" in value:
            raise ValueError(f"Stream Load header {key} contains invalid control characters.")

    lines = [f"PUT {path} HTTP/1.1"]
    lines.extend(f"{key}: {value}" for key, value in request_headers.items())
    request_head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request_head + payload)
        reader = sock.makefile("rb")
        status_line = reader.readline().decode("iso-8859-1", errors="replace").strip()
        while status_line.startswith("HTTP/") and " 100 " in status_line:
            _read_http_headers(reader)
            status_line = reader.readline().decode("iso-8859-1", errors="replace").strip()
        if not status_line.startswith("HTTP/"):
            raise ValueError(f"Stream Load 返回了无效 HTTP 状态行：{status_line}")
        parts = status_line.split(" ", 2)
        status_code = int(parts[1])
        response_headers = _read_http_headers(reader)
        content_length = response_headers.get("Content-Length") or response_headers.get("content-length")
        if content_length is not None:
            body = reader.read(int(content_length))
        else:
            body = reader.read()
    return status_code, response_headers, body


def _read_http_headers(reader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = reader.readline()
        if line in {b"\r\n", b"\n", b""}:
            break
        text = line.decode("iso-8859-1", errors="replace").rstrip("\r\n")
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def _stream_load_header_delimiter(value: Any) -> str:
    text = str(value or "")
    return (
        text.replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _rewrite_stream_load_redirect(location: str, host: str) -> str:
    parsed = urlparse(location)
    if not parsed.scheme or not parsed.netloc:
        return location
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse((parsed.scheme, f"{host}{port}", parsed.path, parsed.params, parsed.query, parsed.fragment))


def _doris_conn(profile: DatabaseConnectionProfile, database: str | None):
    return pymysql.connect(
        host=profile.host,
        port=profile.port or 9030,
        user=profile.username,
        password=decrypt_secret(profile.password_enc, get_settings().credential_encryption_key),
        database=database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _mysql_conn(profile: DatabaseConnectionProfile, database: str | None):
    return pymysql.connect(
        host=profile.host,
        port=profile.port or 3306,
        user=profile.username,
        password=decrypt_secret(profile.password_enc, get_settings().credential_encryption_key),
        database=database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _source_conn(profile: DatabaseConnectionProfile, database: str | None):
    if profile.engine == "mysql":
        return _mysql_conn(profile, database)
    if profile.engine == "oracle":
        return _oracle_conn(profile)
    return _doris_conn(profile, database)


class _OracleCursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._cursor.close()
        return False

    def execute(self, sql, params=None):
        result = self._cursor.execute(sql, params or {})
        if self._cursor.description:
            names = [str(item[0]) for item in self._cursor.description]
            self._cursor.rowfactory = lambda *values: dict(zip(names, values))
        return result

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size):
        return self._cursor.fetchmany(size)


class _OracleConnectionAdapter:
    def __init__(self, profile: DatabaseConnectionProfile):
        service = str(getattr(profile, "service_name", None) or getattr(profile, "database", None) or "").strip()
        dsn = str(getattr(profile, "dsn", None) or "").strip() or oracledb.makedsn(
            profile.host,
            int(profile.port or 1521),
            service_name=service,
        )
        self._connection = oracledb.connect(
            user=profile.username,
            password=decrypt_secret(profile.password_enc, get_settings().credential_encryption_key),
            dsn=dsn,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._connection.close()
        return False

    def cursor(self):
        return _OracleCursorAdapter(self._connection.cursor())


def _oracle_conn(profile: DatabaseConnectionProfile):
    return _OracleConnectionAdapter(profile)


def _probe_data_sync_connections(
    source_profile: DatabaseConnectionProfile,
    target_profile: DatabaseConnectionProfile,
    config: dict[str, Any],
) -> None:
    source_catalog = _clean_source_catalog(source_profile, config.get("source_catalog"))
    source_schema = _clean_identifier(config.get("source_schema"), "源 Schema")
    target_database = _clean_identifier(config.get("target_database"), "目标库")
    with _source_conn(source_profile, None) as source_db:
        with source_db.cursor() as cur:
            if source_profile.engine == "doris":
                _switch_catalog(cur, source_catalog)
                cur.execute(f"USE {_q(source_schema)}")
                cur.execute("SHOW TABLES")
            elif source_profile.engine == "mysql":
                cur.execute(
                    """
                    SELECT TABLE_NAME
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                    LIMIT 1
                    """,
                    (source_schema,),
                )
            else:
                cur.execute("SELECT 1 AS VALUE FROM DUAL")
            cur.fetchone()
    with _doris_conn(target_profile, None) as target_db:
        with target_db.cursor() as cur:
            _switch_catalog(cur, "internal")
            cur.execute(f"USE {_q(target_database)}")
            cur.execute("SELECT 1")


def _ensure_supported_source_profile(profile: DatabaseConnectionProfile) -> None:
    if profile.engine not in _SOURCE_ENGINES:
        raise ValueError("数据同步源连接目前只支持 Doris、MySQL 或 Oracle。")


def _ensure_doris_profile(profile: DatabaseConnectionProfile) -> None:
    if profile.engine != "doris":
        raise ValueError("请选择 Doris 类型的数据连接。")


def _clean_source_catalog(profile: DatabaseConnectionProfile, value: Any) -> str:
    if profile.engine == "mysql":
        clean = str(value or "local_mysql").strip() or "local_mysql"
        if clean != "local_mysql":
            raise ValueError("MySQL 源连接的 Catalog 固定为 local_mysql。")
        return clean
    if profile.engine == "oracle":
        clean = str(value or "local_oracle").strip() or "local_oracle"
        if clean != "local_oracle":
            raise ValueError("Oracle 直连源的 Catalog 固定为 local_oracle。")
        return clean
    return _clean_identifier(value, "源 Catalog")


def _switch_catalog(cur, catalog: str | None) -> None:
    clean = _clean_optional_identifier(catalog, "Catalog")
    if clean:
        try:
            cur.execute(f"SWITCH {_q(clean)}")
        except Exception as exc:
            if not _is_switch_syntax_error(exc):
                raise
            cur.execute(f"SWITCH {clean}")


def _clean_identifier(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not clean or not _IDENT_RE.match(clean):
        raise ValueError(f"{label}只能包含中文、字母、数字和下划线。")
    return clean


def _clean_existing_identifier(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{label}不能为空。")
    if _CONTROL_CHAR_RE.search(clean):
        raise ValueError(f"{label}不能包含换行或控制字符。")
    return clean


def _clean_optional_identifier(value: Any, label: str) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    return _clean_identifier(clean, label)


def _mapping_error_message(mapping: dict[str, Any], exc: Exception) -> str:
    mapping_id = _log_value(mapping.get("id"))
    source_schema = _log_value(mapping.get("source_schema"))
    source_table = _log_value(mapping.get("source_table"))
    target_database = _log_value(mapping.get("target_database"))
    target_table = _log_value(mapping.get("target_table"))
    stage = _log_value(mapping.get("_runtime_stage"))
    write_started = bool(mapping.get("_runtime_write_started"))
    sql_generated = bool(_latest_runtime_sql(mapping))
    exception_text = _exception_summary(exc)
    exception_type = type(exc).__name__
    if _is_retryable_connection_error(exc):
        prefix = "表同步连接异常"
    elif isinstance(exc, ValueError):
        prefix = "表映射配置无效"
    else:
        prefix = "表同步执行失败"
    if write_started:
        sql_state = "写入请求已经或可能已经发出，结果未知，已禁止自动重试；请核对目标表和 Doris 查询状态"
    elif sql_generated:
        sql_state = "已生成 SQL，最后 SQL 见该表日志"
    else:
        sql_state = "尚未生成 SQL"
    return (
        f"{prefix}：映射ID={mapping_id}，"
        f"源表={source_schema}.{source_table}，目标表={target_database}.{target_table}，"
        f"阶段={stage}，异常类型={exception_type}，错误码={_exception_code(exc)}，"
        f"原因：{exception_text}。{sql_state}。"
    )


def _mark_runtime_sql(mapping: dict[str, Any], stage: str, sql: str) -> None:
    mapping["_runtime_stage"] = stage
    mapping["_runtime_last_sql"] = sql


def _mark_write_started(mapping: dict[str, Any], stage: str, sql: str) -> None:
    _mark_runtime_sql(mapping, stage, sql)
    mapping["_runtime_write_started"] = True


def _latest_runtime_sql(mapping: dict[str, Any]) -> str | None:
    current = str(mapping.get("_runtime_last_sql") or "").strip()
    if current:
        return current
    for log in reversed(list(mapping.get("_runtime_logs") or [])):
        payload = log.get("payload") if isinstance(log, dict) else None
        sql = str((payload or {}).get("sql") or "").strip() if isinstance(payload, dict) else ""
        if sql:
            return sql
    return None


def _exception_code(exc: Exception) -> int | str | None:
    seen: set[int] = set()
    current: Any = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        args = getattr(current, "args", ()) or ()
        if args and isinstance(args[0], (int, str)):
            return args[0]
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return None


def _exception_summary(exc: Exception) -> str:
    text = str(exc).strip()
    return _log_value(text or repr(exc))


def _is_retryable_connection_error(exc: Exception) -> bool:
    seen: set[int] = set()
    current: Any = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, DataSyncInfrastructureUnavailable):
            return True
        if isinstance(current, (pymysql.err.InterfaceError, pymysql.err.OperationalError)):
            code = _exception_code(current)
            if code in _RETRYABLE_CONNECTION_ERROR_CODES:
                return True
        if isinstance(current, (ConnectionError, TimeoutError, OSError, URLError)):
            return True
        message = str(current).lower()
        if any(
            marker in message
            for marker in (
                "lost connection",
                "server has gone away",
                "connection reset",
                "connection refused",
                "broken pipe",
                "socket is closed",
                "timed out",
            )
        ):
            return True
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


def _is_switch_syntax_error(exc: Exception) -> bool:
    code = _exception_code(exc)
    if code == 1064:
        return True
    message = str(exc).lower()
    return "syntax error" in message or "parse error" in message


def _log_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _clean_write_mode(value: Any) -> str:
    clean = str(value or "append").strip()
    if clean == "full_replace":
        clean = "truncate_insert"
    if clean not in {"append", "truncate_insert"}:
        raise ValueError("数据同步写入策略只支持 append 或 truncate_insert。")
    return clean


def _clean_sync_method(value: Any) -> str:
    clean = str(value or "auto").strip()
    if clean not in _SYNC_METHODS:
        raise ValueError("data sync method must be auto, insert_select, or stream_load.")
    return clean


def _clean_schema_policy(value: Any) -> str:
    clean = str(value or "target").strip()
    if clean not in _SCHEMA_POLICIES:
        raise ValueError("data sync schema policy must be source or target.")
    return clean


def _resolve_sync_method(value: Any, source_catalog: str, source_engine: str = "doris") -> str:
    clean = _clean_sync_method(value)
    if clean != "auto":
        return clean
    if source_engine != "doris":
        return "stream_load"
    return "insert_select"


def _clean_table_parallelism(value: Any) -> int:
    max_value = max(1, int(get_settings().data_sync_max_table_parallelism or 8))
    raw = value if value not in (None, "") else 1
    return max(1, min(max_value, int(raw)))


def _clean_connection_retry_attempts(value: Any) -> int:
    raw = value if value not in (None, "") else _DEFAULT_CONNECTION_RETRY_ATTEMPTS
    return max(1, min(5, int(raw)))


def _clean_connection_recovery_attempts(value: Any) -> int:
    raw = value if value not in (None, "") else _DEFAULT_CONNECTION_RECOVERY_ATTEMPTS
    return max(1, min(60, int(raw)))


def _clean_connection_recovery_interval_seconds(value: Any) -> float:
    raw = value if value not in (None, "") else _DEFAULT_CONNECTION_RECOVERY_INTERVAL_SECONDS
    return max(0.0, min(60.0, float(raw)))


def _clean_batch_size(value: Any) -> int:
    return max(100, min(50000, int(value or _DEFAULT_BATCH_SIZE)))


def _decode_delimiter(value: Any) -> str:
    text = str(value or "")
    return text.replace("\\t", "\t").replace("\\n", "\n").replace("\\r", "\r")


def _stream_load_retry_attempts(config: dict[str, Any]) -> int:
    raw = config.get("stream_load_retry_attempts")
    if raw in (None, ""):
        raw = _DEFAULT_STREAM_LOAD_RETRY_ATTEMPTS
    return max(1, min(5, int(raw)))


def _stream_load_retry_sleep_seconds(config: dict[str, Any]) -> float:
    raw = config.get("stream_load_retry_sleep_seconds")
    if raw in (None, ""):
        raw = _DEFAULT_STREAM_LOAD_RETRY_SLEEP_SECONDS
    return max(0.0, min(10.0, float(raw)))


def _is_retryable_stream_load_error(body: Any) -> bool:
    text = str(body or "").lower()
    return "[e-240]" in text or "have not get fe master heartbeat yet" in text


def _q(value: str) -> str:
    return f"`{str(value).replace('`', '``')}`"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _csv_value(value: Any) -> Any:
    if value is None:
        return r"\N"
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _source_column_to_doris_type(column: dict[str, Any]) -> str:
    raw_type = str(column.get("type") or "").strip()
    type_text = raw_type.upper()
    base = re.sub(r"\s+", " ", type_text).split("(", 1)[0].strip()
    params = _type_params(type_text)

    if base in {"CHAR", "NCHAR"}:
        length = params[0] if params else 1
        return f"CHAR({max(1, min(_DORIS_MAX_CHAR_LENGTH, length * 2))})"
    if base in {"VARCHAR", "VARCHAR2", "NVARCHAR", "NVARCHAR2", "CHARACTER VARYING"}:
        length = params[0] if params else _DORIS_MAX_VARCHAR_LENGTH
        return f"VARCHAR({max(1, min(_DORIS_MAX_VARCHAR_LENGTH, length * 2))})"
    if base in {"STRING", "TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT", "CLOB", "NCLOB", "LONG"}:
        return "STRING"
    if base in {"TINYINT", "BYTE"}:
        return "TINYINT"
    if base in {"SMALLINT", "INT2"}:
        return "SMALLINT"
    if base in {"INT", "INTEGER", "INT4", "MEDIUMINT"}:
        return "INT"
    if base in {"BIGINT", "INT8"}:
        return "BIGINT"
    if base in {"LARGEINT"}:
        return "LARGEINT"
    if base in {"NUMBER", "NUMERIC", "DECIMAL", "DEC"}:
        precision = params[0] if params else _DORIS_MAX_DECIMAL_PRECISION
        scale = params[1] if len(params) > 1 else 0
        if scale <= 0:
            if precision <= 9:
                return "INT"
            if precision <= 18:
                return "BIGINT"
            if precision <= 38:
                return "LARGEINT"
        precision = max(1, min(_DORIS_MAX_DECIMAL_PRECISION, precision))
        scale = max(0, min(scale, precision))
        return f"DECIMAL({precision},{scale})"
    if base in {"FLOAT", "BINARY_FLOAT"}:
        return "FLOAT"
    if base in {"DOUBLE", "DOUBLE PRECISION", "REAL", "BINARY_DOUBLE"}:
        return "DOUBLE"
    if base in {"BOOLEAN", "BOOL", "BIT"}:
        return "BOOLEAN"
    if base == "DATE":
        return "DATE"
    if base in {"DATETIME", "TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP WITH TIME ZONE"}:
        return "DATETIME"
    if base in {"TIME"}:
        return "STRING"
    if base in {"BLOB", "BINARY", "VARBINARY", "RAW", "LONG RAW", "BYTEA"}:
        return "STRING"
    return "STRING"


def _type_params(type_text: str) -> list[int]:
    match = re.search(r"\(([^)]*)\)", type_text or "")
    if not match:
        return []
    result: list[int] = []
    for item in match.group(1).split(","):
        item = item.strip()
        if not item or not re.match(r"^\d+$", item):
            continue
        result.append(int(item))
    return result


def _auto_target_column(column: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(column.get("name") or ""),
        "type": _source_column_to_doris_type(column),
        "ordinal": int(column.get("ordinal") or 0),
        "nullable": True,
    }


def _first_value(row: dict[str, Any], preferred: tuple[str, ...]) -> Any:
    if not row:
        return None
    lowered = {str(key).lower(): key for key in row.keys()}
    for name in preferred:
        key = lowered.get(name.lower())
        if key is not None:
            return row.get(key)
    for value in row.values():
        if value is not None:
            return value
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable(value) for key, value in row.items()}


def _emit_event(event_hook: DataSyncEventHook | None, event: str, payload: dict[str, Any]) -> None:
    if event_hook:
        event_hook(event, payload)


def _emit_runtime_logs(
    log_sink: Callable[[dict[str, Any]], None] | None,
    logs: list[dict[str, Any]],
) -> None:
    if not log_sink:
        return
    for log in logs:
        log_sink(log)


def _table_event_mapping(mapping: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    source_catalog = str(config.get("source_catalog") or "internal")
    sync_method = _resolve_sync_method(config.get("sync_method"), source_catalog, str(config.get("source_engine") or "doris"))
    return {
        "mapping_id": str(mapping.get("id") or mapping.get("source_table") or ""),
        "source_catalog": source_catalog,
        "source_schema": str(mapping.get("source_schema") or config.get("source_schema") or ""),
        "source_table": str(mapping.get("source_table") or ""),
        "target_database": str(mapping.get("target_database") or config.get("target_database") or ""),
        "target_table": str(mapping.get("target_table") or mapping.get("source_table") or ""),
        "sync_method": sync_method,
        "write_mode": str(config.get("write_mode") or ""),
        "schema_policy": str(mapping.get("schema_policy") or config.get("schema_policy") or ""),
    }


def _table_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary = dict(result or {})
    summary.pop("logs", None)
    return summary


def _skipped_table_result(mapping: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    event_mapping = _table_event_mapping(mapping, config)
    return {
        "table_id": event_mapping["mapping_id"],
        "source_catalog": event_mapping["source_catalog"],
        "source_schema": event_mapping["source_schema"],
        "source_table": event_mapping["source_table"],
        "target_database": event_mapping["target_database"],
        "target_table": event_mapping["target_table"],
        "sync_method": event_mapping["sync_method"],
        "write_mode": event_mapping["write_mode"],
        "schema_policy": event_mapping["schema_policy"],
        "status": "skipped",
        "message": "前序表失败，且任务配置为失败后停止，当前表未执行。",
        "loaded_rows": 0,
        "logs": [_log("INFO", "skipped", "前序表失败，当前表已跳过。")],
    }


def _infrastructure_skipped_table_result(
    mapping: dict[str, Any],
    config: dict[str, Any],
    reason: str | None,
) -> dict[str, Any]:
    event_mapping = _table_event_mapping(mapping, config)
    message = "Doris/Catalog 在连接恢复窗口内仍不可用，当前表未开始执行，已停止故障扩散。"
    if reason:
        message += f" 首个基础设施错误：{reason}"
    return {
        "table_id": event_mapping["mapping_id"],
        "source_catalog": event_mapping["source_catalog"],
        "source_schema": event_mapping["source_schema"],
        "source_table": event_mapping["source_table"],
        "target_database": event_mapping["target_database"],
        "target_table": event_mapping["target_table"],
        "sync_method": event_mapping["sync_method"],
        "write_mode": event_mapping["write_mode"],
        "schema_policy": event_mapping["schema_policy"],
        "status": "skipped",
        "message": message,
        "loaded_rows": 0,
        "failure_type": "infrastructure_unavailable",
        "logs": [
            _log(
                "WARNING",
                "infrastructure_skipped",
                message,
                {"write_started": False, "sql_generated": False},
            )
        ],
    }


def _log(level: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "time": app_now().isoformat(),
        "level": level,
        "stage": stage,
        "message": message,
        "payload": payload or {},
    }
