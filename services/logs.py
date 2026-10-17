"""JSON logging with secret redaction.

Railway parses one JSON object per line and reads `message` and `level`.
Any value of a secret environment variable is replaced before a line is written,
including inside tracebacks, so a secret cannot reach the logs by accident.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from services.config import secret_env_names

REDACTED = "[redacted]"
_MIN_SECRET_LEN = 6  # shorter values would redact ordinary words


def _secret_values() -> list[str]:
    values = []
    for name in secret_env_names():
        value = os.environ.get(name, "")
        if len(value) >= _MIN_SECRET_LEN:
            values.append(value)
    # The database URL embeds a password; redact the password on its own too.
    db_url = os.environ.get("DATABASE_URL", "")
    if "@" in db_url and ":" in db_url.split("@", 1)[0]:
        password = db_url.split("@", 1)[0].rsplit(":", 1)[-1]
        if len(password) >= _MIN_SECRET_LEN:
            values.append(password)
    # Longest first, so a secret that contains another is removed whole.
    return sorted(set(values), key=len, reverse=True)


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = _secret_values() if secrets is None else secrets

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            entry.update(ctx)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        for secret in self._secrets:
            line = line.replace(secret, REDACTED)
            # json.dumps may have escaped characters inside the secret.
            escaped = json.dumps(secret)[1:-1]
            if escaped != secret:
                line = line.replace(escaped, REDACTED)
        return line


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # The SDK logs at DEBUG; keep third-party noise at WARNING unless asked.
    for noisy in ("httpx", "asyncio", "claude_agent_sdk", "mcp"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def ctx(**fields: Any) -> dict[str, Any]:
    """Structured fields for a log call: `log.info("msg", extra=ctx(run_id=...))`."""
    return {"ctx": fields}
