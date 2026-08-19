#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${CONTAINER:-oracle-recovery-oracle19c}"
ORACLE_PWD="${ORACLE_PWD:-ChangeMe_Oracle19c_123}"
ORACLE_PDB="${ORACLE_PDB:-ORCLPDB1}"
ORACLE_HOME_IN_CONTAINER="${ORACLE_HOME_IN_CONTAINER:-/opt/oracle/product/19c/dbhome_1}"
DMP_HOST_ROOT="${DMP_HOST_ROOT:-/data/oracle-recovery/oracle19c/dmp}"
DMP_CONTAINER_ROOT="${DMP_CONTAINER_ROOT:-/opt/oracle/recovery_dmp}"
TS_CONTAINER_ROOT="${TS_CONTAINER_ROOT:-/opt/oracle/recovery_tablespaces}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
EXPORT_HOST_DIR="$DMP_HOST_ROOT/export_test_$RUN_ID"
EXPORT_CONTAINER_DIR="$DMP_CONTAINER_ROOT/export_test_$RUN_ID"
SQLPLUS="$ORACLE_HOME_IN_CONTAINER/bin/sqlplus"
EXPDP="$ORACLE_HOME_IN_CONTAINER/bin/expdp"
EXP="$ORACLE_HOME_IN_CONTAINER/bin/exp"
CONNECT="system/$ORACLE_PWD@//127.0.0.1:1521/$ORACLE_PDB"

mkdir -p \
  "$EXPORT_HOST_DIR/expdp_full" \
  "$EXPORT_HOST_DIR/expdp_schema" \
  "$EXPORT_HOST_DIR/expdp_table" \
  "$EXPORT_HOST_DIR/expdp_tablespace" \
  "$EXPORT_HOST_DIR/legacy_exp_full" \
  "$EXPORT_HOST_DIR/legacy_exp_owner" \
  "$EXPORT_HOST_DIR/legacy_exp_tables" \
  "$EXPORT_HOST_DIR/logs"
chmod -R 777 "$EXPORT_HOST_DIR"

run_sql() {
  docker exec -i "$CONTAINER" /bin/bash -c "$SQLPLUS -s '$CONNECT'" <<SQL
$1
SQL
}

cat > "$EXPORT_HOST_DIR/logs/prepare_test_data.sql" <<SQL
set serveroutput on
whenever sqlerror exit sql.sqlcode

declare
  n number;
begin
  select count(*) into n from dba_tablespaces where tablespace_name = 'CLEANUP_TEST_TS';
  if n = 0 then
    execute immediate q'[create tablespace CLEANUP_TEST_TS datafile '$TS_CONTAINER_ROOT/cleanup_test_ts01.dbf' size 200m autoextend on next 50m maxsize 2g]';
  end if;

  select count(*) into n from dba_tablespaces where tablespace_name = 'CLEANUP_TEST_AUX_TS';
  if n = 0 then
    execute immediate q'[create tablespace CLEANUP_TEST_AUX_TS datafile '$TS_CONTAINER_ROOT/cleanup_test_aux_ts01.dbf' size 100m autoextend on next 50m maxsize 1g]';
  end if;

  select count(*) into n from dba_users where username = 'CLEANUP_TEST_USER';
  if n = 0 then
    execute immediate q'[create user CLEANUP_TEST_USER identified by "Cleanup_Test_123" default tablespace CLEANUP_TEST_TS temporary tablespace TEMP quota unlimited on CLEANUP_TEST_TS]';
  else
    execute immediate q'[alter user CLEANUP_TEST_USER identified by "Cleanup_Test_123" account unlock]';
    execute immediate q'[alter user CLEANUP_TEST_USER default tablespace CLEANUP_TEST_TS quota unlimited on CLEANUP_TEST_TS]';
  end if;

  select count(*) into n from dba_users where username = 'CLEANUP_TEST_AUX';
  if n = 0 then
    execute immediate q'[create user CLEANUP_TEST_AUX identified by "Cleanup_Test_123" default tablespace CLEANUP_TEST_AUX_TS temporary tablespace TEMP quota unlimited on CLEANUP_TEST_AUX_TS]';
  else
    execute immediate q'[alter user CLEANUP_TEST_AUX identified by "Cleanup_Test_123" account unlock]';
    execute immediate q'[alter user CLEANUP_TEST_AUX default tablespace CLEANUP_TEST_AUX_TS quota unlimited on CLEANUP_TEST_AUX_TS]';
  end if;
end;
/

grant connect, resource, create view, create sequence, create procedure, create synonym to CLEANUP_TEST_USER;
grant connect, resource, create view, create sequence, create procedure, create synonym to CLEANUP_TEST_AUX;

begin
  for item in (
    select 'CLEANUP_TEST_USER.EXPORT_ORDER_ITEMS' name from dual union all
    select 'CLEANUP_TEST_USER.EXPORT_ORDERS' from dual union all
    select 'CLEANUP_TEST_USER.EXPORT_CUSTOMERS' from dual union all
    select 'CLEANUP_TEST_USER.EXPORT_AUDIT_LOG' from dual union all
    select 'CLEANUP_TEST_AUX.EXPORT_REF_CODES' from dual
  ) loop
    begin
      execute immediate 'drop table ' || item.name || ' purge';
    exception
      when others then
        if sqlcode != -942 then raise; end if;
    end;
  end loop;
end;
/

begin
  execute immediate 'drop sequence CLEANUP_TEST_USER.EXPORT_ORDER_SEQ';
exception
  when others then
    if sqlcode != -2289 then raise; end if;
end;
/

create table CLEANUP_TEST_USER.EXPORT_CUSTOMERS (
  customer_id number primary key,
  customer_name varchar2(100),
  phone varchar2(32),
  created_at date default sysdate,
  remark varchar2(400)
) tablespace CLEANUP_TEST_TS;

create table CLEANUP_TEST_USER.EXPORT_ORDERS (
  order_id number primary key,
  customer_id number not null references CLEANUP_TEST_USER.EXPORT_CUSTOMERS(customer_id),
  order_no varchar2(40) not null,
  amount number(12,2),
  status varchar2(20),
  created_at date default sysdate
) tablespace CLEANUP_TEST_TS;

create table CLEANUP_TEST_USER.EXPORT_ORDER_ITEMS (
  item_id number primary key,
  order_id number not null references CLEANUP_TEST_USER.EXPORT_ORDERS(order_id),
  product_name varchar2(100),
  quantity number,
  price number(12,2)
) tablespace CLEANUP_TEST_TS;

create table CLEANUP_TEST_USER.EXPORT_AUDIT_LOG (
  log_id number primary key,
  biz_key varchar2(80),
  payload clob,
  created_at timestamp default systimestamp
) tablespace CLEANUP_TEST_TS lob (payload) store as securefile (tablespace CLEANUP_TEST_TS);

create sequence CLEANUP_TEST_USER.EXPORT_ORDER_SEQ start with 10000 increment by 1;

create table CLEANUP_TEST_AUX.EXPORT_REF_CODES (
  code_id number primary key,
  code_type varchar2(40),
  code_value varchar2(80),
  enabled char(1),
  created_at date default sysdate
) tablespace CLEANUP_TEST_AUX_TS;

insert into CLEANUP_TEST_USER.EXPORT_CUSTOMERS (customer_id, customer_name, phone, created_at, remark)
select level,
       '测试客户_' || to_char(level, 'FM000'),
       '1380000' || to_char(level, 'FM0000'),
       trunc(sysdate) - mod(level, 30),
       '用于 Oracle 导出/导入链路验证'
from dual connect by level <= 60;

insert into CLEANUP_TEST_USER.EXPORT_ORDERS (order_id, customer_id, order_no, amount, status, created_at)
select 1000 + level,
       mod(level - 1, 60) + 1,
       'ORD-' || to_char(sysdate, 'YYYYMMDD') || '-' || to_char(level, 'FM0000'),
       round(100 + dbms_random.value(1, 9000), 2),
       case mod(level, 4) when 0 then 'PAID' when 1 then 'NEW' when 2 then 'SHIPPED' else 'CLOSED' end,
       sysdate - mod(level, 20)
from dual connect by level <= 180;

insert into CLEANUP_TEST_USER.EXPORT_ORDER_ITEMS (item_id, order_id, product_name, quantity, price)
select 5000 + level,
       1000 + mod(level - 1, 180) + 1,
       '测试商品_' || to_char(mod(level, 25) + 1, 'FM00'),
       mod(level, 5) + 1,
       round(10 + dbms_random.value(1, 600), 2)
from dual connect by level <= 420;

insert into CLEANUP_TEST_USER.EXPORT_AUDIT_LOG (log_id, biz_key, payload)
select level,
       'EXPORT_TEST_' || to_char(level, 'FM0000'),
       to_clob('{"batch":"$RUN_ID","seq":' || level || ',"message":"Oracle export test payload"}')
from dual connect by level <= 120;

insert into CLEANUP_TEST_AUX.EXPORT_REF_CODES (code_id, code_type, code_value, enabled)
select level,
       case when mod(level, 2) = 0 then 'ORDER_STATUS' else 'CUSTOMER_LEVEL' end,
       'CODE_' || to_char(level, 'FM000'),
       case when mod(level, 7) = 0 then 'N' else 'Y' end
from dual connect by level <= 80;

create or replace view CLEANUP_TEST_USER.V_EXPORT_ORDER_SUMMARY as
select c.customer_id,
       c.customer_name,
       count(o.order_id) order_count,
       nvl(sum(o.amount), 0) total_amount
from CLEANUP_TEST_USER.EXPORT_CUSTOMERS c
left join CLEANUP_TEST_USER.EXPORT_ORDERS o on o.customer_id = c.customer_id
group by c.customer_id, c.customer_name;

create or replace procedure CLEANUP_TEST_USER.P_EXPORT_TEST_MARK(p_text in varchar2) as
begin
  insert into CLEANUP_TEST_USER.EXPORT_AUDIT_LOG(log_id, biz_key, payload)
  values (900000 + CLEANUP_TEST_USER.EXPORT_ORDER_SEQ.nextval, 'PROC_MARK', to_clob(p_text));
end;
/

create or replace directory DP_EXP_FULL as '$EXPORT_CONTAINER_DIR/expdp_full';
create or replace directory DP_EXP_SCHEMA as '$EXPORT_CONTAINER_DIR/expdp_schema';
create or replace directory DP_EXP_TABLE as '$EXPORT_CONTAINER_DIR/expdp_table';
create or replace directory DP_EXP_TABLESPACE as '$EXPORT_CONTAINER_DIR/expdp_tablespace';

commit;

select owner, table_name, num_rows
from dba_tables
where owner in ('CLEANUP_TEST_USER','CLEANUP_TEST_AUX')
  and table_name like 'EXPORT_%'
order by owner, table_name;

exit
SQL

docker exec -i "$CONTAINER" /bin/bash -c "$SQLPLUS -s '$CONNECT'" < "$EXPORT_HOST_DIR/logs/prepare_test_data.sql" \
  | tee "$EXPORT_HOST_DIR/logs/prepare_test_data.out"

cat > "$EXPORT_HOST_DIR/IMPORT_COMMANDS.txt" <<TXT
Export created at: $RUN_ID
Oracle container: $CONTAINER
PDB: $ORACLE_PDB
Host export dir: $EXPORT_HOST_DIR
Container export dir: $EXPORT_CONTAINER_DIR

Data Pump full import example:
impdp system/******@//127.0.0.1:1521/$ORACLE_PDB DIRECTORY=DP_EXP_FULL DUMPFILE=cleanup_expdp_full_%U.dmp FULL=Y LOGFILE=import_full.log

Data Pump schema import example:
impdp system/******@//127.0.0.1:1521/$ORACLE_PDB DIRECTORY=DP_EXP_SCHEMA DUMPFILE=cleanup_expdp_schema.dmp SCHEMAS=CLEANUP_TEST_USER,CLEANUP_TEST_AUX LOGFILE=import_schema.log

Data Pump table import example:
impdp system/******@//127.0.0.1:1521/$ORACLE_PDB DIRECTORY=DP_EXP_TABLE DUMPFILE=cleanup_expdp_tables.dmp TABLES=CLEANUP_TEST_USER.EXPORT_CUSTOMERS,CLEANUP_TEST_USER.EXPORT_ORDERS,CLEANUP_TEST_AUX.EXPORT_REF_CODES LOGFILE=import_tables.log

Data Pump tablespace import example:
impdp system/******@//127.0.0.1:1521/$ORACLE_PDB DIRECTORY=DP_EXP_TABLESPACE DUMPFILE=cleanup_expdp_tablespaces.dmp TABLESPACES=CLEANUP_TEST_TS,CLEANUP_TEST_AUX_TS LOGFILE=import_tablespaces.log

Legacy exp full import example:
imp system/******@//127.0.0.1:1521/$ORACLE_PDB FULL=Y FILE=$EXPORT_CONTAINER_DIR/legacy_exp_full/cleanup_legacy_full.dmp LOG=$EXPORT_CONTAINER_DIR/legacy_exp_full/import_legacy_full.log

Legacy exp owner import example:
imp system/******@//127.0.0.1:1521/$ORACLE_PDB FROMUSER=CLEANUP_TEST_USER,CLEANUP_TEST_AUX TOUSER=CLEANUP_TEST_USER,CLEANUP_TEST_AUX FILE=$EXPORT_CONTAINER_DIR/legacy_exp_owner/cleanup_legacy_owner.dmp LOG=$EXPORT_CONTAINER_DIR/legacy_exp_owner/import_legacy_owner.log
TXT

run_expdp() {
  local name="$1"
  shift
  echo "===== expdp $name =====" | tee -a "$EXPORT_HOST_DIR/logs/export_run.log"
  docker exec "$CONTAINER" /bin/bash -c "$EXPDP '$CONNECT' $*" 2>&1 | tee "$EXPORT_HOST_DIR/logs/${name}.console.log"
}

run_exp() {
  local name="$1"
  shift
  echo "===== exp $name =====" | tee -a "$EXPORT_HOST_DIR/logs/export_run.log"
  docker exec "$CONTAINER" /bin/bash -c "$EXP '$CONNECT' $*" 2>&1 | tee "$EXPORT_HOST_DIR/logs/${name}.console.log"
}

run_expdp expdp_full "FULL=Y DIRECTORY=DP_EXP_FULL DUMPFILE=cleanup_expdp_full_%U.dmp LOGFILE=cleanup_expdp_full.log FILESIZE=512M EXCLUDE=STATISTICS"
run_expdp expdp_schema "SCHEMAS=CLEANUP_TEST_USER,CLEANUP_TEST_AUX DIRECTORY=DP_EXP_SCHEMA DUMPFILE=cleanup_expdp_schema.dmp LOGFILE=cleanup_expdp_schema.log EXCLUDE=STATISTICS"
run_expdp expdp_table "TABLES=CLEANUP_TEST_USER.EXPORT_CUSTOMERS,CLEANUP_TEST_USER.EXPORT_ORDERS,CLEANUP_TEST_USER.EXPORT_ORDER_ITEMS,CLEANUP_TEST_AUX.EXPORT_REF_CODES DIRECTORY=DP_EXP_TABLE DUMPFILE=cleanup_expdp_tables.dmp LOGFILE=cleanup_expdp_tables.log EXCLUDE=STATISTICS"
run_expdp expdp_tablespace "TABLESPACES=CLEANUP_TEST_TS,CLEANUP_TEST_AUX_TS DIRECTORY=DP_EXP_TABLESPACE DUMPFILE=cleanup_expdp_tablespaces.dmp LOGFILE=cleanup_expdp_tablespaces.log EXCLUDE=STATISTICS"

run_exp legacy_full "FULL=Y FILE=$EXPORT_CONTAINER_DIR/legacy_exp_full/cleanup_legacy_full.dmp LOG=$EXPORT_CONTAINER_DIR/legacy_exp_full/cleanup_legacy_full.log CONSISTENT=Y STATISTICS=NONE"
run_exp legacy_owner "OWNER=CLEANUP_TEST_USER,CLEANUP_TEST_AUX FILE=$EXPORT_CONTAINER_DIR/legacy_exp_owner/cleanup_legacy_owner.dmp LOG=$EXPORT_CONTAINER_DIR/legacy_exp_owner/cleanup_legacy_owner.log CONSISTENT=Y STATISTICS=NONE"
run_exp legacy_tables "TABLES='(CLEANUP_TEST_USER.EXPORT_CUSTOMERS,CLEANUP_TEST_USER.EXPORT_ORDERS,CLEANUP_TEST_AUX.EXPORT_REF_CODES)' FILE=$EXPORT_CONTAINER_DIR/legacy_exp_tables/cleanup_legacy_tables.dmp LOG=$EXPORT_CONTAINER_DIR/legacy_exp_tables/cleanup_legacy_tables.log CONSISTENT=Y STATISTICS=NONE"

{
  echo "Export run: $RUN_ID"
  echo "Host export dir: $EXPORT_HOST_DIR"
  echo "Container export dir: $EXPORT_CONTAINER_DIR"
  echo
  echo "Files:"
  find "$EXPORT_HOST_DIR" -maxdepth 2 -type f -printf "%p\t%s bytes\n" | sort
  echo
  echo "Disk:"
  df -h "$EXPORT_HOST_DIR"
} | tee "$EXPORT_HOST_DIR/MANIFEST.txt"

echo "$EXPORT_HOST_DIR"
