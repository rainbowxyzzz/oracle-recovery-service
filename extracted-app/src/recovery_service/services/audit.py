from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.core.models.task import OperationAuditLog
from recovery_service.services.auth import AuthContext


async def record_audit(
    db: AsyncSession,
    actor: AuthContext | None,
    *,
    action: str,
    module: str,
    target_type: str | None = None,
    target_id: str | None = None,
    target_name: str | None = None,
    status: str = "success",
    payload: dict[str, Any] | None = None,
    error_message: str | None = None,
    request: Request | None = None,
) -> None:
    try:
        client_host = request.client.host if request and request.client else None
        user_agent = request.headers.get("user-agent") if request else None
        db.add(
            OperationAuditLog(
                user_id=uuid.UUID(actor.user_id) if actor and actor.user_id else None,
                username=actor.username if actor else None,
                auth_type=actor.auth_type if actor else "system",
                action=action,
                module=module,
                target_type=target_type,
                target_id=target_id,
                target_name=target_name,
                status=status,
                request_ip=client_host,
                user_agent=user_agent,
                payload=payload or {},
                error_message=error_message,
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
