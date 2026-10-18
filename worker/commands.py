"""One-off commands for the owner and the gate: index now, search, and summarise the bank."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from db.locks import try_advisory_lock
from db.models import Asset, Task
from db.session import make_engine, make_sessionmaker
from services.asset_search import search_assets
from services.brands import ensure_brand
from services.config import Settings, load_brand
from services.drive import drive_client
from services.drive_index import index_brand
from worker.main import wait_for_schema
from worker.scheduler import INDEX_ASSETS, missing_prerequisites
from worker.tasks import index_lock_key


async def index_now(
    settings: Settings, *, tag: bool, retag_failed: bool, tag_limit: int | None
) -> str:
    brand = load_brand(settings.brand)
    missing = missing_prerequisites(settings, brand)
    if missing:
        return "Cannot index yet. Set: " + ", ".join(missing)
    assert settings.google_service_account_json is not None
    engine = make_engine(settings.database_url.get_secret_value())
    sessionmaker = make_sessionmaker(engine)
    try:
        await wait_for_schema(engine, max_wait=30)
        async with sessionmaker() as session, session.begin():
            brand_id = await ensure_brand(session, brand)
        async with try_advisory_lock(sessionmaker, index_lock_key(brand.brand)) as got:
            if not got:
                return "Another index run is in progress; try again when it finishes."
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            async with drive_client(settings.google_service_account_json.get_secret_value()) as d:
                report = await index_brand(
                    sessionmaker=sessionmaker,
                    settings=settings,
                    brand=brand,
                    brand_id=brand_id,
                    drive=d,
                    run_id=f"index:{brand.brand}:manual-{stamp}",
                    tag=tag,
                    retag_failed=retag_failed,
                    tag_limit=tag_limit,
                )
    finally:
        await engine.dispose()
    return json.dumps(report.as_dict(), indent=2, default=str)


async def search(
    settings: Settings, query: str, *, kind: str | None, safe_only: bool, limit: int
) -> str:
    brand = load_brand(settings.brand)
    engine = make_engine(settings.database_url.get_secret_value())
    sessionmaker = make_sessionmaker(engine)
    try:
        async with sessionmaker() as session, session.begin():
            brand_id = await ensure_brand(session, brand)
            hits = await search_assets(
                session,
                brand_id=brand_id,
                query=query,
                kind=kind,  # type: ignore[arg-type]
                safe_only=safe_only,
                limit=limit,
                public_base_url=settings.public_base_url,
            )
    finally:
        await engine.dispose()
    if not hits:
        return f"No matches for {query!r}."
    lines = [f"Top {len(hits)} for {query!r}:", ""]
    for n, h in enumerate(hits, 1):
        flags = []
        if not h.safe_for_children_rule:
            flags.append("children, no consent")
        flags.extend(h.issues)
        lines.append(
            f"{n:>2}. {h.name}  [{h.kind}, {h.orientation or '?'}, score {h.score:.3f}]"
            + (f"  ({'; '.join(flags)})" if flags else "")
        )
        lines.append(f"    folder: {h.folder_path or '(top)'}")
        if h.description:
            lines.append(f"    {h.description}")
        lines.append(f"    tags: {', '.join(h.tags[:12])}")
        if h.public_url:
            lines.append(f"    {h.public_url}")
    return "\n".join(lines)


async def assets_summary(settings: Settings) -> str:
    """How complete the index is: the gate's 'whole asset bank is indexed' evidence."""
    brand = load_brand(settings.brand)
    engine = make_engine(settings.database_url.get_secret_value())
    sessionmaker = make_sessionmaker(engine)
    try:
        async with sessionmaker() as session, session.begin():
            brand_id = await ensure_brand(session, brand)

            async def counts(*cols):
                rows = await session.execute(
                    select(*cols, func.count())
                    .where(Asset.brand_id == brand_id)
                    .group_by(*cols)
                    .order_by(*cols)
                )
                return rows.all()

            by_status = await counts(Asset.status, Asset.kind)
            active = Asset.status == "active"
            tagging = (
                await session.execute(
                    select(Asset.tagging_status, func.count())
                    .where(Asset.brand_id == brand_id, active)
                    .group_by(Asset.tagging_status)
                )
            ).all()
            people = (
                await session.execute(
                    select(Asset.people, Asset.consent_cleared, func.count())
                    .where(Asset.brand_id == brand_id, active)
                    .group_by(Asset.people, Asset.consent_cleared)
                    .order_by(Asset.people)
                )
            ).all()
            last = await session.scalar(
                select(Task)
                .where(Task.brand == brand.brand, Task.kind == INDEX_ASSETS)
                .order_by(Task.id.desc())
                .limit(1)
            )
    finally:
        await engine.dispose()
    lines = [f"Asset bank for {brand.name}", "", "Assets by status and kind:"]
    lines += [f"  {status:8} {kind:6} {n:>6}" for status, kind, n in by_status] or ["  none yet"]
    lines += ["", "Tagging (active assets):"]
    lines += [f"  {status:8} {n:>6}" for status, n in tagging] or ["  none yet"]
    lines += ["", "People seen (active assets):"]
    lines += [
        f"  {who:20} {'consent-cleared' if cleared else 'not cleared':16} {n:>6}"
        for who, cleared, n in people
    ] or ["  none yet"]
    lines += ["", "Last scheduled index run:"]
    if last is None:
        lines.append("  none yet")
    else:
        lines.append(f"  task {last.id} {last.status} at {last.updated_at:%Y-%m-%d %H:%M} UTC")
        if last.last_error:
            lines.append(f"  error: {last.last_error}")
        if last.result:
            lines.append("  " + json.dumps(last.result, default=str))
    return "\n".join(lines)
