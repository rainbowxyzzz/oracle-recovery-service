import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy/docker-run/package-images.env"
BASH = str(Path("C:/Program Files/Git/bin/bash.exe")) if os.name == "nt" else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash required")


def test_package_manifest_overrides_legacy_env_image_versions(tmp_path):
    legacy = tmp_path / ".env"
    legacy.write_text(
        "API_IMAGE=legacy-api:old\n"
        "SM4_WORKER_IMAGE=legacy-sm4:old\n"
        "APP_IMAGE_TAR=images/legacy.tar.gz\n",
        encoding="utf-8",
        newline="\n",
    )
    command = (
        '. "$1"; . "$2"; '
        'printf "%s|%s|%s\\n" "$API_IMAGE" "$SM4_WORKER_IMAGE" "$APP_IMAGE_TAR"'
    )

    result = subprocess.run(
        [BASH, "-c", command, "test", legacy.as_posix(), MANIFEST.as_posix()],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    api_image, sm4_image, archive = result.stdout.strip().split("|")
    assert api_image == "oracle-recovery-service-api:20261012-sm4-like-dual-oracle-r1"
    assert sm4_image == "oracle-recovery-service-worker-sm4:20261012-sm4-like-dual-oracle-r1"
    assert archive == "images/oracle-recovery-service-app-images-20261012-sm4-like-dual-oracle-r1.tar.gz"


def test_package_manifest_is_portable_shell_text():
    raw = MANIFEST.read_bytes()
    raw.decode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
