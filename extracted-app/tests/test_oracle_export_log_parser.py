import unittest

from recovery_service.engine.oracle.export_log_parser import (
    bind_export_log,
    parse_oracle_export_log,
)


SAMPLE_LOG = """;;; 
Export: Release 19.0.0.0.0 - Production on Wed Jul 1 18:00:02 2026
Version 19.11.0.0.0
Connected to: Oracle Database 19c Enterprise Edition Release 19.0.0.0.0 - Production
;;; ***************************************************************************
;;; Parfile values:
;;;  parfile:  tables=HFASP.T_A,PROJECTLIB.T_B,
;;;  _parfile: PROJECTLIB.T_B,PROJECTLIB.T_C,
;;;  _parfile: ASSET.T_D
;;;  parfile:  parallel=8
;;;  parfile:  compression=data_only
;;;  parfile:  directory=sjbak
;;; ***************************************************************************
Starting "SYSTEM"."SYS_EXPORT_TABLE_04": system/********@ythdb1 dumpfile=cqdsj_%U.dmp logfile=cqdsj.log parfile=/tmp/a.par
W-1 . . exported "HFASP"."T_A" 1.5 MB 10 rows in 1 seconds using direct_path
W-2 . . exported "PROJECTLIB"."T_B":"P1" 2 KB 0 rows in 1 seconds using direct_path
W-3 . . exported "PROJECTLIB"."T_C" 3 KB 5 rows in 1 seconds using direct_path
W-4 . . exported "ASSET"."T_D" 4 KB 7 rows in 1 seconds using direct_path
ORA-39166: Object PROJECTLIB.T_MISSING was not found or could not be exported or imported.
Dump file set for SYSTEM.SYS_EXPORT_TABLE_04 is:
  /mnt/sjbak/cqdsj_01.dmp
  /mnt/sjbak/cqdsj_02.dmp
Job "SYSTEM"."SYS_EXPORT_TABLE_04" completed with 1 error(s) at Wed Jul 1 22:26:21 2026 elapsed 0 04:26:17
"""


class OracleExportLogParserTests(unittest.TestCase):
    def test_parses_table_export_manifest_and_reconciles_source_gap(self):
        manifest = parse_oracle_export_log(SAMPLE_LOG)

        self.assertTrue(manifest.recognized)
        self.assertEqual(manifest.tool, "expdp")
        self.assertEqual(manifest.source_status, "completed_with_errors")
        self.assertEqual(manifest.job_name, '"SYSTEM"."SYS_EXPORT_TABLE_04"')
        self.assertEqual(manifest.export_mode, "tables")
        self.assertEqual(manifest.schemas, ["ASSET", "HFASP", "PROJECTLIB"])
        self.assertEqual(manifest.requested_tables, ["HFASP.T_A", "PROJECTLIB.T_B", "PROJECTLIB.T_C", "ASSET.T_D"])
        self.assertEqual(manifest.duplicate_tables, ["PROJECTLIB.T_B"])
        self.assertEqual(manifest.dump_files, ["cqdsj_01.dmp", "cqdsj_02.dmp"])
        self.assertEqual(manifest.exported_tables, ["ASSET.T_D", "HFASP.T_A", "PROJECTLIB.T_B", "PROJECTLIB.T_C"])
        self.assertEqual(manifest.exported_rows, 22)
        self.assertEqual(manifest.exported_data_units, 4)
        self.assertEqual(manifest.completion_error_count, 1)
        self.assertEqual(manifest.missing_objects, ["PROJECTLIB.T_MISSING"])
        self.assertEqual(manifest.object_counts, {})
        self.assertTrue(manifest.has_source_gaps)
        self.assertTrue(manifest.usable_for_assisted_import)

    def test_exact_binding_requires_the_declared_dump_set(self):
        manifest = parse_oracle_export_log(SAMPLE_LOG)
        binding = bind_export_log(
            manifest,
            log_filename="cqdsj.log",
            actual_dump_files=["cqdsj_01.dmp", "cqdsj_02.dmp"],
        )
        self.assertTrue(binding.exact)
        self.assertEqual(binding.score, 100)

        mismatch = bind_export_log(
            manifest,
            log_filename="cqdsj.log",
            actual_dump_files=["cqdsj_01.dmp"],
        )
        self.assertEqual(mismatch.state, "mismatch")
        self.assertEqual(mismatch.missing_dump_files, ["cqdsj_02.dmp"])

    def test_unrelated_import_log_is_not_accepted(self):
        manifest = parse_oracle_export_log(
            "Import: Release 19.0.0.0.0 - Production\nJob \"SYSTEM\".\"SYS_IMPORT_TABLE_01\" successfully completed"
        )
        self.assertFalse(manifest.recognized)
        self.assertEqual(manifest.source_status, "unrecognized")

    def test_truncated_export_log_is_not_usable(self):
        text = SAMPLE_LOG.split("Job \"", 1)[0]
        manifest = parse_oracle_export_log(text)
        self.assertEqual(manifest.source_status, "incomplete")
        self.assertFalse(manifest.usable_for_assisted_import)


if __name__ == "__main__":
    unittest.main()
