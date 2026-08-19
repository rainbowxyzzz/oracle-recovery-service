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

MYSQL_RESTORE_CONTAINER_NAME_CONFIGURED="${MYSQL_RESTORE_CONTAINER_NAME:-}"
MYSQL_RESTORE_IMAGE="${MYSQL_RESTORE_IMAGE:-mysql:8.4}"
MYSQL_RESTORE_CONTAINER_NAME="${MYSQL_RESTORE_CONTAINER_NAME:-mysql-recovery-target}"
MYSQL_RESTORE_DEFAULT_CONTAINER_NAME="${MYSQL_RESTORE_DEFAULT_CONTAINER_NAME:-mysql-recovery-target}"
MYSQL_RESTORE_CONTAINER_PREFIXES="${MYSQL_RESTORE_CONTAINER_PREFIXES:-mysql-recovery,mysql}"
MYSQL_RESTORE_IMAGE_PREFIXES="${MYSQL_RESTORE_IMAGE_PREFIXES:-mysql}"
MYSQL_RESTORE_ROOT_PASSWORD="${MYSQL_RESTORE_ROOT_PASSWORD:-ChangeMe_MySqlRestore_123!}"
MYSQL_RESTORE_HOST_PORT="${MYSQL_RESTORE_HOST_PORT:-3307}"
MYSQL_RESTORE_BASE_HOST_PATH="${MYSQL_RESTORE_BASE_HOST_PATH:-/data/mysql-recovery}"
MYSQL_RESTORE_BACKUP_HOST_PATH="${MYSQL_RESTORE_BACKUP_HOST_PATH:-$MYSQL_RESTORE_BASE_HOST_PATH/backup}"
MYSQL_RESTORE_DATA_HOST_PATH="${MYSQL_RESTORE_DATA_HOST_PATH:-$MYSQL_RESTORE_BASE_HOST_PATH/data}"
MYSQL_RESTORE_BACKUP_CONTAINER_PATH="${MYSQL_RESTORE_BACKUP_CONTAINER_PATH:-/recovery_backup}"
MYSQL_RESTORE_WAIT_SECONDS="${MYSQL_RESTORE_WAIT_SECONDS:-600}"
MYSQL_RESTORE_TARGET_MODE="${MYSQL_RESTORE_TARGET_MODE:-auto}"
MYSQL_RESTORE_TARGET_HOST="${MYSQL_RESTORE_TARGET_HOST:-}"
APP_TZ="${TZ:-Asia/Shanghai}"
MYSQL_SESSION_TIME_ZONE="${MYSQL_SESSION_TIME_ZONE:-+08:00}"
NO_IPTABLES_ACTIVE="${NO_IPTABLES_ACTIVE:-false}"

if is_local_target_disabled "$MYSQL_RESTORE_TARGET_MODE" || [ -n "$MYSQL_RESTORE_TARGET_HOST" ]; then
  echo "External MySQL restore target is configured; skipping local MySQL restore container management."
  echo "MySQL restore target: ${MYSQL_RESTORE_TARGET_HOST:-configured service}:${MYSQL_RESTORE_HOST_PORT}"
  record_runtime_env MYSQL_RESTORE_TARGET_HOST "$MYSQL_RESTORE_TARGET_HOST"
  record_runtime_env MYSQL_RESTORE_TARGET_MODE external
  exit 0
fi

ensure_recovery_network

MYSQL_RESTORE_CONTAINER_NAME="$(resolve_container_name \
  "MySQL restore" \
  "$MYSQL_RESTORE_CONTAINER_NAME_CONFIGURED" \
  "$MYSQL_RESTORE_DEFAULT_CONTAINER_NAME" \
  "$MYSQL_RESTORE_HOST_PORT" \
  "$MYSQL_RESTORE_CONTAINER_PREFIXES")"
record_runtime_env MYSQL_RESTORE_CONTAINER_NAME "$MYSQL_RESTORE_CONTAINER_NAME"

mkdir -p "$MYSQL_RESTORE_BACKUP_HOST_PATH" "$MYSQL_RESTORE_DATA_HOST_PATH"
chmod -R 777 "$MYSQL_RESTORE_BASE_HOST_PATH"

PORT_ARGS="-p $MYSQL_RESTORE_HOST_PORT:3306"
if [ "$NO_IPTABLES_ACTIVE" = "true" ]; then
  PORT_ARGS=""
fi

verify_mount() {
  _host_path="$1"
  _container_path="$2"
  _label="$3"
  _line="$(docker inspect --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{println}}{{end}}' "$MYSQL_RESTORE_CONTAINER_NAME" \
    | awk -F '|' -v dst="$_container_path" '$2 == dst { print; exit }')"
  if [ -z "$_line" ]; then
    echo "MySQL restore container $MYSQL_RESTORE_CONTAINER_NAME is missing $_label mount: $_container_path" >&2
    echo "Expected host path: $_host_path" >&2
    exit 1
  fi
  _actual_host="${_line%%|*}"
  if [ "$_actual_host" != "$_host_path" ]; then
    echo "MySQL restore container $MYSQL_RESTORE_CONTAINER_NAME has mismatched $_label mount." >&2
    echo "Expected: $_host_path -> $_container_path" >&2
    echo "Actual:   $_actual_host -> $_container_path" >&2
    exit 1
  fi
}

create_container() {
  echo "Creating MySQL restore container $MYSQL_RESTORE_CONTAINER_NAME from image $MYSQL_RESTORE_IMAGE..."
  docker run -d \
    --name "$MYSQL_RESTORE_CONTAINER_NAME" \
    --restart unless-stopped \
    --privileged \
    --security-opt seccomp=unconfined \
    --pids-limit -1 \
    --ulimit nproc=65535:65535 \
    --network oracle-recovery-net \
    --network-alias "$MYSQL_RESTORE_CONTAINER_NAME" \
    $PORT_ARGS \
    -e TZ="$APP_TZ" \
    -e MYSQL_ROOT_PASSWORD="$MYSQL_RESTORE_ROOT_PASSWORD" \
    -v "$MYSQL_RESTORE_DATA_HOST_PATH:/var/lib/mysql" \
    -v "$MYSQL_RESTORE_BACKUP_HOST_PATH:$MYSQL_RESTORE_BACKUP_CONTAINER_PATH" \
    "$MYSQL_RESTORE_IMAGE" \
    --default-time-zone="$MYSQL_SESSION_TIME_ZONE" >/dev/null
}

if docker ps -a --format '{{.Names}}' | grep -Fx "$MYSQL_RESTORE_CONTAINER_NAME" >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' | grep -Fx "$MYSQL_RESTORE_CONTAINER_NAME" >/dev/null 2>&1; then
    echo "MySQL restore container $MYSQL_RESTORE_CONTAINER_NAME is already running; keeping it."
  else
    echo "Starting existing MySQL restore container $MYSQL_RESTORE_CONTAINER_NAME..."
    if ! docker start "$MYSQL_RESTORE_CONTAINER_NAME" >/dev/null; then
      if [ "$NO_IPTABLES_ACTIVE" = "true" ]; then
        echo "Existing MySQL restore container cannot start without iptables port publishing; recreating it without -p."
        docker rm "$MYSQL_RESTORE_CONTAINER_NAME" >/dev/null
        create_container
      else
        exit 1
      fi
    fi
  fi
  docker network connect oracle-recovery-net "$MYSQL_RESTORE_CONTAINER_NAME" >/dev/null 2>&1 || true
else
  MYSQL_RESTORE_IMAGE="$(resolve_image_name "MySQL" "$MYSQL_RESTORE_IMAGE" "$MYSQL_RESTORE_IMAGE_PREFIXES")"
  create_container
fi

verify_mount "$MYSQL_RESTORE_BACKUP_HOST_PATH" "$MYSQL_RESTORE_BACKUP_CONTAINER_PATH" "backup"
verify_mount "$MYSQL_RESTORE_DATA_HOST_PATH" "/var/lib/mysql" "data"

echo "Waiting for MySQL restore target in $MYSQL_RESTORE_CONTAINER_NAME..."
elapsed=0
while [ "$elapsed" -lt "$MYSQL_RESTORE_WAIT_SECONDS" ]; do
  if docker exec "$MYSQL_RESTORE_CONTAINER_NAME" mysqladmin ping -h 127.0.0.1 -uroot -p"$MYSQL_RESTORE_ROOT_PASSWORD" --silent >/dev/null 2>&1; then
    docker exec "$MYSQL_RESTORE_CONTAINER_NAME" bash -lc "mkdir -p '$MYSQL_RESTORE_BACKUP_CONTAINER_PATH' && chmod -R 777 '$MYSQL_RESTORE_BACKUP_CONTAINER_PATH'" >/dev/null 2>&1 || true
    echo "MySQL restore target is ready: $MYSQL_RESTORE_CONTAINER_NAME"
    exit 0
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

echo "MySQL restore target did not become ready within ${MYSQL_RESTORE_WAIT_SECONDS}s." >&2
docker logs "$MYSQL_RESTORE_CONTAINER_NAME" --tail 120 >&2 || true
exit 1
