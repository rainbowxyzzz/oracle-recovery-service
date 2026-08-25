from recovery_service.services.query_export import run_query_export
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="query_export.run")
def run_query_export_task(self, job_id: str):
    return run_query_export(job_id)
