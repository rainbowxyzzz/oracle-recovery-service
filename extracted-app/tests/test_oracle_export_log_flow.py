import argparse
import asyncio
import unittest
from unittest.mock import patch

from recovery_service.core.domain import DumpArtifact, DumpVolumeGroup, RemoteHost
from recovery_service.engine.oracle.export_log_parser import bind_export_log, parse_oracle_export_log
from recovery_service.infrastructure.ssh.async_client import AsyncSSHClient
from recovery_service.orchestrator.oracle_auto_import_runner import OracleAutoImportRunner
from recovery_service.orchestrator.professional_pipeline import _select_export_log
from recovery_service.tools import oracle_dmp_auto_import as tool
from tests.test_oracle_export_log_parser import SAMPLE_LOG


class OracleExportLogFlowTests(unittest.TestCase):
    def setUp(self):
        self.dumps = [
            DumpArtifact("/dmp/cqdsj_01.dmp", "cqdsj_01.dmp", 100),
            DumpArtifact("/dmp/cqdsj_02.dmp", "cqdsj_02.dmp", 100),
        ]
        self.log = DumpArtifact("/dmp/cqdsj.log", "cqdsj.log", len(SAMPLE_LOG))
        self.manifest = parse_oracle_export_log(SAMPLE_LOG)
        self.binding = bind_export_log(
            self.manifest,
            log_filename=self.log.filename,
            actual_dump_files=[item.filename for item in self.dumps],
        )

    def test_direct_directory_selects_one_exact_export_log(self):
        with (
            patch(
                "recovery_service.orchestrator.professional_pipeline.list_remote_artifacts",
                return_value=[*self.dumps, self.log],
            ),
            patch(
                "recovery_service.orchestrator.professional_pipeline.read_remote_text",
                return_value=SAMPLE_LOG,
            ),
        ):
            selected, reports = _select_export_log(
                RemoteHost("db-host"),
                directory="/dmp",
                dump_files=self.dumps,
            )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.artifact.filename, "cqdsj.log")  # type: ignore[union-attr]
        self.assertEqual(selected.binding.state, "exact")  # type: ignore[union-attr]
        self.assertEqual(reports[0]["source_status"], "completed_with_errors")

    def test_multiple_exact_logs_are_ambiguous(self):
        copy_log = DumpArtifact("/dmp/copy.log", "copy.log", len(SAMPLE_LOG))
        with patch(
            "recovery_service.orchestrator.professional_pipeline.read_remote_text",
            return_value=SAMPLE_LOG,
        ):
            selected, reports = _select_export_log(
                RemoteHost("db-host"),
                directory="/dmp",
                dump_files=self.dumps,
                log_files=[self.log, copy_log],
            )

        self.assertIsNone(selected)
        self.assertEqual(
            {item.get("state") for item in reports},
            {"ambiguous_exact_match"},
        )

    def test_source_gap_requires_explicit_execute_authorization(self):
        payload = {
            "filename": self.log.filename,
            "binding": self.binding.to_dict(),
            "manifest": self.manifest.expectation_dict(),
            "accept_source_gaps": False,
        }
        group = DumpVolumeGroup(group_id="g", dump_files=self.dumps)

        with self.assertRaisesRegex(RuntimeError, "尚未授权"):
            OracleAutoImportRunner()._check_export_log_binding(payload, group, execute=True)

        message, detail = OracleAutoImportRunner()._check_export_log_binding(
            payload,
            group,
            execute=False,
        )
        self.assertIn("门禁通过", message)
        self.assertEqual(detail["missing_object_count"], 1)

    def test_remote_tool_revalidates_dump_set_and_schemas(self):
        args = argparse.Namespace(
            export_log_name="cqdsj.log",
            export_log_sha256="abc",
            export_log_status="completed_with_errors",
            export_log_mode="tables",
            export_log_schemas="ASSET,HFASP,PROJECTLIB",
            export_log_dump_files="cqdsj_01.dmp,cqdsj_02.dmp",
            export_log_missing_count=1,
        )
        dump_spec = tool.DumpSpec(
            source_dir="/dmp",
            source_files=["cqdsj_01.dmp", "cqdsj_02.dmp"],
            dumpfile_arg="cqdsj_%U.dmp",
            display_name="cqdsj",
            is_dump_set=True,
        )
        probe = tool.ProbeResult(
            dump_type="datapump",
            schemas=["ASSET", "HFASP", "PROJECTLIB"],
        )
        tool.validate_export_log_expectations(args, dump_spec, probe)

        probe.schemas.remove("PROJECTLIB")
        with self.assertRaisesRegex(RuntimeError, "PROJECTLIB"):
            tool.validate_export_log_expectations(args, dump_spec, probe)

    def test_sftp_close_supports_sync_exit(self):
        client = AsyncSSHClient(RemoteHost("db-host"))

        class SftpStub:
            def __init__(self):
                self.closed = False

            def exit(self):
                self.closed = True
                return None

        stub = SftpStub()
        client._sftp = stub
        asyncio.run(client.close())

        self.assertTrue(stub.closed)
        self.assertIsNone(client._sftp)


if __name__ == "__main__":
    unittest.main()
