from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImpCommandSpec:
    connection: str
    dumpfile: str
    logfile: str
    full: bool = True
    ignore: bool = True
    extra_args: list[str] = field(default_factory=list)

    def to_args(self) -> list[str]:
        args = ["imp", self.connection, f"FILE={self.dumpfile}", f"LOG={self.logfile}"]
        if self.full:
            args.append("FULL=Y")
        if self.ignore:
            args.append("IGNORE=Y")
        args.extend(self.extra_args)
        return args


@dataclass(frozen=True)
class ImpdpCommandSpec:
    connection: str
    directory: str
    dumpfile: str
    logfile: str
    remap_schemas: list[tuple[str, str]] = field(default_factory=list)
    remap_tablespace: str | None = None
    table_exists_action: str = "REPLACE"
    parallel: int | None = None
    metrics: bool | None = None
    logtime: str | None = None
    access_method: str | None = None
    disable_archive_logging: bool = False
    exclude_indexes: bool = False
    extra_args: list[str] = field(default_factory=list)

    def to_args(self) -> list[str]:
        args = [
            "impdp",
            self.connection,
            f"DIRECTORY={self.directory}",
            f"DUMPFILE={self.dumpfile}",
            f"LOGFILE={self.logfile}",
            f"TABLE_EXISTS_ACTION={self.table_exists_action}",
        ]
        for source_schema, target_schema in self.remap_schemas:
            args.append(f"REMAP_SCHEMA={source_schema}:{target_schema}")
        if self.remap_tablespace:
            args.append(f"REMAP_TABLESPACE=%:{self.remap_tablespace}")
        if self.parallel:
            args.append(f"PARALLEL={self.parallel}")
        if self.metrics is not None:
            args.append(f"METRICS={'Y' if self.metrics else 'N'}")
        if self.logtime:
            args.append(f"LOGTIME={self.logtime}")
        if self.access_method:
            args.append(f"ACCESS_METHOD={self.access_method}")
        if self.disable_archive_logging:
            args.append("TRANSFORM=DISABLE_ARCHIVE_LOGGING:Y")
        if self.exclude_indexes:
            args.append("EXCLUDE=INDEX")
        args.extend(self.extra_args)
        return args
