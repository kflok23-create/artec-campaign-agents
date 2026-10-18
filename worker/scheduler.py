"""Periodic jobs, enqueued as ordinary tasks.

Each job has a fixed interval. Time is cut into buckets of that length (counted from the Unix
epoch), and the task's idempotency key names the bucket, so any number of workers or restarts
enqueue each run exactly once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from services.config import BrandConfig, Settings
from services.logs import ctx
from worker import queue

log = logging.getLogger(__name__)

INDEX_ASSETS = "index_assets"


@dataclass(frozen=True)
class Periodic:
    kind: str
    every: timedelta
    payload: dict[str, Any] = field(default_factory=dict)


def bucket_start(every: timedelta, now: datetime) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    n = (now - epoch) // every
    return epoch + n * every


def idempotency_key(job: Periodic, brand: str, now: datetime) -> str:
    return f"{job.kind}:{brand}:{bucket_start(job.every, now).isoformat()}"


def periodic_jobs(settings: Settings, brand: BrandConfig) -> list[Periodic]:
    """The jobs this brand should run. A job whose prerequisites are missing is left out."""
    jobs = []
    if brand.assets.configured and settings.google_service_account_json is not None:
        jobs.append(Periodic(INDEX_ASSETS, timedelta(minutes=brand.assets.index_every_minutes)))
    return jobs


def missing_prerequisites(settings: Settings, brand: BrandConfig) -> list[str]:
    missing = []
    if not brand.assets.configured:
        missing.append(f"assets.drive_folder_id in brands/{brand.brand}.yaml")
    if settings.google_service_account_json is None:
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
    return missing


async def enqueue_due(
    sessionmaker: async_sessionmaker,
    brand: BrandConfig,
    jobs: list[Periodic],
    now: datetime | None = None,
) -> list[int]:
    """Enqueue the current run of every job, unless it is already queued. Returns new ids."""
    now = now or datetime.now(UTC)
    created = []
    async with sessionmaker() as session, session.begin():
        for job in jobs:
            task_id = await queue.enqueue(
                session,
                brand=brand.brand,
                kind=job.kind,
                idempotency_key=idempotency_key(job, brand.brand, now),
                payload=job.payload,
            )
            if task_id is not None:
                created.append(task_id)
                log.info("scheduled", extra=ctx(task_id=task_id, kind=job.kind))
    return created
