"""Budget guard: checks the ledger before every paid call and records the cost after it.

Usage, for every paid call:

    hold = await reserve_claude(sessionmaker, brand=cfg, run_id=..., ceiling_usd=0.50)
    try:
        ... make the call ...
        await settle(sessionmaker, hold, amount_usd=actual_cost, agent_run_id=...)
    except NothingWasSpent:
        await release(sessionmaker, hold)

`reserve_*` takes a per-brand advisory lock, sums today's settled and still-reserved spend,
and refuses the call if the ceiling would cross the cap. The cap is therefore enforced
before money is spent, and two concurrent calls cannot both squeeze under it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import ROUND_UP, Decimal

from sqlalchemy import case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import SpendLedger
from services.config import BrandConfig
from services.logs import ctx

log = logging.getLogger(__name__)

ANTHROPIC = "anthropic"
FAL = "fal"

_LEDGER_PRECISION = Decimal("0.000001")


class BudgetExceeded(RuntimeError):
    """A paid call was refused because it could push spend over a cap."""


@dataclass(frozen=True)
class Hold:
    ledger_id: int
    reserved_usd: Decimal


def to_usd(value: float | Decimal) -> Decimal:
    """Round a dollar amount up to the ledger's precision (6 decimal places)."""
    return Decimal(str(value)).quantize(_LEDGER_PRECISION, rounding=ROUND_UP)


def local_day_start(brand: BrandConfig, now: datetime | None = None) -> datetime:
    """Start of the brand's current local day, as an aware UTC datetime."""
    now = now or datetime.now(UTC)
    local = now.astimezone(brand.tz)
    return datetime.combine(local.date(), time.min, tzinfo=brand.tz).astimezone(UTC)


async def spent_since(
    session: AsyncSession, *, brand: str, provider: str, since: datetime
) -> Decimal:
    """Settled spend plus open holds for a brand and provider since a moment."""
    held_or_spent = case(
        (SpendLedger.status == "reserved", SpendLedger.reserved_usd),
        else_=SpendLedger.amount_usd,
    )
    total = await session.scalar(
        select(func.coalesce(func.sum(held_or_spent), 0)).where(
            SpendLedger.brand == brand,
            SpendLedger.provider == provider,
            SpendLedger.created_at >= since,
        )
    )
    return Decimal(total)


async def reserve_claude(
    sessionmaker: async_sessionmaker,
    *,
    brand: BrandConfig,
    run_id: str | None,
    ceiling_usd: float | Decimal,
    now: datetime | None = None,
) -> Hold:
    """Hold `ceiling_usd` against today's Claude cap, or raise BudgetExceeded."""
    cap = to_usd(brand.claude_daily_cap_usd())
    ceiling = to_usd(ceiling_usd)
    since = local_day_start(brand, now)
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"budget:{brand.brand}:{ANTHROPIC}"},
        )
        spent = await spent_since(session, brand=brand.brand, provider=ANTHROPIC, since=since)
        if spent + ceiling > cap:
            log.error(
                "Claude daily cap reached; call refused",
                extra=ctx(
                    alert=True,
                    brand=brand.brand,
                    run_id=run_id,
                    spent_usd=str(spent),
                    ceiling_usd=str(ceiling),
                    cap_usd=str(cap),
                ),
            )
            raise BudgetExceeded(
                f"Claude spend today {spent} + this call's ceiling {ceiling} > cap {cap}"
            )
        row = SpendLedger(
            brand=brand.brand,
            run_id=run_id,
            provider=ANTHROPIC,
            status="reserved",
            reserved_usd=ceiling,
            amount_usd=Decimal(0),
        )
        session.add(row)
        await session.flush()
        return Hold(ledger_id=row.id, reserved_usd=ceiling)


async def settle(
    sessionmaker: async_sessionmaker,
    hold: Hold,
    *,
    amount_usd: float | Decimal,
    agent_run_id: int | None = None,
    note: str | None = None,
) -> None:
    """Replace a hold with the actual cost of the call."""
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(SpendLedger)
            .where(SpendLedger.id == hold.ledger_id, SpendLedger.status == "reserved")
            .values(
                status="settled",
                amount_usd=to_usd(amount_usd),
                agent_run_id=agent_run_id,
                note=note,
                settled_at=func.now(),
            )
        )


async def release(sessionmaker: async_sessionmaker, hold: Hold, *, note: str | None = None) -> None:
    """Drop a hold for a call that never reached the paid API."""
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(SpendLedger)
            .where(SpendLedger.id == hold.ledger_id, SpendLedger.status == "reserved")
            .values(status="released", amount_usd=Decimal(0), note=note, settled_at=func.now())
        )
