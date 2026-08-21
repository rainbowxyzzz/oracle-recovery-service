from __future__ import annotations

import hashlib
import threading
import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import select, text

from recovery_service.core.models.task import DorisSm4BatchJob
from recovery_service.db.session import get_sync_session_factory


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_INFLIGHT_STATES = ("queued", "reserved", "running", "stopping")


def _database_lock_name(connection_id: uuid.UUID, database: str) -> str:
    identity = f"{connection_id}:{database.strip().casefold()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
    return f"ors:sm4:db:{digest}"


def _local_lock(name: str) -> threading.RLock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(name, threading.RLock())


@contextmanager
def _named_guard(name: str, *, timeout_seconds: int) -> Iterator[None]:
    session = get_sync_session_factory()()
    local_lock: threading.RLock | None = None
    mysql_lock_acquired = False
    try:
        if session.get_bind().dialect.name == "mysql":
            acquired = session.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": name, "timeout": max(0, timeout_seconds)},
            ).scalar_one()
            if acquired != 1:
                raise TimeoutError("SM4 运行屏障正被其他操作占用，请稍后重试。")
            mysql_lock_acquired = True
        else:
            local_lock = _local_lock(name)
            if not local_lock.acquire(timeout=max(0, timeout_seconds)):
                raise TimeoutError("SM4 运行屏障正被其他操作占用，请稍后重试。")
        yield
    finally:
        if mysql_lock_acquired:
            try:
                session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
            except Exception:
                pass
        if local_lock is not None:
            local_lock.release()
        session.close()


def sm4_database_guard(connection_id: uuid.UUID, database: str, *, timeout_seconds: int = 5) -> Iterator[None]:
    return _named_guard(_database_lock_name(connection_id, database), timeout_seconds=timeout_seconds)


def sm4_dispatch_guard(*, timeout_seconds: int = 0) -> Iterator[None]:
    return _named_guard("ors:sm4:dispatch", timeout_seconds=timeout_seconds)


def list_inflight_sm4_batches(connection_id: uuid.UUID, database: str) -> list[DorisSm4BatchJob]:
    session = get_sync_session_factory()()
    try:
        return list(
            session.execute(
                select(DorisSm4BatchJob)
                .where(DorisSm4BatchJob.connection_id == connection_id)
                .where(DorisSm4BatchJob.database == database)
                .where(DorisSm4BatchJob.state.in_(_INFLIGHT_STATES))
                .order_by(DorisSm4BatchJob.created_at)
            ).scalars().all()
        )
    finally:
        session.close()


def assert_sm4_key_rotation_allowed(connection_id: uuid.UUID, database: str) -> None:
    jobs = list_inflight_sm4_batches(connection_id, database)
    if not jobs:
        return
    details = ", ".join(f"{job.id}({job.state})" for job in jobs[:5])
    if len(jobs) > 5:
        details += f" 等 {len(jobs)} 个任务"
    raise ValueError(
        f"数据库 {database} 存在排队或执行中的 SM4 批次：{details}。"
        "请等待任务完成或明确取消后再刷新密钥函数。"
    )
