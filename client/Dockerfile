# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for orcaster-worker.
# Build context: client/ (repository root for the worker package).

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------
# Single reference to the `uv` image, pinned by version and digest: with
# `:latest`, two builds on different days could pull a different `uv`, and the
# change would reach the worker without review. The two stages below copy from
# this stage. Bumping the version is a reviewed change (Dependabot `docker`
# proposes the version+digest pair); `client/tests/test_dockerfile_pins.py`
# requires the format `:<X.Y.Z>@sha256:<digest>` and a single reference.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM docker.io/library/python:3.13-slim-bookworm AS builder

# Install uv (version pinned in the `uv` stage).
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /build

# Copy lockfile and manifest first for layer caching.
COPY pyproject.toml uv.lock ./
COPY README.pypi.md ./
COPY src/ ./src/

# Sync production dependencies into a virtual environment.
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM docker.io/library/python:3.13-slim-bookworm

# `uv` to create ephemeral per-task venvs. The builder stage already has it,
# but `COPY --from=builder` only copies `/build/.venv`; fetching it again here
# from the official image keeps the runtime self-contained.
COPY --from=uv /uv /usr/local/bin/uv

ENV PATH="/app/.venv/bin:${PATH}"

# Basic Unix tools for user tasks:
#   - curl + ca-certificates: HTTPS downloads from tasks.
#   - coreutils: sha256sum and other standard utilities (the slim image ships
#     a reduced busybox-like subset; coreutils guarantees sha256sum, timeout,
#     stdbuf, etc.).
#   - git: clone repositories in build/deploy tasks.
#   - g++: a task dependency that only publishes an sdist (no wheel for the
#     worker's platform) and carries a C/C++ extension needs a compiler to
#     install. The Python headers already ship in the official image
#     (`/usr/local/include/python3.13`), so the compiler is enough. `g++`
#     pulls in `gcc`, so it also covers pure C extensions. This avoids
#     depending on prebuilt wheels hosted outside a public index, which break
#     when the Python version or the worker architecture changes.
# Keep the set minimal: adding to it means reviewing the attack surface. `g++`
# is the one notable exception (build toolchain): justified because without it
# tasks cannot install dependencies with native extensions that have no wheel.
# `APT::Cache-Start=100000000`: the default (24 MB) overflows under FreeBSD's
# linuxulator when parsing bookworm's Packages file (~8.8 MB, ~50 MB expanded)
# and breaks the build with `Dynamic MMap ran out of room`. It is set through
# a config file so it applies to both `update` and `install`. On native Linux
# it is harmless: apt only preallocates that size in the mmap and does not use
# it if it is not needed.
RUN echo 'APT::Cache-Start "100000000";' > /etc/apt/apt.conf.d/99-cache-size \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates curl coreutils git g++ \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged user.
# `-m -d /home/orcaster` creates the home dir; without it, tools that write to
# `~` (pip -> ~/.cache/pip, uv, poetry, npm, etc.) fail with
# `PermissionError: /home/orcaster` when running user tasks.
RUN groupadd -r orcaster -g 1000 \
    && useradd -r -m -d /home/orcaster -g orcaster -u 1000 orcaster

ENV ORCASTER_HOME=/var/lib/orcaster

WORKDIR /app

# Copy the virtual environment from builder.
COPY --from=builder --chown=orcaster:orcaster /build/.venv /app/.venv

# Pre-create state/cache dirs owned by orcaster so volume mounts inherit the
# right ownership on first attach (rootless Podman/Docker mount over an empty
# dir; without this, the mountpoint is root:root and the worker cannot write
# state.json — see worker.state.save).
RUN mkdir -p /var/lib/orcaster /var/cache/orcaster \
    && chown orcaster:orcaster /var/lib/orcaster /var/cache/orcaster

USER orcaster

# No HEALTHCHECK: `podman exec` handed the command the container's full
# environment, readable by a task of the same uid through /proc. Nothing
# consumed the status.
ENTRYPOINT ["python", "-m", "orcaster_worker"]
