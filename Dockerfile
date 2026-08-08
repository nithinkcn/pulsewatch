# Multi-stage: dependencies resolve in the builder, the runtime image carries
# only the virtualenv and the source. Keeps uv, compilers and build caches out
# of what actually ships.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Dependency metadata first: this layer is cached until pyproject.toml itself
# changes, so editing application code does not reinstall the world.
COPY pyproject.toml README.md ./
RUN uv venv /opt/venv && uv pip install --python /opt/venv/bin/python .

COPY app ./app
RUN uv pip install --python /opt/venv/bin/python --no-deps .


FROM python:3.13-slim AS runtime

# curl is used by the compose healthcheck; nothing else is added.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root. The application code directory stays read-only; Celery Beat is the
# one component that needs to persist state (its schedule file), so it gets an
# explicit writable directory rather than write access to /app.
RUN useradd --create-home --uid 10001 pulsewatch \
    && install -d -o pulsewatch -g pulsewatch /var/lib/pulsewatch

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=pulsewatch:pulsewatch app ./app
COPY --chown=pulsewatch:pulsewatch alembic ./alembic
COPY --chown=pulsewatch:pulsewatch alembic.ini ./

USER pulsewatch

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
