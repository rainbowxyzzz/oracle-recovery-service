#!/usr/bin/env sh
set -eu

docker ps --filter "name=oracle-recovery" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

if [ -x ./status-oracle19c.sh ]; then
  ./status-oracle19c.sh || true
fi

if [ -x ./status-sqlserver.sh ]; then
  ./status-sqlserver.sh || true
fi

if [ -x ./status-mysql-restore.sh ]; then
  ./status-mysql-restore.sh || true
fi
