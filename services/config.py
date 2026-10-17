"""Runtime configuration.

Two sources, never mixed:
- `Settings`: environment variables (Railway variables in production, `.env` locally).
  Every secret is a `SecretStr`, so it never prints by accident.
- `BrandConfig`: `brands/<slug>.yaml`. Every brand fact lives there, never in Python.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
BRANDS_DIR = REPO_ROOT / "brands"

# Placeholder for decisions the owner has not made yet (see docs/BUILD_BRIEF.md).
CONFIRM = "<confirm>"

AGENTS = ("strategist", "curator", "copywriter", "creative", "reviewer", "analyst")
CHANNELS = ("instagram", "facebook", "tiktok", "linkedin")


class Settings(BaseSettings):
    """Process settings from the environment. Unknown variables are ignored."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    database_url: SecretStr
    brand: str = "artec"
    publishing_enabled: bool = False
    log_level: str = "INFO"
    anthropic_api_key: SecretStr | None = None

    # Approval inbox
    inbox_username: str | None = None
    inbox_password: SecretStr | None = None
    token_encryption_key: SecretStr | None = None

    # Providers (used from Phase 1 on)
    google_service_account_json: SecretStr | None = None
    fal_key: SecretStr | None = None
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    ig_user_id: str | None = None
    fb_page_id: str | None = None
    tiktok_client_key: str | None = None
    tiktok_client_secret: SecretStr | None = None
    linkedin_client_id: str | None = None
    linkedin_client_secret: SecretStr | None = None
    linkedin_org_urn: str | None = None

    # Worker
    worker_heartbeat_seconds: int = Field(default=60, ge=5)
    worker_smoke_on_boot: bool = True
    smoke_model: str = "claude-haiku-5-5"

    # Set by Railway on every deployment; None when running locally.
    railway_deployment_id: str | None = None
    railway_git_commit_sha: str | None = None
    railway_service_name: str | None = None
    railway_environment_name: str | None = None
    port: int = 8000

    @property
    def deployment_key(self) -> str:
        """Identifies this deployment; stays the same across restarts of it."""
        return self.railway_deployment_id or "local"


def secret_env_names() -> list[str]:
    """Environment variable names that hold secrets, derived from `Settings`."""
    names = []
    for name, field in Settings.model_fields.items():
        if "SecretStr" in str(field.annotation):
            names.append(name.upper())
    return names


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment


# --- Brand config -----------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Voice(_Strict):
    tone: str
    do: list[str] = []
    dont: list[str] = []


class Claims(_Strict):
    allowed: list[str] = []
    banned: list[str] = []


class ChannelConfig(_Strict):
    enabled: bool
    live: bool  # False = export mode: a ready-to-post package goes to the inbox instead
    formats: list[str]


class Creative(_Strict):
    generative_video: Literal[False]
    model_rendered_text: Literal[False]
    bank_first: Literal[True]
    fal_budget_per_run_usd: float = Field(gt=0, le=2.50)


class Assets(_Strict):
    drive_folder_id: str


class Budget(_Strict):
    claude_daily_usd: float | Literal["<confirm>"]


class Approval(_Strict):
    approvers: list[str]
    auto_approve: Literal[False]  # turning this on is the approver's call, per channel, later
    max_revisions: int = Field(ge=0, le=2)


class BrandConfig(_Strict):
    brand: str
    name: str
    category: str
    markets: list[str]
    timezone: str
    languages: list[str]
    audience: str
    voice: Voice
    claims: Claims
    channels: dict[Literal["instagram", "facebook", "tiktok", "linkedin"], ChannelConfig]
    cadence: str | dict[str, Any]
    creative: Creative
    assets: Assets
    models: dict[str, str]
    budget: Budget
    approval: Approval

    # Set by the loader, not read from the file.
    config_hash: str = ""
    unconfirmed: tuple[str, ...] = ()

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown time zone {v!r}") from e
        return v

    @field_validator("models")
    @classmethod
    def _every_agent_has_a_model(cls, v: dict[str, str]) -> dict[str, str]:
        missing = [a for a in AGENTS if a not in v]
        unknown = [a for a in v if a not in AGENTS]
        if missing or unknown:
            raise ValueError(f"models: missing {missing}, unknown {unknown}")
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def claude_daily_cap_usd(self) -> float:
        """The daily Claude API cap. Raises if the owner has not set it yet."""
        cap = self.budget.claude_daily_usd
        if cap == CONFIRM:
            raise BrandConfigError(
                f"budget.claude_daily_usd is still {CONFIRM} in brands/{self.brand}.yaml; "
                "no paid Claude call can run until it is set"
            )
        return float(cap)


class BrandConfigError(ValueError):
    pass


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _find_unconfirmed(node: Any, path: str = "") -> list[str]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _find_unconfirmed(v, f"{path}{k}.")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _find_unconfirmed(v, f"{path}{i}.")]
    return [path.rstrip(".")] if node == CONFIRM else []


def load_brand(slug: str, brands_dir: Path = BRANDS_DIR) -> BrandConfig:
    """Load and validate `brands/<slug>.yaml`. `<confirm>` values are allowed but listed."""
    if not _SLUG.match(slug):
        raise BrandConfigError(f"invalid brand slug {slug!r}")
    path = brands_dir / f"{slug}.yaml"
    if not path.is_file():
        raise BrandConfigError(f"no brand config at {path}")
    raw_bytes = path.read_bytes()
    data = yaml.safe_load(raw_bytes)
    if not isinstance(data, dict):
        raise BrandConfigError(f"{path} is not a YAML mapping")
    if data.get("brand") != slug:
        raise BrandConfigError(f"{path}: 'brand' must be {slug!r}, got {data.get('brand')!r}")
    return BrandConfig(
        **data,
        config_hash=hashlib.sha256(raw_bytes).hexdigest(),
        unconfirmed=tuple(_find_unconfirmed(data)),
    )
