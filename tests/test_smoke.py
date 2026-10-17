import pytest
from sqlalchemy import select

from db.models import AgentRun
from tests.conftest import FakeQuery, init_message, result_message
from worker.smoke import run_smoke, smoke_run_id, smoke_spec

pytestmark = pytest.mark.anyio

TOOLS = ["mcp__agent__get_brand_name", "StructuredOutput"]


def passing_run(brand):
    return [
        init_message(TOOLS),
        result_message({"ok": True, "brand_name": brand.name, "word_count": 1}),
    ]


async def test_tool_returns_brand_name(settings, brand):
    (get_name,) = smoke_spec(settings, brand).tools
    out = await get_name.handler({})
    assert out["content"][0]["text"] == "Artec"


async def test_smoke_runs_once_per_deployment(settings, brand, sessionmaker):
    """A restart of the same deployment must not repeat the paid call."""
    deployed = settings.model_copy(update={"railway_deployment_id": "dep-123"})
    fake = FakeQuery(passing_run(brand))
    assert await run_smoke(deployed, brand, sessionmaker, query_fn=fake) == "ok"
    # "Restart": same deployment id, fresh call.
    assert await run_smoke(deployed, brand, sessionmaker, query_fn=fake) == "skipped"
    assert len(fake.calls) == 1
    async with sessionmaker() as s:
        rows = (await s.scalars(select(AgentRun))).all()
    assert [(r.run_id, r.status) for r in rows] == [("smoke:dep-123", "ok")]


async def test_new_deployment_runs_again(settings, brand, sessionmaker):
    fake = FakeQuery(passing_run(brand), passing_run(brand))
    first = settings.model_copy(update={"railway_deployment_id": "dep-1"})
    second = settings.model_copy(update={"railway_deployment_id": "dep-2"})
    assert await run_smoke(first, brand, sessionmaker, query_fn=fake) == "ok"
    assert await run_smoke(second, brand, sessionmaker, query_fn=fake) == "ok"
    assert len(fake.calls) == 2


async def test_failed_smoke_is_retried_on_next_boot(settings, brand, sessionmaker):
    from agents.runner import AgentCallFailed

    fake = FakeQuery(
        [init_message(TOOLS), result_message(None, subtype="error_during_execution", cost=0.0)],
        passing_run(brand),
    )
    with pytest.raises(AgentCallFailed):
        await run_smoke(settings, brand, sessionmaker, query_fn=fake)
    assert await run_smoke(settings, brand, sessionmaker, query_fn=fake) == "ok"


def test_local_run_id(settings):
    assert smoke_run_id(settings) == "smoke:local"
