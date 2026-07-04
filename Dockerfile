FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr-all \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
RUN /app/.venv/bin/python manage.py collectstatic --noinput

FROM gcr.io/distroless/python3-debian13:nonroot

WORKDIR /app

COPY --from=builder /app /app
COPY --from=builder /usr/local /usr/local
COPY --from=builder /usr/bin/tesseract /usr/bin/tesseract
COPY --from=builder /lib/x86_64-linux-gnu /lib/x86_64-linux-gnu
COPY --from=builder /usr/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu
COPY --from=builder /usr/share/tesseract-ocr /usr/share/tesseract-ocr

ENV PATH="/app/.venv/bin:${PATH}" \
    TESSDATA_PREFIX="/usr/share/tesseract-ocr/5/tessdata" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER nonroot:nonroot

ENTRYPOINT ["/app/.venv/bin/gunicorn", "config.asgi:application", "-c", "/app/config/gunicorn.conf.py"]
