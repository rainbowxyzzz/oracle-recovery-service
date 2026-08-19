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

SQLSERVER_CONTAINER_NAME_CONFIGURED="${SQLSERVER_CONTAINER_NAME:-}"
SQLSERVER_IMAGE="${SQLSERVER_IMAGE:-f191949a09a6}"
SQLSERVER_CONTAINER_NAME="${SQLSERVER_CONTAINER_NAME:-sqlserver-recovery-mssql}"
SQLSERVER_DEFAULT_CONTAINER_NAME="${SQLSERVER_DEFAULT_CONTAINER_NAME:-sqlserver-recovery-mssql}"
SQLSERVER_CONTAINER_PREFIXES="${SQLSERVER_CONTAINER_PREFIXES:-sqlserver-recovery,sqlserver,mssql}"
SQLSERVER_IMAGE_PREFIXES="${SQLSERVER_IMAGE_PREFIXES:-mcr.microsoft.com/mssql/server,sqlserver,mssql}"
SQLSERVER_SA_PASSWORD="${SQLSERVER_SA_PASSWORD:-ChangeMe_SqlServer_123!}"
SQLSERVER_HOST_PORT="${SQLSERVER_HOST_PORT:-1433}"
SQLSERVER_BASE_HOST_PATH="${SQLSERVER_BASE_HOST_PATH:-/data/sqlserver-recovery}"
SQLSERVER_FILE_HOST_PATH="${SQLSERVER_FILE_HOST_PATH:-$SQLSERVER_BASE_HOST_PATH/files}"
SQLSERVER_DATA_HOST_PATH="${SQLSERVER_DATA_HOST_PATH:-$SQLSERVER_BASE_HOST_PATH/data}"
SQLSERVER_FILE_CONTAINER_PATH="${SQLSERVER_FILE_CONTAINER_PATH:-/var/opt/mssql/recovery_files}"
SQLSERVER_DATA_CONTAINER_PATH="${SQLSERVER_DATA_CONTAINER_PATH:-/var/opt/mssql/data}"
SQLSERVER_WAIT_SECONDS="${SQLSERVER_WAIT_SECONDS:-600}"
SQLSERVER_TARGET_MODE="${SQLSERVER_TARGET_MODE:-auto}"
SQLSERVER_TARGET_HOST="${SQLSERVER_TARGET_HOST:-}"
APP_TZ="${TZ:-Asia/Shanghai}"
NO_IPTABLES_ACTIVE="${NO_IPTABLES_ACTIVE:-false}"

if is_local_target_disabled "$SQLSERVER_TARGET_MODE" || [ -n "$SQLSERVER_TARGET_HOST" ]; then
  echo "External SQL Server target is configured; skipping local SQL Server container management."
  echo "SQL Server target: ${SQLSERVER_TARGET_HOST:-configured service}:${SQLSERVER_HOST_PORT}"
  record_runtime_env SQLSERVER_TARGET_HOST "$SQLSERVER_TARGET_HOST"
  record_runtime_env SQLSERVER_TARGET_MODE external
  exit 0
fi

ensure_recovery_network

SQLSERVER_CONTAINER_NAME="$(resolve_container_name \
  "SQL Server" \
  "$SQLSERVER_CONTAINER_NAME_CONFIGURED" \
  "$SQLSERVER_DEFAULT_CONTAINER_NAME" \
  "$SQLSERVER_HOST_PORT" \
  "$SQLSERVER_CONTAINER_PREFIXES")"
record_runtime_env SQLSERVER_CONTAINER_NAME "$SQLSERVER_CONTAINER_NAME"

mkdir -p "$SQLSERVER_FILE_HOST_PATH" "$SQLSERVER_DATA_HOST_PATH"
chmod -R 777 "$SQLSERVER_BASE_HOST_PATH"

PORT_ARGS="-p $SQLSERVER_HOST_PORT:1433"
if [ "$NO_IPTABLES_ACTIVE" = "true" ]; then
  PORT_ARGS=""
fi

verify_mount() {
  _host_path="$1"
  _container_path="$2"
  _label="$3"
  _line="$(docker inspect --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{println}}{{end}}' "$SQLSERVER_CONTAINER_NAME" \
    | awk -F '|' -v dst="$_container_path" '$2 == dst { print; exit }')"
  if [ -z "$_line" ]; then
    echo "SQL Server container $SQLSERVER_CONTAINER_NAME is missing $_label mount: $_container_path" >&2
    echo "Expected host path: $_host_path" >&2
    exit 1
  fi
  _actual_host="${_line%%|*}"
  if [ "$_actual_host" != "$_host_path" ]; then
    echo "SQL Server container $SQLSERVER_CONTAINER_NAME has mismatched $_label mount." >&2
    echo "Expected: $_host_path -> $_container_path" >&2
    echo "Actual:   $_actual_host -> $_container_path" >&2
    exit 1
  fi
}

if docker ps -a --format '{{.Names}}' | grep -Fx "$SQLSERVER_CONTAINER_NAME" >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' | grep -Fx "$SQLSERVER_CONTAINER_NAME" >/dev/null 2>&1; then
    echo "SQL Server container $SQLSERVER_CONTAINER_NAME is already running; keeping it."
  else
    echo "Starting existing SQL Server container $SQLSERVER_CONTAINER_NAME..."
    docker start "$SQLSERVER_CONTAINER_NAME" >/dev/null
  fi
  docker network connect oracle-recovery-net "$SQLSERVER_CONTAINER_NAME" >/dev/null 2>&1 || true
else
  SQLSERVER_IMAGE="$(resolve_image_name "SQL Server" "$SQLSERVER_IMAGE" "$SQLSERVER_IMAGE_PREFIXES")"
  echo "Creating SQL Server container $SQLSERVER_CONTAINER_NAME from image $SQLSERVER_IMAGE..."
  docker run -d \
    --name "$SQLSERVER_CONTAINER_NAME" \
    --restart unless-stopped \
    --privileged \
    --security-opt seccomp=unconfined \
    --pids-limit -1 \
    --ulimit nproc=65535:65535 \
    --network oracle-recovery-net \
    --network-alias "$SQLSERVER_CONTAINER_NAME" \
    $PORT_ARGS \
    -e TZ="$APP_TZ" \
    -e ACCEPT_EULA=Y \
    -e MSSQL_SA_PASSWORD="$SQLSERVER_SA_PASSWORD" \
    -v "$SQLSERVER_FILE_HOST_PATH:$SQLSERVER_FILE_CONTAINER_PATH" \
    -v "$SQLSERVER_DATA_HOST_PATH:$SQLSERVER_DATA_CONTAINER_PATH" \
    "$SQLSERVER_IMAGE" >/dev/null
fi

verify_mount "$SQLSERVER_FILE_HOST_PATH" "$SQLSERVER_FILE_CONTAINER_PATH" "backup/data file"
verify_mount "$SQLSERVER_DATA_HOST_PATH" "$SQLSERVER_DATA_CONTAINER_PATH" "database data"

echo "Waiting for SQL Server in $SQLSERVER_CONTAINER_NAME..."
elapsed=0
while [ "$elapsed" -lt "$SQLSERVER_WAIT_SECONDS" ]; do
  if docker exec \
    -e SQLSERVER_SA_PASSWORD="$SQLSERVER_SA_PASSWORD" \
    "$SQLSERVER_CONTAINER_NAME" \
    bash -lc 'SQLCMD=$(command -v sqlcmd || command -v /opt/mssql-tools18/bin/sqlcmd || command -v /opt/mssql-tools/bin/sqlcmd); [ -n "$SQLCMD" ] && "$SQLCMD" -S 127.0.0.1 -U SA -P "$SQLSERVER_SA_PASSWORD" -C -Q "select 1" >/dev/null' >/dev/null 2>&1; then
    docker exec "$SQLSERVER_CONTAINER_NAME" bash -lc "mkdir -p '$SQLSERVER_FILE_CONTAINER_PATH' '$SQLSERVER_DATA_CONTAINER_PATH' && chmod -R 777 '$SQLSERVER_FILE_CONTAINER_PATH' '$SQLSERVER_DATA_CONTAINER_PATH'" >/dev/null 2>&1 || true
    echo "SQL Server is ready: $SQLSERVER_CONTAINER_NAME"
    exit 0
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

echo "SQL Server did not become ready within ${SQLSERVER_WAIT_SECONDS}s." >&2
docker logs "$SQLSERVER_CONTAINER_NAME" --tail 120 >&2 || true
exit 1
