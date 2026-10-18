"""Postgres advisory locks, for work that must not run twice at once across workers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


@asynccontextmanager
async def try_advisory_lock(sessionmaker: async_sessionmaker, key: str) -> AsyncIterator[bool]:
    """Hold a session-level lock on `key` for the block. Yields False if someone else holds it."""
    async with sessionmaker() as session:
        got = bool(
            await session.scalar(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key})
        )
        await session.commit()
        try:
            yield got
        finally:
            if got:
                await session.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})
                await session.commit()
