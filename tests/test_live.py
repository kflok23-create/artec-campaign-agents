"""Real paid calls. Skipped unless `pytest --run-live` and ANTHROPIC_API_KEY is set."""

import os

import pytest

from worker.smoke import run_smoke

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_smoke_call(settings, brand, sessionmaker):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    live = settings.model_copy(
        update={"anthropic_api_key": key, "railway_deployment_id": "live-test"}
    )
    assert await run_smoke(live, brand, sessionmaker, force=True) == "ok"
