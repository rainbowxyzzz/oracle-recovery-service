import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.doris_sql_etl import (
    DorisSqlDdlResponse,
    DorisSqlEtlRunListResponse,
    DorisSqlEtlRunStatus,
    DorisSqlEtlRunSubmitResponse,
    DorisSqlEtlTaskCreateRequest,
    DorisSqlEtlTaskListResponse,
    DorisSqlExecuteRequest,
    DorisSqlExecuteResponse,
    DorisSqlObjectListResponse,
    DorisSqlObjectRequest,
    QueryExportCreateRequest,
    QueryExportListResponse,
    QueryExportStatus,
    SqlPreviewRequest,
    SqlPreviewResponse,
)
from recovery_service.services.auth import AuthContext
from recovery_service.services.database_connections import get_profile
from recovery_service.services.doris_sql_etl import (
    create_doris_sql_etl_task,
    delete_doris_sql_etl_task,
    execute_doris_sql,
    get_doris_table_ddl,
    get_doris_sql_etl_run,
    list_doris_catalogs,
    list_doris_columns,
    list_doris_databases,
    list_doris_sql_etl_runs,
    list_doris_sql_etl_tasks,
    list_doris_tables,
    preview_oracle_query,
    preview_doris_table,
    submit_doris_sql_etl_run,
)
from recovery_service.services.query_export import cancel_query_export_job, create_query_export_job, list_query_export_jobs, record_query_export_download

router = APIRouter(prefix="/doris-sql-etl", tags=["doris-sql-etl"])


@router.get("/doris/catalogs", response_model=DorisSqlObjectListResponse)
async def get_doris_catalogs(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(list_doris_catalogs, profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris Catalog 读取失败：{exc}") from exc


@router.get("/doris/databases", response_model=DorisSqlObjectListResponse)
async def get_doris_databases(
    connection_id: uuid.UUID,
    catalog: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(list_doris_databases, profile, catalog=catalog)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 数据库读取失败：{exc}") from exc


@router.get("/doris/tables", response_model=DorisSqlObjectListResponse)
async def get_doris_tables(
    connection_id: uuid.UUID,
    database: str,
    catalog: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(list_doris_tables, profile, catalog=catalog, database=database)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 表读取失败：{exc}") from exc


@router.get("/doris/columns", response_model=DorisSqlObjectListResponse)
async def get_doris_columns(
    connection_id: uuid.UUID,
    database: str,
    table: str,
    catalog: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(list_doris_columns, profile, catalog=catalog, database=database, table=table)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 字段读取失败：{exc}") from exc


@router.post("/doris/table-ddl", response_model=DorisSqlDdlResponse)
async def get_doris_ddl(
    body: DorisSqlObjectRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        if not body.database or not body.table:
            raise ValueError("请选择数据库和表。")
        profile = await get_profile(db, body.connection_id)
        return await asyncio.to_thread(get_doris_table_ddl, profile, catalog=body.catalog, database=body.database, table=body.table)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris DDL 读取失败：{exc}") from exc


@router.post("/doris/table-preview", response_model=DorisSqlExecuteResponse)
async def preview_doris_table_rows(
    body: DorisSqlObjectRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        if not body.database or not body.table:
            raise ValueError("请选择数据库和表。")
        profile = await get_profile(db, body.connection_id)
        return await asyncio.to_thread(preview_doris_table, profile, catalog=body.catalog, database=body.database, table=body.table, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 表数据预览失败：{exc}") from exc


@router.post("/oracle/preview", response_model=SqlPreviewResponse)
async def preview_oracle_sql(
    body: SqlPreviewRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        profile = await get_profile(db, body.connection_id)
        return await asyncio.to_thread(preview_oracle_query, profile, sql=body.sql, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Oracle 查询预览失败：{exc}") from exc


@router.post("/doris/execute", response_model=DorisSqlExecuteResponse)
async def run_doris_sql(
    body: DorisSqlExecuteRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:execute")),
):
    try:
        profile = await get_profile(db, body.connection_id)
        return await asyncio.to_thread(
            execute_doris_sql,
            profile,
            database=body.database,
            sql=body.sql,
            limit=body.limit,
            confirm_dangerous=body.confirm_dangerous,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SQL 执行失败：{exc}") from exc


@router.post("/exports", response_model=QueryExportStatus)
async def create_query_export(
    body: QueryExportCreateRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("queryExport:execute")),
):
    try:
        profile = await get_profile(db, body.connection_id)
        return await asyncio.to_thread(
            create_query_export_job, profile=profile, database=body.database, sql=body.sql,
            export_format=body.export_format, encoding=body.encoding, resource_profile=body.resource_profile, actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"查询导出任务提交失败：{exc}") from exc


@router.get("/exports", response_model=QueryExportListResponse)
async def list_query_exports(
    limit: int = Query(default=100, ge=1, le=200),
    actor: AuthContext = Depends(require_permission("queryExport:read")),
):
    return QueryExportListResponse(jobs=await asyncio.to_thread(list_query_export_jobs, actor=actor, limit=limit))


@router.post("/exports/{job_id}/cancel", response_model=QueryExportStatus)
async def cancel_query_export(
    job_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("queryExport:execute")),
):
    try:
        return await asyncio.to_thread(cancel_query_export_job, job_id, actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/exports/{job_id}/download")
async def download_query_export(
    job_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("queryExport:download")),
):
    try:
        job = await asyncio.to_thread(record_query_export_download, job_id, actor=actor)
        return FileResponse(path=job.file_path, filename=job.file_name, media_type="application/octet-stream")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks", response_model=DorisSqlEtlTaskListResponse)
async def create_etl_task(
    body: DorisSqlEtlTaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisSqlEtl:manage")),
):
    try:
        source = await get_profile(db, body.source_connection_id)
        target = await get_profile(db, body.target_connection_id)
        task = await asyncio.to_thread(
            create_doris_sql_etl_task,
            name=body.name,
            description=body.description,
            source_profile=source,
            target_profile=target,
            source_sql=body.source_sql,
            target_database=body.target_database,
            target_table=body.target_table,
            write_mode=body.write_mode,
            batch_size=body.batch_size,
            column_mapping=[item.model_dump() for item in body.column_mapping],
            actor=actor,
        )
        return DorisSqlEtlTaskListResponse(tasks=[task])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"ETL 任务创建失败：{exc}") from exc


@router.get("/tasks", response_model=DorisSqlEtlTaskListResponse)
async def list_etl_tasks(
    limit: int = Query(default=100, ge=1, le=200),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    return DorisSqlEtlTaskListResponse(tasks=await asyncio.to_thread(list_doris_sql_etl_tasks, limit=limit))


@router.delete("/tasks/{task_id}", response_model=DorisSqlEtlTaskListResponse)
async def delete_etl_task(
    task_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dorisSqlEtl:manage")),
):
    try:
        task = await asyncio.to_thread(delete_doris_sql_etl_task, task_id)
        return DorisSqlEtlTaskListResponse(tasks=[task])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/run", response_model=DorisSqlEtlRunSubmitResponse)
async def run_etl_task(
    task_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("dorisSqlEtl:execute")),
):
    try:
        run = await asyncio.to_thread(submit_doris_sql_etl_run, task_id, actor)
        return DorisSqlEtlRunSubmitResponse(run_id=run.run_id, state="queued", message=run.message)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"ETL 任务提交失败：{exc}") from exc


@router.get("/runs", response_model=DorisSqlEtlRunListResponse)
async def list_etl_runs(
    limit: int = Query(default=100, ge=1, le=200),
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    return DorisSqlEtlRunListResponse(runs=await asyncio.to_thread(list_doris_sql_etl_runs, limit=limit))


@router.get("/runs/{run_id}", response_model=DorisSqlEtlRunStatus)
async def get_etl_run(
    run_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dorisSqlEtl:read")),
):
    try:
        return await asyncio.to_thread(get_doris_sql_etl_run, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
