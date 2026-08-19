from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from recovery_service.api.v1.router import api_router
from recovery_service.common.logging import setup_logging
from recovery_service.db.session import init_db
from recovery_service.services.batch_authorization import (
    start_batch_authorization_scheduler,
    stop_batch_authorization_scheduler,
)
from recovery_service.services.approval_authorization import (
    start_approval_authorization_scheduler,
    stop_approval_authorization_scheduler,
)
from recovery_service.services.data_platform import start_data_platform_scheduler, stop_data_platform_scheduler
from recovery_service.services.doris_encryption import start_sm4_scheduler, stop_sm4_scheduler
from recovery_service.settings import PROJECT_ROOT, get_settings


def _service_mode() -> str:
    return str(get_settings().app_service_mode or "monolith").strip().lower()


def _service_enabled(*modes: str) -> bool:
    mode = _service_mode()
    return mode in {"", "monolith", "all", "gateway"} or mode in set(modes)


def _data_platform_scheduler_enabled() -> bool:
    return _service_mode() in {"", "monolith", "all", "data-platform"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await init_db()
    sm4_scheduler_started = False
    data_platform_scheduler_started = False
    batch_authorization_scheduler_started = False
    approval_authorization_scheduler_started = False
    if _service_enabled("sm4"):
        start_sm4_scheduler()
        sm4_scheduler_started = True
    if _data_platform_scheduler_enabled():
        start_data_platform_scheduler()
        data_platform_scheduler_started = True
    if _service_enabled("batch-auth"):
        start_batch_authorization_scheduler()
        batch_authorization_scheduler_started = True
        start_approval_authorization_scheduler()
        approval_authorization_scheduler_started = True
    try:
        yield
    finally:
        if approval_authorization_scheduler_started:
            stop_approval_authorization_scheduler()
        if batch_authorization_scheduler_started:
            stop_batch_authorization_scheduler()
        if data_platform_scheduler_started:
            stop_data_platform_scheduler()
        if sm4_scheduler_started:
            stop_sm4_scheduler()


app = FastAPI(
    title="Oracle Recovery Service",
    version="0.1.0",
    description="Distributed intelligent Oracle DMP recovery API",
    lifespan=lifespan,
    docs_url=None,
)
app.mount(
    "/static",
    StaticFiles(directory=PROJECT_ROOT / "src" / "recovery_service" / "static"),
    name="static",
)
app.include_router(api_router)


@app.get("/")
async def root():
    return FileResponse(
        PROJECT_ROOT / "src" / "recovery_service" / "static" / "ui.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/ui", include_in_schema=False)
async def recovery_ui():
    return FileResponse(
        PROJECT_ROOT / "src" / "recovery_service" / "static" / "ui.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
    )
