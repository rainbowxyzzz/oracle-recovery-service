from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


DorisSqlEtlWriteMode = Literal["append", "truncate_insert", "drop_create_insert", "create_if_not_exists_insert"]
DorisSqlEtlRunState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class SqlPreviewRequest(BaseModel):
    connection_id: UUID
    sql: str = Field(min_length=1)
    limit: int = Field(default=100, ge=1, le=1000)


class SqlColumn(BaseModel):
    name: str
    type: str | None = None


class SqlPreviewResponse(BaseModel):
    columns: list[SqlColumn] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    message: str = ""


class DorisSqlExecuteRequest(BaseModel):
    connection_id: UUID
    database: str | None = None
    sql: str = Field(min_length=1)
    limit: int = Field(default=200, ge=1, le=1000)
    confirm_dangerous: bool = False


class DorisSqlObjectRequest(BaseModel):
    connection_id: UUID
    catalog: str | None = None
    database: str | None = None
    table: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class DorisSqlExecuteResponse(BaseModel):
    sql_type: str
    columns: list[SqlColumn] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    affected_rows: int | None = None
    duration_ms: int = 0
    message: str = ""


class DorisSqlObjectItem(BaseModel):
    name: str
    type: str | None = None
    comment: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class DorisSqlObjectListResponse(BaseModel):
    items: list[DorisSqlObjectItem] = Field(default_factory=list)
    message: str = ""


class DorisSqlDdlResponse(BaseModel):
    ddl: str = ""
    message: str = ""


class DorisSqlEtlColumnMapping(BaseModel):
    source_name: str
    target_name: str
    target_type: str = "VARCHAR(2000)"
    enabled: bool = True


class DorisSqlEtlTaskCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    source_connection_id: UUID
    target_connection_id: UUID
    source_sql: str = Field(min_length=1)
    target_database: str = Field(min_length=1, max_length=255)
    target_table: str = Field(min_length=1, max_length=255)
    write_mode: DorisSqlEtlWriteMode = "truncate_insert"
    batch_size: int = Field(default=1000, ge=1, le=20000)
    column_mapping: list[DorisSqlEtlColumnMapping] = Field(default_factory=list)


class DorisSqlEtlTaskStatus(BaseModel):
    task_id: UUID
    name: str
    description: str | None = None
    source_connection_id: UUID
    source_connection_name: str | None = None
    target_connection_id: UUID
    target_connection_name: str | None = None
    target_database: str
    target_table: str
    write_mode: DorisSqlEtlWriteMode = "truncate_insert"
    batch_size: int = 1000
    column_mapping: list[DorisSqlEtlColumnMapping] = Field(default_factory=list)
    state: str = "active"
    created_by_username: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DorisSqlEtlTaskListResponse(BaseModel):
    tasks: list[DorisSqlEtlTaskStatus] = Field(default_factory=list)


class DorisSqlEtlRunSubmitResponse(BaseModel):
    run_id: UUID
    state: DorisSqlEtlRunState = "queued"
    message: str


class DorisSqlEtlRunStatus(BaseModel):
    run_id: UUID
    task_id: UUID | None = None
    task_name: str | None = None
    state: DorisSqlEtlRunState
    message: str = ""
    target_database: str | None = None
    target_table: str | None = None
    write_mode: str | None = None
    source_rows: int = 0
    target_rows: int = 0
    batch_count: int = 0
    logs: list[dict[str, Any]] = Field(default_factory=list)
    error_message: str | None = None
    created_by_username: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


class DorisSqlEtlRunListResponse(BaseModel):
    runs: list[DorisSqlEtlRunStatus] = Field(default_factory=list)
