"""SQLAlchemy models. Every schema change goes through a migration in db/migrations.

The tables follow "Data model" in docs/BUILD_BRIEF.md. Status columns are text with a CHECK
constraint rather than Postgres enums, so adding a state later is a one-line migration.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

USD = Numeric(12, 6)

CHANNELS_SQL = "('instagram', 'facebook', 'tiktok', 'linkedin')"


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Base(DeclarativeBase):
    pass


class AgentRun(Base):
    """One row per agent call: what ran, on which model, what it cost and what it returned.

    The row is written as `running` before the call, so a crash mid-call still leaves a trace.
    Never store prompts with secrets here; agents never receive secrets in the first place.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brand: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    run_id: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(SmallInteger, default=1)
    model: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    session_id: Mapped[str | None] = mapped_column(Text)
    output: Mapped[Any | None] = mapped_column(JSONB)
    usage: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'invalid_output', 'error')", name="agent_runs_status"
        ),
        Index("agent_runs_run_id", "run_id"),
        Index("agent_runs_created_at", "created_at"),
    )


class SpendLedger(Base):
    """Money spent or held, per paid call.

    The budget guard writes a `reserved` row before a paid call (holding the call's ceiling),
    then settles it with the actual cost. Reserved rows count against the cap until settled,
    so concurrent calls cannot overrun it, and a crash mid-call keeps its hold.
    """

    __tablename__ = "spend_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brand: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="reserved")
    reserved_usd: Mapped[Decimal] = mapped_column(USD)
    amount_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("provider IN ('anthropic', 'fal')", name="spend_ledger_provider"),
        CheckConstraint(
            "status IN ('reserved', 'settled', 'released')", name="spend_ledger_status"
        ),
        CheckConstraint("reserved_usd >= 0 AND amount_usd >= 0", name="spend_ledger_nonnegative"),
        Index("spend_ledger_window", "provider", "brand", "created_at"),
        Index("spend_ledger_run_id", "run_id"),
    )


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# --- Brands, campaigns and posts ----------------------------------------------------------------


class Brand(Base):
    """One row per brand. The worker upserts it from `brands/<slug>.yaml` on start."""

    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    config_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


CAMPAIGN_STATUSES = ("new", "planned", "in_progress", "done", "cancelled")


class Campaign(Base):
    """A brief and the Strategist's plan for it."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("brands.id"))
    name: Mapped[str] = mapped_column(Text)
    brief: Mapped[Any] = mapped_column(JSONB)
    plan: Mapped[Any | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="new")
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint(f"status IN {_in(CAMPAIGN_STATUSES)}", name="campaigns_status"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="campaigns_dates"),
    )


SLOT_STATUSES = ("planned", "assets_selected", "in_production", "done", "cancelled")


class PostSlot(Base):
    """One planned post. Its channel versions are `post_variants`."""

    __tablename__ = "post_slots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE")
    )
    theme: Mapped[str] = mapped_column(Text)
    details: Mapped[Any] = mapped_column(JSONB, default=dict, server_default=text("'{}'"))
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="planned")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint(f"status IN {_in(SLOT_STATUSES)}", name="post_slots_status"),
        Index("post_slots_campaign", "campaign_id"),
    )


class SlotAsset(Base):
    """A bank asset the Curator picked for a slot, with its one-line reason.

    Not in the brief's table list, which has nowhere to record the Curator's picks.
    """

    __tablename__ = "slot_assets"

    slot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("post_slots.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("assets.id"), primary_key=True)
    rank: Mapped[int] = mapped_column(SmallInteger)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


# The post lifecycle from the brief: main path first, then the branches.
VARIANT_STATUSES = (
    "planned",
    "assets_selected",
    "drafted",
    "rendered",
    "in_review",
    "awaiting_approval",
    "approved",
    "scheduled",
    "published",
    "measured",
    "needs_revision",
    "escalated",
    "rejected",
    "failed",
)


class PostVariant(Base):
    """One slot on one channel: the unit that moves through the post lifecycle."""

    __tablename__ = "post_variants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("post_slots.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(Text)
    format: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    call_to_action: Mapped[str | None] = mapped_column(Text)
    alt_text: Mapped[str | None] = mapped_column(Text)
    media_urls: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    status: Mapped[str] = mapped_column(Text, default="planned")
    revision_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint(f"channel IN {CHANNELS_SQL}", name="post_variants_channel"),
        CheckConstraint(f"status IN {_in(VARIANT_STATUSES)}", name="post_variants_status"),
        CheckConstraint("revision_count >= 0", name="post_variants_revisions"),
        UniqueConstraint("slot_id", "channel", name="post_variants_slot_channel"),
        Index("post_variants_status", "status"),
    )


# --- Assets -----------------------------------------------------------------------------------

ASSET_KINDS = ("image", "video")
ASSET_STATUSES = ("active", "removed")
TAGGING_STATUSES = ("pending", "done", "failed", "skipped")
# What the vision tagger saw. `unknown` until tagged. The media route and the Reviewer treat
# anything other than `none` and `adults` as possibly showing children.
PEOPLE_VALUES = ("unknown", "none", "adults", "children", "adults_and_children")
PEOPLE_WITHOUT_CHILDREN = ("none", "adults")


class Asset(Base):
    """One file from a brand's Drive asset bank.

    `tags` merges tags from the folder path and file name (`name_tags`) with the vision
    tagger's. `search` is the full-text vector over tags, description, name and folders,
    rebuilt on every write. The public URL is `PUBLIC_BASE_URL` + `/media/a/` + `public_token`:
    the token is random, so the URL cannot be guessed, and the base can move to Artec's own
    domain without touching rows.
    """

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("brands.id"))
    drive_file_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    folder_path: Mapped[str] = mapped_column(Text, default="", server_default="")
    mime_type: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    md5: Mapped[str | None] = mapped_column(Text)
    drive_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    consent_cleared: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'"))
    name_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    description: Mapped[str | None] = mapped_column(Text)
    people: Mapped[str] = mapped_column(Text, default="unknown", server_default="unknown")
    vision: Mapped[Any | None] = mapped_column(JSONB)
    tagging_status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    tagging_error: Mapped[str | None] = mapped_column(Text)
    tagged_md5: Mapped[str | None] = mapped_column(Text)
    tagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    public_token: Mapped[str] = mapped_column(Text, unique=True)
    search: Mapped[Any | None] = mapped_column(TSVECTOR)
    first_seen_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("brand_id", "drive_file_id", name="assets_brand_drive_file"),
        CheckConstraint(f"kind IN {_in(ASSET_KINDS)}", name="assets_kind"),
        CheckConstraint(f"status IN {_in(ASSET_STATUSES)}", name="assets_status"),
        CheckConstraint(f"tagging_status IN {_in(TAGGING_STATUSES)}", name="assets_tagging_status"),
        CheckConstraint(f"people IN {_in(PEOPLE_VALUES)}", name="assets_people"),
        Index("assets_search", "search", postgresql_using="gin"),
        Index("assets_tags", "tags", postgresql_using="gin"),
        Index("assets_brand_status", "brand_id", "status"),
    )


# --- Work queue -------------------------------------------------------------------------------

TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")


class Task(Base):
    """The work queue. Workers claim rows with `SELECT ... FOR UPDATE SKIP LOCKED`.

    A claimed task holds a lease (`locked_until`). If its worker dies, the lease runs out and
    another worker reclaims it, so no task is lost. `idempotency_key` is unique, so the same
    piece of work can be enqueued any number of times and still exists once.
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[Any] = mapped_column(JSONB, default=dict, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=3, server_default="3")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    result: Mapped[Any | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"status IN {_in(TASK_STATUSES)}", name="tasks_status"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="tasks_attempts"),
        Index("tasks_ready", "status", "run_after"),
        Index("tasks_kind_ref", "kind", "ref_id"),
    )


# --- Review, approval, publishing -------------------------------------------------------------


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("post_variants.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    verdict: Mapped[str] = mapped_column(Text)
    reasons: Mapped[Any] = mapped_column(JSONB, default=list, server_default=text("'[]'"))
    agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint("verdict IN ('pass', 'revise', 'escalate')", name="reviews_verdict"),
        Index("reviews_variant", "variant_id"),
    )


class Approval(Base):
    """A human decision. Publishers act only on variants with an `approved` row."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("post_variants.id", ondelete="CASCADE")
    )
    approver: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    edited_caption: Mapped[str | None] = mapped_column(Text)  # set when the approver edited it
    decided_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'rejected')", name="approvals_decision"),
        Index("approvals_variant", "variant_id"),
    )


PUBLICATION_STATUSES = ("pending", "published", "unknown", "failed", "exported")


class Publication(Base):
    """One publish per variant and channel, keyed so a post can never go out twice.

    `unknown` means the platform call timed out: check the platform before any retry.
    """

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    variant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("post_variants.id"))
    channel: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    platform_post_id: Mapped[str | None] = mapped_column(Text)
    permalink: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint(f"channel IN {CHANNELS_SQL}", name="publications_channel"),
        CheckConstraint(f"status IN {_in(PUBLICATION_STATUSES)}", name="publications_status"),
        Index("publications_variant", "variant_id"),
    )


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publications.id", ondelete="CASCADE")
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    impressions: Mapped[int | None] = mapped_column(BigInteger)
    reach: Mapped[int | None] = mapped_column(BigInteger)
    engagements: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[Any | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("publication_id", "captured_at", name="metrics_publication_time"),
    )


class ChannelToken(Base):
    """Platform credentials, encrypted at rest with TOKEN_ENCRYPTION_KEY (Phase 4)."""

    __tablename__ = "channel_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("brands.id"))
    channel: Mapped[str] = mapped_column(Text)
    token: Mapped[bytes] = mapped_column(LargeBinary)  # ciphertext, never plaintext
    refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary)  # ciphertext
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint(f"channel IN {CHANNELS_SQL}", name="channel_tokens_channel"),
        UniqueConstraint("brand_id", "channel", name="channel_tokens_brand_channel"),
    )
