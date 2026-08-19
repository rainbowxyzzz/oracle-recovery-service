#!/usr/bin/env python3
"""
Oracle DMP auto-probe/import tool for a Docker-based recovery warehouse.

Design goals from ORACLE_DMP_IMPORT_PRD.md:
  - Default dry-run: probe and write a plan, do not import business data.
  - Execute only with --execute.
  - Run sqlplus/impdp/imp inside the Oracle Docker container.
  - Generate a per-run log directory.
  - Use generated target names from dump file + timestamp unless overridden.
  - Recreate conflicting non-system targets by default for the dedicated
    recovery warehouse.
  - Create target business tablespaces as BIGFILE AUTOEXTEND tablespaces.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


SYSTEM_USERS = {
    "ANONYMOUS",
    "APEX_PUBLIC_USER",
    "CTXSYS",
    "DBSNMP",
    "DIP",
    "EXFSYS",
    "FLOWS_FILES",
    "MDDATA",
    "MDSYS",
    "MGMT_VIEW",
    "OLAPSYS",
    "ORDDATA",
    "ORDPLUGINS",
    "ORDSYS",
    "OUTLN",
    "OWBSYS",
    "SI_INFORMTN_SCHEMA",
    "SYS",
    "SYSMAN",
    "SYSTEM",
    "WMSYS",
    "XDB",
    "XS$NULL",
}

SYSTEM_TABLESPACES = {"SYSTEM", "SYSAUX", "TEMP", "UNDOTBS1"}

SCHEMA_PARSE_EXCLUDES = SYSTEM_USERS | {
    "DATABASE_EXPORT",
    "EXPORT",
    "IMPORT",
    "JOB",
    "OBJECT",
    "PROCESSING",
    "SCHEMA_EXPORT",
    "TABLE_EXPORT",
    "TYPE",
}

TABLESPACE_PARSE_EXCLUDES = SYSTEM_TABLESPACES | {"TO"}


class RunAlreadyActiveError(RuntimeError):
    pass


class ImportStopRequested(RuntimeError):
    pass


def acquire_run_lock(run_dir: Path):
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("The Oracle auto-import run lock requires a POSIX host with fcntl.") from exc

    lock_path = run_dir / ".active.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.seek(0)
        owner = handle.read().strip() or "owner details unavailable"
        handle.close()
        raise RunAlreadyActiveError(
            f"Oracle auto-import run {run_dir.name} is already active ({owner})."
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started_at={dt.datetime.now().isoformat()}\n")
    handle.flush()
    return handle


def release_run_lock(handle) -> None:
    if handle is None:
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@dataclass
class DumpSpec:
    source_dir: str
    source_files: List[str]
    dumpfile_arg: str
    display_name: str
    is_dump_set: bool


@dataclass
class ProbeResult:
    dump_type: str
    schemas: List[str] = field(default_factory=list)
    tablespaces: List[str] = field(default_factory=list)
    datafiles: List[str] = field(default_factory=list)
    exclude_object_types: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    probe_log: Optional[str] = None
    sqlfile: Optional[str] = None
    failure_code: Optional[str] = None
    attempts: List[Dict[str, object]] = field(default_factory=list)


@dataclass
class RuntimeContext:
    run_id: str
    run_dir: str
    probe_dir: str
    cleanup_dir: str
    import_dir: str
    local_plan_path: str
    container_import_dir: str
    dumpfile_arg: str
    dump_display_name: str
    container_dump_dir: str = ""
    zero_copy_dump: bool = False


@dataclass
class ImportPlan:
    run: RuntimeContext
    dump_type: str
    probe_failure_code: Optional[str]
    probe_attempts: List[Dict[str, object]]
    container: str
    directory_object: str
    dump_directory_object: str
    dumpfile_arg: str
    dump_source_files: List[str]
    source_schemas: List[str]
    source_tablespaces: List[str]
    excluded_object_types: List[str]
    schema_map: Dict[str, str]
    tablespace_map: Dict[str, str]
    target_users: List[str]
    target_tablespaces: List[str]
    target_datafile_dir: str
    target_datafiles: Dict[str, str]
    on_conflict: str
    table_exists_action: str
    commands: List[List[str]]
    masked_commands: List[str]
    fallback_commands: List[List[str]] = field(default_factory=list)
    masked_fallback_commands: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    export_log_assisted: bool = False
    export_log_summary: Dict[str, object] = field(default_factory=dict)
    job_name: str = ""


@dataclass
class ImportOutcome:
    status: str = "imported"
    warnings: List[str] = field(default_factory=list)
    repaired_objects: List[str] = field(default_factory=list)
    invalid_objects: List[str] = field(default_factory=list)


class RunLogger:
    def __init__(self, run_dir: Path, username: str, password: str):
        self.run_dir = run_dir
        self.username = username
        self.password = password
        self.log_path = run_dir / "run.log"
        self.timeline_path = run_dir / "timeline.jsonl"
        self.command_seq = 0
        self.current_stage: Optional[str] = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def mask(self, text: str) -> str:
        masked = text
        if self.password:
            masked = masked.replace(f"{self.username}/{self.password}", f"{self.username}/******")
        masked = re.sub(
            r'(?i)(IDENTIFIED\s+BY\s+)("[^"]*"|\'[^\']*\'|\S+)',
            r'\1"******"',
            masked,
        )
        masked = re.sub(r"(?i)(--password\s+)(\S+)", r"\1******", masked)
        masked = re.sub(r"(?i)(PASSWORD=)(\S+)", r"\1******", masked)
        return masked

    def _write(self, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(self.mask(line) + "\n")

    def event(self, event_type: str, name: str, status: str, **detail: object) -> None:
        payload = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "event_type": event_type,
            "name": name,
            "status": status,
            "detail": detail,
        }
        with self.timeline_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        print("@@ORACLE_EVENT@@" + self.mask(json.dumps(payload, ensure_ascii=False)), flush=True)

    def stage(self, name: str, status: str, message: str = "", **detail: object) -> None:
        self.event("stage", name, status, message=message, **detail)
        self.log(f"[stage:{name}] {status}{': ' + message if message else ''}")
        if status == "running":
            self.current_stage = name
        elif self.current_stage == name:
            self.current_stage = None

    def fail_active_stage(self, message: str) -> None:
        if self.current_stage:
            self.stage(self.current_stage, "failed", message)

    def log(self, message: str, *, console: bool = True) -> None:
        stamped = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
        self._write(stamped)
        if console:
            print(self.mask(message))

    def start_command(self, cmd: List[str], input_text: Optional[str] = None) -> int:
        self.command_seq += 1
        command_id = self.command_seq
        display = " ".join(shlex.quote(part) for part in cmd)
        self.event("command", f"cmd:{command_id:03d}", "running", command=self.mask(display))
        self.log(f"[cmd:{command_id:03d}] BEGIN", console=False)
        self.log(f"[cmd:{command_id:03d}] command: {display}", console=False)
        if input_text is not None:
            self.log(f"[cmd:{command_id:03d}] stdin BEGIN", console=False)
            for line in input_text.rstrip().splitlines():
                self.log(f"[cmd:{command_id:03d}][stdin] {line}", console=False)
            self.log(f"[cmd:{command_id:03d}] stdin END", console=False)
        self.log(f"[cmd:{command_id:03d}] output BEGIN", console=False)
        return command_id

    def log_command_output(self, command_id: int, line: str) -> None:
        timestamp = dt.datetime.now().isoformat(timespec="seconds")
        self._write(f"{timestamp} [cmd:{command_id:03d}][output] {line}")

    def finish_command(self, command_id: int, elapsed: float, returncode: int, output_line_count: int) -> None:
        if output_line_count == 0:
            self.log(f"[cmd:{command_id:03d}][output] (no output)", console=False)
        self.log(f"[cmd:{command_id:03d}] output END", console=False)
        self.log(f"[cmd:{command_id:03d}] END exit={returncode} elapsed={elapsed:.2f}s output_lines={output_line_count}", console=False)
        self.event(
            "command",
            f"cmd:{command_id:03d}",
            "succeeded" if returncode == 0 else "failed",
            returncode=returncode,
            elapsed_seconds=round(elapsed, 3),
            output_lines=output_line_count,
            log_file="run.log",
        )


def stop_requested(logger: Optional[RunLogger]) -> bool:
    return bool(logger and (logger.run_dir / "stop.request").exists())


def ensure_not_stopped(logger: Optional[RunLogger]) -> None:
    if stop_requested(logger):
        raise ImportStopRequested("Oracle 导入已收到用户停止请求。")


def run_process(cmd: List[str], *, logger: Optional[RunLogger] = None, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    ensure_not_stopped(logger)
    command_id = logger.start_command(cmd, input_text) if logger else 0
    start = time.monotonic()
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    if input_text is not None and process.stdin is not None:
        try:
            process.stdin.write(input_text)
            process.stdin.close()
        except BrokenPipeError:
            pass

    output_lines: List[str] = []

    def read_output() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            cleaned = line.rstrip("\n")
            output_lines.append(cleaned)
            if logger:
                logger.log_command_output(command_id, cleaned)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    stopped = False
    while process.poll() is None:
        if stop_requested(logger):
            stopped = True
            if logger:
                logger.log("[stop] stop.request detected; terminating the active command process group")
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            break
        time.sleep(0.5)
    returncode = process.wait()
    reader.join(timeout=5)
    elapsed = time.monotonic() - start
    if logger:
        logger.finish_command(command_id, elapsed, returncode, len(output_lines))
    if stopped:
        raise ImportStopRequested("Oracle 导入已按用户请求停止。")
    return subprocess.CompletedProcess(cmd, returncode, "\n".join(output_lines))


def docker_exec(container: str, cmd: str, *, logger: Optional[RunLogger] = None, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    oracle_env = r"""
oracle_home_valid() {
  home="$1"
  [ -n "$home" ] && [ -x "$home/bin/sqlplus" ] && [ -x "$home/bin/impdp" ] &&
    [ -d "$home/sqlplus/mesg" ] &&
    find "$home/sqlplus/mesg" -maxdepth 1 -type f -name 'sp1*.msb' -print -quit 2>/dev/null | grep -q .
}
resolved_oracle_home=""
if oracle_home_valid "${ORACLE_HOME:-}"; then
  resolved_oracle_home="$ORACLE_HOME"
else
  for candidate in \
    /opt/oracle/product/*/dbhome_* \
    /opt/oracle/product/*/dbhome \
    /u01/app/oracle/product/*/dbhome_* \
    /u01/app/oracle/product/*/dbhome \
    /opt/oracle/client; do
    if oracle_home_valid "$candidate"; then
      resolved_oracle_home="$candidate"
      break
    fi
  done
fi
if [ -z "$resolved_oracle_home" ]; then
  sqlplus_path=$(command -v sqlplus 2>/dev/null || true)
  if [ -n "$sqlplus_path" ]; then
    sqlplus_path=$(readlink -f "$sqlplus_path" 2>/dev/null || printf '%s' "$sqlplus_path")
    candidate=$(dirname "$(dirname "$sqlplus_path")")
    if oracle_home_valid "$candidate"; then
      resolved_oracle_home="$candidate"
    fi
  fi
fi
if [ -n "$resolved_oracle_home" ]; then
  export ORACLE_HOME="$resolved_oracle_home"
  export PATH="$ORACLE_HOME/bin:$PATH"
  export LD_LIBRARY_PATH="$ORACLE_HOME/lib:${LD_LIBRARY_PATH:-}"
else
  unset ORACLE_HOME
fi
"""
    oracle_env = oracle_env.strip()
    wrapped = f"export NLS_LANG=AMERICAN_AMERICA.AL32UTF8; export LANG=C.UTF-8; {oracle_env}; {cmd}"
    return run_process(["docker", "exec", "-i", container, "bash", "-lc", wrapped], logger=logger, input_text=input_text)


def docker_cp_from(container: str, container_path: str, local_path: Path, logger: RunLogger) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_process(["docker", "cp", f"{container}:{container_path}", str(local_path)], logger=logger)
    if result.returncode != 0:
        logger.log(f"[warn] could not copy {container_path} from container: {result.stdout.strip()}")


def docker_cp_from_if_present(container: str, container_path: str, local_path: Path, logger: RunLogger) -> bool:
    check = docker_exec(
        container,
        f"if test -f {shlex.quote(container_path)}; then printf present; fi",
        logger=logger,
    )
    if check.returncode != 0 or "present" not in check.stdout:
        return False
    docker_cp_from(container, container_path, local_path, logger)
    return True


def quote_sql(value: str) -> str:
    return value.replace("'", "''")


def qident(value: str) -> str:
    return '"' + value.replace('"', '""').upper() + '"'


def normalize_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value.upper()).strip("_")
    return cleaned or "DMP"


def oracle_name(value: str, max_len: int = 30) -> str:
    cleaned = normalize_token(value)
    if cleaned[0].isdigit():
        cleaned = "O_" + cleaned
    return cleaned[:max_len]


def generated_directory_name(dump_display_name: str, run_id: str, max_len: int = 30) -> str:
    """Generate a readable Oracle identifier while preserving a task-unique suffix."""
    label = normalize_token(dump_display_name)[:8]
    digest = hashlib.sha256(f"{dump_display_name}|{run_id}".encode("utf-8")).hexdigest()[:12].upper()
    return oracle_name(f"DIR_{label}_{digest}", max_len=max_len)


def unique_sorted(values: Iterable[str], excludes: Optional[Set[str]] = None) -> List[str]:
    excludes = excludes or set()
    return sorted({v.upper() for v in values if v and v.upper() not in excludes})


def dumpfile_template_to_regex(template: str):
    escaped = re.escape(template.upper())
    escaped = escaped.replace(r"%U", r"\d{2,}")
    return re.compile(rf"^{escaped}$", re.IGNORECASE)


def resolve_dump_spec(dump_path: Path, dumpfile_template: Optional[str]) -> DumpSpec:
    source_dir = dump_path.parent
    template = Path(dumpfile_template).name if dumpfile_template else dump_path.name

    if "%U" not in template.upper():
        if dumpfile_template and template != dump_path.name:
            raise FileNotFoundError(
                "For a single dump file, --dumpfile must be omitted or equal to the --dump file name. "
                "Use --dump-dir with --dumpfile when you want to import by a Data Pump file pattern."
            )
        if not dump_path.is_file():
            raise FileNotFoundError(f"DMP file not found: {dump_path}")
        return DumpSpec(
            source_dir=str(source_dir),
            source_files=[dump_path.name],
            dumpfile_arg=template,
            display_name=dump_path.stem,
            is_dump_set=False,
        )

    if not source_dir.is_dir():
        raise FileNotFoundError(f"Dump source directory not found: {source_dir}")

    regex = dumpfile_template_to_regex(template)
    matches = sorted(
        path.name
        for path in source_dir.iterdir()
        if path.is_file() and regex.match(path.name)
    )
    if not matches:
        raise FileNotFoundError(f"No dump set files matching {template} under {source_dir}")

    return DumpSpec(
        source_dir=str(source_dir),
        source_files=matches,
        dumpfile_arg=template,
        display_name=template.replace("%U", "SET").rsplit(".", 1)[0],
        is_dump_set=True,
    )


def resolve_dump_inputs(args: argparse.Namespace) -> DumpSpec:
    if args.dump and args.dump_dir:
        raise FileNotFoundError("Use either --dump or --dump-dir, not both.")

    if args.dump_dir:
        source_dir = Path(args.dump_dir).expanduser().resolve()
        if not args.dumpfile:
            raise FileNotFoundError("--dump-dir requires --dumpfile, for example --dump-dir /data/expdp --dumpfile XX%U.DMP.")
        template = Path(args.dumpfile).name
        return resolve_dump_spec(source_dir / template, template)

    if args.dump:
        dump_path = Path(args.dump).expanduser().resolve()
        return resolve_dump_spec(dump_path, args.dumpfile or None)

    raise FileNotFoundError("Pass --dump for a single file, or --dump-dir with --dumpfile for a dump set.")


def ensure_safe_user(user: str) -> None:
    if user.upper() in SYSTEM_USERS:
        raise RuntimeError(f"Refusing to modify system user: {user}")


def ensure_safe_tablespace(tablespace: str) -> None:
    if tablespace.upper() in SYSTEM_TABLESPACES:
        raise RuntimeError(f"Refusing to modify system tablespace: {tablespace}")


def find_oracle_container(logger: Optional[RunLogger] = None) -> str:
    result = run_process(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"], logger=logger)
    if result.returncode != 0:
        raise RuntimeError("Unable to run docker ps. Is Docker running?")
    candidates: List[str] = []
    for line in result.stdout.splitlines():
        name, _, image = line.partition("\t")
        haystack = f"{name} {image}".lower()
        if "oracle" in haystack or "database" in haystack or "orcl" in haystack:
            candidates.append(name)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError("No Oracle-like container found. Pass --container explicitly.")
    raise RuntimeError(f"Multiple Oracle-like containers found: {', '.join(candidates)}. Pass --container explicitly.")


def sqlplus(container: str, username: str, password: str, connect: str, sql: str, logger: RunLogger) -> str:
    login = f"{username}/{password}{connect}"
    script = f"""
set heading off feedback off pagesize 0 verify off echo off termout on linesize 32767
whenever sqlerror exit sql.sqlcode
{sql}
exit
"""
    result = docker_exec(container, f"sqlplus -s {shlex.quote(login)}", logger=logger, input_text=script)
    if result.returncode != 0:
        raise RuntimeError(result.stdout)
    return result.stdout.strip()


def sql_count(container: str, username: str, password: str, connect: str, sql: str, logger: RunLogger) -> int:
    out = sqlplus(container, username, password, connect, sql, logger)
    numbers = re.findall(r"\d+", out)
    return int(numbers[-1]) if numbers else 0


def sql_first_value(container: str, username: str, password: str, connect: str, sql: str, logger: RunLogger) -> str:
    out = sqlplus(container, username, password, connect, sql, logger)
    return next((line.strip() for line in out.splitlines() if line.strip()), "")


def resolve_pdb_connect(args: argparse.Namespace, logger: RunLogger) -> None:
    if args.connect:
        try:
            con_name = sql_first_value(
                args.container,
                args.username,
                args.password,
                args.connect,
                "SELECT SYS_CONTEXT('USERENV','CON_NAME') FROM dual;",
                logger,
            ).upper()
        except Exception as exc:
            logger.log(f"[warn] could not validate explicit connect suffix {args.connect}: {exc}")
            logger.log("[warn] continuing because the database may be non-CDB or the context parameter may be unavailable")
            return
        if con_name == "CDB$ROOT":
            raise RuntimeError("Refusing to operate in CDB$ROOT. This tool must run in a PDB. Pass --connect @ORCLPDB1 or --pdb ORCLPDB1.")
        logger.log(f"[info] using explicit connect suffix: {args.connect}; container context: {con_name or '(unknown)'}")
        return

    try:
        con_name = sql_first_value(
            args.container,
            args.username,
            args.password,
            "",
            "SELECT SYS_CONTEXT('USERENV','CON_NAME') FROM dual;",
            logger,
        ).upper()
    except Exception as exc:
        logger.log(f"[warn] could not detect CDB/PDB context; continuing without --connect: {exc}")
        return

    if con_name != "CDB$ROOT":
        logger.log(f"[info] connected container context: {con_name or '(unknown)'}")
        return

    pdb_name = args.pdb
    if not pdb_name:
        try:
            pdb_name = sql_first_value(
                args.container,
                args.username,
                args.password,
                "",
                """
SELECT name
FROM v$pdbs
WHERE open_mode = 'READ WRITE'
AND name <> 'PDB$SEED'
AND rownum = 1;
""",
                logger,
            )
        except Exception as exc:
            raise RuntimeError(f"Connected to CDB$ROOT but could not auto-detect an open PDB. Pass --connect @ORCLPDB1 or --pdb ORCLPDB1. Detail: {exc}") from exc

    if not pdb_name:
        raise RuntimeError("Connected to CDB$ROOT but no READ WRITE PDB was found. Pass --connect @your_pdb_service.")

    args.connect = f"@{pdb_name}"
    logger.log(f"[info] CDB$ROOT detected; switching to PDB connect suffix: {args.connect}")

    try:
        test_con = sql_first_value(
            args.container,
            args.username,
            args.password,
            args.connect,
            "SELECT SYS_CONTEXT('USERENV','CON_NAME') FROM dual;",
            logger,
        )
        logger.log(f"[info] PDB connection verified: {test_con}")
    except Exception as exc:
        raise RuntimeError(f"Could not connect to detected PDB with {args.connect}. Pass --connect explicitly, for example --connect @ORCLPDB1. Detail: {exc}") from exc


def user_exists(args: argparse.Namespace, user: str, logger: RunLogger) -> bool:
    sql = f"SELECT COUNT(*) FROM dba_users WHERE username = '{quote_sql(user.upper())}';"
    return sql_count(args.container, args.username, args.password, args.connect, sql, logger) > 0


def tablespace_exists(args: argparse.Namespace, tablespace: str, logger: RunLogger) -> bool:
    sql = f"SELECT COUNT(*) FROM dba_tablespaces WHERE tablespace_name = '{quote_sql(tablespace.upper())}';"
    return sql_count(args.container, args.username, args.password, args.connect, sql, logger) > 0


def directory_path(args: argparse.Namespace, directory_object: str, logger: RunLogger) -> str:
    sql = f"""
SELECT directory_path
FROM dba_directories
WHERE directory_name = '{quote_sql(directory_object.upper())}';
"""
    return sql_first_value(args.container, args.username, args.password, args.connect, sql, logger)


def create_directory(args: argparse.Namespace, directory_object: str, container_dir: str, logger: RunLogger) -> None:
    sql = f"""
CREATE OR REPLACE DIRECTORY {qident(directory_object)} AS '{quote_sql(container_dir)}';
BEGIN
  EXECUTE IMMEDIATE 'GRANT READ, WRITE ON DIRECTORY {qident(directory_object)} TO {qident(args.username)}';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE != -1749 THEN
      RAISE;
    END IF;
END;
/
"""
    sqlplus(args.container, args.username, args.password, args.connect, sql, logger)


def grant_directory_access(args: argparse.Namespace, directory_object: str, logger: RunLogger) -> None:
    sql = f"""
BEGIN
  EXECUTE IMMEDIATE 'GRANT READ, WRITE ON DIRECTORY {qident(directory_object)} TO {qident(args.username)}';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE != -1749 THEN
      RAISE;
    END IF;
END;
/
"""
    sqlplus(args.container, args.username, args.password, args.connect, sql, logger)


def verify_directory(args: argparse.Namespace, directory_object: str, container_dir: str, logger: RunLogger) -> None:
    actual_path = directory_path(args, directory_object, logger)
    if actual_path.rstrip("/") != container_dir.rstrip("/"):
        raise RuntimeError(
            f"DIRECTORY verification failed: {directory_object} points to "
            f"{actual_path or '(missing)'}, expected {container_dir}."
        )

    path_check = docker_exec(
        args.container,
        f"test -d {shlex.quote(container_dir)} && test -r {shlex.quote(container_dir)}",
        logger=logger,
    )
    if path_check.returncode != 0:
        raise RuntimeError(
            f"DIRECTORY verification failed: container path is missing or unreadable: {container_dir}."
        )

    marker = f".oracle_recovery_directory_check_{hashlib.sha256(directory_object.encode('utf-8')).hexdigest()[:12]}.tmp"
    sql = f"""
DECLARE
  file_handle UTL_FILE.FILE_TYPE;
BEGIN
  file_handle := UTL_FILE.FOPEN('{quote_sql(directory_object.upper())}', '{marker}', 'W');
  UTL_FILE.PUT_LINE(file_handle, 'oracle recovery directory write check');
  UTL_FILE.FCLOSE(file_handle);
  UTL_FILE.FREMOVE('{quote_sql(directory_object.upper())}', '{marker}');
END;
/
"""
    try:
        sqlplus(args.container, args.username, args.password, args.connect, sql, logger)
    except Exception as exc:
        raise RuntimeError(
            f"DIRECTORY verification failed: Oracle could not write through {directory_object}: {exc}"
        ) from exc
    logger.log(f"[verify] DIRECTORY {directory_object} -> {actual_path}; Oracle read/write check passed")


def verify_dump_directory(
    args: argparse.Namespace,
    directory_object: str,
    container_dir: str,
    source_files: List[str],
    logger: RunLogger,
) -> None:
    actual_path = directory_path(args, directory_object, logger)
    if actual_path.rstrip("/") != container_dir.rstrip("/"):
        raise RuntimeError(
            f"Shared dump DIRECTORY {directory_object} points to {actual_path or '(missing)'}, "
            f"expected {container_dir}. Refusing to replace an existing shared DIRECTORY."
        )

    path_check = docker_exec(
        args.container,
        f"test -d {shlex.quote(container_dir)} && test -r {shlex.quote(container_dir)}",
        logger=logger,
    )
    if path_check.returncode != 0:
        raise RuntimeError(f"Shared dump directory is missing or unreadable in the container: {container_dir}.")

    checks = []
    for file_name in source_files:
        checks.append(
            "UTL_FILE.FGETATTR("
            f"'{quote_sql(directory_object.upper())}', "
            f"'{quote_sql(file_name)}', l_exists, l_length, l_block_size);\n"
            f"IF NOT l_exists THEN RAISE_APPLICATION_ERROR(-20001, 'DMP file not visible: {quote_sql(file_name)}'); END IF;"
        )
    sql = f"""
DECLARE
  l_exists BOOLEAN;
  l_length NUMBER;
  l_block_size BINARY_INTEGER;
BEGIN
  {chr(10).join(checks)}
END;
/
"""
    sqlplus(args.container, args.username, args.password, args.connect, sql, logger)
    logger.log(
        f"[verify] shared dump DIRECTORY {directory_object} -> {actual_path}; "
        f"Oracle can see {len(source_files)} dump file(s)"
    )


def ensure_reusable_dump_directory(
    args: argparse.Namespace,
    dump_spec: DumpSpec,
    container_dir: str,
    logger: RunLogger,
) -> None:
    directory_object = args.dump_directory_object
    existing_path = directory_path(args, directory_object, logger)
    if existing_path:
        if existing_path.rstrip("/") != container_dir.rstrip("/"):
            raise RuntimeError(
                f"Shared dump DIRECTORY {directory_object} already exists at {existing_path}, "
                f"but the configured container path is {container_dir}. Refusing CREATE OR REPLACE."
            )
        logger.log(f"[reuse] shared dump DIRECTORY {directory_object} already points to {existing_path}")
        grant_directory_access(args, directory_object, logger)
    else:
        logger.log(f"[sql] create shared dump DIRECTORY {directory_object} -> {container_dir}")
        create_directory(args, directory_object, container_dir, logger)

    verify_dump_directory(args, directory_object, container_dir, dump_spec.source_files, logger)


def ensure_container_directory(args: argparse.Namespace, container_dir: str, logger: RunLogger) -> None:
    mkdir = docker_exec(args.container, f"mkdir -p {shlex.quote(container_dir)}", logger=logger)
    if mkdir.returncode != 0:
        raise RuntimeError(mkdir.stdout)


def copy_dump_to_container(args: argparse.Namespace, dump_spec: DumpSpec, container_dir: str, logger: RunLogger) -> None:
    mkdir = docker_exec(args.container, f"mkdir -p {shlex.quote(container_dir)}", logger=logger)
    if mkdir.returncode != 0:
        raise RuntimeError(mkdir.stdout)
    target = f"{args.container}:{container_dir.rstrip('/')}/"
    for file_name in dump_spec.source_files:
        source = str(Path(dump_spec.source_dir) / file_name)
        result = run_process(["docker", "cp", source, target], logger=logger)
        if result.returncode != 0:
            raise RuntimeError(result.stdout)


def dumpfile_reference(args: argparse.Namespace, ctx: RuntimeContext) -> str:
    dump_directory_object = getattr(args, "dump_directory_object", "")
    if dump_directory_object:
        return f"{dump_directory_object}:{ctx.dumpfile_arg}"
    return ctx.dumpfile_arg


def get_datafile_dir(args: argparse.Namespace, logger: RunLogger) -> str:
    if args.target_datafile_dir:
        return args.target_datafile_dir.rstrip("/")
    sql = """
SELECT file_name
FROM dba_data_files
WHERE rownum = 1;
"""
    first = sql_first_value(args.container, args.username, args.password, args.connect, sql, logger)
    if not first:
        return "/u01/app/oracle/oradata"
    return first.rsplit("/", 1)[0] if "/" in first else "."


def parse_metadata(text: str) -> Tuple[List[str], List[str], List[str]]:
    schemas: Set[str] = set()
    tablespaces: Set[str] = set()
    datafiles: Set[str] = set()

    schema_patterns = [
        r'CREATE\s+USER\s+"?([A-Za-z0-9_$#]+)"?',
        r'FROMUSER\s*=\s*"?([A-Za-z0-9_$#]+)"?',
        r'CONNECT\s+"?([A-Za-z0-9_$#]+)"?',
        r'CREATE\s+TABLE\s+"?([A-Za-z0-9_$#]+)"?\.',
        r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:VIEW|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|SEQUENCE|SYNONYM)\s+"?([A-Za-z0-9_$#]+)"?\.',
    ]
    for pattern in schema_patterns:
        schemas.update(m.group(1).upper() for m in re.finditer(pattern, text, flags=re.IGNORECASE))

    for m in re.finditer(r'TABLESPACE\s+"?([A-Za-z0-9_$#]+)"?', text, flags=re.IGNORECASE):
        tablespaces.add(m.group(1).upper())

    for m in re.finditer(r"DATAFILE\s+'([^']+)'", text, flags=re.IGNORECASE):
        datafiles.add(m.group(1))

    return (
        unique_sorted(schemas, SCHEMA_PARSE_EXCLUDES),
        unique_sorted(tablespaces, TABLESPACE_PARSE_EXCLUDES),
        sorted(datafiles),
    )


def read_container_file(container: str, path: str, logger: RunLogger) -> str:
    result = docker_exec(container, f"test -f {shlex.quote(path)} && cat {shlex.quote(path)} || true", logger=logger)
    return result.stdout


def run_impdp_sqlfile_probe(
    args: argparse.Namespace,
    ctx: RuntimeContext,
    logger: RunLogger,
    login: str,
    sqlfile_name: str,
    logfile_name: str,
    job_name: str = "ORS_PROBE_JOB",
    extra_params: Optional[List[str]] = None,
) -> Tuple[subprocess.CompletedProcess, str, str, str]:
    cleanup = docker_exec(
        args.container,
        "rm -f -- "
        + shlex.quote(f"{ctx.container_import_dir}/{sqlfile_name}")
        + " "
        + shlex.quote(f"{ctx.container_import_dir}/{logfile_name}"),
        logger=logger,
    )
    if cleanup.returncode != 0:
        raise RuntimeError(f"Could not clear stale probe outputs: {cleanup.stdout}")
    params = [
        "impdp",
        shlex.quote(login),
        f"DIRECTORY={shlex.quote(args.directory_object)}",
        f"DUMPFILE={shlex.quote(dumpfile_reference(args, ctx))}",
        "FULL=Y",
        f"SQLFILE={shlex.quote(sqlfile_name)}",
        f"LOGFILE={shlex.quote(logfile_name)}",
        f"JOB_NAME={shlex.quote(job_name)}",
    ]
    params.extend(extra_params or [])
    result = docker_exec(args.container, " ".join(params), logger=logger)

    sqlfile_container = f"{ctx.container_import_dir}/{sqlfile_name}"
    logfile_container = f"{ctx.container_import_dir}/{logfile_name}"
    sqlfile_text = read_container_file(args.container, sqlfile_container, logger)
    logfile_text = read_container_file(args.container, logfile_container, logger)

    docker_cp_from(args.container, sqlfile_container, Path(ctx.probe_dir) / sqlfile_name, logger)
    docker_cp_from(args.container, logfile_container, Path(ctx.probe_dir) / logfile_name, logger)
    return result, sqlfile_text, logfile_text, result.stdout + "\n" + sqlfile_text + "\n" + logfile_text


def has_legacy_exp_marker(text: str) -> bool:
    return "ORA-39143" in text or "original export" in text.lower()


def has_comment_charset_loss(text: str) -> bool:
    return "ORA-39346" in text and "COMMENT" in text.upper()


def classify_probe_failure(text: str) -> str:
    upper = text.upper()
    if "ORA-39087" in upper:
        return "directory_invalid"
    if "ORA-39054" in upper:
        return "sqlfile_invalid"
    if "ORA-39059" in upper:
        return "dump_set_incomplete"
    if "ORA-31640" in upper or "ORA-27037" in upper or "ORA-19505" in upper:
        return "dump_file_inaccessible"
    if "ORA-31655" in upper:
        return "metadata_not_selected"
    if has_legacy_exp_marker(text):
        return "legacy_exp"
    return "unknown_dump_type"


def probe_attempt(
    args: argparse.Namespace,
    ctx: RuntimeContext,
    logger: RunLogger,
    login: str,
    attempt_number: int,
    extra_params: Optional[List[str]] = None,
) -> Tuple[subprocess.CompletedProcess, str, str, str, Dict[str, object]]:
    suffix = "" if attempt_number == 1 else f"_retry_{attempt_number}"
    sqlfile_name = f"auto_probe{suffix}.sql"
    logfile_name = f"auto_probe_impdp{suffix}.log"
    result, sqlfile_text, logfile_text, combined = run_impdp_sqlfile_probe(
        args,
        ctx,
        logger,
        login,
        sqlfile_name,
        logfile_name,
        f"{getattr(args, 'job_name', 'ORS_IMPORT_JOB')}_P{attempt_number}",
        extra_params,
    )
    attempt = {
        "attempt": attempt_number,
        "returncode": result.returncode,
        "failure_code": "" if result.returncode == 0 else classify_probe_failure(combined),
        "sqlfile": str(Path(ctx.probe_dir) / sqlfile_name),
        "logfile": str(Path(ctx.probe_dir) / logfile_name),
    }
    logger.event("probe_attempt", f"probe:{attempt_number}", "succeeded" if result.returncode == 0 else "failed", **attempt)
    return result, sqlfile_text, logfile_text, combined, attempt


def probe_dump(args: argparse.Namespace, ctx: RuntimeContext, logger: RunLogger) -> ProbeResult:
    login = f"{args.username}/{args.password}{args.connect}"
    attempts: List[Dict[str, object]] = []

    impdp_result, sqlfile_text, _, combined, attempt = probe_attempt(
        args, ctx, logger, login, 1
    )
    attempts.append(attempt)

    failure_code = classify_probe_failure(combined) if impdp_result.returncode != 0 else ""
    if failure_code in {"directory_invalid", "sqlfile_invalid"}:
        logger.log(
            f"[probe] {failure_code} detected; recreating and verifying DIRECTORY before one retry"
        )
        create_directory(args, args.directory_object, ctx.container_import_dir, logger)
        verify_directory(args, args.directory_object, ctx.container_import_dir, logger)
        impdp_result, sqlfile_text, _, retry_combined, attempt = probe_attempt(
            args, ctx, logger, login, 2
        )
        attempts.append(attempt)
        combined = combined + "\n" + retry_combined
        failure_code = classify_probe_failure(retry_combined) if impdp_result.returncode != 0 else ""

    if impdp_result.returncode == 0 and not has_legacy_exp_marker(combined):
        schemas, tablespaces, datafiles = parse_metadata(combined)
        return ProbeResult(
            dump_type="datapump",
            schemas=schemas,
            tablespaces=tablespaces,
            datafiles=datafiles,
            probe_log=str(attempt["logfile"]),
            sqlfile=str(attempt["sqlfile"]),
            notes=["Data Pump dump detected. Use impdp."],
            attempts=attempts,
        )

    if not has_legacy_exp_marker(combined) and has_comment_charset_loss(combined):
        logger.log("[probe] ORA-39346 on COMMENT detected; retrying metadata probe with EXCLUDE=COMMENT")
        retry_result, _, _, retry_combined, attempt = probe_attempt(
            args, ctx, logger, login, len(attempts) + 1, ["EXCLUDE=COMMENT"]
        )
        attempts.append(attempt)
        if retry_result.returncode == 0 and not has_legacy_exp_marker(retry_combined):
            schemas, tablespaces, datafiles = parse_metadata(retry_combined)
            return ProbeResult(
                dump_type="datapump",
                schemas=schemas,
                tablespaces=tablespaces,
                datafiles=datafiles,
                exclude_object_types=["COMMENT"],
                probe_log=str(attempt["logfile"]),
                sqlfile=str(attempt["sqlfile"]),
                notes=[
                    "Data Pump dump detected after retrying probe with EXCLUDE=COMMENT.",
                    "ORA-39346 was raised for COMMENT metadata; COMMENT will be excluded from import to avoid character set conversion data loss.",
                ],
                attempts=attempts,
            )
        combined = combined + "\n" + retry_combined
        failure_code = classify_probe_failure(retry_combined)

    if failure_code == "metadata_not_selected":
        return ProbeResult(
            dump_type="datapump",
            probe_log=str(attempt["logfile"]),
            sqlfile=str(attempt["sqlfile"]),
            failure_code=failure_code,
            attempts=attempts,
            notes=[
                "Data Pump recognized the dump, but SQLFILE FULL=Y selected no metadata (ORA-31655).",
                "The dump may be DATA_ONLY or may require an import filter not available from the current metadata probe.",
                "The system will not guess target structures or report this as an unknown dump type.",
            ],
        )

    if failure_code != "legacy_exp":
        return ProbeResult(
            dump_type="probe_failed" if failure_code != "unknown_dump_type" else "unknown",
            probe_log=str(attempt["logfile"]),
            sqlfile=str(attempt["sqlfile"]),
            failure_code=failure_code,
            attempts=attempts,
            notes=[
                f"impdp probe failed with classified reason: {failure_code}.",
                "Inspect the probe logs before importing.",
                impdp_result.stdout[-2000:],
            ],
        )

    legacy_cmd = (
        f"imp {shlex.quote(login)} "
        f"FILE={shlex.quote(ctx.container_dump_dir.rstrip('/') + '/' + ctx.dumpfile_arg)} "
        f"FULL=Y SHOW=Y LOG={shlex.quote(ctx.container_import_dir.rstrip('/') + '/auto_probe_imp.log')}"
    )
    legacy_result = docker_exec(args.container, legacy_cmd, logger=logger)
    legacy_log_container = f"{ctx.container_import_dir}/auto_probe_imp.log"
    legacy_log_text = read_container_file(args.container, legacy_log_container, logger)
    docker_cp_from(args.container, legacy_log_container, Path(ctx.probe_dir) / "auto_probe_imp.log", logger)
    attempts.append(
        {
            "attempt": len(attempts) + 1,
            "tool": "imp",
            "mode": "SHOW=Y",
            "returncode": legacy_result.returncode,
            "logfile": str(Path(ctx.probe_dir) / "auto_probe_imp.log"),
        }
    )
    if legacy_result.returncode != 0:
        return ProbeResult(
            dump_type="probe_failed",
            probe_log=str(Path(ctx.probe_dir) / "auto_probe_imp.log"),
            failure_code="legacy_probe_failed",
            attempts=attempts,
            notes=[
                "ORA-39143 identified an original exp dump, but imp FULL=Y SHOW=Y also failed.",
                "Inspect the legacy probe output before importing.",
            ],
        )
    schemas, tablespaces, datafiles = parse_metadata(legacy_result.stdout + "\n" + legacy_log_text)
    return ProbeResult(
        dump_type="legacy_exp",
        schemas=schemas,
        tablespaces=tablespaces,
        datafiles=datafiles,
        probe_log=str(Path(ctx.probe_dir) / "auto_probe_imp.log"),
        notes=["Legacy exp dump detected. Use imp."],
        attempts=attempts,
    )


def build_runtime_context(args: argparse.Namespace, dump_spec: DumpSpec, now: dt.datetime) -> RuntimeContext:
    stem = normalize_token(dump_spec.display_name)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    run_id = args.run_id or f"{timestamp}_{stem}"
    run_dir = Path(args.runs_dir) / run_id
    container_import_dir = f"{args.container_dir.rstrip('/')}/{run_id}"
    direct_container_dir = getattr(args, "direct_container_dir", "")
    container_dump_dir = direct_container_dir.rstrip("/") if direct_container_dir else container_import_dir
    return RuntimeContext(
        run_id=run_id,
        run_dir=str(run_dir),
        probe_dir=str(run_dir / "probe"),
        cleanup_dir=str(run_dir / "cleanup"),
        import_dir=str(run_dir / "import"),
        local_plan_path=str(run_dir / "plan.json"),
        container_import_dir=container_import_dir,
        dumpfile_arg=dump_spec.dumpfile_arg,
        dump_display_name=dump_spec.display_name,
        container_dump_dir=container_dump_dir,
        zero_copy_dump=bool(direct_container_dir),
    )


def derive_target_names(args: argparse.Namespace, dump_display_name: str, now: dt.datetime, source_schemas: List[str]) -> Tuple[Dict[str, str], str]:
    base = normalize_token(dump_display_name)
    suffix = now.strftime("%y%m%d_%H%M")
    default_user = oracle_name(f"{base}_{suffix}")
    default_tablespace = oracle_name(f"TBS_{base}_{suffix}")

    if args.target_user and args.keep_source_schema:
        raise RuntimeError("--target-user and --keep-source-schema cannot be used together.")

    if args.target_user:
        if len(source_schemas) > 1:
            raise RuntimeError("--target-user can only be used when a dump contains one source schema.")
        schema_map = {source_schemas[0]: oracle_name(args.target_user)} if source_schemas else {}
    elif args.keep_source_schema:
        if not source_schemas:
            raise RuntimeError("--keep-source-schema requires at least one parsed source schema.")
        schema_map = {schema: schema for schema in source_schemas}
    elif args.target_schema_prefix:
        schema_map = {
            schema: oracle_name(f"{args.target_schema_prefix}{schema}")
            for schema in source_schemas
        }
    elif len(source_schemas) <= 1:
        schema_map = {source_schemas[0]: default_user} if source_schemas else {}
    else:
        schema_map = {
            schema: oracle_name(f"{base}_{schema}_{suffix}")
            for schema in source_schemas
        }

    target_tablespace = oracle_name(args.target_tablespace) if args.target_tablespace else default_tablespace
    return schema_map, target_tablespace


def build_plan(args: argparse.Namespace, ctx: RuntimeContext, dump_spec: DumpSpec, probe: ProbeResult, target_datafile_dir: str, now: dt.datetime) -> ImportPlan:
    schema_map, target_tablespace = derive_target_names(args, dump_spec.display_name, now, probe.schemas)
    if args.target_tablespace:
        tablespace_map = {
            tablespace: target_tablespace
            for tablespace in probe.tablespaces
            if tablespace.upper() not in SYSTEM_TABLESPACES
        }
    elif args.target_tablespace_prefix:
        tablespace_map = {
            tablespace: oracle_name(f"{args.target_tablespace_prefix}{tablespace}")
            for tablespace in probe.tablespaces
            if tablespace.upper() not in SYSTEM_TABLESPACES
        }
    else:
        tablespace_map = {
            tablespace: target_tablespace
            for tablespace in probe.tablespaces
            if tablespace.upper() not in SYSTEM_TABLESPACES
        }

    target_users = sorted(set(schema_map.values()))
    target_tablespaces = sorted(set(tablespace_map.values()) or ({target_tablespace} if target_users else set()))
    target_datafiles = {
        tablespace: f"{target_datafile_dir.rstrip('/')}/{tablespace.lower()}_01.dbf"
        for tablespace in target_tablespaces
    }

    login = f"{args.username}/{args.password}{args.connect}"
    job_name = getattr(args, "job_name", "ORS_IMPORT_JOB")
    commands: List[List[str]] = []
    fallback_commands: List[List[str]] = []
    masked_commands: List[str] = []
    masked_fallback_commands: List[str] = []
    excluded_object_types: List[str] = []

    def build_impdp_command(*, mode: str, logfile: str, job_name: str) -> List[str]:
        params = [
            "impdp",
            login,
            f"DIRECTORY={args.directory_object}",
            f"DUMPFILE={dumpfile_reference(args, ctx)}",
            f"LOGFILE={logfile}",
            f"JOB_NAME={job_name}",
            "FULL=Y" if mode == "full" else "",
            f"SCHEMAS={','.join(probe.schemas)}" if mode == "schemas" and probe.schemas else "",
            args.table_exists_action and f"TABLE_EXISTS_ACTION={args.table_exists_action}",
        ]
        params.extend(f"REMAP_SCHEMA={src}:{dst}" for src, dst in schema_map.items() if src != dst)
        params.extend(f"REMAP_TABLESPACE={src}:{dst}" for src, dst in tablespace_map.items())
        if excluded_object_types:
            params.append(f"EXCLUDE={','.join(excluded_object_types)}")
        shell_cmd = " ".join(shlex.quote(p) for p in params if p)
        return ["docker", "exec", "-i", args.container, "bash", "-lc", f"export NLS_LANG=AMERICAN_AMERICA.AL32UTF8; export LANG=C.UTF-8; {shell_cmd}"]

    if probe.dump_type == "datapump":
        if args.exclude_user_metadata:
            excluded_object_types.append("USER")
        if args.exclude_directory:
            excluded_object_types.append("DIRECTORY")
        if args.exclude_object_grants:
            excluded_object_types.append("OBJECT_GRANT")
        excluded_object_types.extend(obj for obj in probe.exclude_object_types if obj not in excluded_object_types)

        if args.import_mode == "schemas" and not probe.schemas:
            pass
        else:
            commands.append(build_impdp_command(mode=args.import_mode, logfile="auto_import_impdp.log", job_name=job_name))
            if args.import_mode == "schemas" and probe.schemas:
                fallback_commands.append(build_impdp_command(mode="full", logfile="auto_import_impdp_full_retry.log", job_name=f"{job_name}_F"))
    elif probe.dump_type == "legacy_exp":
        if schema_map:
            for src, dst in schema_map.items():
                shell_cmd = (
                    f"imp {shlex.quote(login)} "
                    f"FILE={shlex.quote(ctx.container_dump_dir.rstrip('/') + '/' + ctx.dumpfile_arg)} "
                    f"LOG={shlex.quote(ctx.container_import_dir.rstrip('/') + '/auto_import_' + src.lower() + '_imp.log')} "
                    f"FROMUSER={shlex.quote(src)} TOUSER={shlex.quote(dst)} IGNORE=Y"
                )
                commands.append(["docker", "exec", "-i", args.container, "bash", "-lc", f"export NLS_LANG=AMERICAN_AMERICA.AL32UTF8; export LANG=C.UTF-8; {shell_cmd}"])
        else:
            shell_cmd = (
                f"imp {shlex.quote(login)} "
                f"FILE={shlex.quote(ctx.container_dump_dir.rstrip('/') + '/' + ctx.dumpfile_arg)} "
                f"LOG={shlex.quote(ctx.container_import_dir.rstrip('/') + '/auto_import_legacy_imp.log')} FULL=Y IGNORE=Y"
            )
            commands.append(["docker", "exec", "-i", args.container, "bash", "-lc", f"export NLS_LANG=AMERICAN_AMERICA.AL32UTF8; export LANG=C.UTF-8; {shell_cmd}"])

    for cmd in commands:
        masked = " ".join(shlex.quote(part) for part in cmd).replace(f"{args.username}/{args.password}", f"{args.username}/******")
        masked_commands.append(masked)
    for cmd in fallback_commands:
        masked = " ".join(shlex.quote(part) for part in cmd).replace(f"{args.username}/{args.password}", f"{args.username}/******")
        masked_fallback_commands.append(masked)

    notes = list(probe.notes)
    export_log_summary = _export_log_summary(args)
    if export_log_summary:
        notes.append(
            "This import plan is assisted by an exactly matched Oracle export log; "
            "the DMP probe remains authoritative for executable metadata."
        )
    if probe.dump_type == "legacy_exp" and tablespace_map:
        notes.append("Legacy imp does not support REMAP_TABLESPACE; target users use the created default tablespace.")
    if not probe.schemas:
        notes.append("No business schema was parsed automatically. In schema mode no import command is generated; inspect probe logs or use --import-mode full explicitly.")
    if not probe.tablespaces:
        notes.append("No business tablespace was parsed automatically. Target users will still use the default target bigfile tablespace.")

    return ImportPlan(
        run=ctx,
        dump_type=probe.dump_type,
        probe_failure_code=probe.failure_code,
        probe_attempts=probe.attempts,
        container=args.container,
        directory_object=args.directory_object,
        dump_directory_object=getattr(args, "dump_directory_object", "") or args.directory_object,
        dumpfile_arg=dump_spec.dumpfile_arg,
        dump_source_files=dump_spec.source_files,
        source_schemas=probe.schemas,
        source_tablespaces=probe.tablespaces,
        excluded_object_types=excluded_object_types if probe.dump_type == "datapump" else probe.exclude_object_types,
        schema_map=schema_map,
        tablespace_map=tablespace_map,
        target_users=target_users,
        target_tablespaces=target_tablespaces,
        target_datafile_dir=target_datafile_dir,
        target_datafiles=target_datafiles,
        on_conflict=args.on_conflict,
        table_exists_action=args.table_exists_action,
        commands=commands,
        masked_commands=masked_commands,
        fallback_commands=fallback_commands,
        masked_fallback_commands=masked_fallback_commands,
        notes=notes,
        export_log_assisted=bool(export_log_summary),
        export_log_summary=export_log_summary,
        job_name=job_name,
    )


def _export_log_summary(args: argparse.Namespace) -> Dict[str, object]:
    name = str(getattr(args, "export_log_name", "") or "").strip()
    if not name:
        return {}
    return {
        "name": name,
        "content_sha256": str(getattr(args, "export_log_sha256", "") or ""),
        "source_status": str(getattr(args, "export_log_status", "") or ""),
        "export_mode": str(getattr(args, "export_log_mode", "") or ""),
        "source_schemas": [item for item in str(getattr(args, "export_log_schemas", "") or "").split(",") if item],
        "dump_files": [item for item in str(getattr(args, "export_log_dump_files", "") or "").split(",") if item],
        "missing_object_count": int(getattr(args, "export_log_missing_count", 0) or 0),
    }


def validate_export_log_expectations(args: argparse.Namespace, dump_spec: DumpSpec, probe: ProbeResult) -> None:
    expected = _export_log_summary(args)
    if not expected:
        return
    if expected["source_status"] not in {"clean_success", "completed_with_errors"}:
        raise RuntimeError(
            f"Export log status cannot be used for assisted import: {expected['source_status']}"
        )
    if expected["export_mode"] == "tables" and probe.dump_type != "datapump":
        raise RuntimeError("Export log identifies expdp TABLES export, but the DMP probe did not identify Data Pump.")
    expected_dumps = {str(item).lower() for item in expected["dump_files"]}
    actual_dumps = {str(item).lower() for item in dump_spec.source_files}
    if expected_dumps and expected_dumps != actual_dumps:
        raise RuntimeError("Export log DMP file set differs from the files resolved by the import task.")
    expected_schemas = {str(item).upper() for item in expected["source_schemas"]}
    actual_schemas = {str(item).upper() for item in probe.schemas}
    missing_schemas = sorted(expected_schemas - actual_schemas)
    if missing_schemas:
        raise RuntimeError(
            "Export log schemas are missing from the authoritative DMP probe: "
            + ", ".join(missing_schemas)
        )


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def plan_to_json(plan: ImportPlan) -> Dict[str, object]:
    """Serialize a plan without persisting executable commands containing credentials."""
    data = asdict(plan)
    data["commands"] = []
    data["fallback_commands"] = []
    data["commands_are_masked_only"] = True
    return data


def copy_oracle_import_logs(args: argparse.Namespace, ctx: RuntimeContext, plan: ImportPlan, logger: RunLogger) -> None:
    if plan.dump_type == "datapump":
        docker_cp_from(args.container, f"{ctx.container_import_dir}/auto_import_impdp.log", Path(ctx.import_dir) / "auto_import_impdp.log", logger)
        docker_cp_from_if_present(
            args.container,
            f"{ctx.container_import_dir}/auto_import_impdp_full_retry.log",
            Path(ctx.import_dir) / "auto_import_impdp_full_retry.log",
            logger,
        )
        return

    if plan.dump_type == "legacy_exp":
        if plan.source_schemas:
            for schema in plan.source_schemas:
                docker_cp_from(args.container, f"{ctx.container_import_dir}/auto_import_{schema.lower()}_imp.log", Path(ctx.import_dir) / f"auto_import_{schema.lower()}_imp.log", logger)
        else:
            docker_cp_from(args.container, f"{ctx.container_import_dir}/auto_import_legacy_imp.log", Path(ctx.import_dir) / "auto_import_legacy_imp.log", logger)


def summarize_oracle_errors(log_dir: Path, logger: RunLogger, label: str = "import") -> None:
    patterns = ("ORA-", "IMP-", "UDI-", "LRM-")
    matches: List[str] = []
    for log_path in sorted(log_dir.glob("*.log")):
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if any(pattern in line for pattern in patterns):
                    matches.append(f"{log_path.name}: {line}")
        except OSError as exc:
            logger.log(f"[warn] could not read {log_path}: {exc}")

    if not matches:
        logger.log(f"[error-summary] no ORA/IMP/UDI/LRM lines found in {log_dir}")
        return

    logger.log(f"[error-summary] Oracle {label} errors:")
    if label == "import":
        table_related = [
            line for line in matches
            if "Object type TABLE" in line or "TABLE_DATA" in line
        ]
        grant_related = [
            line for line in matches
            if "OBJECT_GRANT" in line or "ORA-01917" in line or "user or role" in line
        ]
        dependent_related = [line for line in matches if "ORA-39112" in line]

        if table_related:
            logger.log("[error-summary] priority: table/base-object failures:")
            for line in table_related[:40]:
                logger.log(f"[error-summary] {line}")
        else:
            logger.log("[error-summary] no explicit TABLE/TABLE_DATA failure lines found in copied import logs.")

        if grant_related:
            logger.log("[error-summary] object grant failures, usually ignorable in recovery warehouse mode:")
            for line in grant_related[:20]:
                logger.log(f"[error-summary] {line}")

        if dependent_related:
            logger.log("[error-summary] dependent object failures such as INDEX/CONSTRAINT are often follow-up errors after base table failure:")
            for line in dependent_related[:20]:
                logger.log(f"[error-summary] {line}")

        other = [
            line for line in matches
            if line not in table_related and line not in grant_related and line not in dependent_related
        ]
        if other:
            logger.log("[error-summary] other Oracle errors:")
            for line in other[-20:]:
                logger.log(f"[error-summary] {line}")
        return

    for line in matches[-40:]:
        logger.log(f"[error-summary] {line}")


def log_contains_schema_filter_no_objects(import_dir) -> bool:
    text_parts: List[str] = []
    for log_path in sorted(Path(import_dir).glob("*.log")):
        try:
            text_parts.append(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    text = "\n".join(text_parts)
    return "ORA-39039" in text and "ORA-31655" in text


def oracle_error_lines(log_dir: str) -> List[str]:
    patterns = ("ORA-", "IMP-", "UDI-", "LRM-")
    lines: List[str] = []
    for log_path in sorted(Path(log_dir).glob("*.log")):
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if any(pattern in line for pattern in patterns):
                    lines.append(f"{log_path.name}: {line}")
        except OSError:
            continue
    return lines


def import_failed_only_for_compile_warnings(import_dir: str) -> bool:
    lines = oracle_error_lines(import_dir)
    return bool(lines) and all("ORA-39082" in line for line in lines)


def sql_string_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{quote_sql(value.upper())}'" for value in values)


def metadata_object_type_expr() -> str:
    return """
CASE r.object_type
  WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY'
  WHEN 'TYPE BODY' THEN 'TYPE_BODY'
  ELSE r.object_type
END
"""


def repair_remapped_dependencies(args: argparse.Namespace, plan: ImportPlan, logger: RunLogger) -> ImportOutcome:
    outcome = ImportOutcome()
    remaps = [(src.upper(), dst.upper()) for src, dst in plan.schema_map.items() if src.upper() != dst.upper()]
    target_users = sorted({user.upper() for user in plan.target_users})
    if not remaps or not target_users:
        return outcome

    replacements = []
    for src, dst in remaps:
        replacements.extend([
            (f'"{src}".', f'"{dst}".'),
            (f"{src}.", f"{dst}."),
            (f'"{src.lower()}".', f'"{dst}".'),
            (f"{src.lower()}.", f"{dst}."),
        ])

    replace_sql = "\n".join(
        f"      l_ddl := REPLACE(l_ddl, '{quote_sql(old)}', '{quote_sql(new)}');"
        for old, new in replacements
    )
    target_sql = sql_string_list(target_users)
    object_types_sql = "'VIEW','PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY','TRIGGER','TYPE','TYPE BODY'"
    script = f"""
SET SERVEROUTPUT ON SIZE UNLIMITED
SET FEEDBACK OFF
DECLARE
  l_ddl CLOB;
  l_original CLOB;
  l_stmt VARCHAR2(32767);
BEGIN
  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', FALSE);
  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'EMIT_SCHEMA', TRUE);

  FOR r IN (
    SELECT owner, object_name, object_type
    FROM dba_objects
    WHERE owner IN ({target_sql})
      AND object_type IN ({object_types_sql})
      AND status = 'INVALID'
    ORDER BY owner, object_type, object_name
  ) LOOP
    BEGIN
      l_ddl := DBMS_METADATA.GET_DDL({metadata_object_type_expr()}, r.object_name, r.owner);
      l_original := l_ddl;
{replace_sql}
      IF DBMS_LOB.COMPARE(l_original, l_ddl) <> 0 THEN
        l_stmt := DBMS_LOB.SUBSTR(l_ddl, 32767, 1);
        EXECUTE IMMEDIATE l_stmt;
        DBMS_OUTPUT.PUT_LINE('[repair] ' || r.owner || '.' || r.object_type || '.' || r.object_name);
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('[repair-error] ' || r.owner || '.' || r.object_type || '.' || r.object_name || ': ' || SQLERRM);
    END;
  END LOOP;

  FOR r IN (
    SELECT owner, object_name, object_type
    FROM dba_objects
    WHERE owner IN ({target_sql})
      AND object_type IN ({object_types_sql})
    ORDER BY owner, object_type, object_name
  ) LOOP
    BEGIN
      IF r.object_type = 'PACKAGE BODY' THEN
        EXECUTE IMMEDIATE 'ALTER PACKAGE "' || r.owner || '"."' || r.object_name || '" COMPILE BODY';
      ELSIF r.object_type = 'TYPE BODY' THEN
        EXECUTE IMMEDIATE 'ALTER TYPE "' || r.owner || '"."' || r.object_name || '" COMPILE BODY';
      ELSE
        EXECUTE IMMEDIATE 'ALTER ' || r.object_type || ' "' || r.owner || '"."' || r.object_name || '" COMPILE';
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        NULL;
    END;
  END LOOP;

  FOR r IN (
    SELECT owner, object_name, object_type
    FROM dba_objects
    WHERE owner IN ({target_sql})
      AND object_type IN ({object_types_sql})
      AND status = 'INVALID'
    ORDER BY owner, object_type, object_name
  ) LOOP
    DBMS_OUTPUT.PUT_LINE('[invalid] ' || r.owner || '.' || r.object_type || '.' || r.object_name);
  END LOOP;
END;
/
"""
    logger.log("[repair] checking invalid views/procedures/packages after schema remap")
    output = sqlplus(args.container, args.username, args.password, args.connect, script, logger)
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("[repair]"):
            outcome.repaired_objects.append(stripped.replace("[repair] ", "", 1))
        elif stripped.startswith("[invalid]"):
            outcome.invalid_objects.append(stripped.replace("[invalid] ", "", 1))
        elif stripped.startswith("[repair-error]"):
            outcome.warnings.append(stripped)

    if outcome.repaired_objects:
        logger.log(f"[repair] repaired remapped dependency references: {len(outcome.repaired_objects)} object(s)")
        for item in outcome.repaired_objects[:40]:
            logger.log(f"[repair] fixed {item}")
    if outcome.invalid_objects:
        outcome.warnings.append(
            f"{len(outcome.invalid_objects)} invalid object(s) remain after dependency repair."
        )
        logger.log(f"[repair] invalid objects remain: {len(outcome.invalid_objects)}")
        for item in outcome.invalid_objects[:40]:
            logger.log(f"[repair] still invalid {item}")
    return outcome


def cleanup_directory_object(args: argparse.Namespace, directory_object: str, current_container_dir: str, logger: RunLogger) -> None:
    old_path = directory_path(args, directory_object, logger)
    if not old_path:
        return

    logger.log(f"[cleanup] directory {directory_object} exists at {old_path}")
    if args.on_conflict == "stop":
        raise RuntimeError(f"DIRECTORY {directory_object} already exists. Use --on-conflict recreate to replace it.")

    managed_root = args.container_dir.rstrip("/") + "/"
    if old_path.rstrip("/") != current_container_dir.rstrip("/") and old_path.startswith(managed_root):
        cmd = f"rm -rf -- {shlex.quote(old_path.rstrip('/') + '/')}*"
        result = docker_exec(args.container, cmd, logger=logger)
        if result.returncode != 0:
            raise RuntimeError(result.stdout)
        (Path(args.current_cleanup_dir) / "cleanup.log").parent.mkdir(parents=True, exist_ok=True)
        with (Path(args.current_cleanup_dir) / "cleanup.log").open("a", encoding="utf-8") as fh:
            fh.write(f"Removed old directory files under {old_path}\n")
    elif old_path.rstrip("/") == current_container_dir.rstrip("/"):
        logger.log("[cleanup] existing DIRECTORY points to current run directory; keeping copied dump file")
    else:
        raise RuntimeError(f"Refusing to clean unmanaged DIRECTORY path: {old_path}")

    sqlplus(args.container, args.username, args.password, args.connect, f"DROP DIRECTORY {qident(directory_object)};", logger)


def cleanup_zero_copy_work_directory(args: argparse.Namespace, ctx: RuntimeContext, logger: RunLogger) -> None:
    if not ctx.zero_copy_dump:
        return

    managed_root = args.container_dir.rstrip("/") + "/"
    work_dir = ctx.container_import_dir.rstrip("/")
    if not work_dir.startswith(managed_root) or work_dir == args.container_dir.rstrip("/"):
        logger.log(f"[warn] refusing to clean unmanaged zero-copy work directory: {work_dir}")
        return

    try:
        actual_path = directory_path(args, args.directory_object, logger)
        if actual_path and actual_path.rstrip("/") == work_dir:
            sqlplus(
                args.container,
                args.username,
                args.password,
                args.connect,
                f"DROP DIRECTORY {qident(args.directory_object)};",
                logger,
            )
            logger.log(f"[cleanup] dropped per-run work DIRECTORY {args.directory_object}")
        elif actual_path:
            logger.log(
                f"[warn] per-run work DIRECTORY {args.directory_object} points to {actual_path}; "
                f"expected {work_dir}, so it was not dropped"
            )

        result = docker_exec(args.container, f"rm -rf -- {shlex.quote(work_dir)}", logger=logger)
        if result.returncode != 0:
            logger.log(f"[warn] could not remove zero-copy work directory {work_dir}: {result.stdout.strip()}")
        else:
            logger.log(f"[cleanup] removed zero-copy work files under {work_dir}")
    except Exception as exc:
        logger.log(f"[warn] zero-copy work directory cleanup failed: {exc}")


def cleanup_targets(args: argparse.Namespace, plan: ImportPlan, logger: RunLogger) -> None:
    cleanup_log = Path(plan.run.cleanup_dir) / "cleanup.log"
    cleanup_log.parent.mkdir(parents=True, exist_ok=True)

    for user in plan.target_users:
        ensure_safe_user(user)
        exists = user_exists(args, user, logger)
        logger.log(f"[check] user {user}: {'exists' if exists else 'not exists'}")
        if not exists:
            continue
        if args.on_conflict == "stop":
            raise RuntimeError(f"Target user {user} already exists.")
        logger.log(f"[cleanup] drop user {user} cascade")
        sqlplus(args.container, args.username, args.password, args.connect, f"DROP USER {qident(user)} CASCADE;", logger)
        with cleanup_log.open("a", encoding="utf-8") as fh:
            fh.write(f"Dropped user {user} cascade\n")

    for tablespace in plan.target_tablespaces:
        ensure_safe_tablespace(tablespace)
        exists = tablespace_exists(args, tablespace, logger)
        logger.log(f"[check] tablespace {tablespace}: {'exists' if exists else 'not exists'}")
        if not exists:
            continue
        if args.on_conflict == "stop":
            raise RuntimeError(f"Target tablespace {tablespace} already exists.")
        logger.log(f"[cleanup] drop tablespace {tablespace} including contents and datafiles")
        sql = f"DROP TABLESPACE {qident(tablespace)} INCLUDING CONTENTS AND DATAFILES;"
        sqlplus(args.container, args.username, args.password, args.connect, sql, logger)
        with cleanup_log.open("a", encoding="utf-8") as fh:
            fh.write(f"Dropped tablespace {tablespace} including contents and datafiles\n")


def prepare_targets(args: argparse.Namespace, plan: ImportPlan, logger: RunLogger) -> None:
    for tablespace in plan.target_tablespaces:
        ensure_safe_tablespace(tablespace)
        datafile = plan.target_datafiles[tablespace]
        exists = tablespace_exists(args, tablespace, logger)
        logger.log(f"[check] tablespace {tablespace} before create: {'exists' if exists else 'not exists'}")
        if exists:
            continue
        sql = f"""
CREATE BIGFILE TABLESPACE {qident(tablespace)}
DATAFILE '{quote_sql(datafile)}'
SIZE {args.tablespace_size}
AUTOEXTEND ON
NEXT {args.tablespace_next}
MAXSIZE {args.tablespace_maxsize};
"""
        logger.log(f"[create] bigfile tablespace {tablespace} -> {datafile}")
        sqlplus(args.container, args.username, args.password, args.connect, sql, logger)

    default_tablespace = plan.target_tablespaces[0] if plan.target_tablespaces else args.default_tablespace
    for user in plan.target_users:
        ensure_safe_user(user)
        exists = user_exists(args, user, logger)
        logger.log(f"[check] user {user} before create: {'exists' if exists else 'not exists'}")
        if not exists:
            sql = f"""
CREATE USER {qident(user)}
IDENTIFIED BY "{args.target_user_password}"
DEFAULT TABLESPACE {qident(default_tablespace)}
TEMPORARY TABLESPACE TEMP;
"""
            logger.log(f"[create] user {user}")
            sqlplus(args.container, args.username, args.password, args.connect, sql, logger)
        grants = f"""
GRANT CONNECT, RESOURCE TO {qident(user)};
GRANT CREATE VIEW, CREATE SYNONYM, CREATE SEQUENCE, CREATE PROCEDURE, CREATE TRIGGER TO {qident(user)};
"""
        sqlplus(args.container, args.username, args.password, args.connect, grants, logger)
        if plan.target_tablespaces:
            quota_sql = "\n".join(
                f"ALTER USER {qident(user)} QUOTA UNLIMITED ON {qident(tablespace)};"
                for tablespace in plan.target_tablespaces
            )
            logger.log(f"[grant] quota unlimited for {user} on {', '.join(plan.target_tablespaces)}")
            sqlplus(args.container, args.username, args.password, args.connect, quota_sql, logger)


def merge_outcome(base: ImportOutcome, extra: ImportOutcome) -> ImportOutcome:
    base.warnings.extend(extra.warnings)
    base.repaired_objects.extend(extra.repaired_objects)
    base.invalid_objects.extend(extra.invalid_objects)
    if base.warnings or base.repaired_objects or base.invalid_objects:
        base.status = "imported_with_warnings"
    return base


def run_import(args: argparse.Namespace, ctx: RuntimeContext, plan: ImportPlan, logger: RunLogger) -> ImportOutcome:
    if not plan.commands:
        raise RuntimeError("No import command was generated.")
    for cmd in plan.commands:
        display = " ".join(shlex.quote(part) for part in cmd)
        logger.log(f"[run] {logger.mask(display)}")
        result = run_process(cmd, logger=logger)
        if result.returncode != 0:
            copy_oracle_import_logs(args, ctx, plan, logger)
            if plan.fallback_commands and log_contains_schema_filter_no_objects(plan.run.import_dir):
                logger.log("[retry] SCHEMAS import selected no objects (ORA-39039/ORA-31655); retrying once with FULL=Y.")
                for fallback_cmd in plan.fallback_commands:
                    fallback_display = " ".join(shlex.quote(part) for part in fallback_cmd)
                    logger.log(f"[retry] {logger.mask(fallback_display)}")
                    fallback_result = run_process(fallback_cmd, logger=logger)
                    copy_oracle_import_logs(args, ctx, plan, logger)
                    if fallback_result.returncode != 0:
                        if import_failed_only_for_compile_warnings(plan.run.import_dir):
                            logger.log(
                                "[warn] FULL=Y retry returned a non-zero code, but copied logs only contain ORA-39082 compilation warnings."
                            )
                            outcome = ImportOutcome(status="imported_with_warnings")
                            outcome.warnings.extend(oracle_error_lines(plan.run.import_dir))
                            return merge_outcome(outcome, repair_remapped_dependencies(args, plan, logger))
                        raise RuntimeError(f"Import retry with FULL=Y failed with exit code {fallback_result.returncode}. Check {plan.run.import_dir}.")
                logger.log("[retry] FULL=Y retry finished.")
                outcome = ImportOutcome()
                return merge_outcome(outcome, repair_remapped_dependencies(args, plan, logger))
            if import_failed_only_for_compile_warnings(plan.run.import_dir):
                logger.log(
                    "[warn] impdp returned a non-zero code, but copied logs only contain ORA-39082 compilation warnings."
                )
                outcome = ImportOutcome(status="imported_with_warnings")
                outcome.warnings.extend(oracle_error_lines(plan.run.import_dir))
                return merge_outcome(outcome, repair_remapped_dependencies(args, plan, logger))
            raise RuntimeError(f"Import command failed with exit code {result.returncode}. Check {plan.run.import_dir}.")
    return merge_outcome(ImportOutcome(), repair_remapped_dependencies(args, plan, logger))


def build_report(
    plan: ImportPlan,
    status: str,
    error: Optional[str] = None,
    outcome: Optional[ImportOutcome] = None,
) -> Dict[str, object]:
    outcome = outcome or ImportOutcome(status=status)
    return {
        "status": status,
        "error": error,
        "run_id": plan.run.run_id,
        "dump_type": plan.dump_type,
        "probe_failure_code": plan.probe_failure_code,
        "probe_attempts": plan.probe_attempts,
        "source_schemas": plan.source_schemas,
        "source_tablespaces": plan.source_tablespaces,
        "excluded_object_types": plan.excluded_object_types,
        "schema_map": plan.schema_map,
        "tablespace_map": plan.tablespace_map,
        "target_users": plan.target_users,
        "target_tablespaces": plan.target_tablespaces,
        "target_datafiles": plan.target_datafiles,
        "fallback_commands": plan.masked_fallback_commands,
        "export_log_assisted": plan.export_log_assisted,
        "export_log_summary": plan.export_log_summary,
        "notes": plan.notes,
        "warnings": outcome.warnings,
        "repaired_objects": outcome.repaired_objects,
        "invalid_objects": outcome.invalid_objects,
    }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe and optionally import unknown Oracle DMP files into a Docker Oracle recovery warehouse.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  Single dump file:
    python oracle_dmp_auto_import.py --dump /data/expdp/DJ_HIS.DMP --password oracle --pdb ORCLPDB1 --execute

  Data Pump dump set, recommended:
    python oracle_dmp_auto_import.py --dump-dir /data/expdp --dumpfile XX%U.DMP --password oracle --pdb ORCLPDB1 --execute

  Data Pump dump set, compatible:
    python oracle_dmp_auto_import.py --dump /data/expdp/XX01.DMP --dumpfile XX%U.DMP --password oracle --pdb ORCLPDB1 --execute
""",
    )
    parser.add_argument("--dump", default="", help="Host path to one .dmp file. For dump sets, this may be a representative file only when --dumpfile uses %%U.")
    parser.add_argument("--dump-dir", default="", help="Host directory containing dump files. Recommended with --dumpfile for Data Pump dump sets.")
    parser.add_argument("--dumpfile", default="", help="Oracle DUMPFILE value, for example XX%%U.DMP for Data Pump dump sets.")
    parser.add_argument("--container", default="oracle-recovery-oracle19c", help="Oracle Docker container name. Default: oracle-recovery-oracle19c. Use --container auto to auto-detect.")
    parser.add_argument("--username", default="system", help="DBA username. Default: system")
    parser.add_argument("--password", required=True, help="DBA password.")
    parser.add_argument("--connect", default="", help="Optional SQL*Plus connect suffix, for example @ORCL.")
    parser.add_argument("--pdb", default="", help="Optional PDB service/name to use when auto-detecting CDB$ROOT, for example ORCLPDB1.")
    parser.add_argument("--runs-dir", default="runs", help="Local directory for per-run logs and plans.")
    parser.add_argument("--run-id", default="", help="Optional stable run id. Default: timestamp + dump stem.")
    parser.add_argument("--job-name", default="ORS_IMPORT_JOB", help="Stable Data Pump JOB_NAME prefix for this run.")
    parser.add_argument("--container-dir", default="/tmp/oracle_dmp_auto", help="Base directory inside the Oracle container for managed dump files.")
    parser.add_argument("--direct-container-dir", default="", help="Container path where host DMP files are already bind-mounted. When set, DMP files are not copied.")
    parser.add_argument("--directory-object", default="", help="Oracle DIRECTORY object. Default: generated from dump name and timestamp.")
    parser.add_argument("--dump-directory-object", default="", help="Shared Oracle DIRECTORY used only to read bind-mounted DMP files in zero-copy mode.")
    parser.add_argument("--target-user", default="", help="Target user/schema for single-schema dumps. Default: generated from dump name and timestamp.")
    parser.add_argument("--keep-source-schema", action="store_true", help="Import into the original source schema/user names. Missing target users are created by the tool.")
    parser.add_argument("--target-tablespace", default="", help="Target tablespace. Default: generated from dump name and timestamp.")
    parser.add_argument("--target-datafile-dir", default="", help="Container directory for generated target datafiles. Default: inferred from Oracle.")
    parser.add_argument("--target-schema-prefix", default="", help="Compatibility option. Prefer --target-user for single-schema dumps.")
    parser.add_argument("--target-tablespace-prefix", default="", help="Compatibility option. Prefer --target-tablespace.")
    parser.add_argument("--target-user-password", default="Oracle123", help="Password for auto-created target users.")
    parser.add_argument("--default-tablespace", default="USERS", help="Fallback default tablespace.")
    parser.add_argument("--tablespace-size", default="100M", help="Initial size for auto-created BIGFILE tablespaces.")
    parser.add_argument("--tablespace-next", default="100M", help="AUTOEXTEND NEXT size for auto-created BIGFILE tablespaces.")
    parser.add_argument("--tablespace-maxsize", default="UNLIMITED", help="MAXSIZE for auto-created BIGFILE tablespaces.")
    parser.add_argument("--on-conflict", default="recreate", choices=["recreate", "stop"], help="How to handle existing target users/tablespaces/directories. Default: recreate.")
    parser.add_argument("--table-exists-action", default="REPLACE", choices=["SKIP", "APPEND", "TRUNCATE", "REPLACE"], help="Data Pump TABLE_EXISTS_ACTION.")
    parser.add_argument("--import-mode", default="schemas", choices=["schemas", "full"], help="Data Pump import mode after probing.")
    parser.add_argument("--exclude-directory", action="store_true", default=True, help="Exclude DIRECTORY objects from Data Pump imports. Default: true.")
    parser.add_argument("--include-directory", dest="exclude_directory", action="store_false", help="Do not exclude DIRECTORY objects.")
    parser.add_argument("--exclude-user-metadata", action="store_true", default=True, help="Exclude USER metadata from Data Pump imports because the tool creates target users. Default: true.")
    parser.add_argument("--include-user-metadata", dest="exclude_user_metadata", action="store_false", help="Allow Data Pump to import USER metadata. Usually not recommended.")
    parser.add_argument("--exclude-object-grants", action="store_true", default=True, help="Exclude OBJECT_GRANT metadata from Data Pump imports. Default: true for recovery warehouses.")
    parser.add_argument("--include-object-grants", dest="exclude_object_grants", action="store_false", help="Allow Data Pump to import object grants to original users/roles. Usually not recommended.")
    parser.add_argument("--execute", action="store_true", help="Create/recreate targets and run the import. Without this, only probe and write a plan.")
    parser.add_argument("--plan-out", default="", help="Optional extra JSON plan output path. The canonical plan is runs/<run_id>/plan.json.")
    parser.add_argument("--export-log-name", default="", help="Matched Oracle export log filename.")
    parser.add_argument("--export-log-sha256", default="", help="SHA256 of the decoded matched export log content.")
    parser.add_argument("--export-log-status", default="", help="Parsed source export status.")
    parser.add_argument("--export-log-mode", default="", help="Parsed source export mode.")
    parser.add_argument("--export-log-schemas", default="", help="Comma-separated schemas parsed from the export log.")
    parser.add_argument("--export-log-dump-files", default="", help="Comma-separated DMP filenames parsed from the export log.")
    parser.add_argument("--export-log-missing-count", type=int, default=0, help="Number of source objects missing from the export log result.")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    if args.direct_container_dir and not args.dump_directory_object:
        print("--direct-container-dir requires --dump-directory-object.", file=sys.stderr)
        return 2
    if args.dump_directory_object and not args.direct_container_dir:
        print("--dump-directory-object is only valid with --direct-container-dir.", file=sys.stderr)
        return 2
    try:
        dump_spec = resolve_dump_inputs(args)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    now = dt.datetime.now()
    ctx = build_runtime_context(args, dump_spec, now)
    run_dir = Path(ctx.run_dir)
    for path in [run_dir, Path(ctx.probe_dir), Path(ctx.cleanup_dir), Path(ctx.import_dir)]:
        path.mkdir(parents=True, exist_ok=True)

    try:
        run_lock = acquire_run_lock(run_dir)
    except RunAlreadyActiveError as exc:
        print(f"[duplicate] {exc}", file=sys.stderr)
        return 75

    logger = RunLogger(run_dir, args.username, args.password)
    args.current_cleanup_dir = ctx.cleanup_dir

    try:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]{0,27}", args.job_name or ""):
            raise RuntimeError("Invalid Oracle Data Pump job name.")
        args.job_name = args.job_name.upper()
        if args.container.lower() == "auto":
            args.container = find_oracle_container(logger)

        if not args.directory_object:
            args.directory_object = generated_directory_name(dump_spec.display_name, ctx.run_id)
        if args.dump_directory_object and args.dump_directory_object.upper() == args.directory_object.upper():
            raise RuntimeError("The shared dump DIRECTORY and per-run work DIRECTORY must use different names.")

        logger.log(f"[info] run id: {ctx.run_id}")
        logger.log(f"[info] run dir: {run_dir.resolve()}")
        logger.log(f"[info] detailed command log: {logger.log_path.resolve()}")
        logger.log(f"[info] container: {args.container}")
        logger.log(f"[info] dump source dir: {dump_spec.source_dir}")
        logger.log(f"[info] dump files: {', '.join(dump_spec.source_files)}")
        logger.log(f"[info] impdp dumpfile: {dump_spec.dumpfile_arg}")
        logger.log(f"[info] container dump dir: {ctx.container_dump_dir}")
        logger.log(f"[info] container import dir: {ctx.container_import_dir}")
        logger.log(f"[info] directory object: {args.directory_object}")
        if args.dump_directory_object:
            logger.log(f"[info] shared dump directory object: {args.dump_directory_object}")

        logger.stage("pdb_connection", "running", "Resolve and verify the target PDB connection")
        resolve_pdb_connect(args, logger)
        logger.stage("pdb_connection", "succeeded", f"connect suffix={args.connect or '(default)'}")

        if args.execute:
            logger.stage("directory_cleanup", "running", "Check an existing task DIRECTORY object")
            cleanup_directory_object(args, args.directory_object, ctx.container_import_dir, logger)
            logger.stage("directory_cleanup", "succeeded")

        if args.direct_container_dir:
            logger.stage("dump_copy", "running", "Validate bind-mounted DMP files without copying")
            ensure_reusable_dump_directory(args, dump_spec, ctx.container_dump_dir, logger)
            logger.log(
                f"[zero-copy] reuse {args.dump_directory_object}:{dump_spec.dumpfile_arg}; "
                "docker cp was not executed"
            )
            logger.stage(
                "dump_copy",
                "succeeded",
                "zero-copy bind mount",
                zero_copy=True,
                directory_object=args.dump_directory_object,
                container_dir=ctx.container_dump_dir,
            )
        else:
            logger.stage("dump_copy", "running", f"Copy {len(dump_spec.source_files)} dump file(s) into Oracle container")
            logger.log(f"[copy] {len(dump_spec.source_files)} dump file(s) -> {args.container}:{ctx.container_import_dir}/")
            copy_dump_to_container(args, dump_spec, ctx.container_import_dir, logger)
            logger.stage("dump_copy", "succeeded", zero_copy=False)

        logger.stage("directory_create_verify", "running", "Create and verify Oracle DIRECTORY")
        ensure_container_directory(args, ctx.container_import_dir, logger)
        logger.log(f"[sql] create directory {args.directory_object} -> {ctx.container_import_dir}")
        create_directory(args, args.directory_object, ctx.container_import_dir, logger)
        verify_directory(args, args.directory_object, ctx.container_import_dir, logger)
        logger.stage("directory_create_verify", "succeeded", args.directory_object)

        logger.stage("metadata_probe", "running", "Detect dump type and metadata")
        logger.log("[probe] detecting dump type and metadata")
        probe = probe_dump(args, ctx, logger)
        logger.log(f"[probe] dump type: {probe.dump_type}")
        logger.log(f"[probe] schemas: {', '.join(probe.schemas) if probe.schemas else '(none parsed)'}")
        logger.log(f"[probe] tablespaces: {', '.join(probe.tablespaces) if probe.tablespaces else '(none parsed)'}")
        logger.stage(
            "metadata_probe",
            "failed" if probe.failure_code else "succeeded",
            probe.failure_code or probe.dump_type,
            attempts=len(probe.attempts),
        )

        validate_export_log_expectations(args, dump_spec, probe)

        logger.stage("plan", "running", "Build the import plan")
        target_datafile_dir = get_datafile_dir(args, logger)
        plan = build_plan(args, ctx, dump_spec, probe, target_datafile_dir, now)
        write_json(Path(ctx.local_plan_path), plan_to_json(plan))
        if args.plan_out:
            write_json(Path(args.plan_out), plan_to_json(plan))
        logger.log(f"[plan] written: {Path(ctx.local_plan_path).resolve()}")
        logger.stage("plan", "succeeded", str(Path(ctx.local_plan_path).resolve()))

        if probe.failure_code or probe.dump_type not in {"datapump", "legacy_exp"}:
            failure_code = probe.failure_code or "unknown_dump_type"
            write_json(run_dir / "report.json", build_report(plan, "failed", failure_code))
            summarize_oracle_errors(Path(ctx.probe_dir), logger, "probe")
            if probe.probe_log:
                logger.log(f"[stop] primary probe log: {probe.probe_log}")
            logger.log(f"[stop] probe failed with {failure_code}; inspect probe logs before executing.")
            return 1

        if not args.execute:
            write_json(run_dir / "report.json", build_report(plan, "dry-run"))
            logger.log("[dry-run] no business users/tablespaces/data were changed.")
            logger.log("[next] inspect plan.json, then rerun with --execute to recreate targets and import.")
            return 0

        logger.stage("target_cleanup", "running", "Check and clean target conflicts")
        logger.log("[cleanup] checking target conflicts")
        cleanup_targets(args, plan, logger)
        logger.stage("target_cleanup", "succeeded")

        logger.stage("target_prepare", "running", "Create target tablespaces and users")
        logger.log("[prepare] creating target bigfile tablespaces and users")
        prepare_targets(args, plan, logger)
        logger.stage("target_prepare", "succeeded")

        logger.stage("formal_import", "running", "Run impdp/imp inside the Oracle container")
        logger.log("[import] running import inside Docker container")
        try:
            outcome = run_import(args, ctx, plan, logger)
            missing_count = int(getattr(args, "export_log_missing_count", 0) or 0)
            if missing_count and outcome.status == "imported":
                outcome.status = "imported_with_warnings"
                outcome.warnings.append(
                    f"Source export log reported {missing_count} object(s) missing from the original export."
                )
        finally:
            copy_oracle_import_logs(args, ctx, plan, logger)

        write_json(run_dir / "report.json", build_report(plan, outcome.status, outcome=outcome))
        logger.stage("formal_import", "succeeded", outcome.status)
        logger.log(f"[done] import finished with status={outcome.status}. See logs under {run_dir.resolve()}")
        return 0
    except ImportStopRequested as exc:
        logger.fail_active_stage(str(exc))
        logger.log(f"[stop] {exc}")
        write_json(
            run_dir / "report.json",
            {
                "status": "stopped",
                "error": str(exc),
                "run_id": ctx.run_id,
                "job_name": args.job_name,
            },
        )
        return 130
    except Exception as exc:
        logger.fail_active_stage(str(exc))
        logger.log(f"[error] {exc}")
        summarize_oracle_errors(Path(ctx.import_dir), logger, "import")
        report_path = run_dir / "report.json"
        if not report_path.exists():
            write_json(report_path, {"status": "failed", "error": str(exc), "run_id": ctx.run_id})
        return 1
    finally:
        cleanup_zero_copy_work_directory(args, ctx, logger)
        release_run_lock(run_lock)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
