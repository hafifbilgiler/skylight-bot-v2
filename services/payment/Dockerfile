# Skylight Payment Service Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY app/payment_requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/skylight_payment_service.py ./app.py

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8083/health')"

# Expose port
EXPOSE 8083

# Run
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8083", "--workers", "2"]
