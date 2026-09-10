#!/bin/bash
set -u
echo '--- all containers mounts/status ---'
docker ps -a --format '{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}'
echo '--- images ---'
docker images --no-trunc --format '{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}'
echo '--- volume inspect ---'
for v in $(docker volume ls -q); do
  echo "====$v"
  docker volume inspect "$v"
done
echo '--- root top ---'
du -xhd1 / 2>/dev/null | sort -h
echo '--- cache top ---'
du -xhd2 /var/cache 2>/dev/null | sort -h | tail -40
echo '--- log top ---'
du -xhd2 /var/log 2>/dev/null | sort -h | tail -40
echo '--- opt top ---'
du -xhd2 /opt 2>/dev/null | sort -h | tail -60
