# Build the Mini App separately so the runtime image contains no Node toolchain.
FROM node:22.16.0-bookworm-slim AS web-builder

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM python:3.12.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TELEGRAMAIL_DATA_DIR=/app/data \
    TELEGRAMAIL_WEB_DIST=/app/web/dist

WORKDIR /app

RUN groupadd --gid 10001 telegramail \
    && useradd --uid 10001 --gid telegramail --create-home --home-dir /app telegramail

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=telegramail:telegramail app/ ./app/
COPY --chown=telegramail:telegramail scripts/ ./scripts/
COPY --from=web-builder --chown=telegramail:telegramail /web/dist ./web/dist
RUN mkdir -p /app/data && chown telegramail:telegramail /app/data

USER telegramail

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

CMD ["python", "scripts/entrypoint.py"]
