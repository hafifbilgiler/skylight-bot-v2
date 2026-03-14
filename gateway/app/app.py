"""
═══════════════════════════════════════════════════════════════
SKYLIGHT API GATEWAY
═══════════════════════════════════════════════════════════════
Orchestrator-only pattern (Claude AI / Gemini style)
- Authentication & Authorization
- Request Routing
- Response Aggregation
- Rate Limiting
- Health Checks
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, HTTPException, Header, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import httpx
import jwt
import time
import datetime
import asyncio
from contextlib import asynccontextmanager

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Service URLs (from environment - matching original deployment)
CHAT_SERVICE_URL = os.getenv("CHAT_SERVICE_URL", "http://skylight-chat-service:8082")
RAG_SERVICE_URL = os.getenv("RAG_SERVICE_URL", "http://skylight-rag-service:8084")
IMAGE_GEN_SERVICE_URL = os.getenv("IMAGE_GEN_SERVICE_URL", "http://skylight-image-gen-service:8080")
IMAGE_ANALYSIS_SERVICE_URL = os.getenv("IMAGE_ANALYSIS_SERVICE_URL", "http://skylight-bot-image-analysis-service:8080")
SMART_TOOLS_URL = os.getenv("SMART_TOOLS_URL", "http://skylight-smart-tools:8081")
ABUSE_CONTROL_URL = os.getenv("ABUSE_CONTROL_URL", "http://skylight-bot-abuse-control:8010")

# JWT Config (from environment - matching original)
JWT_SECRET = os.getenv("JWT_SECRET", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
API_TOKEN = os.getenv("API_TOKEN", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Database Config (from environment)
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Redis Config (from environment)
REDIS_URL = os.getenv("REDIS_URL", "redis://skylight-redis:6379/0")

# SMTP Config (from environment)
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.hostinger.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# ClamAV Config (from environment)
CLAMAV_ENABLED = os.getenv("CLAMAV_ENABLED", "true").lower() == "true"
CLAMAV_HOST = os.getenv("CLAMAV_HOST", "skylight-bot-antivirus")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT", "3310"))

# Rate Limiting (in-memory for now, use Redis in production)
rate_limit_cache = {}

# Health status cache
service_health = {
    "chat": {"status": "unknown", "last_check": 0},
    "rag": {"status": "unknown", "last_check": 0},
    "image_gen": {"status": "unknown", "last_check": 0},
    "image_analysis": {"status": "unknown", "last_check": 0},
}

# ═══════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    query: str
    mode: str = "assistant"
    conversation_id: Optional[str] = None
    history: Optional[List[ChatMessage]] = None
    context: Optional[str] = None
    image_data: Optional[str] = None
    session_summary: Optional[str] = None

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    services: Dict[str, Any]

# ═══════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown"""
    # Startup
    print("🚀 Skylight API Gateway starting...")
    await check_all_services()
    print("✅ Gateway ready!")
    
    yield
    
    # Shutdown
    print("👋 Gateway shutting down...")

app = FastAPI(
    title="Skylight API Gateway",
    description="Orchestrator for Skylight AI services",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure properly in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════
# AUTHENTICATION & AUTHORIZATION
# ═══════════════════════════════════════════════════════════════

def verify_token(token: str) -> Dict[str, Any]:
    """Verify JWT token and return user data"""
    try:
        if not token or not token.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Invalid token format")
        
        token = token.replace("Bearer ", "")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def check_plan_access(user_plan: Dict, mode: str, feature: str) -> bool:
    """Check if user's plan allows the requested feature"""
    allowed_modes = user_plan.get("features", {}).get("allowed_modes", ["assistant"])
    
    if mode not in allowed_modes:
        return False
    
    if feature == "image_gen":
        return user_plan.get("features", {}).get("image_gen", False)
    
    if feature == "vision":
        return user_plan.get("features", {}).get("vision", False)
    
    return True

# ═══════════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════════

def check_rate_limit(user_id: int, limit_type: str = "message") -> tuple[bool, int, int]:
    """
    Check rate limit for user
    Returns: (allowed, remaining, limit)
    """
    # This is a simple in-memory implementation
    # Use Redis with sliding window in production
    
    today = datetime.date.today().isoformat()
    key = f"{user_id}:{limit_type}:{today}"
    
    # Get user's plan limits (should come from DB)
    # For now, using defaults
    limits = {
        "free": {"message": 50, "image_gen": 0},
        "premium": {"message": 1000, "image_gen": 100},
    }
    
    # Get current count
    current = rate_limit_cache.get(key, 0)
    limit = limits["premium"]["message"]  # Get from user plan
    
    if current >= limit:
        return False, 0, limit
    
    return True, limit - current, limit

def increment_rate_limit(user_id: int, limit_type: str = "message"):
    """Increment rate limit counter"""
    today = datetime.date.today().isoformat()
    key = f"{user_id}:{limit_type}:{today}"
    rate_limit_cache[key] = rate_limit_cache.get(key, 0) + 1

# ═══════════════════════════════════════════════════════════════
# PLANNER - REQUEST ROUTING LOGIC
# ═══════════════════════════════════════════════════════════════

def plan_request(
    query: str,
    mode: str,
    has_image: bool,
    user_plan: Dict,
    user_id: int,
) -> Dict[str, Any]:
    """
    Analyze request and determine routing
    Returns routing plan with target service and metadata
    """
    
    # Check mode access
    if not check_plan_access(user_plan, mode, "mode"):
        return {
            "target": "error",
            "error_type": "mode_locked",
            "mode": mode,
            "user_plan": user_plan.get("plan_name", "Free"),
        }
    
    # Check rate limits
    allowed, remaining, limit = check_rate_limit(user_id, "message")
    if not allowed:
        return {
            "target": "error",
            "error_type": "rate_limit",
            "remaining": remaining,
            "limit": limit,
        }
    
    # Detect image generation request
    image_gen_keywords = ["görsel", "resim", "image", "picture", "draw", "create image"]
    is_image_gen = any(kw in query.lower() for kw in image_gen_keywords)
    
    if mode == "assistant" and is_image_gen:
        if not check_plan_access(user_plan, mode, "image_gen"):
            return {
                "target": "error",
                "error_type": "feature_locked",
                "feature": "image_generation",
            }
        
        allowed, remaining, limit = check_rate_limit(user_id, "image_gen")
        if not allowed:
            return {
                "target": "error",
                "error_type": "rate_limit",
                "limit_type": "image_gen",
                "remaining": remaining,
                "limit": limit,
            }
        
        return {
            "target": "image_generation",
            "service": "image_gen",
            "mode": mode,
        }
    
    # Vision analysis (has image)
    if has_image:
        if not check_plan_access(user_plan, mode, "vision"):
            return {
                "target": "error",
                "error_type": "feature_locked",
                "feature": "vision",
            }
        
        return {
            "target": "vision_analysis",
            "service": "image_analysis",
            "mode": mode,
        }
    
    # Regular chat modes
    return {
        "target": "chat",
        "service": "chat",
        "mode": mode,
        "requires_rag": mode == "assistant",  # Only assistant uses RAG
    }

# ═══════════════════════════════════════════════════════════════
# SERVICE COMMUNICATION
# ═══════════════════════════════════════════════════════════════

async def call_service(
    service_url: str,
    endpoint: str,
    method: str = "POST",
    data: Optional[Dict] = None,
    stream: bool = False,
    timeout: int = 60,
) -> Any:
    """
    Call microservice with proper error handling
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if method == "POST":
                if stream:
                    # Streaming response
                    async with client.stream("POST", f"{service_url}{endpoint}", json=data) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            yield chunk
                else:
                    # Regular response
                    response = await client.post(f"{service_url}{endpoint}", json=data)
                    response.raise_for_status()
                    return response.json()
            
            elif method == "GET":
                response = await client.get(f"{service_url}{endpoint}")
                response.raise_for_status()
                return response.json()
        
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail=f"Service timeout: {service_url}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Service error: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Service call failed: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# EXECUTORS - SERVICE ORCHESTRATION
# ═══════════════════════════════════════════════════════════════

async def execute_chat(plan: Dict, request: ChatRequest, user_id: int):
    """Execute chat request via Chat Service"""
    
    # Prepare request data
    chat_data = {
        "query": request.query,
        "mode": plan["mode"],
        "conversation_id": request.conversation_id,
        "user_id": user_id,
        "history": [{"role": m.role, "content": m.content} for m in (request.history or [])],
        "context": request.context,
        "session_summary": request.session_summary,
    }
    
    # Call RAG service if needed
    if plan.get("requires_rag"):
        try:
            rag_data = await call_service(
                RAG_SERVICE_URL,
                "/retrieve",
                data={"query": request.query, "top_k": 3},
            )
            chat_data["rag_context"] = rag_data.get("context", "")
        except Exception as e:
            print(f"⚠️ RAG service error: {e}")
            # Continue without RAG context
    
    # Stream response from Chat Service
    async def stream_response():
        async for chunk in call_service(
            CHAT_SERVICE_URL,
            "/chat",
            data=chat_data,
            stream=True,
        ):
            yield chunk
    
    # Increment usage in background
    increment_rate_limit(user_id, "message")
    
    return StreamingResponse(stream_response(), media_type="text/plain; charset=utf-8")

async def execute_image_generation(plan: Dict, request: ChatRequest, user_id: int):
    """Execute image generation via Image Gen Service"""
    
    image_data = {
        "prompt": request.query,
        "user_id": user_id,
        "conversation_id": request.conversation_id,
        "context": request.context or "",
    }
    
    async def stream_response():
        async for chunk in call_service(
            IMAGE_GEN_SERVICE_URL,
            "/generate",
            data=image_data,
            stream=True,
            timeout=120,  # Image gen takes longer
        ):
            yield chunk
    
    # Increment image gen usage
    increment_rate_limit(user_id, "image_gen")
    
    return StreamingResponse(stream_response(), media_type="text/plain; charset=utf-8")

async def execute_vision_analysis(plan: Dict, request: ChatRequest, user_id: int):
    """Execute vision analysis via Image Analysis Service"""
    
    vision_data = {
        "query": request.query,
        "image_data": request.image_data,
        "mode": plan["mode"],
        "user_id": user_id,
    }
    
    async def stream_response():
        async for chunk in call_service(
            IMAGE_ANALYSIS_SERVICE_URL,
            "/analyze",
            data=vision_data,
            stream=True,
        ):
            yield chunk
    
    return StreamingResponse(stream_response(), media_type="text/plain; charset=utf-8")

def execute_error(plan: Dict):
    """Return error response"""
    error_type = plan.get("error_type")
    
    if error_type == "mode_locked":
        message = (
            f"🔒 '{plan['mode']}' modu mevcut planında aktif değil.\n\n"
            f"Mevcut plan: {plan['user_plan']}\n"
            f"Bu modu kullanmak için Premium'a geçmen gerekiyor."
        )
    
    elif error_type == "rate_limit":
        if plan.get("limit_type") == "image_gen":
            message = f"⏰ Günlük görsel limitine ulaştın ({plan['limit']}/gün)."
        else:
            message = f"⏰ Günlük mesaj limitine ulaştın ({plan['limit']}/gün).\n\nYarın tekrar deneyebilirsin! 🚀"
    
    elif error_type == "feature_locked":
        feature_names = {
            "image_generation": "Görsel oluşturma",
            "vision": "Görsel analiz",
        }
        message = f"🔒 {feature_names.get(plan['feature'])} özelliği Premium abonelikte mevcut."
    
    else:
        message = "⚠️ Bir hata oluştu. Lütfen tekrar deneyin."
    
    def error_gen():
        yield message
    
    return StreamingResponse(error_gen(), media_type="text/plain; charset=utf-8")

# ═══════════════════════════════════════════════════════════════
# MAIN CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    authorization: str = Header(None),
    background_tasks: BackgroundTasks = None,
):
    """
    Main chat endpoint - Gateway orchestrator
    
    Flow:
    1. Authenticate user
    2. Plan request (routing decision)
    3. Execute plan (call appropriate service)
    4. Return response
    """
    
    # 1. Authentication
    user_data = verify_token(authorization)
    user_id = user_data.get("user_id")
    user_plan = user_data.get("plan", {"plan_name": "Free", "features": {}})
    
    # 2. Planning
    plan = plan_request(
        query=request.query,
        mode=request.mode,
        has_image=bool(request.image_data),
        user_plan=user_plan,
        user_id=user_id,
    )
    
    print(f"[GATEWAY] Plan: {plan}")
    
    # 3. Execution
    target = plan.get("target")
    
    if target == "error":
        return execute_error(plan)
    
    elif target == "chat":
        return await execute_chat(plan, request, user_id)
    
    elif target == "image_generation":
        return await execute_image_generation(plan, request, user_id)
    
    elif target == "vision_analysis":
        return await execute_vision_analysis(plan, request, user_id)
    
    else:
        raise HTTPException(status_code=500, detail="Unknown routing target")

# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK ENDPOINTS
# ═══════════════════════════════════════════════════════════════

async def check_service_health(service_name: str, service_url: str) -> str:
    """Check health of a single service"""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{service_url}/health")
            if response.status_code == 200:
                return "healthy"
            return "unhealthy"
    except:
        return "unreachable"

async def check_all_services():
    """Check health of all services"""
    services = {
        "chat": CHAT_SERVICE_URL,
        "rag": RAG_SERVICE_URL,
        "image_gen": IMAGE_GEN_SERVICE_URL,
        "image_analysis": IMAGE_ANALYSIS_SERVICE_URL,
    }
    
    tasks = [check_service_health(name, url) for name, url in services.items()]
    results = await asyncio.gather(*tasks)
    
    for i, (name, _) in enumerate(services.items()):
        service_health[name]["status"] = results[i]
        service_health[name]["last_check"] = time.time()

@app.get("/health")
async def health_check():
    """Gateway health check"""
    
    # Refresh service health if stale (> 30 seconds)
    if time.time() - service_health["chat"]["last_check"] > 30:
        await check_all_services()
    
    overall_status = "healthy"
    if any(s["status"] != "healthy" for s in service_health.values()):
        overall_status = "degraded"
    
    return HealthResponse(
        status=overall_status,
        timestamp=datetime.datetime.now().isoformat(),
        services=service_health,
    )

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Skylight API Gateway",
        "version": "2.0.0",
        "status": "running",
    }

# ═══════════════════════════════════════════════════════════════
# LEGACY ENDPOINTS (Keep for backward compatibility)
# ═══════════════════════════════════════════════════════════════

# Keep all your existing endpoints:
# - /auth/login
# - /auth/register
# - /conversations
# - /user/profile
# etc.

# These remain UNCHANGED in the gateway

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)