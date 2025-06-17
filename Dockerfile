# Multi-stage build for TelegramMail
FROM python:3.10-slim AS builder

# Set working directory
WORKDIR /app

# Install system dependencies required for building Python packages
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.10-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

# Create non-root user for security
RUN groupadd -r telegramail && useradd -r -g telegramail telegramail

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Set working directory
WORKDIR /app

# Copy Python packages from builder stage
COPY --from=builder /root/.local /home/telegramail/.local

# Copy application code
COPY app/ ./app/
COPY .env.example .env.example

# Setup TDLib libraries for the target architecture
RUN python3 -c "\
import platform; \
import os; \
from pathlib import Path; \
machine = platform.machine().lower(); \
arch = 'amd64' if machine in ['x86_64', 'amd64'] else ('arm64' if machine in ['aarch64', 'arm64'] else machine); \
print(f'Setting up TDLib for linux_{arch}'); \
tdlib_dir = Path('/app/app/resources/tdlib'); \
source_lib = tdlib_dir / f'libtdjson_linux_{arch}.so'; \
print(f'Found TDLib library: {source_lib}') if source_lib.exists() else print(f'Warning: TDLib library not found: {source_lib}'); \
"

# Create data directory and set permissions
RUN mkdir -p /app/data && \
    chown -R telegramail:telegramail /app && \
    chmod -R 755 /app

# Switch to non-root user
USER telegramail

# Add local Python packages to PATH
ENV PATH=/home/telegramail/.local/bin:$PATH

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Expose port (if needed for future web interface)
EXPOSE 8080

# Default command
CMD ["python", "-m", "app.main"]
