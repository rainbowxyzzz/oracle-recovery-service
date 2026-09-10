#!/bin/bash
set -u
active_ids=$(for c in $(docker ps -q); do docker inspect --format '{{.Image}}' "$c"; done | sort -u)
is_active() {
  echo "$active_ids" | grep -Fxq "$1"
}
echo '--- candidate image tags (dry run) ---'
for row in $(docker images --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}|{{.Size}}'); do
  repo=${row%%|*}
  rest=${row#*|}
  tag=${rest%%|*}
  rest=${rest#*|}
  id=${rest%%|*}
  size=${rest#*|}
  candidate=0
  case "$repo" in
    scip-app|scip-runtime|scip-doris)
      candidate=1
      ;;
    ai-catalog-suspect-service)
      [ "$tag" != '0.5.32' ] && candidate=1
      ;;
    oracle-recovery-service-api|oracle-recovery-service-worker|oracle-recovery-service-worker-*)
      case "$tag" in
        20260904-*|20260902-*|20260831-*) candidate=0 ;;
        *) candidate=1 ;;
      esac
      ;;
  esac
  if [ "$candidate" = 1 ] && ! is_active "$id"; then
    echo "$repo:$tag|$id|$size"
  fi
done | sort
echo '--- candidate containers (stopped only) ---'
docker ps -aq --filter status=exited | while read -r c; do
  docker inspect --format '{{.Name}}|{{.Config.Image}}|{{.State.FinishedAt}}' "$c"
done
echo '--- current disk ---'
df -h /
echo '--- current docker df ---'
docker system df
