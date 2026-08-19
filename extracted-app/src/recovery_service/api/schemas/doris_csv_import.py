from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr


class DorisCsvColumnPreview(BaseModel):
    original_name: str
    name: str
    type: str
    nullable: bool = True
    max_length: int = 0
    sample_values: list[str] = Field(default_factory=list)


class DorisCsvBadRowPreview(BaseModel):
    row_number: int
    reason: str
    raw_text: str | None = None
    values: list[str] = Field(default_factory=list)


class DorisCsvFilePreview(BaseModel):
    filename: str
    table_name: str
    charset: str | None = None
    charset_detection: dict[str, Any] = Field(default_factory=dict)
    has_header: bool = True
    expected_columns: int = 0
    valid_row_count: int = 0
    bad_row_count: int = 0
    columns: list[DorisCsvColumnPreview] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    bad_rows: list[DorisCsvBadRowPreview] = Field(default_factory=list)
    target_table_exists: bool = False
    target_table_columns: list[str] = Field(default_factory=list)
    total_preview_rows: int = 0
    warnings: list[str] = Field(default_factory=list)


class DorisCsvPreviewResponse(BaseModel):
    database: str | None = None
    files: list[DorisCsvFilePreview] = Field(default_factory=list)


DorisCsvImportMode = Literal["multiple_tables", "single_table"]
DorisCsvParseTaskState = Literal[
    "created",
    "uploading",
    "queued",
    "parsing",
    "validating",
    "completed",
    "waiting_import",
    "importing",
    "imported",
    "import_failed",
    "failed",
    "stopping",
    "stopped",
]
DorisCsvParseFileState = Literal[
    "waiting",
    "parsing",
    "succeeded",
    "failed",
    "waiting_import",
    "importing",
    "imported",
    "import_failed",
    "stopped",
]


class DorisCsvParseFileStatus(BaseModel):
    id: UUID
    filename: str
    table_name: str | None = None
    file_size: int = 0
    file_index: int = 0
    state: DorisCsvParseFileState | str
    message: str = ""
    total_rows: int = 0
    valid_rows: int = 0
    bad_rows: int = 0
    processed_bytes: int = 0
    preview: DorisCsvFilePreview | None = None
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DorisCsvParseTaskStatus(BaseModel):
    task_id: UUID
    connection_id: UUID
    connection_name: str | None = None
    source: str = "local"
    import_mode: DorisCsvImportMode | str = "multiple_tables"
    database: str | None = None
    delimiter: str = ","
    charset: str = "utf-8-sig"
    has_header: bool = True
    state: DorisCsvParseTaskState | str
    message: str = ""
    current_stage: str | None = None
    current_file: str | None = None
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    processed_bytes: int = 0
    total_rows: int = 0
    valid_rows: int = 0
    bad_rows: int = 0
    progress: float = 0
    stop_requested: bool = False
    error_message: str | None = None
    import_create_table: bool = True
    import_overwrite: bool = False
    import_requested_at: datetime | None = None
    import_started_at: datetime | None = None
    import_finished_at: datetime | None = None
    import_total_files: int = 0
    imported_files: int = 0
    import_failed_files: int = 0
    import_total_rows: int = 0
    import_loaded_rows: int = 0
    import_filtered_rows: int = 0
    result: DorisCsvImportResponse | None = None
    preview: DorisCsvPreviewResponse | None = None
    files: list[DorisCsvParseFileStatus] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DorisCsvParseTaskListResponse(BaseModel):
    tasks: list[DorisCsvParseTaskStatus] = Field(default_factory=list)


class DorisCsvTaskLogItem(BaseModel):
    id: int
    task_id: UUID
    file_id: UUID | None = None
    level: str = "INFO"
    stage: str | None = None
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class DorisCsvTaskLogListResponse(BaseModel):
    logs: list[DorisCsvTaskLogItem] = Field(default_factory=list)


class DorisCsvFilePreviewUpdateRequest(BaseModel):
    preview: DorisCsvFilePreview


class DorisCsvImportStartRequest(BaseModel):
    create_table: bool = True
    overwrite: bool = False
    database: str | None = None


class DorisFtpConnection(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = 21
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr
    directory: str = "/"


class DorisFtpCatalogItem(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory"]
    size: int | None = None
    modified: str | None = None


class DorisFtpCatalogRequest(BaseModel):
    ftp: DorisFtpConnection | None = None
    ftp_connection_id: UUID | None = None
    directory: str | None = None


class DorisFtpCatalogResponse(BaseModel):
    directory: str
    items: list[DorisFtpCatalogItem] = Field(default_factory=list)


class DorisFtpCsvRequest(BaseModel):
    connection_id: UUID
    ftp: DorisFtpConnection | None = None
    ftp_connection_id: UUID | None = None
    ftp_directory: str | None = None
    database: str | None = None
    delimiter: str = ","
    charset: str = "utf-8-sig"
    has_header: bool = True
    import_mode: DorisCsvImportMode = "multiple_tables"
    filenames: list[str] = Field(default_factory=list)
    include_all_csv: bool = False


class DorisFtpCsvImportRequest(DorisFtpCsvRequest):
    create_table: bool = True
    overwrite: bool = False
    table_specs_json: str | None = None


class DorisCsvFileImportResult(BaseModel):
    filename: str
    table_name: str
    state: Literal["success", "failed"]
    message: str
    loaded_rows: int = 0
    filtered_rows: int = 0
    unselected_rows: int = 0
    rejected_rows: int = 0
    reject_preview: list[DorisCsvBadRowPreview] = Field(default_factory=list)
    reject_download_url: str | None = None
    raw_result: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class DorisCsvImportResponse(BaseModel):
    database: str
    state: Literal["success", "failed", "partial"]
    success_count: int = 0
    failed_count: int = 0
    results: list[DorisCsvFileImportResult] = Field(default_factory=list)
