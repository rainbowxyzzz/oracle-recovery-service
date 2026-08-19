"""Staging backend — local path preparation for impdp DIRECTORY usage."""

import os
from pathlib import Path

from recovery_service.settings import get_settings


class LocalStagingBackend:
    def __init__(self, base_dir: str | None = None):
        settings = get_settings()
        self.base_dir = Path(base_dir or settings.staging_dir)

    def task_staging_path(self, task_id: str) -> Path:
        path = self.base_dir / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_oracle_directory_hint(self, task_id: str) -> str:
        """Return DIRECTORY object name; actual Oracle DIRECTORY DDL is environment-specific."""
        return os.environ.get("ORACLE_DATA_PUMP_DIR", "DATA_PUMP_DIR")
