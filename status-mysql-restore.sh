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

MYSQL_RESTORE_CONTAINER_NAME="${MYSQL_RESTORE_CONTAINER_NAME:-mysql-recovery-target}"

docker ps -a --filter "name=$MYSQL_RESTORE_CONTAINER_NAME" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
