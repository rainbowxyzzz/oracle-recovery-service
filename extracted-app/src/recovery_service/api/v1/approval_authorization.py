from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.approval_authorization import (
    ApprovalAuthorizationConfigPayload,
    ApprovalAuthorizationConfigResponse,
    ApprovalAuthorizationRunDetailResponse,
    ApprovalAuthorizationRunRequest,
    ApprovalAuthorizationRunResponse,
    ApprovalAuthorizationStepTestRequest,
    ApprovalAuthorizationStepTestResponse,
)
from recovery_service.services.approval_authorization import (
    create_auto_watch_run,
    create_full_run,
    create_config,
    get_config,
    get_run_detail,
    list_configs,
    list_runs,
    run_full_run_task,
    test_step,
    update_config,
)
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext

router = APIRouter(prefix="/approval-authorization", tags=["approval-authorization"])


@router.get("/configs", response_model=list[ApprovalAuthorizationConfigResponse])
async def list_approval_authorization_configs(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    return await list_configs(db)


@router.post("/configs", response_model=ApprovalAuthorizationConfigResponse, status_code=201)
async def create_approval_authorization_config(
    request: Request,
    body: ApprovalAuthorizationConfigPayload,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:manage")),
):
    try:
        result = await create_config(db, body, actor)
        await record_audit(
            db,
            actor,
            action="approval_authorization.config_create",
            module="approval-authorization",
            target_type="approval_authorization_config",
            target_id=str(result.id),
            target_name=result.name,
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/configs/{config_id}", response_model=ApprovalAuthorizationConfigResponse)
async def get_approval_authorization_config(
    config_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    try:
        return await get_config(db, config_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/configs/{config_id}", response_model=ApprovalAuthorizationConfigResponse)
async def update_approval_authorization_config(
    request: Request,
    config_id: uuid.UUID,
    body: ApprovalAuthorizationConfigPayload,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:manage")),
):
    try:
        result = await update_config(db, config_id, body)
        await record_audit(
            db,
            actor,
            action="approval_authorization.config_update",
            module="approval-authorization",
            target_type="approval_authorization_config",
            target_id=str(result.id),
            target_name=result.name,
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/configs/{config_id}/test-step", response_model=ApprovalAuthorizationStepTestResponse)
async def test_approval_authorization_step(
    config_id: uuid.UUID,
    body: ApprovalAuthorizationStepTestRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:execute")),
):
    try:
        return await test_step(db, config_id, body.step_key, body.context, actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"步骤测试失败：{exc}") from exc


@router.post("/configs/{config_id}/runs", response_model=ApprovalAuthorizationRunResponse, status_code=201)
async def start_approval_authorization_run(
    request: Request,
    config_id: uuid.UUID,
    body: ApprovalAuthorizationRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:execute")),
):
    try:
        result = await create_full_run(db, config_id, body.context, actor)
        background_tasks.add_task(run_full_run_task, result.id, config_id, body.context)
        await record_audit(
            db,
            actor,
            action="approval_authorization.run_submit",
            module="approval-authorization",
            target_type="approval_authorization_run",
            target_id=str(result.id),
            target_name=result.config_name,
            payload={"state": result.state},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/configs/{config_id}/watch-scan", response_model=ApprovalAuthorizationRunResponse, status_code=201)
async def start_approval_authorization_watch_scan(
    request: Request,
    config_id: uuid.UUID,
    body: ApprovalAuthorizationRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:execute")),
):
    try:
        result = await create_auto_watch_run(db, config_id, body.context, actor)
        background_tasks.add_task(run_full_run_task, result.id, config_id, {"mode": "auto_watch", **dict(body.context or {})})
        await record_audit(
            db,
            actor,
            action="approval_authorization.watch_scan_submit",
            module="approval-authorization",
            target_type="approval_authorization_run",
            target_id=str(result.id),
            target_name=result.config_name,
            payload={"state": result.state},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs", response_model=list[ApprovalAuthorizationRunResponse])
async def list_approval_authorization_runs(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    return await list_runs(db, limit=max(1, min(limit, 200)))


@router.get("/runs/{run_id}", response_model=ApprovalAuthorizationRunDetailResponse)
async def get_approval_authorization_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    try:
        return await get_run_detail(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
