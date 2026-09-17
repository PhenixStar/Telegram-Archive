FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Install system dependencies (ffmpeg for video thumbnail extraction)
RUN apt-get update && apt-get install -y \
    gcc \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast, reproducible dependency installation
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

# Install dependencies (locked versions)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Create non-root user for security
RUN useradd -m -u 1000 telegram && \
    mkdir -p /data/backups && \
    chown -R telegram:telegram /data && \
    chmod +x /app/scripts/entrypoint.sh

# Switch to non-root user
USER telegram

# Set default environment variables
ENV BACKUP_PATH=/data/backups \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Volume for persistent data
VOLUME ["/data"]

# Unhealthy when the scheduler is dead, its event loop is wedged, or a backup run is stuck.
# The start period covers entrypoint migrations on a large archive.
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
  CMD ["python3", "/app/scripts/healthcheck_backup.py"]

# Entrypoint runs migrations, then hands off to CMD
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# Default: show help (requires explicit command)
CMD ["python", "-m", "src"]
