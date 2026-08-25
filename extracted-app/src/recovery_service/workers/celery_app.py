from celery import Celery

from recovery_service.settings import get_settings

settings = get_settings()
visibility_timeout = max(
    settings.celery_visibility_timeout_seconds,
    settings.oracle_import_operation_timeout_seconds + 3600,
)

celery_app = Celery(
    "recovery_service",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "recovery_service.workers.tasks.run_recovery",
        "recovery_service.workers.tasks.doris_sm3_mapping",
        "recovery_service.workers.tasks.doris_sm4_batch",
        "recovery_service.workers.tasks.doris_sql_etl",
        "recovery_service.workers.tasks.query_export",
        "recovery_service.workers.tasks.data_platform_component",
        "recovery_service.workers.tasks.data_platform_workflow",
        "recovery_service.workers.tasks.resource_provisioning",
        "recovery_service.workers.tasks.api_orchestration",
    ],
)

task_routes = {
    "recovery.run_task": {"queue": settings.celery_oracle_queue},
    "doris.sm3_mapping": {"queue": settings.celery_sm3_queue},
    "doris.sm4_batch": {"queue": settings.celery_sm4_queue},
    "doris.sql_etl_run": {"queue": settings.celery_sql_queue},
    "query_export.run": {"queue": settings.celery_data_export_queue},
    "data_platform.component_task_run": {"queue": settings.celery_data_sync_queue},
    "data_platform.workflow_run": {"queue": settings.celery_data_platform_queue},
    "resource_provisioning.run_batch": {"queue": settings.celery_resource_provisioning_queue},
    "resource_provisioning.run_permission_batch": {"queue": settings.celery_resource_provisioning_queue},
    "api_orchestration.run": {"queue": settings.celery_api_orchestration_queue},
}

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.app_timezone,
    task_default_queue=settings.celery_default_queue,
    task_routes=task_routes,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
    task_time_limit=settings.oracle_import_operation_timeout_seconds + 600,
    task_soft_time_limit=settings.oracle_import_operation_timeout_seconds,
    broker_transport_options={"visibility_timeout": visibility_timeout},
    result_backend_transport_options={"visibility_timeout": visibility_timeout},
    visibility_timeout=visibility_timeout,
)
