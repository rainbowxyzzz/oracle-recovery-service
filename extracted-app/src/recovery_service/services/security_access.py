from __future__ import annotations

from collections import defaultdict
from typing import Any

import pymysql
import sqlglot
from sqlalchemy import select
from sqlglot import exp

from recovery_service.common.security import decrypt_secret
from recovery_service.core.models.task import (
    DataAsset,
    DatabaseConnectionProfile,
    DataSecurityAccessMapping,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.settings import get_settings


def activate_security_access_mappings(*, profile: DatabaseConnectionProfile, mapping_ids: list[str]) -> list[str]:
    session = get_sync_session_factory()()
    try:
        mappings = [session.get(DataSecurityAccessMapping, value) for value in mapping_ids]
        mappings = [item for item in mappings if item]
        create_security_access_views(profile, mappings)
        for item in mappings:
            item.state = "active"
        session.commit()
        return [str(item.id) for item in mappings]
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_security_access_views(profile: DatabaseConnectionProfile, mappings: list[DataSecurityAccessMapping]) -> None:
    groups: dict[tuple[str, str], list[DataSecurityAccessMapping]] = defaultdict(list)
    for item in mappings:
        if not item.secured_database or not item.secured_table:
            raise ValueError("安全对象尚未生成，不能激活逻辑别名。")
        groups[(item.access_database, item.access_table)].append(item)
    with _doris_conn(profile) as db:
        with db.cursor() as cur:
            for (access_db, access_table), rows in groups.items():
                targets = {(row.secured_database, row.secured_table) for row in rows}
                if len(targets) != 1:
                    raise ValueError(f"安全别名 {access_db}.{access_table} 指向多个安全表。")
                secured_db, secured_table = targets.pop()
                cur.execute(f"CREATE DATABASE IF NOT EXISTS {_q(access_db)}")
                cur.execute(f"CREATE OR REPLACE VIEW {_q(access_db)}.{_q(access_table)} AS SELECT * FROM {_q(secured_db)}.{_q(secured_table)}")


def resolve_security_sql(*, connection_id, default_database: str | None, sql: str) -> dict[str, Any]:
    expression = _parse_security_sql(sql, "受限 SQL")
    if not isinstance(expression, (exp.Select, exp.Union)):
        raise ValueError("受限 SQL 仅允许 SELECT / WITH 查询。")
    return _resolve_security_sources(
        expression,
        default_database=default_database,
        mappings=_load_security_mappings(connection_id),
    )


def resolve_protected_etl_sql(
    *,
    connection_id,
    default_database: str | None,
    sql: str,
    security_context: dict[str, Any],
) -> dict[str, Any]:
    expression = _parse_security_sql(sql, "安全数据加工 SQL")
    if not isinstance(expression, (exp.Select, exp.Union, exp.Insert, exp.Create)):
        raise ValueError("安全数据加工仅允许查询、INSERT ... SELECT 或 CREATE TABLE ... AS SELECT。")
    if isinstance(expression, exp.Insert) and not isinstance(expression.expression, (exp.Select, exp.Union)):
        raise ValueError("安全数据加工的 INSERT 必须从 SELECT 查询写入。")
    if isinstance(expression, exp.Create) and not isinstance(expression.expression, (exp.Select, exp.Union)):
        raise ValueError("安全数据加工的 CREATE TABLE 必须使用 AS SELECT 创建。")

    field_contracts = list((security_context or {}).get("field_contracts") or [])
    contract_hashes = {
        str(item.get("contract_hash") or "")
        for item in field_contracts
        if item.get("contract_hash")
    }
    source_assets = list((security_context or {}).get("source_assets") or [])
    expected_sources = {
        (
            str(item.get("database") or item.get("source_database") or "").casefold(),
            str(item.get("table_name") or item.get("source_table") or "").casefold(),
        )
        for item in [*source_assets, *field_contracts]
        if (item.get("database") or item.get("source_database"))
        and (item.get("table_name") or item.get("source_table"))
    }
    if not contract_hashes or not expected_sources:
        raise ValueError("安全数据加工缺少冻结的 ODS 字段覆盖合同。")

    target_tables = _write_target_tables(expression)
    for table in target_tables:
        if _table_key(table, default_database) in expected_sources:
            raise ValueError("安全数据加工不允许写回冻结合同中的 ODS 明文来源表。")

    mappings = [
        item
        for item in _load_security_mappings(connection_id)
        if item.contract_hash in contract_hashes
    ]
    return _resolve_security_sources(
        expression,
        default_database=default_database,
        mappings=mappings,
        expected_sources=expected_sources,
        skipped_tables={id(item) for item in target_tables},
    )


def _load_security_mappings(connection_id) -> list[DataSecurityAccessMapping]:
    session = get_sync_session_factory()()
    try:
        rows = session.scalars(select(DataSecurityAccessMapping)).all()
        return [item for item in rows if _mapping_connection_matches(session, item, connection_id)]
    finally:
        session.close()


def _parse_security_sql(sql: str, label: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(sql, read="doris")
    except Exception as exc:
        raise ValueError(f"{label} 无法安全解析：{exc}") from exc


def _resolve_security_sources(
    expression: exp.Expression,
    *,
    default_database: str | None,
    mappings: list[DataSecurityAccessMapping],
    expected_sources: set[tuple[str, str]] | None = None,
    skipped_tables: set[int] | None = None,
) -> dict[str, Any]:
    by_source: dict[tuple[str, str], list[DataSecurityAccessMapping]] = defaultdict(list)
    for mapping in mappings:
        by_source[(mapping.source_database.casefold(), mapping.source_table.casefold())].append(mapping)
    cte_names = {cte.alias_or_name.casefold() for cte in expression.find_all(exp.CTE) if cte.alias_or_name}
    resolved: list[dict[str, Any]] = []
    secure_aliases: dict[str, set[str]] = {}
    skipped_tables = skipped_tables or set()
    for table in expression.find_all(exp.Table):
        if id(table) in skipped_tables:
            continue
        name = table.name
        if not name or name.casefold() in cte_names:
            continue
        source_key = _table_key(table, default_database)
        source_db = source_key[0]
        source_mappings = by_source.get(source_key)
        if not source_mappings:
            if expected_sources is not None and source_key in expected_sources:
                raise ValueError(f"冻结合同中的 ODS 来源 {source_db}.{name} 没有可用的安全映射。")
            continue
        inactive = [item for item in source_mappings if item.state != "active"]
        if inactive:
            raise ValueError(f"安全映射 {source_db}.{name} 尚未激活，已拒绝明文回退。")
        access_targets = {(item.access_database, item.access_table) for item in source_mappings}
        if len(access_targets) != 1:
            raise ValueError(f"源表 {source_db}.{name} 存在冲突的安全别名。")
        access_database, access_table = access_targets.pop()
        alias = (table.alias_or_name or name).casefold()
        secure_aliases[alias] = {item.source_field.casefold() for item in source_mappings}
        table.set("this", exp.to_identifier(access_table))
        table.set("db", exp.to_identifier(access_database))
        table.set("catalog", None)
        resolved.append({
            "mapping_ids": [str(item.id) for item in source_mappings],
            "source": f"{source_mappings[0].source_database}.{source_mappings[0].source_table}",
            "access": f"{access_database}.{access_table}",
        })
    _reject_unsafe_ciphertext_operations(expression, secure_aliases)
    return {"sql": expression.sql(dialect="doris"), "mappings": resolved}


def _write_target_tables(expression: exp.Expression) -> list[exp.Table]:
    target = expression.this if isinstance(expression, (exp.Insert, exp.Create)) else None
    return list(target.find_all(exp.Table)) if target is not None else []


def _table_key(table: exp.Table, default_database: str | None) -> tuple[str, str]:
    return ((table.db or default_database or "").casefold(), table.name.casefold())


def _mapping_connection_matches(session, mapping: DataSecurityAccessMapping, connection_id) -> bool:
    asset = session.get(DataAsset, mapping.source_asset_id)
    return bool(asset and asset.connection_id == connection_id)


def _reject_unsafe_ciphertext_operations(expression: exp.Expression, secure_aliases: dict[str, set[str]]) -> None:
    if not secure_aliases:
        return
    for column in expression.find_all(exp.Column):
        field = column.name.casefold()
        alias = (column.table or "").casefold()
        fields = secure_aliases.get(alias)
        if fields is None and len(secure_aliases) == 1:
            fields = next(iter(secure_aliases.values()))
        if not fields or field not in fields:
            continue
        if column.find_ancestor(exp.Where, exp.Having, exp.Join, exp.Group, exp.Order, exp.Func):
            raise ValueError(f"受限 SQL 不允许对密文字段 {column.sql()} 执行 Join、筛选、排序、分组或函数计算。")


def _doris_conn(profile: DatabaseConnectionProfile):
    return pymysql.connect(host=profile.host, port=profile.port or 9030, user=profile.username, password=decrypt_secret(profile.password_enc, get_settings().credential_encryption_key), charset="utf8mb4", autocommit=True, connect_timeout=10)


def _q(value: str) -> str:
    return f"`{str(value).replace('`', '``')}`"
