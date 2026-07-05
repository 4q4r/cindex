# syntax=docker/dockerfile:1.7
# Browser sidecar for cindex HTML connectors.
#
# Runs a FastAPI service that wraps cloakbrowser (source-patched Chromium) so
# the distroless cindex worker can fetch HTML/XML/JSON/RSS from sources behind
# JS challenges (BunnyCDN Shield, Cloudflare Turnstile). The worker calls
# POST /fetch over the internal docker network; this service owns the single
# persistent Chromium context.
#
# NOT distroless: Chromium requires shared libraries. The app/worker services
# stay distroless; only this sidecar carries a slim Python + Chromium runtime.

# ----------------------------------------------------------------------------
# Builder: install deps + Chromium runtime libs + pre-download the cloakbrowser
# Chromium binary (≈200MB) into a cache dir we copy to the runtime image.
# ----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    CLOAKBROWSER_CACHE_DIR=/opt/cloak-cache \
    CLOAKBROWSER_AUTO_UPDATE=false

WORKDIR /app

# Chromium runtime libraries required by the source-patched binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libxext6 libxshmfence1 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2 \
        fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer) then the project.
COPY pyproject.toml README.md ./
COPY src ./src

# Install deps, then pre-download the cloakbrowser Chromium binary (≈200MB)
# into the cache dir so the runtime image needs no network on first request.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev \
    && uv run python -m cloakbrowser install

# ----------------------------------------------------------------------------
# Runtime: slim Python + copied venv + Chromium binary + libs.
# ----------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLOAKBROWSER_CACHE_DIR=/opt/cloak-cache \
    CLOAKBROWSER_AUTO_UPDATE=false \
    CINDEX_PROFILE_DIR=/data/cloak-profile \
    CINDEX_BROWSER_PORT=8081 \
    CINDEX_BROWSER_HEADLESS=1 \
    CINDEX_BROWSER_CONCURRENCY=4 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Chromium runtime libraries (same set as builder) + tini for signal handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libxext6 libxshmfence1 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2 \
        fonts-liberation ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/cloak-cache /opt/cloak-cache
COPY src ./src

# Non-root user for the runtime. Pre-create the persistent profile dir
# (mounted as a named volume in compose) with non-root ownership so the
# volume inherits writable permissions.
RUN groupadd --system --gid 65532 sidecar \
    && useradd --system --uid 65532 --gid 65532 --home-dir /app --shell /usr/sbin/nologin sidecar \
    && mkdir -p /data/cloak-profile \
    && chown -R 65532:65532 /app /opt/cloak-cache /data

USER 65532:65532
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "cindex_browser_sidecar.main:app", "--host", "0.0.0.0", "--port", "8081", "--proxy-headers", "--forwarded-allow-ips=*"]