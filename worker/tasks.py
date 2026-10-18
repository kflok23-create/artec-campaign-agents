"""Task handlers: one async function per task kind.

Every handler must be safe to run again from the start: a task is retried after an error and
reclaimed if its worker dies part-way. A handler returns a small JSON-able summary, stored on
the task row.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from services.config import BrandConfig, Settings
from worker.queue import ClaimedTask


@dataclass(frozen=True)
class WorkerContext:
    settings: Settings
    brand: BrandConfig
    brand_id: int
    sessionmaker: async_sessionmaker


class NonRetryable(Exception):
    """Retrying cannot help (missing configuration, bad input): fail the task now."""


Handler = Callable[[ClaimedTask, WorkerContext], Awaitable[dict[str, Any] | None]]

HANDLERS: dict[str, Handler] = {}


def handles(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if kind in HANDLERS:
            raise ValueError(f"two handlers for task kind {kind!r}")
        HANDLERS[kind] = fn
        return fn

    return register
