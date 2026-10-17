"""Async engine and session factory for Postgres (SQLAlchemy 2 + asyncpg)."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

# libpq `sslmode` values that mean "use TLS"; asyncpg takes `ssl` instead.
_SSL_MODES = {"require", "verify-ca", "verify-full"}


def async_url(url: str) -> tuple[str, dict]:
    """Turn a libpq-style URL (as Railway provides) into an asyncpg URL plus connect args."""
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix) :]
            break
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    connect_args: dict = {}
    sslmode = query.pop("sslmode", None)
    if sslmode in _SSL_MODES:
        connect_args["ssl"] = "require"
    return urlunsplit(parts._replace(query=urlencode(query))), connect_args


def make_engine(database_url: str, **overrides) -> AsyncEngine:
    url, connect_args = async_url(database_url)
    options = {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5, **overrides}
    return create_async_engine(url, connect_args=connect_args, **options)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
