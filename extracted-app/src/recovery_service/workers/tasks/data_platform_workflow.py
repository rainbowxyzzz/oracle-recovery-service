import uuid

from recovery_service.common.logging import setup_logging
from recovery_service.services.data_platform import run_queued_workflow
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="data_platform.workflow_run")
def run_data_platform_workflow(self, run_id: str) -> dict:
    setup_logging()
    run_queued_workflow(uuid.UUID(run_id))
    return {"run_id": run_id}
