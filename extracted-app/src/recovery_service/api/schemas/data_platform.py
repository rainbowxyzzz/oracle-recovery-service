from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


DataPlatformNodeType = Literal["manual", "sm3_mapping", "sm4_batch", "doris_sql", "data_sync", "change_trigger"]
DataPlatformVersionChannel = Literal["dev", "prod"]
DataPlatformVersionStatus = Literal["draft", "submitted", "online", "offline"]
DataPlatformScheduleType = Literal["daily", "weekly", "monthly", "interval"]
DataPlatformRunStatus = Literal["queued", "running", "succeeded", "failed", "partial", "cancelled"]
DataPlatformNodeRunStatus = Literal["queued", "running", "succeeded", "failed", "skipped"]
DataPlatformComponentTableRunStatus = Literal["queued", "running", "succeeded", "failed", "skipped"]
DataPlatformScheduleState = Literal["waiting", "queued", "running", "disabled", "abnormal"]


class DataPlatformNodeCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    node_type: DataPlatformNodeType = "manual"
    description: str | None = None
    config: dict = Field(default_factory=dict)


class DataPlatformNodeUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    node_type: DataPlatformNodeType | None = None
    description: str | None = None
    config: dict | None = None
    status: Literal["active", "disabled"] | None = None


class DataPlatformNodeRunRequest(BaseModel):
    selected_tables: list[str] | None = None


class DataSyncRecognizeRequest(BaseModel):
    source_connection_id: UUID
    target_connection_id: UUID | None = None
    source_catalog: str = Field(min_length=1, max_length=128)
    source_schema: str = Field(min_length=1, max_length=255)
    target_database: str = Field(min_length=1, max_length=255)
    schema_policy: Literal["source", "target"] = "source"


class DataPlatformNodeResponse(BaseModel):
    node_id: UUID
    name: str
    revision: int = 1
    node_type: DataPlatformNodeType
    description: str | None = None
    config: dict = Field(default_factory=dict)
    status: str
    created_by_username: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DataPlatformFolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_id: UUID | None = None


class DataPlatformFolderUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: UUID | None = None
    status: Literal["active", "archived"] | None = None


class DataPlatformFolderResponse(BaseModel):
    folder_id: UUID
    name: str
    parent_id: UUID | None = None
    status: str
    created_by_username: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DataPlatformWorkflowCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    folder_id: UUID | None = None


class DataPlatformWorkflowUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    folder_id: UUID | None = None
    status: Literal["active", "archived"] | None = None


class DataPlatformWorkflowCopyRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    folder_id: UUID | None = None


class DataPlatformWorkflowResponse(BaseModel):
    workflow_id: UUID
    folder_id: UUID | None = None
    name: str
    description: str | None = None
    status: str
    latest_dev_version_id: UUID | None = None
    latest_prod_version_id: UUID | None = None
    online_version_id: UUID | None = None
    created_by_username: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DataPlatformWorkflowNodeSpec(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    node_id: UUID | None = None
    name: str | None = None
    node_type: DataPlatformNodeType = "manual"
    config: dict = Field(default_factory=dict)
    x: int | None = None
    y: int | None = None


class DataPlatformWorkflowEdgeSpec(BaseModel):
    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)


class DataPlatformVersionCreateRequest(BaseModel):
    channel: DataPlatformVersionChannel = "dev"
    nodes: list[DataPlatformWorkflowNodeSpec] = Field(default_factory=list)
    edges: list[DataPlatformWorkflowEdgeSpec] = Field(default_factory=list)
    schedule_enabled: bool = False
    schedule_type: DataPlatformScheduleType = "daily"
    run_time: str = Field(default="02:00", max_length=16)
    day_of_month: int | None = Field(default=1, ge=1, le=31)
    day_of_week: int | None = Field(default=1, ge=1, le=7)
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)


class DataPlatformVersionUpdateRequest(BaseModel):
    nodes: list[DataPlatformWorkflowNodeSpec] | None = None
    edges: list[DataPlatformWorkflowEdgeSpec] | None = None
    schedule_enabled: bool | None = None
    schedule_type: DataPlatformScheduleType | None = None
    run_time: str | None = Field(default=None, max_length=16)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    day_of_week: int | None = Field(default=None, ge=1, le=7)
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)


class DataPlatformVersionResponse(BaseModel):
    version_id: UUID
    workflow_id: UUID
    version_no: int
    channel: DataPlatformVersionChannel
    status: DataPlatformVersionStatus
    nodes: list[DataPlatformWorkflowNodeSpec] = Field(default_factory=list)
    edges: list[DataPlatformWorkflowEdgeSpec] = Field(default_factory=list)
    release_snapshot: dict | None = None
    execution_content_hash: str | None = None
    schedule_enabled: bool = False
    schedule_type: DataPlatformScheduleType = "daily"
    run_time: str = "02:00"
    day_of_month: int | None = None
    day_of_week: int | None = None
    interval_minutes: int | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    submitted_at: datetime | None = None
    published_at: datetime | None = None
    offline_at: datetime | None = None
    created_by_username: str | None = None
    updated_by_username: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DataPlatformScheduleResponse(BaseModel):
    version_id: UUID
    workflow_id: UUID
    workflow_name: str
    folder_id: UUID | None = None
    folder_path: str | None = None
    version_no: int
    version_status: DataPlatformVersionStatus
    schedule_enabled: bool
    schedule_state: DataPlatformScheduleState
    schedule_type: DataPlatformScheduleType
    run_time: str
    day_of_month: int | None = None
    day_of_week: int | None = None
    interval_minutes: int | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    latest_run_id: UUID | None = None
    latest_run_status: DataPlatformRunStatus | None = None
    latest_run_trigger_type: str | None = None
    latest_run_created_at: datetime | None = None
    latest_run_finished_at: datetime | None = None
    updated_by_username: str | None = None
    updated_at: datetime | None = None


class DataPlatformRunResponse(BaseModel):
    run_id: UUID
    workflow_id: UUID
    version_id: UUID
    version_no: int
    channel: DataPlatformVersionChannel
    trigger_type: str
    trigger_context: dict | None = None
    status: DataPlatformRunStatus
    message: str | None = None
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    created_by_username: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class DataPlatformNodeRunResponse(BaseModel):
    node_run_id: UUID
    run_id: UUID
    node_key: str
    node_name: str
    node_type: DataPlatformNodeType
    status: DataPlatformNodeRunStatus
    message: str | None = None
    upstream_keys: list[str] = Field(default_factory=list)
    result: dict = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class DataPlatformComponentRunResponse(BaseModel):
    component_run_id: UUID
    node_id: UUID
    node_type: DataPlatformNodeType
    node_name: str
    node_revision: int = 1
    trigger_type: str = "manual"
    selected_items: list[str] | None = None
    status: DataPlatformRunStatus
    message: str | None = None
    result: dict = Field(default_factory=dict)
    created_by_username: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None
    table_runs: list["DataPlatformComponentRunTableResponse"] = Field(default_factory=list)


class DataPlatformComponentRunTableResponse(BaseModel):
    table_run_id: UUID
    component_run_id: UUID
    node_id: UUID
    mapping_id: str | None = None
    source_catalog: str | None = None
    source_schema: str | None = None
    source_table: str | None = None
    target_database: str | None = None
    target_table: str | None = None
    sync_method: str | None = None
    write_mode: str | None = None
    schema_policy: str | None = None
    status: DataPlatformComponentTableRunStatus
    message: str | None = None
    loaded_rows: int = 0
    duration_ms: int = 0
    result_summary: dict = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class DataPlatformComponentRunLogResponse(BaseModel):
    log_id: UUID
    component_run_id: UUID
    table_run_id: UUID | None = None
    level: str
    stage: str | None = None
    message: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class DataPlatformComponentRunTableListResponse(BaseModel):
    tables: list[DataPlatformComponentRunTableResponse] = Field(default_factory=list)


class DataPlatformComponentRunLogListResponse(BaseModel):
    logs: list[DataPlatformComponentRunLogResponse] = Field(default_factory=list)


class DataPlatformDashboardResponse(BaseModel):
    node_count: int = 0
    workflow_count: int = 0
    online_version_count: int = 0
    running_count: int = 0
    failed_count: int = 0


class DataPlatformNodeListResponse(BaseModel):
    nodes: list[DataPlatformNodeResponse] = Field(default_factory=list)


class DataPlatformWorkflowListResponse(BaseModel):
    workflows: list[DataPlatformWorkflowResponse] = Field(default_factory=list)


class DataPlatformFolderListResponse(BaseModel):
    folders: list[DataPlatformFolderResponse] = Field(default_factory=list)


class DataPlatformVersionListResponse(BaseModel):
    versions: list[DataPlatformVersionResponse] = Field(default_factory=list)


class DataPlatformScheduleListResponse(BaseModel):
    schedules: list[DataPlatformScheduleResponse] = Field(default_factory=list)


class DataPlatformRunListResponse(BaseModel):
    runs: list[DataPlatformRunResponse] = Field(default_factory=list)


class DataPlatformNodeRunListResponse(BaseModel):
    nodes: list[DataPlatformNodeRunResponse] = Field(default_factory=list)


class DataPlatformComponentRunListResponse(BaseModel):
    runs: list[DataPlatformComponentRunResponse] = Field(default_factory=list)


class DataPlatformChangeTriggerResponse(BaseModel):
    trigger_id: UUID
    workflow_id: UUID
    version_id: UUID
    node_key: str
    node_name: str
    enabled: bool = True
    state: str
    config: dict = Field(default_factory=dict)
    observed_value: dict | None = None
    pending_value: dict | None = None
    pending_queue: list[dict] = Field(default_factory=list)
    applied_value: dict | None = None
    consecutive_matches: int = 0
    pending_run_id: UUID | None = None
    last_probe_at: datetime | None = None
    next_probe_at: datetime | None = None
    last_trigger_at: datetime | None = None
    last_success_at: datetime | None = None
    message: str = ""
    created_at: datetime
    updated_at: datetime | None = None


class DataPlatformChangeTriggerListResponse(BaseModel):
    triggers: list[DataPlatformChangeTriggerResponse] = Field(default_factory=list)


class DataPlatformChangeTriggerUpdateRequest(BaseModel):
    enabled: bool


class DataPlatformChangeTriggerBaselineRequest(BaseModel):
    confirm: bool = False


class DataPlatformChangeTriggerEnableRequest(BaseModel):
    enabled: bool


class DataPlatformChangeProbeResponse(BaseModel):
    probe_id: UUID
    trigger_id: UUID
    previous_value: dict | None = None
    current_value: dict | None = None
    condition_results: list[dict] = Field(default_factory=list)
    matched: bool = False
    status: str
    run_id: UUID | None = None
    message: str = ""
    created_at: datetime


class DataPlatformChangeProbeListResponse(BaseModel):
    probes: list[DataPlatformChangeProbeResponse] = Field(default_factory=list)
