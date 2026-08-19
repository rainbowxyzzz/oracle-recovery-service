from __future__ import annotations

import uuid

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    DatabaseConnectionProfile,
    DorisMaskFieldMapping,
    DorisMaskTableAsset,
)

ACTIVE_MASK_STATUSES = {"queued", "running", "cancelling", "succeeded"}
MASK_ROLE_PRIORITY = {
    "source": 1,
    "masked": 2,
    "backup": 3,
}


def register_mask_task(
    session: Session,
    *,
    task_id: uuid.UUID,
    profile: DatabaseConnectionProfile,
    database: str,
    source_table: str,
    output_table: str | None,
    backup_table: str | None,
    algorithm: str,
    table_mode: str,
    columns: list[str],
    status: str = "running",
) -> None:
    _add_asset(
        session,
        task_id=task_id,
        profile=profile,
        database=database,
        table_name=source_table,
        source_table=source_table,
        output_table=output_table,
        backup_table=backup_table,
        role="source",
        algorithm=algorithm,
        table_mode=table_mode,
        columns=columns,
        status=status,
    )
    if output_table and output_table != source_table:
        _add_asset(
            session,
            task_id=task_id,
            profile=profile,
            database=database,
            table_name=output_table,
            source_table=source_table,
            output_table=output_table,
            backup_table=backup_table,
            role="masked",
            algorithm=algorithm,
            table_mode=table_mode,
            columns=columns,
            status=status,
        )
    if backup_table:
        _add_asset(
            session,
            task_id=task_id,
            profile=profile,
            database=database,
            table_name=backup_table,
            source_table=source_table,
            output_table=output_table,
            backup_table=backup_table,
            role="backup",
            algorithm=algorithm,
            table_mode=table_mode,
            columns=columns,
            status=status,
        )
    session.commit()


def finish_mask_task(
    session: Session,
    *,
    task_id: uuid.UUID,
    status: str,
    message: str | None = None,
) -> None:
    now = app_now()
    assets = session.execute(
        select(DorisMaskTableAsset).where(DorisMaskTableAsset.task_id == task_id)
    ).scalars().all()
    for asset in assets:
        asset.status = status
        asset.message = message
        asset.finished_at = now
    session.commit()


def record_field_mappings(
    session: Session,
    *,
    task_id: uuid.UUID,
    profile: DatabaseConnectionProfile,
    source_database: str,
    source_table: str,
    masked_database: str,
    masked_table: str,
    columns: list[str],
    algorithm: str,
    mapping_database: str | None = None,
    mapping_tables: dict[str, str] | None = None,
    status: str = "succeeded",
) -> None:
    existing = session.execute(
        select(DorisMaskFieldMapping).where(DorisMaskFieldMapping.task_id == task_id)
    ).scalars().all()
    for row in existing:
        session.delete(row)
    for column in columns:
        mapping_table = (mapping_tables or {}).get(column)
        session.add(
            DorisMaskFieldMapping(
                task_id=task_id,
                connection_id=profile.id,
                source_database=source_database,
                source_table_name=source_table,
                source_column_name=column,
                masked_database=masked_database,
                masked_table_name=masked_table,
                masked_column_name=column,
                algorithm=algorithm,
                mapping_database=mapping_database,
                mapping_table_name=mapping_table,
                mapping_original_column="original_value" if mapping_table else None,
                mapping_masked_column="sm3_value" if mapping_table else None,
                status=status,
            )
        )
    session.commit()


def latest_mask_assets_for_catalog(
    session: Session,
    *,
    connection_id: uuid.UUID,
    database: str,
) -> dict[str, DorisMaskTableAsset]:
    rows = session.execute(
        select(DorisMaskTableAsset)
        .where(
            DorisMaskTableAsset.connection_id == connection_id,
            DorisMaskTableAsset.database == database,
            DorisMaskTableAsset.status.in_(ACTIVE_MASK_STATUSES),
        )
        .order_by(desc(DorisMaskTableAsset.updated_at), desc(DorisMaskTableAsset.created_at))
    ).scalars().all()
    result: dict[str, DorisMaskTableAsset] = {}
    for row in rows:
        if row.table_name not in result:
            result[row.table_name] = row
    return result


def mask_asset_sort_priority(asset: DorisMaskTableAsset | None) -> int:
    if asset is None:
        return 0
    if asset.status in {"queued", "running", "cancelling"}:
        return 1
    return MASK_ROLE_PRIORITY.get(asset.role, 1)


def _add_asset(
    session: Session,
    *,
    task_id: uuid.UUID,
    profile: DatabaseConnectionProfile,
    database: str,
    table_name: str,
    source_table: str,
    output_table: str | None,
    backup_table: str | None,
    role: str,
    algorithm: str,
    table_mode: str,
    columns: list[str],
    status: str,
) -> None:
    session.add(
        DorisMaskTableAsset(
            task_id=task_id,
            connection_id=profile.id,
            database=database,
            table_name=table_name,
            source_table_name=source_table,
            output_table_name=output_table,
            backup_table_name=backup_table,
            role=role,
            algorithm=algorithm,
            table_mode=table_mode,
            columns=list(columns),
            status=status,
        )
    )
