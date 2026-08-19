from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

DatabaseEngine = Literal["mysql", "sqlserver", "oracle", "doris", "ftp"]


class CleanupConnection(BaseModel):
    engine: DatabaseEngine
    host: str = "127.0.0.1"
    port: int | None = None
    username: str
    password: SecretStr
    database: str | None = None
    service_name: str | None = None
    dsn: str | None = None

    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str | None = None
    ssh_password: SecretStr | None = None
    container_name: str | None = None


class CleanupRequest(BaseModel):
    connection: CleanupConnection | None = None
    connection_id: UUID | None = None


class CleanupTargetRequest(CleanupRequest):
    target_name: str
    drop_storage: bool = False
    cleanup_files: bool = False


class CleanupBatchTargetRequest(CleanupRequest):
    target_names: list[str] = Field(min_length=1)
    drop_storage: bool = False
    cleanup_files: bool = False


class CleanupExecuteRequest(CleanupTargetRequest):
    confirmation: str = Field(
        description="执行前必须与目标名称完全一致。"
    )


class CleanupBatchExecuteRequest(CleanupBatchTargetRequest):
    acknowledged: bool = Field(
        default=False,
        description="The user confirmed the irreversible batch cleanup warning.",
    )


class CleanupStatus(BaseModel):
    ok: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CatalogObject(BaseModel):
    name: str
    type: str
    parent: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CleanupCatalog(BaseModel):
    engine: DatabaseEngine
    targets: list[CatalogObject] = Field(default_factory=list)
    objects: list[CatalogObject] = Field(default_factory=list)
    protected_targets: list[str] = Field(default_factory=list)


class CleanupPlanStep(BaseModel):
    layer: str
    action: str
    target: str
    sql: str | None = None
    required: bool = True
    danger: str = "normal"
    notes: list[str] = Field(default_factory=list)


class CleanupPlan(BaseModel):
    engine: DatabaseEngine
    target_name: str
    protected: bool = False
    can_execute: bool = True
    warnings: list[str] = Field(default_factory=list)
    storage: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[CleanupPlanStep] = Field(default_factory=list)
    confirmation: str


class CleanupBatchPlan(BaseModel):
    engine: DatabaseEngine
    target_names: list[str] = Field(default_factory=list)
    plans: list[CleanupPlan] = Field(default_factory=list)
    can_execute: bool = True
    warnings: list[str] = Field(default_factory=list)
    blocked_targets: list[str] = Field(default_factory=list)


class CleanupExecutionResult(BaseModel):
    engine: DatabaseEngine
    target_name: str
    state: Literal["success", "failed", "blocked"]
    plan: CleanupPlan
    executed_steps: list[CleanupPlanStep] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class CleanupBatchExecutionResult(BaseModel):
    engine: DatabaseEngine
    target_names: list[str] = Field(default_factory=list)
    state: Literal["success", "failed", "partial", "blocked"]
    plan: CleanupBatchPlan
    results: list[CleanupExecutionResult] = Field(default_factory=list)
    success_count: int = 0
    failed_count: int = 0
    blocked_count: int = 0
    error: str | None = None
