import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from recovery_service.core.models.task import RecoveryTask
from recovery_service.orchestrator.oracle_auto_import_runner import (
    OracleAutoImportRunner,
    oracle_datapump_job_candidates,
    oracle_datapump_job_name,
)
from recovery_service.tools import oracle_dmp_auto_import as tool

try:
    from recovery_service.api.v1.tasks import _oracle_log_access, _sanitize_oracle_log_artifact
except ImportError:
    _oracle_log_access = None
    _sanitize_oracle_log_artifact = None


class OracleAutoImportResilienceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "fcntl run locks are only available on POSIX hosts")
    def test_same_run_directory_cannot_be_locked_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            first = tool.acquire_run_lock(run_dir)
            try:
                with self.assertRaises(tool.RunAlreadyActiveError):
                    tool.acquire_run_lock(run_dir)
            finally:
                tool.release_run_lock(first)

            second = tool.acquire_run_lock(run_dir)
            tool.release_run_lock(second)

    def test_generated_directory_name_is_bounded_and_task_unique(self):
        first = tool.generated_directory_name("CQDSJ_20260701_180002_SET", "task_aaaaaaaa")
        second = tool.generated_directory_name("CQDSJ_20260701_180002_SET", "task_bbbbbbbb")

        self.assertLessEqual(len(first), 30)
        self.assertLessEqual(len(second), 30)
        self.assertTrue(first.startswith("DIR_CQDSJ_20_"))
        self.assertNotEqual(first, second)

    def test_direct_runtime_separates_shared_dump_and_work_directories(self):
        args = argparse.Namespace(
            run_id="task_direct_1",
            runs_dir="/tmp/runs",
            container_dir="/opt/oracle/recovery_dmp/auto_import",
            direct_container_dir="/opt/oracle/recovery_dmp",
        )
        dump_spec = tool.DumpSpec(
            source_dir="/data/oracle-recovery/oracle19c/dmp",
            source_files=["sample_01.dmp", "sample_02.dmp"],
            dumpfile_arg="sample_%U.dmp",
            display_name="sample_set",
            is_dump_set=True,
        )

        ctx = tool.build_runtime_context(args, dump_spec, tool.dt.datetime(2026, 7, 16, 10, 0, 0))

        self.assertEqual(ctx.container_dump_dir, "/opt/oracle/recovery_dmp")
        self.assertEqual(
            ctx.container_import_dir,
            "/opt/oracle/recovery_dmp/auto_import/task_direct_1",
        )
        self.assertTrue(ctx.zero_copy_dump)

    def test_direct_impdp_reads_shared_directory_and_writes_to_work_directory(self):
        args = argparse.Namespace(
            username="system",
            password="secret",
            connect="@ORCLPDB1",
            container="oracle",
            directory_object="DIR_TASK_WORK",
            dump_directory_object="RECOVERY_DMP_DIR",
            target_user="",
            keep_source_schema=False,
            target_schema_prefix="",
            target_tablespace="",
            target_tablespace_prefix="",
            table_exists_action="REPLACE",
            import_mode="schemas",
            on_conflict="recreate",
            exclude_user_metadata=True,
            exclude_directory=True,
            exclude_object_grants=True,
        )
        dump_spec = tool.DumpSpec(
            source_dir="/data/oracle-recovery/oracle19c/dmp",
            source_files=["sample_01.dmp", "sample_02.dmp"],
            dumpfile_arg="sample_%U.dmp",
            display_name="sample_set",
            is_dump_set=True,
        )
        ctx = tool.RuntimeContext(
            run_id="task_direct_1",
            run_dir="/tmp/runs/task_direct_1",
            probe_dir="/tmp/runs/task_direct_1/probe",
            cleanup_dir="/tmp/runs/task_direct_1/cleanup",
            import_dir="/tmp/runs/task_direct_1/import",
            local_plan_path="/tmp/runs/task_direct_1/plan.json",
            container_import_dir="/opt/oracle/recovery_dmp/auto_import/task_direct_1",
            dumpfile_arg="sample_%U.dmp",
            dump_display_name="sample_set",
            container_dump_dir="/opt/oracle/recovery_dmp",
            zero_copy_dump=True,
        )
        probe = tool.ProbeResult(
            dump_type="datapump",
            schemas=["SOURCE_USER"],
            tablespaces=["SOURCE_TBS"],
        )

        plan = tool.build_plan(
            args,
            ctx,
            dump_spec,
            probe,
            "/opt/oracle/recovery_tablespaces",
            tool.dt.datetime(2026, 7, 16, 10, 0, 0),
        )

        command = " ".join(plan.commands[0])
        self.assertIn("DIRECTORY=DIR_TASK_WORK", command)
        self.assertIn("DUMPFILE=RECOVERY_DMP_DIR:sample_%U.dmp", command)
        self.assertIn("JOB_NAME=ORS_IMPORT_JOB", command)
        self.assertEqual(plan.dump_directory_object, "RECOVERY_DMP_DIR")

    def test_datapump_job_name_is_task_scoped_and_oracle_compatible(self):
        first = oracle_datapump_job_name(
            "b9364b95-076e-4ba3-bbca-a92500ee699e",
            "task_b9364b95076e4ba3_a1b2c3d4e5f6",
        )
        second = oracle_datapump_job_name(
            "b9364b95-076e-4ba3-bbca-a92500ee699e",
            "task_b9364b95076e4ba3_ffeeddccbbaa",
        )
        self.assertRegex(first, r"^[A-Z][A-Z0-9_]{1,27}$")
        self.assertLessEqual(len(first), 28)
        self.assertNotEqual(first, second)
        candidates = oracle_datapump_job_candidates(first)
        self.assertEqual(candidates[0], first)
        self.assertIn(f"{first}_F", candidates)
        self.assertIn(f"{first}_P1", candidates)

    def test_oracle_home_prefix_falls_back_instead_of_forcing_invalid_config(self):
        runner = OracleAutoImportRunner()
        prefix = runner._oracle_env_prefix("/configured/but/missing")

        self.assertIn("configured_oracle_home=/configured/but/missing", prefix)
        self.assertIn("oracle_home_valid", prefix)
        self.assertIn("sp1*.msb", prefix)
        self.assertIn("command -v sqlplus", prefix)
        self.assertNotIn("export ORACLE_HOME=/configured/but/missing", prefix)
        self.assertNotIn(
            "ORACLE_HOME",
            runner._remote_command(["python3", "/tmp/tool.py"], "/configured/but/missing"),
        )

    def test_oracle_tool_check_rejects_sqlplus_message_initialization_error(self):
        result = SimpleNamespace(
            returncode=0,
            stdout="Error 6 initializing SQL*Plus\nSP2-0667: Message file sp1<lang>.msb not found",
            stderr="",
        )
        with patch(
            "recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command",
            return_value=result,
        ):
            with self.assertRaisesRegex(RuntimeError, "SP2-0667"):
                OracleAutoImportRunner()._check_oracle_tools(
                    SimpleNamespace(),
                    "oracle",
                    "/configured/but/missing",
                )

    def test_oracle_tool_check_reports_resolved_home_after_real_sqlplus_start(self):
        result = SimpleNamespace(
            returncode=0,
            stdout=(
                "ORACLE_HOME=/u01/app/oracle/product/19c/dbhome_1\n"
                "/u01/app/oracle/product/19c/dbhome_1/bin/sqlplus\n"
                "/u01/app/oracle/product/19c/dbhome_1/bin/impdp\n"
                "SQL*Plus: Release 19.0.0.0.0 - Production\n"
            ),
            stderr="",
        )
        with patch(
            "recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command",
            return_value=result,
        ):
            message, detail = OracleAutoImportRunner()._check_oracle_tools(
                SimpleNamespace(),
                "oracle",
                "/configured/but/missing",
            )

        self.assertIn("真实 ORACLE_HOME", message)
        self.assertEqual(detail["oracle_home"], "/u01/app/oracle/product/19c/dbhome_1")
        self.assertTrue(any("SQL*Plus: Release" in line for line in detail["paths_and_version"]))

    def test_stop_request_uses_stop_job_and_current_task_candidates(self):
        commands = []

        def fake_run(_host, command, **_kwargs):
            commands.append(command)
            success = "stop.request" in command or "ATTACH=ORS_TASK_RUN" in command
            return SimpleNamespace(returncode=0 if success else 1, stdout="", stderr="")

        with patch(
            "recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command",
            side_effect=fake_run,
        ):
            result = OracleAutoImportRunner().stop(
                oracle_host=SimpleNamespace(),
                run_dir="/opt/oracle-recovery-service-package/oracle-auto-import-runs/task_1",
                container="oracle-recovery-oracle19c",
                username="SYSTEM",
                password="secret",
                pdb="ORCLPDB1",
                job_name="ORS_TASK_RUN",
                reason="manual stop",
                force=False,
            )

        joined = "\n".join(commands)
        self.assertIn("stop.request", joined)
        self.assertIn("STOP_JOB=IMMEDIATE", joined)
        self.assertNotIn("KILL_JOB", joined)
        self.assertIn("ORS_TASK_RUN", result["stopped_jobs"])
        self.assertFalse(result["process_signal_sent"])

    def test_force_stop_uses_kill_job_and_signals_only_recorded_run(self):
        commands = []

        def fake_run(_host, command, **_kwargs):
            commands.append(command)
            stdout = "signalled" if ".active.lock" in command else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with patch(
            "recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command",
            side_effect=fake_run,
        ):
            result = OracleAutoImportRunner().stop(
                oracle_host=SimpleNamespace(),
                run_dir="/managed/task_1",
                container="oracle-recovery-oracle19c",
                username="SYSTEM",
                password="secret",
                pdb="ORCLPDB1",
                job_name="ORS_TASK_RUN",
                reason="force stop",
                force=True,
            )

        joined = "\n".join(commands)
        self.assertIn("KILL_JOB", joined)
        self.assertIn("/managed/task_1/.active.lock", joined)
        self.assertNotIn("pkill impdp", joined)
        self.assertTrue(result["process_signal_sent"])

    def test_stop_file_prevents_starting_another_remote_command(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "stop.request").write_text("stop", encoding="utf-8")
            logger = tool.RunLogger(run_dir, "system", "secret")
            with patch.object(tool.subprocess, "Popen") as popen:
                with self.assertRaises(tool.ImportStopRequested):
                    tool.run_process(["sleep", "10"], logger=logger)
            popen.assert_not_called()

    def test_existing_shared_dump_directory_is_reused_without_replace(self):
        args = argparse.Namespace(
            dump_directory_object="RECOVERY_DMP_DIR",
            container="oracle",
            username="system",
            password="secret",
            connect="@ORCLPDB1",
        )
        dump_spec = tool.DumpSpec(
            source_dir="/data/oracle-recovery/oracle19c/dmp",
            source_files=["sample.dmp"],
            dumpfile_arg="sample.dmp",
            display_name="sample",
            is_dump_set=False,
        )
        logger = Mock()

        with (
            patch.object(tool, "directory_path", return_value="/opt/oracle/recovery_dmp"),
            patch.object(tool, "grant_directory_access") as grant,
            patch.object(tool, "create_directory") as create,
            patch.object(tool, "verify_dump_directory") as verify,
        ):
            tool.ensure_reusable_dump_directory(
                args,
                dump_spec,
                "/opt/oracle/recovery_dmp",
                logger,
            )

        create.assert_not_called()
        grant.assert_called_once_with(args, "RECOVERY_DMP_DIR", logger)
        verify.assert_called_once_with(
            args,
            "RECOVERY_DMP_DIR",
            "/opt/oracle/recovery_dmp",
            ["sample.dmp"],
            logger,
        )

    def test_shared_dump_directory_path_mismatch_stops_without_replace(self):
        args = argparse.Namespace(dump_directory_object="RECOVERY_DMP_DIR")
        dump_spec = tool.DumpSpec(
            source_dir="/data/oracle-recovery/oracle19c/dmp",
            source_files=["sample.dmp"],
            dumpfile_arg="sample.dmp",
            display_name="sample",
            is_dump_set=False,
        )

        with (
            patch.object(tool, "directory_path", return_value="/wrong/path"),
            patch.object(tool, "create_directory") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "Refusing CREATE OR REPLACE"):
                tool.ensure_reusable_dump_directory(
                    args,
                    dump_spec,
                    "/opt/oracle/recovery_dmp",
                    Mock(),
                )

        create.assert_not_called()

    def test_probe_failure_classification(self):
        cases = {
            "ORA-39087: directory name is invalid": "directory_invalid",
            "ORA-39054: missing or invalid SQLFILE": "sqlfile_invalid",
            "ORA-39059: dump file set is incomplete": "dump_set_incomplete",
            "ORA-31640: unable to open dump file": "dump_file_inaccessible",
            "ORA-31655: no data or metadata objects selected": "metadata_not_selected",
            "ORA-39143: dump file may be an original export dump file": "legacy_exp",
            "ORA-39002: invalid operation": "unknown_dump_type",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(tool.classify_probe_failure(message), expected)

    def test_serialized_plan_never_contains_executable_commands(self):
        plan = SimpleNamespace()
        plan_data = {
            "commands": [["impdp", "system/secret@ORCLPDB1"]],
            "fallback_commands": [["impdp", "system/secret@ORCLPDB1"]],
            "masked_commands": ["impdp system/******@ORCLPDB1"],
        }
        with patch.object(tool, "asdict", return_value=plan_data):
            serialized = tool.plan_to_json(plan)
        self.assertEqual(serialized["commands"], [])
        self.assertEqual(serialized["fallback_commands"], [])
        self.assertNotIn("secret", str(serialized))

    @unittest.skipIf(
        _sanitize_oracle_log_artifact is None,
        "API container code is not installed in this runtime",
    )
    def test_historical_plan_download_masks_oracle_password(self):
        original = b'{"command":"impdp system/ChangeMe_123@ORCLPDB1 DIRECTORY=TEST"}'
        sanitized = _sanitize_oracle_log_artifact("plan.json", original)  # type: ignore[misc]
        self.assertNotIn(b"ChangeMe_123", sanitized)
        self.assertIn(b"system/******@ORCLPDB1", sanitized)

    def test_directory_error_is_retried_once_and_not_reported_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            attempt_results = [
                (1, "ORA-39087: directory name is invalid"),
                (1, "ORA-39087: directory name is invalid"),
            ]

            def fake_probe_attempt(*args, **kwargs):
                returncode, output = attempt_results.pop(0)
                number = kwargs.get("attempt_number") or args[4]
                result = subprocess.CompletedProcess([], returncode, output)
                item = {
                    "attempt": number,
                    "returncode": returncode,
                    "failure_code": "directory_invalid",
                    "sqlfile": str(tmp_path / f"probe_{number}.sql"),
                    "logfile": str(tmp_path / f"probe_{number}.log"),
                }
                return result, "", "", output, item

            args = argparse.Namespace(
                username="system",
                password="secret",
                connect="@ORCLPDB1",
                directory_object="DIR_TEST_123",
                container="oracle",
            )
            ctx = tool.RuntimeContext(
                run_id="task_1",
                run_dir=str(tmp_path),
                probe_dir=str(tmp_path / "probe"),
                cleanup_dir=str(tmp_path / "cleanup"),
                import_dir=str(tmp_path / "import"),
                local_plan_path=str(tmp_path / "plan.json"),
                container_import_dir="/tmp/auto/task_1",
                dumpfile_arg="test.dmp",
                dump_display_name="test.dmp",
            )
            logger = tool.RunLogger(tmp_path, "system", "secret")
            create = Mock()
            verify = Mock()
            with (
                patch.object(tool, "probe_attempt", side_effect=fake_probe_attempt),
                patch.object(tool, "create_directory", create),
                patch.object(tool, "verify_directory", verify),
            ):
                result = tool.probe_dump(args, ctx, logger)

            self.assertEqual(result.dump_type, "probe_failed")
            self.assertEqual(result.failure_code, "directory_invalid")
            self.assertEqual(len(result.attempts), 2)
            create.assert_called_once()
            verify.assert_called_once()

    def test_runner_manifest_rejects_parent_paths(self):
        stdout = "run.log\t12\t100.5\n../secret\t10\t100.5\nprobe/a.log\t4\t101.5\n"
        result = SimpleNamespace(returncode=0, stdout=stdout)
        with patch(
            "recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command",
            return_value=result,
        ):
            manifest = OracleAutoImportRunner()._list_log_artifacts(
                SimpleNamespace(),
                "/opt/oracle-recovery-service-package/oracle-auto-import-runs/task_1",
            )

        self.assertEqual(
            [item["relative_path"] for item in manifest],
            ["probe/a.log", "run.log"],
        )

    @unittest.skipIf(_oracle_log_access is None, "API container code is not installed in this runtime")
    def test_log_access_rejects_path_outside_managed_root(self):
        task = RecoveryTask(
            remote_host="source",
            remote_user="source",
            remote_password_enc="",
            remote_directory="/backup",
            target_connection="db",
            target_admin_user="system",
            target_admin_password_enc="",
            metadata_snapshot={"oracle_auto_import_run_dir": "/tmp/task_1"},
            options={
                "professional_flow": {
                    "oracle_docker": {
                        "host": "db-host",
                        "port": 22,
                        "user": "root",
                        "password": "",
                    }
                }
            },
        )

        with self.assertRaises(Exception) as context:
            _oracle_log_access(task)  # type: ignore[misc]
        self.assertEqual(getattr(context.exception, "status_code", None), 409)


if __name__ == "__main__":
    unittest.main()
