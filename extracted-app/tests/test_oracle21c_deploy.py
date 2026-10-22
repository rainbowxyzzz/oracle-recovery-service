import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/oracle21c-ee/deploy.sh"
BASH = str(Path("C:/Program Files/Git/bin/bash.exe")) if os.name == "nt" else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash required")


MOCK_DOCKER = r'''#!/bin/sh
case "$1 $2" in
  "info ") exit 0 ;;
  "network inspect") exit 0 ;;
  "ps -a") exit 0 ;;
  "image inspect") exit 0 ;;
  "inspect --type=image") echo amd64 ;;
  *) printf '%s\n' "$*" >> "$MOCK_LOG" ;;
esac
'''


def test_incompatible_host_and_image_architecture_fail_before_container_creation(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text(MOCK_DOCKER, encoding="utf-8", newline="\n")
    docker.chmod(0o755)
    uname = tmp_path / "uname"
    uname.write_text("#!/bin/sh\necho arm64\n", encoding="utf-8", newline="\n")
    uname.chmod(0o755)
    env = os.environ.copy()
    env.update(
        ORACLE21C_ENV_FILE=str(tmp_path / "absent.env"),
        ORACLE21C_PASSWORD="test-only",
    )

    result = subprocess.run(
        [
            BASH,
            "-c",
            'mock_dir=$1; if command -v cygpath >/dev/null 2>&1; then '
            'mock_dir=$(cygpath -u "$mock_dir"); fi; export PATH="$mock_dir:$PATH"; '
            'exec sh "$2"',
            "test",
            tmp_path.as_posix(),
            SCRIPT.as_posix(),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )

    assert result.returncode != 0
    assert "host architecture arm64 is incompatible with Oracle image architecture amd64" in result.stderr


def test_oracle_sid_matches_image_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ORACLE21C_SID must be alphanumeric" in text
    assert "ORACLE21C_SID must be at most 12 characters" in text
    assert 'test -x \'$ORACLE_HOME_PATH/bin/sqlplus\'' in text
    assert 'validate_image_compatibility "$existing_image"' in text
    assert "rm -rf" not in text
