from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


DEFAULT_DORIS_ENCRYPTION_KEYWORDS = ["证件号", "住址", "地址", "电话", "手机号", "姓名"]
DorisEncryptionTableMode = Literal["replace_original", "create_suffixed"]
DorisSm4TableStrategy = Literal["append_existing", "drop_recreate", "auto_create"]
DorisSm4ScheduleType = Literal["daily", "weekly", "monthly", "interval"]
DorisSm4ScheduleStatus = Literal["active", "paused", "archived", "deleted"]
DorisSm4JobState = Literal["queued", "reserved", "running", "stopping", "stopped", "cancelled", "succeeded", "failed", "partial"]


class DorisEncryptionCatalogRequest(BaseModel):
    connection_id: UUID
    database: str | None = None
    keywords: list[str] = Field(default_factory=lambda: DEFAULT_DORIS_ENCRYPTION_KEYWORDS.copy())


class DorisEncryptionColumn(BaseModel):
    name: str
    type: str | None = None
    ordinal_position: int = 0
    matched_keywords: list[str] = Field(default_factory=list)
    selected: bool = False


class DorisEncryptionTable(BaseModel):
    name: str
    columns: list[DorisEncryptionColumn] = Field(default_factory=list)
    selected_count: int = 0
    mask_role: str | None = None
    mask_status: str | None = None
    mask_algorithm: str | None = None
    mask_source_table: str | None = None
    mask_output_table: str | None = None
    mask_backup_table: str | None = None
    mask_task_id: UUID | None = None
    mask_updated_at: datetime | None = None


class DorisEncryptionCatalogResponse(BaseModel):
    database: str
    keywords: list[str] = Field(default_factory=list)
    tables: list[DorisEncryptionTable] = Field(default_factory=list)


class DorisEncryptionDatabaseListResponse(BaseModel):
    connection_id: UUID
    databases: list[str] = Field(default_factory=list)


class DorisEncryptionExecuteRequest(BaseModel):
    connection_id: UUID
    database: str
    table_name: str = Field(min_length=1)
    columns: list[str] = Field(default_factory=list)
    backup_suffix: str | None = None
    table_mode: DorisEncryptionTableMode = "replace_original"


class DorisEncryptionTaskStep(BaseModel):
    title: str
    state: Literal["pending", "running", "success", "failed"] = "pending"
    message: str | None = None
    sql: str | None = None


class DorisEncryptionTaskStatus(BaseModel):
    task_id: UUID
    state: Literal["running", "succeeded", "failed"]
    message: str
    database: str
    table_name: str
    table_mode: DorisEncryptionTableMode = "replace_original"
    backup_table_name: str | None = None
    output_table_name: str | None = None
    encrypted_columns: list[str] = Field(default_factory=list)
    source_rows: int | None = None
    target_rows: int | None = None
    steps: list[DorisEncryptionTaskStep] = Field(default_factory=list)
    created_at: datetime
    finished_at: datetime | None = None


class DorisEncryptionExecuteResponse(BaseModel):
    task_id: UUID
    state: Literal["running"]
    message: str


class DorisSm4BatchTableSpec(BaseModel):
    table_name: str = Field(min_length=1)
    columns: list[str] = Field(default_factory=list)
    target_database: str | None = None
    target_table: str | None = None


class DorisSm4BatchRequest(BaseModel):
    connection_id: UUID
    database: str
    tables: list[DorisSm4BatchTableSpec] = Field(default_factory=list)
    table_strategy: DorisSm4TableStrategy = "drop_recreate"
    target_suffix: str | None = None
    schedule_id: UUID | None = None
    key_id: UUID | None = None
    execution_window_enabled: bool = False
    execution_window_start: str | None = None
    execution_window_end: str | None = None
    allow_running_cross_window: bool = True


class DorisSm4AutoSnapshotRequest(BaseModel):
    name: str | None = None
    connection_id: UUID
    include_databases: list[str] = Field(default_factory=list)
    exclude_databases: list[str] = Field(default_factory=list)
    exclude_tables: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=lambda: DEFAULT_DORIS_ENCRYPTION_KEYWORDS.copy())
    table_strategy: DorisSm4TableStrategy = "drop_recreate"
    target_suffix: str | None = "sm4"
    key_id: UUID | None = None
    execution_window_enabled: bool = True
    execution_window_start: str = "22:00"
    execution_window_end: str = "09:00"
    allow_running_cross_window: bool = True
    scan_interval_minutes: int = Field(default=60, ge=1, le=1440)


class DorisSm4BatchExecuteResponse(BaseModel):
    batch_id: UUID
    state: Literal["queued", "reserved", "running"] = "queued"
    message: str


class DorisSm4BatchTableResult(BaseModel):
    table_name: str
    target_database: str | None = None
    target_table: str | None = None
    columns: list[str] = Field(default_factory=list)
    state: Literal["queued", "running", "stopping", "stopped", "cancelled", "succeeded", "failed"] = "queued"
    message: str | None = None
    source_rows: int | None = None
    target_rows: int | None = None
    db_session_id: str | None = None
    stop_requested_at: datetime | None = None
    stop_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DorisSm4BatchStatus(BaseModel):
    batch_id: UUID
    schedule_id: UUID | None = None
    connection_id: UUID
    connection_name: str | None = None
    database: str
    sm4_key_version_id: UUID | None = None
    sm4_key_fingerprint: str | None = None
    table_strategy: DorisSm4TableStrategy = "drop_recreate"
    target_suffix: str | None = None
    execution_window_enabled: bool = False
    execution_window_start: str | None = None
    execution_window_end: str | None = None
    allow_running_cross_window: bool = True
    auto_snapshot: bool = False
    auto_snapshot_config: dict | None = None
    state: DorisSm4JobState
    message: str
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    tables: list[DorisSm4BatchTableSpec] = Field(default_factory=list)
    results: list[DorisSm4BatchTableResult] = Field(default_factory=list)
    created_by_username: str | None = None
    created_by_auth_type: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class DorisSm4BatchListResponse(BaseModel):
    batches: list[DorisSm4BatchStatus] = Field(default_factory=list)


class DorisSm4AutoSnapshotResponse(BaseModel):
    task_id: UUID | None = None
    batch_ids: list[UUID] = Field(default_factory=list)
    batches: list[DorisSm4BatchStatus] = Field(default_factory=list)
    database_count: int = 0
    table_count: int = 0
    changed_table_count: int = 0
    message: str


class DorisSm4AutoSnapshotTaskStatus(BaseModel):
    task_id: UUID
    name: str
    connection_id: UUID
    connection_name: str | None = None
    include_databases: list[str] = Field(default_factory=list)
    exclude_databases: list[str] = Field(default_factory=list)
    exclude_tables: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    target_suffix: str | None = None
    execution_window_enabled: bool = True
    execution_window_start: str | None = None
    execution_window_end: str | None = None
    allow_running_cross_window: bool = True
    scan_interval_minutes: int = 60
    database_count: int = 0
    table_count: int = 0
    last_scan_at: datetime | None = None
    next_scan_at: datetime | None = None
    last_change_at: datetime | None = None
    enabled: bool = True
    state: str = "active"
    message: str = ""
    created_at: datetime
    updated_at: datetime | None = None


class DorisSm4AutoSnapshotTaskListResponse(BaseModel):
    tasks: list[DorisSm4AutoSnapshotTaskStatus] = Field(default_factory=list)


class DorisSm4AutoSnapshotTaskUpdateRequest(BaseModel):
    scan_interval_minutes: int = Field(ge=1, le=1440)


class DorisSm4TaskLogEntry(BaseModel):
    id: int
    task_id: UUID
    level: str
    stage: str | None = None
    message: str = ""
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


class DorisSm4TaskLogResponse(BaseModel):
    batch_id: UUID
    logs: list[DorisSm4TaskLogEntry] = Field(default_factory=list)


class DorisSm4ScheduleBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    connection_id: UUID
    database: str
    tables: list[DorisSm4BatchTableSpec] = Field(default_factory=list)
    table_strategy: DorisSm4TableStrategy = "drop_recreate"
    target_suffix: str | None = None
    schedule_type: DorisSm4ScheduleType = "monthly"
    run_time: str = Field(default="02:00", max_length=16)
    day_of_month: int | None = Field(default=1, ge=1, le=31)
    day_of_week: int | None = Field(default=1, ge=1, le=7)
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)
    enabled: bool = True


class DorisSm4ScheduleCreateRequest(DorisSm4ScheduleBase):
    pass


class DorisSm4ScheduleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection_id: UUID | None = None
    database: str | None = None
    tables: list[DorisSm4BatchTableSpec] | None = None
    table_strategy: DorisSm4TableStrategy | None = None
    target_suffix: str | None = None
    schedule_type: DorisSm4ScheduleType | None = None
    run_time: str | None = Field(default=None, max_length=16)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    day_of_week: int | None = Field(default=None, ge=1, le=7)
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)
    enabled: bool | None = None


class DorisSm4ScheduleLifecycleRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class DorisSm4ScheduleResponse(DorisSm4ScheduleBase):
    schedule_id: UUID
    status: DorisSm4ScheduleStatus = "active"
    connection_name: str | None = None
    created_by_username: str | None = None
    created_by_auth_type: str | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    archived_at: datetime | None = None
    archived_by_username: str | None = None
    deleted_at: datetime | None = None
    deleted_by_username: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DorisSm4ScheduleListResponse(BaseModel):
    schedules: list[DorisSm4ScheduleResponse] = Field(default_factory=list)


class DorisSm4TaskDefinitionBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    connection_id: UUID
    database: str
    tables: list[DorisSm4BatchTableSpec] = Field(default_factory=list)
    table_strategy: DorisSm4TableStrategy = "drop_recreate"
    target_suffix: str | None = None


class DorisSm4TaskDefinitionCreateRequest(DorisSm4TaskDefinitionBase):
    pass


class DorisSm4TaskDefinitionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection_id: UUID | None = None
    database: str | None = None
    tables: list[DorisSm4BatchTableSpec] | None = None
    table_strategy: DorisSm4TableStrategy | None = None
    target_suffix: str | None = None


class DorisSm4TaskDefinitionResponse(DorisSm4TaskDefinitionBase):
    task_id: UUID
    revision: int = 1
    connection_name: str | None = None
    created_by_username: str | None = None
    created_by_auth_type: str | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DorisSm4TaskDefinitionListResponse(BaseModel):
    tasks: list[DorisSm4TaskDefinitionResponse] = Field(default_factory=list)


class DorisSm4TaskReference(BaseModel):
    workflow_id: UUID
    workflow_name: str
    version_id: UUID
    version_no: int
    channel: str
    status: str
    schedule_enabled: bool = False
    next_run_at: datetime | None = None
    node_count: int = 1
    frozen_revision: int | None = None


class DorisSm4TaskReferenceResponse(BaseModel):
    task_id: UUID
    task_name: str
    revision: int
    development_count: int = 0
    production_count: int = 0
    online_count: int = 0
    references: list[DorisSm4TaskReference] = Field(default_factory=list)


DorisSm4KeyMode = Literal["random", "manual"]
DorisSm4FunctionKeySource = Literal["random", "manual", "existing"]


class DorisSm4KeyVersionResponse(BaseModel):
    key_id: UUID
    connection_id: UUID | None = None
    connection_name: str | None = None
    name: str
    key_fingerprint: str
    key_mode: DorisSm4KeyMode
    function_name: str | None = None
    decrypt_function_name: str | None = None
    jar_filename: str | None = None
    status: str = "active"
    created_by_username: str | None = None
    created_by_auth_type: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DorisSm4KeyVersionListResponse(BaseModel):
    keys: list[DorisSm4KeyVersionResponse] = Field(default_factory=list)


class DorisSm4FunctionDatabaseCapability(BaseModel):
    database: str = Field(min_length=1, max_length=128)
    encrypt_enabled: bool = False
    decrypt_enabled: bool = False


class DorisSm4FunctionRefreshRequest(BaseModel):
    connection_id: UUID
    key_mode: DorisSm4FunctionKeySource = "random"
    key_id: UUID | None = None
    sm4_key: str | None = Field(default=None, min_length=1, max_length=128)
    function_name: str = Field(default="CQ_SM4_ENCRYPT", min_length=1, max_length=64)
    include_system_databases: bool = False
    databases: list[str] = Field(default_factory=list)
    database_capabilities: list[DorisSm4FunctionDatabaseCapability] = Field(default_factory=list)


class DorisSm4FunctionDatabaseResult(BaseModel):
    database: str
    encrypt_enabled: bool = True
    decrypt_enabled: bool = True
    state: Literal["success", "failed", "skipped"]
    message: str
    drop_sql: str | None = None
    create_sql: str | None = None
    verification_state: Literal["success", "failed", "skipped"] | None = None
    verification_message: str | None = None
    verification_sql: str | None = None
    attempted_at: datetime | None = None


class DorisSm4FunctionDeploymentResponse(BaseModel):
    connection_id: UUID
    connection_name: str | None = None
    database: str
    function_name: str
    decrypt_function_name: str | None = None
    key_version_id: UUID | None = None
    key_fingerprint: str | None = None
    jar_filename: str | None = None
    encrypt_enabled: bool = True
    decrypt_enabled: bool = True
    state: Literal["success", "failed"]
    message: str = ""
    verification_state: Literal["success", "failed", "skipped"] | None = None
    verification_message: str | None = None
    attempted_at: datetime
    last_success_at: datetime | None = None


class DorisSm4FunctionDeploymentListResponse(BaseModel):
    connection_id: UUID
    deployments: list[DorisSm4FunctionDeploymentResponse] = Field(default_factory=list)


class DorisSm4FunctionRefreshResponse(BaseModel):
    state: Literal["success", "partial", "failed"]
    message: str
    function_name: str
    decrypt_function_name: str | None = None
    key_id: UUID | None = None
    key_fingerprint: str
    jar_filename: str
    jar_url: str
    total_databases: int = 0
    success_count: int = 0
    failed_count: int = 0
    results: list[DorisSm4FunctionDatabaseResult] = Field(default_factory=list)
