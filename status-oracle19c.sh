#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  . ./.env
  if [ -f .runtime-databases.env ]; then
    . ./.runtime-databases.env
  fi
  set +a
fi

ORACLE_CONTAINER_NAME="${ORACLE_CONTAINER_NAME:-oracle-recovery-oracle19c}"
ORACLE_PDB="${ORACLE_PDB:-ORCLPDB1}"
ORACLE_HOME_IN_CONTAINER="${ORACLE_HOME_IN_CONTAINER:-/opt/oracle/product/19c/dbhome_1}"

docker ps -a --filter "name=$ORACLE_CONTAINER_NAME" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

if docker ps --format '{{.Names}}' | grep -Fx "$ORACLE_CONTAINER_NAME" >/dev/null 2>&1; then
  if [ -n "${ORACLE_PWD:-}" ]; then
    docker exec \
      -e ORACLE_PWD="$ORACLE_PWD" \
      -e ORACLE_PDB="$ORACLE_PDB" \
      -e ORACLE_HOME_IN_CONTAINER="$ORACLE_HOME_IN_CONTAINER" \
      "$ORACLE_CONTAINER_NAME" \
      bash -lc 'export ORACLE_HOME="$ORACLE_HOME_IN_CONTAINER"; export PATH="$ORACLE_HOME/bin:$PATH"; { echo "set pages 100 lines 200"; echo "select name, open_mode from v\$pdbs;"; echo "select parameter, value from nls_database_parameters order by parameter;"; } | sqlplus -L -s "system/$ORACLE_PWD@//127.0.0.1:1521/$ORACLE_PDB"' || true
  fi
fi
