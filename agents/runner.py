"""Runs one agent call through the Claude Agent SDK, with every guardrail on the way.

For each call the runner:
1. holds the call's ceiling against the daily Claude cap (services.budget);
2. writes an `agent_runs` row as `running`;
3. runs one locked-down `query()`: no built-in tools, no filesystem settings, only the agent's
   own in-process tools, a JSON-schema output, a turn limit and a dollar limit;
4. records model, tokens and cost on the row and settles the ledger;
5. validates the output against the agent's Pydantic model. Invalid output is retried once
   with the validation errors attached, then the call fails with AgentOutputInvalid.

Agents never see secrets: the CLI subprocess inherits the worker's environment, so every
secret except the Anthropic key is blanked before it starts.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk import query as sdk_query
from pydantic import BaseModel, ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import AgentRun
from services import budget
from services.config import BrandConfig, Settings, secret_env_names
from services.logs import ctx
from services.pricing import cost_from_usage

log = logging.getLogger(__name__)

TOOL_SERVER = "agent"  # tools appear to the model as mcp__agent__<name>

# Tools the CLI adds for features we turn on, which have no side effects. `StructuredOutput`
# is how the CLI returns the final JSON when `output_format` is set.
CLI_INTERNAL_TOOLS = frozenset({"StructuredOutput"})

# The SDK's own dollar limit can overshoot by the turn that crosses it, so it is set below the
# amount held in the ledger.
SDK_LIMIT_FRACTION = 0.8

# The CLI keeps per-session state here instead of the host's ~/.claude, so a developer's own
# Claude Code settings, memory or plugins can never leak into an agent.
_CLI_CONFIG_DIR = Path(tempfile.gettempdir()) / "artec-agents-claude-config"

QueryFn = Callable[..., AsyncIterator[Any]]


class AgentCallFailed(RuntimeError):
    """The call did not produce a result (API error, limit hit, CLI failure)."""


class AgentOutputInvalid(RuntimeError):
    """The agent's output failed validation twice."""


class AgentLockdownBroken(RuntimeError):
    """The CLI exposed a tool the agent was not given. Fails closed before any API call."""


@dataclass(frozen=True)
class AgentSpec[T: BaseModel]:
    name: str
    model: str
    system_prompt: str
    output_model: type[T]
    tools: Sequence[SdkMcpTool[Any]] = ()
    max_turns: int = 8
    ceiling_usd: float = 0.50  # most one call may cost; held in the ledger before the call


@dataclass(frozen=True)
class RunContext:
    settings: Settings
    brand: BrandConfig
    sessionmaker: async_sessionmaker
    run_id: str | None = None
    task_id: int | None = None


@dataclass
class AgentResult[T: BaseModel]:
    output: T
    agent_run_ids: list[int] = field(default_factory=list)
    cost_usd: Decimal = Decimal(0)


@dataclass
class _Attempt:
    agent_run_id: int
    cost_usd: Decimal
    structured_output: Any = None
    validation_errors: str | None = None


def tool_names(spec: AgentSpec[Any]) -> list[str]:
    return [f"mcp__{TOOL_SERVER}__{t.name}" for t in spec.tools]


def cli_env(settings: Settings) -> dict[str, str]:
    """Environment overrides for the CLI subprocess."""
    if settings.anthropic_api_key is None:
        raise AgentCallFailed("ANTHROPIC_API_KEY is not set; refusing to call the API")
    env = {name: "" for name in secret_env_names()}  # blank every secret...
    env.update(
        {
            # ...except the one the CLI needs. API billing only: never a subscription login.
            "ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value(),
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CLAUDE_CONFIG_DIR": str(_CLI_CONFIG_DIR),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    return env


def build_options(spec: AgentSpec[Any], settings: Settings, cwd: str) -> ClaudeAgentOptions:
    servers = {}
    if spec.tools:
        servers[TOOL_SERVER] = create_sdk_mcp_server(TOOL_SERVER, "1.0.0", list(spec.tools))
    return ClaudeAgentOptions(
        model=spec.model,
        system_prompt=spec.system_prompt,
        tools=[],  # no built-in Claude Code tools at all
        mcp_servers=servers,
        strict_mcp_config=True,  # ignore any MCP config found on disk
        allowed_tools=tool_names(spec),
        permission_mode="dontAsk",  # anything not pre-approved is denied, never prompted
        setting_sources=[],  # no CLAUDE.md, settings or skills from the filesystem
        cwd=cwd,
        max_turns=spec.max_turns,
        max_budget_usd=round(spec.ceiling_usd * SDK_LIMIT_FRACTION, 4),
        output_format={"type": "json_schema", "schema": spec.output_model.model_json_schema()},
        env=cli_env(settings),
    )


def _check_lockdown(message: SystemMessage, spec: AgentSpec[Any]) -> list[str]:
    exposed = list((message.data or {}).get("tools") or [])
    allowed = set(tool_names(spec)) | CLI_INTERNAL_TOOLS
    extra = [t for t in exposed if t not in allowed]
    if extra:
        raise AgentLockdownBroken(f"CLI exposed tools the agent was not given: {extra}")
    return exposed


def _usage_summary(result: ResultMessage | None, model: str, exposed_tools: list[str]) -> dict:
    usage = (result.usage if result else None) or {}
    return {
        "usage": usage,
        "model_usage": (result.model_usage if result else None) or {},
        "sdk_cost_usd": result.total_cost_usd if result else None,
        "computed_cost_usd": cost_from_usage(model, usage),
        "num_turns": result.num_turns if result else None,
        "subtype": result.subtype if result else None,
        "tools_exposed": exposed_tools,
    }


def _call_cost(
    result: ResultMessage | None, model: str, hold: budget.Hold
) -> tuple[Decimal, str | None]:
    """Cost to record for one call, plus a ledger note when it is not the SDK's own figure."""
    if result is None:
        # The call died without reporting usage, so the spend is unknown. Keep the full hold.
        return hold.reserved_usd, "no usage reported; full hold kept"
    if result.total_cost_usd is not None:
        return budget.to_usd(result.total_cost_usd), None
    computed = cost_from_usage(model, result.usage)
    if computed is not None:
        return budget.to_usd(computed), "SDK reported no cost; computed from tokens"
    return hold.reserved_usd, "no cost reported and model not priced; full hold kept"


async def _call_once[T: BaseModel](
    spec: AgentSpec[T], prompt: str, rc: RunContext, attempt: int, query_fn: QueryFn
) -> _Attempt:
    hold = await budget.reserve_claude(
        rc.sessionmaker, brand=rc.brand, run_id=rc.run_id, ceiling_usd=spec.ceiling_usd
    )
    async with rc.sessionmaker() as session, session.begin():
        row = AgentRun(
            brand=rc.brand.brand,
            agent=spec.name,
            task_id=rc.task_id,
            run_id=rc.run_id,
            attempt=attempt,
            model=spec.model,
            status="running",
        )
        session.add(row)
        await session.flush()
        agent_run_id = row.id

    result: ResultMessage | None = None
    exposed: list[str] = []
    error: str | None = None
    lockdown: AgentLockdownBroken | None = None
    may_have_spent = False  # becomes True once the CLI passed init and could call the API
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix=f"agent-{spec.name}-") as cwd:
            options = build_options(spec, rc.settings, cwd)
            async for message in query_fn(prompt=prompt, options=options):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    exposed = _check_lockdown(message, spec)
                    may_have_spent = True
                elif isinstance(message, ResultMessage):
                    result = message
    except AgentLockdownBroken as e:
        lockdown, error = e, str(e)
    except (ClaudeSDKError, AgentCallFailed) as e:
        # ResultError lands here too, after its ResultMessage was captured above.
        error = f"{type(e).__name__}: {e}"
    duration_ms = int((time.monotonic() - started) * 1000)

    # Money first: settle what the call cost, or release the hold if it never reached the API.
    if result is None and not may_have_spent:
        cost, note = Decimal(0), "call never reached the API"
        await budget.release(rc.sessionmaker, hold, note=note)
    else:
        cost, note = _call_cost(result, spec.model, hold)
        await budget.settle(
            rc.sessionmaker, hold, amount_usd=cost, agent_run_id=agent_run_id, note=note
        )

    # Then the verdict on the output.
    structured = result.structured_output if result else None
    validation_errors: str | None = None
    if result is not None and result.subtype == "error_max_structured_output_retries":
        error, validation_errors = None, "the model could not produce output matching the schema"
    elif error is None and (result is None or result.is_error or result.subtype != "success"):
        detail = (result.errors if result else None) or ""
        error = f"result {result.subtype if result else 'missing'} {detail}".strip()
    elif error is None and structured is None:
        validation_errors = "no structured output returned"
    elif error is None:
        try:
            spec.output_model.model_validate(structured)
        except ValidationError as e:
            validation_errors = str(e)

    usage = (result.usage if result else None) or {}
    status = "error" if error else ("invalid_output" if validation_errors else "ok")
    async with rc.sessionmaker() as session, session.begin():
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == agent_run_id)
            .values(
                status=status,
                finished_at=datetime.now(UTC),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                cost_usd=cost,
                duration_ms=duration_ms,
                session_id=result.session_id if result else None,
                output=structured,
                usage=_usage_summary(result, spec.model, exposed),
                error=error or validation_errors,
            )
        )
    log.info(
        "agent call finished",
        extra=ctx(
            agent=spec.name,
            agent_run_id=agent_run_id,
            run_id=rc.run_id,
            attempt=attempt,
            model=spec.model,
            status=status,
            cost_usd=str(cost),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            duration_ms=duration_ms,
        ),
    )
    if lockdown is not None:
        raise lockdown
    if error:
        raise AgentCallFailed(f"{spec.name} attempt {attempt}: {error}")
    return _Attempt(agent_run_id, cost, structured, validation_errors)


def _retry_prompt(prompt: str, errors: str) -> str:
    return (
        f"{prompt}\n\n"
        "Your previous answer did not match the required output schema. Validation errors:\n"
        f"{errors}\n"
        "Answer again, matching the schema exactly."
    )


async def run_agent[T: BaseModel](
    spec: AgentSpec[T], prompt: str, rc: RunContext, *, query_fn: QueryFn = sdk_query
) -> AgentResult[T]:
    """Run an agent and return its validated output. Invalid output is retried once."""
    ids: list[int] = []
    total = Decimal(0)
    current = prompt
    for attempt in (1, 2):
        outcome = await _call_once(spec, current, rc, attempt, query_fn)
        ids.append(outcome.agent_run_id)
        total += outcome.cost_usd
        if outcome.validation_errors is None:
            output = spec.output_model.model_validate(outcome.structured_output)
            return AgentResult(output=output, agent_run_ids=ids, cost_usd=total)
        current = _retry_prompt(prompt, outcome.validation_errors)
    raise AgentOutputInvalid(f"{spec.name}: output invalid after 2 attempts (agent_runs {ids})")
