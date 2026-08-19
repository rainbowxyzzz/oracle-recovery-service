#!/bin/sh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ORACLE21C_ENV_FILE=${ORACLE21C_ENV_FILE:-$SCRIPT_DIR/.env}
export ORACLE21C_ENV_FILE

exec sh "$SCRIPT_DIR/oracle21c-ee/start-oracle21c.sh"
