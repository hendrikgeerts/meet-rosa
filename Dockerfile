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

# ---- Base ---------------------------------------------------------------
FROM python:3.12-slim AS base

# System deps: git for `rosa update`, tini for signal-forwarding.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      tini \
      curl \
      && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Deps ---------------------------------------------------------------
FROM base AS deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- Runtime ------------------------------------------------------------
FROM deps AS runtime
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/config.example.yaml ./config/config.example.yaml
COPY LICENSE README.md ./

# Make `rosa` CLI available on PATH
RUN ln -s /app/scripts/rosa /usr/local/bin/rosa

# ROSA_HOME as a volume — persists config, secrets, data across restarts.
VOLUME ["/data"]
ENV ROSA_HOME=/data \
    PYTHONPATH=/app/src \
    PYTHON=/usr/local/bin/python

# Wizard port (setup wizard + settings UI)
EXPOSE 8765

# tini forwards SIGTERM/SIGHUP properly to the Python process.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/src/main.py"]
