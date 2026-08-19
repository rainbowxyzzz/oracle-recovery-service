import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.doris_csv_import import (
    DorisCsvImportResponse,
    DorisCsvImportMode,
    DorisCsvFilePreviewUpdateRequest,
    DorisCsvImportStartRequest,
    DorisCsvParseTaskListResponse,
    DorisCsvParseTaskStatus,
    DorisCsvTaskLogListResponse,
    DorisCsvPreviewResponse,
    DorisFtpCatalogRequest,
    DorisFtpCatalogResponse,
    DorisFtpCsvImportRequest,
    DorisFtpCsvRequest,
)
from recovery_service.services.database_connections import get_profile, profile_to_ftp_connection
from recovery_service.services.doris_csv_import import (
    create_csv_parse_task,
    fetch_ftp_csv_files,
    import_csv_files,
    list_ftp_directory,
    list_csv_parse_tasks_sync,
    list_csv_task_logs_sync,
    get_csv_parse_task_status_sync,
    preview_csv_files,
    reject_file_path,
    request_import_csv_task_sync,
    request_stop_csv_parse_task_sync,
    run_csv_import_task_sync,
    run_csv_parse_task_sync,
    update_csv_parse_file_preview_sync,
)

router = APIRouter(prefix="/doris-csv", tags=["doris-csv"])


@router.get("/rejects/{token}")
async def download_doris_csv_rejects(
    token: str,
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        path = reject_file_path(token)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, filename=f"doris_csv_problem_rows_{token}.csv", media_type="text/csv")


async def _doris_profile(db: AsyncSession, connection_id: uuid.UUID):
    profile = await get_profile(db, connection_id)
    if profile.engine != "doris":
        raise HTTPException(status_code=422, detail="请选择 Doris 类型的数据库连接。")
    return profile


async def _ftp_connection_from_body(
    db: AsyncSession,
    *,
    ftp_connection_id: uuid.UUID | None,
    ftp,
    directory: str | None = None,
):
    if ftp_connection_id:
        profile = await get_profile(db, ftp_connection_id)
        if profile.engine != "ftp":
            raise HTTPException(status_code=422, detail="请选择 FTP 类型的数据连接。")
        return profile_to_ftp_connection(profile, directory=directory)
    if ftp:
        if directory:
            ftp.directory = directory
        return ftp
    raise HTTPException(status_code=422, detail="请先选择 FTP 数据连接。")


async def _read_files(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if not files:
        raise HTTPException(status_code=422, detail="请至少上传一个 CSV 文件。")
    result: list[tuple[str, bytes]] = []
    for file in files:
        filename = file.filename or "upload.csv"
        if not filename.lower().endswith(".csv"):
            raise HTTPException(status_code=422, detail=f"{filename} 不是 CSV 文件。")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=422, detail=f"{filename} 是空文件。")
        result.append((filename, content))
    return result


def _merge_table_specs_json(
    table_specs_json: str | None,
    table_specs_json_parts: list[str] | None,
) -> str | None:
    parts = [part for part in table_specs_json_parts or [] if part]
    if parts:
        return "".join(parts)
    return table_specs_json


@router.post("/preview", response_model=DorisCsvPreviewResponse)
async def preview_doris_csv(
    connection_id: uuid.UUID = Form(...),
    database: str | None = Form(default=None),
    delimiter: str = Form(default=","),
    charset: str = Form(default="utf-8-sig"),
    has_header: bool = Form(default=True),
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        profile = await get_profile(db, connection_id)
        if profile.engine != "doris":
            raise HTTPException(status_code=422, detail="请选择 Doris 类型的数据库连接。")
        uploaded = await _read_files(files)
        return preview_csv_files(
            uploaded,
            database=(database or profile.database or "").strip() or None,
            delimiter=delimiter,
            charset=charset,
            has_header=has_header,
            profile=profile,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析 CSV 失败：{exc}") from exc


@router.post("/parse-tasks", response_model=DorisCsvParseTaskStatus)
async def create_doris_csv_parse_task(
    connection_id: uuid.UUID = Form(...),
    database: str | None = Form(default=None),
    delimiter: str = Form(default=","),
    charset: str = Form(default="utf-8-sig"),
    has_header: bool = Form(default=True),
    import_mode: DorisCsvImportMode = Form(default="multiple_tables"),
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        profile = await get_profile(db, connection_id)
        if profile.engine != "doris":
            raise HTTPException(status_code=422, detail="请选择 Doris 类型的数据库连接。")
        uploaded = await _read_files(files)
        task = create_csv_parse_task(
            profile,
            uploaded,
            database=(database or profile.database or "").strip() or None,
            delimiter=delimiter,
            charset=charset,
            has_header=has_header,
            import_mode=import_mode,
            source="local",
        )
        asyncio.create_task(asyncio.to_thread(run_csv_parse_task_sync, str(task.task_id)))
        return task
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris CSV 任务创建失败：{exc}") from exc


@router.get("/parse-tasks", response_model=DorisCsvParseTaskListResponse)
async def list_doris_csv_parse_tasks(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        return await asyncio.to_thread(list_csv_parse_tasks_sync, limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取 CSV 任务列表失败：{exc}") from exc


@router.get("/parse-tasks/{task_id}", response_model=DorisCsvParseTaskStatus)
async def get_doris_csv_parse_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        return await asyncio.to_thread(get_csv_parse_task_status_sync, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取 CSV 任务失败：{exc}") from exc


@router.post("/parse-tasks/{task_id}/stop", response_model=DorisCsvParseTaskStatus)
async def stop_doris_csv_parse_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:import")),
):
    try:
        return await asyncio.to_thread(request_stop_csv_parse_task_sync, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"停止 CSV 任务失败：{exc}") from exc


@router.patch("/parse-tasks/{task_id}/files/{file_id}", response_model=DorisCsvParseTaskStatus)
async def update_doris_csv_parse_task_file(
    task_id: uuid.UUID,
    file_id: uuid.UUID,
    body: DorisCsvFilePreviewUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:import")),
):
    try:
        return await asyncio.to_thread(update_csv_parse_file_preview_sync, task_id, file_id, body.preview)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"保存 CSV 文件映射失败：{exc}") from exc


@router.get("/parse-tasks/{task_id}/logs", response_model=DorisCsvTaskLogListResponse)
async def list_doris_csv_parse_task_logs(
    task_id: uuid.UUID,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        return await asyncio.to_thread(list_csv_task_logs_sync, task_id, limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取 CSV 任务日志失败：{exc}") from exc


@router.post("/parse-tasks/{task_id}/import", response_model=DorisCsvParseTaskStatus)
async def start_doris_csv_import_task(
    task_id: uuid.UUID,
    body: DorisCsvImportStartRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:import")),
):
    try:
        status = await asyncio.to_thread(
            request_import_csv_task_sync,
            task_id,
            create_table=body.create_table,
            overwrite=body.overwrite,
            database=body.database,
        )
        asyncio.create_task(asyncio.to_thread(run_csv_import_task_sync, str(task_id)))
        return status
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"启动 CSV 导入失败：{exc}") from exc


@router.post("/import", response_model=DorisCsvImportResponse)
async def import_doris_csv(
    connection_id: uuid.UUID = Form(...),
    database: str | None = Form(default=None),
    delimiter: str = Form(default=","),
    charset: str = Form(default="utf-8-sig"),
    has_header: bool = Form(default=True),
    import_mode: DorisCsvImportMode = Form(default="multiple_tables"),
    create_table: bool = Form(default=True),
    overwrite: bool = Form(default=False),
    table_specs_json: str | None = Form(default=None),
    table_specs_json_parts: list[str] | None = Form(default=None),
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:import")),
):
    try:
        profile = await get_profile(db, connection_id)
        if profile.engine != "doris":
            raise HTTPException(status_code=422, detail="请选择 Doris 类型的数据库连接。")
        uploaded = await _read_files(files)
        response = await import_csv_files(
            profile,
            uploaded,
            database=database,
            delimiter=delimiter,
            charset=charset,
            has_header=has_header,
            import_mode=import_mode,
            create_table=create_table,
            overwrite=overwrite,
            table_specs_json=_merge_table_specs_json(table_specs_json, table_specs_json_parts),
        )
        return response
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris CSV 导入失败：{exc}") from exc


@router.post("/ftp/list", response_model=DorisFtpCatalogResponse)
async def list_doris_csv_ftp_files(
    body: DorisFtpCatalogRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        ftp = await _ftp_connection_from_body(
            db,
            ftp_connection_id=body.ftp_connection_id,
            ftp=body.ftp,
            directory=body.directory,
        )
        return await asyncio.to_thread(list_ftp_directory, ftp)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"FTP 目录读取失败：{exc}") from exc


@router.post("/ftp/preview", response_model=DorisCsvPreviewResponse)
async def preview_doris_csv_from_ftp(
    body: DorisFtpCsvRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:read")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        ftp = await _ftp_connection_from_body(
            db,
            ftp_connection_id=body.ftp_connection_id,
            ftp=body.ftp,
            directory=body.ftp_directory,
        )
        files = await asyncio.to_thread(
            fetch_ftp_csv_files,
            ftp,
            filenames=body.filenames,
            include_all_csv=body.include_all_csv,
        )
        return preview_csv_files(
            files,
            database=(body.database or profile.database or "").strip() or None,
            delimiter=body.delimiter,
            charset=body.charset,
            has_header=body.has_header,
            profile=profile,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"FTP CSV 解析失败：{exc}") from exc


@router.post("/ftp/import", response_model=DorisCsvImportResponse)
async def import_doris_csv_from_ftp(
    body: DorisFtpCsvImportRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisCsv:import")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        ftp = await _ftp_connection_from_body(
            db,
            ftp_connection_id=body.ftp_connection_id,
            ftp=body.ftp,
            directory=body.ftp_directory,
        )
        files = await asyncio.to_thread(
            fetch_ftp_csv_files,
            ftp,
            filenames=body.filenames,
            include_all_csv=body.include_all_csv,
        )
        return await import_csv_files(
            profile,
            files,
            database=body.database,
            delimiter=body.delimiter,
            charset=body.charset,
            has_header=body.has_header,
            import_mode=body.import_mode,
            create_table=body.create_table,
            overwrite=body.overwrite,
            table_specs_json=body.table_specs_json,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"FTP Doris CSV 导入失败：{exc}") from exc
