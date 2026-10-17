import anyio
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from db.session import async_url, make_engine
from services.config import Settings
from worker.main import schema_head, wait_for_schema


def test_health_ok(settings):
    with TestClient(create_app(settings)) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "web", "db": "ok", "commit": "local"}


def test_health_reports_unreachable_db():
    dead = Settings(_env_file=None, database_url="postgresql://u:p@127.0.0.1:1/none")
    with TestClient(create_app(dead)) as client:
        r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["db"] == "unreachable"


def test_no_public_api_docs(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_schema_head_is_latest_migration():
    assert schema_head() == "0001"


@pytest.mark.anyio
async def test_worker_waits_for_schema_at_head(migrated_db):
    engine = make_engine(migrated_db)
    try:
        with anyio.fail_after(5):
            await wait_for_schema(engine, max_wait=5)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("url", "expected", "ssl"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d", None),
        ("postgres://u:p@h/d", "postgresql+asyncpg://u:p@h/d", None),
        ("postgresql://u:p@h/d?sslmode=require", "postgresql+asyncpg://u:p@h/d", "require"),
    ],
)
def test_async_url(url, expected, ssl):
    got, connect_args = async_url(url)
    assert got == expected
    assert connect_args.get("ssl") == ssl
