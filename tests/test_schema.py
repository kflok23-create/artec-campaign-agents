"""The constraints the safety rules lean on are enforced by Postgres, not just by code."""

import pytest
from sqlalchemy.exc import IntegrityError

from db.models import (
    Approval,
    Brand,
    Campaign,
    PostSlot,
    PostVariant,
    Publication,
    Task,
)

pytestmark = pytest.mark.anyio


async def _variant(sessionmaker) -> int:
    async with sessionmaker() as s, s.begin():
        brand = Brand(slug="artec", name="Artec", config_hash="x")
        s.add(brand)
        await s.flush()
        campaign = Campaign(brand_id=brand.id, name="c", brief={})
        s.add(campaign)
        await s.flush()
        slot = PostSlot(campaign_id=campaign.id, theme="t")
        s.add(slot)
        await s.flush()
        variant = PostVariant(slot_id=slot.id, channel="instagram")
        s.add(variant)
        await s.flush()
        return variant.id


async def test_a_publication_idempotency_key_is_used_once(sessionmaker):
    variant_id = await _variant(sessionmaker)
    async with sessionmaker() as s, s.begin():
        s.add(Publication(variant_id=variant_id, channel="instagram", idempotency_key="k1"))
    with pytest.raises(IntegrityError):
        async with sessionmaker() as s, s.begin():
            s.add(Publication(variant_id=variant_id, channel="instagram", idempotency_key="k1"))


async def test_one_variant_per_slot_and_channel(sessionmaker):
    variant_id = await _variant(sessionmaker)
    async with sessionmaker() as s:
        slot_id = (await s.get(PostVariant, variant_id)).slot_id
    with pytest.raises(IntegrityError):
        async with sessionmaker() as s, s.begin():
            s.add(PostVariant(slot_id=slot_id, channel="instagram"))


@pytest.mark.parametrize(
    "row",
    [
        lambda v: Approval(variant_id=v, approver="kah", decision="auto"),
        lambda v: PostVariant(slot_id=1, channel="myspace"),
        lambda v: Task(brand="artec", kind="x", idempotency_key="t", status="lost"),
    ],
    ids=["approval decision", "variant channel", "task status"],
)
async def test_unknown_values_are_rejected(sessionmaker, row):
    variant_id = await _variant(sessionmaker)
    with pytest.raises(IntegrityError):
        async with sessionmaker() as s, s.begin():
            s.add(row(variant_id))
