import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from recovery_service.api.deps import require_admin
from recovery_service.services.auth import AuthContext
from recovery_service.services import harness_assistant as service
from recovery_service.services.assistant_execution import resume

router = APIRouter(prefix="/assistant", tags=["智能助手"], dependencies=[Depends(require_admin)])


class PlanRequest(BaseModel):
    pipeline_id: UUID
    file_name: str = Field(min_length=1, max_length=1024)
    sm4_task_id: UUID | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    pipeline_id: UUID | None = None
    sm4_task_id: UUID | None = None


class ConfirmRequest(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)


class ResumeRequest(BaseModel):
    acknowledge_partial_writes: bool = False


async def call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422 if isinstance(exc, ValueError) else 404, detail=str(exc)) from exc


@router.get("/catalog", summary="读取助手可用处理路径与全库加密任务，不执行数据操作")
async def catalog():
    return await call(service.catalog)


@router.post("/chat", summary="Harness 理解中文指令并生成待确认计划，不执行还原")
async def chat(body: ChatRequest, actor: AuthContext = Depends(require_admin)):
    return await call(service.interpret, body.message, str(body.pipeline_id) if body.pipeline_id else None,
                      str(body.sm4_task_id) if body.sm4_task_id else None, actor)


@router.post("/plans", summary="显式选择路径和文件生成待确认计划")
async def prepare(body: PlanRequest, actor: AuthContext = Depends(require_admin)):
    return await call(service.prepare_plan, body.pipeline_id, body.file_name, body.sm4_task_id, actor)


@router.get("/plans", summary="读取最近助手计划")
async def plans():
    return await call(service.list_plans)


@router.get("/plans/{plan_id}", summary="查看计划影响范围和四阶段运行进度")
async def get_plan(plan_id: UUID):
    return await call(service.get_plan, plan_id)


@router.post("/plans/{plan_id}/confirm", summary="确认完整范围并启动四阶段数据处理，会写入业务数据")
async def confirm(plan_id: UUID, body: ConfirmRequest, actor: AuthContext = Depends(require_admin)):
    return await call(service.confirm_plan, plan_id, body.plan_hash, actor)


@router.post("/plans/{plan_id}/resume", summary="核对部分写入后重试明确失败的阶段，不接受结果未知状态")
async def resume_plan(plan_id: UUID, body: ResumeRequest, actor: AuthContext = Depends(require_admin)):
    return await call(resume, plan_id, body.acknowledge_partial_writes, actor)
