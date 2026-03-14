"""
═══════════════════════════════════════════════════════════════
SKYLIGHT RAG SERVICE
═══════════════════════════════════════════════════════════════
Retrieval-Augmented Generation service
- Qdrant vector search
- Embedding generation
- Smart Tools (web search) integration
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
import httpx
import os
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Qdrant (from environment - matching original deployment)
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "skylight_docs")
EMBEDDING_DIM = int(os.getenv("EMBED_VECTOR_SIZE", "1024"))

# DeepInfra for embeddings (from environment)
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_EMBED_MODEL = os.getenv("DEEPINFRA_EMBED_MODEL", "BAAI/bge-m3")
EMBEDDING_MODEL = DEEPINFRA_EMBED_MODEL
EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "deepinfra")

# Smart Tools (web search) - from environment
SMART_TOOLS_URL = os.getenv("SMART_TOOLS_URL", "http://skylight-smart-tools:8081")
SMART_TOOLS_ENABLED = os.getenv("SMART_TOOLS_ENABLED", "true").lower() == "true"

# Local embedding fallback (from environment)
EMBED_URL = os.getenv("EMBED_URL", "http://skylight-embed-service:80/embed")

# Initialize Qdrant client
qdrant_client = QdrantClient(url=QDRANT_URL)

app = FastAPI(title="Skylight RAG Service", version="1.0.0")

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class RetrievalRequest(BaseModel):
    query: str
    top_k: int = 3
    user_id: Optional[int] = None
    filters: Optional[Dict] = None

class RetrievalResponse(BaseModel):
    context: str
    sources: List[Dict]
    search_used: bool = False

class WebSearchRequest(BaseModel):
    query: str
    user_id: Optional[int] = None

# ═══════════════════════════════════════════════════════════════
# EMBEDDING GENERATION
# ═══════════════════════════════════════════════════════════════

async def get_embedding(text: str) -> List[float]:
    """Generate embedding using DeepInfra"""
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
    }
    
    payload = {
        "model": EMBEDDING_MODEL,
        "input": [text],
        "encoding_format": "float",
    }
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.deepinfra.com/v1/openai/embeddings",
            headers=headers,
            json=payload,
        )
        
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Embedding generation failed")
        
        data = response.json()
        return data["data"][0]["embedding"]

# ═══════════════════════════════════════════════════════════════
# QDRANT VECTOR SEARCH
# ═══════════════════════════════════════════════════════════════

async def search_vectors(
    query_vector: List[float],
    top_k: int = 3,
    filters: Optional[Dict] = None,
) -> List[Dict]:
    """Search Qdrant for similar vectors"""
    
    try:
        # Build filter if provided
        query_filter = None
        if filters:
            conditions = []
            for key, value in filters.items():
                conditions.append(
                    FieldCondition(
                        key=key,
                        match=MatchValue(value=value),
                    )
                )
            if conditions:
                query_filter = Filter(must=conditions)
        
        # Search
        results = qdrant_client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=query_vector,
            limit=top_k,
            query_filter=query_filter,
        )
        
        # Format results
        docs = []
        for result in results:
            docs.append({
                "content": result.payload.get("content", ""),
                "metadata": result.payload.get("metadata", {}),
                "score": result.score,
            })
        
        return docs
    
    except Exception as e:
        print(f"⚠️ Qdrant search error: {e}")
        return []

# ═══════════════════════════════════════════════════════════════
# WEB SEARCH (Smart Tools)
# ═══════════════════════════════════════════════════════════════

async def web_search(query: str) -> Optional[str]:
    """Call Smart Tools for web search"""
    
    if not SMART_TOOLS_ENABLED:
        return None
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{SMART_TOOLS_URL}/search",
                json={"query": query},
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("results", "")
    
    except Exception as e:
        print(f"⚠️ Web search error: {e}")
    
    return None

# ═══════════════════════════════════════════════════════════════
# RETRIEVAL ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/retrieve", response_model=RetrievalResponse)
async def retrieve(request: RetrievalRequest):
    """
    Retrieve relevant context for a query
    
    Flow:
    1. Generate query embedding
    2. Search Qdrant for similar documents
    3. Optionally call web search
    4. Combine and format results
    """
    
    # 1. Generate embedding
    try:
        query_vector = await get_embedding(request.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")
    
    # 2. Vector search
    docs = await search_vectors(
        query_vector=query_vector,
        top_k=request.top_k,
        filters=request.filters,
    )
    
    # 3. Format context
    context_parts = []
    sources = []
    
    for i, doc in enumerate(docs, 1):
        content = doc["content"]
        metadata = doc["metadata"]
        score = doc["score"]
        
        # Add to context
        source_info = metadata.get("source", f"Document {i}")
        context_parts.append(f"[Source {i}: {source_info}]\n{content}\n")
        
        # Track source
        sources.append({
            "index": i,
            "source": source_info,
            "score": score,
            "metadata": metadata,
        })
    
    # Combine context
    context = "\n---\n".join(context_parts) if context_parts else ""
    
    # 4. Web search (optional - based on query characteristics)
    search_used = False
    web_results = None
    
    # Detect if web search would be beneficial
    search_keywords = ["güncel", "current", "latest", "recent", "news", "2025", "2026"]
    should_search = any(kw in request.query.lower() for kw in search_keywords)
    
    if should_search and SMART_TOOLS_ENABLED:
        web_results = await web_search(request.query)
        if web_results:
            context += f"\n\n[WEB ARAMA SONUÇLARI]\n{web_results}\n[/WEB ARAMA SONUÇLARI]"
            search_used = True
    
    return RetrievalResponse(
        context=context,
        sources=sources,
        search_used=search_used,
    )

# ═══════════════════════════════════════════════════════════════
# DOCUMENT MANAGEMENT (Optional - for adding docs)
# ═══════════════════════════════════════════════════════════════

class DocumentRequest(BaseModel):
    content: str
    metadata: Dict = {}

@app.post("/add_document")
async def add_document(request: DocumentRequest):
    """Add a document to Qdrant"""
    
    # Generate embedding
    try:
        vector = await get_embedding(request.content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")
    
    # Generate ID
    import uuid
    doc_id = str(uuid.uuid4())
    
    # Add to Qdrant
    try:
        qdrant_client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=[
                PointStruct(
                    id=doc_id,
                    vector=vector,
                    payload={
                        "content": request.content,
                        "metadata": request.metadata,
                    },
                )
            ],
        )
        
        return {"status": "success", "document_id": doc_id}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Qdrant upsert failed: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# HEALTH & COLLECTION INIT
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """Initialize Qdrant collection if not exists"""
    
    try:
        collections = qdrant_client.get_collections().collections
        collection_names = [c.name for c in collections]
        
        if QDRANT_COLLECTION not in collection_names:
            print(f"📦 Creating Qdrant collection: {QDRANT_COLLECTION}")
            qdrant_client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=VectorParams(
                    size=EMBEDDING_DIM,
                    distance=Distance.COSINE,
                ),
            )
            print(f"✅ Collection created: {QDRANT_COLLECTION}")
        else:
            print(f"✅ Collection exists: {QDRANT_COLLECTION}")
    
    except Exception as e:
        print(f"⚠️ Qdrant initialization error: {e}")

@app.get("/health")
async def health():
    # Check Qdrant connection
    try:
        qdrant_client.get_collections()
        qdrant_status = "healthy"
    except:
        qdrant_status = "unhealthy"
    
    return {
        "status": "healthy",
        "service": "rag",
        "qdrant": qdrant_status,
        "web_search": SMART_TOOLS_ENABLED,
    }

@app.get("/")
async def root():
    return {
        "service": "Skylight RAG Service",
        "version": "1.0.0",
        "collection": QDRANT_COLLECTION,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8084)