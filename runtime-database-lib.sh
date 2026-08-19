#!/usr/bin/env sh

ensure_recovery_network() {
  docker network inspect oracle-recovery-net >/dev/null 2>&1 \
    || docker network create oracle-recovery-net >/dev/null
}

is_enabled() {
  case "${1:-}" in
    true|TRUE|1|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

is_local_target_disabled() {
  case "${1:-auto}" in
    external|EXTERNAL|remote|REMOTE|service|SERVICE) return 0 ;;
    *) return 1 ;;
  esac
}

normalize_csv_words() {
  echo "$1" | tr ',' ' '
}

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fx "$1" >/dev/null 2>&1
}

container_running() {
  docker ps --format '{{.Names}}' | grep -Fx "$1" >/dev/null 2>&1
}

find_container_by_port() {
  _port="$1"
  docker ps -a --format '{{.Names}}|{{.Ports}}' \
    | awk -F '|' -v port="$_port" '
      $2 ~ ":" port "->" || $2 ~ "0.0.0.0:" port "->" || $2 ~ "\\[::\\]:" port "->" {
        print $1
        exit
      }'
}

find_container_by_prefixes() {
  _prefixes="$(normalize_csv_words "$1")"
  for _prefix in $_prefixes; do
    docker ps -a --format '{{.Names}}' \
      | awk -v prefix="$_prefix" 'index($0, prefix) == 1 { print; exit }'
  done | awk 'NF { print; exit }'
}

resolve_container_name() {
  _label="$1"
  _configured="$2"
  _default="$3"
  _default_port="$4"
  _prefixes="$5"

  if [ -n "$_configured" ] && [ "$_configured" != "auto" ]; then
    if container_exists "$_configured"; then
      echo "$_configured"
      return 0
    fi
    if [ "$_configured" != "$_default" ]; then
      echo "$_configured"
      return 0
    fi
  fi

  if [ -n "$_default" ] && container_exists "$_default"; then
    echo "$_default"
    return 0
  fi

  _by_port="$(find_container_by_port "$_default_port" || true)"
  if [ -n "$_by_port" ]; then
    echo "$_by_port"
    return 0
  fi

  _by_prefix="$(find_container_by_prefixes "$_prefixes" || true)"
  if [ -n "$_by_prefix" ]; then
    echo "$_by_prefix"
    return 0
  fi

  echo "${_configured:-$_default}"
}

find_image_by_prefixes() {
  _prefixes="$(normalize_csv_words "$1")"
  for _prefix in $_prefixes; do
    docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
      | awk -v prefix="$_prefix" '
        $1 != "<none>:<none>" && index($1, prefix) == 1 { print $1; exit }
        index($2, prefix) == 1 { print $2; exit }
      '
  done | awk 'NF { print; exit }'
}

resolve_image_name() {
  _label="$1"
  _configured="$2"
  _prefixes="$3"

  if [ -n "$_configured" ] && [ "$_configured" != "auto" ]; then
    if docker image inspect "$_configured" >/dev/null 2>&1; then
      echo "$_configured"
      return 0
    fi
    _fallback="$(find_image_by_prefixes "$_prefixes" || true)"
    if [ -n "$_fallback" ]; then
      echo "Configured $_label image $_configured was not found; using local image $_fallback." >&2
      echo "$_fallback"
      return 0
    fi
    echo "$_label image $_configured was not found on this server." >&2
    return 1
  fi

  _found="$(find_image_by_prefixes "$_prefixes" || true)"
  if [ -n "$_found" ]; then
    echo "$_found"
    return 0
  fi

  echo "$_label image was not found on this server. Configure an image id/name or load a local image whose name starts with: $_prefixes" >&2
  return 1
}

start_or_keep_container() {
  _label="$1"
  _container="$2"

  if container_exists "$_container"; then
    if container_running "$_container"; then
      echo "$_label container $_container is already running; keeping it."
    else
      echo "Starting existing $_label container $_container..."
      docker start "$_container" >/dev/null
    fi
    docker network connect oracle-recovery-net "$_container" >/dev/null 2>&1 || true
    return 0
  fi
  return 1
}

record_runtime_env() {
  _key="$1"
  _value="$2"
  _file="${RUNTIME_ENV_FILE:-.runtime-databases.env}"
  if [ -n "$_value" ]; then
    printf '%s=%s\n' "$_key" "$_value" >> "$_file"
  fi
}
