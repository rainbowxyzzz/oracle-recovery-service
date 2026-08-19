from recovery_service.common.logging import setup_logging
from recovery_service.services.api_orchestration import execute_run
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="api_orchestration.run")
def run_api_orchestration(self, run_id: str) -> dict:
    setup_logging()
    execute_run(run_id)
    return {"run_id": run_id}
