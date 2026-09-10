#!/bin/bash
set -u
echo '--- active image ids ---'
for c in $(docker ps -q); do
  docker inspect --format '{{.Name}}\t{{.Image}}\t{{.Config.Image}}' "$c"
done
echo '--- stopped container image ids ---'
for c in $(docker ps -aq --filter status=exited); do
  docker inspect --format '{{.Name}}\t{{.Image}}\t{{.Config.Image}}\t{{.State.FinishedAt}}' "$c"
done
echo '--- image inventory ---'
docker images --no-trunc --format '{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}'
echo '--- image ids referenced by active containers ---'
for c in $(docker ps -q); do
  docker inspect --format '{{.Image}}' "$c"
done | sort -u
