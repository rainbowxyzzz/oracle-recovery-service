from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class DataAutomationPipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    status: Literal["active", "paused", "archived"] = "active"
    auto_watch_enabled: bool = False
    watch_interval_minutes: int = Field(default=5, ge=1, le=1440)
    stable_wait_seconds: int = Field(default=60, ge=10, le=86400)
    file_pattern: str = Field(default="*.dmp", min_length=1, max_length=255)
    restore_template_task_id: UUID | None = None
    data_sync_node_id: UUID | None = None
    standard_workflow_version_id: UUID | None = None
    sm4_task_definition_id: UUID | None = None
    business_domain: str | None = Field(default=None, max_length=128)
    standard_target: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class DataAutomationPipelineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["active", "paused", "archived"] | None = None
    auto_watch_enabled: bool | None = None
    watch_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    stable_wait_seconds: int | None = Field(default=None, ge=10, le=86400)
    file_pattern: str | None = Field(default=None, min_length=1, max_length=255)
    restore_template_task_id: UUID | None = None
    data_sync_node_id: UUID | None = None
    standard_workflow_version_id: UUID | None = None
    sm4_task_definition_id: UUID | None = None
    business_domain: str | None = Field(default=None, max_length=128)
    standard_target: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


class DataAutomationBlueprintCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    source_rule: dict[str, Any] = Field(default_factory=dict)
    schema_signature: str | None = Field(default=None, max_length=64)
    schema_contract: dict[str, Any] = Field(default_factory=dict)
    execution_snapshot: dict[str, Any] = Field(default_factory=dict)
    auto_execute: bool = False


class DataAssetCreate(BaseModel):
    connection_id: UUID | None = None
    connection_name: str | None = None
    engine: str = "doris"
    catalog: str = ""
    database: str
    table_name: str
    layer: Literal["restored", "raw", "standard", "secured"] = "raw"
    business_domain: str | None = None
    schema_signature: str | None = None
    columns: list[dict[str, Any]] = Field(default_factory=list)


class DataLineageCreate(BaseModel):
    batch_id: UUID | None = None
    source_asset_id: UUID
    source_field: str | None = None
    target_asset_id: UUID
    target_field: str | None = None
    transformation_type: Literal["direct", "rename", "cast", "expression", "join", "aggregate", "constant"] = "direct"
    expression: str | None = None
    workflow_version_id: UUID | None = None
    node_key: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)
    review_required: bool | None = None


class DataClassificationRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    priority: int = 100
    match_config: dict[str, Any] = Field(default_factory=dict)
    classification: Literal["public", "internal", "sensitive", "highly_sensitive"] = "internal"
    protection_action: Literal["none", "mask", "sm3", "sm4", "review"] = "review"
    auto_apply: bool = False


class ReverseEncryptionExecuteRequest(BaseModel):
    pipeline_id: UUID
    confirm: bool = False
