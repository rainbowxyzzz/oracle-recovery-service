"""Policy tree executor — strict order per business requirements."""

from dataclasses import dataclass, field
from importlib import import_module

from recovery_service.common.logging import get_logger
from recovery_service.core.domain import (
    DumpVolumeGroup,
    ExportMetadata,
    ImpdpParams,
    RemoteHost,
    TargetDatabase,
)
from recovery_service.core.enums import PolicyNodeId, TaskState
from recovery_service.core.exceptions import ImpdpError, OraCorrectionExhaustedError
from recovery_service.engine.correction.conflict_resolver import resolve_with_catalog
from recovery_service.engine.correction.ora_dictionary import OraDictionary
from recovery_service.engine.metadata.filename_heuristics import infer_from_filenames
from recovery_service.engine.metadata.log_parser import parse_export_log
from recovery_service.engine.metadata.parfile_parser import parse_parfile
from recovery_service.engine.metadata.sqlfile_probe import probe_via_sqlfile
from recovery_service.engine.metadata.trial_import_analyzer import trial_import_and_analyze
from recovery_service.infrastructure.ssh.sync_client import read_remote_text
from recovery_service.settings import get_settings

logger = get_logger(__name__)
create_impdp_runner = import_module(
    "recovery_service.engine.import.executor_factory"
).create_impdp_runner
build_impdp_params = import_module("recovery_service.engine.import.param_builder").build_impdp_params


@dataclass
class PolicyContext:
    host: RemoteHost
    target: TargetDatabase
    group: DumpVolumeGroup
    remote_directory: str = ""
    options: dict = field(default_factory=dict)
    metadata: ExportMetadata | None = None
    impdp_params: ImpdpParams | None = None
    last_stderr: str = ""
    correction_attempts: int = 0


@dataclass
class PolicyResult:
    success: bool
    state: TaskState
    metadata: ExportMetadata | None
    message: str
    node_id: str | None = None


class PolicyTreeEngine:
    def __init__(self):
        settings = get_settings()
        self.config = settings.load_yaml("policy_tree.yaml")
        self.ora_dict = OraDictionary()
        self.max_corrections = (
            self.config.get("nodes", [{}])[-1].get("max_iterations")
            or settings.load_yaml("default.yaml").get("retry", {}).get("max_correction_attempts", 5)
        )

    def run(self, ctx: PolicyContext) -> PolicyResult:
        runner = create_impdp_runner(ctx.host, ctx.options, ctx.remote_directory)
        return self._run_with_runner(ctx, runner)

    def _run_with_runner(self, ctx: PolicyContext, runner) -> PolicyResult:
        # 1. parse log
        meta = self._try_parse_log(ctx)
        if meta and self._metadata_sufficient(meta):
            ctx.metadata = meta
            return self._execute_import(ctx, PolicyNodeId.PARSE_LOG, runner)

        # 2. parse parfile
        meta = self._try_parse_parfile(ctx)
        if meta and self._metadata_sufficient(meta):
            ctx.metadata = meta
            return self._execute_import(ctx, PolicyNodeId.PARSE_PARFILE, runner)

        # 3. filename
        meta = infer_from_filenames(ctx.group)
        if meta:
            ctx.metadata = meta
            if self._metadata_sufficient(meta):
                return self._execute_import(ctx, PolicyNodeId.PARSE_FILENAME, runner)

        # 4. sqlfile probe
        ctx.impdp_params = build_impdp_params(
            ctx.target, ctx.group, ctx.metadata or ExportMetadata(), options=ctx.options
        )
        sqlfile = ctx.options.get("sqlfile", "probe_metadata.sql")
        meta, _ = probe_via_sqlfile(runner, ctx.target, ctx.impdp_params, sqlfile)
        if meta:
            ctx.metadata = self._merge_metadata(ctx.metadata, meta)
            return self._execute_import(ctx, PolicyNodeId.SQLFILE_PROBE, runner)

        # 5. trial import
        meta, stderr, _ = trial_import_and_analyze(
            runner,
            ctx.target,
            ctx.impdp_params,
            timeout=get_settings().oracle_import_operation_timeout_seconds,
        )
        ctx.last_stderr = stderr
        if meta:
            ctx.metadata = self._merge_metadata(ctx.metadata, meta)

        # 6. execute import with correction loop
        return self._execute_import_with_corrections(ctx, PolicyNodeId.TRIAL_IMPORT, runner)

    def _metadata_sufficient(self, meta: ExportMetadata) -> bool:
        return meta.confidence >= 0.5 and (
            meta.export_mode.value != "UNKNOWN" or meta.dumpfile_param is not None
        )

    def _merge_metadata(self, base: ExportMetadata | None, new: ExportMetadata) -> ExportMetadata:
        if not base:
            return new
        if not base.source_version and new.source_version:
            base.source_version = new.source_version
        if base.export_mode.value == "UNKNOWN" and new.export_mode.value != "UNKNOWN":
            base.export_mode = new.export_mode
        base.schemas = list({*base.schemas, *new.schemas})
        base.tables = list({*base.tables, *new.tables})
        base.confidence = max(base.confidence, new.confidence)
        return base

    def _try_parse_log(self, ctx: PolicyContext) -> ExportMetadata | None:
        for log in ctx.group.log_files:
            try:
                text = read_remote_text(ctx.host, log.remote_path)
                return parse_export_log(text)
            except Exception as e:
                logger.warning("log_parse_failed", path=log.remote_path, error=str(e))
        return None

    def _try_parse_parfile(self, ctx: PolicyContext) -> ExportMetadata | None:
        for par in ctx.group.par_files:
            try:
                text = read_remote_text(ctx.host, par.remote_path)
                return parse_parfile(text)
            except Exception as e:
                logger.warning("parfile_parse_failed", path=par.remote_path, error=str(e))
        return None

    def _execute_import(self, ctx: PolicyContext, node: PolicyNodeId, runner) -> PolicyResult:
        ctx.impdp_params = build_impdp_params(
            ctx.target, ctx.group, ctx.metadata or ExportMetadata(), options=ctx.options
        )
        return self._execute_import_with_corrections(ctx, node, runner)

    def _execute_import_with_corrections(
        self, ctx: PolicyContext, node: PolicyNodeId, runner
    ) -> PolicyResult:
        settings = get_settings()
        auto = ctx.options.get("auto_confirm", settings.auto_confirm_import)
        params = ctx.impdp_params or build_impdp_params(
            ctx.target, ctx.group, ctx.metadata or ExportMetadata(), options=ctx.options
        )

        if not auto:
            params.content = "METADATA_ONLY"

        while ctx.correction_attempts <= self.max_corrections:
            try:
                runner.run_import(
                    params,
                    timeout=settings.oracle_import_operation_timeout_seconds,
                    allow_failure=False,
                )
                return PolicyResult(
                    success=True,
                    state=TaskState.SUCCEEDED,
                    metadata=ctx.metadata,
                    message="Import completed",
                    node_id=node.value,
                )
            except ImpdpError as e:
                ctx.last_stderr = e.stderr
                match = self.ora_dict.match(e.stderr)
                if not match or ctx.correction_attempts >= self.max_corrections:
                    return PolicyResult(
                        success=False,
                        state=TaskState.FAILED,
                        metadata=ctx.metadata,
                        message=str(e),
                        node_id=PolicyNodeId.ORA_CORRECTION_LOOP.value,
                    )
                ctx.correction_attempts += 1
                params = resolve_with_catalog(
                    params, ctx.target, match, e.stderr, ctx.options
                )
                ctx.impdp_params = params

        raise OraCorrectionExhaustedError("Max ORA correction attempts exceeded")
