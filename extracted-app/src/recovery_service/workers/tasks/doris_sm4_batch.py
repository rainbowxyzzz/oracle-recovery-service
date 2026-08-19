import uuid

from recovery_service.common.logging import setup_logging
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="doris.sm4_batch")
def run_doris_sm4_batch(self, batch_id: str) -> dict:
    setup_logging()
    from recovery_service.services.doris_encryption import run_sm4_batch_job

    return run_sm4_batch_job(uuid.UUID(batch_id))
