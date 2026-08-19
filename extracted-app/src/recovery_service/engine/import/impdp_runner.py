import os
from dataclasses import dataclass

from recovery_service.core.domain import ImpdpParams, TargetDatabase
from recovery_service.core.exceptions import ImpdpError
from recovery_service.infrastructure.subprocess.safe_runner import ProcessResult, run_command
from recovery_service.settings import get_settings


@dataclass
class ImpdpRunContext:
    target: TargetDatabase
    oracle_directory: str = "DATA_PUMP_DIR"


class ImpdpRunner:
    def __init__(self, impdp_bin: str | None = None):
        settings = get_settings()
        self.impdp_bin = impdp_bin or settings.impdp_bin
        self._mask = settings.load_yaml("default.yaml").get("security", {}).get("mask_patterns")

    def _build_cmd(self, params: ImpdpParams) -> list[str]:
        user = params.connection
        if "@" not in user and "/" not in user:
            raise ImpdpError("connection must be user/pass@dsn")
        cmd = [self.impdp_bin, user]
        cmd.extend(params.to_cli_args())
        return cmd

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        settings = get_settings()
        if settings.oracle_client_lib_dir:
            env["LD_LIBRARY_PATH"] = settings.oracle_client_lib_dir
        return env

    def run_import(
        self,
        params: ImpdpParams,
        *,
        timeout: int | None = None,
        allow_failure: bool = False,
    ) -> ProcessResult:
        cmd = self._build_cmd(params)
        result = run_command(
            cmd,
            timeout=timeout or get_settings().oracle_import_operation_timeout_seconds,
            env=self._env(),
            mask_patterns=self._mask,
        )
        if result.returncode != 0 and not allow_failure:
            raise ImpdpError(
                f"impdp failed with code {result.returncode}",
                stderr=result.stderr,
                return_code=result.returncode,
            )
        return result
