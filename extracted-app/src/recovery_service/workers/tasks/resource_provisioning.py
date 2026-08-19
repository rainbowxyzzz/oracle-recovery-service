import uuid

from recovery_service.common.logging import setup_logging
from recovery_service.services.resource_permissions import run_permission_batch
from recovery_service.services.resource_provisioning import run_batch
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="resource_provisioning.run_batch")
def run_resource_provisioning_batch(self, batch_id: str) -> dict:
    setup_logging()
    return run_batch(uuid.UUID(batch_id))


@celery_app.task(bind=True, name="resource_provisioning.run_permission_batch")
def run_resource_permission_batch(self, batch_id: str) -> dict:
    setup_logging()
    return run_permission_batch(uuid.UUID(batch_id))
