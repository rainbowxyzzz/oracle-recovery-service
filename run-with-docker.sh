#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Missing .env" >&2
  exit 1
fi

set -a
. ./.env
set +a

if [ -f ./runtime-database-lib.sh ]; then
  . ./runtime-database-lib.sh
else
  echo "Missing runtime-database-lib.sh" >&2
  exit 1
fi

disable_firewall_on_start() {
  case "${DISABLE_FIREWALL_ON_START:-true}" in
    true|TRUE|1|yes|YES) ;;
    false|FALSE|0|no|NO)
      echo "DISABLE_FIREWALL_ON_START=false; keeping host firewall unchanged."
      return 0
      ;;
    *)
      echo "Unsupported DISABLE_FIREWALL_ON_START=${DISABLE_FIREWALL_ON_START}. Use true or false." >&2
      exit 1
      ;;
  esac

  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet firewalld 2>/dev/null; then
      echo "Stopping firewalld so containers can reach host-published database ports..."
      systemctl stop firewalld || echo "Warning: failed to stop firewalld; continuing." >&2
    fi
    if systemctl is-enabled --quiet firewalld 2>/dev/null; then
      echo "Disabling firewalld autostart..."
      systemctl disable firewalld >/dev/null 2>&1 || echo "Warning: failed to disable firewalld autostart; continuing." >&2
    fi
  elif command -v service >/dev/null 2>&1; then
    service firewalld stop >/dev/null 2>&1 || true
  fi
}

disable_firewall_on_start

API_PORT="${API_PORT:-8000}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
REDIS_PORT="${REDIS_PORT:-6379}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root}"
MYSQL_DATABASE="${MYSQL_DATABASE:-oracle_recovery}"
MYSQL_USER="${MYSQL_USER:-recovery}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-recovery}"
MYSQL_SERVICE_IMAGE="${MYSQL_SERVICE_IMAGE:-mysql:8.4}"
MYSQL_SERVICE_IMAGE_PREFIXES="${MYSQL_SERVICE_IMAGE_PREFIXES:-mysql}"
REDIS_SERVICE_IMAGE="${REDIS_SERVICE_IMAGE:-redis:7-alpine}"
REDIS_SERVICE_IMAGE_PREFIXES="${REDIS_SERVICE_IMAGE_PREFIXES:-redis}"
SERVICE_MYSQL_MODE="${SERVICE_MYSQL_MODE:-auto}"
SERVICE_REDIS_MODE="${SERVICE_REDIS_MODE:-auto}"
NO_IPTABLES_MODE="${NO_IPTABLES_MODE:-auto}"
ORACLE_HOME_HOST="${ORACLE_HOME_HOST:-./oracle-client-empty}"
case "$ORACLE_HOME_HOST" in
  /*) ;;
  ./*) ORACLE_HOME_HOST="$PWD/${ORACLE_HOME_HOST#./}" ;;
  *) ORACLE_HOME_HOST="$PWD/$ORACLE_HOME_HOST" ;;
esac

SERVICE_IMAGE_TAR="${SERVICE_IMAGE_TAR:-oracle-recovery-service-images.tar}"
if [ -f "$SERVICE_IMAGE_TAR" ]; then
  docker load -i "$SERVICE_IMAGE_TAR"
else
  echo "Image package $SERVICE_IMAGE_TAR was not found; using images already loaded on this server."
fi

ensure_recovery_network

detect_no_iptables() {
  case "$NO_IPTABLES_MODE" in
    true|TRUE|1|yes|YES) return 0 ;;
    false|FALSE|0|no|NO) return 1 ;;
    auto|"")
      if command -v iptables >/dev/null 2>&1; then
        return 1
      fi
      return 0
      ;;
    *)
      echo "Unsupported NO_IPTABLES_MODE=$NO_IPTABLES_MODE. Use auto, true, or false." >&2
      exit 1
      ;;
  esac
}

if detect_no_iptables; then
  NO_IPTABLES_ACTIVE=true
  echo "No iptables detected; using Docker 17 no-iptables compatibility mode."
else
  NO_IPTABLES_ACTIVE=false
fi
export NO_IPTABLES_MODE NO_IPTABLES_ACTIVE

RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-.runtime-databases.env}"
rm -f "$RUNTIME_ENV_FILE"
export RUNTIME_ENV_FILE

USE_LOCAL_MYSQL=false
case "$SERVICE_MYSQL_MODE" in
  container|CONTAINER|local|LOCAL) USE_LOCAL_MYSQL=true ;;
  external|EXTERNAL|remote|REMOTE|service|SERVICE) USE_LOCAL_MYSQL=false ;;
  auto|"")
    if [ "${MYSQL_HOST:-mysql}" = "mysql" ]; then
      USE_LOCAL_MYSQL=true
    fi
    ;;
  *)
    echo "Unsupported SERVICE_MYSQL_MODE=$SERVICE_MYSQL_MODE. Use auto, container, or external." >&2
    exit 1
    ;;
esac

USE_LOCAL_REDIS=false
case "$SERVICE_REDIS_MODE" in
  container|CONTAINER|local|LOCAL) USE_LOCAL_REDIS=true ;;
  external|EXTERNAL|remote|REMOTE|service|SERVICE) USE_LOCAL_REDIS=false ;;
  auto|"")
    if [ "${REDIS_HOST:-redis}" = "redis" ]; then
      USE_LOCAL_REDIS=true
    fi
    ;;
  *)
    echo "Unsupported SERVICE_REDIS_MODE=$SERVICE_REDIS_MODE. Use auto, container, or external." >&2
    exit 1
    ;;
esac

HOST_GATEWAY_IP="$(docker network inspect oracle-recovery-net --format '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}}{{end}}{{end}}' 2>/dev/null || true)"
if [ -z "$HOST_GATEWAY_IP" ]; then
  HOST_GATEWAY_IP="$(ip route 2>/dev/null | awk '/default/ {print $3; exit}' || true)"
fi
if [ -z "$HOST_GATEWAY_IP" ]; then
  echo "Could not detect Docker host gateway IP for host.docker.internal" >&2
  exit 1
fi

MYSQL_PORT_ARGS="-p $MYSQL_PORT:3306"
REDIS_PORT_ARGS="-p $REDIS_PORT:6379"
API_NETWORK_ARGS="--network oracle-recovery-net --add-host host.docker.internal:$HOST_GATEWAY_IP -p $API_PORT:8000"
API_ENV_ARGS=""
if [ "$NO_IPTABLES_ACTIVE" = "true" ]; then
  MYSQL_PORT_ARGS=""
  REDIS_PORT_ARGS=""
  API_NETWORK_ARGS="--network host"
fi

./start-oracle19c.sh
if [ "${SQLSERVER_ENABLED:-true}" = "true" ]; then
  ./start-sqlserver.sh
fi
if [ "${MYSQL_RESTORE_ENABLED:-true}" = "true" ]; then
  ./start-mysql-restore.sh
fi

RUNTIME_ENV_ARGS=""
if [ -f "$RUNTIME_ENV_FILE" ]; then
  RUNTIME_ENV_ARGS="--env-file $RUNTIME_ENV_FILE"
fi

docker rm -f \
  oracle-recovery-api \
  oracle-recovery-worker \
  oracle-recovery-worker-oracle \
  oracle-recovery-worker-sm4 \
  oracle-recovery-worker-sm3 \
  oracle-recovery-worker-sql \
  oracle-recovery-worker-data-sync \
  oracle-recovery-worker-data-platform >/dev/null 2>&1 || true
if [ "$USE_LOCAL_MYSQL" = "true" ]; then
  docker rm -f oracle-recovery-mysql >/dev/null 2>&1 || true
fi
if [ "$USE_LOCAL_REDIS" = "true" ]; then
  docker rm -f oracle-recovery-redis >/dev/null 2>&1 || true
fi

if [ "$USE_LOCAL_MYSQL" = "true" ]; then
  MYSQL_SERVICE_IMAGE="$(resolve_image_name "service MySQL" "$MYSQL_SERVICE_IMAGE" "$MYSQL_SERVICE_IMAGE_PREFIXES")"
  docker run -d \
    --name oracle-recovery-mysql \
    --restart unless-stopped \
    --privileged \
    --security-opt seccomp=unconfined \
    --pids-limit -1 \
    --ulimit nproc=65535:65535 \
    --network oracle-recovery-net \
    --network-alias mysql \
    -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
    -e MYSQL_DATABASE="$MYSQL_DATABASE" \
    -e MYSQL_USER="$MYSQL_USER" \
    -e MYSQL_PASSWORD="$MYSQL_PASSWORD" \
    $MYSQL_PORT_ARGS \
    -v oracle_recovery_mysql_data:/var/lib/mysql \
    "$MYSQL_SERVICE_IMAGE"
else
  echo "Using configured service MySQL at ${MYSQL_HOST:-127.0.0.1}:${MYSQL_PORT}; local MySQL container will not be started."
fi

if [ "$USE_LOCAL_REDIS" = "true" ]; then
  REDIS_SERVICE_IMAGE="$(resolve_image_name "service Redis" "$REDIS_SERVICE_IMAGE" "$REDIS_SERVICE_IMAGE_PREFIXES")"
  docker run -d \
    --name oracle-recovery-redis \
    --restart unless-stopped \
    --privileged \
    --security-opt seccomp=unconfined \
    --pids-limit -1 \
    --ulimit nproc=65535:65535 \
    --network oracle-recovery-net \
    --network-alias redis \
    $REDIS_PORT_ARGS \
    "$REDIS_SERVICE_IMAGE"
else
  echo "Using configured Redis at ${REDIS_HOST:-127.0.0.1}:${REDIS_PORT}; local Redis container will not be started."
fi

if [ "$USE_LOCAL_MYSQL" = "true" ]; then
  echo "Waiting for MySQL..."
  for i in $(seq 1 60); do
    if docker exec oracle-recovery-mysql mysqladmin ping -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" --silent >/dev/null 2>&1; then
      break
    fi
    if [ "$i" -eq 60 ]; then
      echo "MySQL did not become ready in time" >&2
      docker logs oracle-recovery-mysql --tail 80 >&2 || true
      exit 1
    fi
    sleep 2
  done
fi

if [ "$NO_IPTABLES_ACTIVE" = "true" ]; then
  if [ "$USE_LOCAL_MYSQL" = "true" ]; then
    MYSQL_API_HOST="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' oracle-recovery-mysql 2>/dev/null || true)"
  else
    MYSQL_API_HOST="${MYSQL_HOST:-}"
  fi
  if [ "$USE_LOCAL_REDIS" = "true" ]; then
    REDIS_API_HOST="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' oracle-recovery-redis 2>/dev/null || true)"
  else
    REDIS_API_HOST="${REDIS_HOST:-}"
  fi
  if [ -z "$MYSQL_API_HOST" ] || [ -z "$REDIS_API_HOST" ]; then
    echo "Could not detect MySQL/Redis host for no-iptables API mode" >&2
    exit 1
  fi
  API_ENV_ARGS="-e MYSQL_HOST=$MYSQL_API_HOST -e REDIS_HOST=$REDIS_API_HOST"
fi

docker run --rm \
  --network oracle-recovery-net \
  --privileged \
  --security-opt seccomp=unconfined \
  --pids-limit -1 \
  --ulimit nproc=65535:65535 \
  --env-file .env \
  $RUNTIME_ENV_ARGS \
  -v "$PWD/config:/app/config:ro" \
  oracle-recovery-service-api:latest \
  python scripts/init_db.py

docker run -d \
  --name oracle-recovery-api \
  --restart unless-stopped \
  $API_NETWORK_ARGS \
  --privileged \
  --security-opt seccomp=unconfined \
  --pids-limit -1 \
  --ulimit nproc=65535:65535 \
  --env-file .env \
  -e APP_SERVICE_MODE="${APP_SERVICE_MODE:-monolith}" \
  $RUNTIME_ENV_ARGS \
  $API_ENV_ARGS \
  -v "$PWD/config:/app/config:ro" \
  -v oracle_recovery_sm4_jars:/app/data/sm4-jars \
  oracle-recovery-service-api:latest

mkdir -p "$ORACLE_HOME_HOST"

start_worker_container() {
  worker_name="$1"
  worker_mode="$2"
  docker run -d \
    --name "$worker_name" \
    --restart unless-stopped \
    --network oracle-recovery-net \
    --privileged \
    --security-opt seccomp=unconfined \
    --pids-limit -1 \
    --ulimit nproc=65535:65535 \
    --add-host "host.docker.internal:$HOST_GATEWAY_IP" \
    --env-file .env \
    -e WORKER_SERVICE_MODE="$worker_mode" \
    $RUNTIME_ENV_ARGS \
    -v "$PWD/config:/app/config:ro" \
    -v oracle_recovery_sm4_jars:/app/data/sm4-jars \
    -v oracle_recovery_staging:/tmp/oracle-recovery-staging \
    -v "$ORACLE_HOME_HOST:/opt/oracle/client:ro" \
    oracle-recovery-service-worker:latest
}

if [ "${ENABLE_BUSINESS_WORKERS:-false}" = "true" ]; then
  start_worker_container oracle-recovery-worker-oracle oracle-restore
  start_worker_container oracle-recovery-worker-sm4 sm4
  start_worker_container oracle-recovery-worker-sm3 sm3
  start_worker_container oracle-recovery-worker-sql doris-sql
  start_worker_container oracle-recovery-worker-data-sync data-sync
  start_worker_container oracle-recovery-worker-data-platform data-platform
else
  start_worker_container oracle-recovery-worker "${WORKER_SERVICE_MODE:-monolith}"
fi

echo "Started. Open http://127.0.0.1:$API_PORT/ui"
