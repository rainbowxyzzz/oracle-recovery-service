#!/usr/bin/env sh
set -eu

docker rm -f oracle-recovery-api oracle-recovery-worker oracle-recovery-mysql oracle-recovery-redis >/dev/null 2>&1 || true
echo "Stopped oracle-recovery containers."
echo "Oracle19c, SQL Server, and MySQL restore target containers are kept intact."
echo "Stop target database containers manually only if you really need to."
