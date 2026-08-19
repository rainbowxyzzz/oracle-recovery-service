import uuid

from recovery_service.common.logging import setup_logging
from recovery_service.db.session import get_sync_session_factory
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="doris.sm3_mapping")
def run_doris_sm3_job(self, job_id: str) -> dict:
    setup_logging()
    session = get_sync_session_factory()()
    try:
        from recovery_service.services.doris_sm3_mapping import run_sm3_mapping_job

        return run_sm3_mapping_job(session, uuid.UUID(job_id))
    finally:
        session.close()
