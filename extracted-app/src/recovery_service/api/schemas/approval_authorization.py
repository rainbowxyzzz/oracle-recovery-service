from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator


class ApprovalAuthorizationConfigPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    doris_connection_id: UUID
    workflow_base_url: str = Field(min_length=4, max_length=1024)
    workflow_username: str = Field(min_length=1, max_length=255)
    workflow_password: SecretStr | None = None
    youdata_base_url: str = Field(min_length=4, max_length=1024)
    youdata_email: str = Field(min_length=1, max_length=255)
    youdata_password: SecretStr | None = None
    default_doris_password: SecretStr | None = None
    status: Literal["active", "disabled"] = "active"
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workflow_base_url", "youdata_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


class ApprovalAuthorizationConfigResponse(BaseModel):
    id: UUID
    name: str
    status: str
    doris_connection_id: UUID
    workflow_base_url: str
    workflow_username: str
    has_workflow_password: bool
    youdata_base_url: str
    youdata_email: str
    has_youdata_password: bool
    has_default_doris_password: bool
    config: dict[str, Any] = Field(default_factory=dict)
    created_by_username: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ApprovalAuthorizationStepTestRequest(BaseModel):
    step_key: str = Field(min_length=1, max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)


class ApprovalAuthorizationRunRequest(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)


class ApprovalAuthorizationStepLogResponse(BaseModel):
    id: UUID
    run_id: UUID
    config_id: UUID
    apply_flow_id: str | None = None
    step_key: str
    step_name: str
    status: str
    message: str | None = None
    request_data: dict[str, Any] | None = None
    response_data: dict[str, Any] | None = None
    extracted_data: dict[str, Any] | None = None
    sql_text: str | None = None
    sql_params: dict[str, Any] | None = None
    sql_result: dict[str, Any] | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class ApprovalAuthorizationRunResponse(BaseModel):
    id: UUID
    config_id: UUID
    config_name: str
    state: str
    message: str | None = None
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    current_apply_flow_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_by_username: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ApprovalAuthorizationRunDetailResponse(ApprovalAuthorizationRunResponse):
    logs: list[ApprovalAuthorizationStepLogResponse] = Field(default_factory=list)


class ApprovalAuthorizationStepTestResponse(BaseModel):
    run: ApprovalAuthorizationRunResponse
    log: ApprovalAuthorizationStepLogResponse
