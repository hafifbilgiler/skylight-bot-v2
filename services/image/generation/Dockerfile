# ═══════════════════════════════════════════════════════════════
# SKYLIGHT IMAGE GENERATION SERVICE - DOCKERFILE
# ═══════════════════════════════════════════════════════════════

FROM python:3.11-slim

LABEL maintainer="Skylight Team"
LABEL description="Image Generation Service - FLUX-2 with Context Awareness"

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        curl && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY app/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY prompts/ ./prompts/

# Create non-root user
RUN useradd -m -u 1000 skylight && \
    chown -R skylight:skylight /app

USER skylight

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

EXPOSE 8001

CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8001"]