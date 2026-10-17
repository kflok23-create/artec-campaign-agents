"""Phase 0 smoke check: one locked-down Agent SDK call from the worker, logged with its cost.

It exercises the same path every real agent will use (budget hold, `agent_runs` row, one
in-process tool, JSON-schema output, ledger settlement). It runs once per deployment: a restart
of the same deployment finds the earlier `ok` row and skips, so restarts never repeat paid work.
"""

from __future__ import annotations

import logging

from claude_agent_sdk import query as sdk_query
from claude_agent_sdk import tool
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agents.runner import AgentSpec, QueryFn, RunContext, run_agent
from db.models import AgentRun
from services.config import BrandConfig, Settings
from services.logs import ctx

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the health check of a marketing system. Follow the user's steps exactly. "
    "Do not add anything else."
)
PROMPT = (
    "1. Call the get_brand_name tool.\n"
    "2. Return ok=true, brand_name set to exactly what the tool returned, "
    "and word_count set to the number of words in that name."
)


class SmokeOutput(BaseModel):
    ok: bool
    brand_name: str = Field(max_length=80)
    word_count: int = Field(ge=0, le=20)


def smoke_spec(settings: Settings, brand: BrandConfig) -> AgentSpec[SmokeOutput]:
    @tool("get_brand_name", "Return the display name of the brand this deployment serves.", {})
    async def get_brand_name(args: dict) -> dict:
        return {"content": [{"type": "text", "text": brand.name}]}

    return AgentSpec(
        name="smoke",
        model=settings.smoke_model,
        system_prompt=SYSTEM_PROMPT,
        output_model=SmokeOutput,
        tools=[get_brand_name],
        max_turns=4,
        ceiling_usd=0.05,
    )


def smoke_run_id(settings: Settings) -> str:
    return f"smoke:{settings.deployment_key}"


async def run_smoke(
    settings: Settings,
    brand: BrandConfig,
    sessionmaker: async_sessionmaker,
    *,
    force: bool = False,
    query_fn: QueryFn = sdk_query,
) -> str:
    """Run the smoke call unless this deployment already passed it. Returns ok/skipped/busy."""
    run_id = smoke_run_id(settings)
    async with sessionmaker() as lock_session:
        # Session-level lock, held for the whole call, so two replicas cannot both run it.
        got_lock = await lock_session.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": run_id}
        )
        if not got_lock:
            log.info("smoke check already running elsewhere", extra=ctx(run_id=run_id))
            return "busy"
        try:
            passed = await lock_session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.run_id == run_id, AgentRun.status == "ok")
            )
            await lock_session.commit()
            if passed and not force:
                log.info(
                    "smoke check already passed for this deployment; skipping",
                    extra=ctx(run_id=run_id),
                )
                return "skipped"

            rc = RunContext(
                settings=settings, brand=brand, sessionmaker=sessionmaker, run_id=run_id
            )
            result = await run_agent(smoke_spec(settings, brand), PROMPT, rc, query_fn=query_fn)
            matched = result.output.brand_name == brand.name
            log.info(
                "smoke check passed" if matched else "smoke check returned the wrong brand name",
                extra=ctx(
                    run_id=run_id,
                    agent_run_ids=result.agent_run_ids,
                    cost_usd=str(result.cost_usd),
                    output=result.output.model_dump(),
                ),
            )
            return "ok"
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": run_id}
            )
            await lock_session.commit()
