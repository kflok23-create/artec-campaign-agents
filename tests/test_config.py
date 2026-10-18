from pathlib import Path

import pytest
from pydantic import ValidationError

from services.config import CONFIRM, BrandConfigError, Settings, load_brand, secret_env_names


def test_artec_brand_loads(brand):
    assert brand.brand == "artec"
    assert brand.name == "Artec"
    assert brand.timezone == "Asia/Kuala_Lumpur"
    assert len(brand.config_hash) == 64
    assert brand.claude_daily_cap_usd() == 5.0
    assert set(brand.models) == {
        "strategist",
        "curator",
        "copywriter",
        "creative",
        "reviewer",
        "analyst",
        "asset_tagger",
    }
    assert brand.assets.consent_folder_names == ["consent-cleared"]
    assert not brand.assets.configured  # drive_folder_id is still <confirm>


def test_unconfirmed_values_are_listed(brand):
    # These decisions are still open in the brief; the loader must surface them.
    for path in (
        "audience",
        "voice.tone",
        "cadence",
        "assets.drive_folder_id",
        "approval.approvers.0",
    ):
        assert path in brand.unconfirmed
    assert "budget.claude_daily_usd" not in brand.unconfirmed


def _rewrite(brands_dir: Path, old: str, new: str) -> None:
    f = brands_dir / "artec.yaml"
    text = f.read_text(encoding="utf-8")
    assert old in text
    f.write_text(text.replace(old, new), encoding="utf-8")


def test_unset_claude_cap_refuses_paid_calls(brands_dir):
    _rewrite(brands_dir, "claude_daily_usd: 5.00", f'claude_daily_usd: "{CONFIRM}"')
    cfg = load_brand("artec", brands_dir)
    with pytest.raises(BrandConfigError, match="claude_daily_usd"):
        cfg.claude_daily_cap_usd()


def test_unknown_key_is_rejected(brands_dir):
    _rewrite(
        brands_dir, "category: educational puzzles", "category: educational puzzles\nslogan: x"
    )
    with pytest.raises(ValidationError, match="slogan"):
        load_brand("artec", brands_dir)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("generative_video: false", "generative_video: true"),
        ("model_rendered_text: false", "model_rendered_text: true"),
        ("fal_budget_per_run_usd: 2.50", "fal_budget_per_run_usd: 3.00"),
        ("auto_approve: false", "auto_approve: true"),
        ("max_revisions: 2", "max_revisions: 3"),
    ],
)
def test_locked_creative_and_approval_rules(brands_dir, old, new):
    _rewrite(brands_dir, old, new)
    with pytest.raises(ValidationError):
        load_brand("artec", brands_dir)


def test_every_agent_needs_a_model(brands_dir):
    _rewrite(brands_dir, "  analyst: claude-sonnet-5-5\n", "")
    with pytest.raises(ValidationError, match="analyst"):
        load_brand("artec", brands_dir)


def test_bad_timezone_rejected(brands_dir):
    _rewrite(brands_dir, "timezone: Asia/Kuala_Lumpur", "timezone: Mars/Olympus")
    with pytest.raises(ValidationError, match="time zone"):
        load_brand("artec", brands_dir)


@pytest.mark.parametrize("slug", ["../etc/passwd", "Artec", "", "a/b"])
def test_brand_slug_is_validated(slug):
    with pytest.raises(BrandConfigError):
        load_brand(slug)


def test_brand_field_must_match_file_name(brands_dir):
    (brands_dir / "other.yaml").write_text(
        (brands_dir / "artec.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(BrandConfigError, match="must be 'other'"):
        load_brand("other", brands_dir)


def test_secrets_never_print():
    s = Settings(
        _env_file=None,
        database_url="postgresql://u:hunter2-pass@h/db",
        anthropic_api_key="sk-ant-secret-xyz",
    )
    shown = repr(s) + str(s) + s.model_dump_json()
    assert "hunter2-pass" not in shown
    assert "sk-ant-secret-xyz" not in shown


def test_secret_env_names():
    names = secret_env_names()
    for n in (
        "ANTHROPIC_API_KEY",
        "DATABASE_URL",
        "FAL_KEY",
        "META_APP_SECRET",
        "TOKEN_ENCRYPTION_KEY",
    ):
        assert n in names
    for n in ("META_APP_ID", "IG_USER_ID", "BRAND"):
        assert n not in names


def test_publishing_disabled_by_default():
    assert Settings(_env_file=None, database_url="postgresql://x@h/db").publishing_enabled is False
