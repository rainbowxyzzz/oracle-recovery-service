#!/bin/sh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${ORACLE21C_ENV_FILE:-$SCRIPT_DIR/oracle21c.env}

if [ -f "$ENV_FILE" ]; then
  . "$ENV_FILE"
fi

IMAGE=${ORACLE21C_IMAGE:-softwareplant/oracle:clean-21.3.0-ee}
IMAGE_TAR=${ORACLE21C_IMAGE_TAR:-}
CONTAINER=${ORACLE21C_CONTAINER:-oracle-recovery-oracle21c-ee}
HOSTNAME=${ORACLE21C_HOSTNAME:-oracle21c-ee}
NETWORK=${ORACLE21C_NETWORK:-oracle-recovery-net}
LISTENER_PORT=${ORACLE21C_LISTENER_PORT:-1522}
EM_PORT=${ORACLE21C_EM_PORT:-5501}
ORACLE_SID=${ORACLE21C_SID:-ORCL21C}
ORACLE_PDB=${ORACLE21C_PDB:-ORCLPDB1}
ORACLE_PASSWORD=${ORACLE21C_PASSWORD:-}
ORACLE_CHARACTERSET=${ORACLE21C_CHARACTERSET:-ZHS16GBK}
ENABLE_ARCHIVELOG=${ORACLE21C_ENABLE_ARCHIVELOG:-false}
DATA_ROOT=${ORACLE21C_DATA_ROOT:-/data/oracle-recovery/oracle21c}
ORADATA_HOST_PATH=${ORACLE21C_ORADATA_HOST_PATH:-$DATA_ROOT/oradata}
DMP_HOST_PATH=${ORACLE21C_DMP_HOST_PATH:-$DATA_ROOT/dmp}
TABLESPACE_HOST_PATH=${ORACLE21C_TABLESPACE_HOST_PATH:-$DATA_ROOT/tablespaces}
MEMORY_LIMIT=${ORACLE21C_MEMORY_LIMIT:-}
SHM_SIZE=${ORACLE21C_SHM_SIZE:-1g}
INIT_SGA_SIZE=${ORACLE21C_INIT_SGA_SIZE:-}
INIT_PGA_SIZE=${ORACLE21C_INIT_PGA_SIZE:-}
INIT_CPU_COUNT=${ORACLE21C_INIT_CPU_COUNT:-}
INIT_PROCESSES=${ORACLE21C_INIT_PROCESSES:-}

log() {
  echo "[oracle21c-deploy] $*"
}

fail() {
  echo "[oracle21c-deploy] ERROR: $*" >&2
  exit 1
}

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fx "$CONTAINER" >/dev/null 2>&1
}

container_running() {
  docker ps --format '{{.Names}}' | grep -Fx "$CONTAINER" >/dev/null 2>&1
}

image_exists() {
  docker image inspect "$IMAGE" >/dev/null 2>&1
}

mount_source() {
  destination=$1
  docker inspect "$CONTAINER" --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{println}}{{end}}' | \
    awk -F '|' -v dst="$destination" '$2 == dst { print $1; exit }'
}

validate_identifier() {
  value=$1
  label=$2
  case "$value" in
    ''|*[!A-Za-z0-9_\#\$]*) fail "$label must be a simple Oracle identifier" ;;
  esac
}

validate_path() {
  value=$1
  label=$2
  case "$value" in
    /data/oracle-recovery/*) ;;
    *) fail "$label must stay below /data/oracle-recovery" ;;
  esac
}

resolve_image_tar() {
  case "$IMAGE_TAR" in
    '') return ;;
    /*) ;;
    *) IMAGE_TAR=$(cd "$(dirname "$ENV_FILE")" && pwd)/$IMAGE_TAR ;;
  esac
}

ensure_image_available() {
  if image_exists; then
    log "image is available: $IMAGE"
    return
  fi

  resolve_image_tar
  if [ -n "$IMAGE_TAR" ] && [ -f "$IMAGE_TAR" ]; then
    log "loading image archive: $IMAGE_TAR"
    docker load -i "$IMAGE_TAR"
  fi

  image_exists || fail "image not found: $IMAGE; preload it or set ORACLE21C_IMAGE_TAR"
}

prepare_directories() {
  mkdir -p "$ORADATA_HOST_PATH" "$DMP_HOST_PATH" "$TABLESPACE_HOST_PATH"
  chmod 0777 "$ORADATA_HOST_PATH" "$DMP_HOST_PATH" "$TABLESPACE_HOST_PATH"
}

validate_existing_mounts() {
  actual_oradata=$(mount_source /opt/oracle/oradata)
  actual_dmp=$(mount_source /opt/oracle/recovery_dmp)
  actual_tablespaces=$(mount_source /opt/oracle/recovery_tablespaces)

  [ -n "$actual_oradata" ] || fail "existing container is missing /opt/oracle/oradata mount"
  [ -n "$actual_dmp" ] || fail "existing container is missing /opt/oracle/recovery_dmp mount"
  [ -n "$actual_tablespaces" ] || fail "existing container is missing /opt/oracle/recovery_tablespaces mount"

  if [ -n "${ORACLE21C_ORADATA_HOST_PATH:-}" ] && [ "$actual_oradata" != "$ORADATA_HOST_PATH" ]; then
    fail "existing oradata mount is $actual_oradata, configured value is $ORADATA_HOST_PATH"
  fi
  if [ -n "${ORACLE21C_DMP_HOST_PATH:-}" ] && [ "$actual_dmp" != "$DMP_HOST_PATH" ]; then
    fail "existing DMP mount is $actual_dmp, configured value is $DMP_HOST_PATH"
  fi
  if [ -n "${ORACLE21C_TABLESPACE_HOST_PATH:-}" ] && [ "$actual_tablespaces" != "$TABLESPACE_HOST_PATH" ]; then
    fail "existing tablespace mount is $actual_tablespaces, configured value is $TABLESPACE_HOST_PATH"
  fi

  log "existing mounts: oradata=$actual_oradata, dmp=$actual_dmp, tablespaces=$actual_tablespaces"
}

create_container() {
  ensure_image_available
  prepare_directories

  set -- docker run -d \
    --name "$CONTAINER" \
    --hostname "$HOSTNAME" \
    --restart unless-stopped \
    --privileged \
    --security-opt seccomp=unconfined \
    --pids-limit -1 \
    --ulimit nproc=65535:65535 \
    --network "$NETWORK" \
    --network-alias "$CONTAINER" \
    --shm-size "$SHM_SIZE" \
    -p "$LISTENER_PORT:1521" \
    -p "$EM_PORT:5500" \
    -e "ORACLE_SID=$ORACLE_SID" \
    -e "ORACLE_PDB=$ORACLE_PDB" \
    -e "ORACLE_PWD=$ORACLE_PASSWORD" \
    -e "ORACLE_CHARACTERSET=$ORACLE_CHARACTERSET" \
    -e "ENABLE_ARCHIVELOG=$ENABLE_ARCHIVELOG" \
    -v "$ORADATA_HOST_PATH:/opt/oracle/oradata" \
    -v "$DMP_HOST_PATH:/opt/oracle/recovery_dmp" \
    -v "$TABLESPACE_HOST_PATH:/opt/oracle/recovery_tablespaces"

  if [ -n "$MEMORY_LIMIT" ]; then set -- "$@" --memory "$MEMORY_LIMIT"; fi
  if [ -n "$INIT_SGA_SIZE" ]; then set -- "$@" -e "INIT_SGA_SIZE=$INIT_SGA_SIZE"; fi
  if [ -n "$INIT_PGA_SIZE" ]; then set -- "$@" -e "INIT_PGA_SIZE=$INIT_PGA_SIZE"; fi
  if [ -n "$INIT_CPU_COUNT" ]; then set -- "$@" -e "INIT_CPU_COUNT=$INIT_CPU_COUNT"; fi
  if [ -n "$INIT_PROCESSES" ]; then set -- "$@" -e "INIT_PROCESSES=$INIT_PROCESSES"; fi

  set -- "$@" "$IMAGE"
  log "creating container $CONTAINER from $IMAGE"
  "$@" >/dev/null
}

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker info >/dev/null 2>&1 || fail "docker daemon is unavailable"
validate_identifier "$ORACLE_SID" ORACLE21C_SID
validate_identifier "$ORACLE_PDB" ORACLE21C_PDB
[ -n "$ORACLE_PASSWORD" ] || fail "ORACLE21C_PASSWORD is required"
validate_path "$ORADATA_HOST_PATH" ORACLE21C_ORADATA_HOST_PATH
validate_path "$DMP_HOST_PATH" ORACLE21C_DMP_HOST_PATH
validate_path "$TABLESPACE_HOST_PATH" ORACLE21C_TABLESPACE_HOST_PATH

docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

if container_exists; then
  validate_existing_mounts
  if container_running; then
    log "container is already running: $CONTAINER"
  else
    log "starting existing container: $CONTAINER"
    docker start "$CONTAINER" >/dev/null
  fi
  docker network connect "$NETWORK" "$CONTAINER" >/dev/null 2>&1 || true
else
  create_container
fi

log "container lifecycle check completed"
docker ps --filter "name=$CONTAINER" --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}'
