FROM python:3.11-slim

# Set environment variables: unbuffered stdout, default runtime configurations
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAHA_PUBLIC_MODE=true \
    SAHA_DEMO_MODE=true \
    SAHA_DB_PATH=/data/saha.db \
    SAHA_WORKSPACE_ROOT=/data/workspace

WORKDIR /app

# Create non-root user and persistent data directories
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser && \
    mkdir -p /data/workspace && \
    chown -R appuser:appgroup /data

# Install pinned dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only needed application code, skills, and web assets
COPY backend/ ./backend/
COPY cli/ ./cli/
COPY skills/ ./skills/
COPY web/ ./web/

# Ensure app directory permissions
RUN chown -R appuser:appgroup /app

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8080"]
