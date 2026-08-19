import uuid

from recovery_service.common.logging import setup_logging
from recovery_service.services.data_platform import run_queued_component_task
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="data_platform.component_task_run")
def run_data_platform_component_task(self, component_run_id: str) -> dict:
    setup_logging()
    return run_queued_component_task(uuid.UUID(component_run_id))
