from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from db.models import Asset
from services.brands import ensure_brand
from services.drive import DriveNotFound

pytestmark = pytest.mark.anyio

CONTENT = b"\x89PNG fake image bytes"


class Upstream:
    headers = {"content-length": str(len(CONTENT))}

    async def aiter_bytes(self):
        yield CONTENT[:5]
        yield CONTENT[5:]


class FakeMediaDrive:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.opened: list[str] = []
        self.closed = 0

    @asynccontextmanager
    async def open_stream(self, file_id):
        if file_id in self.missing:
            raise DriveNotFound("404")
        self.opened.append(file_id)
        try:
            yield Upstream()
        finally:
            self.closed += 1


TOKENS = {
    "product": "tok-product-0000000001",
    "kids_uncleared": "tok-kids-uncleared-001",
    "kids_cleared": "tok-kids-cleared-00001",
    "untagged": "tok-untagged-000000001",
    "removed": "tok-removed-0000000001",
    "gone_from_drive": "tok-gone-from-drive-01",
}


@pytest.fixture
async def assets(sessionmaker, brand):
    async with sessionmaker() as s, s.begin():
        brand_id = await ensure_brand(s, brand)

        def a(key, **kw):
            base = dict(
                brand_id=brand_id,
                drive_file_id=f"file-{key}",
                public_token=TOKENS[key],
                name=f"{key}.png",
                mime_type="image/png",
                kind="image",
                size_bytes=len(CONTENT),
                tagging_status="done",
                people="none",
            )
            base.update(kw)
            return Asset(**base)

        s.add_all(
            [
                a("product"),
                a("kids_uncleared", people="children"),
                a("kids_cleared", people="adults_and_children", consent_cleared=True),
                a("untagged", tagging_status="pending", people="unknown"),
                a("removed", status="removed"),
                a("gone_from_drive"),
            ]
        )


@pytest.fixture
def client(settings, assets):
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.drive = FakeMediaDrive(missing={"file-gone_from_drive"})
        yield c


async def test_safe_asset_is_streamed(client):
    r = client.get(f"/media/a/{TOKENS['product']}")
    assert r.status_code == 200
    assert r.content == CONTENT
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=3600"
    assert r.headers["x-robots-tag"] == "noindex"
    drive = client.app.state.drive
    assert drive.opened == ["file-product"] and drive.closed == 1


async def test_extension_is_allowed(client):
    assert client.get(f"/media/a/{TOKENS['product']}.png").status_code == 200


async def test_consent_cleared_children_are_served(client):
    assert client.get(f"/media/a/{TOKENS['kids_cleared']}").status_code == 200


@pytest.mark.parametrize("key", ["kids_uncleared", "untagged", "removed"])
async def test_unsafe_or_unknown_assets_are_404(client, key):
    r = client.get(f"/media/a/{TOKENS[key]}")
    assert r.status_code == 404
    assert client.app.state.drive.opened == []  # Drive is never touched


@pytest.mark.parametrize("token", ["tok-does-not-exist-0001", "short", "../../etc/passwd"])
async def test_bad_tokens_are_404(client, token):
    assert client.get(f"/media/a/{token}").status_code == 404


async def test_head_answers_from_the_index(client):
    r = client.head(f"/media/a/{TOKENS['product']}")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(CONTENT))
    assert client.app.state.drive.opened == []


async def test_file_missing_in_drive_is_404(client):
    assert client.get(f"/media/a/{TOKENS['gone_from_drive']}").status_code == 404


async def test_no_drive_credentials_means_503(settings, assets):
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.get(f"/media/a/{TOKENS['product']}").status_code == 503
