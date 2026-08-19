from recovery_service.common.utils.patterns import analyze_dump_filename
from recovery_service.core.domain import DumpArtifact, DumpVolumeGroup, ExportMetadata
from recovery_service.core.enums import DumpVolumeType, ImportMode


def infer_from_filenames(group: DumpVolumeGroup) -> ExportMetadata | None:
    if not group.dump_files:
        return None

    primary = group.dump_files[0]
    hint = analyze_dump_filename(primary.filename)
    meta = ExportMetadata(discovery_source="filename", confidence=0.55)

    if hint.export_mode_hint:
        meta.export_mode = ImportMode(hint.export_mode_hint)

    if group.volume_type == DumpVolumeType.MULTI or hint.volume_type == "multi":
        meta.dumpfile_param = _build_multivolume_pattern(group.dump_files)
    else:
        meta.dumpfile_param = primary.filename

    if meta.dumpfile_param or meta.export_mode != ImportMode.UNKNOWN:
        return meta
    return None


def _build_multivolume_pattern(files: list[DumpArtifact]) -> str:
    names = [f.filename for f in files]
    if any("%U" in n.upper() for n in names):
        base = names[0].split("%")[0]
        return f"{base}%U.dmp"
    if len(names) == 1:
        return names[0]
    return ",".join(sorted(names))
