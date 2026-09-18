#!/bin/sh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ORACLE21C_ENV_FILE=${ORACLE21C_ENV_FILE:-$SCRIPT_DIR/.env}
export ORACLE21C_ENV_FILE

sh "$SCRIPT_DIR/oracle21c-ee/start-oracle21c.sh"
sh "$SCRIPT_DIR/oracle21c-ee/start-oracle21c-utf8.sh"
