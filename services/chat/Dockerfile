# services/chat/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app/app.py .

# Prompts
COPY prompts/production.py ./prompts_production.py

# Intent Classifier & Reasoning Layer
COPY app/intent_classifier.py .
COPY app/conversation_state.py .
COPY app/task_builder.py .
# Run
EXPOSE 8082
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8082", "--workers", "2"]