FROM python:3.11-slim
WORKDIR /app
COPY sosyal_requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY sosyal_app.py app.py
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8091"]