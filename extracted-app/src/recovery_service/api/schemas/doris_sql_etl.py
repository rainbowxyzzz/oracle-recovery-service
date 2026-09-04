from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


DorisSqlEtlWriteMode = Literal["append", "truncate_insert", "drop_create_insert", "create_if_not_exists_insert"]
DorisSqlEtlRunState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
QueryExportFormat = Literal["csv", "tsv", "jsonl", "xlsx", "parquet"]
QueryExportState = Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
QueryExportProfile = Literal["streaming", "columnar"]


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


class DorisSqlCollectionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    business_domain: str | None = Field(default=None, max_length=128)
    data_layer: str | None = Field(default=None, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=20)
    default_connection_id: UUID | None = None
    default_database: str | None = Field(default=None, max_length=255)
    task_ids: list[UUID] = Field(default_factory=list, max_length=200)


class DorisSqlCollectionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    business_domain: str | None = Field(default=None, max_length=128)
    data_layer: str | None = Field(default=None, max_length=32)
    tags: list[str] | None = Field(default=None, max_length=20)
    default_connection_id: UUID | None = None
    default_database: str | None = Field(default=None, max_length=255)
    task_ids: list[UUID] | None = Field(default=None, max_length=200)


class DorisSqlCollectionMember(BaseModel):
    task_id: UUID
    name: str
    revision: int = 1
    description: str | None = None
    connection_id: UUID | None = None
    connection_name: str | None = None
    database: str | None = None
    sql_summary: str = ""
    position: int = 0


class DorisSqlCollectionReference(BaseModel):
    reference_type: str
    reference_id: UUID | None = None
    name: str
    version_id: UUID | None = None
    current_online: bool = False


class DorisSqlCollectionRunSummary(BaseModel):
    run_id: UUID
    version_id: UUID
    version_no: int
    channel: str
    trigger_type: str
    status: str
    message: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class DorisSqlCollectionStatus(BaseModel):
    collection_id: UUID
    name: str
    description: str | None = None
    business_domain: str | None = None
    data_layer: str | None = None
    tags: list[str] = Field(default_factory=list)
    default_connection_id: UUID | None = None
    default_database: str | None = None
    members: list[DorisSqlCollectionMember] = Field(default_factory=list)
    member_count: int = 0
    latest_dev_version_id: UUID | None = None
    latest_dev_version_no: int | None = None
    latest_prod_version_id: UUID | None = None
    latest_prod_version_no: int | None = None
    online_version_id: UUID | None = None
    online_version_no: int | None = None
    schedule_enabled: bool = False
    next_run_at: datetime | None = None
    references: list[DorisSqlCollectionReference] = Field(default_factory=list)
    recent_runs: list[DorisSqlCollectionRunSummary] = Field(default_factory=list)
    created_by_username: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DorisSqlCollectionListResponse(BaseModel):
    collections: list[DorisSqlCollectionStatus] = Field(default_factory=list)
    ungrouped_task_ids: list[UUID] = Field(default_factory=list)
    ungrouped_count: int = 0


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


class QueryExportCreateRequest(BaseModel):
    connection_id: UUID
    database: str | None = Field(default=None, max_length=255)
    sql: str = Field(min_length=1)
    export_format: QueryExportFormat = "csv"
    encoding: Literal["utf-8", "utf-8-sig", "gbk"] = "utf-8-sig"
    resource_profile: QueryExportProfile = "streaming"


class QueryExportStatus(BaseModel):
    job_id: UUID
    connection_name: str | None = None
    database: str | None = None
    sql_summary: str = ""
    export_format: QueryExportFormat
    encoding: str | None = None
    resource_profile: QueryExportProfile = "streaming"
    state: QueryExportState
    message: str = ""
    row_count: int = 0
    byte_size: int = 0
    processed_rows: int = 0
    progress_percent: int | None = None
    current_stage: str = "queued"
    throughput_rows_per_second: float | None = None
    sha256: str | None = None
    file_name: str | None = None
    error_message: str | None = None
    created_by_username: str | None = None
    download_count: int = 0
    expires_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


class QueryExportListResponse(BaseModel):
    jobs: list[QueryExportStatus] = Field(default_factory=list)
