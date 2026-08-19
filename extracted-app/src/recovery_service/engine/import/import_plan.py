import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from recovery_service.core.domain import DumpArtifact, DumpVolumeGroup


@dataclass(frozen=True)
class ImportPlan:
    tool: str
    dumpfiles: list[str]
    logfile: str
    use_percent_u: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)


def choose_initial_import_plan(group: DumpVolumeGroup, log_text: str = "") -> ImportPlan:
    tool, reason = _detect_tool(log_text)
    dumps = sorted(group.dump_files, key=lambda a: a.filename)
    logfile = _logfile_name(group)

    if tool == "impdp":
        pattern = _percent_u_pattern(dumps)
        if pattern:
            return ImportPlan(
                tool="impdp",
                dumpfiles=[pattern],
                logfile=logfile,
                use_percent_u=True,
                reason=f"{reason}; multi-volume pattern detected",
            )
        return ImportPlan(
            tool="impdp",
            dumpfiles=[d.filename for d in dumps],
            logfile=logfile,
            reason=reason,
        )

    return ImportPlan(
        tool="imp",
        dumpfiles=[d.filename for d in dumps],
        logfile=logfile,
        reason=reason,
    )


def should_retry_with_impdp(output: str) -> bool:
    upper = output.upper()
    markers = [
        "ORA-390",
        "UDI-",
        "MASTER TABLE",
        "DATA PUMP",
        "IMP-00010",
        "NOT A VALID EXPORT FILE",
    ]
    return any(marker in upper for marker in markers)


def _detect_tool(log_text: str) -> tuple[str, str]:
    upper = log_text.upper()
    if not upper:
        return "imp", "no related log; default to imp"
    if "DATA PUMP" in upper or "EXPDP" in upper or "ORACLE DATABASE 10G ENTERPRISE EDITION" in upper:
        return "impdp", "related log indicates Data Pump"
    if "EXPORT:V" in upper or "IMP-" in upper or "EXPORT DONE IN" in upper:
        return "imp", "related log indicates legacy exp"
    return "imp", "related log inconclusive; default to imp"


def _percent_u_pattern(dumps: list[DumpArtifact]) -> str | None:
    if len(dumps) < 2:
        return None
    names = [d.filename for d in dumps]
    parsed = [_split_volume_name(name) for name in names]
    if any(p is None for p in parsed):
        return None
    prefixes = {p[0] for p in parsed if p}
    suffixes = {p[2] for p in parsed if p}
    widths = {len(p[1]) for p in parsed if p}
    if len(prefixes) == 1 and len(suffixes) == 1 and len(widths) == 1:
        p = parsed[0]
        assert p is not None
        return f"{p[0]}%U{p[2]}"
    return None


def _split_volume_name(filename: str) -> tuple[str, str, str] | None:
    m = re.match(r"^(.*?)(\d{2,})(\.[^.]+)$", filename)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _logfile_name(group: DumpVolumeGroup) -> str:
    if group.dump_files:
        dumps = sorted(group.dump_files, key=lambda a: a.filename)
        parsed = [_split_volume_name(d.filename) for d in dumps]
        if len(dumps) > 1 and all(p is not None for p in parsed):
            first = parsed[0]
            assert first is not None
            base = first[0].rstrip("_-.") or dumps[0].filename.rsplit(".", 1)[0]
        else:
            base = PurePosixPath(dumps[0].filename).name.rsplit(".", 1)[0]
        return _dedupe_logfile(f"{base}_import.log", group)
    return "import.log"


def _dedupe_logfile(logfile: str, group: DumpVolumeGroup) -> str:
    existing = {PurePosixPath(a.filename).name.upper() for a in group.log_files}
    if logfile.upper() not in existing:
        return logfile
    base, dot, suffix = logfile.rpartition(".")
    if not dot:
        base, suffix = logfile, "log"
    return f"{base}_run.{suffix}"
