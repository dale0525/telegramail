# Multi-stage build for TelegramMail (Pixi-managed dependencies)
FROM debian:bookworm-slim AS pixi-builder

ARG PIXI_VERSION=v0.62.2
ENV PIXI_VERSION=${PIXI_VERSION}

WORKDIR /app

# Install Pixi
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libunwind-14 \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://pixi.sh/install.sh | sh
ENV PATH=/root/.pixi/bin:$PATH
ENV LD_LIBRARY_PATH=/app/.pixi/envs/default/lib

# Copy Pixi manifest & lockfile first for better caching
COPY pixi.toml pixi.lock ./
# Skip dev-only tooling packages inside the image
RUN pixi install --locked --skip docker-cli --skip docker-compose

# Copy application code and scripts
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY requirements.txt .

# Setup & validate TDLib libraries for the target architecture
RUN pixi run tdlib-validate --verbose

# Runtime stage
FROM debian:bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app
ENV LD_LIBRARY_PATH=/app/.pixi/envs/default/lib
ENV SSL_CERT_FILE=/app/.pixi/envs/default/ssl/cacert.pem

WORKDIR /app

# TDLib depends on LLVM libunwind (libunwind.so.1), which is not available in conda-forge
RUN apt-get update && apt-get install -y --no-install-recommends \
    libunwind-14 \
    && rm -rf /var/lib/apt/lists/*

# Pixi binary + project environment (includes Python + runtime libs)
COPY --from=pixi-builder /root/.pixi /root/.pixi
COPY --from=pixi-builder /app /app

ENV PATH=/root/.pixi/bin:$PATH

CMD ["pixi", "run", "dev"]
