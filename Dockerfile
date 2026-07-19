# syntax=docker/dockerfile:1.9

# Rosa in a container — BYOC (Bring Your Own Cloud) install.
#
# Build:
#     docker build -t rosa:latest .
#
# Run:
#     docker run -d \
#       -v rosa-home:/data \
#       -p 8765:8765 \
#       -e ROSA_HOME=/data \
#       -e OLLAMA_HOST=http://ollama:11434 \
#       --name rosa rosa:latest
#
# For local + Ollama sidecar: use docker-compose.yml.
#
# Notes:
# - iMessage bridge is macOS-only; in a container Slack is the recommended
#   main_channel. On first boot the setup wizard runs on :8765.
# - Full Disk Access is meaningless in a container — Slack path only.

# =====================================================================
# Stage 1: builder — installs deps into an isolated venv so we can
# COPY it into a slim runtime image without pip / build tools in prod.
# =====================================================================
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# =====================================================================
# Stage 2: runtime — slim image with only what's needed to run + update.
# No pip cache, no build tools, no apt lists.
# =====================================================================
FROM python:3.12-slim AS runtime

# git needed for `rosa update`; tini for signal forwarding; curl for
# healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      tini \
      curl \
      && rm -rf /var/lib/apt/lists/*

# Non-root user (L-3 fix). UID 1000 works well with named-volume perms.
RUN useradd --uid 1000 --create-home --shell /bin/bash rosa

WORKDIR /app

# Copy the isolated venv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# App code
COPY --chown=rosa:rosa src/ ./src/
COPY --chown=rosa:rosa scripts/ ./scripts/
COPY --chown=rosa:rosa config/config.example.yaml ./config/config.example.yaml
COPY --chown=rosa:rosa LICENSE README.md ./

# Make `rosa` CLI available on PATH; venv already active via ENV below.
RUN ln -s /app/scripts/rosa /usr/local/bin/rosa \
    && mkdir -p /data \
    && chown rosa:rosa /data

VOLUME ["/data"]
ENV ROSA_HOME=/data \
    PYTHONPATH=/app/src \
    PATH=/opt/venv/bin:$PATH \
    PYTHON=/opt/venv/bin/python

EXPOSE 8765

USER rosa

# tini forwards SIGTERM/SIGHUP properly to the Python process.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/src/main.py"]
