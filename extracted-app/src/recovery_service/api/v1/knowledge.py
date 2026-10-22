import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.knowledge import (
    KnowledgeChatRequest,
    KnowledgeSearchRequest,
    KnowledgeSourceCreate,
    KnowledgeSourceUpdate,
)
from recovery_service.core.models.knowledge import (
    KnowledgeDocument,
    KnowledgeIndexRun,
    KnowledgeSource,
)
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext
from recovery_service.services.knowledge_base import (
    KnowledgeServiceError,
    chat_knowledge,
    document_dict,
    index_run_dict,
    resolve_source_path,
    run_index,
    search_knowledge,
    source_dict,
)
from recovery_service.settings import get_settings

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


async def _source_or_404(db: AsyncSession, source_id: uuid.UUID) -> KnowledgeSource:
    source = await db.get(KnowledgeSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="知识源不存在。")
    return source


@router.get("/status")
async def get_status(
    _: AuthContext = Depends(require_permission("knowledgeBase:read")),
):
    settings = get_settings()
    return {
        "enabled": settings.knowledge_enabled,
        "vault_available": settings.knowledge_vault_root.resolve().is_dir(),
        "qdrant_configured": bool(settings.knowledge_qdrant_url),
        "embedding_configured": bool(settings.knowledge_embedding_base_url and settings.knowledge_embedding_model),
        "chat_configured": bool(settings.knowledge_chat_base_url and settings.knowledge_chat_model),
        "collection": settings.knowledge_qdrant_collection,
        "embedding_model": settings.knowledge_embedding_model,
        "chat_model": settings.knowledge_chat_model,
    }


@router.get("/sources")
async def list_sources(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("knowledgeBase:read")),
):
    items = (await db.scalars(select(KnowledgeSource).order_by(KnowledgeSource.created_at.desc()))).all()
    return [source_dict(item) for item in items]


@router.post("/sources")
async def create_source(
    body: KnowledgeSourceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("knowledgeBase:manage")),
):
    try:
        resolve_source_path(body.root_path)
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source = KnowledgeSource(
        name=body.name.strip(),
        root_path=(body.root_path.strip() or "."),
        enabled=body.enabled,
        created_by_username=actor.username,
    )
    db.add(source)
    try:
        await db.commit()
        await db.refresh(source)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="知识源名称已存在。") from exc
    await record_audit(
        db,
        actor,
        action="knowledge.source_create",
        module="knowledge-base",
        target_type="knowledge_source",
        target_id=str(source.id),
        target_name=source.name,
        payload={"root_path": source.root_path, "enabled": source.enabled},
        request=request,
    )
    return source_dict(source)


@router.put("/sources/{source_id}")
async def update_source(
    source_id: uuid.UUID,
    body: KnowledgeSourceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("knowledgeBase:manage")),
):
    source = await _source_or_404(db, source_id)
    values = body.model_dump(exclude_unset=True)
    if "root_path" in values:
        try:
            resolve_source_path(values["root_path"])
        except KnowledgeServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        values["root_path"] = values["root_path"].strip() or "."
    if "name" in values:
        values["name"] = values["name"].strip()
    for key, value in values.items():
        setattr(source, key, value)
    try:
        await db.commit()
        await db.refresh(source)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="知识源名称已存在。") from exc
    await record_audit(
        db,
        actor,
        action="knowledge.source_update",
        module="knowledge-base",
        target_type="knowledge_source",
        target_id=str(source.id),
        target_name=source.name,
        payload={"changed_fields": sorted(values)},
        request=request,
    )
    return source_dict(source)


@router.post("/sources/{source_id}/index")
async def index_source(
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("knowledgeBase:index")),
):
    source = await _source_or_404(db, source_id)
    try:
        result = await run_index(db, source, actor.username or actor.auth_type)
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="knowledge.index_run",
        module="knowledge-base",
        target_type="knowledge_source",
        target_id=str(source.id),
        target_name=source.name,
        status="success" if result["state"] == "succeeded" else "warning",
        payload={key: result[key] for key in (
            "state", "scanned_count", "indexed_count", "unchanged_count", "skipped_count", "deleted_count", "error_count"
        )},
        request=request,
    )
    return result


@router.get("/index-runs")
async def list_index_runs(
    source_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("knowledgeBase:index")),
):
    stmt = select(KnowledgeIndexRun)
    if source_id:
        stmt = stmt.where(KnowledgeIndexRun.source_id == source_id)
    items = (await db.scalars(stmt.order_by(KnowledgeIndexRun.started_at.desc()).limit(limit))).all()
    return [index_run_dict(item) for item in items]


@router.get("/documents")
async def list_documents(
    source_id: uuid.UUID | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("knowledgeBase:read")),
):
    stmt = select(KnowledgeDocument)
    if source_id:
        stmt = stmt.where(KnowledgeDocument.source_id == source_id)
    if not include_deleted:
        stmt = stmt.where(KnowledgeDocument.deleted_at.is_(None))
    items = (await db.scalars(stmt.order_by(KnowledgeDocument.updated_at.desc()).limit(limit))).all()
    return [document_dict(item) for item in items]


@router.post("/search")
async def search(
    body: KnowledgeSearchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("knowledgeBase:read")),
):
    try:
        hits = await search_knowledge(body.query.strip(), body.top_k)
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="knowledge.search",
        module="knowledge-base",
        target_type="knowledge_query",
        payload={"result_count": len(hits), "top_k": body.top_k},
        request=request,
    )
    return {"results": hits}


@router.post("/chat")
async def chat(
    body: KnowledgeChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("knowledgeBase:chat")),
):
    try:
        result = await chat_knowledge(body.query.strip(), body.top_k)
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="knowledge.chat",
        module="knowledge-base",
        target_type="knowledge_query",
        payload={"citation_count": len(result["citations"]), "top_k": body.top_k},
        request=request,
    )
    return result
