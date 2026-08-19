#!/usr/bin/env sh
set -eu

# Tune the Oracle Recovery Service system metadata MySQL for large Data Sync
# component run logs. This changes MySQL runtime variables and persists them
# through MySQL SET PERSIST, without clearing data or rebuilding containers.

MYSQL_CONTAINER_NAME="${MYSQL_CONTAINER_NAME:-oracle-recovery-mysql}"
MYSQL_ROOT_USER="${MYSQL_ROOT_USER:-root}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root}"

MYSQL_TUNE_SORT_BUFFER_SIZE="${MYSQL_TUNE_SORT_BUFFER_SIZE:-16777216}"
MYSQL_TUNE_JOIN_BUFFER_SIZE="${MYSQL_TUNE_JOIN_BUFFER_SIZE:-4194304}"
MYSQL_TUNE_READ_BUFFER_SIZE="${MYSQL_TUNE_READ_BUFFER_SIZE:-1048576}"
MYSQL_TUNE_READ_RND_BUFFER_SIZE="${MYSQL_TUNE_READ_RND_BUFFER_SIZE:-4194304}"
MYSQL_TUNE_TMP_TABLE_SIZE="${MYSQL_TUNE_TMP_TABLE_SIZE:-268435456}"
MYSQL_TUNE_MAX_HEAP_TABLE_SIZE="${MYSQL_TUNE_MAX_HEAP_TABLE_SIZE:-268435456}"
MYSQL_TUNE_MAX_ALLOWED_PACKET="${MYSQL_TUNE_MAX_ALLOWED_PACKET:-268435456}"
MYSQL_TUNE_INNODB_BUFFER_POOL_SIZE="${MYSQL_TUNE_INNODB_BUFFER_POOL_SIZE:-536870912}"

mysql_exec() {
  docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$MYSQL_CONTAINER_NAME" \
    mysql -u"$MYSQL_ROOT_USER" "$@"
}

show_variables() {
  mysql_exec -N -e "
SHOW VARIABLES LIKE 'sort_buffer_size';
SHOW VARIABLES LIKE 'join_buffer_size';
SHOW VARIABLES LIKE 'read_buffer_size';
SHOW VARIABLES LIKE 'read_rnd_buffer_size';
SHOW VARIABLES LIKE 'tmp_table_size';
SHOW VARIABLES LIKE 'max_heap_table_size';
SHOW VARIABLES LIKE 'max_allowed_packet';
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW VARIABLES LIKE 'max_connections';
"
}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker command not found. Run this script on the Docker host." >&2
  exit 1
fi

if ! docker inspect "$MYSQL_CONTAINER_NAME" >/dev/null 2>&1; then
  echo "MySQL container not found: $MYSQL_CONTAINER_NAME" >&2
  echo "Override with: MYSQL_CONTAINER_NAME=your-container sh $0" >&2
  exit 1
fi

echo "Target MySQL container: $MYSQL_CONTAINER_NAME"
docker inspect "$MYSQL_CONTAINER_NAME" --format 'Image={{.Config.Image}} Status={{.State.Status}}'

echo "Host memory:"
free -h 2>/dev/null || true

echo "MySQL version:"
mysql_exec -N -e "SELECT VERSION();"

echo "Variables before tuning:"
show_variables

echo "Applying persistent tuning..."
mysql_exec -e "
SET PERSIST sort_buffer_size=${MYSQL_TUNE_SORT_BUFFER_SIZE};
SET PERSIST join_buffer_size=${MYSQL_TUNE_JOIN_BUFFER_SIZE};
SET PERSIST read_buffer_size=${MYSQL_TUNE_READ_BUFFER_SIZE};
SET PERSIST read_rnd_buffer_size=${MYSQL_TUNE_READ_RND_BUFFER_SIZE};
SET PERSIST tmp_table_size=${MYSQL_TUNE_TMP_TABLE_SIZE};
SET PERSIST max_heap_table_size=${MYSQL_TUNE_MAX_HEAP_TABLE_SIZE};
SET PERSIST max_allowed_packet=${MYSQL_TUNE_MAX_ALLOWED_PACKET};
SET PERSIST innodb_buffer_pool_size=${MYSQL_TUNE_INNODB_BUFFER_POOL_SIZE};
"

echo "Variables after tuning:"
show_variables

echo "Persisted MySQL config file, if available:"
docker exec "$MYSQL_CONTAINER_NAME" sh -c \
  'find /var/lib/mysql -maxdepth 2 -name mysqld-auto.cnf -print -exec cat {} \; 2>/dev/null || true'

echo "System MySQL tuning complete."
