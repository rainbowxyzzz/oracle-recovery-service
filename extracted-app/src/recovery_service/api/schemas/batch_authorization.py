from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class BatchAuthDepartmentUserResponse(BaseModel):
    id: UUID
    db_username: str
    db_user_identity: str
    display_name: str | None = None
    status: str

    model_config = {"from_attributes": True}


class BatchAuthDepartmentDatabaseResponse(BaseModel):
    id: UUID
    connection_id: UUID
    department_database: str
    default_privilege: str = "SELECT"
    status: str

    model_config = {"from_attributes": True}


class BatchAuthDepartmentResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    status: str
    users: list[BatchAuthDepartmentUserResponse] = Field(default_factory=list)
    databases: list[BatchAuthDepartmentDatabaseResponse] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BatchAuthPreviewIssue(BaseModel):
    row_no: int
    level: str = "error"
    message: str


class BatchAuthInitPreviewRow(BaseModel):
    row_no: int
    department_name: str
    db_username: str
    db_user_identity: str
    display_name: str | None = None
    department_database: str
    initial_password_provided: bool = False
    generated_password: bool = False
    valid: bool = True
    messages: list[str] = Field(default_factory=list)


class BatchAuthInitPreviewResponse(BaseModel):
    filename: str
    total_count: int
    valid_count: int
    invalid_count: int
    rows: list[BatchAuthInitPreviewRow] = Field(default_factory=list)
    issues: list[BatchAuthPreviewIssue] = Field(default_factory=list)


class BatchAuthInitImportRowResponse(BaseModel):
    id: int
    row_no: int
    department_name: str
    db_username: str
    db_user_identity: str
    display_name: str | None = None
    department_database: str
    generated_password: bool
    state: str
    message: str | None = None
    error_message: str | None = None

    model_config = {"from_attributes": True}


class BatchAuthInitImportBatchResponse(BaseModel):
    id: UUID
    connection_id: UUID
    connection_name: str | None = None
    filename: str
    state: str
    total_count: int
    success_count: int
    failed_count: int
    message: str | None = None
    created_by_username: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    rows: list[BatchAuthInitImportRowResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class BatchAuthGeneratedCredential(BaseModel):
    department_name: str
    db_username: str
    db_user_identity: str
    department_database: str
    initial_password: str


class BatchAuthInitExecuteResponse(BaseModel):
    batch: BatchAuthInitImportBatchResponse
    generated_credentials: list[BatchAuthGeneratedCredential] = Field(default_factory=list)


class BatchAuthGrantPreviewRow(BaseModel):
    row_no: int
    source_database: str
    source_table: str
    source_object_level: str | None = None
    target_database: str
    target_object: str
    valid: bool = True
    messages: list[str] = Field(default_factory=list)


class BatchAuthGrantPreviewResponse(BaseModel):
    filename: str
    department_id: UUID
    department_name: str
    department_database: str
    user_count: int
    total_count: int
    valid_count: int
    invalid_count: int
    rows: list[BatchAuthGrantPreviewRow] = Field(default_factory=list)
    issues: list[BatchAuthPreviewIssue] = Field(default_factory=list)


class BatchAuthGrantTableResponse(BaseModel):
    id: UUID
    source_database: str
    source_table: str
    source_object_level: str | None = None
    target_database: str
    target_object: str
    target_object_type: str
    publish_sql: str | None = None
    offline_sql: str | None = None
    state: str
    error_message: str | None = None
    published_at: datetime | None = None
    offlined_at: datetime | None = None

    model_config = {"from_attributes": True}


class BatchAuthGrantUserResponse(BaseModel):
    id: int
    table_id: UUID
    db_username: str
    db_user_identity: str
    privilege_type: str
    grant_sql: str | None = None
    revoke_sql: str | None = None
    grant_state: str
    revoke_state: str
    privilege_existed_before: bool = False
    granted_by_this_batch: bool = True
    revoke_decision: str | None = None
    revoke_decision_reason: str | None = None
    checked_before_grant_at: datetime | None = None
    checked_before_revoke_at: datetime | None = None
    error_message: str | None = None
    granted_at: datetime | None = None
    revoked_at: datetime | None = None

    model_config = {"from_attributes": True}


class BatchAuthGrantBatchResponse(BaseModel):
    id: UUID
    connection_id: UUID
    connection_name: str | None = None
    department_id: UUID
    department_name: str
    department_database: str
    name: str
    filename: str
    privilege_type: str
    starts_at: datetime
    expires_at: datetime
    state: str
    total_table_count: int
    success_table_count: int
    failed_table_count: int
    message: str | None = None
    created_by_username: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    offlined_at: datetime | None = None
    tables: list[BatchAuthGrantTableResponse] = Field(default_factory=list)
    users: list[BatchAuthGrantUserResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class BatchAuthExtendRequest(BaseModel):
    expires_at: datetime


class BatchAuthDiscoveryRequest(BaseModel):
    connection_id: UUID
    user_prefix: str = "cqssj_"
    database_prefix: str = "DWH_"


class BatchAuthDiscoveryRow(BaseModel):
    selected: bool = True
    department_name: str
    db_username: str
    db_user_identity: str
    display_name: str | None = None
    department_database: str
    database_candidates: list[str] = Field(default_factory=list)


class BatchAuthDiscoveryResponse(BaseModel):
    rows: list[BatchAuthDiscoveryRow] = Field(default_factory=list)
    available_databases: list[str] = Field(default_factory=list)


class BatchAuthApplyDiscoveryRequest(BaseModel):
    connection_id: UUID
    rows: list[BatchAuthDiscoveryRow] = Field(default_factory=list)
    default_password: str = "doris@2024"


class BatchAuthDepartmentRelationRequest(BaseModel):
    connection_id: UUID
    department_name: str
    db_username: str
    display_name: str | None = None
    department_database: str
    default_password: str = "doris@2024"
