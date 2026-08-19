import re
from dataclasses import dataclass

MULTI_VOLUME_PATTERNS = [
    re.compile(r"(?i)%U"),
    re.compile(r"(?i)expdat\d+\.dmp"),
    re.compile(r"(?i)_\d{2}\.dmp$"),
    re.compile(r"(?i)\.\d{2}\.dmp$"),
]

SINGLE_VOLUME_PATTERNS = [
    re.compile(r"(?i)\.dmp$"),
    re.compile(r"(?i)full.*\.dmp"),
    re.compile(r"(?i)schema.*\.dmp"),
]


@dataclass
class FilenameHint:
    volume_type: str  # single | multi | unknown
    export_mode_hint: str | None  # FULL | SCHEMA | TABLE | None
    base_name: str


def analyze_dump_filename(name: str) -> FilenameHint:
    upper = name.upper()
    export_hint = None
    if "FULL" in upper:
        export_hint = "FULL"
    elif "SCHEMA" in upper or "OWNER" in upper:
        export_hint = "SCHEMA"
    elif "TABLE" in upper or "TBL" in upper:
        export_hint = "TABLE"

    volume_type = "unknown"
    for pat in MULTI_VOLUME_PATTERNS:
        if pat.search(name):
            volume_type = "multi"
            break
    if volume_type == "unknown":
        for pat in SINGLE_VOLUME_PATTERNS:
            if pat.search(name):
                volume_type = "single"
                break

    return FilenameHint(volume_type=volume_type, export_mode_hint=export_hint, base_name=name)
