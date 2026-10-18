"""Phase 1: the full data model (brands to channel_tokens) and the agent_runs -> tasks link.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'cancelled')", name="tasks_status"
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="tasks_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("tasks_kind_ref", "tasks", ["kind", "ref_id"], unique=False)
    op.create_index("tasks_ready", "tasks", ["status", "run_after"], unique=False)
    op.create_table(
        "assets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("brand_id", sa.BigInteger(), nullable=False),
        sa.Column("drive_file_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("folder_path", sa.Text(), server_default="", nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("md5", sa.Text(), nullable=True),
        sa.Column("drive_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("consent_cleared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "name_tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("people", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("vision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tagging_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("tagging_error", sa.Text(), nullable=True),
        sa.Column("tagged_md5", sa.Text(), nullable=True),
        sa.Column("tagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("public_token", sa.Text(), nullable=False),
        sa.Column("search", postgresql.TSVECTOR(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('image', 'video')", name="assets_kind"),
        sa.CheckConstraint(
            "people IN ('unknown', 'none', 'adults', 'children', 'adults_and_children')",
            name="assets_people",
        ),
        sa.CheckConstraint("status IN ('active', 'removed')", name="assets_status"),
        sa.CheckConstraint(
            "tagging_status IN ('pending', 'done', 'failed', 'skipped')",
            name="assets_tagging_status",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brands.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brand_id", "drive_file_id", name="assets_brand_drive_file"),
        sa.UniqueConstraint("public_token"),
    )
    op.create_index("assets_brand_status", "assets", ["brand_id", "status"], unique=False)
    op.create_index("assets_search", "assets", ["search"], unique=False, postgresql_using="gin")
    op.create_index("assets_tags", "assets", ["tags"], unique=False, postgresql_using="gin")
    op.create_table(
        "campaigns",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("brand_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("brief", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('new', 'planned', 'in_progress', 'done', 'cancelled')",
            name="campaigns_status",
        ),
        sa.CheckConstraint("end_date IS NULL OR end_date >= start_date", name="campaigns_dates"),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brands.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "channel_tokens",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("brand_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("token", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column(
            "scopes", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('instagram', 'facebook', 'tiktok', 'linkedin')",
            name="channel_tokens_channel",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brands.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brand_id", "channel", name="channel_tokens_brand_channel"),
    )
    op.create_table(
        "post_slots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'assets_selected', 'in_production', 'done', 'cancelled')",
            name="post_slots_status",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("post_slots_campaign", "post_slots", ["campaign_id"], unique=False)
    op.create_table(
        "post_variants",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("slot_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column(
            "hashtags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("call_to_action", sa.Text(), nullable=True),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column(
            "media_urls",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("revision_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('instagram', 'facebook', 'tiktok', 'linkedin')",
            name="post_variants_channel",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'assets_selected', 'drafted', 'rendered', 'in_review', "
            "'awaiting_approval', 'approved', 'scheduled', 'published', 'measured', "
            "'needs_revision', 'escalated', 'rejected', 'failed')",
            name="post_variants_status",
        ),
        sa.CheckConstraint("revision_count >= 0", name="post_variants_revisions"),
        sa.ForeignKeyConstraint(["slot_id"], ["post_slots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slot_id", "channel", name="post_variants_slot_channel"),
    )
    op.create_index("post_variants_status", "post_variants", ["status"], unique=False)
    op.create_table(
        "slot_assets",
        sa.Column("slot_id", sa.BigInteger(), nullable=False),
        sa.Column("asset_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
        ),
        sa.ForeignKeyConstraint(["slot_id"], ["post_slots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("slot_id", "asset_id"),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("variant_id", sa.BigInteger(), nullable=False),
        sa.Column("approver", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("edited_caption", sa.Text(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="approvals_decision"),
        sa.ForeignKeyConstraint(["variant_id"], ["post_variants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("approvals_variant", "approvals", ["variant_id"], unique=False)
    op.create_table(
        "publications",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("variant_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("platform_post_id", sa.Text(), nullable=True),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('instagram', 'facebook', 'tiktok', 'linkedin')",
            name="publications_channel",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'unknown', 'failed', 'exported')",
            name="publications_status",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["post_variants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("publications_variant", "publications", ["variant_id"], unique=False)
    op.create_table(
        "reviews",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("variant_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("verdict IN ('pass', 'revise', 'escalate')", name="reviews_verdict"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["variant_id"], ["post_variants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("reviews_variant", "reviews", ["variant_id"], unique=False)
    op.create_table(
        "metrics",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("publication_id", sa.BigInteger(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("reach", sa.BigInteger(), nullable=True),
        sa.Column("engagements", sa.BigInteger(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("publication_id", "captured_at", name="metrics_publication_time"),
    )
    op.create_foreign_key(
        "agent_runs_task_id_fkey", "agent_runs", "tasks", ["task_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("agent_runs_task_id_fkey", "agent_runs", type_="foreignkey")
    op.drop_table("metrics")
    op.drop_index("reviews_variant", table_name="reviews")
    op.drop_table("reviews")
    op.drop_index("publications_variant", table_name="publications")
    op.drop_table("publications")
    op.drop_index("approvals_variant", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("slot_assets")
    op.drop_index("post_variants_status", table_name="post_variants")
    op.drop_table("post_variants")
    op.drop_index("post_slots_campaign", table_name="post_slots")
    op.drop_table("post_slots")
    op.drop_table("channel_tokens")
    op.drop_table("campaigns")
    op.drop_index("assets_tags", table_name="assets", postgresql_using="gin")
    op.drop_index("assets_search", table_name="assets", postgresql_using="gin")
    op.drop_index("assets_brand_status", table_name="assets")
    op.drop_table("assets")
    op.drop_index("tasks_ready", table_name="tasks")
    op.drop_index("tasks_kind_ref", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("brands")
