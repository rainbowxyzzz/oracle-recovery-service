#!/bin/sh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${ORACLE21C_ENV_FILE:-$SCRIPT_DIR/oracle21c.env}

if [ -f "$ENV_FILE" ]; then
  . "$ENV_FILE"
fi

CONTAINER=${ORACLE21C_CONTAINER:-oracle-recovery-oracle21c-ee}
ORACLE_PDB=${ORACLE21C_PDB:-ORCLPDB1}
ORACLE_PASSWORD=${ORACLE21C_PASSWORD:-}
TIMEOUT=${ORACLE21C_STARTUP_TIMEOUT_SECONDS:-1800}
TEMP_AUTO_EXTEND=${ORACLE21C_TEMP_AUTO_EXTEND:-true}
TEMPFILE_NAME=${ORACLE21C_TEMPFILE_NAME:-temp_recovery_01.dbf}
TEMPFILE_INITIAL_SIZE=${ORACLE21C_TEMPFILE_INITIAL_SIZE:-20G}
TEMPFILE_NEXT_SIZE=${ORACLE21C_TEMPFILE_NEXT_SIZE:-2G}
TEMPFILE_MAX_SIZE=${ORACLE21C_TEMPFILE_MAX_SIZE:-100G}

log() {
  echo "[oracle21c-init] $*"
}

fail() {
  echo "[oracle21c-init] ERROR: $*" >&2
  exit 1
}

case "$ORACLE_PDB" in
  ''|*[!A-Za-z0-9_\#\$]*) fail "ORACLE21C_PDB must be a simple Oracle identifier" ;;
esac
[ -n "$ORACLE_PASSWORD" ] || fail "ORACLE21C_PASSWORD is required"
case "$TEMPFILE_NAME" in
  ''|*/*|*\\*) fail "ORACLE21C_TEMPFILE_NAME must be a file name, not a path" ;;
esac
case "$TEMPFILE_INITIAL_SIZE:$TEMPFILE_NEXT_SIZE:$TEMPFILE_MAX_SIZE" in
  *[!0-9GgMmKk:]*|'') fail "Oracle tempfile size values must use simple units, for example 20G or 20480M" ;;
esac

docker ps --format '{{.Names}}' | grep -Fx "$CONTAINER" >/dev/null 2>&1 || fail "container is not running: $CONTAINER"

oracle_sql() {
  docker exec -i "$CONTAINER" sh -c '
    resolved_home=
    for candidate in "${ORACLE_HOME:-}" /opt/oracle/product/21c/dbhome_1 /opt/oracle/product/19c/dbhome_1
    do
      if [ -n "$candidate" ] && [ -x "$candidate/bin/sqlplus" ] && ls "$candidate"/sqlplus/mesg/sp1*.msb >/dev/null 2>&1
      then
        resolved_home=$candidate
        break
      fi
    done
    [ -n "$resolved_home" ] || exit 127
    export ORACLE_HOME=$resolved_home
    export PATH=$ORACLE_HOME/bin:$PATH
    export LD_LIBRARY_PATH=$ORACLE_HOME/lib:${LD_LIBRARY_PATH:-}
    exec "$ORACLE_HOME/bin/sqlplus" -L -s "/ as sysdba"
  '
}

pdb_is_ready() {
  output=$(oracle_sql 2>/dev/null <<SQL || true
set heading off feedback off pages 0 verify off echo off
select open_mode from v\$pdbs where name = upper('$ORACLE_PDB');
exit
SQL
)
  echo "$output" | grep -F 'READ WRITE' >/dev/null 2>&1
}

elapsed=0
while ! pdb_is_ready
do
  if [ "$elapsed" -ge "$TIMEOUT" ]; then
    docker logs --tail 200 "$CONTAINER" >&2 || true
    fail "PDB $ORACLE_PDB did not become READ WRITE within $TIMEOUT seconds"
  fi
  log "waiting for PDB $ORACLE_PDB ($elapsed/$TIMEOUT seconds)"
  sleep 10
  elapsed=$((elapsed + 10))
done

log "creating directory objects and saving PDB state"
oracle_sql <<SQL
whenever sqlerror exit sql.sqlcode
set heading off feedback on pages 100 lines 240 verify off
alter pluggable database $ORACLE_PDB save state;
alter session set container=$ORACLE_PDB;
create or replace directory RECOVERY_DMP_DIR as '/opt/oracle/recovery_dmp';
create or replace directory RECOVERY_TABLESPACE_DIR as '/opt/oracle/recovery_tablespaces';
grant read, write on directory RECOVERY_DMP_DIR to SYSTEM;
grant read, write on directory RECOVERY_TABLESPACE_DIR to SYSTEM;
select directory_name || '=' || directory_path
from dba_directories
where directory_name in ('RECOVERY_DMP_DIR', 'RECOVERY_TABLESPACE_DIR')
order by directory_name;
exit
SQL

if [ "$TEMP_AUTO_EXTEND" = "true" ]; then
  log "ensuring TEMP tablespace has recovery tempfile $TEMPFILE_NAME"
  oracle_sql <<SQL
whenever sqlerror exit sql.sqlcode
set heading off feedback on pages 100 lines 240 verify off
alter session set container=$ORACLE_PDB;
declare
  v_count number := 0;
  v_dir varchar2(4000);
  v_file varchar2(4000);
begin
  select count(*) into v_count
  from dba_temp_files
  where tablespace_name = 'TEMP'
    and file_name like '%/$TEMPFILE_NAME';

  if v_count = 0 then
    select regexp_replace(file_name, '[^/]+$', '') into v_dir
    from (
      select file_name
      from dba_temp_files
      where tablespace_name = 'TEMP'
      order by file_id
    )
    where rownum = 1;

    v_file := v_dir || '$TEMPFILE_NAME';
    execute immediate 'alter tablespace TEMP add tempfile ''' || v_file || ''' size $TEMPFILE_INITIAL_SIZE autoextend on next $TEMPFILE_NEXT_SIZE maxsize $TEMPFILE_MAX_SIZE';
  end if;
end;
/
select tablespace_name || ' ' || file_name || ' ' || round(bytes/1024/1024) || 'MB auto=' || autoextensible
from dba_temp_files
where tablespace_name = 'TEMP'
order by file_id;
exit
SQL
else
  log "TEMP tablespace auto extension skipped by ORACLE21C_TEMP_AUTO_EXTEND=$TEMP_AUTO_EXTEND"
fi

CONNECT_OUTPUT=$(docker exec -i \
  -e ORACLE_PWD="$ORACLE_PASSWORD" \
  -e ORACLE_PDB="$ORACLE_PDB" \
  "$CONTAINER" sh -c '
    resolved_home=
    for candidate in "${ORACLE_HOME:-}" /opt/oracle/product/21c/dbhome_1
    do
      if [ -n "$candidate" ] && [ -x "$candidate/bin/sqlplus" ] && ls "$candidate"/sqlplus/mesg/sp1*.msb >/dev/null 2>&1
      then
        resolved_home=$candidate
        break
      fi
    done
    [ -n "$resolved_home" ] || exit 127
    export ORACLE_HOME=$resolved_home
    export PATH=$ORACLE_HOME/bin:$PATH
    export LD_LIBRARY_PATH=$ORACLE_HOME/lib:${LD_LIBRARY_PATH:-}
    printf "set heading off feedback off pages 0\nselect sys_context('"'"'USERENV'"'"','"'"'CON_NAME'"'"') from dual;\nexit\n" | "$ORACLE_HOME/bin/sqlplus" -L -s "system/$ORACLE_PWD@//127.0.0.1:1521/$ORACLE_PDB"
  ')
echo "$CONNECT_OUTPUT"
echo "$CONNECT_OUTPUT" | grep -F "$ORACLE_PDB" >/dev/null 2>&1 || fail "SYSTEM connection verification failed"
echo "$CONNECT_OUTPUT" | grep -E 'ORA-|SP2-|Error 6 initializing' >/dev/null 2>&1 && fail "SQLPlus returned an Oracle initialization error"

HOME_OUTPUT=$(docker exec "$CONTAINER" sh -c '
  resolved_home=${ORACLE_HOME:-/opt/oracle/product/21c/dbhome_1}
  export ORACLE_HOME=$resolved_home
  export PATH=$ORACLE_HOME/bin:$PATH
  printf "ORACLE_HOME=%s\n" "$ORACLE_HOME"
  "$ORACLE_HOME/bin/sqlplus" -V
')
echo "$HOME_OUTPUT"
echo "$HOME_OUTPUT" | grep -F 'SQL*Plus: Release 21' >/dev/null 2>&1 || fail "Oracle 21c SQLPlus verification failed"

log "initialization completed"
