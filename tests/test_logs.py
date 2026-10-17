import json
import logging

from services.logs import REDACTED, JsonFormatter, _secret_values, ctx


def _record(msg: str, **extra) -> logging.LogRecord:
    logger = logging.getLogger("t")
    return logger.makeRecord("t", logging.INFO, __file__, 1, msg, (), None, extra=extra)


def test_json_shape_for_railway():
    line = JsonFormatter(secrets=[]).format(_record("hello", **ctx(run_id="r1")))
    entry = json.loads(line)
    assert entry["message"] == "hello"
    assert entry["level"] == "info"
    assert entry["run_id"] == "r1"


def test_secret_redacted_in_message_fields_and_traceback():
    secret = "sk-ant-api03-SUPERSECRET"
    fmt = JsonFormatter(secrets=[secret])
    try:
        raise ValueError(f"bad key {secret}")
    except ValueError:
        import sys

        rec = _record(f"using {secret}", **ctx(key=secret))
        rec.exc_info = sys.exc_info()
    line = fmt.format(rec)
    assert secret not in line
    assert line.count(REDACTED) >= 3
    json.loads(line)  # still valid JSON


def test_secret_values_come_from_secret_env_vars(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-secret-value-123")
    monkeypatch.setenv("META_APP_ID", "1234567890")  # an ID, not a secret
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:db-password-42@host:5432/db")
    values = _secret_values()
    assert "fal-secret-value-123" in values
    assert "db-password-42" in values
    assert "1234567890" not in values
