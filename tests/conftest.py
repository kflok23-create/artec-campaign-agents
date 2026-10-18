"""Shared fixtures.

Database tests need Postgres at TEST_DATABASE_URL (default: the local compose database,
started with `docker compose up -d`). They fail, rather than skip, when it is missing.
Tests marked `live` make real paid API calls and only run with `--run-live`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from claude_agent_sdk import ResultMessage, SystemMessage
from sqlalchemy import text

from db.models import Base
from db.session import make_engine, make_sessionmaker
from services.config import REPO_ROOT, Settings, load_brand

# Local throwaway database from compose.yaml; never a production credential.
DEFAULT_TEST_DB = "postgresql://artec:artec-local-only@127.0.0.1:54329/artec_agents_test"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB)
FAKE_API_KEY = "sk-ant-test-not-a-real-key-000000"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-live", action="store_true", help="run tests that make paid API calls")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="paid API call; pass --run-live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Migrate the test database from scratch once per session; returns its URL."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["skip_logging_config"] = True
    try:
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    except Exception as e:  # noqa: BLE001
        pytest.fail(
            f"Postgres not usable at TEST_DATABASE_URL ({type(e).__name__}: {e}). "
            "Start it with: docker compose up -d"
        )
    return TEST_DATABASE_URL


@pytest.fixture
async def sessionmaker(migrated_db: str) -> AsyncIterator[Any]:
    engine = make_engine(migrated_db)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def settings(migrated_db: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=migrated_db,
        anthropic_api_key=FAKE_API_KEY,
        worker_smoke_on_boot=False,
    )


@pytest.fixture
def brand():
    return load_brand("artec")


@pytest.fixture
def brands_dir(tmp_path: Path) -> Path:
    """A scratch brands/ directory holding a copy of artec.yaml to mutate."""
    d = tmp_path / "brands"
    d.mkdir()
    (d / "artec.yaml").write_text(
        (REPO_ROOT / "brands" / "artec.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return d


# --- Fake SDK -----------------------------------------------------------------------------------


def init_message(tools: list[str]) -> SystemMessage:
    return SystemMessage(subtype="init", data={"type": "system", "subtype": "init", "tools": tools})


def result_message(
    structured_output: Any = None,
    *,
    subtype: str = "success",
    cost: float | None = 0.0012,
    usage: dict | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1200,
        duration_api_ms=900,
        is_error=subtype != "success",
        num_turns=2,
        session_id="sess-test",
        total_cost_usd=cost,
        usage=usage
        if usage is not None
        else {
            "input_tokens": 1500,
            "output_tokens": 120,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        structured_output=structured_output,
    )


class FakeQuery:
    """Stands in for claude_agent_sdk.query. Each call plays the next scripted run."""

    def __init__(self, *runs: list[Any] | Exception) -> None:
        self.runs = list(runs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, prompt: str, options: Any):
        self.calls.append({"prompt": prompt, "options": options})
        script = self.runs.pop(0)
        return self._play(script)

    async def _play(self, script: list[Any]):
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item
