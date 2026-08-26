from __future__ import annotations

import asyncio
import csv
import ftplib
import io
import json
import posixpath
import re
import uuid
import shutil
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import quote

import httpx
import pymysql
from pymysql.cursors import DictCursor
from sqlalchemy import select
from sqlalchemy.orm import Session

from recovery_service.api.schemas.doris_csv_import import (
    DorisCsvBadRowPreview,
    DorisCsvColumnPreview,
    DorisCsvFileImportResult,
    DorisCsvFilePreview,
    DorisCsvImportResponse,
    DorisCsvParseTaskListResponse,
    DorisCsvParseFileStatus,
    DorisCsvParseTaskStatus,
    DorisCsvTaskLogItem,
    DorisCsvTaskLogListResponse,
    DorisCsvPreviewResponse,
    DorisFtpCatalogItem,
    DorisFtpCatalogResponse,
    DorisFtpConnection,
    normalize_doris_csv_column_type,
)
from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import DatabaseConnectionProfile
from recovery_service.core.models.task import DorisCsvParseFile, DorisCsvParseTask, DorisCsvTaskLog
from recovery_service.db.session import get_sync_session_factory
from recovery_service.settings import get_settings

_IDENT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_INT_RE = re.compile(r"^[+-]?\d+$")
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+\.\d+|\d+\.|\.\d+)$")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)
_NULL_VALUES = {"", "null", "none", "nil", "nan", "n/a", "\\n"}
_CSV_PARSE_TERMINAL_STATES = {"completed", "failed", "stopped", "imported", "import_failed"}
_AUTO_CHARSET_VALUES = {"auto", "automatic", "detect", "auto-detect", "auto_detect", ""}


def create_csv_parse_task(
    profile: DatabaseConnectionProfile,
    files: list[tuple[str, bytes]],
    *,
    database: str | None,
    delimiter: str = ",",
    charset: str = "utf-8-sig",
    has_header: bool = True,
    import_mode: str = "multiple_tables",
    source: str = "local",
) -> DorisCsvParseTaskStatus:
    if not files:
        raise ValueError("请至少选择一个 CSV 文件。")
    if import_mode not in {"multiple_tables", "single_table"}:
        raise ValueError("导入模式无效。")
    task_id = uuid.uuid4()
    task_dir = _csv_parse_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    session = get_sync_session_factory()()
    try:
        task = DorisCsvParseTask(
            id=task_id,
            connection_id=profile.id,
            connection_name=profile.name,
            source=source,
            import_mode=import_mode,
            database=(database or profile.database or "").strip() or None,
            delimiter=delimiter,
            charset=charset,
            has_header=has_header,
            state="queued",
            message="文件已登记，等待后台解析。",
            current_stage="queued",
            total_files=len(files),
            progress=20.0,
        )
        session.add(task)
        for index, (filename, content) in enumerate(files):
            safe_name = f"{index + 1:04d}_{_safe_storage_filename(filename)}"
            path = task_dir / safe_name
            path.write_bytes(content)
            size = len(content)
            total_bytes += size
            session.add(
                DorisCsvParseFile(
                    task_id=task_id,
                    filename=filename,
                    storage_path=str(path),
                    file_size=size,
                    file_index=index,
                    state="waiting",
                    message="等待解析。",
                )
            )
        task.total_bytes = total_bytes
        session.commit()
        _append_csv_task_log(
            session,
            task.id,
            None,
            "INFO",
            "upload",
            "CSV 文件已登记，等待后台解析。",
            {"total_files": len(files), "total_bytes": total_bytes},
        )
        session.commit()
        return get_csv_parse_task_status_sync(task_id, session=session)
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)
        session.rollback()
        raise
    finally:
        session.close()


def list_csv_parse_tasks_sync(limit: int = 20) -> DorisCsvParseTaskListResponse:
    session = get_sync_session_factory()()
    try:
        tasks = list(
            session.execute(
                select(DorisCsvParseTask).order_by(DorisCsvParseTask.created_at.desc()).limit(limit)
            ).scalars().all()
        )
        return DorisCsvParseTaskListResponse(tasks=[get_csv_parse_task_status_sync(task.id, session=session) for task in tasks])
    finally:
        session.close()


def update_csv_parse_file_preview_sync(
    task_id: uuid.UUID | str,
    file_id: uuid.UUID | str,
    preview: DorisCsvFilePreview,
) -> DorisCsvParseTaskStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisCsvParseTask, uuid.UUID(str(task_id)))
        if not task:
            raise KeyError("CSV 解析任务不存在。")
        file_row = session.get(DorisCsvParseFile, uuid.UUID(str(file_id)))
        if not file_row or file_row.task_id != task.id:
            raise KeyError("CSV 文件节点不存在。")
        file_row.preview = preview.model_dump(mode="json")
        file_row.table_name = preview.table_name
        file_row.warnings = list(preview.warnings or [])
        file_row.updated_at = app_now()
        task.updated_at = app_now()
        session.commit()
        _append_csv_task_log(
            session,
            task.id,
            file_row.id,
            "INFO",
            "spec",
            "已保存文件映射。",
            {
                "filename": file_row.filename,
                "table_name": preview.table_name,
                "column_count": len(preview.columns or []),
            },
        )
        session.commit()
        return get_csv_parse_task_status_sync(task.id, session=session)
    finally:
        session.close()


def list_csv_task_logs_sync(task_id: uuid.UUID | str, limit: int = 200) -> DorisCsvTaskLogListResponse:
    session = get_sync_session_factory()()
    try:
        task_uuid = uuid.UUID(str(task_id))
        task = session.get(DorisCsvParseTask, task_uuid)
        if not task:
            raise KeyError("CSV 解析任务不存在。")
        rows = list(
            session.execute(
                select(DorisCsvTaskLog)
                .where(DorisCsvTaskLog.task_id == task_uuid)
                .order_by(DorisCsvTaskLog.created_at.asc(), DorisCsvTaskLog.id.asc())
                .limit(limit)
            ).scalars().all()
        )
        return DorisCsvTaskLogListResponse(
            logs=[
                DorisCsvTaskLogItem(
                    id=row.id,
                    task_id=row.task_id,
                    file_id=row.file_id,
                    level=row.level,
                    stage=row.stage,
                    message=row.message or "",
                    payload=dict(row.payload or {}),
                    created_at=row.created_at,
                )
                for row in rows
            ]
        )
    finally:
        session.close()


def request_import_csv_task_sync(
    task_id: uuid.UUID | str,
    *,
    create_table: bool = True,
    overwrite: bool = False,
    database: str | None = None,
) -> DorisCsvParseTaskStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisCsvParseTask, uuid.UUID(str(task_id)))
        if not task:
            raise KeyError("CSV 解析任务不存在。")
        if task.state not in {"completed", "import_failed", "imported"}:
            raise ValueError("请先完成 CSV 解析后再确认导入。")
        target_database = (database or "").strip()
        if target_database:
            task.database = target_database
            if isinstance(task.result, dict):
                task.result["database"] = target_database
        task.import_create_table = create_table
        task.import_overwrite = overwrite
        task.import_requested_at = app_now()
        task.import_finished_at = None
        task.error_message = None
        task.state = "waiting_import"
        task.current_stage = "waiting_import"
        task.message = "已确认导入，等待后台开始执行。"
        task.updated_at = app_now()
        session.commit()
        _append_csv_task_log(
            session,
            task.id,
            None,
            "INFO",
            "import",
            "已提交导入任务。",
            {"create_table": create_table, "overwrite": overwrite, "database": task.database},
        )
        session.commit()
        return get_csv_parse_task_status_sync(task.id, session=session)
    finally:
        session.close()


def _append_csv_task_log(
    session: Session,
    task_id: uuid.UUID,
    file_id: uuid.UUID | None,
    level: str,
    stage: str | None,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        DorisCsvTaskLog(
            task_id=task_id,
            file_id=file_id,
            level=level,
            stage=stage,
            message=message,
            payload=payload or {},
        )
    )


def run_csv_parse_task_sync(task_id: uuid.UUID | str) -> None:
    task_uuid = uuid.UUID(str(task_id))
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisCsvParseTask, task_uuid)
        if not task or task.state in _CSV_PARSE_TERMINAL_STATES:
            return
        profile = session.get(DatabaseConnectionProfile, task.connection_id)
        if not profile:
            _fail_csv_parse_task(session, task, "Doris 数据连接不存在或已被删除。")
            return
        files = _csv_parse_files(session, task.id)
        if not files:
            _fail_csv_parse_task(session, task, "解析任务没有可处理的文件。")
            return

        now = app_now()
        task.state = "parsing"
        task.message = "正在解析 CSV 文件。"
        task.current_stage = "parsing"
        task.started_at = now
        task.updated_at = now
        task.progress = max(float(task.progress or 0), 20.0)
        session.commit()
        _append_csv_task_log(session, task.id, None, "INFO", "parse", "CSV 解析任务开始执行。", {"file_count": len(files)})
        session.commit()

        baseline_preview: DorisCsvFilePreview | None = None
        preview_files: list[DorisCsvFilePreview] = []
        completed = 0
        failed = 0
        processed_bytes = 0
        valid_rows = 0
        bad_rows = 0
        total_rows = 0

        for index, file_row in enumerate(files):
            if _csv_parse_stop_requested(session, task):
                _stop_remaining_csv_files(session, task, files[index:])
                return

            file_row.state = "parsing"
            file_row.message = "正在解析文件。"
            file_row.started_at = app_now()
            task.current_file = file_row.filename
            task.current_stage = "parsing"
            task.progress = _csv_parse_progress(index, len(files), 25)
            session.commit()

            try:
                content = Path(file_row.storage_path).read_bytes()
                if task.import_mode == "single_table" and baseline_preview is not None:
                    preview = _preview_single_table_followup(
                        file_row.filename,
                        content,
                        baseline_preview=baseline_preview,
                        delimiter=_delimiter(task.delimiter),
                        charset=task.charset,
                        has_header=task.has_header,
                        profile=profile,
                        database=task.database,
                    )
                else:
                    preview = _preview_one_file(
                        file_row.filename,
                        content,
                        delimiter=_delimiter(task.delimiter),
                        charset=task.charset,
                        has_header=task.has_header,
                        profile=profile,
                        database=task.database,
                    )
                    if task.import_mode == "single_table":
                        baseline_preview = preview

                if task.import_mode == "single_table" and baseline_preview is not None:
                    preview.table_name = baseline_preview.table_name
                    if preview.expected_columns != baseline_preview.expected_columns:
                        raise ValueError(
                            f"列数不一致：标准文件 {baseline_preview.filename} 为 {baseline_preview.expected_columns} 列，"
                            f"当前文件为 {preview.expected_columns} 列。"
                        )

                file_row.table_name = preview.table_name
                file_row.state = "succeeded"
                file_row.message = "解析成功。"
                file_row.total_rows = int(preview.valid_row_count or 0) + int(preview.bad_row_count or 0)
                file_row.valid_rows = int(preview.valid_row_count or 0)
                file_row.bad_rows = int(preview.bad_row_count or 0)
                file_row.processed_bytes = int(file_row.file_size or 0)
                file_row.preview = preview.model_dump(mode="json")
                file_row.warnings = list(preview.warnings or [])
                file_row.finished_at = app_now()
                preview_files.append(preview)
                completed += 1
                valid_rows += file_row.valid_rows
                bad_rows += file_row.bad_rows
                total_rows += file_row.total_rows
                _append_csv_task_log(
                    session,
                    task.id,
                    file_row.id,
                    "INFO",
                    "parse",
                    "CSV 文件解析成功。",
                    {
                        "filename": file_row.filename,
                        "table_name": preview.table_name,
                        "charset": preview.charset,
                        "charset_detection": preview.charset_detection,
                        "valid_rows": file_row.valid_rows,
                        "bad_rows": file_row.bad_rows,
                    },
                )
            except Exception as exc:
                failed += 1
                file_row.state = "failed"
                file_row.message = str(exc)
                file_row.finished_at = app_now()
                _append_csv_task_log(
                    session,
                    task.id,
                    file_row.id,
                    "ERROR",
                    "parse",
                    "CSV 文件解析失败。",
                    {"filename": file_row.filename, "error": str(exc)},
                )
            processed_bytes += int(file_row.file_size or 0)
            task.completed_files = completed
            task.failed_files = failed
            task.processed_bytes = processed_bytes
            task.valid_rows = valid_rows
            task.bad_rows = bad_rows
            task.total_rows = total_rows
            task.current_stage = "validating"
            task.progress = _csv_parse_progress(index + 1, len(files), 95)
            task.updated_at = app_now()
            session.commit()

        task.state = "completed" if failed == 0 else ("failed" if completed == 0 else "completed")
        task.message = (
            "CSV 文件解析完成。"
            if failed == 0
            else f"CSV 文件解析完成，成功 {completed} 个，失败 {failed} 个。"
        )
        task.current_stage = "completed"
        task.current_file = None
        task.progress = 100.0
        task.result = DorisCsvPreviewResponse(database=task.database, files=preview_files).model_dump(mode="json")
        task.finished_at = app_now()
        task.updated_at = task.finished_at
        session.commit()
        _append_csv_task_log(
            session,
            task.id,
            None,
            "INFO",
            "parse",
            "CSV 解析任务结束。",
            {"completed_files": completed, "failed_files": failed},
        )
        session.commit()
    except Exception as exc:
        task = session.get(DorisCsvParseTask, task_uuid)
        if task:
            _fail_csv_parse_task(session, task, str(exc))
    finally:
        session.close()


def get_csv_parse_task_status_sync(
    task_id: uuid.UUID | str,
    *,
    session: Session | None = None,
) -> DorisCsvParseTaskStatus:
    close_session = session is None
    db = session or get_sync_session_factory()()
    try:
        task = db.get(DorisCsvParseTask, uuid.UUID(str(task_id)))
        if not task:
            raise KeyError("CSV 解析任务不存在。")
        files = _csv_parse_files(db, task.id)
        preview_files = []
        file_statuses = []
        for file_row in files:
            preview = None
            if file_row.preview:
                preview = DorisCsvFilePreview.model_validate(file_row.preview)
                if file_row.state in {"succeeded", "waiting_import", "imported"}:
                    preview_files.append(preview)
            file_statuses.append(
                DorisCsvParseFileStatus(
                    id=file_row.id,
                    filename=file_row.filename,
                    table_name=file_row.table_name,
                    file_size=file_row.file_size,
                    file_index=file_row.file_index,
                    state=file_row.state,
                    message=file_row.message or "",
                    total_rows=file_row.total_rows,
                    valid_rows=file_row.valid_rows,
                    bad_rows=file_row.bad_rows,
                    processed_bytes=file_row.processed_bytes,
                    preview=preview,
                    warnings=list(file_row.warnings or []),
                    started_at=file_row.started_at,
                    finished_at=file_row.finished_at,
                    created_at=file_row.created_at,
                    updated_at=file_row.updated_at,
                )
            )
        preview_response = None
        if preview_files:
            preview_response = DorisCsvPreviewResponse(database=task.database, files=preview_files)
        import_result = None
        if task.result and {"state", "results"}.issubset(set(task.result.keys())):
            import_result = DorisCsvImportResponse.model_validate(task.result)
        return DorisCsvParseTaskStatus(
            task_id=task.id,
            connection_id=task.connection_id,
            connection_name=task.connection_name,
            source=task.source,
            import_mode=task.import_mode,
            database=task.database,
            delimiter=task.delimiter,
            charset=task.charset,
            has_header=task.has_header,
            state=task.state,
            message=task.message or "",
            current_stage=task.current_stage,
            current_file=task.current_file,
            total_files=task.total_files,
            completed_files=task.completed_files,
            failed_files=task.failed_files,
            total_bytes=task.total_bytes,
            processed_bytes=task.processed_bytes,
            total_rows=task.total_rows,
            valid_rows=task.valid_rows,
            bad_rows=task.bad_rows,
            progress=float(task.progress or 0),
            stop_requested=bool(task.stop_requested),
            error_message=task.error_message,
            import_create_table=bool(task.import_create_table),
            import_overwrite=bool(task.import_overwrite),
            import_requested_at=task.import_requested_at,
            import_started_at=task.import_started_at,
            import_finished_at=task.import_finished_at,
            import_total_files=int(task.import_total_files or 0),
            imported_files=int(task.imported_files or 0),
            import_failed_files=int(task.import_failed_files or 0),
            import_total_rows=int(task.import_total_rows or 0),
            import_loaded_rows=int(task.import_loaded_rows or 0),
            import_filtered_rows=int(task.import_filtered_rows or 0),
            result=import_result,
            preview=preview_response,
            files=file_statuses,
            started_at=task.started_at,
            finished_at=task.finished_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    finally:
        if close_session:
            db.close()


def request_stop_csv_parse_task_sync(task_id: uuid.UUID | str) -> DorisCsvParseTaskStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisCsvParseTask, uuid.UUID(str(task_id)))
        if not task:
            raise KeyError("CSV 解析任务不存在。")
        if task.state in _CSV_PARSE_TERMINAL_STATES:
            return get_csv_parse_task_status_sync(task.id, session=session)
        task.stop_requested = True
        task.state = "stopping"
        task.message = "已请求停止，等待当前文件处理到安全点。"
        task.current_stage = "stopping"
        task.updated_at = app_now()
        session.commit()
        return get_csv_parse_task_status_sync(task.id, session=session)
    finally:
        session.close()


def run_csv_import_task_sync(task_id: uuid.UUID | str) -> None:
    task_uuid = uuid.UUID(str(task_id))
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisCsvParseTask, task_uuid)
        if not task or task.state in {"imported", "import_failed", "stopped"}:
            return
        if task.state not in {"waiting_import", "completed", "import_failed"}:
            raise ValueError("请先完成解析，再执行导入。")
        profile = session.get(DatabaseConnectionProfile, task.connection_id)
        if not profile:
            _fail_csv_parse_task(session, task, "Doris 数据连接不存在或已被删除。")
            return
        target_database = (task.database or profile.database or "").strip()
        if not target_database:
            _fail_csv_import_task(session, task, "Missing Doris target database.")
            return
        _ensure_database(profile, target_database)
        files = _csv_parse_files(session, task.id)
        importable = [file_row for file_row in files if file_row.state in {"succeeded", "waiting_import", "import_failed"}]
        if not importable:
            _fail_csv_parse_task(session, task, "没有可导入的 CSV 文件。")
            return

        now = app_now()
        task.state = "importing"
        task.current_stage = "importing"
        task.message = "正在执行 CSV 导入。"
        task.import_started_at = now
        task.import_finished_at = None
        task.import_total_files = len(importable)
        task.imported_files = 0
        task.import_failed_files = 0
        task.import_total_rows = 0
        task.import_loaded_rows = 0
        task.import_filtered_rows = 0
        task.progress = max(float(task.progress or 0), 80.0)
        task.updated_at = now
        session.commit()
        _append_csv_task_log(session, task.id, None, "INFO", "import", "导入任务开始执行。", {
            "database": target_database,
            "create_table": bool(task.import_create_table),
            "overwrite": bool(task.import_overwrite),
            "file_count": len(importable),
        })
        session.commit()

        created_tables: set[str] = set()
        results: list[DorisCsvFileImportResult] = []
        baseline_import_preview: DorisCsvFilePreview | None = None

        for index, file_row in enumerate(importable):
            if _csv_parse_stop_requested(session, task):
                _stop_remaining_csv_import_files(session, task, importable[index:])
                _append_csv_task_log(session, task.id, file_row.id, "WARN", "import", "导入任务已停止。", {})
                session.commit()
                return

            preview_data = file_row.preview or {}
            preview = DorisCsvFilePreview.model_validate(preview_data)
            if task.import_mode == "single_table":
                if baseline_import_preview is None:
                    baseline_import_preview = DorisCsvFilePreview.model_validate(preview.model_dump())
                else:
                    if preview.expected_columns != baseline_import_preview.expected_columns:
                        raise ValueError(
                            f"列数不一致：标准文件 {baseline_import_preview.filename} 为 "
                            f"{baseline_import_preview.expected_columns} 列，当前文件 {file_row.filename} 为 "
                            f"{preview.expected_columns} 列。"
                        )
                    preview.columns = [
                        DorisCsvColumnPreview.model_validate(column.model_dump())
                        for column in baseline_import_preview.columns
                    ]
                    preview.table_name = baseline_import_preview.table_name
            file_row.state = "importing"
            file_row.message = "正在导入。"
            file_row.started_at = file_row.started_at or app_now()
            file_row.updated_at = app_now()
            task.current_file = file_row.filename
            task.current_stage = "importing"
            task.progress = _csv_import_progress(index, len(importable), 85)
            session.commit()

            try:
                content = Path(file_row.storage_path).read_bytes()
                prepared = _prepare_import_content(
                    file_row.filename,
                    content,
                    preview=preview,
                    delimiter=_delimiter(task.delimiter),
                    charset=_preview_charset(preview, task.charset),
                    has_header=preview.has_header,
                )
                if task.import_create_table:
                    table_key = preview.table_name if task.import_mode == "single_table" else f"{file_row.filename}:{preview.table_name}"
                    if table_key not in created_tables:
                        ddl = _create_table(
                            profile,
                            target_database,
                            preview,
                            overwrite=task.import_overwrite,
                        )
                        created_tables.add(table_key)
                        _append_csv_task_log(
                            session,
                            task.id,
                            file_row.id,
                            "INFO",
                            "create_table",
                            "目标表创建/检查完成。",
                            {"sql": ddl, "table_name": preview.table_name, "database": target_database},
                        )
                        session.commit()

                _append_csv_task_log(
                    session,
                    task.id,
                    file_row.id,
                    "INFO",
                    "stream_load",
                    "开始执行 Stream Load。",
                    {
                        "database": target_database,
                        "table_name": preview.table_name,
                        "columns": prepared["columns"],
                        "rejected_rows": len(prepared["bad_rows"]),
                    },
                )
                session.commit()

                result = asyncio.run(
                    _stream_load(
                        profile,
                        target_database,
                        file_row.filename,
                        preview.table_name,
                        prepared["content"],
                        columns=prepared["columns"],
                        delimiter=_delimiter(task.delimiter),
                        rejected_rows=len(prepared["bad_rows"]),
                        reject_preview=prepared["bad_rows"][:20],
                        reject_download_url=prepared["reject_download_url"],
                    )
                )
                results.append(result)

                file_row.total_rows = int(file_row.total_rows or 0) or len(prepared["bad_rows"]) + len(prepared["content"].splitlines())
                file_row.valid_rows = int(result.loaded_rows or 0)
                file_row.bad_rows = len(prepared["bad_rows"])
                file_row.processed_bytes = int(file_row.file_size or 0)
                file_row.state = "imported" if result.state == "success" else "import_failed"
                file_row.message = result.message or ("导入成功。" if result.state == "success" else "导入失败。")
                file_row.finished_at = app_now()
                file_row.updated_at = file_row.finished_at
                task.imported_files = int(task.imported_files or 0) + (1 if result.state == "success" else 0)
                task.import_failed_files = int(task.import_failed_files or 0) + (0 if result.state == "success" else 1)
                task.import_total_rows = int(task.import_total_rows or 0) + int(file_row.total_rows or 0)
                task.import_loaded_rows = int(task.import_loaded_rows or 0) + int(result.loaded_rows or 0)
                task.import_filtered_rows = int(task.import_filtered_rows or 0) + int(result.filtered_rows or 0)
                _append_csv_task_log(
                    session,
                    task.id,
                    file_row.id,
                    "INFO",
                    "stream_load",
                    "Stream Load 执行完成。",
                    {
                        "state": result.state,
                        "loaded_rows": result.loaded_rows,
                        "filtered_rows": result.filtered_rows,
                        "unselected_rows": result.unselected_rows,
                        "message": result.message,
                    },
                )
            except Exception as exc:
                file_row.state = "import_failed"
                file_row.message = str(exc)
                file_row.finished_at = app_now()
                file_row.updated_at = file_row.finished_at
                task.import_failed_files = int(task.import_failed_files or 0) + 1
                _append_csv_task_log(
                    session,
                    task.id,
                    file_row.id,
                    "ERROR",
                    "import",
                    "导入失败。",
                    {"error": str(exc)},
                )
                results.append(
                    DorisCsvFileImportResult(
                        filename=file_row.filename,
                        table_name=preview.table_name,
                        state="failed",
                        message=str(exc),
                    )
                )

            task.updated_at = app_now()
            task.progress = _csv_import_progress(index + 1, len(importable), 99)
            session.commit()

        success_count = sum(1 for item in results if item.state == "success")
        failed_count = len(results) - success_count
        response_state = "success" if failed_count == 0 else ("partial" if success_count else "failed")
        task.state = "imported" if failed_count == 0 else "import_failed"
        task.current_stage = task.state
        task.current_file = None
        task.import_finished_at = app_now()
        task.progress = 100.0
        task.message = (
            "CSV 导入完成。"
            if failed_count == 0
            else f"CSV 导入完成，成功 {success_count} 个，失败 {failed_count} 个。"
        )
        task.result = DorisCsvImportResponse(
            database=target_database,
            state=response_state,
            success_count=success_count,
            failed_count=failed_count,
            results=results,
        ).model_dump(mode="json")
        task.finished_at = app_now()
        task.updated_at = task.finished_at
        session.commit()
        _append_csv_task_log(
            session,
            task.id,
            None,
            "INFO",
            "import",
            "导入任务结束。",
            {"state": task.state, "success_count": success_count, "failed_count": failed_count},
        )
        session.commit()
    except Exception as exc:
        task = session.get(DorisCsvParseTask, task_uuid)
        if task:
            _fail_csv_import_task(session, task, str(exc))
    finally:
        session.close()


def _csv_parse_files(session: Session, task_id: uuid.UUID) -> list[DorisCsvParseFile]:
    return list(
        session.execute(
            select(DorisCsvParseFile)
            .where(DorisCsvParseFile.task_id == task_id)
            .order_by(DorisCsvParseFile.file_index.asc(), DorisCsvParseFile.created_at.asc())
        ).scalars().all()
    )


def _csv_parse_stop_requested(session: Session, task: DorisCsvParseTask) -> bool:
    session.refresh(task)
    return bool(task.stop_requested or task.state == "stopping")


def _stop_remaining_csv_files(
    session: Session,
    task: DorisCsvParseTask,
    files: list[DorisCsvParseFile],
) -> None:
    now = app_now()
    for file_row in files:
        if file_row.state in {"waiting", "parsing"}:
            file_row.state = "stopped"
            file_row.message = "任务停止，文件未继续解析。"
            file_row.finished_at = now
    task.state = "stopped"
    task.message = "CSV 解析任务已停止。"
    task.current_stage = "stopped"
    task.current_file = None
    task.finished_at = now
    task.updated_at = now
    session.commit()


def _stop_remaining_csv_import_files(
    session: Session,
    task: DorisCsvParseTask,
    files: list[DorisCsvParseFile],
) -> None:
    now = app_now()
    for file_row in files:
        if file_row.state in {"waiting", "parsing", "succeeded", "waiting_import", "importing"}:
            file_row.state = "stopped"
            file_row.message = "任务已停止，文件未继续导入。"
            file_row.finished_at = now
            file_row.updated_at = now
    task.state = "stopped"
    task.message = "CSV 导入任务已停止。"
    task.current_stage = "stopped"
    task.current_file = None
    task.finished_at = now
    task.updated_at = now
    session.commit()


def _fail_csv_parse_task(session: Session, task: DorisCsvParseTask, message: str) -> None:
    now = app_now()
    task.state = "failed"
    task.message = message
    task.error_message = message
    task.current_stage = "failed"
    task.current_file = None
    task.finished_at = now
    task.updated_at = now
    session.commit()


def _fail_csv_import_task(session: Session, task: DorisCsvParseTask, message: str) -> None:
    now = app_now()
    task.state = "import_failed"
    task.message = message
    task.error_message = message
    task.current_stage = "import_failed"
    task.current_file = None
    task.import_finished_at = now
    task.finished_at = now
    task.updated_at = now
    session.commit()


def _csv_parse_progress(done_files: int, total_files: int, stage_ceiling: float) -> float:
    if total_files <= 0:
        return min(stage_ceiling, 100.0)
    return round(min(20.0 + (float(done_files) / float(total_files)) * 75.0, stage_ceiling), 2)


def _csv_import_progress(done_files: int, total_files: int, stage_ceiling: float) -> float:
    if total_files <= 0:
        return min(stage_ceiling, 100.0)
    return round(min(80.0 + (float(done_files) / float(total_files)) * 19.0, stage_ceiling), 2)


def _csv_parse_task_dir(task_id: uuid.UUID) -> Path:
    return Path(get_settings().staging_dir) / "doris-csv-parse" / str(task_id)


def _safe_storage_filename(filename: str) -> str:
    name = PurePath(filename or "upload.csv").name
    safe = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name).strip("._")
    return safe or "upload.csv"


def _preview_single_table_followup(
    filename: str,
    content: bytes,
    *,
    baseline_preview: DorisCsvFilePreview,
    delimiter: str,
    charset: str,
    has_header: bool,
    profile: DatabaseConnectionProfile | None = None,
    database: str | None = None,
) -> DorisCsvFilePreview:
    resolved_charset, charset_detection = _resolve_csv_charset(content, charset)
    use_header = has_header
    warnings: list[str] = []
    if has_header:
        first_row = _first_csv_row(filename, content, delimiter=delimiter, charset=resolved_charset)
        baseline_header = [column.original_name or column.name for column in baseline_preview.columns]
        if _normalized_header(first_row) == _normalized_header(baseline_header):
            use_header = True
        elif len(first_row) == len(baseline_header):
            use_header = False
            warnings.append("首行与标准表头不一致，已按数据行处理；请确认该文件是否缺少表头。")
        else:
            raise ValueError(
                f"表头列数与标准文件不一致：标准 {len(baseline_header)} 列，当前首行 {len(first_row)} 列。"
            )
    preview = _preview_one_file(
        filename,
        content,
        delimiter=delimiter,
        charset=resolved_charset,
        has_header=use_header,
        profile=profile,
        database=database,
        resolved_charset=resolved_charset,
        charset_detection=charset_detection,
    )
    if preview.expected_columns == baseline_preview.expected_columns:
        preview.columns = [DorisCsvColumnPreview.model_validate(item.model_dump()) for item in baseline_preview.columns]
        preview.table_name = baseline_preview.table_name
        preview.has_header = use_header
        preview.warnings.extend(warnings)
    return preview


def _first_csv_row(filename: str, content: bytes, *, delimiter: str, charset: str) -> list[str]:
    text = _decode(content, charset)
    try:
        return next(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
    except StopIteration as exc:
        raise ValueError(f"{filename} 是空文件。") from exc
    except csv.Error as exc:
        raise ValueError(f"{filename} CSV 格式错误：{exc}") from exc


def _normalized_header(values: list[str]) -> list[str]:
    return [re.sub(r"\s+", "", str(value or "")).lower() for value in values]



def list_ftp_directory(ftp_conn: DorisFtpConnection) -> DorisFtpCatalogResponse:
    directory = _normalize_ftp_directory(ftp_conn.directory)
    items: list[DorisFtpCatalogItem] = []
    with _ftp_client(ftp_conn) as ftp:
        try:
            for name, facts in ftp.mlsd(directory):
                if name in {".", ".."}:
                    continue
                item_type = "directory" if facts.get("type") == "dir" else "file"
                items.append(
                    DorisFtpCatalogItem(
                        name=name,
                        path=_ftp_join(directory, name),
                        type=item_type,
                        size=_safe_int(facts.get("size")) if facts.get("size") else None,
                        modified=facts.get("modify"),
                    )
                )
        except Exception:
            current = ftp.pwd()
            ftp.cwd(directory)
            try:
                for name in ftp.nlst():
                    if name in {".", ".."}:
                        continue
                    path = _ftp_join(directory, posixpath.basename(name))
                    items.append(
                        DorisFtpCatalogItem(
                            name=posixpath.basename(name),
                            path=path,
                            type="file",
                            size=_ftp_size(ftp, path),
                        )
                    )
            finally:
                ftp.cwd(current)
    items.sort(key=lambda item: (item.type != "directory", item.name.lower()))
    return DorisFtpCatalogResponse(directory=directory, items=items)


def fetch_ftp_csv_files(
    ftp_conn: DorisFtpConnection,
    *,
    filenames: list[str],
    include_all_csv: bool = False,
) -> list[tuple[str, bytes]]:
    directory = _normalize_ftp_directory(ftp_conn.directory)
    selected = [name for name in filenames if name.lower().endswith(".csv")]
    if include_all_csv:
        catalog = list_ftp_directory(ftp_conn)
        selected = [item.name for item in catalog.items if item.type == "file" and item.name.lower().endswith(".csv")]
    if not selected:
        raise ValueError("请至少选择一个 CSV 文件。")

    files: list[tuple[str, bytes]] = []
    with _ftp_client(ftp_conn) as ftp:
        for raw_name in selected:
            name = posixpath.basename(raw_name)
            remote_path = _ftp_join(directory, name)
            buffer = io.BytesIO()
            ftp.retrbinary(f"RETR {remote_path}", buffer.write)
            content = buffer.getvalue()
            if not content:
                raise ValueError(f"{name} 是空文件。")
            files.append((name, content))
    return files


def preview_csv_files(
    files: list[tuple[str, bytes]],
    *,
    database: str | None,
    delimiter: str = ",",
    charset: str = "utf-8-sig",
    has_header: bool = True,
    profile: DatabaseConnectionProfile | None = None,
) -> DorisCsvPreviewResponse:
    previews = [
        _preview_one_file(
            filename,
            content,
            delimiter=_delimiter(delimiter),
            charset=charset,
            has_header=has_header,
            profile=profile,
            database=database,
        )
        for filename, content in files
    ]
    return DorisCsvPreviewResponse(database=database or None, files=previews)


async def import_csv_files(
    profile: DatabaseConnectionProfile,
    files: list[tuple[str, bytes]],
    *,
    database: str | None,
    delimiter: str = ",",
    charset: str = "utf-8-sig",
    has_header: bool = True,
    import_mode: str = "multiple_tables",
    create_table: bool = True,
    overwrite: bool = False,
    table_specs_json: str | None = None,
) -> DorisCsvImportResponse:
    target_database = (database or profile.database or "").strip()
    if not target_database:
        raise ValueError("请先选择或填写 Doris 目标库。")

    specs = _load_specs(table_specs_json)
    results: list[DorisCsvFileImportResult] = []
    sep = _delimiter(delimiter)
    _ensure_database(profile, target_database)
    created_tables: set[str] = set()

    for filename, content in files:
        try:
            preview = specs.get(filename) or _preview_one_file(
                filename,
                content,
                delimiter=sep,
                charset=charset,
                has_header=has_header,
                profile=profile,
                database=target_database,
            )
            prepared = _prepare_import_content(
                filename,
                content,
                preview=preview,
                delimiter=sep,
                charset=_preview_charset(preview, charset),
                has_header=preview.has_header,
            )
            if create_table:
                table_key = preview.table_name if import_mode == "single_table" else f"{filename}:{preview.table_name}"
                if table_key in created_tables:
                    pass
                else:
                    _create_table(
                        profile,
                        target_database,
                        preview,
                        overwrite=overwrite,
                    )
                    created_tables.add(table_key)
            results.append(
                await _stream_load(
                    profile,
                    target_database,
                    filename,
                    preview.table_name,
                    prepared["content"],
                    columns=prepared["columns"],
                    delimiter=sep,
                    rejected_rows=len(prepared["bad_rows"]),
                    reject_preview=prepared["bad_rows"][:20],
                    reject_download_url=prepared["reject_download_url"],
                )
            )
        except Exception as exc:
            table_name = specs.get(filename).table_name if filename in specs else _safe_identifier(
                PurePath(filename).stem or "csv_table"
            )
            results.append(
                DorisCsvFileImportResult(
                    filename=filename,
                    table_name=table_name,
                    state="failed",
                    message=str(exc),
                )
            )

    success_count = sum(1 for item in results if item.state == "success")
    failed_count = len(results) - success_count
    state = "success" if failed_count == 0 else ("partial" if success_count else "failed")
    return DorisCsvImportResponse(
        database=target_database,
        state=state,
        success_count=success_count,
        failed_count=failed_count,
        results=results,
    )


def _preview_one_file(
    filename: str,
    content: bytes,
    *,
    delimiter: str,
    charset: str,
    has_header: bool,
    profile: DatabaseConnectionProfile | None = None,
    database: str | None = None,
    resolved_charset: str | None = None,
    charset_detection: dict[str, Any] | None = None,
) -> DorisCsvFilePreview:
    effective_charset, detection = (
        (resolved_charset, charset_detection or {})
        if resolved_charset
        else _resolve_csv_charset(content, charset)
    )
    raw_header, good_rows, bad_rows = _scan_csv_rows(
        filename,
        content,
        delimiter=delimiter,
        charset=effective_charset,
        has_header=has_header,
    )
    warnings: list[str] = []
    columns = _build_columns(raw_header, warnings)

    for col_index, column in enumerate(columns):
        values = [_cell(row, col_index) for row in good_rows[:200]]
        column.type = _infer_type(values, column)
        column.nullable = any(_is_null(value) for value in values)
        column.sample_values = _sample_values(values)

    sample_rows: list[dict[str, Any]] = []
    for row in good_rows[:20]:
        sample_rows.append({column.name: _cell(row, idx) for idx, column in enumerate(columns)})

    table_name = _safe_identifier(PurePath(filename).stem or "csv_table")
    table_exists = False
    target_columns: list[str] = []
    if profile and database:
        table_exists, target_columns = _target_table_columns(profile, database, table_name)

    return DorisCsvFilePreview(
        filename=filename,
        table_name=table_name,
        charset=effective_charset,
        charset_detection=detection,
        has_header=has_header,
        expected_columns=len(columns),
        valid_row_count=len(good_rows),
        bad_row_count=len(bad_rows),
        columns=columns,
        sample_rows=sample_rows,
        bad_rows=bad_rows[:20],
        target_table_exists=table_exists,
        target_table_columns=target_columns,
        total_preview_rows=len(good_rows),
        warnings=warnings,
    )


def _scan_csv_rows(
    filename: str,
    content: bytes,
    *,
    delimiter: str,
    charset: str,
    has_header: bool,
) -> tuple[list[str], list[list[str]], list[DorisCsvBadRowPreview]]:
    text = _decode(content, charset)
    raw_lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    bad_rows: list[DorisCsvBadRowPreview] = []
    try:
        first_row = next(reader)
    except StopIteration as exc:
        raise ValueError(f"{filename} 是空文件。") from exc
    except csv.Error as exc:
        raise ValueError(f"{filename} CSV 格式错误：{exc}") from exc
    if not first_row:
        raise ValueError(f"{filename} 第一行为空，无法判断列数。")

    if has_header:
        raw_header = first_row
        expected_columns = len(raw_header)
        good_rows: list[list[str]] = []
        row_number = 2
    else:
        expected_columns = len(first_row)
        raw_header = [_excel_column_name(index) for index in range(expected_columns)]
        good_rows = [first_row]
        row_number = 2

    previous_line_num = reader.line_num
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            start_line = previous_line_num + 1
            _scan_physical_rows_after_error(
                raw_lines,
                start_line=start_line,
                delimiter=delimiter,
                expected_columns=expected_columns,
                good_rows=good_rows,
                bad_rows=bad_rows,
                first_error=exc,
            )
            break
        start_line = previous_line_num + 1
        end_line = reader.line_num
        raw_text = _raw_text_slice(raw_lines, start_line, end_line)
        if len(row) != expected_columns:
            bad_rows.append(
                DorisCsvBadRowPreview(
                    row_number=start_line,
                    reason=f"列数不一致：期望 {expected_columns} 列，实际 {len(row)} 列。",
                    raw_text=raw_text,
                    values=row[:50],
                )
            )
        else:
            good_rows.append(row)
        previous_line_num = end_line
        row_number += 1
    return raw_header, good_rows, bad_rows


def _raw_text_slice(raw_lines: list[str], start_line: int, end_line: int) -> str:
    if not raw_lines:
        return ""
    start = max(start_line - 1, 0)
    end = min(max(end_line, start_line), len(raw_lines))
    return "".join(raw_lines[start:end]).rstrip("\r\n")


def _scan_physical_rows_after_error(
    raw_lines: list[str],
    *,
    start_line: int,
    delimiter: str,
    expected_columns: int,
    good_rows: list[list[str]],
    bad_rows: list[DorisCsvBadRowPreview],
    first_error: csv.Error,
) -> None:
    for line_number in range(start_line, len(raw_lines) + 1):
        raw_text = _raw_text_slice(raw_lines, line_number, line_number)
        if raw_text == "":
            continue
        try:
            row = next(csv.reader(io.StringIO(raw_text), delimiter=delimiter, strict=True))
        except csv.Error as exc:
            bad_rows.append(
                DorisCsvBadRowPreview(
                    row_number=line_number,
                    reason=f"CSV 格式错误：{first_error if line_number == start_line else exc}",
                    raw_text=raw_text,
                    values=[],
                )
            )
            continue
        if len(row) != expected_columns:
            bad_rows.append(
                DorisCsvBadRowPreview(
                    row_number=line_number,
                    reason=f"列数不一致：期望 {expected_columns} 列，实际 {len(row)} 列。",
                    raw_text=raw_text,
                    values=row[:50],
                )
            )
        else:
            good_rows.append(row)


def _build_columns(raw_header: list[str], warnings: list[str]) -> list[DorisCsvColumnPreview]:
    seen: dict[str, int] = {}
    columns: list[DorisCsvColumnPreview] = []
    for index, raw in enumerate(raw_header, start=1):
        original = (raw or "").strip()
        name = _safe_identifier(original or f"col_{index}", fallback=f"col_{index}")
        count = seen.get(name, 0) + 1
        seen[name] = count
        if count > 1:
            name = f"{name}_{count}"
            warnings.append(f"字段 {original or index} 重名，已改为 {name}。")
        if not original:
            warnings.append(f"第 {index} 个字段名为空，已改为 {name}。")
        columns.append(
            DorisCsvColumnPreview(
                original_name=original,
                name=name,
                type="VARCHAR(65533)",
            )
        )
    return columns


def _infer_type(values: list[str], column: DorisCsvColumnPreview) -> str:
    non_empty = [value.strip() for value in values if not _is_null(value)]
    column.max_length = max((len(value) for value in non_empty), default=0)
    if not non_empty:
        return "VARCHAR(65533)"
    if all(_is_bool(value) for value in non_empty):
        return "BOOLEAN"
    if all(_INT_RE.match(value) for value in non_empty):
        return "BIGINT"
    if all(_is_decimal(value) for value in non_empty):
        scale = min(max((_decimal_scale(value) for value in non_empty), default=0), 18)
        return f"DECIMAL(38,{scale})"
    if all(_is_date(value) for value in non_empty):
        return "DATE"
    if all(_is_datetime(value) for value in non_empty):
        return "DATETIME"
    return "VARCHAR(65533)"


def _load_specs(table_specs_json: str | None) -> dict[str, DorisCsvFilePreview]:
    if not table_specs_json:
        return {}
    raw = json.loads(table_specs_json)
    if isinstance(raw, dict) and "files" in raw:
        raw = raw["files"]
    specs: dict[str, DorisCsvFilePreview] = {}
    for item in raw or []:
        preview = DorisCsvFilePreview.model_validate(item)
        specs[preview.filename] = preview
    return specs


def _prepare_import_content(
    filename: str,
    content: bytes,
    *,
    preview: DorisCsvFilePreview,
    delimiter: str,
    charset: str,
    has_header: bool,
) -> dict[str, Any]:
    _, good_rows, bad_rows = _scan_csv_rows(
        filename,
        content,
        delimiter=delimiter,
        charset=charset,
        has_header=has_header,
    )
    mapped = [(index, column.name.strip()) for index, column in enumerate(preview.columns) if column.name.strip()]
    if not mapped:
        raise ValueError(f"{filename} 没有可导入的映射字段。")

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, quotechar='"', quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow([name for _, name in mapped])
    for row in good_rows:
        writer.writerow([_cell(row, index) for index, _ in mapped])

    return {
        "content": buffer.getvalue().encode("utf-8"),
        "columns": [name for _, name in mapped],
        "bad_rows": bad_rows,
        "reject_download_url": _write_reject_file(filename, bad_rows),
    }


def _write_reject_file(filename: str, bad_rows: list[DorisCsvBadRowPreview]) -> str | None:
    if not bad_rows:
        return None
    reject_dir = Path("/tmp/doris-csv-rejects")
    reject_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = reject_dir / f"{token}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "row_number", "reason", "raw_text", "values"])
        for row in bad_rows:
            writer.writerow(
                [
                    filename,
                    row.row_number,
                    row.reason,
                    row.raw_text or "",
                    json.dumps(row.values, ensure_ascii=False),
                ]
            )
    return f"/api/v1/doris-csv/rejects/{token}"


def reject_file_path(token: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("问题数据文件标识无效。")
    path = Path("/tmp/doris-csv-rejects") / f"{token}.csv"
    if not path.exists():
        raise FileNotFoundError("问题数据文件不存在或已过期。")
    return path


def _ensure_database(profile: DatabaseConnectionProfile, database: str) -> None:
    with _doris_mysql_conn(profile, None) as db:
        with db.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_ident(database)}")


def _create_table(
    profile: DatabaseConnectionProfile,
    database: str,
    preview: DorisCsvFilePreview,
    *,
    overwrite: bool,
) -> str:
    columns = [column for column in preview.columns if column.name.strip()]
    if not columns:
        raise ValueError(f"{preview.filename} 没有可导入字段。")
    key_column = _choose_key_column(columns)
    definitions = []
    for column in columns:
        col_type = normalize_doris_csv_column_type(column.type)
        if column.name == key_column and (_varchar_type_length(col_type) or 0) > 255:
            col_type = "VARCHAR(255)"
            preview.warnings.append(
                f"{column.name} 被用作 Doris 明细模型 key，已按 VARCHAR(255) 建表。"
            )
        definitions.append(f"{_ident(column.name)} {col_type}")
    ddl = f"""
CREATE TABLE IF NOT EXISTS {_ident(database)}.{_ident(preview.table_name)} (
  {", ".join(definitions)}
)
DUPLICATE KEY({_ident(key_column)})
DISTRIBUTED BY HASH({_ident(key_column)}) BUCKETS 10
PROPERTIES ("replication_num" = "1")
"""
    with _doris_mysql_conn(profile, database) as db:
        with db.cursor() as cur:
            if overwrite:
                cur.execute(f"DROP TABLE IF EXISTS {_ident(database)}.{_ident(preview.table_name)}")
            cur.execute(ddl)
    return ddl


async def _stream_load(
    profile: DatabaseConnectionProfile,
    database: str,
    filename: str,
    table_name: str,
    content: bytes,
    *,
    columns: list[str],
    delimiter: str,
    rejected_rows: int = 0,
    reject_preview: list[DorisCsvBadRowPreview] | None = None,
    reject_download_url: str | None = None,
) -> DorisCsvFileImportResult:
    url = (
        f"{_stream_load_base_url(profile)}/api/"
        f"{quote(database, safe='')}/{quote(table_name, safe='')}/_stream_load"
    )
    label = f"csv_import_{uuid.uuid4().hex}"
    headers = _stream_load_headers(columns, delimiter=delimiter, label=label)
    async with httpx.AsyncClient(timeout=600.0, follow_redirects=False) as client:
        response = await _trusted_stream_load_put(
            client,
            url,
            content=content,
            headers=headers,
            username=profile.username,
            password=_profile_password(profile),
        )
    data: dict[str, Any]
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    status = str(data.get("Status") or data.get("status") or "")
    ok = response.is_success and status.lower() == "success"
    return DorisCsvFileImportResult(
        filename=filename,
        table_name=table_name,
        state="success" if ok else "failed",
        message=data.get("Message") or data.get("msg") or response.reason_phrase,
        loaded_rows=_safe_int(data.get("NumberLoadedRows")),
        filtered_rows=_safe_int(data.get("NumberFilteredRows")),
        unselected_rows=_safe_int(data.get("NumberUnselectedRows")),
        rejected_rows=rejected_rows,
        reject_preview=reject_preview or [],
        reject_download_url=reject_download_url,
        raw_result=data,
    )


def _stream_load_headers(columns: list[str], *, delimiter: str, label: str) -> dict[str, str]:
    has_non_ascii_column = any(not column.isascii() for column in columns)
    headers = {
        "format": "csv_with_names" if has_non_ascii_column else "csv",
        "column_separator": _stream_load_separator(delimiter),
        "strict_mode": "false",
        "max_filter_ratio": "1",
        "enclose": '"',
        "trim_double_quotes": "true",
        "label": label,
        "Expect": "100-continue",
    }
    if not has_non_ascii_column:
        headers["skip_lines"] = "1"
        headers["columns"] = ",".join(columns)
    return headers


async def _trusted_stream_load_put(
    client: httpx.AsyncClient,
    url: str,
    *,
    content: bytes,
    headers: dict[str, str],
    username: str,
    password: str,
) -> httpx.Response:
    response = await client.put(
        url,
        content=content,
        headers=headers,
        auth=(username, password),
    )
    if response.status_code not in {301, 302, 303, 307, 308}:
        return response
    location = response.headers.get("location")
    if not location:
        return response
    return await client.put(
        location,
        content=content,
        headers=headers,
        auth=(username, password),
    )


def _doris_mysql_conn(profile: DatabaseConnectionProfile, database: str | None):
    return pymysql.connect(
        host=profile.host,
        port=profile.port or 9030,
        user=profile.username,
        password=_profile_password(profile),
        database=database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _profile_password(profile: DatabaseConnectionProfile) -> str:
    return decrypt_secret(profile.password_enc, get_settings().credential_encryption_key)


def _stream_load_base_url(profile: DatabaseConnectionProfile) -> str:
    dsn = (profile.dsn or "").strip()
    if dsn.startswith(("http://", "https://")):
        return dsn.rstrip("/")
    if dsn.isdigit():
        return f"http://{profile.host}:{dsn}"
    if dsn:
        return ("http://" + dsn).rstrip("/")
    return f"http://{profile.host}:8030"


def _target_table_columns(
    profile: DatabaseConnectionProfile,
    database: str,
    table_name: str,
) -> tuple[bool, list[str]]:
    try:
        with _doris_mysql_conn(profile, database) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                    """,
                    (database, table_name),
                )
                rows = cur.fetchall()
        columns = [str(row.get("COLUMN_NAME") or row.get("column_name") or next(iter(row.values()))) for row in rows]
        return bool(columns), columns
    except Exception:
        return False, []


def _safe_identifier(value: str, *, fallback: str = "csv_table") -> str:
    name = _IDENT_RE.sub("_", value.strip().replace("`", "_"))
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = fallback
    if name[0].isdigit():
        name = f"t_{name}"
    return name[:64]


def _excel_column_name(index: int) -> str:
    name = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _ftp_client(ftp_conn: DorisFtpConnection):
    ftp = ftplib.FTP()
    ftp.connect(ftp_conn.host, ftp_conn.port, timeout=30)
    ftp.login(ftp_conn.username, ftp_conn.password.get_secret_value())
    ftp.set_pasv(True)
    return ftp


def _normalize_ftp_directory(value: str | None) -> str:
    directory = (value or "/").strip() or "/"
    if not directory.startswith("/"):
        directory = "/" + directory
    normalized = posixpath.normpath(directory)
    return "/" if normalized == "." else normalized


def _ftp_join(directory: str, name: str) -> str:
    return posixpath.normpath(posixpath.join(directory, posixpath.basename(name)))


def _ftp_size(ftp, path: str) -> int | None:
    try:
        ftp.voidcmd("TYPE I")
        return int(ftp.size(path))
    except Exception:
        return None


def _ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def _delimiter(value: str) -> str:
    if value in {"\\t", "tab", "TAB"}:
        return "\t"
    return value or ","


def _stream_load_separator(value: str) -> str:
    return "\\t" if value == "\t" else value


def _preview_charset(preview: DorisCsvFilePreview | None, fallback: str) -> str:
    return (getattr(preview, "charset", None) or fallback or "utf-8-sig").strip() or "utf-8-sig"


def _resolve_csv_charset(content: bytes, charset: str | None) -> tuple[str, dict[str, Any]]:
    requested = (charset or "utf-8-sig").strip().lower()
    if requested not in _AUTO_CHARSET_VALUES:
        return requested, {"mode": "manual", "charset": requested}

    if content.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", {"mode": "auto", "charset": "utf-8-sig", "reason": "utf8_bom"}

    candidates = ("utf-8", "gb18030", "gbk")
    errors: dict[str, str] = {}
    for candidate in candidates:
        try:
            content.decode(candidate)
            return candidate, {"mode": "auto", "charset": candidate, "candidates": list(candidates)}
        except UnicodeDecodeError as exc:
            errors[candidate] = str(exc)
    raise ValueError("文件编码无法自动识别，请为该文件手动选择 UTF-8、GB18030 或 GBK 后重试。")


def _decode(content: bytes, charset: str) -> str:
    try:
        resolved, _ = _resolve_csv_charset(content, charset)
        return content.decode(resolved or "utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件编码不是 {charset}，请切换编码后重试。") from exc


def _cell(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _sample_values(values: list[str]) -> list[str]:
    samples: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        samples.append(text[:80])
        if len(samples) >= 3:
            break
    return samples


def _is_null(value: str) -> bool:
    return value.strip().lower() in _NULL_VALUES


def _is_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "false", "yes", "no", "0", "1"}


def _is_decimal(value: str) -> bool:
    if not (_DECIMAL_RE.match(value) or _INT_RE.match(value)):
        return False
    try:
        Decimal(value)
        return True
    except InvalidOperation:
        return False


def _decimal_scale(value: str) -> int:
    return max(-Decimal(value).as_tuple().exponent, 0)


def _is_date(value: str) -> bool:
    return _matches_datetime(value, _DATE_FORMATS)


def _is_datetime(value: str) -> bool:
    return _matches_datetime(value, _DATETIME_FORMATS)


def _matches_datetime(value: str, formats: tuple[str, ...]) -> bool:
    for fmt in formats:
        try:
            datetime.strptime(value.strip(), fmt)
            return True
        except ValueError:
            pass
    return False


def _choose_key_column(columns: list[DorisCsvColumnPreview]) -> str:
    for column in columns:
        if _is_doris_key_type(column.type) and (_varchar_type_length(column.type) or 0) <= 255:
            return column.name
    for column in columns:
        if _is_doris_key_type(column.type):
            return column.name
    raise ValueError("至少需要一个可作为 Doris 明细模型 Key 的整数、日期、定点数或 CHAR/VARCHAR 字段。")


def _varchar_type_length(column_type: str) -> int | None:
    match = re.fullmatch(r"VARCHAR\((\d+)\)", normalize_doris_csv_column_type(column_type))
    return int(match.group(1)) if match else None


def _is_doris_key_type(column_type: str) -> bool:
    normalized = normalize_doris_csv_column_type(column_type)
    return normalized.startswith(("CHAR(", "VARCHAR(", "DECIMAL(", "DECIMALV3(")) or normalized in {
        "BOOLEAN",
        "TINYINT",
        "SMALLINT",
        "INT",
        "BIGINT",
        "LARGEINT",
        "DATE",
        "DATETIME",
        "DATEV2",
        "DATETIMEV2",
    } or normalized.startswith("DATETIMEV2(")


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
