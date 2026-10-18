import io
from datetime import UTC, datetime

import pytest
from PIL import Image
from sqlalchemy import select

from db.models import AgentRun, Asset, SpendLedger
from services import drive_index
from services.brands import ensure_brand
from services.drive import FOLDER_MIME, SHORTCUT_MIME, DriveError, DriveFile, DriveNotFound
from services.drive_index import (
    IndexConfigError,
    index_brand,
    is_consent_cleared,
    name_tags,
    words,
)
from tests.conftest import FakeQuery, init_message, result_message

pytestmark = pytest.mark.anyio

ROOT = "root-folder-0001"
TAGGER_TOOLS = ["StructuredOutput"]


def png(color="red", size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def folder(fid, name):
    return DriveFile(id=fid, name=name, mime_type=FOLDER_MIME)


def image(fid, name, md5="m1", mime="image/png", **kw):
    return DriveFile(
        id=fid,
        name=name,
        mime_type=mime,
        md5=md5,
        size=1000,
        width=64,
        height=48,
        modified_time=datetime(2026, 10, 1, tzinfo=UTC),
        **kw,
    )


class FakeDrive:
    account_email = "asset-indexer@test.iam.gserviceaccount.com"

    def __init__(self, tree, content=None, fail_listing=()):
        self.tree = tree  # folder id -> children
        self.content = content or {}
        self.fail_listing = set(fail_listing)
        self.files = {ROOT: folder(ROOT, "Bank")}
        for children in tree.values():
            for f in children:
                self.files[f.id] = f
        self.thumbnail_links: list[str] = []

    async def get(self, file_id):
        if file_id not in self.files:
            raise DriveNotFound("404")
        return self.files[file_id]

    async def list_children(self, folder_id, *, drive_id=None):
        if folder_id in self.fail_listing:
            raise DriveError("Drive returned HTTP 500")
        for f in self.tree.get(folder_id, []):
            yield f

    async def download(self, file_id, *, max_bytes):
        return self.content[file_id]

    async def thumbnail(self, link, *, max_bytes):
        self.thumbnail_links.append(link)
        return self.content["thumbnail"]


def bank():
    return {
        ROOT: [
            folder("f-products", "Products"),
            folder("f-events", "Events 2026"),
            image("a-logo", "ArtecLogo.png"),
            DriveFile(id="doc-1", name="Price list", mime_type="application/pdf"),
            DriveFile(id="sc-1", name="Shortcut", mime_type=SHORTCUT_MIME),
        ],
        "f-products": [image("a-robo", "ArtecRobo_Kit-01 FINAL.png")],
        "f-events": [
            folder("f-consent", "Consent Cleared"),
            image("a-crowd", "crowd.png"),
        ],
        "f-consent": [
            image("a-kids", "kids_building.png"),
            image(
                "a-video",
                "demo.mp4",
                mime="video/mp4",
                thumbnail_link="https://lh3.googleusercontent.com/thumb=s220",
            ),
        ],
    }


@pytest.fixture
def ready_brand(brand):
    return brand.model_copy(
        update={"assets": brand.assets.model_copy(update={"drive_folder_id": ROOT})}
    )


@pytest.fixture
async def brand_id(sessionmaker, ready_brand):
    async with sessionmaker() as s, s.begin():
        return await ensure_brand(s, ready_brand)


async def _assets(sessionmaker) -> dict[str, Asset]:
    async with sessionmaker() as s:
        return {a.drive_file_id: a for a in (await s.scalars(select(Asset))).all()}


def vision(**kw):
    out = {
        "description": "A child builds a robot from coloured blocks on a classroom table.",
        "tags": ["Robot Kit", "building", "classroom", "child", "robot kit"],
        "people": "children",
        "faces_identifiable": True,
        "text_in_image": False,
        "issues": [],
    }
    out.update(kw)
    return out


async def _index(sessionmaker, settings, ready_brand, brand_id, drive, query_fn=None, **kw):
    return await index_brand(
        sessionmaker=sessionmaker,
        settings=settings,
        brand=ready_brand,
        brand_id=brand_id,
        drive=drive,
        run_id="index:artec:test",
        query_fn=query_fn or FakeQuery(),
        **kw,
    )


# --- Names and consent ------------------------------------------------------------------------


def test_words_split_names_into_search_terms():
    assert words("ArtecRobo_Kit-01 FINAL") == ["artecrobo", "artec", "robo", "kit"]
    assert words("76909 ArtecRobo Education Set") == [
        "76909",
        "artecrobo",
        "artec",
        "robo",
        "education",
        "set",
    ]
    assert words("IMG_2041") == ["2041"]


def test_name_tags_cover_folders_and_file_name():
    tags = name_tags(["Products", "Puzzles"], "WoodenShape-Puzzle.jpg")
    assert tags == ["products", "puzzles", "woodenshape", "wooden", "shape", "puzzle"]


@pytest.mark.parametrize(
    "folders, cleared",
    [
        (["Events", "Consent Cleared"], True),
        (["consent_cleared", "2026"], True),
        (["CONSENT-CLEARED"], True),
        (["Events", "Consent pending"], False),
        (["not-consent-cleared"], False),
        ([], False),
    ],
)
def test_consent_comes_only_from_the_folder_convention(folders, cleared):
    assert is_consent_cleared(folders, ["consent-cleared"]) is cleared


# --- Walk and sync ----------------------------------------------------------------------------


async def test_index_lists_the_whole_bank(sessionmaker, settings, ready_brand, brand_id):
    report = await _index(
        sessionmaker, settings, ready_brand, brand_id, FakeDrive(bank()), tag=False
    )
    assert (report.folders, report.files_seen, report.images, report.videos) == (3, 5, 4, 1)
    assert report.skipped_types == {"application/pdf": 1}
    assert report.shortcuts == 1
    assert report.sync.new == 5
    assets = await _assets(sessionmaker)
    robo = assets["a-robo"]
    assert robo.folder_path == "Products"
    assert {"artecrobo", "kit", "products"} <= set(robo.tags)
    assert robo.tagging_status == "pending" and robo.people == "unknown"
    assert len(robo.public_token) >= 24
    assert assets["a-kids"].consent_cleared and assets["a-video"].consent_cleared
    assert not assets["a-crowd"].consent_cleared
    assert assets["a-video"].kind == "video"


async def test_reindexing_is_idempotent_and_tracks_changes(
    sessionmaker, settings, ready_brand, brand_id
):
    tree = bank()
    await _index(sessionmaker, settings, ready_brand, brand_id, FakeDrive(tree), tag=False)
    tokens = {k: a.public_token for k, a in (await _assets(sessionmaker)).items()}

    again = await _index(sessionmaker, settings, ready_brand, brand_id, FakeDrive(tree), tag=False)
    assert (again.sync.new, again.sync.unchanged, again.sync.removed) == (0, 5, 0)

    tree["f-products"] = [image("a-robo", "ArtecRobo_Kit-01 FINAL.png", md5="m2")]  # edited
    tree["f-events"] = [folder("f-consent", "Consent Cleared")]  # crowd.png deleted
    changed = await _index(
        sessionmaker, settings, ready_brand, brand_id, FakeDrive(tree), tag=False
    )
    assert (changed.sync.changed, changed.sync.removed) == (1, 1)
    assets = await _assets(sessionmaker)
    assert assets["a-crowd"].status == "removed"
    assert assets["a-robo"].public_token == tokens["a-robo"]  # URLs survive edits


async def test_listing_error_removes_nothing(sessionmaker, settings, ready_brand, brand_id):
    await _index(sessionmaker, settings, ready_brand, brand_id, FakeDrive(bank()), tag=False)
    broken = FakeDrive(bank(), fail_listing={"f-events"})
    with pytest.raises(DriveError):
        await _index(sessionmaker, settings, ready_brand, brand_id, broken, tag=False)
    assert {a.status for a in (await _assets(sessionmaker)).values()} == {"active"}


async def test_unshared_folder_is_a_config_error(sessionmaker, settings, ready_brand, brand_id):
    drive = FakeDrive({})
    drive.files.clear()
    with pytest.raises(IndexConfigError, match="shared with asset-indexer@"):
        await _index(sessionmaker, settings, ready_brand, brand_id, drive, tag=False)


# --- Vision tagging ---------------------------------------------------------------------------


def _one_image_bank():
    return {ROOT: [image("a-kids", "kids_building.png")]}


async def test_tagging_stores_vision_and_feeds_search(
    sessionmaker, settings, ready_brand, brand_id
):
    drive = FakeDrive(_one_image_bank(), content={"a-kids": png()})
    fake = FakeQuery([init_message(TAGGER_TOOLS), result_message(vision(), cost=0.0004)])
    report = await _index(sessionmaker, settings, ready_brand, brand_id, drive, query_fn=fake)
    assert (report.tagging.tagged, report.tagging.pending_left) == (1, 0)
    assert report.tagging_cost_usd == "0.000400"

    asset = (await _assets(sessionmaker))["a-kids"]
    assert asset.tagging_status == "done" and asset.people == "children"
    assert asset.tags[:4] == ["robot kit", "building", "classroom", "child"]  # normalised, first
    assert "kids" in asset.tags  # name tags kept after the vision tags
    assert asset.tagged_md5 == "m1"

    # The tagger saw a downsized JPEG, with the file context as data in the prompt.
    (msg,) = [m async for m in fake.calls[0]["prompt"]]
    blocks = msg["message"]["content"]
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert "<file_context>" in blocks[1]["text"] and "kids_building.png" in blocks[1]["text"]
    assert fake.calls[0]["options"].model == "claude-haiku-5-5"

    async with sessionmaker() as s:
        (run,) = (await s.scalars(select(AgentRun))).all()
        (ledger,) = (await s.scalars(select(SpendLedger))).all()
        hit = await s.scalar(
            select(Asset.drive_file_id).where(
                Asset.search.op("@@")(drive_index.func.plainto_tsquery("english", "classroom"))
            )
        )
    assert (run.agent, run.status, ledger.status) == ("asset_tagger", "ok", "settled")
    assert hit == "a-kids"


async def test_video_is_tagged_from_its_thumbnail(sessionmaker, settings, ready_brand, brand_id):
    tree = {
        ROOT: [
            image(
                "a-video",
                "demo.mp4",
                mime="video/mp4",
                thumbnail_link="https://lh3.googleusercontent.com/abc=s220",
            )
        ]
    }
    drive = FakeDrive(tree, content={"thumbnail": png("blue")})
    fake = FakeQuery([init_message(TAGGER_TOOLS), result_message(vision(people="none"))])
    report = await _index(sessionmaker, settings, ready_brand, brand_id, drive, query_fn=fake)
    assert report.tagging.tagged == 1
    assert drive.thumbnail_links == ["https://lh3.googleusercontent.com/abc=s1024"]


async def test_file_without_preview_is_skipped(sessionmaker, settings, ready_brand, brand_id):
    tree = {ROOT: [image("a-heic", "photo.heic", mime="image/heic")]}  # no thumbnail link
    report = await _index(sessionmaker, settings, ready_brand, brand_id, FakeDrive(tree))
    assert report.tagging.skipped == 1
    assert (await _assets(sessionmaker))["a-heic"].tagging_status == "skipped"


async def test_invalid_output_twice_marks_the_asset_failed(
    sessionmaker, settings, ready_brand, brand_id
):
    drive = FakeDrive(_one_image_bank(), content={"a-kids": png()})
    bad = vision(people="teenagers")
    fake = FakeQuery(
        [init_message(TAGGER_TOOLS), result_message(bad)],
        [init_message(TAGGER_TOOLS), result_message(bad)],
    )
    report = await _index(sessionmaker, settings, ready_brand, brand_id, drive, query_fn=fake)
    assert report.tagging.failed == 1
    asset = (await _assets(sessionmaker))["a-kids"]
    assert asset.tagging_status == "failed" and asset.people == "unknown"


async def test_run_budget_stops_tagging(sessionmaker, settings, ready_brand, brand_id):
    tiny = ready_brand.model_copy(
        update={
            "assets": ready_brand.assets.model_copy(update={"tagging_budget_usd_per_run": 0.01})
        }
    )
    drive = FakeDrive(_one_image_bank(), content={"a-kids": png()})
    fake = FakeQuery()
    report = await _index(sessionmaker, settings, tiny, brand_id, drive, query_fn=fake)
    assert (report.tagging.stopped, report.tagging.pending_left) == ("run budget", 1)
    assert fake.calls == []


async def test_daily_cap_pauses_tagging_without_spending(
    sessionmaker, settings, ready_brand, brand_id
):
    capped = ready_brand.model_copy(
        update={"budget": ready_brand.budget.model_copy(update={"claude_daily_usd": 0.01})}
    )
    drive = FakeDrive(_one_image_bank(), content={"a-kids": png()})
    fake = FakeQuery()
    report = await _index(sessionmaker, settings, capped, brand_id, drive, query_fn=fake)
    assert (report.tagging.stopped, report.tagging.pending_left) == ("daily cap", 1)
    assert fake.calls == []
    assert (await _assets(sessionmaker))["a-kids"].tagging_status == "pending"


async def test_changed_file_is_tagged_again(sessionmaker, settings, ready_brand, brand_id):
    drive = FakeDrive(_one_image_bank(), content={"a-kids": png()})
    fake = FakeQuery([init_message(TAGGER_TOOLS), result_message(vision())])
    await _index(sessionmaker, settings, ready_brand, brand_id, drive, query_fn=fake)
    edited = FakeDrive(
        {ROOT: [image("a-kids", "kids_building.png", md5="m9")]}, content={"a-kids": png("green")}
    )
    fake2 = FakeQuery([init_message(TAGGER_TOOLS), result_message(vision(people="none"))])
    report = await _index(sessionmaker, settings, ready_brand, brand_id, edited, query_fn=fake2)
    assert (report.sync.changed, report.tagging.tagged) == (1, 1)
    asset = (await _assets(sessionmaker))["a-kids"]
    assert (asset.people, asset.tagged_md5) == ("none", "m9")
