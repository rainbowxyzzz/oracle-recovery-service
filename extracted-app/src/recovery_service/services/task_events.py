import uuid
from typing import Any

from recovery_service.common.logging import get_logger
from recovery_service.core.models.task import RecoveryTask, TaskEvent
from recovery_service.db.session import get_sync_session_factory

logger = get_logger(__name__)


def update_oracle_runtime_control(
    task_id: str | uuid.UUID | None,
    *,
    run_id: str,
    run_dir: str,
    job_name: str,
    container: str,
) -> None:
    if not task_id:
        return
    task_uuid = task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(str(task_id))
    session = get_sync_session_factory()()
    try:
        task = session.get(RecoveryTask, task_uuid)
        if not task:
            return
        task.oracle_run_id = run_id
        task.oracle_run_dir = run_dir
        task.oracle_job_name = job_name
        task.oracle_container = container
        metadata = dict(task.metadata_snapshot or {})
        metadata.update(
            {
                "oracle_auto_import_run_id": run_id,
                "oracle_auto_import_run_dir": run_dir,
                "oracle_datapump_job_name": job_name,
                "oracle_container": container,
            }
        )
        task.metadata_snapshot = metadata
        session.commit()
    finally:
        session.close()


def record_task_event(
    task_id: str | uuid.UUID | None,
    *,
    event_type: str,
    title: str,
    status: str = "info",
    message: str | None = None,
    payload: dict[str, Any] | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> None:
    if not task_id:
        return
    try:
        task_uuid = task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(str(task_id))
        session_factory = get_sync_session_factory()
        session = session_factory()
        try:
            session.add(
                TaskEvent(
                    task_id=task_uuid,
                    event_type=event_type,
                    title=title,
                    status=status,
                    message=message,
                    payload=payload or {},
                    stdout=stdout,
                    stderr=stderr,
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.warning(
            "task_event_record_failed",
            task_id=str(task_id),
            event_type=event_type,
            error=str(exc),
        )
