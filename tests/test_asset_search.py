import pytest

from db.models import Asset
from services.asset_search import get_asset, list_asset_tags, orientation_of, search_assets
from services.brands import ensure_brand
from services.drive_index import merged_tags, name_tags, rebuild_search

pytestmark = pytest.mark.anyio

BASE = "https://web.example.test"


def _asset(brand_id, key, name, folders, vision=None, **kw):
    ntags = name_tags(folders, name)
    defaults = dict(
        brand_id=brand_id,
        drive_file_id=f"file-{key}",
        public_token=f"tok-{key}",
        name=name,
        folder_path="/".join(folders),
        mime_type="image/jpeg",
        kind="image",
        width=1080,
        height=1350,
        name_tags=ntags,
        tags=merged_tags(ntags, vision),
        vision=vision,
        description=(vision or {}).get("description"),
        people=(vision or {}).get("people", "unknown"),
        tagging_status="done" if vision else "pending",
    )
    defaults.update(kw)
    return Asset(**defaults)


def _vision(description, tags, people="none", issues=()):
    return {"description": description, "tags": tags, "people": people, "issues": list(issues)}


@pytest.fixture
async def bank(sessionmaker, brand):
    async with sessionmaker() as s, s.begin():
        brand_id = await ensure_brand(s, brand)
        rows = [
            _asset(
                brand_id,
                "robot",
                "ArtecRobo_kit.jpg",
                ["Products", "Robotics"],
                _vision("A robot kit on a white background.", ["robot kit", "product shot"]),
            ),
            _asset(
                brand_id,
                "kids",
                "workshop_03.jpg",
                ["Events"],
                _vision(
                    "Children build a robot together in a classroom.",
                    ["children", "classroom", "robot", "building"],
                    people="children",
                ),
            ),
            _asset(
                brand_id,
                "kids-ok",
                "workshop_04.jpg",
                ["Events", "consent-cleared"],
                _vision(
                    "Children build a robot at a table.",
                    ["children", "robot", "building"],
                    people="children",
                ),
                consent_cleared=True,
            ),
            _asset(
                brand_id,
                "puzzle-blurry",
                "puzzle_a.jpg",
                ["Products"],
                _vision("A wooden puzzle.", ["wooden puzzle", "puzzle"], issues=["blurry"]),
            ),
            _asset(
                brand_id,
                "puzzle",
                "puzzle_b.jpg",
                ["Products"],
                _vision("A wooden puzzle.", ["wooden puzzle", "puzzle"]),
                width=1600,
                height=900,
            ),
            _asset(
                brand_id,
                "gone",
                "robot_old.jpg",
                ["Products"],
                _vision("An old robot.", ["robot"]),
                status="removed",
            ),
        ]
        s.add_all(rows)
        await s.flush()
        await rebuild_search(s, [r.id for r in rows])
    return brand_id


async def _search(sessionmaker, brand_id, q, **kw):
    async with sessionmaker() as s:
        return await search_assets(s, brand_id=brand_id, query=q, public_base_url=BASE, **kw)


async def test_best_match_first(sessionmaker, bank):
    hits = await _search(sessionmaker, bank, "robot kit")
    assert hits[0].name == "ArtecRobo_kit.jpg"
    assert "robot_old.jpg" not in [h.name for h in hits]  # removed from the bank


async def test_product_names_match_split_or_joined(sessionmaker, bank):
    for q in ("ArtecRobo", "artec robo"):
        assert (await _search(sessionmaker, bank, q))[0].name == "ArtecRobo_kit.jpg"


async def test_safe_only_hides_uncleared_children(sessionmaker, bank):
    names = [h.name for h in await _search(sessionmaker, bank, "children building robot")]
    assert {"workshop_03.jpg", "workshop_04.jpg"} <= set(names)
    safe = [h.name for h in await _search(sessionmaker, bank, "children robot", safe_only=True)]
    assert "workshop_04.jpg" in safe and "workshop_03.jpg" not in safe


async def test_clean_images_rank_above_flawed_ones(sessionmaker, bank):
    hits = await _search(sessionmaker, bank, "wooden puzzle")
    assert [h.name for h in hits[:2]] == ["puzzle_b.jpg", "puzzle_a.jpg"]
    assert hits[1].issues == ["blurry"]


async def test_filters(sessionmaker, bank):
    landscape = await _search(sessionmaker, bank, "puzzle", orientation="landscape")
    assert [h.name for h in landscape] == ["puzzle_b.jpg"]
    assert await _search(sessionmaker, bank, "puzzle", kind="video") == []


async def test_empty_or_stopword_queries_return_nothing(sessionmaker, bank):
    assert await _search(sessionmaker, bank, "") == []
    assert await _search(sessionmaker, bank, "the photo of") == []


async def test_hit_details(sessionmaker, bank):
    (hit, *_) = await _search(sessionmaker, bank, "robot kit")
    assert hit.public_url == f"{BASE}/media/a/tok-robot"
    assert hit.orientation == "portrait" and hit.safe_for_children_rule
    async with sessionmaker() as s:
        same = await get_asset(s, brand_id=bank, asset_id=hit.id)
    assert same.name == hit.name and same.public_url is None  # no base URL configured


async def test_list_asset_tags(sessionmaker, bank):
    async with sessionmaker() as s:
        tags = dict(await list_asset_tags(s, brand_id=bank))
    assert tags["robot"] == 2  # the removed asset is not counted
    assert tags["products"] == 3


def test_orientation():
    assert orientation_of(1080, 1350) == "portrait"
    assert orientation_of(1000, 1020) == "square"
    assert orientation_of(None, 10) is None
