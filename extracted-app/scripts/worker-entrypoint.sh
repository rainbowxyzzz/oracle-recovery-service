#!/bin/sh
set -e

ORACLE_CLIENT="${ORACLE_HOME:-/opt/oracle/client}"

if [ -d "$ORACLE_CLIENT/bin" ]; then
  export ORACLE_HOME="$ORACLE_CLIENT"
  export PATH="$ORACLE_HOME/bin:$PATH"
  export LD_LIBRARY_PATH="$ORACLE_HOME/lib:${LD_LIBRARY_PATH:-}"
  echo "[worker] ORACLE_HOME=$ORACLE_HOME"
else
  echo "[worker] WARN: Oracle client not mounted at $ORACLE_CLIENT — impdp will fail until ORACLE_HOME is configured."
fi

if command -v impdp >/dev/null 2>&1; then
  echo "[worker] impdp: $(command -v impdp)"
else
  echo "[worker] WARN: impdp not in PATH. Mount host Oracle home via ORACLE_HOME_HOST in .env / install.sh"
fi

WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-2}"
WORKER_PREFETCH="${CELERY_WORKER_PREFETCH_MULTIPLIER:-1}"
WORKER_SERVICE_MODE="${WORKER_SERVICE_MODE:-monolith}"
WORKER_QUEUES="${WORKER_QUEUES:-}"

if [ -z "$WORKER_QUEUES" ]; then
  case "$WORKER_SERVICE_MODE" in
    oracle-restore|oracle)
      WORKER_QUEUES="${CELERY_ORACLE_QUEUE:-oracle_restore}"
      ;;
    sm3|doris-sm3)
      WORKER_QUEUES="${CELERY_SM3_QUEUE:-doris_sm3}"
      ;;
    sm4|doris-sm4)
      WORKER_QUEUES="${CELERY_SM4_QUEUE:-doris_sm4}"
      ;;
    doris-sql|sql)
      WORKER_QUEUES="${CELERY_SQL_QUEUE:-doris_sql}"
      ;;
    data-sync)
      WORKER_QUEUES="${CELERY_DATA_SYNC_QUEUE:-data_sync}"
      ;;
    data-platform)
      WORKER_QUEUES="${CELERY_DATA_PLATFORM_QUEUE:-data_platform}"
      ;;
    resource-provisioning)
      WORKER_QUEUES="${CELERY_RESOURCE_PROVISIONING_QUEUE:-resource_provisioning}"
      ;;
    api-orchestration)
      WORKER_QUEUES="${CELERY_API_ORCHESTRATION_QUEUE:-api_orchestration}"
      ;;
    monolith|all|*)
      WORKER_QUEUES="${CELERY_DEFAULT_QUEUE:-celery},${CELERY_ORACLE_QUEUE:-oracle_restore},${CELERY_SM3_QUEUE:-doris_sm3},${CELERY_SM4_QUEUE:-doris_sm4},${CELERY_SQL_QUEUE:-doris_sql},${CELERY_DATA_SYNC_QUEUE:-data_sync},${CELERY_DATA_PLATFORM_QUEUE:-data_platform},${CELERY_RESOURCE_PROVISIONING_QUEUE:-resource_provisioning},${CELERY_API_ORCHESTRATION_QUEUE:-api_orchestration}"
      ;;
  esac
fi

echo "[worker] service_mode=$WORKER_SERVICE_MODE queues=$WORKER_QUEUES concurrency=$WORKER_CONCURRENCY prefetch_multiplier=$WORKER_PREFETCH"
exec celery -A recovery_service.workers.celery_app:celery_app worker --loglevel=info -Q "$WORKER_QUEUES" -c "$WORKER_CONCURRENCY" --prefetch-multiplier="$WORKER_PREFETCH"
