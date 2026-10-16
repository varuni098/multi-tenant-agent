FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dev images also get test/lint tooling (set by compose.override.yml)
ARG INSTALL_DEV=false
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

# bun for the Reflex UI, from the official image (no download at container start)
COPY --from=oven/bun:1.4.2 /usr/local/bin/bun /usr/local/bin/bun

RUN useradd --create-home appuser
COPY --chown=appuser:appuser . .
# Writable dirs for the UI: frontend build cache (a named volume) and Reflex's data dir
RUN chown appuser:appuser /app \
    && mkdir -p /app/ui/.web /home/appuser/.local/share/reflex \
    && chown -R appuser:appuser /app/ui /home/appuser/.local
USER appuser

EXPOSE 8000

# Production default; compose.override.yml swaps in --reload for development
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
