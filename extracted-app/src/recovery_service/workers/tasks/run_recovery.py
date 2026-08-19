import uuid

from sqlalchemy import update

from recovery_service.common.time import app_now
from recovery_service.common.logging import setup_logging
from recovery_service.core.models.task import RecoveryTask
from recovery_service.db.session import get_sync_session_factory
from recovery_service.orchestrator.pipeline import RecoveryPipeline
from recovery_service.services.task_events import record_task_event
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="recovery.run_task")
def run_recovery_task(self, task_id: str, volume_group_index: int = 0) -> dict:
    setup_logging()
    session = get_sync_session_factory()()
    task = None
    try:
        task_uuid = uuid.UUID(task_id)
        claimed = session.execute(
            update(RecoveryTask)
            .where(RecoveryTask.id == task_uuid, RecoveryTask.state == "created")
            .values(
                state="policy_running",
                error_message=None,
                finished_at=None,
                updated_at=app_now(),
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
        task = session.get(RecoveryTask, task_uuid)
        if not task:
            return {"error": "task not found"}
        if claimed.rowcount != 1:
            message = f"Duplicate recovery delivery ignored because task is already {task.state}."
            record_task_event(
                task.id,
                event_type="duplicate_delivery_ignored",
                title="重复任务消息已忽略",
                status="warning",
                message=message,
                payload={
                    "celery_task_id": str(getattr(self.request, "id", "") or ""),
                    "task_state": task.state,
                    "volume_group_index": volume_group_index,
                },
            )
            return {
                "state": task.state,
                "success": task.state in {"succeeded", "succeeded_with_warnings"},
                "message": message,
                "duplicate_ignored": True,
            }

        record_task_event(
            task.id,
            event_type="task",
            title="任务开始",
            status="running",
            message="Worker 已开始执行恢复任务。",
            payload={
                "remote_host": task.remote_host,
                "remote_directory": task.remote_directory,
                "target_connection": task.target_connection,
            },
        )

        options = dict(task.options or {})
        options["_task_id"] = str(task.id)
        pipeline = RecoveryPipeline()
        result = pipeline.run_task(
            remote_host=task.remote_host,
            remote_port=task.remote_port,
            remote_user=task.remote_user,
            remote_password=task.remote_password_enc,
            remote_directory=task.remote_directory,
            target_connection=task.target_connection,
            target_admin_user=task.target_admin_user,
            target_admin_password=task.target_admin_password_enc,
            options=options,
            volume_group_index=volume_group_index,
        )

        session.refresh(task)
        runtime_metadata = dict(task.metadata_snapshot or {})
        runtime_metadata.update(result.get("metadata") or {})
        task.metadata_snapshot = runtime_metadata
        if task.stop_requested or result.get("state") == "cancelled":
            task.state = "cancelled"
            task.error_message = task.stop_reason or result.get("message") or "Oracle 导入已由用户停止。"
            task.stopped_at = app_now()
        else:
            task.state = result["state"]
            task.error_message = None if result["success"] else result.get("message")
        task.progress_percent = 100.0 if result["success"] else task.progress_percent
        task.correction_attempts = result.get("correction_attempts", 0)
        if task.state in ("succeeded", "succeeded_with_warnings", "failed", "cancelled"):
            task.finished_at = app_now()
        session.commit()
        record_task_event(
            task.id,
            event_type="task",
            title="任务结束",
            status="cancelled" if task.state == "cancelled" else ("succeeded" if result["success"] else "failed"),
            message=result.get("message"),
            payload={"state": task.state, "metadata": task.metadata_snapshot},
        )
        return result
    except Exception as e:
        if task:
            try:
                session.refresh(task)
            except Exception:
                session.rollback()
                task = session.get(RecoveryTask, uuid.UUID(task_id))
            stopped = bool(task and task.stop_requested)
            task.state = "cancelled" if stopped else "failed"
            task.error_message = task.stop_reason if stopped else str(e)
            if stopped:
                task.stopped_at = app_now()
            task.finished_at = app_now()
            session.commit()
            record_task_event(
                task.id,
                event_type="task",
                title="任务已按请求停止" if stopped else "任务异常结束",
                status="cancelled" if stopped else "failed",
                message=task.stop_reason if stopped else str(e),
            )
            if stopped:
                return {"state": "cancelled", "success": False, "message": task.stop_reason or "Oracle 导入已停止。"}
        raise
    finally:
        session.close()
