from recovery_service.services.openmetadata import run_outbox_item
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="openmetadata.sync_outbox", max_retries=5)
def run_openmetadata_outbox(self, outbox_id: str) -> dict:
    result = run_outbox_item(outbox_id)
    if result.get("retry"):
        raise self.retry(countdown=60, max_retries=5)
    return result
