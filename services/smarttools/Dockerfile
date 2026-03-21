FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi==0.109.0 \
    uvicorn[standard]==0.27.0 \
    requests==2.31.0 \
    httpx==0.27.0 \    
    beautifulsoup4==4.12.3 \
    lxml==5.1.0 \
    pytz==2024.1

COPY smart_tools.py /app/smart_tools_service.py
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8081/health', timeout=3)"

# workers=4 — çoklu kullanıcı için
CMD ["uvicorn", "smart_tools_service:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "4"]