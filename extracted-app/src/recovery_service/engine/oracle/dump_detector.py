import re
from dataclasses import dataclass, field

from recovery_service.core.domain import DumpArtifact, DumpVolumeGroup


@dataclass(frozen=True)
class OracleDumpDecision:
    tool: str
    dumpfiles: list[str]
    logfile: str
    use_percent_u: bool = False
    confidence: float = 0.0
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    probe_output: str = ""
    metadata_probe: str = ""


def detect_from_text(group: DumpVolumeGroup, related_text: str) -> OracleDumpDecision:
    tool, confidence, reason, evidence = _detect_tool_from_text(related_text)
    dumpfiles, use_percent_u = choose_dumpfiles(group, related_text)
    return OracleDumpDecision(
        tool=tool,
        dumpfiles=dumpfiles,
        logfile=import_logfile_name(group),
        use_percent_u=use_percent_u,
        confidence=confidence,
        reason=reason,
        evidence=evidence,
    )


def detect_with_probe(
    group: DumpVolumeGroup,
    related_text: str,
    *,
    probe_impdp_sqlfile,
    probe_imp_show,
) -> OracleDumpDecision:
    initial = detect_from_text(group, related_text)
    if related_text.strip():
        return initial

    dumpfiles, use_percent_u = choose_dumpfiles(group, related_text)
    probe_dumpfile = dumpfiles[0] if dumpfiles else ""
    evidence: list[str] = []

    impdp_result = probe_impdp_sqlfile(probe_dumpfile)
    impdp_output = _combined_output(impdp_result)
    if getattr(impdp_result, "returncode", 1) == 0:
        return OracleDumpDecision(
            tool="impdp",
            dumpfiles=dumpfiles,
            logfile=import_logfile_name(group),
            use_percent_u=use_percent_u,
            confidence=0.9,
            reason="no related log; impdp SQLFILE probe succeeded",
            evidence=["impdp sqlfile probe succeeded"],
            probe_output=impdp_output,
            metadata_probe=impdp_output,
        )
    if "ORA-39143" in impdp_output.upper() or "NOT A DATA PUMP" in impdp_output.upper():
        return OracleDumpDecision(
            tool="imp",
            dumpfiles=dumpfiles,
            logfile=import_logfile_name(group),
            use_percent_u=False,
            confidence=0.9,
            reason="no related log; impdp reports legacy/non-Data Pump dump",
            evidence=["ORA-39143 from impdp probe"],
            probe_output=impdp_output,
        )
    evidence.append("impdp sqlfile probe failed")

    imp_result = probe_imp_show(probe_dumpfile)
    imp_output = _combined_output(imp_result)
    if getattr(imp_result, "returncode", 1) == 0:
        return OracleDumpDecision(
            tool="imp",
            dumpfiles=[d.filename for d in sorted(group.dump_files, key=lambda item: item.filename)],
            logfile=import_logfile_name(group),
            use_percent_u=False,
            confidence=0.85,
            reason="no related log; imp SHOW=Y probe succeeded",
            evidence=[*evidence, "imp SHOW=Y probe succeeded"],
            probe_output="\n".join([impdp_output, imp_output]),
        )

    return OracleDumpDecision(
        tool="imp",
        dumpfiles=dumpfiles,
        logfile=import_logfile_name(group),
        use_percent_u=use_percent_u,
        confidence=0.15,
        reason="no related log; both impdp SQLFILE and imp SHOW=Y probes failed",
        evidence=[*evidence, "imp SHOW=Y probe failed"],
        probe_output="\n".join([impdp_output, imp_output]),
        metadata_probe=imp_output,
    )


def enrich_with_metadata_probe(
    decision: OracleDumpDecision,
    *,
    probe_impdp_sqlfile=None,
    probe_imp_show=None,
) -> OracleDumpDecision:
    probe_dumpfile = decision.dumpfiles[0] if decision.dumpfiles else ""
    if not probe_dumpfile:
        return decision
    if decision.tool == "impdp" and probe_impdp_sqlfile:
        result = probe_impdp_sqlfile(probe_dumpfile)
        output = _combined_output(result)
        evidence = [*decision.evidence]
        if getattr(result, "returncode", 1) == 0:
            evidence.append("impdp SQLFILE metadata probe succeeded")
        else:
            evidence.append("impdp SQLFILE metadata probe failed")
        return OracleDumpDecision(
            tool=decision.tool,
            dumpfiles=decision.dumpfiles,
            logfile=decision.logfile,
            use_percent_u=decision.use_percent_u,
            confidence=decision.confidence,
            reason=decision.reason,
            evidence=evidence,
            probe_output="\n".join([decision.probe_output, output]),
            metadata_probe=output,
        )
    if decision.tool == "imp" and probe_imp_show:
        result = probe_imp_show(probe_dumpfile)
        output = _combined_output(result)
        evidence = [*decision.evidence]
        if getattr(result, "returncode", 1) == 0:
            evidence.append("imp SHOW=Y metadata probe succeeded")
        else:
            evidence.append("imp SHOW=Y metadata probe failed")
        return OracleDumpDecision(
            tool=decision.tool,
            dumpfiles=decision.dumpfiles,
            logfile=decision.logfile,
            use_percent_u=decision.use_percent_u,
            confidence=decision.confidence,
            reason=decision.reason,
            evidence=evidence,
            probe_output="\n".join([decision.probe_output, output]),
            metadata_probe=output,
        )
    return decision


def choose_dumpfiles(group: DumpVolumeGroup, related_text: str = "") -> tuple[list[str], bool]:
    from_text = _dumpfiles_from_text(related_text)
    if from_text:
        pattern = _percent_u_pattern_from_names(from_text)
        return ([pattern], True) if pattern else (from_text, False)

    dumps = sorted(group.dump_files, key=lambda a: a.filename)
    pattern = _percent_u_pattern(dumps)
    if pattern:
        return [pattern], True
    return [d.filename for d in dumps], False


def import_logfile_name(group: DumpVolumeGroup) -> str:
    if not group.dump_files:
        return "import.log"
    dumps = sorted(group.dump_files, key=lambda a: a.filename)
    names = [d.filename for d in dumps]
    pattern = _percent_u_pattern_from_names(names)
    if pattern:
        base = pattern.replace("%U", "").rsplit(".", 1)[0].strip("_-.") or "dump"
    else:
        base = names[0].rsplit(".", 1)[0]
    existing = {a.filename.upper() for a in group.log_files}
    logfile = f"{base}_import.log"
    if logfile.upper() in existing:
        logfile = f"{base}_run.log"
    return logfile


def _detect_tool_from_text(text: str) -> tuple[str, float, str, list[str]]:
    upper = text.upper()
    if not upper:
        return "imp", 0.25, "no related log; will probe before final tool choice", []
    expdp_markers = ["SYS_EXPORT_", "DATA PUMP", "EXPDP", "PROCESSING OBJECT TYPE"]
    exp_markers = ["EXPORT: RELEASE", "EXPORT:V", "EXPORT DONE IN", "ABOUT TO EXPORT"]
    if any(marker in upper for marker in expdp_markers):
        return "impdp", 0.9, "related text indicates Data Pump export", ["Data Pump marker"]
    if any(marker in upper for marker in exp_markers):
        return "imp", 0.85, "related text indicates legacy exp export", ["legacy exp marker"]
    return "imp", 0.35, "related text inconclusive; will default to imp with retry support", []


def _dumpfiles_from_text(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"\bDUMPFILE\s*=\s*([^\n\r]+)", text, re.I):
        raw = re.split(r"\s+[A-Z_]+\s*=", match.group(1), maxsplit=1)[0]
        values.extend(v.strip().strip("'\"") for v in raw.split(",") if v.strip())
    values.extend(
        m.group(1).strip().strip("'\"")
        for m in re.finditer(r"(?:dump file|dumpfile)\s*[:=]\s*['\"]?([^'\"\s,;]+\.dmp)", text, re.I)
    )
    return _unique(values)


def _percent_u_pattern(dumps: list[DumpArtifact]) -> str | None:
    return _percent_u_pattern_from_names([d.filename for d in dumps])


def _percent_u_pattern_from_names(names: list[str]) -> str | None:
    if len(names) < 2:
        return None
    parsed = [_split_volume_name(name) for name in names]
    if any(item is None for item in parsed):
        return None
    prefixes = {item[0] for item in parsed if item}
    suffixes = {item[2] for item in parsed if item}
    widths = {len(item[1]) for item in parsed if item}
    if len(prefixes) == len(suffixes) == len(widths) == 1:
        first = parsed[0]
        assert first is not None
        return f"{first[0]}%U{first[2]}"
    return None


def _split_volume_name(filename: str) -> tuple[str, str, str] | None:
    match = re.match(r"^(.*?)(\d{2,})(\.[^.]+)$", filename)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _combined_output(result) -> str:
    return "\n".join([getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or ""])


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.upper()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
