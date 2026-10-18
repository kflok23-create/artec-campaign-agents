import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt

from services.drive import (
    DriveClient,
    DriveError,
    DriveFile,
    DriveNotFound,
    FileTooLarge,
    check_file_id,
)
from services.google_auth import GoogleAuthError, TokenSource, parse_service_account

pytestmark = pytest.mark.anyio

TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER = "1AbCdEfGhIjKlMnOpQrStUv"


@pytest.fixture(scope="module")
def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return pem, public


@pytest.fixture
def sa_info(key_pair):
    return {
        "type": "service_account",
        "client_email": "asset-indexer@artec-test.iam.gserviceaccount.com",
        "private_key": key_pair[0],
        "private_key_id": "kid-123",
        "token_uri": TOKEN_URI,
    }


class FakeGoogle:
    """Token endpoint plus scripted Drive responses, recording every request."""

    def __init__(self, drive_responses=None, *, token_status=200):
        self.drive_responses = list(drive_responses or [])
        self.token_status = token_status
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URI:
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            self.tokens_issued += 1
            return httpx.Response(
                200, json={"access_token": f"tok-{self.tokens_issued}", "expires_in": 3600}
            )
        return self.drive_responses.pop(0)

    def drive_requests(self):
        return [r for r in self.requests if str(r.url) != TOKEN_URI]


def _client(sa_info, fake, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    tokens = TokenSource(sa_info, ["https://www.googleapis.com/auth/drive.readonly"], http, **kw)

    async def no_sleep(_):
        return None

    return DriveClient(http, tokens, sleep=no_sleep), tokens


# --- Service account key ------------------------------------------------------------------------


def test_key_accepted_as_raw_or_base64_json(sa_info):
    raw = json.dumps(sa_info)
    assert parse_service_account(raw)["client_email"] == sa_info["client_email"]
    b64 = base64.b64encode(raw.encode()).decode()
    assert parse_service_account(b64)["private_key_id"] == "kid-123"


@pytest.mark.parametrize(
    "raw, match",
    [
        ("not json or base64 !!", "neither JSON nor base64"),
        ('{"type": "authorized_user"}', "not a service account"),
        ('{"type": "service_account", "client_email": "x"}', "missing"),
    ],
)
def test_bad_keys_are_rejected_without_echoing_them(raw, match):
    with pytest.raises(GoogleAuthError, match=match) as e:
        parse_service_account(raw)
    assert 'client_email": "x' not in str(e.value)


# --- Tokens ----------------------------------------------------------------------------------


async def test_token_request_is_a_signed_jwt_bearer_grant(sa_info, key_pair):
    fake = FakeGoogle()
    now = int(time.time())
    _, tokens = _client(sa_info, fake, clock=lambda: now)
    assert await tokens.token() == "tok-1"
    (req,) = fake.requests
    form = parse_qs(req.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(form["assertion"][0], certs=key_pair[1], audience=TOKEN_URI)
    assert claims["iss"] == sa_info["client_email"]
    assert claims["scope"] == "https://www.googleapis.com/auth/drive.readonly"
    assert (claims["iat"], claims["exp"]) == (now, now + 3600)


async def test_token_is_cached_until_near_expiry(sa_info):
    now = [1_000_000.0]
    fake = FakeGoogle()
    _, tokens = _client(sa_info, fake, clock=lambda: now[0])
    assert await tokens.token() == "tok-1"
    now[0] += 3000  # 10 minutes left: still fine
    assert await tokens.token() == "tok-1"
    now[0] += 400  # under 5 minutes left: refresh
    assert await tokens.token() == "tok-2"


async def test_refused_token_error_has_no_secrets(sa_info):
    fake = FakeGoogle(token_status=400)
    _, tokens = _client(sa_info, fake)
    with pytest.raises(GoogleAuthError) as e:
        await tokens.token()
    assert str(e.value) == "token request refused: HTTP 400 invalid_grant"
    assert "PRIVATE KEY" not in str(e.value)


# --- Drive -----------------------------------------------------------------------------------


def _page(files, token=None, incomplete=False):
    body = {"files": files, "incompleteSearch": incomplete}
    if token:
        body["nextPageToken"] = token
    return httpx.Response(200, json=body)


async def test_list_children_pages_through_results(sa_info):
    fake = FakeGoogle(
        [
            _page([{"id": "a" * 12, "name": "one.jpg", "mimeType": "image/jpeg"}], token="p2"),
            _page([{"id": "b" * 12, "name": "two.png", "mimeType": "image/png"}]),
        ]
    )
    drive, _ = _client(sa_info, fake)
    names = [f.name async for f in drive.list_children(FOLDER, drive_id="0ADriveIdXyz")]
    assert names == ["one.jpg", "two.png"]
    first, second = fake.drive_requests()
    assert first.url.params["q"] == f"'{FOLDER}' in parents and trashed = false"
    assert first.url.params["corpora"] == "drive"
    assert first.url.params["supportsAllDrives"] == "true"
    assert first.headers["Authorization"] == "Bearer tok-1"
    assert second.url.params["pageToken"] == "p2"


async def test_incomplete_listing_is_an_error(sa_info):
    drive, _ = _client(sa_info, FakeGoogle([_page([], incomplete=True)]))
    with pytest.raises(DriveError, match="incomplete"):
        [f async for f in drive.list_children(FOLDER)]


def test_ids_with_query_syntax_are_refused():
    with pytest.raises(DriveError):
        check_file_id("abc' or name contains 'x")


async def test_rate_limits_and_server_errors_are_retried(sa_info):
    rate_limited = httpx.Response(
        403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
    )
    fake = FakeGoogle(
        [
            httpx.Response(429),
            rate_limited,
            httpx.Response(503),
            httpx.Response(200, json={"id": FOLDER, "name": "Bank", "mimeType": FOLDER_MIME}),
        ]
    )
    drive, _ = _client(sa_info, fake)
    assert (await drive.get(FOLDER)).is_folder
    assert len(fake.drive_requests()) == 4


async def test_permission_errors_are_not_retried(sa_info):
    forbidden = httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    fake = FakeGoogle([forbidden])
    drive, _ = _client(sa_info, fake)
    with pytest.raises(DriveError, match="403 forbidden"):
        await drive.get(FOLDER)
    assert len(fake.drive_requests()) == 1


async def test_not_found_means_not_shared(sa_info):
    drive, _ = _client(sa_info, FakeGoogle([httpx.Response(404)]))
    with pytest.raises(DriveNotFound):
        await drive.get(FOLDER)


async def test_expired_token_is_refreshed_once(sa_info):
    ok = httpx.Response(200, json={"id": FOLDER, "name": "Bank", "mimeType": FOLDER_MIME})
    fake = FakeGoogle([httpx.Response(401), ok])
    drive, _ = _client(sa_info, fake)
    await drive.get(FOLDER)
    assert [r.headers["Authorization"] for r in fake.drive_requests()] == [
        "Bearer tok-1",
        "Bearer tok-2",
    ]


async def test_download_is_capped(sa_info):
    fake = FakeGoogle([httpx.Response(200, content=b"x" * 100), httpx.Response(200, content=b"y")])
    drive, _ = _client(sa_info, fake)
    with pytest.raises(FileTooLarge):
        await drive.download(FOLDER, max_bytes=50)
    assert await drive.download(FOLDER, max_bytes=50) == b"y"
    assert fake.drive_requests()[0].url.params["alt"] == "media"


def test_rotated_images_swap_width_and_height():
    f = DriveFile.from_api(
        {
            "id": "c" * 12,
            "name": "p.jpg",
            "mimeType": "image/jpeg",
            "size": "2048",
            "modifiedTime": "2026-10-01T03:04:05.000Z",
            "imageMediaMetadata": {"width": 4000, "height": 3000, "rotation": 1},
        }
    )
    assert (f.width, f.height, f.size) == (3000, 4000, 2048)
    assert f.modified_time.year == 2026


FOLDER_MIME = "application/vnd.google-apps.folder"
