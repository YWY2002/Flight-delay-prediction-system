# Ingestion service image: the poller loop only.
#
# Build:  docker build -t flight-delay-ingest .
# Run:    docker run --init --env-file .env -v fdp-data:/data flight-delay-ingest
#
# For a Graviton EC2 instance (t4g.*), build with --platform linux/arm64.

# ---------------------------------------------------------------------------
# Stage 1: resolve and install dependencies into a self-contained virtualenv.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Bytecode is compiled at build time so the first poll does not pay for it, and
# packages are copied rather than hardlinked because the cache mount below lives
# on a different filesystem than /app.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# git, because `opensky-api` is not resolved from PyPI. pyproject.toml pins it
# to a GitHub checkout via [tool.uv.sources], so uv shells out to `git clone` to
# build it, and the uv base image ships no git binary. Installed before the
# dependency layer so it stays cached across lockfile edits. Builder-stage only:
# the runtime image gets the built venv, never the source, so it needs no git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pyspark alone is 496 MB, polars another 193 MB, and neither is imported by
# anything under src/ -- they are there for notebook analysis and the future
# silver/gold jobs, not for the poller. duckdb is the same story. Excluding
# them here keeps the runtime image roughly a tenth of the size and avoids
# shipping a JVM-dependent package with no JVM. The tidier fix is to move all
# three into an optional extra in pyproject.toml; this flag does the same job
# without changing the dependency contract for everyone else.
ARG EXCLUDE="--no-install-package pyspark --no-install-package polars --no-install-package duckdb"

# Dependencies first, in their own layer keyed on the lockfile, so editing
# source does not re-resolve or re-download anything. --frozen makes the build
# fail rather than silently drift if uv.lock and pyproject.toml disagree.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev ${EXCLUDE}

# Then the project itself. --no-editable installs a real copy into the venv so
# the runtime stage does not need /app/src to exist at all.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable ${EXCLUDE}

# ---------------------------------------------------------------------------
# Stage 2: runtime. No uv, no build tooling, no source tree.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Unbuffered so `docker logs` shows a poll the moment it happens rather than
# when a 4 KB buffer fills, which at one line every three minutes is a long
# time to stare at nothing.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Reference data the service reads at startup, and where it writes.
#
# Both are set explicitly rather than left to the defaults in config.py, which
# derive from `Path(__file__).parents[3]`. That resolves to the repo root for a
# source checkout, but for an installed package it lands inside site-packages
# and the paths point at directories that do not exist. Setting the env vars
# makes the container independent of how the package was installed.
ENV FDP_AIRPORTS_FILE=/app/config/airports.toml \
    FDP_DATA_DIR=/data \
    FDP_LOG_JSON=true

# Non-root: the process only needs to read its own code and write /data.
RUN groupadd --system --gid 1001 ingest \
    && useradd --system --uid 1001 --gid ingest --no-create-home ingest \
    && mkdir -p /data \
    && chown -R ingest:ingest /data

WORKDIR /app
COPY --from=builder --chown=ingest:ingest /app/.venv /app/.venv
COPY --chown=ingest:ingest config ./config

USER ingest

# Bronze output. Mount a named volume or an EBS-backed host path here: without
# one, every container restart starts the day file over.
VOLUME ["/data"]

# Exec form, so python is PID 1 and receives SIGTERM directly. The shell form
# would put /bin/sh at PID 1, which does not forward signals, and `docker stop`
# would hit the 10 second timeout and SIGKILL mid-write instead of letting the
# SIGTERM handler in main_poller set its stop event. Run with --init (or ECS,
# which does this for you) to get a real init process reaping any strays.
CMD ["python", "-m", "flight_delay.data_ingestion.main_poller"]
