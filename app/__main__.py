"""Entry point: `python -m app` serves the web service on $PORT."""

from __future__ import annotations

import uvicorn

from services.config import get_settings
from services.logs import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - must listen on all interfaces inside the container
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",  # Railway's edge proxy sets X-Forwarded-*
        log_config=None,  # keep our JSON logging
    )


if __name__ == "__main__":
    main()
