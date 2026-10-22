"""Standalone preflight: fake Docker, no real container or database mutations."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deploy/oracle21c-ee"
BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="Bash required")

DOCKER = r'''#!/bin/sh
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$1" in
  info) exit 0 ;;
  ps) printf '%s\n' "${MOCK_CONTAINERS:-}" ;;
  inspect)
    case "$*" in
      *State.Running*) echo false ;;
      *Config.Env*) printf '%s\n' ORACLE_SID=ORCLUTF8 ORACLE_PDB=ORCLPDBUTF8 ORACLE_CHARACTERSET=AL32UTF8 INIT_SGA_SIZE=1536 INIT_PGA_SIZE=1024 ;;
      *Destination*) printf '%s\n' '/data/oracle-recovery/oracle21c-utf8/oradata|/opt/oracle/oradata' '/data/oracle-recovery/oracle21c-utf8/tablespaces|/opt/oracle/recovery_tablespaces' '/data/oracle-recovery/oracle21c/dmp|/opt/oracle/recovery_dmp' ;;
      *) printf '%s\n' "${MOCK_OTHER_MOUNT:-/data/oracle-recovery/oracle21c/oradata}" ;;
    esac ;;
  image) exit 0 ;;
  *) exit 99 ;;
esac
'''


def run_entry(tmp_path, replace=None, reset=False, **extra_env):
    for name in ("create-oracle-utf8.sh", "oracle-utf8.env.example"):
        shutil.copyfile(SOURCE / name, tmp_path / name)
    config = (tmp_path / "oracle-utf8.env.example").read_text(encoding="utf-8")
    config = config.replace("ORACLE21C_PASSWORD=''", "ORACLE21C_PASSWORD='test-only'")
    for old, new in (replace or {}).items():
        config = config.replace(old, new)
    (tmp_path / "oracle-utf8.env").write_text(config, encoding="utf-8", newline="\n")
    (tmp_path / ".env").write_text("exit 97\n", encoding="utf-8")
    for name in ("deploy.sh", "initialize.sh"):
        (tmp_path / name).write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{name}' >> \"$MOCK_LOG\"\n"
            "[ \"$ORACLE21C_ENV_FILE\" = /dev/null ]\n"
            "[ \"$ORACLE21C_CHARACTERSET\" = AL32UTF8 ]\n",
            encoding="utf-8", newline="\n",
        )
    for name, text in {"docker": DOCKER, "awk": "#!/bin/sh\necho ${MOCK_AVAILABLE_KB:-33554432}\n"}.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORACLE21C_")}
    env.update(MOCK_LOG=log.as_posix(), **extra_env)
    result = subprocess.run(
        [BASH, "-c", 'dir=$1; if command -v cygpath >/dev/null; then dir=$(cygpath -u "$dir"); fi; '
         'export PATH="$dir:$PATH"; shift; exec sh "$dir/create-oracle-utf8.sh" "$@"',
         "test", tmp_path.as_posix(), *(["--reset"] if reset else [])],
        env=env, capture_output=True, text=True, timeout=15,
    )
    return result, log.read_text() if log.exists() else ""


def test_dedicated_config_and_sequential_helpers(tmp_path):
    result, calls = run_entry(tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls.index("deploy.sh") < calls.index("initialize.sh")
    assert "READY:" in result.stdout
    assert "stop " not in calls and "rm " not in calls


def test_primary_directory_rejected_even_for_reset(tmp_path):
    result, calls = run_entry(tmp_path, reset=True, replace={
        "/data/oracle-recovery/oracle21c-utf8/oradata": "/data/oracle-recovery/oracle21c/oradata"})
    assert result.returncode != 0
    assert "primary Oracle directory" in result.stderr
    assert "stop " not in calls and "deploy.sh" not in calls


@pytest.mark.parametrize("mount", ["/data/oracle-recovery/oracle21c-utf8/oradata", "/data", "/data/oracle-recovery/oracle21c-utf8/oradata/nested"])
def test_other_container_overlap_blocks_reset(tmp_path, mount):
    result, calls = run_entry(tmp_path, reset=True, MOCK_CONTAINERS="primary-ee", MOCK_OTHER_MOUNT=mount)
    assert result.returncode != 0
    assert "overlaps container primary-ee" in result.stderr
    assert "stop " not in calls and "deploy.sh" not in calls


def test_old_container_settings_require_explicit_reset(tmp_path):
    result, calls = run_entry(tmp_path, MOCK_CONTAINERS="oracle-recovery-oracle21c-utf8")
    assert result.returncode != 0
    assert "expected INIT_SGA_SIZE=8192" in result.stderr
    assert "stop " not in calls and "deploy.sh" not in calls


def test_memory_check_precedes_reset(tmp_path):
    result, calls = run_entry(tmp_path, reset=True, MOCK_AVAILABLE_KB="1048576")
    assert result.returncode != 0
    assert "12288MB" in result.stderr
    assert "stop " not in calls and "deploy.sh" not in calls
