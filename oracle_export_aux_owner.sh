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

cat > "$EXPORT_HOST_DIR/legacy_exp_owner/legacy_owner_aux.par" <<TXT
USERID=$CONNECT
OWNER=CLEANUP_TEST_AUX
FILE=$EXPORT_CONTAINER_DIR/legacy_exp_owner/cleanup_legacy_owner_aux_success.dmp
LOG=$EXPORT_CONTAINER_DIR/legacy_exp_owner/cleanup_legacy_owner_aux_success.log
CONSISTENT=Y
STATISTICS=NONE
TXT

docker exec "$CONTAINER" /bin/bash -c "$EXP PARFILE='$EXPORT_CONTAINER_DIR/legacy_exp_owner/legacy_owner_aux.par'" \
  2>&1 | tee "$EXPORT_HOST_DIR/logs/legacy_owner_aux_success.console.log"

{
  echo
  echo "Additional successful legacy OWNER export:"
  echo "- legacy_exp_owner/cleanup_legacy_owner_aux_success.dmp"
} >> "$EXPORT_HOST_DIR/MANIFEST.txt"

find "$EXPORT_HOST_DIR" -maxdepth 2 -type f -printf "%p\t%s bytes\n" | sort > "$EXPORT_HOST_DIR/FILE_LIST.txt"
echo "$EXPORT_HOST_DIR"
