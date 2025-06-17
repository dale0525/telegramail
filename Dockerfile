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

# Install runtime dependencies including C++ runtime for TDLib
RUN apt-get update && apt-get install -y \
    ca-certificates \
    libc++1 \
    libc++abi1 \
    libssl3 \
    zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Set working directory
WORKDIR /app

# Copy Python packages from builder stage to root location temporarily
COPY --from=builder /root/.local /root/.local

# Copy application code and scripts
COPY app/ ./app/
COPY scripts/ ./scripts/

# Add Python packages to PATH for setup script
ENV PATH=/root/.local/bin:$PATH

# Setup TDLib libraries for the target architecture
RUN python3 scripts/setup_tdlib.py --verbose

# Default command
CMD ["python", "-m", "app.main"]
