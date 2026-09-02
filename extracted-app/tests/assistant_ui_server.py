"""Local-only UI fixture; all business calls and files are simulated, DB is in memory."""
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from test_harness_assistant import env, ACTOR
from recovery_service.api.deps import get_current_actor
from recovery_service.api.v1.harness_assistant import router


if __name__ == "__main__":
    patch = pytest.MonkeyPatch()
    fixture = env.__wrapped__(patch)
    next(fixture)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_actor] = lambda: ACTOR
    app.mount("/static", StaticFiles(directory=Path(__file__).parents[1] / "src/recovery_service/static"))
    try:
        uvicorn.run(app, host="127.0.0.1", port=18098, log_level="warning")
    finally:
        fixture.close()
        patch.undo()
