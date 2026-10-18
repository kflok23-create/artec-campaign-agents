"""The Postgres work queue: enqueue, claim with SKIP LOCKED, finish, retry with backoff.

A task moves queued -> running -> done. On an error it goes back to queued after an
exponential backoff, and the third error makes it failed (with an alert). A running task holds
a lease (`locked_until`): if its worker dies, the lease runs out and another claim picks the
task up again. Every update checks `locked_by` and `attempts`, so a worker whose lease was
taken over cannot overwrite the new owner's result.

All times are the database's clock (`now()`), so workers never disagree about leases.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Task
from services.logs import ctx

log = logging.getLogger(__name__)

LEASE_SECONDS = 600
BACKOFF_BASE_SECONDS = 60  # 1st retry after 1 minute, 2nd after 2 minutes
MAX_ERROR_CHARS = 2000


@dataclass(frozen=True)
class ClaimedTask:
    id: int
    brand: str
    kind: str
    ref_id: int | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    idempotency_key: str
    locked_by: str


def backoff_seconds(attempts: int) -> int:
    return BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0)


def _seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


async def enqueue(
    session: AsyncSession,
    *,
    brand: str,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    ref_id: int | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> int | None:
    """Add a task unless one with this key exists. Returns the new id, or None if it existed."""
    values: dict[str, Any] = {
        "brand": brand,
        "kind": kind,
        "idempotency_key": idempotency_key,
        "payload": payload or {},
        "ref_id": ref_id,
        "max_attempts": max_attempts,
    }
    if run_after is not None:
        values["run_after"] = run_after
    stmt = (
        pg_insert(Task)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Task.id)
    )
    return await session.scalar(stmt)


async def claim(
    sessionmaker: async_sessionmaker,
    *,
    worker_id: str,
    kinds: list[str] | None = None,
    lease_seconds: float = LEASE_SECONDS,
) -> ClaimedTask | None:
    """Claim the next ready task, or reclaim one whose lease ran out. None if nothing is ready."""
    while True:
        async with sessionmaker() as session, session.begin():
            ready = or_(
                and_(Task.status == "queued", Task.run_after <= func.now()),
                and_(Task.status == "running", Task.locked_until < func.now()),
            )
            query = select(Task).where(ready)
            if kinds is not None:
                query = query.where(Task.kind.in_(kinds))
            task = await session.scalar(
                query.order_by(Task.run_after, Task.id).limit(1).with_for_update(skip_locked=True)
            )
            if task is None:
                return None
            if task.status == "running" and task.attempts >= task.max_attempts:
                # Its worker died on the last allowed attempt: give up rather than loop forever.
                task.status = "failed"
                task.finished_at = func.now()
                task.locked_until = None
                task.last_error = f"lease expired on attempt {task.attempts}; worker stopped"
                log.error(
                    "task failed",
                    extra=ctx(task_id=task.id, kind=task.kind, error=task.last_error, alert=True),
                )
                continue
            if task.status == "running":
                log.warning(
                    "reclaiming task after its lease expired",
                    extra=ctx(task_id=task.id, kind=task.kind, previous_owner=task.locked_by),
                )
            task.status = "running"
            task.attempts += 1
            task.locked_by = worker_id
            task.locked_until = func.now() + _seconds(lease_seconds)
            await session.flush()
            return ClaimedTask(
                id=task.id,
                brand=task.brand,
                kind=task.kind,
                ref_id=task.ref_id,
                payload=dict(task.payload or {}),
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                idempotency_key=task.idempotency_key,
                locked_by=worker_id,
            )


def _owned(task: ClaimedTask):
    return and_(
        Task.id == task.id,
        Task.status == "running",
        Task.locked_by == task.locked_by,
        Task.attempts == task.attempts,
    )


async def _update(sessionmaker: async_sessionmaker, task: ClaimedTask, **values: Any) -> bool:
    async with sessionmaker() as session, session.begin():
        result = await session.execute(update(Task).where(_owned(task)).values(**values))
    if result.rowcount == 0:
        log.warning("task no longer owned; update skipped", extra=ctx(task_id=task.id))
    return result.rowcount == 1


async def extend_lease(
    sessionmaker: async_sessionmaker, task: ClaimedTask, lease_seconds: float = LEASE_SECONDS
) -> bool:
    return await _update(sessionmaker, task, locked_until=func.now() + _seconds(lease_seconds))


async def finish(
    sessionmaker: async_sessionmaker, task: ClaimedTask, result: dict[str, Any] | None = None
) -> bool:
    return await _update(
        sessionmaker,
        task,
        status="done",
        result=result,
        finished_at=func.now(),
        locked_until=None,
        last_error=None,
    )


async def fail(
    sessionmaker: async_sessionmaker, task: ClaimedTask, error: str, *, retry: bool = True
) -> str:
    """Record an error. Retries after a backoff until max_attempts, then fails with an alert."""
    error = error[:MAX_ERROR_CHARS]
    if retry and task.attempts < task.max_attempts:
        delay = backoff_seconds(task.attempts)
        await _update(
            sessionmaker,
            task,
            status="queued",
            run_after=func.now() + _seconds(delay),
            locked_until=None,
            locked_by=None,
            last_error=error,
        )
        log.warning(
            "task error; will retry",
            extra=ctx(task_id=task.id, kind=task.kind, attempt=task.attempts, retry_in_s=delay),
        )
        return "queued"
    await _update(
        sessionmaker,
        task,
        status="failed",
        finished_at=func.now(),
        locked_until=None,
        last_error=error,
    )
    log.error(
        "task failed",
        extra=ctx(task_id=task.id, kind=task.kind, attempt=task.attempts, error=error, alert=True),
    )
    return "failed"


async def defer(
    sessionmaker: async_sessionmaker, task: ClaimedTask, until: datetime, reason: str
) -> bool:
    """Put a task back to run later without counting the attempt (budget cap, shutdown)."""
    return await _update(
        sessionmaker,
        task,
        status="queued",
        attempts=task.attempts - 1,
        run_after=until,
        locked_until=None,
        locked_by=None,
        last_error=reason[:MAX_ERROR_CHARS],
    )
