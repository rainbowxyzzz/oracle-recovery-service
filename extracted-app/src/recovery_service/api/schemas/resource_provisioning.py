from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator, model_validator


class ResourceProvisioningPreviewRow(BaseModel):
    row_no: int
    person_name: str
    department_name: str
    mobile: str
    db_username: str
    database_name: str
    valid: bool = True
    issues: list[str] = Field(default_factory=list)


class ResourceProvisioningPreviewResponse(BaseModel):
    filename: str
    total_count: int
    valid_count: int
    invalid_count: int
    rows: list[ResourceProvisioningPreviewRow]


class ResourceProvisioningRowInput(BaseModel):
    row_no: int = Field(ge=1)
    person_name: str = Field(min_length=1, max_length=128)
    department_name: str = Field(min_length=1, max_length=128)
    mobile: str = Field(min_length=11, max_length=32)
    db_username: str = Field(min_length=1, max_length=64)
    database_name: str = Field(min_length=1, max_length=128)

    @field_validator("person_name", "department_name", "mobile", "db_username", "database_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ResourceProvisioningBatchCreateRequest(BaseModel):
    filename: str = Field(default="batch.xlsx", max_length=255)
    connection_id: uuid.UUID
    user_password: SecretStr
    api_url: HttpUrl
    youdata_login_name: str | None = Field(default=None, max_length=255)
    youdata_password: SecretStr | None = None
    api_token: SecretStr | None = None
    project_id: int = Field(gt=0)
    paths: list[str] = Field(min_length=1)
    server: str = Field(min_length=1, max_length=255)
    port: int = Field(default=9030, ge=1, le=65535)
    parallelism: int = Field(default=2, ge=1, le=10)
    rows: list[ResourceProvisioningRowInput] = Field(min_length=1)

    @field_validator("user_password")
    @classmethod
    def require_user_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Doris 用户初始密码不能为空。")
        return value

    @field_validator("youdata_login_name")
    @classmethod
    def normalize_youdata_login_name(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_youdata_auth(self) -> "ResourceProvisioningBatchCreateRequest":
        login_name = self.youdata_login_name or ""
        login_password = self.youdata_password.get_secret_value().strip() if self.youdata_password else ""
        legacy_token = self.api_token.get_secret_value().strip() if self.api_token else ""
        if bool(login_name) != bool(login_password):
            raise ValueError("有数登录账号和密码必须同时填写。")
        if login_name and legacy_token:
            raise ValueError("有数账号密码登录与历史手工 Token 不能同时使用。")
        if not login_name and not legacy_token:
            raise ValueError("请填写有数登录账号和密码。")
        return self

    @field_validator("paths")
    @classmethod
    def normalize_paths(cls, value: list[str]) -> list[str]:
        paths = [item.strip() for item in value if item.strip()]
        if not paths:
            raise ValueError("连接目录不能为空。")
        return paths


class ResourceProvisioningStepResponse(BaseModel):
    id: int
    step: str
    attempt: int
    state: str
    sql_text: str | None = None
    request_summary: dict | None = None
    response_summary: dict | None = None
    message: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ResourceProvisioningRowResponse(BaseModel):
    id: uuid.UUID
    row_no: int
    person_name: str
    department_name: str
    mobile: str
    db_username: str
    database_name: str
    state: str
    current_step: str | None = None
    message: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: list[ResourceProvisioningStepResponse] = Field(default_factory=list)


class ResourceProvisioningBatchResponse(BaseModel):
    id: uuid.UUID
    filename: str
    connection_id: uuid.UUID
    connection_name: str
    api_url: str
    youdata_token_url: str | None = None
    youdata_login_name: str | None = None
    token_strategy: str = "youdata_user_password"
    project_id: int
    paths: list[str]
    server: str
    port: int
    parallelism: int
    state: str
    total_count: int
    success_count: int
    failed_count: int
    message: str | None = None
    created_by_username: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    rows: list[ResourceProvisioningRowResponse] = Field(default_factory=list)


class ResourceProvisioningBatchListResponse(BaseModel):
    items: list[ResourceProvisioningBatchResponse]


RESOURCE_PERMISSION_OPTIONS = (
    "view",
    "addModel",
    "customSql",
    "sqlFetch",
    "sqlFetchCopyData",
    "sqlFetchExport",
    "sqlFetchShare",
    "updateData",
    "relationship",
)
_SAFE_RESOURCE_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,127}$"
)


class ResourcePermissionBatchCreateRequest(BaseModel):
    source_batch_id: uuid.UUID
    lookup_connection_id: uuid.UUID
    lookup_database: str = Field(default="TESTS", min_length=1, max_length=128)
    lookup_table: str = Field(default="data_connection", min_length=1, max_length=128)
    lookup_name_column: str = Field(default="name", min_length=1, max_length=128)
    lookup_id_column: str = Field(default="id", min_length=1, max_length=128)
    permission_api_url: HttpUrl
    project_id: int = Field(gt=0)
    paths: list[str] = Field(min_length=1)
    expire_at: datetime
    permissions: list[str] = Field(default_factory=lambda: list(RESOURCE_PERMISSION_OPTIONS), min_length=1)
    parallelism: int = Field(default=2, ge=1, le=10)
    lookup_timeout_seconds: int = Field(default=60, ge=1, le=600)
    lookup_interval_seconds: int = Field(default=2, ge=1, le=30)

    @field_validator("lookup_database", "lookup_table", "lookup_name_column", "lookup_id_column")
    @classmethod
    def validate_lookup_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not _SAFE_RESOURCE_IDENTIFIER_RE.fullmatch(normalized):
            raise ValueError("资源查询库、表和字段名称只能包含中文、字母、数字和下划线。")
        return normalized

    @field_validator("paths")
    @classmethod
    def normalize_permission_paths(cls, value: list[str]) -> list[str]:
        paths = [item.strip() for item in value if item.strip()]
        if not paths:
            raise ValueError("授权目录不能为空。")
        return paths

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, value: list[str]) -> list[str]:
        unique = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        invalid = [item for item in unique if item not in RESOURCE_PERMISSION_OPTIONS]
        if invalid:
            raise ValueError(f"不支持的数据连接权限：{', '.join(invalid)}")
        if not unique:
            raise ValueError("至少选择一项数据连接权限。")
        return unique


class ResourcePermissionStepResponse(BaseModel):
    id: int
    step: str
    attempt: int
    state: str
    sql_text: str | None = None
    request_summary: dict | None = None
    response_summary: dict | None = None
    message: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ResourcePermissionRowResponse(BaseModel):
    id: uuid.UUID
    source_row_id: uuid.UUID
    row_no: int
    person_name: str
    department_name: str
    mobile: str
    database_name: str
    resource_id: int | None = None
    role_id: int | None = None
    role_delete_state: str = "not_created"
    role_delete_message: str | None = None
    role_deleted_at: datetime | None = None
    state: str
    current_step: str | None = None
    message: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: list[ResourcePermissionStepResponse] = Field(default_factory=list)


class ResourcePermissionBatchResponse(BaseModel):
    id: uuid.UUID
    source_batch_id: uuid.UUID
    source_filename: str
    lookup_connection_id: uuid.UUID
    lookup_connection_name: str
    lookup_database: str
    lookup_table: str
    lookup_name_column: str
    lookup_id_column: str
    permission_api_url: str
    youdata_login_name: str | None = None
    token_strategy: str = "youdata_user_password"
    project_id: int
    paths: list[str]
    expire_at: datetime
    permissions: list[str]
    parallelism: int
    lookup_timeout_seconds: int
    lookup_interval_seconds: int
    state: str
    total_count: int
    success_count: int
    failed_count: int
    message: str | None = None
    created_by_username: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    rows: list[ResourcePermissionRowResponse] = Field(default_factory=list)


class ResourcePermissionBatchListResponse(BaseModel):
    items: list[ResourcePermissionBatchResponse]
