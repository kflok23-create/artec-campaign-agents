"""The always-on worker process.

Phase 0 scope: start up, wait for the schema, run the smoke call once per deployment, then
heartbeat until told to stop. Phase 1 adds the task queue loop here.
"""

from __future__ import annotations

import logging
import signal
from decimal import Decimal

import anyio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from db.models import AgentRun
from db.session import make_engine, make_sessionmaker
from services import budget
from services.config import REPO_ROOT, BrandConfig, Settings, load_brand
from services.logs import ctx
from worker.smoke import run_smoke

log = logging.getLogger("worker")

SCHEMA_WAIT_SECONDS = 180


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
        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_signals, stop)
            if settings.worker_smoke_on_boot:
                try:
                    await run_smoke(settings, brand, sessionmaker)
                except Exception:
                    # Keep the worker up; the failure is on the agent_runs row and in this log.
                    log.exception("smoke check failed", extra=ctx(alert=True))
            beat = 0
            while not stop.is_set():
                beat += 1
                log.info("heartbeat", extra=ctx(beat=beat, deployment=settings.deployment_key))
                with anyio.move_on_after(settings.worker_heartbeat_seconds):
                    await stop.wait()
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
