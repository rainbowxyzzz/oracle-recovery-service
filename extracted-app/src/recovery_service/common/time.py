from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from recovery_service.settings import get_settings


def app_now() -> datetime:
    """Return application local time as a naive datetime for MySQL DATETIME fields."""
    return datetime.now(_app_timezone()).replace(tzinfo=None)


def to_app_naive(value: datetime) -> datetime:
    """Normalize incoming datetimes to the configured application timezone."""
    if value.tzinfo is None:
        return value
    return value.astimezone(_app_timezone()).replace(tzinfo=None)


def _app_timezone() -> ZoneInfo:
    timezone_name = (get_settings().app_timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")
