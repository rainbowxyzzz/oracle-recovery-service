#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${CONTAINER:-oracle-recovery-oracle19c}"
ORACLE_PWD="${ORACLE_PWD:-ChangeMe_Oracle19c_123}"
ORACLE_PDB="${ORACLE_PDB:-ORCLPDB1}"
ORACLE_HOME_IN_CONTAINER="${ORACLE_HOME_IN_CONTAINER:-/opt/oracle/product/19c/dbhome_1}"
DMP_HOST_ROOT="${DMP_HOST_ROOT:-/data/oracle-recovery/oracle19c/dmp}"
DMP_CONTAINER_ROOT="${DMP_CONTAINER_ROOT:-/opt/oracle/recovery_dmp}"
EXPORT_HOST_DIR="${EXPORT_HOST_DIR:-$(ls -td "$DMP_HOST_ROOT"/export_test_* | head -1)}"
EXPORT_CONTAINER_DIR="$DMP_CONTAINER_ROOT/$(basename "$EXPORT_HOST_DIR")"
EXP="$ORACLE_HOME_IN_CONTAINER/bin/exp"
CONNECT="system/$ORACLE_PWD@//127.0.0.1:1521/$ORACLE_PDB"

mkdir -p "$EXPORT_HOST_DIR/legacy_exp_owner" "$EXPORT_HOST_DIR/legacy_exp_tables" "$EXPORT_HOST_DIR/logs"
chmod -R 777 "$EXPORT_HOST_DIR"

cat > "$EXPORT_HOST_DIR/legacy_exp_full/FAILED_LEGACY_FULL.txt" <<TXT
Legacy exp FULL=Y was attempted, but Oracle 19c returned:
EXP-00058: Password Verify Function for ORA_STIG_PROFILE profile does not exist
EXP-00000: Export terminated unsuccessfully

I left system profiles unchanged and continued with legacy OWNER/TABLES exports.
Data Pump FULL=Y is available in expdp_full/.
TXT

cat > "$EXPORT_HOST_DIR/legacy_exp_owner/legacy_owner.par" <<TXT
USERID=$CONNECT
OWNER=(CLEANUP_TEST_USER,CLEANUP_TEST_AUX)
FILE=$EXPORT_CONTAINER_DIR/legacy_exp_owner/cleanup_legacy_owner.dmp
LOG=$EXPORT_CONTAINER_DIR/legacy_exp_owner/cleanup_legacy_owner.log
CONSISTENT=Y
STATISTICS=NONE
TXT

cat > "$EXPORT_HOST_DIR/legacy_exp_tables/legacy_tables.par" <<TXT
USERID=$CONNECT
TABLES=(CLEANUP_TEST_USER.EXPORT_CUSTOMERS,CLEANUP_TEST_USER.EXPORT_ORDERS,CLEANUP_TEST_AUX.EXPORT_REF_CODES)
FILE=$EXPORT_CONTAINER_DIR/legacy_exp_tables/cleanup_legacy_tables.dmp
LOG=$EXPORT_CONTAINER_DIR/legacy_exp_tables/cleanup_legacy_tables.log
CONSISTENT=Y
STATISTICS=NONE
TXT

run_legacy() {
  local name="$1"
  local parfile="$2"
  echo "===== exp $name =====" | tee -a "$EXPORT_HOST_DIR/logs/export_run.log"
  set +e
  docker exec "$CONTAINER" /bin/bash -c "$EXP PARFILE='$parfile'" 2>&1 | tee "$EXPORT_HOST_DIR/logs/${name}.console.log"
  local code=${PIPESTATUS[0]}
  set -e
  echo "$name exit code: $code" | tee -a "$EXPORT_HOST_DIR/logs/export_run.log"
  return 0
}

run_legacy legacy_owner "$EXPORT_CONTAINER_DIR/legacy_exp_owner/legacy_owner.par"
run_legacy legacy_tables "$EXPORT_CONTAINER_DIR/legacy_exp_tables/legacy_tables.par"

{
  echo "Export directory: $EXPORT_HOST_DIR"
  echo "Container directory: $EXPORT_CONTAINER_DIR"
  echo
  echo "Data Pump exports:"
  echo "- expdp_full/cleanup_expdp_full_01.dmp"
  echo "- expdp_schema/cleanup_expdp_schema.dmp"
  echo "- expdp_table/cleanup_expdp_tables.dmp"
  echo "- expdp_tablespace/cleanup_expdp_tablespaces.dmp"
  echo
  echo "Legacy exp exports:"
  echo "- legacy_exp_owner/cleanup_legacy_owner.dmp"
  echo "- legacy_exp_tables/cleanup_legacy_tables.dmp"
  echo "- legacy_exp_full/FAILED_LEGACY_FULL.txt"
  echo
  echo "Files:"
  find "$EXPORT_HOST_DIR" -maxdepth 2 -type f -printf "%p\t%s bytes\n" | sort
  echo
  echo "Disk:"
  df -h "$EXPORT_HOST_DIR"
} | tee "$EXPORT_HOST_DIR/MANIFEST.txt"

echo "$EXPORT_HOST_DIR"
