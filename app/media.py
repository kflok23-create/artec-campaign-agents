"""Public media URLs for bank assets: `/media/a/<token>`, streamed from Drive.

Instagram and TikTok fetch media from a public HTTPS URL, so assets need one. The token is
random (from the indexer), so URLs cannot be guessed or listed, and Drive sharing never changes.

An asset is served only when it is safe to be public: it has been tagged, and it either shows no
children or is consent-cleared. Anything else, including assets not yet tagged, is a 404, the
same answer as an unknown token, so the response reveals nothing.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from db.models import PEOPLE_WITHOUT_CHILDREN, Asset
from services.drive import DriveError, DriveNotFound

log = logging.getLogger("web.media")

router = APIRouter()

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
CACHE_CONTROL = "public, max-age=3600"


def servable(asset: Asset) -> bool:
    if asset.status != "active" or asset.tagging_status != "done":
        return False
    return asset.consent_cleared or asset.people in PEOPLE_WITHOUT_CHILDREN


@router.api_route("/media/a/{token}", methods=["GET", "HEAD"])
async def asset_media(token: str, request: Request) -> Response:
    token = token.split(".", 1)[0]  # an extension, as in <token>.jpg, is allowed and ignored
    if not _TOKEN.match(token):
        raise HTTPException(status_code=404)
    async with request.app.state.sessionmaker() as session:
        asset = await session.scalar(select(Asset).where(Asset.public_token == token))
    if asset is None or not servable(asset):
        raise HTTPException(status_code=404)

    headers = {"Cache-Control": CACHE_CONTROL, "X-Robots-Tag": "noindex"}
    if request.method == "HEAD":
        if asset.size_bytes:
            headers["Content-Length"] = str(asset.size_bytes)
        return Response(media_type=asset.mime_type, headers=headers)

    drive = request.app.state.drive
    if drive is None:
        raise HTTPException(status_code=503, detail="media is not configured")
    stream = drive.open_stream(asset.drive_file_id)
    try:
        upstream = await stream.__aenter__()
    except DriveNotFound:
        log.warning("asset missing in Drive", extra={"ctx": {"asset_id": asset.id}})
        raise HTTPException(status_code=404) from None
    except DriveError as e:
        log.warning(
            "Drive error serving media", extra={"ctx": {"asset_id": asset.id, "error": str(e)}}
        )
        raise HTTPException(status_code=502) from None
    if length := upstream.headers.get("content-length"):
        headers["Content-Length"] = length

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await stream.__aexit__(None, None, None)

    return StreamingResponse(body(), media_type=asset.mime_type, headers=headers)
