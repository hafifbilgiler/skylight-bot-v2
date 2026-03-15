FROM python:3.9-slim

# Install system dependencies for psycopg2
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
RUN pip install --no-cache-dir \
    psycopg2-binary==2.9.9

# Copy controller script
COPY db_controller.py .

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV CHECK_INTERVAL=3600
ENV DB_HOST=postgres
ENV DB_NAME=skylight
ENV DB_USER=postgres
ENV DB_PASSWORD=postgres

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys; from db_controller import health_check; sys.exit(0 if health_check()['status'] == 'healthy' else 1)"

# Run controller
CMD ["python", "-u", "db_controller.py"]