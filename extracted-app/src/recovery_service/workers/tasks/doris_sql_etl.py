from recovery_service.services.doris_sql_etl import run_doris_sql_etl
from recovery_service.workers.celery_app import celery_app


@celery_app.task(bind=True, name="doris.sql_etl_run")
def run_doris_sql_etl_task(self, run_id: str):
    return run_doris_sql_etl(run_id)

