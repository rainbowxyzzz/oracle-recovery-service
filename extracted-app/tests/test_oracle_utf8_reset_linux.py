"""Linux-only filesystem regression; fake Docker, real directory archival.

Run: ORACLE_UTF8_FILESYSTEM_TEST=1 python3 test_oracle_utf8_reset_linux.py
Uses a unique fixture below /data/oracle-recovery; never uses real DB directories.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(os.environ.get("ORACLE_UTF8_SCRIPT_DIR", Path(__file__).resolve().parents[2] / "deploy/oracle21c-ee"))
DOCKER = r'''#!/bin/sh
printf '%s\n' "$*" >> "$TEST_ROOT/calls.log"
case "$1" in
 info|image|stop|rm) exit 0 ;;
 ps) printf '%s\n' primary-ee; [ "$TEST_EXISTS" = no ] || echo oracle-recovery-oracle21c-utf8; exit 0 ;;
 inspect)
  case "$*" in
   *State.Running*) echo false ;;
   *Config.Env*) printf '%s\n' ORACLE_SID=ORCLUTF8 ORACLE_PDB=ORCLPDBUTF8 ORACLE_CHARACTERSET=AL32UTF8 INIT_SGA_SIZE=1536 INIT_PGA_SIZE=1024 ;;
   *Destination*) printf '%s\n' "$TEST_ROOT/utf/oradata|/opt/oracle/oradata" "$TEST_ROOT/utf/tablespaces|/opt/oracle/recovery_tablespaces" "$TEST_ROOT/main/dmp|/opt/oracle/recovery_dmp" ;;
   *Source*) echo "$TEST_ROOT/main" ;;
   *) echo '[{"Name":"oracle-recovery-oracle21c-utf8"}]' ;;
  esac ;;
 *) exit 99 ;;
esac
'''


@unittest.skipUnless(os.name == "posix" and os.environ.get("ORACLE_UTF8_FILESYSTEM_TEST") == "1", "explicit Linux fixture run required")
class ResetFilesystemTest(unittest.TestCase):
    def setUp(self):
        parent = Path("/data/oracle-recovery")
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="codex-utf8-test-", dir=str(parent))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("utf/oradata", "utf/tablespaces", "main/dmp", "main/oradata"):
            path = self.root / name
            path.mkdir(parents=True)
            (path / "keep.txt").write_text(name)
        for name in ("create-oracle-utf8.sh", "oracle-utf8.env.example"):
            shutil.copyfile(SOURCE / name, self.root / name)
        config = (self.root / "oracle-utf8.env.example").read_text()
        config = config.replace("ORACLE21C_PASSWORD=''", "ORACLE21C_PASSWORD='fixture-only'")
        config = config.replace("/data/oracle-recovery/oracle21c-utf8", str(self.root / "utf"))
        config = config.replace("/data/oracle-recovery/oracle21c/dmp", str(self.root / "main/dmp"))
        (self.root / "oracle-utf8.env").write_text(config)
        for name, content in {
            "docker": DOCKER,
            "awk": "#!/bin/sh\necho 33554432\n",
            "deploy.sh": '#!/bin/sh\nset -eu\n[ ! -e "$ORACLE21C_ORADATA_HOST_PATH" ]\n[ ! -e "$ORACLE21C_TABLESPACE_HOST_PATH" ]\nmkdir -p "$ORACLE21C_ORADATA_HOST_PATH" "$ORACLE21C_TABLESPACE_HOST_PATH"\necho deploy.sh >> "$TEST_ROOT/calls.log"\n',
            "initialize.sh": '#!/bin/sh\necho initialize.sh >> "$TEST_ROOT/calls.log"\n',
        }.items():
            path = self.root / name
            path.write_text(content)
            path.chmod(0o755)

    def run_entry(self, reset=True, exists=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORACLE21C_")}
        env.update(TEST_ROOT=str(self.root), TEST_EXISTS="yes" if exists else "no", PATH=str(self.root) + ":" + env["PATH"])
        return subprocess.run(["sh", str(self.root / "create-oracle-utf8.sh"), *(["--reset"] if reset else [])],
                              env=env, capture_output=True, text=True, timeout=15)

    def test_reset_preserves_database_files_and_shared_dmp(self):
        result = self.run_entry()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("oradata", "tablespaces"):
            backups = list((self.root / "utf").glob(name + ".backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "keep.txt").read_text(), "utf/" + name)
            self.assertEqual(list((self.root / "utf" / name).iterdir()), [])
        for name in ("main/dmp", "main/oradata"):
            self.assertEqual((self.root / name / "keep.txt").read_text(), name)
        snapshots = list((self.root / "utf").glob("*.container-backup-*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)
        calls = (self.root / "calls.log").read_text().splitlines()
        mutations = [line for line in calls if line.startswith(("stop ", "rm "))]
        self.assertEqual(mutations, ["stop oracle-recovery-oracle21c-utf8", "rm oracle-recovery-oracle21c-utf8"])
        self.assertEqual(calls[-2:], ["deploy.sh", "initialize.sh"])

    def test_residual_files_without_container_are_not_reused(self):
        result = self.run_entry(reset=False, exists=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("directory is not empty", result.stderr)
        self.assertEqual((self.root / "utf/oradata/keep.txt").read_text(), "utf/oradata")
        self.assertNotIn("deploy.sh", (self.root / "calls.log").read_text())

    def test_symlink_reset_rejected(self):
        link = self.root / "utf/link"
        link.symlink_to(self.root / "utf/oradata", target_is_directory=True)
        config = self.root / "oracle-utf8.env"
        config.write_text(config.read_text().replace("/utf/oradata", "/utf/link"))
        result = self.run_entry()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual((link / "keep.txt").read_text(), "utf/oradata")


if __name__ == "__main__":
    unittest.main(verbosity=2)
