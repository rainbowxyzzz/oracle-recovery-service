"""Execute the initializer with fake Docker; no database or containers are modified."""

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/oracle21c-ee/initialize.sh"
BASH = str(Path("C:/Program Files/Git/bin/bash.exe")) if os.name == "nt" else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash required")

MOCK_DOCKER = r'''#!/bin/sh
case "$1" in
  ps) echo oracle-recovery-oracle21c-ee ;;
  exec)
    case "$*" in
      *ORACLE_PWD=*) echo ORCLPDB1 ;;
      *'sqlplus" -V'*) echo 'SQL*Plus: Release 21' ;;
      *)
        sql=$(cat)
        printf '%s\n' "$sql" >> "$MOCK_LOG"
        case "$sql" in
          *'select open_mode'*) echo 'READ WRITE' ;;
          *TEMP_META=*)
            [ "${MOCK_METADATA_ERROR:-0}" = 0 ] || exit 7
            printf 'Session altered.\nTEMP_META=%s\n' "${MOCK_METADATA:-NO:8192}"
            ;;
          *'add tempfile'*)
            [ "${MOCK_CREATE_ERROR:-0}" = 0 ] || exit 9
            echo 'PL/SQL procedure successfully completed.'
            ;;
        esac
        ;;
    esac
    ;;
  *) exit 99 ;;
esac
'''


def run_initializer(tmp_path, **overrides):
    docker = tmp_path / "docker"
    docker.write_text(MOCK_DOCKER, encoding="utf-8", newline="\n")
    docker.chmod(0o755)
    log = tmp_path / "sql.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("ORACLE21C_")}
    env.update(ORACLE21C_PASSWORD="test-only", ORACLE21C_STARTUP_TIMEOUT_SECONDS="0",
               ORACLE21C_ENV_FILE=(tmp_path / "absent.env").as_posix(), MOCK_LOG=log.as_posix())
    env.update(overrides)
    result = subprocess.run(
        [BASH, "-c", 'mock_dir=$1; if command -v cygpath >/dev/null 2>&1; then '
         'mock_dir=$(cygpath -u "$mock_dir"); fi; export PATH="$mock_dir:$PATH"; '
         'exec sh "$2"', "test", tmp_path.as_posix(), SCRIPT.as_posix()],
        env=env, capture_output=True, text=True, timeout=20,
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.parametrize("block_size", [2048, 4096, 8192, 16384, 32768])
def test_smallfile_cap_and_startup_completion(tmp_path, block_size):
    result, sql = run_initializer(tmp_path, MOCK_METADATA=f"NO:{block_size}")
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"size (\d+)K autoextend on next (\d+)K maxsize (\d+)K", sql)
    assert match, sql
    initial, step, maximum = map(int, match.groups())
    limit = ((2**22 - 1) * block_size // 1048576) * 1024
    assert maximum == min(100 * 1024**2, limit)
    assert initial == min(20 * 1024**2, maximum)
    assert step == min(2 * 1024**2, maximum)
    assert maximum * 1024 / block_size < 2**22
    assert "initialization completed" in result.stdout
    assert "save state" in sql and "grant read, write" in sql
    assert "if v_count = 0 then" in sql
    assert "regexp_substr(file_name, '[^/]+$') = 'temp_recovery_01.dbf'" in sql
    assert "drop tempfile" not in sql.lower() and "resize" not in sql.lower()


def test_configured_cap_clamps_initial_and_step(tmp_path):
    result, sql = run_initializer(tmp_path, ORACLE21C_TEMPFILE_MAX_SIZE="1g")
    assert result.returncode == 0, result.stderr
    assert "size 1048576K autoextend on next 1048576K maxsize 1048576K" in sql


def test_custom_sizes_block_alignment(tmp_path):
    result, sql = run_initializer(tmp_path, ORACLE21C_TEMPFILE_INITIAL_SIZE="65537K",
                                  ORACLE21C_TEMPFILE_NEXT_SIZE="65537K",
                                  ORACLE21C_TEMPFILE_MAX_SIZE="100001K")
    assert result.returncode == 0, result.stderr
    assert "size 65544K autoextend on next 65544K maxsize 100000K" in sql


def test_bigfile_preserves_existing_file(tmp_path):
    result, sql = run_initializer(tmp_path, MOCK_METADATA="YES:8192")
    assert result.returncode == 0, result.stderr
    assert "BIGFILE" in result.stdout and "initialization completed" in result.stdout
    assert "add tempfile" not in sql


def test_disabled_temp_preserves_old_behavior(tmp_path):
    result, sql = run_initializer(tmp_path, ORACLE21C_TEMP_AUTO_EXTEND="false")
    assert result.returncode == 0, result.stderr
    assert "TEMP_META=" not in sql and "add tempfile" not in sql
    assert "initialization completed" in result.stdout


@pytest.mark.parametrize("overrides", [
    {"MOCK_METADATA_ERROR": "1"}, {"MOCK_METADATA": "garbage"},
    {"MOCK_CREATE_ERROR": "1"}, {"ORACLE21C_TEMPFILE_MAX_SIZE": "0G"},
    {"ORACLE21C_TEMPFILE_INITIAL_SIZE": "1GG"},
])
def test_invalid_metadata_sizes_and_sql_errors_stop_startup(tmp_path, overrides):
    result, _ = run_initializer(tmp_path, **overrides)
    assert result.returncode != 0
    assert "initialization completed" not in result.stdout


def test_script_text_and_shell_syntax():
    raw = SCRIPT.read_bytes()
    raw.decode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    result = subprocess.run([BASH, "-c", 'sh -n "$1"', "syntax", SCRIPT.as_posix()],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
