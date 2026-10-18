"""Searching the asset index: full-text search in Postgres, no external service.

A query is split into words the same way file names are ("ArtecRobo" also matches "artec
robo"), the words are OR-ed, and results are ranked so that assets matching more words, and
matching vision tags rather than folder names, come first. Assets with image issues (blurry,
watermark, ...) sink below clean ones of the same rank.

These functions back the Curator's `search_assets` and `get_asset` tools and the Strategist's
`list_asset_tags` tool (Phase 2), and the `python -m worker search` command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PEOPLE_WITHOUT_CHILDREN
from services.drive_index import words

Orientation = Literal["portrait", "landscape", "square"]
MAX_LIMIT = 50


@dataclass(frozen=True)
class AssetHit:
    id: int
    name: str
    folder_path: str
    kind: str
    width: int | None
    height: int | None
    tags: list[str]
    description: str | None
    people: str
    consent_cleared: bool
    issues: list[str]
    score: float
    public_url: str | None

    @property
    def orientation(self) -> Orientation | None:
        return orientation_of(self.width, self.height)

    @property
    def safe_for_children_rule(self) -> bool:
        """False if it may show children and is not consent-cleared (the Reviewer blocks it)."""
        return self.consent_cleared or self.people in PEOPLE_WITHOUT_CHILDREN


def orientation_of(width: int | None, height: int | None) -> Orientation | None:
    if not width or not height:
        return None
    ratio = width / height
    if ratio > 1.05:
        return "landscape"
    if ratio < 0.95:
        return "portrait"
    return "square"


def public_url(base: str | None, token: str) -> str | None:
    return f"{base.rstrip('/')}/media/a/{token}" if base else None


_SELECT = """
    SELECT id, name, folder_path, kind, width, height, tags, description, people,
           consent_cleared, public_token,
           coalesce((SELECT array_agg(i) FROM jsonb_array_elements_text(vision->'issues') i),
                    '{{}}') AS issues,
           {score} AS score
    FROM assets
"""


def _hit(row, base_url: str | None) -> AssetHit:
    return AssetHit(
        id=row.id,
        name=row.name,
        folder_path=row.folder_path,
        kind=row.kind,
        width=row.width,
        height=row.height,
        tags=list(row.tags or []),
        description=row.description,
        people=row.people,
        consent_cleared=row.consent_cleared,
        issues=list(row.issues or []),
        score=float(row.score),
        public_url=public_url(base_url, row.public_token),
    )


async def search_assets(
    session: AsyncSession,
    *,
    brand_id: int,
    query: str,
    kind: Literal["image", "video"] | None = None,
    orientation: Orientation | None = None,
    safe_only: bool = False,
    limit: int = 10,
    public_base_url: str | None = None,
) -> list[AssetHit]:
    """Best matches for a free-text query among the brand's active assets."""
    terms = words(query)
    if not terms:
        return []
    filters = ["brand_id = :brand_id", "status = 'active'", "search @@ q.tsq"]
    params: dict = {
        "brand_id": brand_id,
        "terms": " ".join(terms),
        "limit": max(1, min(limit, MAX_LIMIT)),
        "safe_people": list(PEOPLE_WITHOUT_CHILDREN),
    }
    if kind:
        filters.append("kind = :kind")
        params["kind"] = kind
    if orientation == "landscape":
        filters.append("width > height * 1.05")
    elif orientation == "portrait":
        filters.append("width < height * 0.95")
    elif orientation == "square":
        filters.append("width BETWEEN height * 0.95 AND height * 1.05")
    if safe_only:
        filters.append("(consent_cleared OR people = ANY(:safe_people))")
    sql = (
        "WITH q AS (SELECT replace(plainto_tsquery('english', :terms)::text, '&', '|')::tsquery"
        " AS tsq) "
        + _SELECT.format(score="ts_rank(search, q.tsq)")
        + ", q WHERE "
        + " AND ".join(filters)
        + " ORDER BY score DESC,"
        " coalesce(jsonb_array_length(vision->'issues'), 0) ASC, id"
        " LIMIT :limit"
    )
    rows = (await session.execute(text(sql), params)).all()
    return [_hit(r, public_base_url) for r in rows]


async def get_asset(
    session: AsyncSession, *, brand_id: int, asset_id: int, public_base_url: str | None = None
) -> AssetHit | None:
    sql = _SELECT.format(score="0") + " WHERE brand_id = :brand_id AND id = :id"
    row = (await session.execute(text(sql), {"brand_id": brand_id, "id": asset_id})).first()
    return _hit(row, public_base_url) if row else None


async def list_asset_tags(
    session: AsyncSession, *, brand_id: int, limit: int = 100
) -> list[tuple[str, int]]:
    """The most common tags in the brand's active assets, with how many assets carry each."""
    rows = await session.execute(
        text(
            "SELECT tag, count(*) AS n FROM assets, unnest(tags) AS tag"
            " WHERE brand_id = :brand_id AND status = 'active'"
            " GROUP BY tag ORDER BY n DESC, tag LIMIT :limit"
        ),
        {"brand_id": brand_id, "limit": limit},
    )
    return [(r.tag, r.n) for r in rows]
