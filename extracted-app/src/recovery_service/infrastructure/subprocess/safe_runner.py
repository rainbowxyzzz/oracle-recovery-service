import subprocess
from dataclasses import dataclass

from recovery_service.common.security import mask_sensitive
from recovery_service.core.exceptions import ImpdpError


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    command: list[str]


def run_command(
    cmd: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    mask_patterns: list[str] | None = None,
) -> ProcessResult:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    return ProcessResult(
        returncode=proc.returncode,
        stdout=mask_sensitive(proc.stdout or "", mask_patterns),
        stderr=mask_sensitive(proc.stderr or "", mask_patterns),
        command=[mask_sensitive(c, mask_patterns) for c in cmd],
    )


def run_command_or_raise(
    cmd: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    error_prefix: str = "Command failed",
) -> ProcessResult:
    result = run_command(cmd, timeout=timeout, env=env)
    if result.returncode != 0:
        raise ImpdpError(
            f"{error_prefix}: exit {result.returncode}",
            stderr=result.stderr,
            return_code=result.returncode,
        )
    return result
