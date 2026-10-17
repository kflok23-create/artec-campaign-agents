"""Alembic environment: async engine, URL from the environment, one migrator at a time."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from db.models import Base
from db.session import async_url

config = context.config
if config.config_file_name and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Web and worker both run `alembic upgrade head` before they deploy. This advisory lock makes
# the second one wait, then find the schema already at head.
MIGRATION_LOCK_KEY = 727_001


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return url
    from services.config import get_settings

    return get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    url, _ = async_url(_database_url())
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
        context.run_migrations()


async def run_migrations_online() -> None:
    url, connect_args = async_url(_database_url())
    engine = create_async_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
