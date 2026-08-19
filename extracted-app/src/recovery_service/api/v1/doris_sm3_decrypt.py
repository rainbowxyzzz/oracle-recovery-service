import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.doris_sm3_decrypt import DorisSm3DecryptRequest, DorisSm3DecryptResponse
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext
from recovery_service.services.database_connections import get_profile
from recovery_service.services.doris_sm3_decrypt import decrypt_sm3_by_mapping

router = APIRouter(prefix="/doris-sm3", tags=["doris-sm3"])


@router.post("/decrypt", response_model=DorisSm3DecryptResponse)
async def decrypt_doris_sm3_by_mapping(
    body: DorisSm3DecryptRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisSm3:decrypt")),
):
    try:
        profile = await get_profile(db, body.connection_id)
        result = await asyncio.to_thread(decrypt_sm3_by_mapping, profile, body)
        await record_audit(
            db,
            actor,
            action="decrypt_sm3_mapping",
            module="doris-sm3",
            target_type="mapping",
            target_name=body.field_category,
            payload={
                "field_category": body.field_category,
                "batch_size": len(body.items) if body.items else len(body.encrypted_values),
                "found": result.found,
                "ambiguous": result.ambiguous,
                "mapping_sources_count": len(result.mapping_sources),
            },
            request=request,
        )
        return result
    except ValueError as exc:
        await record_audit(
            db,
            actor,
            action="decrypt_sm3_mapping",
            module="doris-sm3",
            target_type="mapping",
            target_name=body.field_category,
            status="failed",
            payload={"field_category": body.field_category, "batch_size": len(body.items) if body.items else len(body.encrypted_values)},
            error_message=str(exc),
            request=request,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await record_audit(
            db,
            actor,
            action="decrypt_sm3_mapping",
            module="doris-sm3",
            target_type="mapping",
            target_name=body.field_category,
            status="failed",
            payload={"field_category": body.field_category, "batch_size": len(body.items) if body.items else len(body.encrypted_values)},
            error_message=str(exc),
            request=request,
        )
        raise HTTPException(status_code=400, detail=f"Doris SM3 mapping decrypt failed: {exc}") from exc
