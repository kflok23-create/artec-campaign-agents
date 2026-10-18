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

from db.locks import try_advisory_lock
from services.config import BrandConfig, Settings
from services.drive import drive_client
from services.drive_index import IndexConfigError, index_brand
from worker.queue import ClaimedTask
from worker.scheduler import INDEX_ASSETS


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


def index_lock_key(brand: str) -> str:
    return f"index_assets:{brand}"


@handles(INDEX_ASSETS)
async def index_assets(task: ClaimedTask, w: WorkerContext) -> dict[str, Any]:
    """Scan the brand's Drive bank, then tag new or changed images within the run budget."""
    if not w.brand.assets.configured or w.settings.google_service_account_json is None:
        raise NonRetryable("asset bank not configured (drive_folder_id or service account)")
    async with try_advisory_lock(w.sessionmaker, index_lock_key(w.brand.brand)) as got:
        if not got:
            return {"skipped": "another index run holds the lock"}
        try:
            async with drive_client(w.settings.google_service_account_json.get_secret_value()) as d:
                report = await index_brand(
                    sessionmaker=w.sessionmaker,
                    settings=w.settings,
                    brand=w.brand,
                    brand_id=w.brand_id,
                    drive=d,
                    run_id=f"index:{w.brand.brand}:task-{task.id}",
                    task_id=task.id,
                )
        except IndexConfigError as e:
            raise NonRetryable(str(e)) from e  # a wrong folder ID will not fix itself
    return report.as_dict()
