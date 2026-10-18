from decimal import Decimal

import pytest
from claude_agent_sdk import ProcessError, ResultError, tool
from pydantic import BaseModel
from sqlalchemy import select

from agents.runner import (
    AgentCallFailed,
    AgentLockdownBroken,
    AgentOutputInvalid,
    AgentSpec,
    RunContext,
    build_options,
    run_agent,
)
from db.models import AgentRun, SpendLedger, Task
from services.budget import BudgetExceeded
from services.config import Budget
from tests.conftest import FAKE_API_KEY, FakeQuery, init_message, result_message

pytestmark = pytest.mark.anyio

TOOLS_OK = ["mcp__agent__echo", "StructuredOutput"]


class Out(BaseModel):
    answer: str
    score: int


@tool("echo", "Echo the input back.", {"text": str})
async def echo(args):
    return {"content": [{"type": "text", "text": args["text"]}]}


def spec(**kw) -> AgentSpec[Out]:
    return AgentSpec(
        name="tester",
        model="claude-haiku-5-5",
        system_prompt="You test.",
        output_model=Out,
        tools=[echo],
        **kw,
    )


def rc(settings, brand, sessionmaker, **kw) -> RunContext:
    return RunContext(
        settings=settings, brand=brand, sessionmaker=sessionmaker, run_id="run-1", **kw
    )


async def _rows(sessionmaker):
    async with sessionmaker() as s:
        runs = (await s.scalars(select(AgentRun).order_by(AgentRun.id))).all()
        ledger = (await s.scalars(select(SpendLedger).order_by(SpendLedger.id))).all()
    return runs, ledger


# --- Lockdown --------------------------------------------------------------------------------


def test_options_are_locked_down(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("FAL_KEY", "fal-real-secret")
    monkeypatch.setenv("META_APP_SECRET", "meta-real-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw-secret@h/db")
    opts = build_options(spec(ceiling_usd=0.5), settings, str(tmp_path))
    assert opts.tools == []  # no built-in Claude Code tools
    assert opts.setting_sources == []  # no CLAUDE.md / settings / skills from disk
    assert opts.permission_mode == "dontAsk"
    assert opts.strict_mcp_config is True
    assert opts.allowed_tools == ["mcp__agent__echo"]
    assert list(opts.mcp_servers) == ["agent"]
    assert opts.max_budget_usd == pytest.approx(0.4)
    assert opts.output_format["schema"]["title"] == "Out"
    # Every secret but the API key is blanked for the CLI subprocess.
    assert opts.env["FAL_KEY"] == ""
    assert opts.env["META_APP_SECRET"] == ""
    assert opts.env["DATABASE_URL"] == ""
    assert opts.env["ANTHROPIC_API_KEY"] == FAKE_API_KEY
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert opts.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert opts.env["CLAUDE_CONFIG_DIR"]


async def test_lockdown_breach_aborts_before_any_spend(settings, brand, sessionmaker):
    fake = FakeQuery(
        [init_message(["Bash", *TOOLS_OK]), result_message({"answer": "x", "score": 1})]
    )
    with pytest.raises(AgentLockdownBroken, match="Bash"):
        await run_agent(spec(), "go", rc(settings, brand, sessionmaker), query_fn=fake)
    runs, ledger = await _rows(sessionmaker)
    assert [r.status for r in runs] == ["error"]
    assert ledger[0].status == "released"
    assert ledger[0].amount_usd == 0


async def test_no_api_key_means_no_call(settings, brand, sessionmaker):
    fake = FakeQuery()
    no_key = settings.model_copy(update={"anthropic_api_key": None})
    with pytest.raises(AgentCallFailed, match="ANTHROPIC_API_KEY"):
        await run_agent(spec(), "go", rc(no_key, brand, sessionmaker), query_fn=fake)
    assert fake.calls == []
    runs, ledger = await _rows(sessionmaker)
    assert runs[0].status == "error"
    assert ledger[0].status == "released"


# --- Logging and cost ------------------------------------------------------------------------


async def test_success_logs_tokens_and_cost(settings, brand, sessionmaker):
    async with sessionmaker() as s, s.begin():
        task = Task(brand="artec", kind="test", idempotency_key="test:1")
        s.add(task)
    fake = FakeQuery(
        [init_message(TOOLS_OK), result_message({"answer": "hi", "score": 3}, cost=0.0042)]
    )
    result = await run_agent(
        spec(), "go", rc(settings, brand, sessionmaker, task_id=task.id), query_fn=fake
    )
    assert result.output == Out(answer="hi", score=3)
    assert result.cost_usd == Decimal("0.0042")
    runs, ledger = await _rows(sessionmaker)
    (run,) = runs
    assert run.status == "ok"
    assert (run.agent, run.model, run.task_id, run.run_id, run.attempt) == (
        "tester",
        "claude-haiku-5-5",
        task.id,
        "run-1",
        1,
    )
    assert (run.input_tokens, run.output_tokens) == (1500, 120)
    assert run.cost_usd == Decimal("0.0042")
    assert run.output == {"answer": "hi", "score": 3}
    assert run.usage["tools_exposed"] == TOOLS_OK
    assert run.usage["computed_cost_usd"] == pytest.approx(1500 * 0.1e-6 + 120 * 0.5e-6)
    assert run.finished_at is not None
    (row,) = ledger
    assert (row.status, row.amount_usd, row.agent_run_id) == ("settled", Decimal("0.0042"), run.id)


async def test_cost_computed_when_sdk_reports_none(settings, brand, sessionmaker):
    fake = FakeQuery(
        [init_message(TOOLS_OK), result_message({"answer": "a", "score": 1}, cost=None)]
    )
    result = await run_agent(spec(), "go", rc(settings, brand, sessionmaker), query_fn=fake)
    assert result.cost_usd == Decimal("0.000210")  # 1500 in x $0.10/M + 120 out x $0.50/M
    _, ledger = await _rows(sessionmaker)
    assert "computed from tokens" in ledger[0].note


# --- Invalid output: retried once, then fail -------------------------------------------------


async def test_invalid_output_retried_once_then_ok(settings, brand, sessionmaker):
    fake = FakeQuery(
        [init_message(TOOLS_OK), result_message({"answer": "hi", "score": "lots"})],
        [init_message(TOOLS_OK), result_message({"answer": "hi", "score": 5})],
    )
    result = await run_agent(spec(), "go", rc(settings, brand, sessionmaker), query_fn=fake)
    assert result.output.score == 5
    assert len(result.agent_run_ids) == 2
    assert "score" in fake.calls[1]["prompt"]  # the retry carries the validation errors
    runs, ledger = await _rows(sessionmaker)
    assert [(r.status, r.attempt) for r in runs] == [("invalid_output", 1), ("ok", 2)]
    assert [row.status for row in ledger] == ["settled", "settled"]


async def test_invalid_output_twice_fails(settings, brand, sessionmaker):
    bad = [init_message(TOOLS_OK), result_message(None)]
    fake = FakeQuery(bad, list(bad))
    with pytest.raises(AgentOutputInvalid):
        await run_agent(spec(), "go", rc(settings, brand, sessionmaker), query_fn=fake)
    runs, _ = await _rows(sessionmaker)
    assert [r.status for r in runs] == ["invalid_output", "invalid_output"]


async def test_schema_retries_exhausted_counts_as_invalid(settings, brand, sessionmaker):
    exhausted = result_message(None, subtype="error_max_structured_output_retries")
    fake = FakeQuery(
        [init_message(TOOLS_OK), exhausted, ResultError("schema retries exhausted")],
        [init_message(TOOLS_OK), result_message({"answer": "ok", "score": 1})],
    )
    result = await run_agent(spec(), "go", rc(settings, brand, sessionmaker), query_fn=fake)
    assert result.output.answer == "ok"


# --- Failures ----------------------------------------------------------------------------------


async def test_budget_limit_error_is_recorded_with_its_cost(settings, brand, sessionmaker):
    over = result_message(None, subtype="error_max_budget_usd", cost=0.41)
    fake = FakeQuery([init_message(TOOLS_OK), over, ResultError("budget")])
    with pytest.raises(AgentCallFailed, match="ResultError"):
        await run_agent(spec(), "go", rc(settings, brand, sessionmaker), query_fn=fake)
    runs, ledger = await _rows(sessionmaker)
    assert runs[0].status == "error"
    assert ledger[0].amount_usd == Decimal("0.41")


async def test_crash_after_init_keeps_full_hold(settings, brand, sessionmaker):
    fake = FakeQuery([init_message(TOOLS_OK), ProcessError("CLI died", exit_code=137)])
    with pytest.raises(AgentCallFailed, match="CLI died"):
        await run_agent(
            spec(ceiling_usd=0.3), "go", rc(settings, brand, sessionmaker), query_fn=fake
        )
    _, ledger = await _rows(sessionmaker)
    assert ledger[0].status == "settled"
    assert ledger[0].amount_usd == Decimal("0.3")  # unknown spend: assume the worst
    assert "full hold kept" in ledger[0].note


async def test_cap_reached_means_no_call_and_no_row(settings, brand, sessionmaker):
    tiny = brand.model_copy(update={"budget": Budget(claude_daily_usd=0.10)})
    fake = FakeQuery()
    with pytest.raises(BudgetExceeded):
        await run_agent(
            spec(ceiling_usd=0.5), "go", rc(settings, tiny, sessionmaker), query_fn=fake
        )
    assert fake.calls == []
    runs, ledger = await _rows(sessionmaker)
    assert runs == [] and ledger == []


# --- Images ----------------------------------------------------------------------------------


async def test_images_are_sent_as_content_blocks(settings, brand, sessionmaker):
    from agents.runner import ImageInput

    fake = FakeQuery([init_message(TOOLS_OK), result_message({"answer": "a cat", "score": 1})])
    image = ImageInput(media_type="image/jpeg", data=b"\xff\xd8jpeg-bytes")
    await run_agent(
        spec(), "describe", rc(settings, brand, sessionmaker), images=[image], query_fn=fake
    )
    messages = [m async for m in fake.calls[0]["prompt"]]
    (msg,) = messages
    assert msg["type"] == "user"
    blocks = msg["message"]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "/9hqcGVnLWJ5dGVz",
    }
    assert blocks[1] == {"type": "text", "text": "describe"}


async def test_text_only_prompt_stays_a_string(settings, brand, sessionmaker):
    fake = FakeQuery([init_message(TOOLS_OK), result_message({"answer": "x", "score": 1})])
    await run_agent(spec(), "go", rc(settings, brand, sessionmaker), query_fn=fake)
    assert fake.calls[0]["prompt"] == "go"
