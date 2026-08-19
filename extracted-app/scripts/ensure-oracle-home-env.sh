#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ensure-oracle-home-env.sh [--write-profile] [oracle-container-name]

Purpose:
  Detect the real Oracle Home inside an Oracle Docker container, verify SQL*Plus,
  and optionally persist the detected environment into /etc/profile.d.

Examples:
  ./scripts/ensure-oracle-home-env.sh oracle21c
  ./scripts/ensure-oracle-home-env.sh --write-profile oracle21c
USAGE
}

WRITE_PROFILE=0
CONTAINER=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --write-profile)
      WRITE_PROFILE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [ -n "$CONTAINER" ]; then
        echo "ERROR: multiple container names provided: $CONTAINER and $1" >&2
        usage >&2
        exit 2
      fi
      CONTAINER="$1"
      ;;
  esac
  shift
done

detect_container() {
  local matches
  matches="$(docker ps --format '{{.Names}}\t{{.Image}}' \
    | awk '
      BEGIN { IGNORECASE=1 }
      {
        name=$1
        image=$2
        haystack=name " " image
        if (name ~ /(api|worker|mysql|redis|migrate|frontend|nginx)/) {
          next
        }
        if (haystack ~ /(oracle[0-9]*c|oracle.*(ee|xe)|oracledb|oracle\/database|container-registry\.oracle\.com\/database|database.*oracle|orcl)/) {
          print name
        }
      }
    ')"
  local count
  count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [ "$count" = "1" ]; then
    printf '%s\n' "$matches" | sed '/^$/d' | head -n 1
    return 0
  fi
  if [ "$count" = "0" ]; then
    echo "ERROR: no Oracle-like Docker container found; pass container name explicitly." >&2
  else
    echo "ERROR: multiple Oracle-like containers found; pass one explicitly:" >&2
    printf '%s\n' "$matches" >&2
  fi
  return 1
}

if [ -z "$CONTAINER" ]; then
  CONTAINER="$(detect_container)"
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "ERROR: container not found: $CONTAINER" >&2
  exit 1
fi

DETECTED_HOME="$(
  docker exec "$CONTAINER" bash -lc '
set -euo pipefail
for candidate in \
  /opt/oracle/product/*/dbhome_* \
  /u01/app/oracle/product/*/dbhome_* \
  /opt/oracle/client; do
  if [ -x "$candidate/bin/sqlplus" ] && [ -d "$candidate/sqlplus/mesg" ]; then
    printf "%s\n" "$candidate"
    exit 0
  fi
done
exit 1
' 2>/dev/null
)"

if [ -z "$DETECTED_HOME" ]; then
  echo "ERROR: could not find a usable Oracle Home with bin/sqlplus and sqlplus/mesg in container $CONTAINER." >&2
  echo "Hint: run: docker exec -it $CONTAINER bash -lc \"find /opt/oracle /u01 -path '*/bin/sqlplus' -o -path '*/sqlplus/mesg'\"" >&2
  exit 1
fi

echo "Detected Oracle container: $CONTAINER"
echo "Detected ORACLE_HOME: $DETECTED_HOME"

docker exec "$CONTAINER" bash -lc "
export ORACLE_HOME='$DETECTED_HOME'
export PATH=\"\$ORACLE_HOME/bin:\$PATH\"
export LD_LIBRARY_PATH=\"\$ORACLE_HOME/lib:\${LD_LIBRARY_PATH:-}\"
echo \"ORACLE_HOME=\$ORACLE_HOME\"
echo \"sqlplus=\$(command -v sqlplus)\"
sqlplus -v
"

if [ "$WRITE_PROFILE" = "1" ]; then
  docker exec "$CONTAINER" bash -lc "cat > /etc/profile.d/oracle-home-autodetect.sh <<'EOF'
export ORACLE_HOME='$DETECTED_HOME'
export PATH=\"\$ORACLE_HOME/bin:\$PATH\"
export LD_LIBRARY_PATH=\"\$ORACLE_HOME/lib:\${LD_LIBRARY_PATH:-}\"
export NLS_LANG=AMERICAN_AMERICA.AL32UTF8
export LANG=C.UTF-8
EOF
chmod 0644 /etc/profile.d/oracle-home-autodetect.sh
"
  echo "Wrote /etc/profile.d/oracle-home-autodetect.sh inside $CONTAINER"
fi

echo "OK: Oracle Home environment is usable for SQL*Plus."
