FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements
COPY app/requirements.txt /app/requirements.txt

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application
COPY app/payment_service.py /app/payment_service.py

# Expose port (must match deployment.yaml)
EXPOSE 8005

# Run application
CMD ["uvicorn", "payment_service:app", "--host", "0.0.0.0", "--port", "8005"]