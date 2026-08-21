from fastapi import APIRouter

from recovery_service.api.v1 import (
    api_keys,
    api_orchestration,
    approval_authorization,
    auth,
    batches,
    batch_authorization,
    data_platform,
    data_automation,
    database_cleanup,
    database_connections,
    doris_csv_import,
    doris_encryption,
    doris_sql_etl,
    doris_sm3_decrypt,
    doris_sm3_mapping,
    health,
    resource_provisioning,
    setup,
    tasks,
    users,
)
from recovery_service.settings import get_settings

api_router = APIRouter(prefix="/api/v1")


def _router_subset(source: APIRouter, predicate) -> APIRouter:
    subset = APIRouter()
    subset.routes.extend(route for route in source.routes if predicate(route.path))
    return subset


def _is_data_platform_component_route(path: str) -> bool:
    return (
        path.startswith("/data-platform/metadata/")
        or path == "/data-platform/data-sync/recognize"
        or path
        in {
            "/data-platform/nodes",
            "/data-platform/nodes/{node_id}",
            "/data-platform/nodes/{node_id}/run",
            "/data-platform/nodes/{node_id}/component-runs",
        }
    )


_DATA_PLATFORM_COMPONENT_ROUTER = _router_subset(
    data_platform.router,
    _is_data_platform_component_route,
)

_COMMON_ROUTERS = (health.router, auth.router)
_MANAGEMENT_ROUTERS = (users.router, api_keys.router, setup.router, database_connections.router)
_ALL_BUSINESS_ROUTERS = (
    tasks.router,
    batches.router,
    batch_authorization.router,
    data_platform.router,
    data_automation.router,
    database_cleanup.router,
    doris_csv_import.router,
    doris_encryption.router,
    doris_sql_etl.router,
    doris_sm3_mapping.router,
    doris_sm3_decrypt.router,
    resource_provisioning.router,
    api_orchestration.router,
    approval_authorization.router,
)
_SERVICE_ROUTERS = {
    "gateway": _MANAGEMENT_ROUTERS + _ALL_BUSINESS_ROUTERS,
    "oracle-restore": _MANAGEMENT_ROUTERS + (tasks.router, batches.router),
    "data-sync": _MANAGEMENT_ROUTERS + (_DATA_PLATFORM_COMPONENT_ROUTER,),
    "data-platform": _MANAGEMENT_ROUTERS + (data_platform.router, data_automation.router),
    "sm4": _MANAGEMENT_ROUTERS + (doris_encryption.router,),
    "sm3": _MANAGEMENT_ROUTERS + (doris_sm3_mapping.router, doris_sm3_decrypt.router),
    "doris-sql": _MANAGEMENT_ROUTERS + (doris_sql_etl.router, _DATA_PLATFORM_COMPONENT_ROUTER),
    "batch-auth": _MANAGEMENT_ROUTERS + (batch_authorization.router, approval_authorization.router),
    "doris-csv": _MANAGEMENT_ROUTERS + (doris_csv_import.router,),
    "cleanup": _MANAGEMENT_ROUTERS + (database_cleanup.router,),
    "resource-provisioning": _MANAGEMENT_ROUTERS + (resource_provisioning.router,),
    "api-orchestration": _MANAGEMENT_ROUTERS + (api_orchestration.router,),
}


def _service_mode() -> str:
    return str(get_settings().app_service_mode or "monolith").strip().lower()


def _routers_for_mode(mode: str):
    if mode in {"", "monolith", "all"}:
        return _COMMON_ROUTERS + _MANAGEMENT_ROUTERS + _ALL_BUSINESS_ROUTERS
    return _COMMON_ROUTERS + _SERVICE_ROUTERS.get(mode, _MANAGEMENT_ROUTERS)


for router in _routers_for_mode(_service_mode()):
    api_router.include_router(router)
