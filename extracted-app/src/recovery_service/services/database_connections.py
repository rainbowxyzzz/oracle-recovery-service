from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.schemas.database_cleanup import CleanupConnection
from recovery_service.api.schemas.database_connection import (
    DatabaseConnectionCreate,
    DatabaseConnectionResponse,
    DatabaseConnectionUpdate,
)
from recovery_service.api.schemas.doris_csv_import import DorisFtpConnection
from recovery_service.common.security import decrypt_secret, encrypt_secret
from recovery_service.core.models.task import DatabaseConnectionProfile
from recovery_service.settings import get_settings


def _secret_value(value) -> str:
    return value.get_secret_value() if value else ""


def _encrypt(value: str, key: str) -> str:
    return encrypt_secret(value, key) if value else ""


def _decrypt(value: str, key: str) -> str:
    return decrypt_secret(value, key) if value else ""


def profile_response(profile: DatabaseConnectionProfile) -> DatabaseConnectionResponse:
    return DatabaseConnectionResponse(
        id=profile.id,
        name=profile.name,
        engine=profile.engine,  # type: ignore[arg-type]
        host=profile.host,
        port=profile.port,
        username=profile.username,
        database=profile.database,
        service_name=profile.service_name,
        dsn=profile.dsn,
        ssh_host=profile.ssh_host,
        ssh_port=profile.ssh_port,
        ssh_user=profile.ssh_user,
        container_name=profile.container_name,
        is_default=profile.is_default,
        has_password=bool(profile.password_enc),
        has_ssh_password=bool(profile.ssh_password_enc),
        last_test_ok=profile.last_test_ok,
        last_test_message=profile.last_test_message,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


async def ensure_default_profiles(session: AsyncSession) -> None:
    total = await session.scalar(select(func.count()).select_from(DatabaseConnectionProfile))
    if total:
        return
    settings = get_settings()
    enc = settings.credential_encryption_key
    defaults = _default_profiles()
    for item in defaults:
        profile = DatabaseConnectionProfile(
            name=item["name"],
            engine=item["engine"],
            host=item["host"],
            port=item["port"],
            username=item["username"],
            password_enc=_encrypt(item.get("password", ""), enc),
            database=item.get("database"),
            service_name=item.get("service_name"),
            dsn=item.get("dsn"),
            ssh_host=item.get("ssh_host"),
            ssh_port=item.get("ssh_port") or 22,
            ssh_user=item.get("ssh_user"),
            ssh_password_enc=_encrypt(item.get("ssh_password", ""), enc),
            container_name=item.get("container_name"),
            is_default=True,
        )
        session.add(profile)
    await session.commit()


async def list_profiles(session: AsyncSession) -> list[DatabaseConnectionProfile]:
    await ensure_default_profiles(session)
    result = await session.execute(
        select(DatabaseConnectionProfile).order_by(
            DatabaseConnectionProfile.engine.asc(),
            DatabaseConnectionProfile.is_default.desc(),
            DatabaseConnectionProfile.name.asc(),
        )
    )
    return list(result.scalars())


async def get_profile(session: AsyncSession, profile_id: uuid.UUID) -> DatabaseConnectionProfile:
    await ensure_default_profiles(session)
    profile = await session.get(DatabaseConnectionProfile, profile_id)
    if not profile:
        raise ValueError("数据库连接不存在。")
    return profile


async def create_profile(
    session: AsyncSession,
    body: DatabaseConnectionCreate,
) -> DatabaseConnectionProfile:
    enc = get_settings().credential_encryption_key
    profile = DatabaseConnectionProfile(
        name=body.name,
        engine=body.engine,
        host=body.host,
        port=body.port,
        username=body.username,
        password_enc=_encrypt(_secret_value(body.password), enc),
        database=body.database,
        service_name=body.service_name,
        dsn=body.dsn,
        ssh_host=body.ssh_host,
        ssh_port=body.ssh_port,
        ssh_user=body.ssh_user,
        ssh_password_enc=_encrypt(_secret_value(body.ssh_password), enc),
        container_name=body.container_name,
        is_default=body.is_default,
    )
    session.add(profile)
    if body.is_default:
        await _clear_other_defaults(session, profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def update_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
    body: DatabaseConnectionUpdate,
) -> DatabaseConnectionProfile:
    profile = await get_profile(session, profile_id)
    enc = get_settings().credential_encryption_key
    data = body.model_dump(exclude_unset=True)
    for field in (
        "name",
        "engine",
        "host",
        "port",
        "username",
        "database",
        "service_name",
        "dsn",
        "ssh_host",
        "ssh_port",
        "ssh_user",
        "container_name",
        "is_default",
    ):
        if field in data:
            setattr(profile, field, data[field])
    if "password" in data and body.password is not None:
        profile.password_enc = _encrypt(body.password.get_secret_value(), enc)
    if "ssh_password" in data and body.ssh_password is not None:
        profile.ssh_password_enc = _encrypt(body.ssh_password.get_secret_value(), enc)
    if profile.is_default:
        await _clear_other_defaults(session, profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def delete_profile(session: AsyncSession, profile_id: uuid.UUID) -> None:
    profile = await get_profile(session, profile_id)
    await session.delete(profile)
    await session.commit()


async def mark_test_result(
    session: AsyncSession,
    profile: DatabaseConnectionProfile,
    ok: bool,
    message: str,
) -> None:
    profile.last_test_ok = ok
    profile.last_test_message = message
    await session.commit()


def profile_to_cleanup_connection(profile: DatabaseConnectionProfile) -> CleanupConnection:
    enc = get_settings().credential_encryption_key
    return CleanupConnection(
        engine=profile.engine,  # type: ignore[arg-type]
        host=profile.host,
        port=profile.port,
        username=profile.username,
        password=_decrypt(profile.password_enc, enc),
        database=profile.database,
        service_name=profile.service_name,
        dsn=profile.dsn,
        ssh_host=profile.ssh_host,
        ssh_port=profile.ssh_port,
        ssh_user=profile.ssh_user,
        ssh_password=_decrypt(profile.ssh_password_enc, enc) or None,
        container_name=profile.container_name,
    )


def profile_to_ftp_connection(
    profile: DatabaseConnectionProfile,
    *,
    directory: str | None = None,
) -> DorisFtpConnection:
    enc = get_settings().credential_encryption_key
    return DorisFtpConnection(
        host=profile.host,
        port=profile.port or 21,
        username=profile.username,
        password=_decrypt(profile.password_enc, enc),
        directory=directory or profile.database or profile.dsn or "/",
    )


def connection_display(profile: DatabaseConnectionProfile) -> str:
    if profile.engine == "oracle":
        service = profile.service_name or profile.database or ""
        return f"{profile.host}:{profile.port or 1521}/{service}".rstrip("/")
    if profile.engine == "mysql":
        return f"{profile.host}:{profile.port or 3306}"
    if profile.engine == "doris":
        database = profile.database or ""
        return f"{profile.host}:{profile.port or 9030}/{database}".rstrip("/")
    if profile.engine == "ftp":
        directory = profile.database or profile.dsn or ""
        return f"{profile.host}:{profile.port or 21}/{directory.lstrip('/')}".rstrip("/")
    return f"{profile.host}:{profile.port or 1433}"


async def _clear_other_defaults(
    session: AsyncSession,
    profile: DatabaseConnectionProfile,
) -> None:
    result = await session.execute(
        select(DatabaseConnectionProfile).where(
            DatabaseConnectionProfile.engine == profile.engine,
            DatabaseConnectionProfile.id != profile.id,
        )
    )
    for item in result.scalars():
        item.is_default = False


def _default_profiles() -> Iterable[dict]:
    settings = get_settings()
    mysql_host = settings.mysql_restore_target_host or settings.mysql_restore_container_name
    mysql_port = settings.mysql_restore_host_port if settings.mysql_restore_target_host else 3306
    oracle_host = settings.oracle_target_host or settings.oracle_container_name
    oracle_port = settings.oracle_host_port if settings.oracle_target_host else 1521
    sqlserver_host = settings.sqlserver_target_host or settings.sqlserver_container_name
    sqlserver_port = settings.sqlserver_host_port if settings.sqlserver_target_host else 1433
    return [
        {
            "name": "默认 Oracle",
            "engine": "oracle",
            "host": oracle_host,
            "port": oracle_port,
            "username": "SYSTEM",
            "password": settings.oracle_pwd,
            "service_name": settings.oracle_pdb,
            "database": settings.oracle_pdb,
            "ssh_host": settings.oracle_docker_host,
            "ssh_port": settings.oracle_docker_ssh_port,
            "ssh_user": settings.oracle_docker_ssh_user,
            "ssh_password": settings.oracle_docker_ssh_password,
            "container_name": settings.oracle_container_name,
        },
        {
            "name": "默认 MySQL",
            "engine": "mysql",
            "host": mysql_host,
            "port": mysql_port,
            "username": "root",
            "password": settings.mysql_restore_root_password,
            "database": "",
            "ssh_host": settings.mysql_restore_docker_host,
            "ssh_port": settings.mysql_restore_docker_ssh_port,
            "ssh_user": settings.mysql_restore_docker_ssh_user,
            "ssh_password": settings.mysql_restore_docker_ssh_password,
            "container_name": settings.mysql_restore_container_name,
        },
        {
            "name": "默认 SQL Server",
            "engine": "sqlserver",
            "host": sqlserver_host,
            "port": sqlserver_port,
            "username": "SA",
            "password": settings.sqlserver_sa_password,
            "database": "",
            "ssh_host": settings.sqlserver_docker_host,
            "ssh_port": settings.sqlserver_docker_ssh_port,
            "ssh_user": settings.sqlserver_docker_ssh_user,
            "ssh_password": settings.sqlserver_docker_ssh_password,
            "container_name": settings.sqlserver_container_name,
        },
    ]
