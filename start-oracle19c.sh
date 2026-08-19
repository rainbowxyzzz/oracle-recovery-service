#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Missing .env" >&2
  exit 1
fi

set -a
. ./.env
set +a

if [ -f ./runtime-database-lib.sh ]; then
  . ./runtime-database-lib.sh
else
  echo "Missing runtime-database-lib.sh" >&2
  exit 1
fi

ORACLE_CONTAINER_NAME_CONFIGURED="${ORACLE_CONTAINER_NAME:-}"
ORACLE_IMAGE="${ORACLE_IMAGE:-53661f3d548e}"
ORACLE_CONTAINER_NAME="${ORACLE_CONTAINER_NAME:-oracle-recovery-oracle19c}"
ORACLE_DEFAULT_CONTAINER_NAME="${ORACLE_DEFAULT_CONTAINER_NAME:-oracle-recovery-oracle19c}"
ORACLE_CONTAINER_PREFIXES="${ORACLE_CONTAINER_PREFIXES:-oracle-recovery-oracle,oracle,oracle19c}"
ORACLE_IMAGE_PREFIXES="${ORACLE_IMAGE_PREFIXES:-oracle,oracle19c,liujunel/oracle19c}"
ORACLE_SID="${ORACLE_SID:-ORCLCDB}"
ORACLE_PDB="${ORACLE_PDB:-ORCLPDB1}"
ORACLE_PWD="${ORACLE_PWD:-ChangeMe_Oracle19c_123}"
ORACLE_CHARACTERSET="${ORACLE_CHARACTERSET:-ZHS16GBK}"
ORACLE_HOST_PORT="${ORACLE_HOST_PORT:-1521}"
ORACLE_BASE_HOST_PATH="${ORACLE_BASE_HOST_PATH:-/data/oracle-recovery/oracle19c}"
ORACLE_ORADATA_HOST_PATH="${ORACLE_ORADATA_HOST_PATH:-$ORACLE_BASE_HOST_PATH/oradata}"
ORACLE_DMP_HOST_PATH="${ORACLE_DMP_HOST_PATH:-$ORACLE_BASE_HOST_PATH/dmp}"
ORACLE_TABLESPACE_HOST_PATH="${ORACLE_TABLESPACE_HOST_PATH:-$ORACLE_BASE_HOST_PATH/tablespaces}"
ORACLE_DMP_CONTAINER_PATH="${ORACLE_DMP_CONTAINER_PATH:-/opt/oracle/recovery_dmp}"
ORACLE_TABLESPACE_CONTAINER_PATH="${ORACLE_TABLESPACE_CONTAINER_PATH:-/opt/oracle/recovery_tablespaces}"
ORACLE_HOME_IN_CONTAINER="${ORACLE_HOME_IN_CONTAINER:-/opt/oracle/product/19c/dbhome_1}"
ORACLE_WAIT_SECONDS="${ORACLE_WAIT_SECONDS:-1800}"
ORACLE_RECREATE="${ORACLE_RECREATE:-false}"
ORACLE_DELETE_ORADATA="${ORACLE_DELETE_ORADATA:-false}"
ORACLE_TARGET_MODE="${ORACLE_TARGET_MODE:-auto}"
ORACLE_TARGET_HOST="${ORACLE_TARGET_HOST:-}"
APP_TZ="${TZ:-Asia/Shanghai}"
NO_IPTABLES_ACTIVE="${NO_IPTABLES_ACTIVE:-false}"

if is_local_target_disabled "$ORACLE_TARGET_MODE" || [ -n "$ORACLE_TARGET_HOST" ]; then
  echo "External Oracle target is configured; skipping local Oracle container management."
  echo "Oracle target: ${ORACLE_TARGET_HOST:-configured service}:${ORACLE_HOST_PORT}/${ORACLE_PDB}"
  record_runtime_env ORACLE_TARGET_HOST "$ORACLE_TARGET_HOST"
  record_runtime_env ORACLE_TARGET_MODE external
  exit 0
fi

ensure_recovery_network

ORACLE_CONTAINER_NAME="$(resolve_container_name \
  "Oracle" \
  "$ORACLE_CONTAINER_NAME_CONFIGURED" \
  "$ORACLE_DEFAULT_CONTAINER_NAME" \
  "$ORACLE_HOST_PORT" \
  "$ORACLE_CONTAINER_PREFIXES")"
record_runtime_env ORACLE_CONTAINER_NAME "$ORACLE_CONTAINER_NAME"

PORT_ARGS="-p $ORACLE_HOST_PORT:1521"
if [ "$NO_IPTABLES_ACTIVE" = "true" ]; then
  PORT_ARGS=""
fi

delete_oradata_if_requested() {
  if [ "$ORACLE_DELETE_ORADATA" != "true" ]; then
    return
  fi
  case "$ORACLE_ORADATA_HOST_PATH" in
    ""|"/"|"/data"|"/data/"|"$ORACLE_BASE_HOST_PATH"|"$ORACLE_BASE_HOST_PATH/")
      echo "Refusing to delete unsafe ORACLE_ORADATA_HOST_PATH: $ORACLE_ORADATA_HOST_PATH" >&2
      exit 1
      ;;
  esac
  if [ -d "$ORACLE_ORADATA_HOST_PATH" ]; then
    echo "Deleting old Oracle data directory: $ORACLE_ORADATA_HOST_PATH"
    rm -rf "$ORACLE_ORADATA_HOST_PATH"
  fi
}

verify_mount() {
  _host_path="$1"
  _container_path="$2"
  _label="$3"
  _line="$(docker inspect --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{println}}{{end}}' "$ORACLE_CONTAINER_NAME" \
    | awk -F '|' -v dst="$_container_path" '$2 == dst { print; exit }')"
  if [ -z "$_line" ]; then
    echo "Oracle container $ORACLE_CONTAINER_NAME is missing $_label mount: $_container_path" >&2
    echo "Expected host path: $_host_path" >&2
    echo "Docker volumes are fixed when the container is created. Recreate the Oracle container with start-oracle19c.sh if the mount is missing." >&2
    exit 1
  fi
  _actual_host="${_line%%|*}"
  if [ "$_actual_host" != "$_host_path" ]; then
    echo "Oracle container $ORACLE_CONTAINER_NAME has mismatched $_label mount." >&2
    echo "Expected: $_host_path -> $_container_path" >&2
    echo "Actual:   $_actual_host -> $_container_path" >&2
    echo "Update .env to match the existing container mount, or recreate the Oracle container with the expected mount." >&2
    exit 1
  fi
}

if [ "$ORACLE_RECREATE" = "true" ]; then
  if docker ps -a --format '{{.Names}}' | grep -Fx "$ORACLE_CONTAINER_NAME" >/dev/null 2>&1; then
    echo "Recreating Oracle container $ORACLE_CONTAINER_NAME..."
    docker stop "$ORACLE_CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm "$ORACLE_CONTAINER_NAME" >/dev/null
  fi
  delete_oradata_if_requested
fi

mkdir -p "$ORACLE_ORADATA_HOST_PATH" "$ORACLE_DMP_HOST_PATH" "$ORACLE_TABLESPACE_HOST_PATH"
chmod -R 777 "$ORACLE_BASE_HOST_PATH"

if docker ps -a --format '{{.Names}}' | grep -Fx "$ORACLE_CONTAINER_NAME" >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' | grep -Fx "$ORACLE_CONTAINER_NAME" >/dev/null 2>&1; then
    echo "Oracle container $ORACLE_CONTAINER_NAME is already running; keeping it."
  else
    echo "Starting existing Oracle container $ORACLE_CONTAINER_NAME..."
    docker start "$ORACLE_CONTAINER_NAME" >/dev/null
  fi
  docker network connect oracle-recovery-net "$ORACLE_CONTAINER_NAME" >/dev/null 2>&1 || true
else
  ORACLE_IMAGE="$(resolve_image_name "Oracle" "$ORACLE_IMAGE" "$ORACLE_IMAGE_PREFIXES")"
  echo "Creating Oracle19c container $ORACLE_CONTAINER_NAME from image $ORACLE_IMAGE..."
  docker run -d \
    --name "$ORACLE_CONTAINER_NAME" \
    --restart unless-stopped \
    --privileged \
    --security-opt seccomp=unconfined \
    --pids-limit -1 \
    --ulimit nproc=65535:65535 \
    --network oracle-recovery-net \
    --network-alias "$ORACLE_CONTAINER_NAME" \
    $PORT_ARGS \
    -e TZ="$APP_TZ" \
    -e ORACLE_SID="$ORACLE_SID" \
    -e ORACLE_PDB="$ORACLE_PDB" \
    -e ORACLE_PWD="$ORACLE_PWD" \
    -e ORACLE_CHARACTERSET="$ORACLE_CHARACTERSET" \
    -v "$ORACLE_ORADATA_HOST_PATH:/opt/oracle/oradata" \
    -v "$ORACLE_DMP_HOST_PATH:$ORACLE_DMP_CONTAINER_PATH" \
    -v "$ORACLE_TABLESPACE_HOST_PATH:$ORACLE_TABLESPACE_CONTAINER_PATH" \
    "$ORACLE_IMAGE" >/dev/null
fi

verify_mount "$ORACLE_DMP_HOST_PATH" "$ORACLE_DMP_CONTAINER_PATH" "DMP"
verify_mount "$ORACLE_TABLESPACE_HOST_PATH" "$ORACLE_TABLESPACE_CONTAINER_PATH" "tablespace"

echo "Waiting for Oracle PDB $ORACLE_PDB in $ORACLE_CONTAINER_NAME..."
elapsed=0
while [ "$elapsed" -lt "$ORACLE_WAIT_SECONDS" ]; do
  if docker exec \
    -e ORACLE_PWD="$ORACLE_PWD" \
    -e ORACLE_PDB="$ORACLE_PDB" \
    -e ORACLE_HOME_IN_CONTAINER="$ORACLE_HOME_IN_CONTAINER" \
    "$ORACLE_CONTAINER_NAME" \
    bash -lc 'export ORACLE_HOME="$ORACLE_HOME_IN_CONTAINER"; export PATH="$ORACLE_HOME/bin:$PATH"; echo "select 1 from dual;" | sqlplus -L -s "system/$ORACLE_PWD@//127.0.0.1:1521/$ORACLE_PDB"' >/dev/null 2>&1; then
    docker exec "$ORACLE_CONTAINER_NAME" bash -lc "mkdir -p '$ORACLE_DMP_CONTAINER_PATH' '$ORACLE_TABLESPACE_CONTAINER_PATH' && chmod -R 777 '$ORACLE_DMP_CONTAINER_PATH' '$ORACLE_TABLESPACE_CONTAINER_PATH'" >/dev/null 2>&1 || true
    echo "Oracle is ready: $ORACLE_CONTAINER_NAME / $ORACLE_PDB / charset=$ORACLE_CHARACTERSET"
    exit 0
  fi
  sleep 10
  elapsed=$((elapsed + 10))
done

echo "Oracle did not become ready within ${ORACLE_WAIT_SECONDS}s." >&2
docker logs "$ORACLE_CONTAINER_NAME" --tail 120 >&2 || true
exit 1
