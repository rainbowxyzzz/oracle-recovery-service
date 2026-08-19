from recovery_service.common.utils.patterns import analyze_dump_filename
from recovery_service.core.domain import DumpArtifact, DumpVolumeGroup
from recovery_service.core.enums import DumpVolumeType
import re


def classify_artifacts(artifacts: list[DumpArtifact]) -> list[DumpVolumeGroup]:
    dumps = [a for a in artifacts if a.filename.lower().endswith(".dmp")]
    logs = [a for a in artifacts if a.filename.lower().endswith(".log")]
    pars = [a for a in artifacts if a.filename.lower().endswith(".par")]

    if not dumps:
        return []

    groups: dict[str, DumpVolumeGroup] = {}

    for d in dumps:
        hint = analyze_dump_filename(d.filename)
        if hint.volume_type == "multi":
            base = _multi_volume_base(d.filename)
            gid = f"multi:{base}"
            vol_type = DumpVolumeType.MULTI
        else:
            gid = f"single:{d.filename}"
            vol_type = DumpVolumeType.SINGLE

        if gid not in groups:
            groups[gid] = DumpVolumeGroup(group_id=gid, volume_type=vol_type)
        groups[gid].dump_files.append(d)

    for g in groups.values():
        g.log_files = _match_related(logs, g.dump_files)
        g.par_files = _match_related(pars, g.dump_files)

    return list(groups.values())


def _multi_volume_base(filename: str) -> str:
    if "%" in filename:
        return filename.split("%", 1)[0].rstrip("_.-") or filename
    match = re.match(r"^(.*?)(\d{2,})(\.[^.]+)$", filename)
    if match:
        return match.group(1).rstrip("_.-") or filename.rsplit(".", 1)[0]
    return filename.rsplit(".", 1)[0].rstrip("_.-") or filename


def _match_related(extras: list[DumpArtifact], dumps: list[DumpArtifact]) -> list[DumpArtifact]:
    if not extras or not dumps:
        return extras
    base = dumps[0].filename.rsplit(".", 1)[0].lower()
    matched = [e for e in extras if base[: min(len(base), 8)] in e.filename.lower()]
    return matched if matched else extras
