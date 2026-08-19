#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

export ORACLE_CHARACTERSET="${ORACLE_CHARACTERSET:-ZHS16GBK}"
export ORACLE_RECREATE=true
export ORACLE_DELETE_ORADATA=true

echo "This will remove the existing Oracle container and delete ORACLE_ORADATA_HOST_PATH."
echo "New Oracle database character set: $ORACLE_CHARACTERSET"
./start-oracle19c.sh
