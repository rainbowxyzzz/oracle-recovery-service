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

SQLSERVER_CONTAINER_NAME="${SQLSERVER_CONTAINER_NAME:-sqlserver-recovery-mssql}"

docker ps -a --filter "name=^/${SQLSERVER_CONTAINER_NAME}$"
docker inspect --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' "$SQLSERVER_CONTAINER_NAME" 2>/dev/null || true
