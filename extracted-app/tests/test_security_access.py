import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import (
    Base,
    DataAsset,
    DatabaseConnectionProfile,
    DataSecurityAccessMapping,
)
from recovery_service.services.security_access import (
    resolve_protected_etl_sql,
    resolve_security_sql,
)


def _factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def test_restricted_sql_resolves_source_to_access_view_and_blocks_ciphertext_filter() -> None:
    engine, factory = _factory(); connection_id = uuid.uuid4(); asset_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=connection_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x"))
        session.add(DataAsset(id=asset_id, connection_id=connection_id, engine="doris", catalog="", database="ODS", table_name="CASE_RAW", layer="raw", schema_signature="v1"))
        session.add(DataSecurityAccessMapping(id=uuid.uuid4(), pipeline_id=uuid.uuid4(), standard_asset_id=uuid.uuid4(), source_asset_id=asset_id, sm4_task_definition_id=uuid.uuid4(), source_database="ODS", source_table="CASE_RAW", source_field="PHONE", standard_field="PHONE", secured_database="ODS", secured_table="CASE_RAW_SM4", access_database="ACCESS", access_table="CASE_RAW", contract_hash="x" * 64, source_schema_signature="v1", state="active"))
        session.add(DataSecurityAccessMapping(id=uuid.uuid4(), pipeline_id=uuid.uuid4(), standard_asset_id=uuid.uuid4(), source_asset_id=asset_id, sm4_task_definition_id=uuid.uuid4(), source_database="ODS", source_table="CASE_RAW", source_field="ID_CARD", standard_field="ID_CARD", secured_database="ODS", secured_table="CASE_RAW_SM4", access_database="ACCESS", access_table="CASE_RAW", contract_hash="y" * 64, source_schema_signature="v1", state="active"))
        session.commit()
    with patch("recovery_service.services.security_access.get_sync_session_factory", return_value=factory):
        resolved = resolve_security_sql(connection_id=connection_id, default_database="ODS", sql="SELECT PHONE, NAME FROM ODS.CASE_RAW")
        assert "ACCESS.CASE_RAW" in resolved["sql"]
        with pytest.raises(ValueError, match="密文字段"):
            resolve_security_sql(connection_id=connection_id, default_database="ODS", sql="SELECT NAME FROM ODS.CASE_RAW WHERE PHONE = '1'")
        with pytest.raises(ValueError, match="密文字段"):
            resolve_security_sql(connection_id=connection_id, default_database="ODS", sql="SELECT NAME FROM ODS.CASE_RAW WHERE ID_CARD = '1'")
    engine.dispose()


def test_restricted_sql_rejects_unactivated_mapping_without_plaintext_fallback() -> None:
    engine, factory = _factory(); connection_id = uuid.uuid4(); asset_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=connection_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x"))
        session.add(DataAsset(id=asset_id, connection_id=connection_id, engine="doris", catalog="", database="ODS", table_name="CASE_RAW", layer="raw", schema_signature="v1"))
        session.add(DataSecurityAccessMapping(id=uuid.uuid4(), pipeline_id=uuid.uuid4(), standard_asset_id=uuid.uuid4(), source_asset_id=asset_id, sm4_task_definition_id=uuid.uuid4(), source_database="ODS", source_table="CASE_RAW", source_field="PHONE", standard_field="PHONE", access_database="ACCESS", access_table="CASE_RAW", contract_hash="x" * 64, source_schema_signature="v1", state="blocked"))
        session.commit()
    with patch("recovery_service.services.security_access.get_sync_session_factory", return_value=factory):
        with pytest.raises(ValueError, match="拒绝明文回退"):
            resolve_security_sql(connection_id=connection_id, default_database="ODS", sql="SELECT PHONE FROM ODS.CASE_RAW")
    engine.dispose()


def test_protected_etl_rewrites_only_the_frozen_source_and_keeps_the_write_target() -> None:
    engine, factory = _factory(); connection_id = uuid.uuid4(); asset_id = uuid.uuid4(); contract_hash = "z" * 64
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=connection_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x"))
        session.add(DataAsset(id=asset_id, connection_id=connection_id, engine="doris", catalog="", database="原始数据层", table_name="案件登记原始表", layer="raw", schema_signature="v1"))
        session.add(DataSecurityAccessMapping(id=uuid.uuid4(), pipeline_id=uuid.uuid4(), standard_asset_id=uuid.uuid4(), source_asset_id=asset_id, sm4_task_definition_id=uuid.uuid4(), source_database="原始数据层", source_table="案件登记原始表", source_field="身份证号码", standard_field="身份证号码", secured_database="原始数据层", secured_table="案件登记原始表_密文", access_database="安全访问层", access_table="案件登记原始表", contract_hash=contract_hash, source_schema_signature="v1", state="active"))
        session.commit()
    context = {
        "source_assets": [{"database": "原始数据层", "table_name": "案件登记原始表"}],
        "field_contracts": [{"source_asset_id": str(asset_id), "source_field": "身份证号码", "contract_hash": contract_hash}],
    }
    with patch("recovery_service.services.security_access.get_sync_session_factory", return_value=factory):
        insert_result = resolve_protected_etl_sql(
            connection_id=connection_id,
            default_database="原始数据层",
            sql="INSERT INTO 明细数据层.案件登记标准表 SELECT 案件编号, 身份证号码 FROM 原始数据层.案件登记原始表",
            security_context=context,
        )
        create_result = resolve_protected_etl_sql(
            connection_id=connection_id,
            default_database="原始数据层",
            sql='CREATE TABLE 明细数据层.案件登记标准表副本 PROPERTIES ("replication_num" = "1") AS SELECT 案件编号, 身份证号码 FROM 原始数据层.案件登记原始表',
            security_context=context,
        )
        assert "明细数据层.案件登记标准表" in insert_result["sql"]
        assert "明细数据层.案件登记标准表副本" in create_result["sql"]
        assert "`安全访问层`.`案件登记原始表`" in insert_result["sql"]
        assert "`安全访问层`.`案件登记原始表`" in create_result["sql"]
        assert "原始数据层.案件登记原始表" not in insert_result["sql"]
        with pytest.raises(ValueError, match="不允许写回"):
            resolve_protected_etl_sql(
                connection_id=connection_id,
                default_database="原始数据层",
                sql="INSERT INTO 原始数据层.案件登记原始表 SELECT * FROM 安全访问层.案件登记原始表",
                security_context=context,
            )
    engine.dispose()
