"""The `brands` row for a brand config: created on first start, kept in step with the file."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Brand
from services.config import BrandConfig


async def ensure_brand(session: AsyncSession, brand: BrandConfig) -> int:
    """Upsert the brand row from its config and return its id."""
    stmt = (
        pg_insert(Brand)
        .values(slug=brand.brand, name=brand.name, config_hash=brand.config_hash)
        .on_conflict_do_update(
            index_elements=["slug"],
            set_={"name": brand.name, "config_hash": brand.config_hash},
        )
        .returning(Brand.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def brand_id(session: AsyncSession, slug: str) -> int | None:
    return await session.scalar(select(Brand.id).where(Brand.slug == slug))
