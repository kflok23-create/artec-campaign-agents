"""The always-on worker process.

It starts up, waits for the schema, runs the smoke call once per deployment, then loops:
enqueue any periodic jobs that are due, claim the next task from the queue, run its handler.
Code decides what runs; handlers live in worker/tasks.py.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from datetime import UTC, datetime
from decimal import Decimal

import anyio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from db.models import AgentRun
from db.session import make_engine, make_sessionmaker
from services import budget
from services.brands import ensure_brand
from services.config import REPO_ROOT, BrandConfig, Settings, load_brand
from services.logs import ctx
from worker import queue, scheduler
from worker.smoke import run_smoke
from worker.tasks import HANDLERS, Handler, NonRetryable, WorkerContext

log = logging.getLogger("worker")

SCHEMA_WAIT_SECONDS = 180
LOOP_ERROR_PAUSE_SECONDS = 15


def schema_head() -> str:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    head = script.get_current_head()
    assert head is not None
    return head


async def wait_for_schema(engine: AsyncEngine, max_wait: float = SCHEMA_WAIT_SECONDS) -> None:
    """Block until the database schema is at this code's migration head."""
    head = schema_head()
    delay = 1.0
    with anyio.fail_after(max_wait):
        while True:
            try:
                async with engine.connect() as conn:
                    current = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                if current == head:
                    return
                log.info("waiting for migrations", extra=ctx(current=current, head=head))
            except Exception as e:  # noqa: BLE001 - DB not up yet or table missing
                log.info("waiting for database", extra=ctx(error=type(e).__name__))
            await anyio.sleep(delay)
            delay = min(delay * 2, 15)


async def _watch_signals(stop: anyio.Event) -> None:
    try:
        with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
            async for signum in signals:
                log.info("shutdown signal received", extra=ctx(signal=signal.Signals(signum).name))
                stop.set()
                return
    except NotImplementedError:
        # Windows: no signal receiver; Ctrl+C raises KeyboardInterrupt instead.
        await anyio.sleep_forever()


def _startup_fields(settings: Settings, brand: BrandConfig) -> dict:
    return {
        "deployment": settings.deployment_key,
        "commit": (settings.railway_git_commit_sha or "local")[:7],
        "brand": brand.brand,
        "brand_config_hash": brand.config_hash[:12],
        "brand_unconfirmed": list(brand.unconfirmed),
        "publishing_enabled": settings.publishing_enabled,
    }


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _heartbeat(settings: Settings, stop: anyio.Event) -> None:
    beat = 0
    while not stop.is_set():
        beat += 1
        log.info("heartbeat", extra=ctx(beat=beat, deployment=settings.deployment_key))
        with anyio.move_on_after(settings.worker_heartbeat_seconds):
            await stop.wait()


async def _renew_lease(
    wctx: WorkerContext, task: queue.ClaimedTask, lease_seconds: float, lost: anyio.CancelScope
) -> None:
    while True:
        await anyio.sleep(lease_seconds / 3)
        if not await queue.extend_lease(wctx.sessionmaker, task, lease_seconds):
            # Another worker took the task over; stop working on it.
            log.warning("lease lost; abandoning task", extra=ctx(task_id=task.id))
            lost.cancel()
            return


async def _cancel_on_shutdown(stop: anyio.Event, scope: anyio.CancelScope, grace: float) -> None:
    await stop.wait()
    log.info("shutdown: letting the running task finish", extra=ctx(grace_seconds=grace))
    await anyio.sleep(grace)
    scope.cancel()


async def run_task(
    task: queue.ClaimedTask,
    wctx: WorkerContext,
    stop: anyio.Event,
    *,
    handlers: dict[str, Handler] = HANDLERS,
    lease_seconds: float = queue.LEASE_SECONDS,
) -> str:
    """Run one claimed task's handler and record the outcome. Returns the task's new status."""
    fields = {"task_id": task.id, "kind": task.kind, "attempt": task.attempts}
    log.info("task started", extra=ctx(**fields))
    started = time.monotonic()
    result = None
    error: BaseException | None = None
    returned = False  # the handler returned or raised, as opposed to being cancelled
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_renew_lease, wctx, task, lease_seconds, scope)
            tg.start_soon(
                _cancel_on_shutdown, stop, scope, wctx.settings.worker_shutdown_grace_seconds
            )
            try:
                result = await handlers[task.kind](task, wctx)
            except Exception as e:  # noqa: BLE001 - every failure is recorded on the task
                error = e
            returned = True
            tg.cancel_scope.cancel()
    duration_ms = int((time.monotonic() - started) * 1000)

    with anyio.CancelScope(shield=True):  # always record the outcome, even while stopping
        if not returned:
            status = "queued"
            await queue.defer(
                wctx.sessionmaker, task, datetime.now(UTC), "interrupted (shutdown or lease lost)"
            )
        elif error is None:
            status = "done"
            await queue.finish(wctx.sessionmaker, task, result)
        elif isinstance(error, budget.BudgetExceeded):
            status = "queued"
            resume = budget.next_local_day_start(wctx.brand)
            await queue.defer(wctx.sessionmaker, task, resume, f"budget cap: {error}")
            log.error(
                "budget cap reached; task paused until the next budget day",
                extra=ctx(**fields, resume_at=resume.isoformat(), alert=True),
            )
        else:
            message = f"{type(error).__name__}: {error}"
            status = await queue.fail(
                wctx.sessionmaker, task, message, retry=not isinstance(error, NonRetryable)
            )
            if status == "failed":
                log.error("task error", exc_info=error, extra=ctx(**fields))
    log.info("task finished", extra=ctx(**fields, status=status, duration_ms=duration_ms))
    return status


async def _work(wctx: WorkerContext, stop: anyio.Event, me: str) -> None:
    jobs = scheduler.periodic_jobs(wctx.settings, wctx.brand)
    missing = scheduler.missing_prerequisites(wctx.settings, wctx.brand)
    if missing:
        log.warning("asset indexing is off until these are set", extra=ctx(missing=missing))
    while not stop.is_set():
        try:
            if jobs:
                await scheduler.enqueue_due(wctx.sessionmaker, wctx.brand, jobs)
            task = await queue.claim(wctx.sessionmaker, worker_id=me, kinds=list(HANDLERS))
        except Exception:
            # Usually the database restarting. Keep the worker up and try again shortly.
            log.exception("queue loop error")
            with anyio.move_on_after(LOOP_ERROR_PAUSE_SECONDS):
                await stop.wait()
            continue
        if task is not None:
            await run_task(task, wctx, stop)
            continue
        with anyio.move_on_after(wctx.settings.worker_poll_seconds):
            await stop.wait()


async def serve(settings: Settings) -> None:
    brand = load_brand(settings.brand)
    log.info("worker starting", extra=ctx(**_startup_fields(settings, brand)))
    if settings.publishing_enabled:
        log.warning("PUBLISHING_ENABLED is true, but no publisher exists yet (Phase 4)")

    engine = make_engine(settings.database_url.get_secret_value())
    sessionmaker = make_sessionmaker(engine)
    stop = anyio.Event()
    try:
        await wait_for_schema(engine)
        async with sessionmaker() as session, session.begin():
            brand_id = await ensure_brand(session, brand)
        wctx = WorkerContext(settings, brand, brand_id, sessionmaker)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_signals, stop)
            tg.start_soon(_heartbeat, settings, stop)
            if settings.worker_smoke_on_boot:
                try:
                    await run_smoke(settings, brand, sessionmaker)
                except Exception:
                    # Keep the worker up; the failure is on the agent_runs row and in this log.
                    log.exception("smoke check failed", extra=ctx(alert=True))
            await _work(wctx, stop, worker_id())
            tg.cancel_scope.cancel()
    finally:
        await engine.dispose()
    log.info("worker stopped cleanly", extra=ctx(deployment=settings.deployment_key))


async def smoke_once(settings: Settings, force: bool) -> str:
    brand = load_brand(settings.brand)
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        await wait_for_schema(engine, max_wait=30)
        return await run_smoke(settings, brand, make_sessionmaker(engine), force=force)
    finally:
        await engine.dispose()


async def status(settings: Settings, limit: int = 10) -> str:
    """Plain-text report of recent agent runs and today's Claude spend, for the gate."""
    brand = load_brand(settings.brand)
    engine = make_engine(settings.database_url.get_secret_value())
    sessionmaker: async_sessionmaker = make_sessionmaker(engine)
    try:
        async with sessionmaker() as session:
            rows = (
                await session.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(limit))
            ).all()
            spent = await budget.spent_since(
                session,
                brand=brand.brand,
                provider=budget.ANTHROPIC,
                since=budget.local_day_start(brand),
            )
    finally:
        await engine.dispose()
    cap = Decimal(str(brand.claude_daily_cap_usd()))
    lines = [
        f"Claude spend today ({brand.timezone}): ${spent:.6f} of ${cap:.2f} cap",
        "",
        f"{'id':>5}  {'created (UTC)':19}  {'agent':10} {'model':18} {'status':14} "
        f"{'in':>6} {'out':>6} {'cost_usd':>10}  run_id",
    ]
    for r in rows:
        lines.append(
            f"{r.id:>5}  {r.created_at:%Y-%m-%d %H:%M:%S}  {r.agent:10} {r.model:18} "
            f"{r.status:14} {r.input_tokens:>6} {r.output_tokens:>6} {r.cost_usd:>10.6f}  "
            f"{r.run_id}"
        )
    return "\n".join(lines)
