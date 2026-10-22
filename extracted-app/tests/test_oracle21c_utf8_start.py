import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/oracle21c-ee/start-oracle21c-utf8.sh"
PACKAGE_SCRIPT = ROOT / "deploy/oracle21c-ee/package-start-oracle21c.sh"
BASH = str(Path("C:/Program Files/Git/bin/bash.exe")) if os.name == "nt" else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash required")


def test_utf8_profile_reuses_image_and_isolates_database_files(tmp_path):
    script_dir = tmp_path / "oracle21c-ee"
    script_dir.mkdir()
    shutil.copyfile(SCRIPT, script_dir / SCRIPT.name)
    (script_dir / "start-oracle21c.sh").write_text(
        "#!/bin/sh\nenv | sort\n",
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / ".env").write_text(
        "ORACLE21C_IMAGE=softwareplant/oracle:clean-21.3.0-ee\n"
        "ORACLE21C_PASSWORD=test-password\n"
        "ORACLE21C_DMP_HOST_PATH=/data/oracle-recovery/oracle21c/dmp\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [BASH, (script_dir / SCRIPT.name).as_posix()],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "ORACLE21C_MODE=auto" in result.stdout
    assert "ORACLE21C_IMAGE=softwareplant/oracle:clean-21.3.0-ee" in result.stdout
    assert "ORACLE21C_CONTAINER=oracle-recovery-oracle21c-utf8" in result.stdout
    assert "ORACLE21C_PDB=ORCLPDBUTF8" in result.stdout
    assert "ORACLE21C_CHARACTERSET=AL32UTF8" in result.stdout
    assert "ORACLE21C_ORADATA_HOST_PATH=/data/oracle-recovery/oracle21c-utf8/oradata" in result.stdout
    assert "ORACLE21C_DMP_HOST_PATH=/data/oracle-recovery/oracle21c/dmp" in result.stdout


def test_package_entry_starts_primary_then_utf8():
    text = PACKAGE_SCRIPT.read_text(encoding="utf-8")
    assert 'sh "$SCRIPT_DIR/oracle21c-ee/start-oracle21c.sh"' in text
    assert 'sh "$SCRIPT_DIR/oracle21c-ee/start-oracle21c-utf8.sh"' in text


def test_package_entry_executes_isolated_primary_and_utf8_profiles_in_order(tmp_path):
    script_dir = tmp_path / "oracle21c-ee"
    script_dir.mkdir()
    shutil.copyfile(PACKAGE_SCRIPT, tmp_path / "start-oracle21c.sh")
    shutil.copyfile(SCRIPT, script_dir / SCRIPT.name)
    (script_dir / "start-oracle21c.sh").write_text(
        "#!/bin/sh\n"
        "[ ! -f \"$ORACLE21C_ENV_FILE\" ] || . \"$ORACLE21C_ENV_FILE\"\n"
        "printf '%s|%s|%s|%s|%s|%s\\n' \"${ORACLE21C_CONTAINER:-}\" "
        "\"${ORACLE21C_SID:-}\" \"${ORACLE21C_PDB:-}\" \"${ORACLE21C_CHARACTERSET:-}\" "
        "\"${ORACLE21C_ORADATA_HOST_PATH:-}\" \"${ORACLE21C_DMP_HOST_PATH:-}\" >> \"$MOCK_LOG\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / ".env").write_text(
        "ORACLE21C_CONTAINER=oracle-primary\n"
        "ORACLE21C_SID=ORCL21C\n"
        "ORACLE21C_PDB=ORCLPDB1\n"
        "ORACLE21C_PASSWORD=test-password\n"
        "ORACLE21C_CHARACTERSET=ZHS16GBK\n"
        "ORACLE21C_ORADATA_HOST_PATH=/data/oracle-recovery/oracle21c/oradata\n"
        "ORACLE21C_DMP_HOST_PATH=/data/oracle-recovery/oracle21c/dmp\n"
        "UTF_ORACLE21C_CONTAINER=oracle-utf8\n"
        "UTF_ORACLE21C_SID=ORCLUTF8\n"
        "UTF_ORACLE21C_PDB=ORCLPDBUTF8\n"
        "UTF_ORACLE21C_CHARACTERSET=AL32UTF8\n"
        "UTF_ORACLE21C_ORADATA_HOST_PATH=/data/oracle-recovery/oracle21c-utf8/oradata\n",
        encoding="utf-8",
        newline="\n",
    )
    log = tmp_path / "profiles.log"
    env = os.environ.copy()
    env["MOCK_LOG"] = log.as_posix()

    result = subprocess.run(
        [BASH, (tmp_path / "start-oracle21c.sh").as_posix()],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "oracle-primary|ORCL21C|ORCLPDB1|ZHS16GBK|"
        "/data/oracle-recovery/oracle21c/oradata|/data/oracle-recovery/oracle21c/dmp",
        "oracle-utf8|ORCLUTF8|ORCLPDBUTF8|AL32UTF8|"
        "/data/oracle-recovery/oracle21c-utf8/oradata|/data/oracle-recovery/oracle21c/dmp",
    ]


def test_utf8_profile_recovers_password_from_primary_container(tmp_path):
    script_dir = tmp_path / "oracle21c-ee"
    script_dir.mkdir()
    shutil.copyfile(SCRIPT, script_dir / SCRIPT.name)
    (script_dir / "start-oracle21c.sh").write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$ORACLE21C_PASSWORD\"\n",
        encoding="utf-8",
        newline="\n",
    )
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'ORACLE_SID=ORCL21C' 'ORACLE_PWD=reused-primary-password'\n",
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ORACLE21C_", "UTF_ORACLE21C_"))}

    result = subprocess.run(
        [
            BASH,
            "-c",
            'mock_dir=$1; if command -v cygpath >/dev/null 2>&1; then '
            'mock_dir=$(cygpath -u "$mock_dir"); fi; export PATH="$mock_dir:$PATH"; '
            'exec sh "$2"',
            "test",
            tmp_path.as_posix(),
            (script_dir / SCRIPT.name).as_posix(),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reused-primary-password"
