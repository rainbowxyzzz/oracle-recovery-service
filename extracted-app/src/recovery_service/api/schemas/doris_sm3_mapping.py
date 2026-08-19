from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


DEFAULT_DORIS_SM3_KEYWORDS = ["证件号", "住址", "地址", "电话", "手机号", "姓名"]
DorisSm3TableMode = Literal["replace_original", "create_suffixed"]
DorisSm3JobState = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]


class DorisSm3CatalogRequest(BaseModel):
    connection_id: UUID
    database: str | None = None
    keywords: list[str] = Field(default_factory=lambda: DEFAULT_DORIS_SM3_KEYWORDS.copy())


class DorisSm3Column(BaseModel):
    name: str
    type: str | None = None
    ordinal_position: int = 0
    matched_keywords: list[str] = Field(default_factory=list)
    selected: bool = False
    default_mapping_table: str | None = None


class DorisSm3ColumnAudit(BaseModel):
    column_name: str
    mapping_table: str | None = None


class DorisSm3Table(BaseModel):
    name: str
    columns: list[DorisSm3Column] = Field(default_factory=list)
    selected_count: int = 0
    last_sm3_at: datetime | None = None
    last_sm3_output_table: str | None = None
    last_sm3_columns: list[str] = Field(default_factory=list)
    mask_role: str | None = None
    mask_status: str | None = None
    mask_algorithm: str | None = None
    mask_source_table: str | None = None
    mask_output_table: str | None = None
    mask_backup_table: str | None = None
    mask_task_id: UUID | None = None
    mask_updated_at: datetime | None = None


class DorisSm3CatalogResponse(BaseModel):
    database: str
    keywords: list[str] = Field(default_factory=list)
    tables: list[DorisSm3Table] = Field(default_factory=list)


class DorisSm3ExecuteRequest(BaseModel):
    connection_id: UUID
    database: str
    table_name: str = Field(min_length=1)
    columns: list[str] = Field(default_factory=list)
    mapping_database: str | None = None
    mapping_tables: dict[str, str] = Field(default_factory=dict)
    field_mapping_database: str | None = None
    field_mapping_table: str | None = None
    output_suffix: str | None = None
    table_mode: DorisSm3TableMode = "create_suffixed"


class DorisSm3TaskStep(BaseModel):
    title: str
    state: Literal["pending", "running", "success", "failed"] = "pending"
    message: str | None = None
    sql: str | None = None


class DorisSm3TaskStatus(BaseModel):
    task_id: UUID
    state: DorisSm3JobState
    message: str
    database: str
    table_name: str
    table_mode: DorisSm3TableMode = "create_suffixed"
    backup_table_name: str | None = None
    output_table_name: str | None = None
    hashed_columns: list[str] = Field(default_factory=list)
    mapping_database: str | None = None
    mapping_tables: dict[str, str] = Field(default_factory=dict)
    field_mapping_database: str | None = None
    field_mapping_table: str | None = None
    source_rows: int | None = None
    target_rows: int | None = None
    steps: list[DorisSm3TaskStep] = Field(default_factory=list)
    created_at: datetime
    finished_at: datetime | None = None


class DorisSm3ExecuteResponse(BaseModel):
    task_id: UUID
    state: Literal["queued", "running"] = "queued"
    message: str


class DorisSm3JobResponse(BaseModel):
    task_id: UUID
    celery_task_id: str | None = None
    connection_id: UUID
    connection_name: str | None = None
    database: str
    table_name: str
    table_mode: DorisSm3TableMode = "create_suffixed"
    backup_table_name: str | None = None
    output_table_name: str | None = None
    hashed_columns: list[str] = Field(default_factory=list)
    mapping_database: str | None = None
    mapping_tables: dict[str, str] = Field(default_factory=dict)
    field_mapping_database: str | None = None
    field_mapping_table: str | None = None
    created_by_user_id: UUID | None = None
    created_by_username: str | None = None
    created_by_auth_type: str | None = None
    state: DorisSm3JobState
    message: str
    current_step: str | None = None
    source_rows: int | None = None
    target_rows: int | None = None
    cancel_requested: bool = False
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    waiting_seconds: int | None = None
    running_seconds: int | None = None


class DorisSm3JobListResponse(BaseModel):
    tasks: list[DorisSm3JobResponse] = Field(default_factory=list)


class DorisSm3TaskDefinitionBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    connection_id: UUID
    database: str
    table_name: str = Field(min_length=1)
    columns: list[str] = Field(default_factory=list)
    mapping_database: str | None = None
    mapping_tables: dict[str, str] = Field(default_factory=dict)
    field_mapping_database: str | None = None
    field_mapping_table: str | None = None
    output_suffix: str | None = None
    table_mode: DorisSm3TableMode = "create_suffixed"


class DorisSm3TaskDefinitionCreateRequest(DorisSm3TaskDefinitionBase):
    pass


class DorisSm3TaskDefinitionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection_id: UUID | None = None
    database: str | None = None
    table_name: str | None = Field(default=None, min_length=1)
    columns: list[str] | None = None
    mapping_database: str | None = None
    mapping_tables: dict[str, str] | None = None
    field_mapping_database: str | None = None
    field_mapping_table: str | None = None
    output_suffix: str | None = None
    table_mode: DorisSm3TableMode | None = None


class DorisSm3TaskDefinitionResponse(DorisSm3TaskDefinitionBase):
    task_id: UUID
    revision: int = 1
    connection_name: str | None = None
    created_by_username: str | None = None
    created_by_auth_type: str | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DorisSm3TaskDefinitionListResponse(BaseModel):
    tasks: list[DorisSm3TaskDefinitionResponse] = Field(default_factory=list)


class DorisSm3QueueTask(BaseModel):
    celery_task_id: str | None = None
    job_id: UUID | None = None
    name: str | None = None
    worker: str | None = None
    state: str | None = None
    database: str | None = None
    table_name: str | None = None
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    waiting_seconds: int | None = None
    running_seconds: int | None = None


class DorisSm3QueueStatusResponse(BaseModel):
    broker: str = "redis"
    broker_url: str
    result_backend: str
    queue_name: str
    redis_host: str | None = None
    redis_port: int | None = None
    redis_db: int | None = None
    pending_count: int = 0
    active_worker_count: int = 0
    configured_concurrency: int = 1
    prefetch_multiplier: int = 1
    active_count: int = 0
    reserved_count: int = 0
    scheduled_count: int = 0
    active_tasks: list[DorisSm3QueueTask] = Field(default_factory=list)
    reserved_tasks: list[DorisSm3QueueTask] = Field(default_factory=list)
    scheduled_tasks: list[DorisSm3QueueTask] = Field(default_factory=list)
    running_jobs: list[DorisSm3QueueTask] = Field(default_factory=list)
    queued_jobs: list[DorisSm3QueueTask] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DorisSm3TaskLogEntry(BaseModel):
    id: int
    task_id: UUID
    level: str
    stage: str | None = None
    message: str
    sql_type: str | None = None
    sql_text: str | None = None
    database_engine: str = "doris"
    connection_id: UUID | None = None
    database_name: str | None = None
    table_name: str | None = None
    db_session_id: str | None = None
    duration_ms: int | None = None
    affected_rows: int | None = None
    error_message: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class DorisSm3TaskLogResponse(BaseModel):
    task_id: UUID
    logs: list[DorisSm3TaskLogEntry] = Field(default_factory=list)
