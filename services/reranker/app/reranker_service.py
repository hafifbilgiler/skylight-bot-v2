"""
═══════════════════════════════════════════════════════════════
SKYLIGHT RERANKER SERVICE — v1.0
BGE Reranker v2-m3 — Lokal pod, sıfır API maliyeti
═══════════════════════════════════════════════════════════════
Endpoint:
  POST /rerank  → query + documents → skorlanmış liste
  GET  /health  → servis durumu

Kullanım:
  smart_tools.py → /rerank çağırır
  chunk listesini sıralar → top_k döndürür
═══════════════════════════════════════════════════════════════
"""

import os
import logging
import asyncio
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Model yükleme — startup'ta bir kez yapılır
# ─────────────────────────────────────────────────────────────

MODEL_NAME = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
USE_FP16   = os.getenv("USE_FP16", "false").lower() == "true"  # CPU için false
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "512"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "16"))

reranker = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global reranker
    logger.info(f"[RERANKER] Model yükleniyor: {MODEL_NAME}")
    try:
        from FlagEmbedding import FlagReranker
        reranker = FlagReranker(
            MODEL_NAME,
            use_fp16=USE_FP16,
        )
        logger.info(f"[RERANKER] ✅ Model hazır — fp16={USE_FP16}")
    except Exception as e:
        logger.error(f"[RERANKER] ❌ Model yüklenemedi: {e}")
        reranker = None
    yield
    logger.info("[RERANKER] Kapatılıyor...")


app = FastAPI(
    title="Skylight Reranker",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# MODELLER
# ─────────────────────────────────────────────────────────────

class RerankRequest(BaseModel):
    query:     str
    documents: List[str]
    top_k:     Optional[int] = 5
    normalize: Optional[bool] = True  # Skoru 0-1 arasına map et


class RerankResult(BaseModel):
    index:    int
    document: str
    score:    float


class RerankResponse(BaseModel):
    results:  List[RerankResult]
    model:    str
    top_k:    int
    total:    int


# ─────────────────────────────────────────────────────────────
# ENDPOINTler
# ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":  "healthy" if reranker else "degraded",
        "model":   MODEL_NAME,
        "ready":   reranker is not None,
        "fp16":    USE_FP16,
        "version": "1.0.0",
    }


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    """
    Query ile document listesini puanla, en alakalıları döndür.

    Örnek:
        POST /rerank
        {
          "query": "dolar kuru 2025",
          "documents": ["chunk1...", "chunk2...", ...],
          "top_k": 5
        }
    """
    if not req.query or not req.documents:
        raise HTTPException(400, "query ve documents zorunlu")

    top_k = min(req.top_k or 5, len(req.documents))

    # Model hazır değilse keyword fallback
    if reranker is None:
        logger.warning("[RERANKER] Model yok — keyword fallback")
        results = _keyword_fallback(req.query, req.documents, top_k)
        return RerankResponse(
            results=results,
            model="keyword_fallback",
            top_k=top_k,
            total=len(req.documents),
        )

    try:
        # Query-document pairs oluştur
        pairs = [[req.query, doc[:MAX_LENGTH]] for doc in req.documents]

        # CPU-friendly: asyncio executor'da çalıştır (event loop bloklanmaz)
        loop   = asyncio.get_event_loop()
        scores = await loop.run_in_executor(
            None,
            lambda: reranker.compute_score(pairs, normalize=req.normalize, batch_size=BATCH_SIZE)
        )

        # Skoru float'a çevir (numpy scalar olabilir)
        scores = [float(s) for s in scores]

        # Sırala ve top_k al
        indexed = sorted(
            enumerate(scores),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

        results = [
            RerankResult(
                index=idx,
                document=req.documents[idx],
                score=score,
            )
            for idx, score in indexed
        ]

        logger.info(
            f"[RERANKER] ✅ {len(req.documents)} doc → top {top_k} "
            f"| best_score={results[0].score:.3f}"
        )

        return RerankResponse(
            results=results,
            model=MODEL_NAME,
            top_k=top_k,
            total=len(req.documents),
        )

    except Exception as e:
        logger.error(f"[RERANKER] ❌ {e}")
        # Fallback
        results = _keyword_fallback(req.query, req.documents, top_k)
        return RerankResponse(
            results=results,
            model="keyword_fallback",
            top_k=top_k,
            total=len(req.documents),
        )


def _keyword_fallback(query: str, docs: List[str], top_k: int) -> List[RerankResult]:
    """Model yoksa basit keyword overlap skoru."""
    q_words = set(query.lower().split())
    scored  = []
    for i, doc in enumerate(docs):
        words = set(doc.lower().split())
        score = len(q_words & words) / (len(q_words) + 1)
        scored.append((i, doc, score))
    scored.sort(key=lambda x: x[2], reverse=True)
    return [
        RerankResult(index=i, document=doc, score=score)
        for i, doc, score in scored[:top_k]
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8087, workers=1)
