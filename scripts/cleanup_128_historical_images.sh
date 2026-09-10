#!/bin/bash
set -u
set -o pipefail

stamp=$(date +%Y%m%d-%H%M%S)
audit_dir="/opt/oracle-recovery/cleanup-audit-$stamp"
mkdir -p "$audit_dir"

echo "audit_dir=$audit_dir"
df -h / > "$audit_dir/df-before.txt"
docker ps -a > "$audit_dir/docker-ps-a.txt"
docker images --no-trunc > "$audit_dir/docker-images.txt"
docker volume ls > "$audit_dir/docker-volumes.txt"
for c in $(docker ps -aq); do
  docker inspect "$c" > "$audit_dir/container-$c.json"
done

active_ids=$(for c in $(docker ps -q); do docker inspect --format '{{.Image}}' "$c"; done | sort -u)
is_active() {
  echo "$active_ids" | grep -Fxq "$1"
}

remove_ref() {
  local repo="$1"
  local tag="$2"
  local id
  id=$(docker images --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}' | awk -F'|' -v r="$repo" -v t="$tag" '$1 == r && $2 == t {print $3; exit}')
  [ -n "$id" ] || return 0
  if is_active "$id"; then
    echo "KEEP active $repo:$tag $id"
    return 0
  fi
  echo "REMOVE $repo:$tag $id"
  docker rmi "$repo:$tag" || echo "WARN failed to remove $repo:$tag" >&2
}

echo '--- remove stopped containers ---'
for c in $(docker ps -aq --filter status=exited); do
  echo "REMOVE container $c"
  docker rm "$c" || echo "WARN failed to remove container $c" >&2
done

echo '--- remove scip image history ---'
for repo in scip-app scip-runtime scip-doris; do
  docker images --no-trunc --format '{{.Repository}}|{{.Tag}}' | while IFS='|' read -r r t; do
    [ "$r" = "$repo" ] && remove_ref "$r" "$t"
  done
done

echo '--- remove old ai catalog image history ---'
docker images --no-trunc --format '{{.Repository}}|{{.Tag}}' | while IFS='|' read -r r t; do
  if [ "$r" = 'ai-catalog-suspect-service' ] && [ "$t" != '0.5.32' ]; then
    remove_ref "$r" "$t"
  fi
done

echo '--- remove old oracle-recovery application image history ---'
docker images --no-trunc --format '{{.Repository}}|{{.Tag}}' | while IFS='|' read -r r t; do
  case "$r" in
    oracle-recovery-service-api|oracle-recovery-service-worker|oracle-recovery-service-worker-*)
      case "$t" in
        20260904-*|20260902-*|20260831-*) ;;
        *) remove_ref "$r" "$t" ;;
      esac
      ;;
  esac
done

echo '--- remove dangling layers again ---'
docker image prune -f

df -h / > "$audit_dir/df-after.txt"
docker system df > "$audit_dir/docker-system-df-after.txt"
docker ps -a > "$audit_dir/docker-ps-a-after.txt"
docker images --no-trunc > "$audit_dir/docker-images-after.txt"
echo '--- after ---'
cat "$audit_dir/df-after.txt"
cat "$audit_dir/docker-system-df-after.txt"
echo "audit_dir=$audit_dir"
