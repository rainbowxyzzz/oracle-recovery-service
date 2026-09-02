import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid
from recovery_service.common.time import app_now

LONG_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")


class Base(DeclarativeBase):
    pass


class RecoveryTask(Base):
    __tablename__ = "recovery_tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    state: Mapped[str] = mapped_column(String(32), default="created", index=True)
    current_policy_node: Mapped[str | None] = mapped_column(String(64), nullable=True)

    remote_host: Mapped[str] = mapped_column(String(255))
    remote_port: Mapped[int] = mapped_column(Integer, default=22)
    remote_user: Mapped[str] = mapped_column(String(128))
    remote_password_enc: Mapped[str] = mapped_column(Text)
    remote_directory: Mapped[str] = mapped_column(String(1024))

    target_connection: Mapped[str] = mapped_column(String(512))
    target_admin_user: Mapped[str] = mapped_column(String(128))
    target_admin_password_enc: Mapped[str] = mapped_column(Text)

    options: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    impdp_params_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

    progress_percent: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_attempts: Mapped[int] = mapped_column(Integer, default=0)

    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    force_stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    oracle_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    oracle_run_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    oracle_job_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    oracle_container: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TaskStep(Base):
    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    node_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="info", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    stdout: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    stderr: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AssistantPlan(Base):
    __tablename__ = "assistant_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    state: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    plan_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)


class DataAutomationPipeline(Base):
    __tablename__ = "data_automation_pipelines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    auto_watch_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    watch_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    stable_wait_seconds: Mapped[int] = mapped_column(Integer, default=60)
    file_pattern: Mapped[str] = mapped_column(String(255), default="*.dmp")
    restore_template_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    data_sync_node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    standard_workflow_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    sm4_task_definition_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    business_domain: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    standard_target: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DataAutomationBlueprint(Base):
    __tablename__ = "data_automation_blueprints"
    __table_args__ = (UniqueConstraint("pipeline_id", "version_no", name="uq_data_automation_blueprint_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1, index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_rule: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_signature: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    schema_contract: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    auto_execute: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DataAutomationBatch(Base):
    __tablename__ = "data_automation_batches"
    __table_args__ = (UniqueConstraint("pipeline_id", "source_fingerprint", name="uq_data_automation_source"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    blueprint_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    blueprint_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(48), default="discovered", index=True)
    resume_from_stage: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    source_path: Mapped[str] = mapped_column(String(1024))
    source_files: Mapped[list] = mapped_column(JSON, default=list)
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    source_observed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    source_stable_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restore_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    standard_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    encryption_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    restored_target: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_target: Mapped[dict] = mapped_column(JSON, default=dict)
    standard_target: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_signature: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DataAutomationEvent(Base):
    __tablename__ = "data_automation_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    stage: Mapped[str] = mapped_column(String(48), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="info", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DataAsset(Base):
    __tablename__ = "data_assets"
    __table_args__ = (UniqueConstraint("connection_id", "catalog", "database", "table_name", "layer", name="uq_data_asset_identity"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    engine: Mapped[str] = mapped_column(String(32), index=True)
    catalog: Mapped[str] = mapped_column(String(128), default="")
    database: Mapped[str] = mapped_column(String(128), index=True)
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    layer: Mapped[str] = mapped_column(String(32), default="raw", index=True)
    business_domain: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    schema_signature: Mapped[str] = mapped_column(String(64), index=True)
    schema_contract: Mapped[dict] = mapped_column(JSON, default=dict)
    classification_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    first_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    last_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DataLineageEdge(Base):
    __tablename__ = "data_lineage_edges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    source_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    source_field: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    target_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    target_field: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    transformation_type: Mapped[str] = mapped_column(String(32), default="direct", index=True)
    expression: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    workflow_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    node_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    review_required: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DataClassificationRule(Base):
    __tablename__ = "data_classification_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    match_config: Mapped[dict] = mapped_column(JSON, default=dict)
    classification: Mapped[str] = mapped_column(String(32), default="internal", index=True)
    protection_action: Mapped[str] = mapped_column(String(32), default="review", index=True)
    auto_apply: Mapped[bool] = mapped_column(Boolean, default=False)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class BatchJob(Base):
    __tablename__ = "batch_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    state: Mapped[str] = mapped_column(String(32), default="created")
    parent_options: Mapped[dict] = mapped_column(JSON, default=dict)
    task_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="operator", index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiKeyCredential(Base):
    __tablename__ = "api_key_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    key_prefix: Mapped[str] = mapped_column(String(32), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class OperationAuditLog(Base):
    __tablename__ = "operation_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    target_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="success", index=True)
    request_ip: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ApprovalAuthorizationConfig(Base):
    __tablename__ = "approval_authorization_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    doris_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    workflow_base_url: Mapped[str] = mapped_column(String(1024))
    workflow_username: Mapped[str] = mapped_column(String(255))
    workflow_password_enc: Mapped[str] = mapped_column(LONG_TEXT)
    youdata_base_url: Mapped[str] = mapped_column(String(1024))
    youdata_email: Mapped[str] = mapped_column(String(255))
    youdata_password_enc: Mapped[str] = mapped_column(LONG_TEXT)
    default_doris_password_enc: Mapped[str] = mapped_column(LONG_TEXT)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class ApprovalAuthorizationRun(Base):
    __tablename__ = "approval_authorization_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    config_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    config_name: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(32), default="created", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    current_apply_flow_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class ApprovalAuthorizationStepLog(Base):
    __tablename__ = "approval_authorization_step_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    config_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    apply_flow_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    step_key: Mapped[str] = mapped_column(String(64), index=True)
    step_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extracted_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sql_text: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    sql_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sql_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DataPlatformNode(Base):
    __tablename__ = "data_platform_nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, index=True)
    node_type: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DataPlatformFolder(Base):
    __tablename__ = "data_platform_folders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DataPlatformWorkflow(Base):
    __tablename__ = "data_platform_workflows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DataPlatformWorkflowVersion(Base):
    __tablename__ = "data_platform_workflow_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1, index=True)
    channel: Mapped[str] = mapped_column(String(16), default="dev", index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    business_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    release_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    schedule_type: Mapped[str] = mapped_column(String(32), default="daily", index=True)
    run_time: Mapped[str] = mapped_column(String(16), default="02:00")
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    offline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    updated_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DataPlatformWorkflowRun(Base):
    __tablename__ = "data_platform_workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1, index=True)
    channel: Mapped[str] = mapped_column(String(16), default="dev", index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    trigger_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DataPlatformNodeRun(Base):
    __tablename__ = "data_platform_node_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    node_key: Mapped[str] = mapped_column(String(128), index=True)
    node_name: Mapped[str] = mapped_column(String(128), index=True)
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_keys: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DataPlatformComponentRun(Base):
    __tablename__ = "data_platform_component_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    node_name: Mapped[str] = mapped_column(String(128), index=True)
    node_revision: Mapped[int] = mapped_column(Integer, default=1, index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    selected_items: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DataPlatformComponentRunTable(Base):
    __tablename__ = "data_platform_component_run_tables"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    component_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    mapping_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_catalog: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_schema: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_table: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    target_database: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_table: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    sync_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    write_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schema_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    loaded_rows: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DataPlatformComponentRunLog(Base):
    __tablename__ = "data_platform_component_run_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    component_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    table_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO", index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DataPlatformChangeTriggerState(Base):
    __tablename__ = "data_platform_change_trigger_states"
    __table_args__ = (UniqueConstraint("version_id", "node_key", name="uq_data_platform_change_trigger"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    node_key: Mapped[str] = mapped_column(String(128), index=True)
    node_name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending_queue: Mapped[list] = mapped_column(JSON, default=list)
    applied_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    consecutive_matches: Mapped[int] = mapped_column(Integer, default=0)
    pending_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_trigger_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DataPlatformChangeProbe(Base):
    __tablename__ = "data_platform_change_probes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    trigger_state_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    node_key: Mapped[str] = mapped_column(String(128), index=True)
    previous_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    current_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    condition_results: Mapped[list] = mapped_column(JSON, default=list)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="observed", index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DatabaseConnectionProfile(Base):
    __tablename__ = "database_connection_profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    engine: Mapped[str] = mapped_column(String(32), index=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(128))
    password_enc: Mapped[str] = mapped_column(Text, default="")
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    service_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dsn: Mapped[str | None] = mapped_column(String(512), nullable=True)

    ssh_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ssh_password_enc: Mapped[str] = mapped_column(Text, default="")
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisCsvParseTask(Base):
    __tablename__ = "doris_csv_parse_tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="local", index=True)
    import_mode: Mapped[str] = mapped_column(String(32), default="multiple_tables", index=True)
    database: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    delimiter: Mapped[str] = mapped_column(String(16), default=",")
    charset: Mapped[str] = mapped_column(String(64), default="utf-8-sig")
    has_header: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(32), default="created", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    current_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    completed_files: Mapped[int] = mapped_column(Integer, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    processed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0)
    bad_rows: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_create_table: Mapped[bool] = mapped_column(Boolean, default=True)
    import_overwrite: Mapped[bool] = mapped_column(Boolean, default=False)
    import_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    import_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    import_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    import_total_files: Mapped[int] = mapped_column(Integer, default=0)
    imported_files: Mapped[int] = mapped_column(Integer, default=0)
    import_failed_files: Mapped[int] = mapped_column(Integer, default=0)
    import_total_rows: Mapped[int] = mapped_column(Integer, default=0)
    import_loaded_rows: Mapped[int] = mapped_column(Integer, default=0)
    import_filtered_rows: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DorisCsvParseFile(Base):
    __tablename__ = "doris_csv_parse_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    filename: Mapped[str] = mapped_column(String(512), index=True)
    storage_path: Mapped[str] = mapped_column(String(1024))
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    file_index: Mapped[int] = mapped_column(Integer, default=0, index=True)
    table_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0)
    bad_rows: Mapped[int] = mapped_column(Integer, default=0)
    processed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    preview: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)


class DorisCsvTaskLog(Base):
    __tablename__ = "doris_csv_task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO", index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DorisSm3Job(Base):
    __tablename__ = "doris_sm3_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    table_mode: Mapped[str] = mapped_column(String(32), default="create_suffixed")
    backup_table_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_table_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashed_columns: Mapped[list] = mapped_column(JSON, default=list)
    mapping_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mapping_tables: Mapped[dict] = mapped_column(JSON, default=dict)
    field_mapping_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_mapping_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)

    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    current_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    source_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active_query_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DorisSm3Audit(Base):
    __tablename__ = "doris_sm3_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    output_table_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    table_mode: Mapped[str] = mapped_column(String(32))
    hashed_columns: Mapped[list] = mapped_column(JSON, default=list)
    mapping_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mapping_tables: Mapped[dict] = mapped_column(JSON, default=dict)
    source_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    succeeded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DorisSm3TaskLog(Base):
    __tablename__ = "doris_sm3_task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    level: Mapped[str] = mapped_column(String(32), default="INFO", index=True)
    stage: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    sql_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sql_text: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    database_engine: Mapped[str] = mapped_column(String(32), default="doris")
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    database_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    table_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    db_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    affected_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DorisSm4BatchJob(Base):
    __tablename__ = "doris_sm4_batch_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    sm4_key_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    sm4_key_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    table_strategy: Mapped[str] = mapped_column(String(32), default="drop_recreate", index=True)
    target_suffix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_window_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    execution_window_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_window_end: Mapped[str | None] = mapped_column(String(16), nullable=True)
    allow_running_cross_window: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_snapshot: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_snapshot_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tables: Mapped[list] = mapped_column(JSON, default=list)
    results: Mapped[list] = mapped_column(JSON, default=list)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DorisSm4AutoSnapshotTask(Base):
    __tablename__ = "doris_sm4_auto_snapshot_tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    include_databases: Mapped[list] = mapped_column(JSON, default=list)
    exclude_databases: Mapped[list] = mapped_column(JSON, default=list)
    exclude_tables: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    table_strategy: Mapped[str] = mapped_column(String(32), default="drop_recreate", index=True)
    target_suffix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_window_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    execution_window_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_window_end: Mapped[str | None] = mapped_column(String(16), nullable=True)
    allow_running_cross_window: Mapped[bool] = mapped_column(Boolean, default=True)
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisSqlEtlTaskDefinition(Base):
    __tablename__ = "doris_sql_etl_task_definitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    source_connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    target_connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_sql: Mapped[str] = mapped_column(LONG_TEXT)
    target_database: Mapped[str] = mapped_column(String(255), index=True)
    target_table: Mapped[str] = mapped_column(String(255), index=True)
    write_mode: Mapped[str] = mapped_column(String(32), default="truncate_insert", index=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=1000)
    column_mapping: Mapped[list] = mapped_column(JSON, default=list)
    schedule_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisSqlEtlRun(Base):
    __tablename__ = "doris_sql_etl_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    task_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    source_connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    target_connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    target_database: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    target_table: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    write_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_rows: Mapped[int] = mapped_column(Integer, default=0)
    target_rows: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    logs: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QueryExportJob(Base):
    __tablename__ = "query_export_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sql_text: Mapped[str] = mapped_column(LONG_TEXT)
    sql_summary: Mapped[str] = mapped_column(String(512), default="")
    export_format: Mapped[str] = mapped_column(String(16), default="csv", index=True)
    encoding: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resource_profile: Mapped[str] = mapped_column(String(32), default="streaming")
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    progress_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_stage: Mapped[str] = mapped_column(String(64), default="queued")
    throughput_rows_per_second: Mapped[float | None] = mapped_column(nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    downloaded_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DorisSm4KeyVersion(Base):
    __tablename__ = "doris_sm4_key_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    key_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    key_seed_enc: Mapped[str] = mapped_column(LONG_TEXT)
    key_mode: Mapped[str] = mapped_column(String(32), default="random", index=True)
    function_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decrypt_function_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jar_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisSm4FunctionDeployment(Base):
    __tablename__ = "doris_sm4_function_deployments"
    __table_args__ = (
        UniqueConstraint("connection_id", "database", "function_name", name="uq_sm4_function_deployment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    function_name: Mapped[str] = mapped_column(String(64))
    decrypt_function_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    key_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jar_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypt_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    decrypt_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(LONG_TEXT, default="")
    verification_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verification_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisSm4Schedule(Base):
    __tablename__ = "doris_sm4_schedules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    table_strategy: Mapped[str] = mapped_column(String(32), default="drop_recreate")
    target_suffix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tables: Mapped[list] = mapped_column(JSON, default=list)
    schedule_type: Mapped[str] = mapped_column(String(32), default="monthly", index=True)
    run_time: Mapped[str] = mapped_column(String(16), default="02:00")
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    archived_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    archived_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    deleted_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    delete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisSm4TaskDefinition(Base):
    __tablename__ = "doris_sm4_task_definitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    table_strategy: Mapped[str] = mapped_column(String(32), default="drop_recreate", index=True)
    target_suffix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tables: Mapped[list] = mapped_column(JSON, default=list)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    archived_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisSm4TaskDefinitionRevision(Base):
    __tablename__ = "doris_sm4_task_definition_revisions"
    __table_args__ = (UniqueConstraint("task_definition_id", "revision", name="uq_sm4_task_revision"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    task_definition_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    revision: Mapped[int] = mapped_column(Integer, index=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DorisSm3TaskDefinition(Base):
    __tablename__ = "doris_sm3_task_definitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    table_mode: Mapped[str] = mapped_column(String(32), default="create_suffixed", index=True)
    hashed_columns: Mapped[list] = mapped_column(JSON, default=list)
    mapping_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mapping_tables: Mapped[dict] = mapped_column(JSON, default=dict)
    field_mapping_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_mapping_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_suffix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    archived_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DorisSm3TaskDefinitionRevision(Base):
    __tablename__ = "doris_sm3_task_definition_revisions"
    __table_args__ = (UniqueConstraint("task_definition_id", "revision", name="uq_sm3_task_revision"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    task_definition_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    revision: Mapped[int] = mapped_column(Integer, index=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DorisMaskTableAsset(Base):
    __tablename__ = "doris_mask_table_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    database: Mapped[str] = mapped_column(String(255), index=True)
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    source_table_name: Mapped[str] = mapped_column(String(255), index=True)
    output_table_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backup_table_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    algorithm: Mapped[str] = mapped_column(String(32), index=True)
    table_mode: Mapped[str] = mapped_column(String(32), default="")
    columns: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DorisMaskFieldMapping(Base):
    __tablename__ = "doris_mask_field_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    source_database: Mapped[str] = mapped_column(String(255), index=True)
    source_table_name: Mapped[str] = mapped_column(String(255), index=True)
    source_column_name: Mapped[str] = mapped_column(String(255), index=True)
    masked_database: Mapped[str] = mapped_column(String(255), index=True)
    masked_table_name: Mapped[str] = mapped_column(String(255), index=True)
    masked_column_name: Mapped[str] = mapped_column(String(255), index=True)
    algorithm: Mapped[str] = mapped_column(String(32), index=True)
    mapping_database: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    mapping_table_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    mapping_original_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mapping_masked_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="succeeded", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BatchAuthDepartment(Base):
    __tablename__ = "batch_auth_departments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BatchAuthDepartmentUser(Base):
    __tablename__ = "batch_auth_department_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    department_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    db_username: Mapped[str] = mapped_column(String(128), index=True)
    db_user_identity: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BatchAuthDepartmentDatabase(Base):
    __tablename__ = "batch_auth_department_databases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    department_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    department_database: Mapped[str] = mapped_column(String(255), index=True)
    default_privilege: Mapped[str] = mapped_column(String(64), default="SELECT")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BatchAuthInitImportBatch(Base):
    __tablename__ = "batch_auth_init_import_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BatchAuthInitImportRow(Base):
    __tablename__ = "batch_auth_init_import_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    department_name: Mapped[str] = mapped_column(String(128), index=True)
    db_username: Mapped[str] = mapped_column(String(128), index=True)
    db_user_identity: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    department_database: Mapped[str] = mapped_column(String(255), index=True)
    generated_password: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class BatchAuthGrantBatch(Base):
    __tablename__ = "batch_auth_grant_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    department_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    department_name: Mapped[str] = mapped_column(String(128), index=True)
    department_database: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    privilege_type: Mapped[str] = mapped_column(String(64), default="SELECT")
    starts_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    total_table_count: Mapped[int] = mapped_column(Integer, default=0)
    success_table_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_table_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="api-key", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    offlined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class BatchAuthGrantTable(Base):
    __tablename__ = "batch_auth_grant_tables"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    source_database: Mapped[str] = mapped_column(String(255), index=True)
    source_table: Mapped[str] = mapped_column(String(255), index=True)
    source_object_level: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_database: Mapped[str] = mapped_column(String(255), index=True)
    target_object: Mapped[str] = mapped_column(String(255), index=True)
    target_object_type: Mapped[str] = mapped_column(String(32), default="view", index=True)
    publish_sql: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    offline_sql: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    offlined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class BatchAuthPrivilegeLease(Base):
    __tablename__ = "batch_auth_privilege_leases"
    __table_args__ = (
        UniqueConstraint(
            "lease_key_hash",
            name="uq_batch_auth_privilege_lease_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    lease_key_hash: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    db_user_identity: Mapped[str] = mapped_column(String(255), index=True)
    source_database: Mapped[str] = mapped_column(String(255), index=True)
    source_table: Mapped[str] = mapped_column(String(255), index=True)
    privilege_type: Mapped[str] = mapped_column(String(64), default="SELECT", index=True)
    baseline_existed_before_system: Mapped[bool] = mapped_column(Boolean, default=False)
    owned_by_system: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ownership_state: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    grant_owner_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    active_reference_count: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BatchAuthGrantUser(Base):
    __tablename__ = "batch_auth_grant_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    table_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    lease_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    db_username: Mapped[str] = mapped_column(String(128), index=True)
    db_user_identity: Mapped[str] = mapped_column(String(255), index=True)
    privilege_type: Mapped[str] = mapped_column(String(64), default="SELECT")
    grant_sql: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    revoke_sql: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    grant_state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    revoke_state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    privilege_existed_before: Mapped[bool] = mapped_column(Boolean, default=False)
    granted_by_this_batch: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    revoke_decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoke_decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_before_grant_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_before_revoke_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ResourceProvisioningBatch(Base):
    __tablename__ = "resource_provisioning_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255), default="")
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    connection_name: Mapped[str] = mapped_column(String(128), default="")
    api_url: Mapped[str] = mapped_column(String(1024))
    api_token_enc: Mapped[str] = mapped_column(LONG_TEXT, default="")
    youdata_login_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    youdata_password_enc: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    youdata_token_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    user_password_enc: Mapped[str] = mapped_column(LONG_TEXT)
    project_id: Mapped[int] = mapped_column(Integer)
    paths: Mapped[list] = mapped_column(JSON, default=list)
    server: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=9030)
    parallelism: Mapped[int] = mapped_column(Integer, default=2)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResourceProvisioningRow(Base):
    __tablename__ = "resource_provisioning_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_no", name="uq_resource_provisioning_batch_row"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    person_name: Mapped[str] = mapped_column(String(128))
    department_name: Mapped[str] = mapped_column(String(128))
    mobile: Mapped[str] = mapped_column(String(32))
    db_username: Mapped[str] = mapped_column(String(64), index=True)
    database_name: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResourceProvisioningStepLog(Base):
    __tablename__ = "resource_provisioning_step_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    row_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    step: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    sql_text: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    request_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResourcePermissionBatch(Base):
    __tablename__ = "resource_permission_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    source_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    source_filename: Mapped[str] = mapped_column(String(255), default="")
    lookup_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    lookup_connection_name: Mapped[str] = mapped_column(String(128), default="")
    lookup_database: Mapped[str] = mapped_column(String(128), default="TESTS")
    lookup_table: Mapped[str] = mapped_column(String(128), default="data_connection")
    lookup_name_column: Mapped[str] = mapped_column(String(128), default="name")
    lookup_id_column: Mapped[str] = mapped_column(String(128), default="id")
    permission_api_url: Mapped[str] = mapped_column(String(1024))
    api_token_enc: Mapped[str] = mapped_column(LONG_TEXT, default="")
    youdata_login_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    youdata_password_enc: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    youdata_token_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    project_id: Mapped[int] = mapped_column(Integer)
    paths: Mapped[list] = mapped_column(JSON, default=list)
    expire_at: Mapped[datetime] = mapped_column(DateTime)
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    parallelism: Mapped[int] = mapped_column(Integer, default=2)
    lookup_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    lookup_interval_seconds: Mapped[int] = mapped_column(Integer, default=2)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_auth_type: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResourcePermissionRow(Base):
    __tablename__ = "resource_permission_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_no", name="uq_resource_permission_batch_row"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    source_row_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    person_name: Mapped[str] = mapped_column(String(128))
    department_name: Mapped[str] = mapped_column(String(128))
    mobile: Mapped[str] = mapped_column(String(32), index=True)
    database_name: Mapped[str] = mapped_column(String(128), index=True)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    role_delete_state: Mapped[str] = mapped_column(String(32), default="not_created", index=True)
    role_delete_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    role_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResourcePermissionStepLog(Base):
    __tablename__ = "resource_permission_step_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    row_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    step: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    sql_text: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    request_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiOrchestrationConnector(Base):
    __tablename__ = "api_orchestration_connectors"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    method: Mapped[str] = mapped_column(String(16), default="POST")
    url: Mapped[str] = mapped_column(String(2048))
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    query: Mapped[dict] = mapped_column(JSON, default=dict)
    body_template: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    auth_type: Mapped[str] = mapped_column(String(32), default="none")
    auth_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    auth_secret_enc: Mapped[str] = mapped_column(LONG_TEXT, default="")
    success_statuses: Mapped[list] = mapped_column(JSON, default=lambda: [200])
    success_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    success_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApiOrchestrationSqlApi(Base):
    __tablename__ = "api_orchestration_sql_apis"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sql_text: Mapped[str] = mapped_column(LONG_TEXT)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(16), default="read", index=True)
    max_rows: Mapped[int] = mapped_column(Integer, default=200)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApiOrchestrationWorkflow(Base):
    __tablename__ = "api_orchestration_workflows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    published_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApiOrchestrationRun(Base):
    __tablename__ = "api_orchestration_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    workflow_name: Mapped[str] = mapped_column(String(128))
    workflow_revision: Mapped[int] = mapped_column(Integer)
    workflow_snapshot: Mapped[dict] = mapped_column(JSON)
    input_data: Mapped[dict] = mapped_column(JSON, default=dict)
    context_data: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiOrchestrationNodeRun(Base):
    __tablename__ = "api_orchestration_node_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), index=True)
    node_key: Mapped[str] = mapped_column(String(128), index=True)
    node_name: Mapped[str] = mapped_column(String(128))
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    request_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_data: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
