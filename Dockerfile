# One image for both Railway services. Each service sets its own start command:
#   web:    python -m app
#   worker: python -m worker
# The Claude Code CLI that the Agent SDK drives ships inside the claude-agent-sdk wheel,
# so the image needs no Node.js.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

WORKDIR /code

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# Run as an unprivileged user. Agents get a throwaway working directory under /tmp.
RUN useradd --create-home --uid 10001 app
USER app
ENV HOME=/home/app

CMD ["python", "-m", "worker"]
