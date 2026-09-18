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
