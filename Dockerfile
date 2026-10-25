# syntax=docker/dockerfile:1
# One image for all SETU-X services; each container runs a different module (see compose.yaml).
# Base image pinned by digest for reproducibility (python:3.11-slim, multi-arch index digest).
ARG PYTHON_IMAGE=python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# ---- builder: resolve and build wheels once (runtime + dev extras) -------------------------
FROM base AS builder
COPY pyproject.toml README.md ./
COPY requirements ./requirements
COPY src ./src
# Optional build secret "pip_ca": a CA bundle for networks with a TLS-intercepting proxy
# (corporate networks). Not needed on a normal connection.
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip wheel --wheel-dir /wheels --constraint requirements/dev.lock ".[dev]"

# ---- runtime: minimal, non-root ------------------------------------------------------------
FROM base AS runtime
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin setu
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-index --find-links /wheels setu-x
# Default configuration baked into the image; compose.yaml mounts ./configs over it (read-only).
COPY configs ./configs
# Experiment results directory (the runner mounts the named volume "setu-results" here; a new named
# volume copies this ownership, so the non-root user can write to it).
RUN mkdir -p /app/results && chown 10001:10001 /app/results
USER 10001
EXPOSE 8000
CMD ["python", "-m", "setu_x.services.gateway"]

# ---- test: runtime + pytest + the test suite -----------------------------------------------
FROM runtime AS test
USER root
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-index --find-links /wheels "setu-x[dev]"
COPY pyproject.toml compose.yaml ./
# src/ is copied only so `ruff check src tests` can run in this image; tests import the installed package.
COPY src ./src
COPY tests ./tests
USER 10001
CMD ["pytest", "-p", "no:cacheprovider", "-m", "not e2e"]
