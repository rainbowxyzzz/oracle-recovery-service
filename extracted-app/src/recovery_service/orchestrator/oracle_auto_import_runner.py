import json
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from recovery_service.common.security import mask_sensitive
from recovery_service.core.domain import DumpVolumeGroup, RemoteHost, TargetDatabase
from recovery_service.engine.oracle.dump_detector import choose_dumpfiles
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command, run_ssh_command_stream
from recovery_service.infrastructure.ssh.file_transfer import ensure_remote_directory
from recovery_service.infrastructure.ssh.sync_client import read_remote_text
from recovery_service.settings import get_settings


SCRIPT_LOCAL_PATH = Path(__file__).resolve().parents[1] / "tools" / "oracle_dmp_auto_import.py"
REMOTE_TOOL_DIR = "/opt/oracle-recovery-service-package/tools"
REMOTE_RUNS_DIR = "/opt/oracle-recovery-service-package/oracle-auto-import-runs"

ESSENTIAL_PREFLIGHT_CODES = {"ssh", "python", "script_compile"}


def oracle_datapump_job_name(task_id: str, run_id: str) -> str:
    task_token = "".join(ch for ch in task_id.upper() if ch.isalnum())[:12] or "TASK"
    run_token = "".join(ch for ch in run_id.upper() if ch.isalnum())[-8:] or "RUN"
    return f"ORS_{task_token}_{run_token}"[:28]


def oracle_datapump_job_candidates(job_name: str) -> list[str]:
    base = job_name.strip().upper()
    return [base, f"{base}_F", *(f"{base}_P{index}" for index in range(1, 6))]


def _auto_import_tablespace_size(value: str, fallback: str) -> str:
    normalized = (value or "").strip().upper()
    match = re.fullmatch(r"(\d+)\s*([KMG])", normalized)
    if not normalized:
        return fallback
    if match and match.group(2) == "G" and int(match.group(1)) >= 1:
        return fallback
    return normalized


@dataclass
class OraclePreflightCheck:
    code: str
    name: str
    state: str
    message: str
    detail: dict = field(default_factory=dict)
    suggestion: str = ""

    @property
    def ok(self) -> bool:
        return self.state in {"passed", "warning"}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "state": self.state,
            "message": self.message,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


@dataclass
class OracleAutoImportResult:
    success: bool
    state: str
    message: str
    tool: str = "oracle_auto_import"
    returncode: int = 1
    stdout: str = ""
    stderr: str = ""
    run_id: str = ""
    run_dir: str = ""
    plan: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    run_log: str = ""
    command: str = ""
    preflight_checks: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    log_manifest: list[dict] = field(default_factory=list)


class OracleAutoImportRunner:
    def run(
        self,
        *,
        task_id: str,
        oracle_host: RemoteHost,
        group: DumpVolumeGroup,
        dmp_host_path: str,
        dmp_container_path: str,
        tablespace_container_path: str,
        container: str,
        target: TargetDatabase,
        target_user_password: str,
        oracle_home_in_container: str | None,
        oracle_directory: str | None,
        execute: bool,
        manual_dumpfile: str | None = None,
        export_log: dict | None = None,
        on_event: Callable[[dict], None] | None = None,
        on_runtime: Callable[[dict], None] | None = None,
    ) -> OracleAutoImportResult:
        preflight_checks = self.preflight(
            oracle_host=oracle_host,
            group=group,
            dmp_host_path=dmp_host_path,
            dmp_container_path=dmp_container_path,
            container=container,
            target=target,
            oracle_home_in_container=oracle_home_in_container,
            oracle_directory=oracle_directory,
            manual_dumpfile=manual_dumpfile,
            export_log=export_log,
            execute=execute,
        )
        failed = [item for item in preflight_checks if item["state"] == "failed"]
        if failed:
            first = failed[0]
            return OracleAutoImportResult(
                success=False,
                state="failed",
                message=f"Oracle 自动导入前置检查失败：{first['name']}，{first['message']}",
                returncode=1,
                preflight_checks=preflight_checks,
            )

        self._install_script(oracle_host)
        dump_args = self._dump_args(group, dmp_host_path, manual_dumpfile=manual_dumpfile)
        task_token = task_id.replace("-", "")[:16]
        run_id = f"task_{task_token}_{uuid.uuid4().hex[:12]}"
        run_dir = str(PurePosixPath(REMOTE_RUNS_DIR) / run_id)
        job_name = oracle_datapump_job_name(task_id, run_id)
        if on_runtime:
            on_runtime(
                {
                    "run_id": run_id,
                    "run_dir": run_dir,
                    "job_name": job_name,
                    "container": container,
                }
            )
        remote_script = str(PurePosixPath(REMOTE_TOOL_DIR) / "oracle_dmp_auto_import.py")
        python_bin = self._remote_python(oracle_host)
        self._verify_remote_script(oracle_host, python_bin, remote_script)
        command_args = [
            python_bin,
            remote_script,
            *dump_args,
            "--container",
            container,
            "--username",
            target.admin_user,
            "--password",
            target.admin_password,
            "--pdb",
            target.connection_string.rsplit("/", 1)[-1] if "/" in target.connection_string else "",
            "--runs-dir",
            REMOTE_RUNS_DIR,
            "--run-id",
            run_id,
            "--job-name",
            job_name,
            "--container-dir",
            dmp_container_path.rstrip("/") + "/auto_import",
            "--target-datafile-dir",
            tablespace_container_path,
            "--target-user-password",
            target_user_password,
            "--default-tablespace",
            target.default_tablespace,
            "--tablespace-size",
            _auto_import_tablespace_size(get_settings().oracle_tablespace_initial_size, "100M"),
            "--tablespace-next",
            _auto_import_tablespace_size(get_settings().oracle_tablespace_next_size, "100M"),
            "--tablespace-maxsize",
            get_settings().oracle_tablespace_max_size,
            "--on-conflict",
            "recreate",
            "--table-exists-action",
            "REPLACE",
            "--import-mode",
            "schemas",
        ]
        if manual_dumpfile:
            if not oracle_directory:
                raise RuntimeError("直接使用 Oracle DMP 目录时必须配置 Oracle DIRECTORY 对象。")
            command_args.extend(
                [
                    "--direct-container-dir",
                    dmp_container_path,
                    "--dump-directory-object",
                    oracle_directory,
                ]
            )
        elif oracle_directory:
            command_args.extend(["--directory-object", oracle_directory])
        if export_log:
            manifest = export_log.get("manifest") or {}
            command_args.extend(
                [
                    "--export-log-name",
                    str(export_log.get("filename") or ""),
                    "--export-log-sha256",
                    str(manifest.get("content_sha256") or ""),
                    "--export-log-status",
                    str(manifest.get("source_status") or ""),
                    "--export-log-mode",
                    str(manifest.get("export_mode") or ""),
                    "--export-log-schemas",
                    ",".join(manifest.get("source_schemas") or []),
                    "--export-log-dump-files",
                    ",".join(manifest.get("dump_files") or []),
                    "--export-log-missing-count",
                    str(manifest.get("missing_object_count") or 0),
                ]
            )
        if execute:
            command_args.append("--execute")

        command = self._remote_command(command_args, oracle_home_in_container)
        def handle_stdout(line: str) -> None:
            marker = "@@ORACLE_EVENT@@"
            if not on_event or not line.startswith(marker):
                return
            try:
                event = json.loads(line[len(marker):])
            except json.JSONDecodeError:
                return
            if isinstance(event, dict):
                on_event(event)

        result = run_ssh_command_stream(
            oracle_host,
            command,
            timeout=get_settings().oracle_import_operation_timeout_seconds,
            on_stdout=handle_stdout,
        )
        plan = self._read_json(oracle_host, str(PurePosixPath(run_dir) / "plan.json"))
        report = self._read_json(oracle_host, str(PurePosixPath(run_dir) / "report.json"))
        run_log = self._read_text(
            oracle_host,
            str(PurePosixPath(run_dir) / "run.log"),
            max_bytes=50_000_000,
        )
        timeline = self._read_json_lines(
            oracle_host,
            str(PurePosixPath(run_dir) / "timeline.jsonl"),
        )
        if export_log and run_dir:
            self._archive_export_log(
                oracle_host,
                source_path=str(export_log.get("remote_path") or ""),
                filename=str(export_log.get("filename") or "export.log"),
                run_dir=run_dir,
            )
        log_manifest = self._list_log_artifacts(oracle_host, run_dir)
        report_status = report.get("status")
        success = result.returncode == 0 and report_status in {"dry-run", "imported", "imported_with_warnings"}
        state = (
            "cancelled"
            if report_status == "stopped" or result.returncode == 130
            else ("succeeded_with_warnings" if report_status == "imported_with_warnings" else ("succeeded" if success else "failed"))
        )
        message = self._message(result, report, run_log)
        return OracleAutoImportResult(
            success=success,
            state=state,
            message=message,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            run_id=run_id,
            run_dir=run_dir,
            plan=plan,
            report=report,
            run_log=run_log,
            command=_mask_command(command),
            preflight_checks=preflight_checks,
            timeline=timeline,
            log_manifest=log_manifest,
        )

    def stop(
        self,
        *,
        oracle_host: RemoteHost,
        run_dir: str,
        container: str,
        username: str,
        password: str,
        pdb: str,
        job_name: str,
        reason: str,
        force: bool = False,
    ) -> dict:
        if not run_dir or not job_name:
            raise ValueError("Oracle 导入运行信息尚未建立，暂时无法定位 Data Pump Job。")
        stop_file = str(PurePosixPath(run_dir) / "stop.request")
        flag_command = (
            f"mkdir -p {shlex.quote(run_dir)} && "
            f"printf '%s\\n' {shlex.quote(reason)} > {shlex.quote(stop_file)}"
        )
        flag_result = run_ssh_command(oracle_host, flag_command, timeout=30)
        if flag_result.returncode != 0:
            raise RuntimeError(flag_result.stderr or flag_result.stdout or "停止标记写入失败。")

        login = f"{username}/{password}{('@' + pdb) if pdb else ''}"
        action = "KILL_JOB" if force else "STOP_JOB=IMMEDIATE"
        attempts: list[dict] = []
        stopped_jobs: list[str] = []
        for candidate in oracle_datapump_job_candidates(job_name):
            input_text = f"{action}\nYES\n"
            inner = (
                "export NLS_LANG=AMERICAN_AMERICA.AL32UTF8; export LANG=C.UTF-8; export LC_ALL=C.UTF-8; "
                f"printf %s {shlex.quote(input_text)} | "
                f"impdp {shlex.quote(login)} ATTACH={shlex.quote(candidate)}"
            )
            try:
                result = run_ssh_command(
                    oracle_host,
                    self._docker_exec(container, inner),
                    timeout=20,
                )
                output = mask_sensitive((result.stdout or "") + "\n" + (result.stderr or ""))[-2000:]
                attempts.append({"job_name": candidate, "returncode": result.returncode, "output": output})
                if result.returncode == 0:
                    stopped_jobs.append(candidate)
            except Exception as exc:
                attempts.append({"job_name": candidate, "returncode": -1, "output": mask_sensitive(str(exc))})

        process_signal_sent = False
        if force:
            lock_path = str(PurePosixPath(run_dir) / ".active.lock")
            signal_command = (
                f"pid=$(sed -n 's/^pid=\\([0-9][0-9]*\\).*/\\1/p' {shlex.quote(lock_path)} 2>/dev/null | head -1); "
                "if test -n \"$pid\" && kill -0 \"$pid\" 2>/dev/null; then "
                "pkill -TERM -P \"$pid\" 2>/dev/null || true; kill -TERM \"$pid\" 2>/dev/null || true; printf signalled; "
                "else printf inactive; fi"
            )
            signal_result = run_ssh_command(oracle_host, signal_command, timeout=30)
            process_signal_sent = "signalled" in (signal_result.stdout or "")

        return {
            "stop_file": stop_file,
            "action": action,
            "job_name": job_name,
            "stopped_jobs": stopped_jobs,
            "process_signal_sent": process_signal_sent,
            "attempts": attempts,
        }

    def preflight(
        self,
        *,
        oracle_host: RemoteHost,
        group: DumpVolumeGroup,
        dmp_host_path: str,
        dmp_container_path: str,
        container: str,
        target: TargetDatabase,
        oracle_home_in_container: str | None,
        oracle_directory: str | None,
        manual_dumpfile: str | None = None,
        export_log: dict | None = None,
        execute: bool = True,
    ) -> list[dict]:
        checks: list[OraclePreflightCheck] = []
        python_bin = ""

        self._append_preflight(
            checks,
            code="ssh",
            name="Oracle 宿主机 SSH",
            suggestion="检查 Oracle Docker 宿主机 IP、端口、账号密码和防火墙。",
            fn=lambda: self._check_ssh(oracle_host),
        )
        self._append_preflight(
            checks,
            code="python",
            name="宿主机 Python 3.7+",
            suggestion="确保 Oracle Docker 宿主机的 python3 可执行，版本不低于 Python 3.7。",
            fn=lambda: self._check_python(oracle_host),
        )
        if checks[-1].state == "passed":
            python_bin = str(checks[-1].detail.get("python_bin") or "")
        self._append_preflight(
            checks,
            code="script_compile",
            name="自动导入脚本兼容性",
            suggestion="确认服务包内 oracle_dmp_auto_import.py 已发布，并能被远端 Python 编译。",
            fn=lambda: self._check_script(oracle_host, python_bin),
        )
        self._append_preflight(
            checks,
            code="docker_container",
            name="Oracle Docker 容器",
            suggestion="检查容器名是否正确，容器是否已启动，必要时先启动 Oracle 容器。",
            fn=lambda: self._check_docker_container(oracle_host, container),
        )
        self._append_preflight(
            checks,
            code="oracle_tools",
            name="容器内 sqlplus/impdp",
            suggestion="检查 Oracle 镜像环境变量和 ORACLE_HOME，确保 sqlplus、impdp 在容器 PATH 中。",
            fn=lambda: self._check_oracle_tools(oracle_host, container, oracle_home_in_container),
        )
        self._append_preflight(
            checks,
            code="host_dmp_files",
            name="宿主机 DMP 文件",
            suggestion="检查 DMP 目录和 DUMPFILE 模式，例如分卷文件使用 cqdsj_20260701_180002_%U.dmp。",
            fn=lambda: self._check_host_dump_files(oracle_host, python_bin, group, dmp_host_path, manual_dumpfile),
        )
        if export_log:
            self._append_preflight(
                checks,
                code="export_log_binding",
                name="Oracle 导出日志专项导入",
                suggestion="确认导出日志与当前 DMP 分卷属于同一批次；源端缺失对象需要显式授权后才能正式导入。",
                fn=lambda: self._check_export_log_binding(
                    export_log,
                    group,
                    execute=execute,
                ),
            )
        self._append_preflight(
            checks,
            code="container_dmp_path",
            name="容器内 DMP 目录",
            suggestion="检查宿主机 DMP 目录是否正确挂载到 Oracle 容器内目录。",
            fn=lambda: self._check_container_path(oracle_host, container, dmp_container_path),
        )
        self._append_preflight(
            checks,
            code="pdb",
            name="Oracle PDB 连接",
            suggestion="检查目标连接串的 service/PDB、SYSTEM 密码和 PDB 是否 READ WRITE。",
            fn=lambda: self._check_pdb(oracle_host, container, target, oracle_home_in_container),
        )
        self._append_preflight(
            checks,
            code="directory_policy",
            name="DIRECTORY 路径策略",
            suggestion="直读模式复用路径一致的 DMP DIRECTORY，运行日志使用独立工作 DIRECTORY。",
            fn=lambda: self._check_directory_policy(
                oracle_directory,
                dmp_container_path,
                direct_import=bool(manual_dumpfile),
            ),
        )
        return [item.to_dict() for item in checks]

    def _append_preflight(
        self,
        checks: list[OraclePreflightCheck],
        *,
        code: str,
        name: str,
        suggestion: str,
        fn,
    ) -> None:
        if code in self._skip_preflight_codes():
            if code in ESSENTIAL_PREFLIGHT_CODES:
                checks.append(
                    OraclePreflightCheck(
                        code=code,
                        name=name,
                        state="failed",
                        message="This preflight check is required for Oracle auto import and cannot be skipped.",
                        suggestion=suggestion,
                    )
                )
                return
            checks.append(
                OraclePreflightCheck(
                    code=code,
                    name=name,
                    state="warning",
                    message="Skipped by ORACLE_AUTO_IMPORT_SKIP_PREFLIGHT_CODES. The import may still fail later if this item is invalid.",
                    detail={"skipped": True},
                    suggestion=suggestion,
                )
            )
            return

        retries = max(0, get_settings().oracle_auto_import_preflight_retries)
        delay_seconds = max(0, get_settings().oracle_auto_import_preflight_retry_delay_seconds)
        attempts = retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                message, detail = fn()
                if attempt > 1:
                    detail = dict(detail or {})
                    detail["attempt"] = attempt
                    detail["retries"] = retries
                checks.append(OraclePreflightCheck(code=code, name=name, state="passed", message=message, detail=detail, suggestion=suggestion))
                return
            except Exception as exc:
                last_exc = exc
                if attempt < attempts and delay_seconds:
                    time.sleep(delay_seconds)

        checks.append(
            OraclePreflightCheck(
                code=code,
                name=name,
                state="failed",
                message=str(last_exc) if last_exc else "Preflight check failed.",
                detail={"attempts": attempts, "retries": retries},
                suggestion=suggestion,
            )
        )

    def _skip_preflight_codes(self) -> set[str]:
        configured = get_settings().oracle_auto_import_skip_preflight_codes
        return {item.strip() for item in configured.split(",") if item.strip()}

    def _check_ssh(self, host: RemoteHost) -> tuple[str, dict]:
        result = run_ssh_command(host, "printf oracle-preflight-ok", timeout=get_settings().oracle_ssh_check_timeout_seconds)
        if result.returncode != 0 or "oracle-preflight-ok" not in result.stdout:
            raise RuntimeError((result.stderr or result.stdout or "SSH command failed").strip())
        return "SSH 可连接。", {"returncode": result.returncode}

    def _check_python(self, host: RemoteHost) -> tuple[str, dict]:
        python_bin = self._remote_python(host)
        result = run_ssh_command(
            host,
            f"{shlex.quote(python_bin)} -c 'import sys; print(\"%d.%d.%d\" % sys.version_info[:3])'",
            timeout=get_settings().oracle_ssh_check_timeout_seconds,
        )
        version = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else "unknown"
        return f"远端 Python 可用：{python_bin} ({version})。", {"python_bin": python_bin, "version": version}

    def _check_script(self, host: RemoteHost, python_bin: str) -> tuple[str, dict]:
        if not python_bin:
            raise RuntimeError("缺少可用的 Python 3.7+，无法编译自动导入脚本。")
        self._install_script(host)
        remote_script = str(PurePosixPath(REMOTE_TOOL_DIR) / "oracle_dmp_auto_import.py")
        self._verify_remote_script(host, python_bin, remote_script)
        return "自动导入脚本已上传并通过 py_compile。", {"remote_script": remote_script}

    def _check_docker_container(self, host: RemoteHost, container: str) -> tuple[str, dict]:
        command = (
            f"docker inspect --format "
            f"{shlex.quote('{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}')} "
            f"{shlex.quote(container)}"
        )
        result = run_ssh_command(host, command, timeout=get_settings().oracle_ssh_check_timeout_seconds)
        output = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(output or f"容器不存在：{container}")
        parts = output.split()
        running = parts[0].lower() == "true" if parts else False
        health = parts[1] if len(parts) > 1 else ""
        if not running:
            raise RuntimeError(f"容器未运行：{container}，状态={output}")
        return "Oracle 容器已运行。", {"container": container, "inspect": output, "health": health}

    def _check_oracle_tools(self, host: RemoteHost, container: str, oracle_home: str | None) -> tuple[str, dict]:
        prefix = self._oracle_env_prefix(oracle_home)
        inner = (
            prefix
            + 'printf "ORACLE_HOME=%s\\n" "${ORACLE_HOME:-}"; '
            + "command -v sqlplus && command -v impdp && sqlplus -V"
        )
        result = run_ssh_command(
            host,
            self._docker_exec(container, inner),
            timeout=get_settings().oracle_ssh_check_timeout_seconds,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        upper = output.upper()
        if result.returncode != 0 or "SP2-" in upper or "ERROR 6 INITIALIZING SQL*PLUS" in upper:
            raise RuntimeError(mask_sensitive(output or "sqlplus/impdp not found"))
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        resolved_home = ""
        if lines and lines[0].startswith("ORACLE_HOME="):
            resolved_home = lines.pop(0).split("=", 1)[1]
        return "sqlplus 和 impdp 已使用容器真实 ORACLE_HOME 启动验证。", {
            "oracle_home": resolved_home,
            "paths_and_version": lines,
        }

    def _check_host_dump_files(
        self,
        host: RemoteHost,
        python_bin: str,
        group: DumpVolumeGroup,
        dmp_host_path: str,
        manual_dumpfile: str | None,
    ) -> tuple[str, dict]:
        if not python_bin:
            raise RuntimeError("缺少可用的 Python 3.7+，无法检查 DMP 文件模式。")
        if manual_dumpfile:
            code = (
                "import glob, os, sys; "
                "base=sys.argv[1]; pattern=sys.argv[2].replace('%U','*'); "
                "files=sorted(glob.glob(os.path.join(base, pattern))); "
                "print('\\n'.join(os.path.basename(x) for x in files[:50])); "
                "raise SystemExit(0 if files else 1)"
            )
            command = " ".join([shlex.quote(python_bin), "-c", shlex.quote(code), shlex.quote(dmp_host_path), shlex.quote(manual_dumpfile)])
            result = run_ssh_command(host, command, timeout=get_settings().oracle_import_operation_timeout_seconds)
            files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if result.returncode != 0 or not files:
                raise RuntimeError(f"目录 {dmp_host_path} 下没有匹配 DUMPFILE={manual_dumpfile} 的 DMP 文件。")
            return f"匹配到 {len(files)} 个 DMP 文件。", {"dmp_host_path": dmp_host_path, "manual_dumpfile": manual_dumpfile, "sample_files": files[:20]}

        names = [item.filename for item in sorted(group.dump_files, key=lambda item: item.filename)]
        if not names:
            raise RuntimeError("当前分组没有 DMP 文件。")
        code = (
            "import os, sys; "
            "base=sys.argv[1]; names=sys.argv[2:]; "
            "missing=[n for n in names if not os.path.isfile(os.path.join(base,n))]; "
            "print('\\n'.join(missing)); "
            "raise SystemExit(1 if missing else 0)"
        )
        command = " ".join([shlex.quote(python_bin), "-c", shlex.quote(code), shlex.quote(dmp_host_path), *(shlex.quote(name) for name in names)])
        result = run_ssh_command(host, command, timeout=get_settings().oracle_import_operation_timeout_seconds)
        if result.returncode != 0:
            missing = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            raise RuntimeError(f"宿主机 DMP 目录缺少文件：{', '.join(missing[:10])}")
        return f"宿主机 DMP 目录可读，确认 {len(names)} 个 DMP 文件。", {"dmp_host_path": dmp_host_path, "sample_files": names[:20]}

    def _check_export_log_binding(
        self,
        export_log: dict,
        group: DumpVolumeGroup,
        *,
        execute: bool,
    ) -> tuple[str, dict]:
        binding = export_log.get("binding") or {}
        manifest = export_log.get("manifest") or {}
        if binding.get("state") != "exact":
            raise RuntimeError("Oracle 导出日志与当前 DMP 分卷不是唯一精确匹配。")
        if manifest.get("tool") != "expdp":
            raise RuntimeError("专项导入第一阶段只接受已识别的 expdp 导出日志。")
        if manifest.get("source_status") not in {"clean_success", "completed_with_errors"}:
            raise RuntimeError(
                f"Oracle 导出日志状态不可用于专项导入：{manifest.get('source_status') or 'unknown'}"
            )

        actual = {item.filename.lower() for item in group.dump_files}
        declared = {str(name).lower() for name in manifest.get("dump_files") or []}
        if not declared or actual != declared:
            raise RuntimeError(
                "导出日志声明的 DMP 分卷与任务实际分卷不一致。"
                f" declared={sorted(declared)} actual={sorted(actual)}"
            )

        missing_count = int(manifest.get("missing_object_count") or 0)
        if execute and missing_count and not export_log.get("accept_source_gaps"):
            raise RuntimeError(
                f"源导出日志记录了 {missing_count} 个缺失对象，尚未授权接受源导出缺口。"
            )
        detail = {
            "filename": export_log.get("filename"),
            "content_sha256": manifest.get("content_sha256"),
            "source_status": manifest.get("source_status"),
            "export_mode": manifest.get("export_mode"),
            "dump_file_count": len(declared),
            "source_schemas": manifest.get("source_schemas") or [],
            "missing_object_count": missing_count,
            "accept_source_gaps": bool(export_log.get("accept_source_gaps")),
        }
        return "导出日志与 DMP 分卷精确匹配，专项导入门禁通过。", detail

    def _check_container_path(self, host: RemoteHost, container: str, dmp_container_path: str) -> tuple[str, dict]:
        inner = f"test -d {shlex.quote(dmp_container_path)} && ls -1 {shlex.quote(dmp_container_path)} | head -50"
        result = run_ssh_command(
            host,
            self._docker_exec(container, inner),
            timeout=get_settings().oracle_ssh_check_timeout_seconds,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or f"容器内目录不可访问：{dmp_container_path}").strip())
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return "容器内 DMP 目录可访问。", {"dmp_container_path": dmp_container_path, "sample_files": files[:20]}

    def _check_pdb(self, host: RemoteHost, container: str, target: TargetDatabase, oracle_home: str | None) -> tuple[str, dict]:
        pdb = target.connection_string.rsplit("/", 1)[-1] if "/" in target.connection_string else ""
        if not pdb:
            raise RuntimeError("目标连接串缺少 PDB/service，例如 host:1521/ORCLPDB1。")
        login = f"{target.admin_user}/{target.admin_password}@{pdb}"
        sql = (
            "set heading off feedback off pages 0 verify off echo off\n"
            "select sys_context('USERENV','CON_NAME') || ':' || open_mode "
            "from v$pdbs where name = sys_context('USERENV','CON_NAME');\n"
            "exit\n"
        )
        inner = self._oracle_env_prefix(oracle_home) + f"printf %s {shlex.quote(sql)} | sqlplus -S {shlex.quote(login)}"
        result = run_ssh_command(
            host,
            self._docker_exec(container, inner),
            timeout=get_settings().oracle_ssh_check_timeout_seconds,
        )
        output = (result.stdout or result.stderr).strip()
        if result.returncode != 0 or "ORA-" in output or "SP2-" in output:
            raise RuntimeError(mask_sensitive(output or "PDB 连接失败。"))
        pdb_status = output.splitlines()[-1].strip() if output.splitlines() else ""
        con_name, _, open_mode = pdb_status.partition(":")
        if con_name.upper() == "CDB$ROOT":
            raise RuntimeError("当前连接落在 CDB$ROOT，自动导入必须在 PDB 中执行。")
        if open_mode.upper() != "READ WRITE":
            raise RuntimeError(f"PDB {con_name or pdb} 当前不是 READ WRITE，open_mode={open_mode or 'unknown'}。")
        return f"PDB 可连接且 READ WRITE：{con_name or pdb}。", {"pdb": pdb, "container_context": con_name, "open_mode": open_mode}

    def _check_directory_policy(
        self,
        oracle_directory: str | None,
        dmp_container_path: str,
        *,
        direct_import: bool,
    ) -> tuple[str, dict]:
        if direct_import:
            if not oracle_directory:
                raise RuntimeError("直接使用 Oracle DMP 目录时缺少 Oracle DIRECTORY 配置。")
            return "将复用共享 DMP DIRECTORY，并为日志创建独立工作 DIRECTORY。", {
                "dump_directory_object": oracle_directory,
                "dump_directory_path": dmp_container_path,
                "zero_copy": True,
                "directory_conflict_policy": "reuse_only_when_path_matches",
            }
        if oracle_directory:
            return "将使用指定 DIRECTORY，导入工具会在执行时校验并按策略处理。", {"oracle_directory": oracle_directory}
        return "将为本次任务创建独立 DIRECTORY，路径位于受控自动导入目录。", {
            "directory_object": "auto-generated",
            "base_container_path": dmp_container_path.rstrip("/") + "/auto_import",
        }

    def _docker_exec(self, container: str, inner_shell: str) -> str:
        return f"docker exec -i {shlex.quote(container)} bash -lc {shlex.quote(inner_shell)}"

    def _oracle_env_prefix(self, oracle_home: str | None) -> str:
        q_home = shlex.quote((oracle_home or "").strip())
        return (
            f"configured_oracle_home={q_home}; "
            "oracle_home_valid() { "
            "home=\"$1\"; "
            "[ -n \"$home\" ] && [ -x \"$home/bin/sqlplus\" ] && [ -x \"$home/bin/impdp\" ] && "
            "[ -d \"$home/sqlplus/mesg\" ] && "
            "find \"$home/sqlplus/mesg\" -maxdepth 1 -type f -name 'sp1*.msb' -print -quit 2>/dev/null | grep -q .; "
            "}; "
            "resolved_oracle_home=''; "
            "if oracle_home_valid \"$configured_oracle_home\"; then "
            "resolved_oracle_home=\"$configured_oracle_home\"; "
            "elif oracle_home_valid \"${ORACLE_HOME:-}\"; then "
            "resolved_oracle_home=\"$ORACLE_HOME\"; "
            "else "
            "for candidate in /opt/oracle/product/*/dbhome_* /opt/oracle/product/*/dbhome "
            "/u01/app/oracle/product/*/dbhome_* /u01/app/oracle/product/*/dbhome /opt/oracle/client; do "
            "if oracle_home_valid \"$candidate\"; then resolved_oracle_home=\"$candidate\"; break; fi; "
            "done; "
            "fi; "
            "if [ -z \"$resolved_oracle_home\" ]; then "
            "sqlplus_path=$(command -v sqlplus 2>/dev/null || true); "
            "if [ -n \"$sqlplus_path\" ]; then "
            "sqlplus_path=$(readlink -f \"$sqlplus_path\" 2>/dev/null || printf '%s' \"$sqlplus_path\"); "
            "candidate=$(dirname \"$(dirname \"$sqlplus_path\")\"); "
            "if oracle_home_valid \"$candidate\"; then resolved_oracle_home=\"$candidate\"; fi; "
            "fi; "
            "fi; "
            "if [ -n \"$resolved_oracle_home\" ]; then "
            "export ORACLE_HOME=\"$resolved_oracle_home\"; "
            "export PATH=\"$ORACLE_HOME/bin:$PATH\"; "
            "export LD_LIBRARY_PATH=\"$ORACLE_HOME/lib:${LD_LIBRARY_PATH:-}\"; "
            "else unset ORACLE_HOME; fi; "
            "export NLS_LANG=AMERICAN_AMERICA.AL32UTF8; export LANG=C.UTF-8; export LC_ALL=C.UTF-8; "
        )

    def _install_script(self, host: RemoteHost) -> None:
        if not SCRIPT_LOCAL_PATH.exists():
            raise FileNotFoundError(f"Oracle auto import script not found: {SCRIPT_LOCAL_PATH}")
        ensure_remote_directory(host, REMOTE_TOOL_DIR, mode="755")
        remote_path = shlex.quote(str(PurePosixPath(REMOTE_TOOL_DIR) / "oracle_dmp_auto_import.py"))
        command = f"cat > {remote_path} <<'PYEOF'\n{SCRIPT_LOCAL_PATH.read_text(encoding='utf-8')}\nPYEOF\nchmod 755 {remote_path}"
        result = run_ssh_command(host, command, timeout=get_settings().oracle_ssh_check_timeout_seconds)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "failed to install Oracle auto import script")

    def _remote_python(self, host: RemoteHost) -> str:
        configured = get_settings().oracle_auto_import_python_bin.strip()
        candidates = [
            configured,
            "python3",
            "python3.12",
            "python3.11",
            "python3.10",
            "python3.9",
            "python3.8",
            "python3.7",
            "python",
            "/usr/local/bin/python3",
            "/usr/local/bin/python3.12",
            "/usr/local/bin/python3.11",
            "/usr/local/bin/python3.10",
            "/usr/local/bin/python3.9",
            "/usr/local/bin/python3.8",
            "/usr/local/bin/python3.7",
            "/usr/local/python3.8.18/bin/python3.8",
            "/usr/local/python3.8/bin/python3.8",
            "/usr/local/python3.7/bin/python3.7",
            "/opt/python3.8/bin/python3.8",
            "/opt/python3.7/bin/python3.7",
        ]
        unique_candidates = []
        for candidate in candidates:
            if candidate and candidate not in unique_candidates:
                unique_candidates.append(candidate)
        quoted_candidates = " ".join(shlex.quote(candidate) for candidate in unique_candidates)
        command = f"""
tried=''
for candidate in {quoted_candidates}; do
  case "$candidate" in
    /*)
      path="$candidate"
      [ -x "$path" ] || {{ tried="${{tried}}${{candidate}} -> not executable\\n"; continue; }}
      ;;
    *)
      path=$(command -v "$candidate" 2>/dev/null) || {{ tried="${{tried}}${{candidate}} -> not found\\n"; continue; }}
      ;;
  esac
  version=$("$path" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3]); raise SystemExit(0 if sys.version_info >= (3, 7) else 1)' 2>&1)
  status=$?
  if [ "$status" -eq 0 ]; then
    echo "$path"
    exit 0
  fi
  tried="${{tried}}${{path}} -> ${{version}}\\n"
done
printf '%b' "$tried" >&2
exit 1
""".strip()
        result = run_ssh_command(
            host,
            command,
            timeout=get_settings().oracle_ssh_check_timeout_seconds,
        )
        if result.returncode != 0 or not result.stdout.strip():
            detail = (result.stderr or result.stdout or "no candidate matched").strip()
            raise RuntimeError(f"Python 3.7+ is required on the Oracle Docker host to run Oracle auto import. Checked: {detail}")
        return result.stdout.strip().splitlines()[0]

    def _verify_remote_script(self, host: RemoteHost, python_bin: str, remote_script: str) -> None:
        command = " ".join(
            [
                shlex.quote(python_bin),
                "-m",
                "py_compile",
                shlex.quote(remote_script),
            ]
        )
        result = run_ssh_command(host, command, timeout=get_settings().oracle_ssh_check_timeout_seconds)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "remote py_compile failed").strip()
            raise RuntimeError(f"Oracle auto import script is not compatible with remote Python: {detail}")

    def _dump_args(
        self,
        group: DumpVolumeGroup,
        dmp_host_path: str,
        *,
        manual_dumpfile: str | None = None,
    ) -> list[str]:
        if manual_dumpfile:
            return ["--dump-dir", dmp_host_path, "--dumpfile", PurePosixPath(manual_dumpfile).name]
        dumpfiles, use_percent_u = choose_dumpfiles(group, "")
        if use_percent_u and dumpfiles:
            return ["--dump-dir", dmp_host_path, "--dumpfile", dumpfiles[0]]
        dumps = sorted(group.dump_files, key=lambda item: item.filename)
        if not dumps:
            raise ValueError("No DMP file found for Oracle auto import.")
        return ["--dump", str(PurePosixPath(dmp_host_path) / dumps[0].filename)]

    def _remote_command(self, args: list[str], oracle_home: str | None) -> str:
        # ORACLE_HOME belongs to the Oracle container, not the Docker host that runs this Python tool.
        return " ".join(shlex.quote(part) for part in args if part != "")

    def _read_json(self, host: RemoteHost, path: str) -> dict:
        text = self._read_text(host, path)
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def _read_text(self, host: RemoteHost, path: str, max_bytes: int = 2_000_000) -> str:
        try:
            return read_remote_text(host, path, max_bytes=max_bytes)
        except Exception:
            return ""

    def _read_json_lines(self, host: RemoteHost, path: str) -> list[dict]:
        items: list[dict] = []
        for line in self._read_text(host, path, max_bytes=20_000_000).splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                items.append(item)
        return items

    def _archive_export_log(
        self,
        host: RemoteHost,
        *,
        source_path: str,
        filename: str,
        run_dir: str,
    ) -> None:
        safe_name = PurePosixPath(filename).name
        if not source_path or not safe_name.lower().endswith(".log"):
            return
        target_dir = str(PurePosixPath(run_dir) / "source_export")
        target_path = str(PurePosixPath(target_dir) / safe_name)
        command = (
            f"mkdir -p {shlex.quote(target_dir)} && "
            f"test -f {shlex.quote(source_path)} && "
            f"cp -- {shlex.quote(source_path)} {shlex.quote(target_path)}"
        )
        try:
            result = run_ssh_command(
                host,
                command,
                timeout=get_settings().oracle_ssh_check_timeout_seconds,
            )
        except Exception:
            return
        if result.returncode != 0:
            return

    def _list_log_artifacts(self, host: RemoteHost, run_dir: str) -> list[dict]:
        command = (
            f"find {shlex.quote(run_dir)} -type f "
            r"-printf '%P\t%s\t%T@\n' 2>/dev/null"
        )
        try:
            result = run_ssh_command(host, command, timeout=30)
        except Exception:
            return []
        if result.returncode != 0:
            return []

        artifacts: list[dict] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            relative_path, size_text, modified_text = parts
            relative = PurePosixPath(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            try:
                size_bytes = int(size_text)
                modified_epoch = float(modified_text)
            except ValueError:
                continue
            artifacts.append(
                {
                    "relative_path": relative.as_posix(),
                    "remote_path": str(PurePosixPath(run_dir) / relative),
                    "size_bytes": size_bytes,
                    "modified_epoch": modified_epoch,
                    "kind": self._artifact_kind(relative.as_posix()),
                }
            )
        return sorted(artifacts, key=lambda item: item["relative_path"])

    @staticmethod
    def _artifact_kind(relative_path: str) -> str:
        if relative_path == "run.log":
            return "command_log"
        if relative_path == "timeline.jsonl":
            return "timeline"
        if relative_path.endswith(".json"):
            return "report"
        if relative_path.startswith("probe/"):
            return "probe"
        if relative_path.startswith("import/"):
            return "import"
        if relative_path.startswith("cleanup/"):
            return "cleanup"
        if relative_path.startswith("source_export/"):
            return "source_export"
        return "log"

    def _message(self, result, report: dict, run_log: str) -> str:
        if report.get("status") == "imported":
            return "Oracle auto import completed."
        if report.get("status") == "dry-run":
            return "Oracle auto import dry-run completed."
        if report.get("error"):
            return str(report["error"])
        for line in reversed(run_log.splitlines()):
            if "[error]" in line or "[stop]" in line:
                return line[-1000:]
        return (result.stderr or result.stdout or f"Oracle auto import exited with {result.returncode}")[-2000:]


def _mask_command(command: str) -> str:
    text = mask_sensitive(command)
    return text.replace("--password " + shlex.quote(get_settings().oracle_pwd), "--password ******")
