import pytest

from services.pricing import cost_from_usage


def test_sonnet_list_price():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert cost_from_usage("claude-sonnet-5-5", usage) == pytest.approx(12.00)


def test_cache_multipliers():
    usage = {"cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    # Sonnet 5.5: write 1.25 x $2, read 0.1 x $2
    assert cost_from_usage("claude-sonnet-5-5", usage) == pytest.approx(2.50 + 0.20)


def test_haiku_long_prompt_tier():
    small = {"input_tokens": 100_000, "output_tokens": 1000}
    large = {"input_tokens": 100_001, "output_tokens": 1000}
    assert cost_from_usage("claude-haiku-5-5", small) == pytest.approx(0.01 + 0.0005)
    assert cost_from_usage("claude-haiku-5-5", large) == pytest.approx(100_001 * 0.5e-6 + 0.0025)


def test_unknown_model_or_no_usage():
    assert cost_from_usage("claude-unknown", {"input_tokens": 10}) is None
    assert cost_from_usage("claude-sonnet-5-5", None) is None
