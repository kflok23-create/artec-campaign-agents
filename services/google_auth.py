"""Access tokens for a Google service account (OAuth 2.0 JWT-bearer flow).

google-auth signs the assertion; the token request goes over httpx, so nothing blocks the event
loop. Flow: https://developers.google.com/identity/protocols/oauth2/service-account

The key comes from GOOGLE_SERVICE_ACCOUNT_JSON (base64 or raw JSON). Errors never include the
key or a token: only the HTTP status and Google's error code.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable
from typing import Any

import anyio
import httpx
from google.auth import crypt, jwt

DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TOKEN_LIFETIME_SECONDS = 3600
REFRESH_MARGIN_SECONDS = 300
_REQUIRED = ("client_email", "private_key", "private_key_id", "token_uri")


class GoogleAuthError(RuntimeError):
    pass


def parse_service_account(raw: str) -> dict[str, Any]:
    """Decode the service account key from base64 or raw JSON, and check it is one."""
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as e:
            raise GoogleAuthError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is neither JSON nor base64 JSON"
            ) from e
    try:
        info = json.loads(text)
    except json.JSONDecodeError as e:
        raise GoogleAuthError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from e
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise GoogleAuthError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service account key")
    missing = [k for k in _REQUIRED if not info.get(k)]
    if missing:
        raise GoogleAuthError(f"service account key is missing {missing}")
    return info


class TokenSource:
    """Hands out a cached access token, fetching a new one shortly before it expires."""

    def __init__(
        self,
        info: dict[str, Any],
        scopes: list[str],
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._info = info
        self._scopes = scopes
        self._http = http
        self._clock = clock
        self._signer = crypt.RSASigner.from_service_account_info(info)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = anyio.Lock()

    @property
    def email(self) -> str:
        return self._info["client_email"]

    def invalidate(self) -> None:
        self._token = None

    async def token(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() > self._expires_at - REFRESH_MARGIN_SECONDS:
                await self._fetch()
            assert self._token is not None
            return self._token

    async def _fetch(self) -> None:
        now = int(self._clock())
        claims = {
            "iss": self.email,
            "scope": " ".join(self._scopes),
            "aud": self._info["token_uri"],
            "iat": now,
            "exp": now + TOKEN_LIFETIME_SECONDS,
        }
        assertion = jwt.encode(self._signer, claims, key_id=self._info["private_key_id"])
        try:
            response = await self._http.post(
                self._info["token_uri"],
                data={"grant_type": JWT_BEARER, "assertion": assertion.decode("ascii")},
            )
        except httpx.HTTPError as e:
            raise GoogleAuthError(f"token request failed: {type(e).__name__}") from e
        if response.status_code != 200:
            code = _error_code(response)
            raise GoogleAuthError(f"token request refused: HTTP {response.status_code} {code}")
        data = response.json()
        self._token = data["access_token"]
        self._expires_at = now + int(data.get("expires_in", TOKEN_LIFETIME_SECONDS))


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) else ""
