import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from recovery_service.api.deps import require_permission
from recovery_service.api.schemas.data_automation import (
    DataAssetCreate,
    DataAutomationBlueprintCreate,
    DataAutomationPipelineCreate,
    DataAutomationPipelineUpdate,
    DataClassificationRuleCreate,
    DataLineageCreate,
    ReverseEncryptionExecuteRequest,
)
from recovery_service.services import data_automation as service
from recovery_service.services.auth import AuthContext

router = APIRouter(prefix="/data-automation", tags=["data-automation"])


@router.get("/pipelines")
async def pipelines(_: None = Depends(require_permission("dataPlatform:read"))):
    return {"pipelines": await asyncio.to_thread(service.list_pipelines)}


@router.post("/pipelines", status_code=201)
async def create_pipeline(body: DataAutomationPipelineCreate, actor: AuthContext = Depends(require_permission("dataPlatform:design"))):
    return await asyncio.to_thread(service.create_pipeline, body.model_dump(), actor)


@router.patch("/pipelines/{pipeline_id}")
async def update_pipeline(pipeline_id: UUID, body: DataAutomationPipelineUpdate, _: None = Depends(require_permission("dataPlatform:design"))):
    return await asyncio.to_thread(service.update_pipeline, pipeline_id, body.model_dump(exclude_unset=True))


@router.post("/pipelines/{pipeline_id}/scan")
async def scan_pipeline(pipeline_id: UUID, _: None = Depends(require_permission("dataPlatform:execute"))):
    return await asyncio.to_thread(service.scan_pipeline, pipeline_id)


@router.get("/batches")
async def batches(pipeline_id: UUID | None = None, limit: int = Query(default=100, ge=1, le=500), _: None = Depends(require_permission("dataPlatform:read"))):
    return {"batches": await asyncio.to_thread(service.list_batches, pipeline_id, limit)}


@router.get("/batches/{batch_id}")
async def batch(batch_id: UUID, _: None = Depends(require_permission("dataPlatform:read"))):
    return await asyncio.to_thread(service.get_batch, batch_id)


@router.post("/batches/{batch_id}/resume")
async def resume_batch(batch_id: UUID, _: None = Depends(require_permission("dataPlatform:execute"))):
    return await asyncio.to_thread(service.resume_batch, batch_id)


@router.post("/batches/{batch_id}/match-blueprint")
async def match_blueprint(batch_id: UUID, schema_contract: dict | None = None, _: None = Depends(require_permission("dataPlatform:execute"))):
    return await asyncio.to_thread(service.match_batch_blueprint, batch_id, schema_contract)


@router.post("/batches/{batch_id}/confirm-blueprint/{blueprint_id}")
async def confirm_blueprint(batch_id: UUID, blueprint_id: UUID, _: None = Depends(require_permission("dataPlatform:design"))):
    return await asyncio.to_thread(service.confirm_batch_blueprint, batch_id, blueprint_id)


@router.get("/blueprints")
async def blueprints(pipeline_id: UUID | None = None, _: None = Depends(require_permission("dataPlatform:read"))):
    return {"blueprints": await asyncio.to_thread(service.list_blueprints, pipeline_id)}


@router.post("/pipelines/{pipeline_id}/blueprints", status_code=201)
async def create_blueprint(pipeline_id: UUID, body: DataAutomationBlueprintCreate, actor: AuthContext = Depends(require_permission("dataPlatform:design"))):
    return await asyncio.to_thread(service.create_blueprint, pipeline_id, body.model_dump(), actor)


@router.get("/assets")
async def assets(limit: int = Query(default=200, ge=1, le=1000), _: None = Depends(require_permission("dataPlatform:read"))):
    return {"assets": await asyncio.to_thread(service.list_assets, limit)}


@router.post("/assets", status_code=201)
async def create_asset(body: DataAssetCreate, batch_id: UUID | None = None, _: None = Depends(require_permission("dataPlatform:design"))):
    return await asyncio.to_thread(service.register_asset, body.model_dump(), batch_id)


@router.post("/lineage", status_code=201)
async def create_lineage(body: DataLineageCreate, _: None = Depends(require_permission("dataPlatform:design"))):
    return await asyncio.to_thread(service.create_lineage_edge, body.model_dump(exclude_none=True))


@router.get("/lineage")
async def lineage_overview(
    search: str | None = Query(default=None, max_length=255),
    layer: str | None = Query(default=None, pattern="^(restored|raw|standard|secured)$"),
    batch_id: UUID | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    _: None = Depends(require_permission("dataPlatform:read")),
):
    return await asyncio.to_thread(service.lineage_overview, search=search, layer=layer, batch_id=batch_id, limit=limit)


@router.get("/lineage/{asset_id}")
async def lineage(asset_id: UUID, direction: str = Query(default="upstream", pattern="^(upstream|downstream)$"), max_depth: int = Query(default=8, ge=1, le=20), batch_id: UUID | None = None, _: None = Depends(require_permission("dataPlatform:read"))):
    return await asyncio.to_thread(service.trace_lineage, asset_id, direction=direction, max_depth=max_depth, batch_id=batch_id)


@router.get("/classification-rules")
async def classification_rules(_: None = Depends(require_permission("dataPlatform:read"))):
    return {"rules": await asyncio.to_thread(service.list_classification_rules)}


@router.post("/classification-rules", status_code=201)
async def create_classification_rule(body: DataClassificationRuleCreate, actor: AuthContext = Depends(require_permission("dataPlatform:design"))):
    return await asyncio.to_thread(service.create_classification_rule, body.model_dump(), actor)


@router.post("/assets/{asset_id}/classify")
async def classify_asset(asset_id: UUID, _: None = Depends(require_permission("dataPlatform:execute"))):
    return await asyncio.to_thread(service.classify_asset, asset_id)


@router.get("/assets/{asset_id}/reverse-encryption-plan")
async def reverse_encryption_plan(asset_id: UUID, _: None = Depends(require_permission("dorisEncrypt:read"))):
    return await asyncio.to_thread(service.build_reverse_encryption_plan, asset_id)


@router.post("/assets/{asset_id}/reverse-encryption-executions")
async def execute_reverse_encryption(
    asset_id: UUID,
    body: ReverseEncryptionExecuteRequest,
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    return await asyncio.to_thread(
        service.execute_reverse_encryption_plan,
        asset_id,
        body.pipeline_id,
        confirm=body.confirm,
        actor=actor,
    )
