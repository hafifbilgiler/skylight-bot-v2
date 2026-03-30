FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc g++ && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch==2.4.0+cpu --extra-index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir "transformers==4.44.2" "FlagEmbedding==1.2.9" "fastapi==0.115.0" "uvicorn[standard]==0.30.0" "pydantic>=2.0.0"
RUN python -c "from FlagEmbedding import FlagReranker; FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=False); print('Model hazir')"
COPY app/reranker_service.py .
EXPOSE 8087
CMD ["uvicorn", "reranker_service:app", "--host", "0.0.0.0", "--port", "8087", "--workers", "1"]
