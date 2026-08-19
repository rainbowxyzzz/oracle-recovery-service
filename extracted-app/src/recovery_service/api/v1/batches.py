
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.task_create import BatchCreateRequest
from recovery_service.common.security import encrypt_secret
from recovery_service.core.domain import RemoteHost
from recovery_service.core.models.task import BatchJob, RecoveryTask
from recovery_service.db.repositories.task_repo import TaskRepository
from recovery_service.engine.discovery.remote_scanner import RemoteScanner
from recovery_service.settings import get_settings
from recovery_service.workers.tasks.run_recovery import run_recovery_task

router = APIRouter(prefix="/batches", tags=["batches"])


@router.post("", status_code=202)
async def create_batch(
    body: BatchCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("restore:submit")),
):
    settings = get_settings()
    enc = settings.credential_encryption_key
    host = RemoteHost(
        host=body.remote_host,
        port=body.remote_port,
        username=body.remote_user,
        password=body.remote_password.get_secret_value(),
    )
    groups = RemoteScanner().scan(host, body.remote_directory)
    repo = TaskRepository(db)
    task_ids: list[str] = []

    for idx in range(len(groups)):
        task = RecoveryTask(
            remote_host=body.remote_host,
            remote_port=body.remote_port,
            remote_user=body.remote_user,
            remote_password_enc=encrypt_secret(body.remote_password.get_secret_value(), enc),
            remote_directory=body.remote_directory,
            target_connection=body.target_connection,
            target_admin_user=body.target_admin_user,
            target_admin_password_enc=encrypt_secret(
                body.target_admin_password.get_secret_value(), enc
            ),
            options=body.options,
            state="created",
        )
        task = await repo.create(task)
        run_recovery_task.apply_async(
            args=[str(task.id)],
            kwargs={"volume_group_index": idx},
            queue=settings.celery_oracle_queue,
        )
        task_ids.append(str(task.id))

    batch = BatchJob(parent_options=body.model_dump(), task_ids=task_ids, state="running")
    db.add(batch)
    await db.commit()

    return {"batch_id": str(batch.id), "task_ids": task_ids, "volume_groups": len(groups)}
