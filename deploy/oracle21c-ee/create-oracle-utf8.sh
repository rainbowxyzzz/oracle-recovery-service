#!/bin/sh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
RESET=false
if [ "${1:-}" = --reset ]; then RESET=true; shift; fi
[ "$#" -le 1 ] || { echo 'usage: sh create-oracle-utf8.sh [--reset] [config-file]' >&2; exit 1; }
CONFIG=${1:-$SCRIPT_DIR/oracle-utf8.env}
case "$CONFIG" in /*) ;; *) CONFIG=$(pwd)/$CONFIG ;; esac
fail() { echo "[oracle-utf8] ERROR: $*" >&2; exit 1; }
[ -f "$CONFIG" ] || fail "copy oracle-utf8.env.example to oracle-utf8.env and set ORACLE21C_PASSWORD first"
set -a
. "$CONFIG"
set +a

# This entry owns its configuration; child scripts must not reload the business .env.
export ORACLE21C_ENV_FILE=/dev/null
export ORACLE21C_MODE=container
[ "${ORACLE21C_CHARACTERSET:-}" = AL32UTF8 ] || fail "ORACLE21C_CHARACTERSET must be AL32UTF8"
[ "${ORACLE21C_NCHAR_CHARACTERSET:-}" = AL16UTF16 ] || fail "ORACLE21C_NCHAR_CHARACTERSET must be AL16UTF16"
[ -n "${ORACLE21C_PASSWORD:-}" ] || fail "ORACLE21C_PASSWORD must be set in $CONFIG"
for name in ORACLE21C_CONTAINER ORACLE21C_SID ORACLE21C_PDB ORACLE21C_ORADATA_HOST_PATH ORACLE21C_TABLESPACE_HOST_PATH ORACLE21C_DMP_HOST_PATH
do
  value=$(printenv "$name") || fail "$name is required"
  [ -n "$value" ] || fail "$name is required"
done
case "${ORACLE21C_INIT_SGA_SIZE:-}" in ''|*[!0-9]*) fail "INIT_SGA_SIZE must be positive integer MB" ;; esac
case "${ORACLE21C_INIT_PGA_SIZE:-}" in ''|*[!0-9]*) fail "INIT_PGA_SIZE must be positive integer MB" ;; esac
[ "$ORACLE21C_INIT_SGA_SIZE" -gt 0 ] && [ "$ORACLE21C_INIT_PGA_SIZE" -gt 0 ] || fail "SGA and PGA must be positive"
case "${ORACLE21C_IMAGE_TAR:-}" in
  ''|/*) ;;
  *) export ORACLE21C_IMAGE_TAR=$(dirname "$CONFIG")/$ORACLE21C_IMAGE_TAR ;;
esac

if [ "$RESET" = true ]; then
  [ ! -L "$ORACLE21C_ORADATA_HOST_PATH" ] && [ ! -L "$ORACLE21C_TABLESPACE_HOST_PATH" ] || fail "cannot archive a symbolic link"
fi
data_path=$(readlink -m "$ORACLE21C_ORADATA_HOST_PATH")
tablespace_path=$(readlink -m "$ORACLE21C_TABLESPACE_HOST_PATH")
dmp_path=$(readlink -m "$ORACLE21C_DMP_HOST_PATH")
for path in "$data_path" "$tablespace_path" "$dmp_path"
do
  case "$path" in /data/oracle-recovery/*) ;; *) fail "resolved path must stay below /data/oracle-recovery: $path" ;; esac
done
case "$data_path/" in "$tablespace_path/"*|"$dmp_path/"*) fail "data, tablespace and DMP paths must not overlap" ;; esac
case "$tablespace_path/" in "$data_path/"*|"$dmp_path/"*) fail "data, tablespace and DMP paths must not overlap" ;; esac
case "$dmp_path/" in "$data_path/"*|"$tablespace_path/"*) fail "data, tablespace and DMP paths must not overlap" ;; esac
# These are reserved primary database locations, even if its container is absent.
for path in "$data_path" "$tablespace_path"
do
  for primary in /data/oracle-recovery/oracle21c /data/oracle-recovery/oracle19c
  do
    primary=$(readlink -m "$primary")
    case "$path/" in "$primary/"*) fail "refusing primary Oracle directory: $path" ;; esac
    case "$primary/" in "$path/"*) fail "refusing ancestor of primary Oracle directory: $path" ;; esac
  done
done
export ORACLE21C_ORADATA_HOST_PATH=$data_path
export ORACLE21C_TABLESPACE_HOST_PATH=$tablespace_path
export ORACLE21C_DMP_HOST_PATH=$dmp_path

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker info >/dev/null 2>&1 || fail "docker daemon is unavailable"
containers=$(docker ps -a --format '{{.Names}}') || fail "cannot list containers"
# Compare all mounts, including stopped containers and bind mount symlinks.
for other in $containers
do
  [ "$other" != "$ORACLE21C_CONTAINER" ] || continue
  mounts=$(docker inspect "$other" --format '{{range .Mounts}}{{println .Source}}{{end}}') || fail "cannot inspect mounts: $other"
  while IFS= read -r mount
  do
    [ -n "$mount" ] || continue
    mount=$(readlink -m "$mount")
    for path in "$data_path" "$tablespace_path"
    do
      case "$path/" in "$mount/"*) fail "data path overlaps container $other mount $mount" ;; esac
      case "$mount/" in "$path/"*) fail "data path overlaps container $other mount $mount" ;; esac
    done
  done <<MOUNTS
$mounts
MOUNTS
done

exists=false
if printf '%s\n' "$containers" | grep -Fx "$ORACLE21C_CONTAINER" >/dev/null; then
  exists=true
  existing_env=$(docker inspect "$ORACLE21C_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}') || fail "cannot inspect existing container"
  for setting in "ORACLE_SID=$ORACLE21C_SID" "ORACLE_PDB=$ORACLE21C_PDB" "ORACLE_CHARACTERSET=AL32UTF8"
  do
    printf '%s\n' "$existing_env" | grep -Fx "$setting" >/dev/null || \
      fail "existing container differs: expected $setting. Editing env does not update a container or SPFILE. Preserve existing data; use a new container name, ports and empty data directories for a fresh database."
  done
  actual_mounts=$(docker inspect "$ORACLE21C_CONTAINER" --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{println}}{{end}}') || fail "cannot inspect target mounts"
  for entry in "$data_path|/opt/oracle/oradata" "$tablespace_path|/opt/oracle/recovery_tablespaces" "$dmp_path|/opt/oracle/recovery_dmp"
  do
    printf '%s\n' "$actual_mounts" | grep -Fx "$entry" >/dev/null || fail "target mount differs: $entry; use the actual mount paths in configuration"
  done
  if [ "$RESET" = false ]; then
    for setting in "INIT_SGA_SIZE=$ORACLE21C_INIT_SGA_SIZE" "INIT_PGA_SIZE=$ORACLE21C_INIT_PGA_SIZE"
    do
      printf '%s\n' "$existing_env" | grep -Fx "$setting" >/dev/null || fail "existing container differs: expected $setting; use --reset to archive the old UTF database and create a NEW empty one, or keep it for manual repair"
    done
  fi
else
  for path in "$data_path" "$tablespace_path"
  do
    [ ! -e "$path" ] || [ -d "$path" ] || fail "not a directory: $path"
    if [ -d "$path" ]; then
      entries=$(find "$path" -mindepth 1 -maxdepth 1 -print -quit) || fail "cannot inspect $path"
      [ -z "$entries" ] || [ "$RESET" = true ] || fail "directory is not empty: $path; use --reset to archive it before creating a new database"
    fi
  done
fi

running=$(docker inspect --format '{{.State.Running}}' "$ORACLE21C_CONTAINER" 2>/dev/null || true)
if [ "$running" != true ] || [ "$RESET" = true ]; then
  available_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  [ -n "$available_kb" ] || fail "cannot read MemAvailable from /proc/meminfo"
  required_mb=$((ORACLE21C_INIT_SGA_SIZE + ORACLE21C_INIT_PGA_SIZE + 2048))
  [ "$available_kb" -ge "$((required_mb * 1024))" ] || fail "need at least ${required_mb}MB available RAM before startup; currently $((available_kb / 1024))MB"
fi

if [ "$RESET" = true ]; then
  suffix=backup-$(date -u +%Y%m%dT%H%M%SZ)-$$
  docker image inspect "$ORACLE21C_IMAGE" >/dev/null 2>&1 || fail "preload $ORACLE21C_IMAGE before --reset"
  # Every check above completes before stopping or moving any existing data.
  for path in "$data_path" "$tablespace_path"
  do
    [ ! -L "$path" ] || fail "cannot archive a symbolic link: $path"
    [ ! -e "$path" ] || [ -d "$path" ] || fail "not a directory: $path"
    [ ! -e "$path.$suffix" ] || fail "backup path already exists: $path.$suffix"
  done
  if [ "$exists" = true ]; then
    backup_inspect=$data_path.container-$suffix.json
    [ ! -e "$backup_inspect" ] || fail "backup file already exists: $backup_inspect"
    (umask 077; docker inspect "$ORACLE21C_CONTAINER" > "$backup_inspect")
    docker stop "$ORACLE21C_CONTAINER" >/dev/null
    docker rm "$ORACLE21C_CONTAINER" >/dev/null
    echo "[oracle-utf8] old UTF container removed; configuration preserved: $backup_inspect (contains credentials; protect this file)"
  fi
  for path in "$data_path" "$tablespace_path"
  do
    if [ -d "$path" ]; then
      mv "$path" "$path.$suffix"
      echo "[oracle-utf8] old data preserved: $path.$suffix"
    fi
  done
fi

sh "$SCRIPT_DIR/deploy.sh"
sh "$SCRIPT_DIR/initialize.sh"
echo "[oracle-utf8] READY: $ORACLE21C_CONTAINER / $ORACLE21C_PDB / AL32UTF8"
