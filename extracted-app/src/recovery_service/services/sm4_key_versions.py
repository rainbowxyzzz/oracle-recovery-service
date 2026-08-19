from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import desc, select

from recovery_service.api.schemas.doris_encryption import DorisSm4KeyVersionResponse
from recovery_service.common.security import decrypt_secret, encrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import DorisSm4BatchJob, DorisSm4FunctionDeployment, DorisSm4KeyVersion
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.settings import get_settings


def sm4_key_fingerprint(key_seed: str) -> str:
    return hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:16]


def register_sm4_key_version(
    *,
    key_seed: str,
    key_mode: str,
    connection_id: uuid.UUID | None = None,
    connection_name: str | None = None,
    function_name: str | None,
    decrypt_function_name: str | None,
    jar_filename: str | None,
    actor: AuthContext | None,
) -> DorisSm4KeyVersionResponse:
    fingerprint = sm4_key_fingerprint(key_seed)
    now = app_now()
    session = get_sync_session_factory()()
    try:
        existing = session.execute(
            select(DorisSm4KeyVersion)
            .where(DorisSm4KeyVersion.key_fingerprint == fingerprint)
            .where(DorisSm4KeyVersion.connection_id == connection_id)
            .where(DorisSm4KeyVersion.status == "active")
            .order_by(desc(DorisSm4KeyVersion.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if existing:
            existing.connection_name = connection_name
            existing.key_mode = "manual" if key_mode == "manual" else "random"
            existing.function_name = function_name
            existing.decrypt_function_name = decrypt_function_name
            existing.jar_filename = jar_filename
            existing.updated_at = now
            session.commit()
            session.refresh(existing)
            return _key_response(existing)
        row = DorisSm4KeyVersion(
            connection_id=connection_id,
            connection_name=connection_name,
            name=f"SM4-{fingerprint}",
            key_fingerprint=fingerprint,
            key_seed_enc=encrypt_secret(key_seed, get_settings().credential_encryption_key),
            key_mode="manual" if key_mode == "manual" else "random",
            function_name=function_name,
            decrypt_function_name=decrypt_function_name,
            jar_filename=jar_filename,
            status="active",
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            created_by_auth_type=actor.auth_type if actor else "api-key",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _key_response(row)
    finally:
        session.close()


def list_sm4_key_versions(*, status: str = "active", limit: int = 100) -> list[DorisSm4KeyVersionResponse]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DorisSm4KeyVersion).order_by(desc(DorisSm4KeyVersion.created_at)).limit(max(1, min(limit, 500)))
        if status != "all":
            stmt = stmt.where(DorisSm4KeyVersion.status == status)
        return [_key_response(row) for row in session.execute(stmt).scalars().all()]
    finally:
        session.close()


def get_sm4_key_seed(key_id: uuid.UUID) -> str:
    session = get_sync_session_factory()()
    try:
        row = session.get(DorisSm4KeyVersion, key_id)
        if not row or row.status != "active":
            raise KeyError("SM4 key version does not exist or is not active.")
        return decrypt_secret(row.key_seed_enc, get_settings().credential_encryption_key)
    finally:
        session.close()


def get_active_sm4_key_seed_for_jar(jar_filename: str) -> tuple[str, uuid.UUID, str]:
    clean_filename = (jar_filename or "").strip()
    if not clean_filename:
        raise KeyError("SM4 jar filename is required.")
    session = get_sync_session_factory()()
    try:
        row = session.execute(
            select(DorisSm4KeyVersion)
            .where(DorisSm4KeyVersion.jar_filename == clean_filename)
            .where(DorisSm4KeyVersion.status == "active")
            .order_by(desc(DorisSm4KeyVersion.updated_at), desc(DorisSm4KeyVersion.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if not row:
            raise KeyError(f"No active SM4 key version is bound to jar {clean_filename}.")
        seed = decrypt_secret(row.key_seed_enc, get_settings().credential_encryption_key)
        return seed, row.id, row.key_fingerprint
    finally:
        session.close()


def resolve_sm4_key_version_for_batch(
    *,
    key_id: uuid.UUID | None,
    connection_id: uuid.UUID | None,
    database: str | None = None,
) -> DorisSm4KeyVersionResponse | None:
    session = get_sync_session_factory()()
    try:
        row: DorisSm4KeyVersion | None = None
        if key_id:
            row = session.get(DorisSm4KeyVersion, key_id)
            if not row or row.status != "active":
                raise KeyError("SM4 key version does not exist or is not active.")
            return _key_response(row)
        if connection_id:
            has_deployment = session.execute(
                select(DorisSm4FunctionDeployment.id)
                .where(DorisSm4FunctionDeployment.connection_id == connection_id)
                .limit(1)
            ).scalar_one_or_none()
            if has_deployment and database:
                deployment = session.execute(
                    select(DorisSm4FunctionDeployment)
                    .where(DorisSm4FunctionDeployment.connection_id == connection_id)
                    .where(DorisSm4FunctionDeployment.database == database)
                    .where(DorisSm4FunctionDeployment.function_name == "CQ_SM4_ENCRYPT")
                    .order_by(desc(DorisSm4FunctionDeployment.attempted_at))
                    .limit(1)
                ).scalar_one_or_none()
                if not deployment or deployment.state != "success" or not deployment.key_version_id:
                    raise KeyError(
                        f"数据库 {database} 没有可用的 SM4 函数部署记录，请先在密钥函数页面重新创建并验证。"
                    )
                row = session.get(DorisSm4KeyVersion, deployment.key_version_id)
                if not row or row.status != "active":
                    raise KeyError(f"数据库 {database} 绑定的 SM4 密钥版本不存在或已停用。")
                return _key_response(row)
            row = session.execute(
                select(DorisSm4KeyVersion)
                .where(DorisSm4KeyVersion.connection_id == connection_id)
                .where(DorisSm4KeyVersion.status == "active")
                .order_by(desc(DorisSm4KeyVersion.updated_at), desc(DorisSm4KeyVersion.created_at))
                .limit(1)
            ).scalar_one_or_none()
        if not row:
            row = session.execute(
                select(DorisSm4KeyVersion)
                .where(DorisSm4KeyVersion.status == "active")
                .order_by(desc(DorisSm4KeyVersion.updated_at), desc(DorisSm4KeyVersion.created_at))
                .limit(1)
            ).scalar_one_or_none()
        return _key_response(row) if row else None
    finally:
        session.close()


def get_sm4_key_seed_for_batch(batch_id: uuid.UUID) -> tuple[str, uuid.UUID, str]:
    session = get_sync_session_factory()()
    try:
        job = session.get(DorisSm4BatchJob, batch_id)
        if not job:
            raise KeyError("SM4 batch job does not exist.")
        if not job.sm4_key_version_id:
            raise KeyError("SM4 batch job is not bound to a key version.")
        row = session.get(DorisSm4KeyVersion, job.sm4_key_version_id)
        if not row or row.status != "active":
            raise KeyError("SM4 key version does not exist or is not active.")
        seed = decrypt_secret(row.key_seed_enc, get_settings().credential_encryption_key)
        return seed, row.id, row.key_fingerprint
    finally:
        session.close()


def _key_response(row: DorisSm4KeyVersion) -> DorisSm4KeyVersionResponse:
    return DorisSm4KeyVersionResponse(
        key_id=row.id,
        connection_id=row.connection_id,
        connection_name=row.connection_name,
        name=row.name,
        key_fingerprint=row.key_fingerprint,
        key_mode=row.key_mode,  # type: ignore[arg-type]
        function_name=row.function_name,
        decrypt_function_name=row.decrypt_function_name,
        jar_filename=row.jar_filename,
        status=row.status,
        created_by_username=row.created_by_username,
        created_by_auth_type=row.created_by_auth_type,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _actor_uuid(actor: AuthContext | None) -> uuid.UUID | None:
    if not actor or not actor.user_id:
        return None
    try:
        return uuid.UUID(str(actor.user_id))
    except (TypeError, ValueError):
        return None
