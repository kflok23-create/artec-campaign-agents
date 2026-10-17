from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
import pytest
from sqlalchemy import select

from db.models import SpendLedger
from services import budget
from services.config import BrandConfigError, Budget

pytestmark = pytest.mark.anyio


def with_cap(brand, cap):
    return brand.model_copy(update={"budget": Budget(claude_daily_usd=cap)})


async def _spent(sessionmaker, brand) -> Decimal:
    async with sessionmaker() as s:
        return await budget.spent_since(
            s, brand=brand.brand, provider=budget.ANTHROPIC, since=budget.local_day_start(brand)
        )


async def test_reserve_writes_a_hold(sessionmaker, brand):
    hold = await budget.reserve_claude(sessionmaker, brand=brand, run_id="r1", ceiling_usd=0.5)
    async with sessionmaker() as s:
        row = await s.get(SpendLedger, hold.ledger_id)
    assert row.status == "reserved"
    assert row.reserved_usd == Decimal("0.5")
    assert row.provider == "anthropic"
    assert await _spent(sessionmaker, brand) == Decimal("0.5")


async def test_holds_count_against_the_cap(sessionmaker, brand):
    cfg = with_cap(brand, 0.10)
    await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.05)
    await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.05)
    with pytest.raises(budget.BudgetExceeded):
        await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.01)


async def test_settle_replaces_hold_with_actual_cost(sessionmaker, brand):
    cfg = with_cap(brand, 0.10)
    hold = await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.08)
    await budget.settle(sessionmaker, hold, amount_usd=0.0123)
    assert await _spent(sessionmaker, cfg) == Decimal("0.012300")
    # The freed headroom is usable again.
    await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.08)


async def test_release_frees_the_hold(sessionmaker, brand):
    hold = await budget.reserve_claude(sessionmaker, brand=brand, run_id="r", ceiling_usd=0.5)
    await budget.release(sessionmaker, hold, note="never sent")
    assert await _spent(sessionmaker, brand) == Decimal(0)


async def test_settle_is_idempotent(sessionmaker, brand):
    hold = await budget.reserve_claude(sessionmaker, brand=brand, run_id="r", ceiling_usd=0.5)
    await budget.settle(sessionmaker, hold, amount_usd=0.01)
    await budget.settle(sessionmaker, hold, amount_usd=0.40)  # second settle must not overwrite
    assert await _spent(sessionmaker, brand) == Decimal("0.01")


async def test_yesterday_does_not_count(sessionmaker, brand):
    cfg = with_cap(brand, 1.0)
    async with sessionmaker() as s, s.begin():
        s.add(
            SpendLedger(
                brand=cfg.brand,
                provider="anthropic",
                status="settled",
                reserved_usd=Decimal("0.9"),
                amount_usd=Decimal("0.9"),
                created_at=budget.local_day_start(cfg) - timedelta(seconds=1),
            )
        )
    await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.9)


def test_local_day_start_uses_brand_timezone(brand):
    # 2026-10-17 17:30 UTC is 01:30 on the 18th in Kuala Lumpur (UTC+8).
    now = datetime(2026, 10, 17, 17, 30, tzinfo=UTC)
    assert budget.local_day_start(brand, now) == datetime(2026, 10, 17, 16, 0, tzinfo=UTC)


async def test_concurrent_reservations_never_exceed_cap(sessionmaker, brand):
    cfg = with_cap(brand, 0.20)
    outcomes: list[str] = []

    async def one():
        try:
            await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.05)
            outcomes.append("ok")
        except budget.BudgetExceeded:
            outcomes.append("refused")

    async with anyio.create_task_group() as tg:
        for _ in range(10):
            tg.start_soon(one)
    assert outcomes.count("ok") == 4
    assert await _spent(sessionmaker, cfg) == Decimal("0.20")


async def test_unset_cap_refuses_before_writing(sessionmaker, brand):
    cfg = brand.model_copy(update={"budget": Budget(claude_daily_usd="<confirm>")})
    with pytest.raises(BrandConfigError):
        await budget.reserve_claude(sessionmaker, brand=cfg, run_id="r", ceiling_usd=0.01)
    async with sessionmaker() as s:
        assert (await s.scalars(select(SpendLedger))).all() == []
