#!/bin/sh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PACKAGE_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${ORACLE21C_ENV_FILE:-$PACKAGE_ROOT/.env}

if [ -f "$ENV_FILE" ]; then
  . "$ENV_FILE"
fi

MODE=${ORACLE21C_MODE:-auto}
IMAGE=${ORACLE21C_IMAGE:-softwareplant/oracle:clean-21.3.0-ee}
IMAGE_TAR=${ORACLE21C_IMAGE_TAR:-}
CONTAINER=${ORACLE21C_CONTAINER:-oracle-recovery-oracle21c-ee}

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fx "$CONTAINER" >/dev/null 2>&1
}

container_env_value() {
  key=$1
  docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' | \
    sed -n "s/^${key}=//p" | head -n 1
}

image_exists() {
  docker image inspect "$IMAGE" >/dev/null 2>&1
}

image_tar_exists() {
  [ -n "$IMAGE_TAR" ] || return 1
  case "$IMAGE_TAR" in
    /*) test -f "$IMAGE_TAR" ;;
    *) test -f "$(dirname "$ENV_FILE")/$IMAGE_TAR" ;;
  esac
}

case "$MODE" in
  external|EXTERNAL|false|FALSE|0|no|NO)
    echo "[oracle21c] external mode; skipping local Oracle 21c management"
    exit 0
    ;;
  container|CONTAINER|true|TRUE|1|yes|YES)
    ;;
  auto|AUTO|'')
    if ! container_exists && ! image_exists && ! image_tar_exists; then
      echo "[oracle21c] auto mode found no container, image, or image archive; skipping"
      exit 0
    fi
    ;;
  *)
    echo "[oracle21c] ERROR: ORACLE21C_MODE must be auto, container, or external" >&2
    exit 1
    ;;
esac

if container_exists; then
  if [ -z "${ORACLE21C_PASSWORD:-}" ]; then
    ORACLE21C_PASSWORD=$(container_env_value ORACLE_PWD)
    export ORACLE21C_PASSWORD
  fi
  if [ -z "${ORACLE21C_SID:-}" ]; then
    ORACLE21C_SID=$(container_env_value ORACLE_SID)
    export ORACLE21C_SID
  fi
  if [ -z "${ORACLE21C_PDB:-}" ]; then
    ORACLE21C_PDB=$(container_env_value ORACLE_PDB)
    export ORACLE21C_PDB
  fi
fi

export ORACLE21C_ENV_FILE=$ENV_FILE
if ! sh "$SCRIPT_DIR/deploy.sh"; then
  case "$MODE" in
    auto|AUTO|'')
      if container_exists; then
        echo "[oracle21c] WARNING: auto mode could not manage existing container $CONTAINER; skipping local Oracle 21c management. Set ORACLE21C_MODE=container to fail strictly, or ORACLE21C_MODE=external to manage it outside this package." >&2
        exit 0
      fi
      ;;
  esac
  exit 1
fi
sh "$SCRIPT_DIR/initialize.sh"
