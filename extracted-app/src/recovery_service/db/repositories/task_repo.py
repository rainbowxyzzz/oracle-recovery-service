import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.common.time import app_now
from recovery_service.core.models.task import RecoveryTask, TaskEvent, TaskStep


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, task: RecoveryTask) -> RecoveryTask:
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def get(self, task_id: uuid.UUID) -> RecoveryTask | None:
        return await self.session.get(RecoveryTask, task_id)

    async def update_state(
        self,
        task: RecoveryTask,
        state: str,
        *,
        policy_node: str | None = None,
        error: str | None = None,
        progress: float | None = None,
    ) -> RecoveryTask:
        task.state = state
        if policy_node is not None:
            task.current_policy_node = policy_node
        if error is not None:
            task.error_message = error
        if progress is not None:
            task.progress_percent = progress
        if state in ("succeeded", "succeeded_with_warnings", "failed", "cancelled"):
            task.finished_at = app_now()
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def add_step(
        self,
        task_id: uuid.UUID,
        node_id: str,
        status: str,
        message: str | None = None,
        stderr: str | None = None,
        stdout: str | None = None,
    ) -> TaskStep:
        step = TaskStep(
            task_id=task_id,
            node_id=node_id,
            status=status,
            message=message,
            stderr_excerpt=stderr,
            stdout_excerpt=stdout,
            finished_at=app_now(),
        )
        self.session.add(step)
        await self.session.commit()
        await self.session.refresh(step)
        return step

    async def list_recent(self, limit: int = 50) -> list[RecoveryTask]:
        result = await self.session.execute(
            select(RecoveryTask).order_by(RecoveryTask.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def list_events(self, task_id: uuid.UUID) -> list[TaskEvent]:
        result = await self.session.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
        )
        return list(result.scalars().all())
