import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import APIRouter

from recovery_service.api.v1 import router as api_router_module
from recovery_service import main as main_module
from recovery_service.workers.celery_app import celery_app


def _paths_for_mode(mode: str) -> set[str]:
    router = APIRouter(prefix="/api/v1")
    for child_router in api_router_module._routers_for_mode(mode):
        router.include_router(child_router)
    return {route.path for route in router.routes}


class MicroserviceModeTests(unittest.TestCase):
    def test_monolith_mode_keeps_legacy_business_routes(self) -> None:
        paths = _paths_for_mode("monolith")

        self.assertIn("/api/v1/tasks", paths)
        self.assertIn("/api/v1/data-platform/nodes", paths)
        self.assertIn("/api/v1/doris-encryption/batches", paths)
        self.assertIn("/api/v1/doris-sm3/tasks", paths)
        self.assertIn("/api/v1/resource-provisioning/batches", paths)
        self.assertIn("/api/v1/api-orchestration/connectors", paths)

    def test_oracle_restore_mode_exposes_restore_only_business_routes(self) -> None:
        paths = _paths_for_mode("oracle-restore")

        self.assertIn("/api/v1/tasks", paths)
        self.assertIn("/api/v1/batches", paths)
        self.assertNotIn("/api/v1/data-platform/nodes", paths)
        self.assertNotIn("/api/v1/doris-encryption/batches", paths)

    def test_data_sync_mode_exposes_data_platform_routes_without_sm4(self) -> None:
        paths = _paths_for_mode("data-sync")

        self.assertIn("/api/v1/data-platform/nodes", paths)
        self.assertIn("/api/v1/data-platform/data-sync/recognize", paths)
        self.assertNotIn("/api/v1/data-platform/workflows", paths)
        self.assertNotIn("/api/v1/data-platform/schedules", paths)
        self.assertNotIn("/api/v1/tasks", paths)
        self.assertNotIn("/api/v1/doris-encryption/batches", paths)

    def test_sm4_mode_exposes_sm4_without_oracle_restore(self) -> None:
        paths = _paths_for_mode("sm4")

        self.assertIn("/api/v1/doris-encryption/batches", paths)
        self.assertNotIn("/api/v1/tasks", paths)
        self.assertNotIn("/api/v1/data-platform/nodes", paths)

    def test_celery_tasks_are_routed_to_business_queues(self) -> None:
        routes = celery_app.conf.task_routes

        self.assertEqual(routes["recovery.run_task"]["queue"], "oracle_restore")
        self.assertEqual(routes["doris.sm3_mapping"]["queue"], "doris_sm3")
        self.assertEqual(routes["doris.sm4_batch"]["queue"], "doris_sm4")
        self.assertEqual(routes["doris.sql_etl_run"]["queue"], "doris_sql")
        self.assertEqual(routes["data_platform.component_task_run"]["queue"], "data_sync")
        self.assertEqual(routes["data_platform.workflow_run"]["queue"], "data_platform")
        self.assertEqual(routes["resource_provisioning.run_batch"]["queue"], "resource_provisioning")
        self.assertEqual(routes["api_orchestration.run"]["queue"], "api_orchestration")

    def test_resource_provisioning_mode_exposes_only_its_business_routes(self) -> None:
        paths = _paths_for_mode("resource-provisioning")

        self.assertIn("/api/v1/resource-provisioning/batches", paths)
        self.assertNotIn("/api/v1/tasks", paths)
        self.assertNotIn("/api/v1/data-platform/nodes", paths)

    def test_api_orchestration_mode_exposes_only_its_business_routes(self) -> None:
        paths = _paths_for_mode("api-orchestration")

        self.assertIn("/api/v1/api-orchestration/connectors", paths)
        self.assertIn("/api/v1/api-orchestration/sql/{slug}/invoke", paths)
        self.assertNotIn("/api/v1/tasks", paths)
        self.assertNotIn("/api/v1/resource-provisioning/batches", paths)

    def test_worker_entrypoint_contains_business_mode_queue_mapping(self) -> None:
        script = Path("scripts/worker-entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn("oracle-restore|oracle)", script)
        self.assertIn("data-sync)", script)
        self.assertIn("data-platform)", script)
        self.assertIn("CELERY_DATA_SYNC_QUEUE", script)
        self.assertIn("CELERY_DATA_PLATFORM_QUEUE", script)
        self.assertIn("resource-provisioning)", script)
        self.assertIn("CELERY_RESOURCE_PROVISIONING_QUEUE", script)
        self.assertIn("api-orchestration)", script)
        self.assertIn("CELERY_API_ORCHESTRATION_QUEUE", script)
        self.assertIn('WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-2}"', script)
        self.assertNotIn('WORKER_CONCURRENCY="${SM3_WORKER_CONCURRENCY', script)

    def test_data_platform_scheduler_only_runs_in_owner_modes(self) -> None:
        with patch.object(main_module, "_service_mode", return_value="data-platform"):
            self.assertTrue(main_module._data_platform_scheduler_enabled())
        with patch.object(main_module, "_service_mode", return_value="monolith"):
            self.assertTrue(main_module._data_platform_scheduler_enabled())
        for mode in ("gateway", "data-sync", "doris-sql"):
            with patch.object(main_module, "_service_mode", return_value=mode):
                self.assertFalse(main_module._data_platform_scheduler_enabled())

    def test_sm3_queue_status_reads_the_sm3_business_queue(self) -> None:
        service = Path("src/recovery_service/services/doris_sm3_mapping.py").read_text(encoding="utf-8")

        self.assertIn("queue_name = settings.celery_sm3_queue", service)
        self.assertNotIn("queue_name=settings.celery_default_queue", service)


if __name__ == "__main__":
    unittest.main()
