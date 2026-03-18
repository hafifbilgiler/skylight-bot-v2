FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY app/payment_service.py /app/payment_service.py

EXPOSE 8001

CMD ["uvicorn", "payment_service:app", "--host", "0.0.0.0", "--port", "8001"]