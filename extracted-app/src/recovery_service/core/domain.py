"""Domain objects for import metadata and impdp parameters."""

from dataclasses import dataclass, field
from typing import Any

from recovery_service.core.enums import DumpVolumeType, ImportMode


@dataclass
class RemoteHost:
    host: str
    port: int = 22
    username: str = ""
    password: str = ""
    private_key_path: str | None = None


@dataclass
class TargetDatabase:
    connection_string: str
    admin_user: str
    admin_password: str
    default_tablespace: str = "USERS"
    default_temp_tablespace: str = "TEMP"


@dataclass
class DumpArtifact:
    remote_path: str
    filename: str
    size_bytes: int = 0


@dataclass
class DumpVolumeGroup:
    """Logical group of dump files (single or multi-volume)."""
    group_id: str
    dump_files: list[DumpArtifact] = field(default_factory=list)
    log_files: list[DumpArtifact] = field(default_factory=list)
    par_files: list[DumpArtifact] = field(default_factory=list)
    volume_type: DumpVolumeType = DumpVolumeType.UNKNOWN


@dataclass
class ExportMetadata:
    """Inferred metadata — never from binary dmp reads."""
    source_version: str | None = None
    export_mode: ImportMode = ImportMode.UNKNOWN
    schemas: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    tablespaces: list[str] = field(default_factory=list)
    dumpfile_param: str | None = None
    logfile_param: str | None = None
    raw_parfile_lines: list[str] = field(default_factory=list)
    confidence: float = 0.0
    discovery_source: str = ""


@dataclass
class ImpdpParams:
    """Buildable impdp command parameters."""
    connection: str
    directory: str | None = None
    dumpfile: str | None = None
    logfile: str | None = None
    sqlfile: str | None = None
    schemas: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    table_exists_action: str = "SKIP"
    parallel: int = 4
    content: str | None = None
    access_method: str | None = None
    full: bool = False
    version: str | None = None
    remap_schema: list[str] = field(default_factory=list)
    remap_tablespace: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_cli_args(self) -> list[str]:
        args: list[str] = []
        if self.full:
            args.append("FULL=Y")
        if self.schemas:
            args.append(f"SCHEMAS={','.join(self.schemas)}")
        if self.tables:
            args.append(f"TABLES={','.join(self.tables)}")
        if self.dumpfile:
            args.append(f"DUMPFILE={self.dumpfile}")
        if self.logfile:
            args.append(f"LOGFILE={self.logfile}")
        if self.sqlfile:
            args.append(f"SQLFILE={self.sqlfile}")
        if self.directory:
            args.append(f"DIRECTORY={self.directory}")
        if self.table_exists_action:
            args.append(f"TABLE_EXISTS_ACTION={self.table_exists_action}")
        if self.parallel:
            args.append(f"PARALLEL={self.parallel}")
        if self.content:
            args.append(f"CONTENT={self.content}")
        if self.access_method:
            args.append(f"ACCESS_METHOD={self.access_method}")
        if self.version:
            args.append(f"VERSION={self.version}")
        for rs in self.remap_schema:
            args.append(f"REMAP_SCHEMA={rs}")
        for rt in self.remap_tablespace:
            args.append(f"REMAP_TABLESPACE={rt}")
        for ex in self.exclude:
            args.append(f"EXCLUDE={ex}")
        for k, v in self.extra.items():
            args.append(f"{k}={v}")
        return args
