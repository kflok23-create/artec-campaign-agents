"""A small read-only Google Drive v3 client for the asset bank (async, over httpx).

It lists folders, reads file metadata and downloads content: nothing here can change Drive.
Rate limits and server errors are retried with exponential backoff; an expired token is
refreshed once. API reference: https://developers.google.com/workspace/drive/api/reference/rest/v3
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import anyio
import httpx

from services.google_auth import DRIVE_READONLY, TokenSource, parse_service_account

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FILE_FIELDS = (
    "id,name,mimeType,md5Checksum,modifiedTime,size,description,thumbnailLink,driveId,"
    "imageMediaMetadata(width,height,rotation),videoMediaMetadata(width,height,durationMillis)"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
# Drive file IDs are URL-safe base64-like strings. Anything else never reaches a query string.
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


class DriveError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DriveNotFound(DriveError):
    """404: wrong ID, or the file is not shared with the service account."""


class FileTooLarge(DriveError):
    pass


def check_file_id(file_id: str) -> str:
    if not _FILE_ID.match(file_id):
        raise DriveError(f"not a Drive file ID: {file_id!r}")
    return file_id


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    md5: str | None = None
    modified_time: datetime | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    thumbnail_link: str | None = None
    description: str | None = None
    drive_id: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> DriveFile:
        image = d.get("imageMediaMetadata") or {}
        video = d.get("videoMediaMetadata") or {}
        width = image.get("width") or video.get("width")
        height = image.get("height") or video.get("height")
        if image.get("rotation") in (1, 3):  # quarter turns: the image is shown the other way up
            width, height = height, width
        modified = d.get("modifiedTime")
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            mime_type=d.get("mimeType", ""),
            md5=d.get("md5Checksum"),
            modified_time=datetime.fromisoformat(modified) if modified else None,
            size=int(d["size"]) if d.get("size") else None,
            width=int(width) if width else None,
            height=int(height) if height else None,
            duration_ms=int(video["durationMillis"]) if video.get("durationMillis") else None,
            thumbnail_link=d.get("thumbnailLink"),
            description=d.get("description"),
            drive_id=d.get("driveId"),
        )


class DriveClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        *,
        max_retries: int = 4,
        sleep: Callable[[float], Awaitable[Any]] = anyio.sleep,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._max_retries = max_retries
        self._sleep = sleep

    @property
    def account_email(self) -> str:
        return self._tokens.email

    async def _send(self, method: str, url: str, *, stream: bool = False, **kw) -> httpx.Response:
        refreshed = False
        for attempt in range(self._max_retries + 1):
            headers = {"Authorization": f"Bearer {await self._tokens.token()}"}
            request = self._http.build_request(method, url, headers=headers, **kw)
            try:
                response = await self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise DriveError(f"Drive unreachable: {type(e).__name__}") from e
                await self._backoff(attempt, f"transport {type(e).__name__}")
                continue
            if response.status_code < 400:
                return response
            if stream:
                await response.aread()
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self._tokens.invalidate()
                continue
            if self._retryable(response) and attempt < self._max_retries:
                await self._backoff(attempt, f"HTTP {response.status_code}")
                continue
            raise self._error(response)
        raise DriveError("Drive request kept failing")  # pragma: no cover - loop always returns

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            errors = response.json()["error"]["errors"]
            return errors[0].get("reason", "")
        except (ValueError, KeyError, IndexError, TypeError):
            return ""

    def _retryable(self, response: httpx.Response) -> bool:
        if response.status_code in RETRY_STATUSES:
            return True
        return response.status_code == 403 and self._reason(response) in RATE_LIMIT_REASONS

    def _error(self, response: httpx.Response) -> DriveError:
        reason = self._reason(response)
        message = f"Drive returned HTTP {response.status_code} {reason}".strip()
        if response.status_code == 404:
            return DriveNotFound(message, response.status_code)
        return DriveError(message, response.status_code)

    async def _backoff(self, attempt: int, why: str) -> None:
        delay = min(2**attempt, 30) + random.uniform(0, 1)  # noqa: S311 - jitter, not crypto
        log.info("Drive request retry", extra={"ctx": {"why": why, "delay_s": round(delay, 1)}})
        await self._sleep(delay)

    async def get(self, file_id: str) -> DriveFile:
        response = await self._send(
            "GET",
            f"{API}/files/{check_file_id(file_id)}",
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return DriveFile.from_api(response.json())

    async def list_children(
        self, folder_id: str, *, drive_id: str | None = None
    ) -> AsyncIterator[DriveFile]:
        """Every non-trashed item directly inside a folder, page by page."""
        params: dict[str, Any] = {
            "q": f"'{check_file_id(folder_id)}' in parents and trashed = false",
            "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if drive_id:
            params.update(corpora="drive", driveId=check_file_id(drive_id))
        while True:
            body = (await self._send("GET", f"{API}/files", params=params)).json()
            if body.get("incompleteSearch"):
                raise DriveError(f"Drive returned an incomplete listing for folder {folder_id}")
            for item in body.get("files", []):
                yield DriveFile.from_api(item)
            token = body.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    async def download(self, file_id: str, *, max_bytes: int) -> bytes:
        url = f"{API}/files/{check_file_id(file_id)}"
        params = {"alt": "media", "supportsAllDrives": "true"}
        return await self._read_capped(url, max_bytes, params=params)

    async def thumbnail(self, link: str, *, max_bytes: int) -> bytes:
        """Fetch a file's Drive thumbnail (for videos and formats Pillow cannot open)."""
        if not link.startswith("https://"):
            raise DriveError("thumbnail link is not HTTPS")
        return await self._read_capped(link, max_bytes)

    async def _read_capped(self, url: str, max_bytes: int, **kw) -> bytes:
        response = await self._send("GET", url, stream=True, **kw)
        try:
            chunks, total = [], 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLarge(f"file is over {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            await response.aclose()

    @asynccontextmanager
    async def open_stream(self, file_id: str) -> AsyncIterator[httpx.Response]:
        """Open a file's content as a stream (for the media route). Close by leaving the block."""
        url = f"{API}/files/{check_file_id(file_id)}"
        response = await self._send(
            "GET", url, stream=True, params={"alt": "media", "supportsAllDrives": "true"}
        )
        try:
            yield response
        finally:
            await response.aclose()


@asynccontextmanager
async def drive_client(service_account_json: str) -> AsyncIterator[DriveClient]:
    """A DriveClient with its own HTTP connection pool, closed when the block ends."""
    info = parse_service_account(service_account_json)
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        yield DriveClient(http, TokenSource(info, [DRIVE_READONLY], http))
