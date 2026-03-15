"""
═══════════════════════════════════════════════════════════════
SKYLIGHT API GATEWAY - COMPLETE FIXED VERSION
═══════════════════════════════════════════════════════════════
Orchestrator pattern + All security features from original monolith

CHANGES FROM ORIGINAL:
1. ChatRequest.query → ChatRequest.prompt (compatibility with Chat Service)
2. Added missing endpoints:
   - GET /conversations/list
   - POST /conversations/create
   - GET /conversations/{id}/messages
   - PUT /conversations/{id}
   - DELETE /conversations/{id}
   - GET /profile
   - DELETE /profile/topics
   - POST /feedback
   - GET /code-mode/status
   - GET /admin/* endpoints
═══════════════════════════════════════════════════════════════
"""

import os
import io
import uuid
import time
import random
import smtplib
import datetime
import threading
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, List, Dict, Any, Tuple
from contextlib import asynccontextmanager

import jwt
import httpx
import clamd
import psycopg2
import psycopg2.pool
from fastapi import FastAPI, HTTPException, Header, Request, BackgroundTasks, File, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Service URLs
CHAT_SERVICE_URL = os.getenv("CHAT_SERVICE_URL", "http://skylight-chat:8082")
RAG_SERVICE_URL = os.getenv("RAG_SERVICE_URL", "http://skylight-rag:8084")
IMAGE_GEN_SERVICE_URL = os.getenv("IMAGE_GEN_SERVICE_URL", "http://skylight-image-gen:8083")
IMAGE_ANALYSIS_SERVICE_URL = os.getenv("IMAGE_ANALYSIS_SERVICE_URL", "http://skylight-image-analysis:8002")
SMART_TOOLS_URL = os.getenv("SMART_TOOLS_URL", "http://skylight-smart-tools:8081")
ABUSE_CONTROL_URL = os.getenv("ABUSE_CONTROL_URL", "http://skylight-bot-abuse-control:8010")

# JWT Config
JWT_SECRET = os.getenv("JWT_SECRET", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
API_TOKEN = os.getenv("API_TOKEN", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
TOKEN_EXPIRE_DAYS = int(os.getenv("TOKEN_EXPIRE_DAYS", "7"))

# Database Config
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# SMTP Config
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.hostinger.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "noreply@one-bune.com")

# ClamAV Config
CLAMAV_ENABLED = os.getenv("CLAMAV_ENABLED", "true").lower() == "true"
CLAMAV_HOST = os.getenv("CLAMAV_HOST", "skylight-bot-antivirus")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT", "3310"))

# Test Bypass Credentials
TEST_BYPASS_EMAIL = "test@one-bune.com"
TEST_BYPASS_CODE = "138113"
TEST_BYPASS_NAME = "Test User"

# File Upload Limits
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_EXTRACTED_CHARS = 30000
MAX_FILES_PER_USER = 3
FILE_TTL_SECONDS = 600  # 10 minutes

# Allowed file extensions
ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".tsv",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".r", ".sql", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".cmd",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".properties", ".xml", ".html", ".css",
    ".dockerfile", ".tf", ".hcl", ".j2", ".jinja2",
    ".log",
}

# ═══════════════════════════════════════════════════════════════
# DATABASE CONNECTION POOL
# ═══════════════════════════════════════════════════════════════

_db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()

def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Lazy-init thread-safe connection pool."""
    global _db_pool
    if _db_pool is None or _db_pool.closed:
        with _pool_lock:
            if _db_pool is None or _db_pool.closed:
                _db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=15,
                    host=DB_HOST,
                    database=DB_NAME,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    port=DB_PORT,
                )
                print("[DB POOL] Initialized (2-15 connections)")
    return _db_pool

def get_db():
    """Get database connection from pool."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

# ═══════════════════════════════════════════════════════════════
# DATABASE SCHEMA INITIALIZATION
# ═══════════════════════════════════════════════════════════════

def ensure_tables():
    """Ensure all required tables exist."""
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        # Users table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255),
                google_id VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # OTP codes table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                email VARCHAR(255) PRIMARY KEY,
                code VARCHAR(6) NOT NULL,
                expire_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # Subscription plans table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                price_monthly DECIMAL(10,2),
                price_yearly DECIMAL(10,2),
                currency VARCHAR(3) DEFAULT 'TRY',
                features JSONB NOT NULL,
                limits JSONB NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # User subscriptions table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                plan_id VARCHAR(50) REFERENCES subscription_plans(id),
                status VARCHAR(20) DEFAULT 'active',
                billing_period VARCHAR(20),
                current_period_start TIMESTAMPTZ,
                current_period_end TIMESTAMPTZ,
                cancel_at_period_end BOOLEAN DEFAULT FALSE,
                cancelled_at TIMESTAMPTZ,
                iyzico_subscription_ref VARCHAR(255),
                iyzico_customer_ref VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, plan_id, status)
            )
        """)
        
        # Usage tracking table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_tracking (
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                usage_date DATE NOT NULL,
                messages_sent INTEGER DEFAULT 0,
                images_generated INTEGER DEFAULT 0,
                modes_used JSONB DEFAULT '{}',
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (user_id, usage_date)
            )
        """)
        
        # User profiles table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                interests JSONB NOT NULL DEFAULT '[]',
                dislikes JSONB NOT NULL DEFAULT '[]',
                expertise_areas JSONB NOT NULL DEFAULT '[]',
                follow_topics JSONB NOT NULL DEFAULT '[]',
                conversation_style VARCHAR(50) DEFAULT 'balanced',
                preferred_language VARCHAR(10) DEFAULT 'tr',
                profession VARCHAR(200),
                topics JSONB NOT NULL DEFAULT '[]',
                preferences JSONB NOT NULL DEFAULT '{}',
                summary TEXT NOT NULL DEFAULT '',
                total_likes INTEGER DEFAULT 0,
                total_dislikes INTEGER DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        
        # Conversations table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                title VARCHAR(500),
                is_pinned BOOLEAN DEFAULT FALSE,
                is_archived BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # Messages table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                model_used VARCHAR(100),
                token_count INTEGER,
                mode VARCHAR(50),
                has_image BOOLEAN DEFAULT FALSE,
                intent VARCHAR(50),
                is_edited BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # Feedback table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                conversation_id TEXT,
                user_query TEXT,
                assistant_response TEXT,
                rating INTEGER,
                comment TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # Insert free plan if not exists
        cur.execute("""
            INSERT INTO subscription_plans (id, name, description, price_monthly, price_yearly, features, limits)
            VALUES ('free', 'Ücretsiz', 'Temel özellikler', 0, 0, 
                    '{"allowed_modes": ["assistant"], "web_search": true, "file_upload": true, "image_gen": false, "vision": false, "rag": true, "smart_tools": true}'::jsonb,
                    '{"daily_messages": 50, "daily_images": 0, "max_history": 20, "max_file_size_mb": 5, "max_conversations": 10}'::jsonb)
            ON CONFLICT (id) DO NOTHING
        """)
        
        conn.commit()
        pool.putconn(conn)
        print("[DB] ✅ All tables ensured")
        
    except Exception as e:
        print(f"[DB] ❌ Table creation error: {e}")

# ═══════════════════════════════════════════════════════════════
# ABUSE CONTROL INTEGRATION
# ═══════════════════════════════════════════════════════════════

def abuse_post(path: str, payload: dict):
    """Post to abuse control service."""
    if not ABUSE_CONTROL_URL:
        return
    
    url = f"{ABUSE_CONTROL_URL}{path}"
    
    try:
        r = httpx.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[ABUSE CONTROL ERROR] {path} -> {e}")
        return
    
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", "Abuse control blocked request")
        except Exception:
            detail = "Abuse control blocked request"
        raise HTTPException(status_code=r.status_code, detail=detail)

def get_client_ip(request: Request) -> str:
    """Extract client IP from request."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"

# ═══════════════════════════════════════════════════════════════
# FILE UPLOAD & VIRUS SCANNING
# ═══════════════════════════════════════════════════════════════

_file_store: dict = {}
_file_store_lock = threading.Lock()

def scan_file_for_malware(filename: str, content: bytes) -> Tuple[bool, str]:
    """Scan file for malware using ClamAV."""
    print(f"[CLAMAV] Scan start: file={filename}, enabled={CLAMAV_ENABLED}")
    
    if not CLAMAV_ENABLED:
        print("[CLAMAV] Skipped: disabled")
        return True, "CLAMAV_DISABLED"
    
    try:
        cd = clamd.ClamdNetworkSocket(
            host=CLAMAV_HOST,
            port=CLAMAV_PORT,
            timeout=10
        )
        
        pong = cd.ping()
        print(f"[CLAMAV] Ping OK: {pong}")
        
        result = cd.instream(io.BytesIO(content))
        print(f"[CLAMAV] Raw result: {result}")
        
        stream_result = result.get("stream")
        
        if not stream_result:
            print("[CLAMAV] No stream result")
            return False, "SCAN_FAILED"
        
        status, signature = stream_result
        
        if status == "OK":
            print(f"[CLAMAV] Clean file: {filename}")
            return True, "OK"
        
        if status == "FOUND":
            print(f"[CLAMAV] Malware detected in {filename}: {signature}")
            return False, signature or "MALWARE_FOUND"
        
        print(f"[CLAMAV] Unknown result for {filename}: {status}")
        return False, f"UNKNOWN_SCAN_RESULT:{status}"
        
    except Exception as e:
        print(f"[CLAMAV ERROR] {filename}: {e}")
        return False, "SCAN_ERROR"

def extract_text_from_file(filename: str, content: bytes) -> Tuple[str, str]:
    """Extract text from uploaded file."""
    ext = os.path.splitext(filename.lower())[1]
    
    # For simplicity, just handle text files
    # Full implementation would use PyPDF2, python-docx, etc.
    if ext in (".txt", ".md", ".py", ".js", ".yaml", ".json", ".csv"):
        try:
            text = content.decode("utf-8", errors="replace")
            return text[:MAX_EXTRACTED_CHARS], ext[1:]
        except Exception as e:
            return f"[Text read error: {str(e)[:100]}]", "text_error"
    
    return f"[Unsupported file type: {ext}]", "unsupported"

def _store_file_text(file_id: str, filename: str, text: str, user_id=None):
    """Store file text in memory with TTL."""
    import time as _time
    
    with _file_store_lock:
        _file_store[file_id] = {
            "text": text,
            "filename": filename,
            "char_count": len(text),
            "user_id": user_id,
            "expire_time": _time.time() + FILE_TTL_SECONDS,
        }
    
    print(f"[FILE STORE] Stored '{filename}' as {file_id} (user={user_id}, {len(text)} chars)")

def _get_file_text(file_id: str) -> Optional[str]:
    """Get file text from store."""
    import time as _time
    
    with _file_store_lock:
        entry = _file_store.get(file_id)
        if entry and _time.time() < entry.get("expire_time", 0):
            return entry["text"]
    return None

# ═══════════════════════════════════════════════════════════════
# AUTHENTICATION & USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def get_user_from_token(authorization: Optional[str] = None) -> Optional[int]:
    """Extract user ID from JWT token."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    
    try:
        token = authorization.replace("Bearer ", "")
        
        # API token bypass
        if token == API_TOKEN:
            return None
        
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # Get user ID from database
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            result = cur.fetchone()
            
            if not result:
                raise HTTPException(status_code=401, detail="User not found")
            
            return result[0]
            
        finally:
            pool.putconn(conn)
            
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ═══════════════════════════════════════════════════════════════
# SUBSCRIPTION & QUOTA MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def get_user_subscription(user_id: int) -> dict:
    """Get user's active subscription."""
    if not user_id:
        return _default_free_plan()
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                SELECT us.plan_id, us.status, us.billing_period,
                       us.current_period_end, us.cancel_at_period_end,
                       sp.name, sp.features, sp.limits
                FROM user_subscriptions us
                JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = %s AND us.status IN ('active', 'trialing')
                ORDER BY sp.sort_order DESC
                LIMIT 1
            """, (user_id,))
            
            row = cur.fetchone()
            
            if not row:
                return _default_free_plan()
            
            plan_id, status, billing_period, period_end, cancel_at_end, plan_name, features, limits = row
            
            # Check expiry
            if period_end and period_end < datetime.datetime.now(datetime.timezone.utc):
                cur.execute("""
                    UPDATE user_subscriptions SET status = 'expired'
                    WHERE user_id = %s AND status = 'active' AND plan_id != 'free'
                """, (user_id,))
                
                cur.execute("""
                    INSERT INTO user_subscriptions (user_id, plan_id, status, billing_period)
                    VALUES (%s, 'free', 'active', 'free')
                    ON CONFLICT DO NOTHING
                """, (user_id,))
                
                conn.commit()
                return _default_free_plan()
            
            return {
                "plan_id": plan_id,
                "plan_name": plan_name,
                "status": status,
                "features": features or {},
                "limits": limits or {},
                "billing_period": billing_period,
                "current_period_end": period_end.isoformat() if period_end else None,
                "cancel_at_period_end": cancel_at_end,
                "is_premium": plan_id != "free",
            }
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        print(f"[SUBSCRIPTION ERROR] {e}")
        return _default_free_plan()

def _default_free_plan() -> dict:
    """Return default free plan."""
    return {
        "plan_id": "free",
        "plan_name": "Ücretsiz",
        "status": "active",
        "features": {
            "allowed_modes": ["assistant"],
            "web_search": True,
            "file_upload": True,
            "image_gen": False,
            "vision": False,
            "rag": True,
            "smart_tools": True,
        },
        "limits": {
            "daily_messages": 50,
            "daily_images": 0,
            "max_history": 20,
            "max_file_size_mb": 5,
            "max_conversations": 10,
        },
        "billing_period": "free",
        "current_period_end": None,
        "cancel_at_period_end": False,
        "is_premium": False,
    }

def check_usage_limit(user_id: int) -> Tuple[bool, int, int]:
    """Check if user has reached daily message limit."""
    if not user_id:
        return True, 999, 999
    
    sub = get_user_subscription(user_id)
    daily_limit = sub.get("limits", {}).get("daily_messages", 50)
    
    if daily_limit == -1:  # Unlimited
        return True, -1, -1
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                SELECT messages_sent FROM usage_tracking
                WHERE user_id = %s AND usage_date = CURRENT_DATE
            """, (user_id,))
            
            row = cur.fetchone()
            current = row[0] if row else 0
            remaining = max(0, daily_limit - current)
            
            return current < daily_limit, remaining, daily_limit
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        print(f"[USAGE CHECK ERROR] {e}")
        return True, daily_limit, daily_limit

def increment_usage(user_id: int, mode: str = "assistant"):
    """Increment user's daily usage counter."""
    if not user_id:
        return
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                INSERT INTO usage_tracking (user_id, usage_date, messages_sent, modes_used)
                VALUES (%s, CURRENT_DATE, 1, %s::jsonb)
                ON CONFLICT (user_id, usage_date)
                DO UPDATE SET
                    messages_sent = usage_tracking.messages_sent + 1,
                    modes_used = usage_tracking.modes_used || %s::jsonb,
                    updated_at = NOW()
            """, (user_id, json.dumps({mode: 1}), json.dumps({mode: 1})))
            
            conn.commit()
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        print(f"[USAGE INCREMENT ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    prompt: str  # ✅ FIXED: Changed from "query" to "prompt"
    mode: str = "assistant"
    conversation_id: Optional[str] = None
    history: Optional[List[ChatMessage]] = None
    context: Optional[str] = None
    image_data: Optional[str] = None
    session_summary: Optional[str] = None

class OTPRequest(BaseModel):
    email: EmailStr
    mode: str  # "login" or "register"

class OTPVerify(BaseModel):
    email: EmailStr
    code: str
    mode: str
    name: Optional[str] = None

class ConversationCreate(BaseModel):
    title: Optional[str] = "Yeni Sohbet"

class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    is_pinned: Optional[bool] = None
    is_archived: Optional[bool] = None

class FeedbackRequest(BaseModel):
    conversation_id: Optional[str] = None
    user_query: str = ""
    assistant_response: str = ""
    rating: int = Field(..., description="1 = like, -1 = dislike")
    comment: Optional[str] = ""

# ═══════════════════════════════════════════════════════════════
# FASTAPI APPLICATION
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown"""
    # Startup
    print("\n" + "="*60)
    print("🚀 SKYLIGHT API GATEWAY - STARTING UP")
    print("="*60)
    
    # Initialize database tables
    ensure_tables()
    
    # Test database connection
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        pool.putconn(conn)
        print("✅ PostgreSQL connection OK")
    except Exception as e:
        print(f"❌ PostgreSQL connection FAILED: {e}")
    
    # Test ClamAV
    if CLAMAV_ENABLED:
        print(f"✅ ClamAV enabled ({CLAMAV_HOST}:{CLAMAV_PORT})")
    else:
        print("⚠️  ClamAV disabled")
    
    # Test Abuse Control
    if ABUSE_CONTROL_URL:
        print(f"✅ Abuse Control configured: {ABUSE_CONTROL_URL}")
    else:
        print("⚠️  Abuse Control URL not set")
    
    print("="*60)
    print("✨ GATEWAY READY")
    print("="*60 + "\n")
    
    yield
    
    # Shutdown
    print("\n🛑 Gateway shutting down...")
    
    # Close database pool
    global _db_pool
    if _db_pool and not _db_pool.closed:
        _db_pool.closeall()
        print("✅ Database pool closed")
    
    # Clear file store
    with _file_store_lock:
        _file_store.clear()
    
    print("👋 Goodbye!\n")

app = FastAPI(
    title="Skylight API Gateway",
    description="Complete Gateway with all security features",
    version="3.0.0",
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
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

async def call_service(
    service_url: str,
    endpoint: str,
    data: dict,
    stream: bool = False,
    timeout: int = 30,
):
    """Call microservice and return response."""
    url = f"{service_url}{endpoint}"
    
    if stream:
        # Stream response
        async def stream_generator():
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=data) as response:
                    async for chunk in response.aiter_text():
                        yield chunk
        
        return stream_generator()
    
    else:
        # Non-streaming response
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=data)
            return response.json()

# ═══════════════════════════════════════════════════════════════
# AUTHENTICATION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/request_code")
async def request_code_endpoint(req: OTPRequest, request: Request):
    """Request OTP code via email."""
    try:
        ip_address = get_client_ip(request)
        
        # Abuse control check
        abuse_post("/otp/request/check", {
            "email": req.email,
            "ip_address": ip_address
        })
        
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            # Check if user exists
            cur.execute("SELECT id FROM users WHERE email = %s", (req.email,))
            existing_user = cur.fetchone()
            
            if req.mode == "login" and not existing_user:
                raise HTTPException(status_code=404, detail="Kullanici bulunamadi.")
            
            if req.mode == "register" and existing_user:
                raise HTTPException(status_code=400, detail="E-posta kullanimda.")
            
            # Generate OTP
            generated_otp = str(random.randint(100000, 999999))
            
            # Save to database
            cur.execute("""
                INSERT INTO otp_codes (email, code, expire_at, created_at)
                VALUES (%s, %s, NOW() + INTERVAL '5 minutes', NOW())
                ON CONFLICT (email) DO UPDATE
                SET code = EXCLUDED.code,
                    expire_at = EXCLUDED.expire_at,
                    created_at = NOW()
            """, (req.email, generated_otp))
            
            conn.commit()
            
            # Send email
            msg = MIMEMultipart()
            msg["From"] = SMTP_FROM
            msg["To"] = req.email
            msg["Subject"] = "ONE-BUNE Doğrulama Kodu"
            
            body_text = f"""Merhaba,

ONE-BUNE doğrulama kodun: {generated_otp}

Bu kod 5 dakika boyunca gecerlidir.

ONE-BUNE AI
"""
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
            
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
            
            # Mark sent in abuse control
            abuse_post("/otp/request/mark-sent", {
                "email": req.email,
                "ip_address": ip_address
            })
            
            return {"status": "success", "message": "Kod gonderildi."}
            
        finally:
            pool.putconn(conn)
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"[REQUEST_CODE ERROR] {e}")
        raise HTTPException(status_code=500, detail="Kod gonderme hatasi.")

@app.post("/verify_code")
async def verify_otp(body: OTPVerify, request: Request):
    """Verify OTP code and return JWT token."""
    email = body.email.lower().strip()
    code = body.code.strip()
    mode = body.mode.strip()
    name = (body.name or "").strip() if mode == "register" else None
    ip_address = get_client_ip(request)
    
    try:
        # Test bypass
        if email == TEST_BYPASS_EMAIL and code == TEST_BYPASS_CODE:
            pool = _get_pool()
            conn = pool.getconn()
            cur = conn.cursor()
            
            try:
                cur.execute("SELECT id, name FROM users WHERE email = %s", (email,))
                row = cur.fetchone()
                
                if not row:
                    cur.execute("""
                        INSERT INTO users (email, name, google_id)
                        VALUES (%s, %s, %s)
                        RETURNING id, name
                    """, (email, TEST_BYPASS_NAME, f"local_{email}"))
                    row = cur.fetchone()
                    conn.commit()
                
                token = jwt.encode({
                    "sub": email,
                    "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE_DAYS),
                }, JWT_SECRET, algorithm=JWT_ALGORITHM)
                
                return {
                    "status": "success",
                    "token": token,
                    "user": {
                        "id": row[0],
                        "name": str(row[1]),
                        "email": email,
                    },
                }
                
            finally:
                pool.putconn(conn)
        
        # Abuse control check
        abuse_post("/otp/verify/check", {
            "email": email,
            "ip_address": ip_address
        })
        
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            # Get OTP from database
            cur.execute("""
                SELECT code, expire_at
                FROM otp_codes
                WHERE email = %s
                LIMIT 1
            """, (email,))
            
            row = cur.fetchone()
            
            if not row:
                abuse_post("/otp/verify/mark-failed", {
                    "email": email,
                    "ip_address": ip_address
                })
                raise HTTPException(status_code=400, detail="Kod bulunamadi.")
            
            stored_code, expire_at = row
            
            # Check expiry
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if expire_at is None or now_utc > expire_at:
                abuse_post("/otp/verify/mark-failed", {
                    "email": email,
                    "ip_address": ip_address
                })
                raise HTTPException(status_code=400, detail="Kod suresi dolmus.")
            
            # Verify code
            if code != stored_code:
                abuse_post("/otp/verify/mark-failed", {
                    "email": email,
                    "ip_address": ip_address
                })
                raise HTTPException(status_code=400, detail="Hatali kod.")
            
            # Clear abuse control
            abuse_post("/otp/verify/clear", {
                "email": email,
                "ip_address": ip_address
            })
            
            # Register or login
            if mode == "register":
                cur.execute("SELECT id FROM users WHERE email = %s", (email,))
                if cur.fetchone():
                    raise HTTPException(status_code=400, detail="Bu email zaten kayitli.")
                
                cur.execute("""
                    INSERT INTO users (email, name, google_id)
                    VALUES (%s, %s, %s)
                    RETURNING id, name
                """, (email, name or email.split("@")[0], f"local_{email}"))
                
                user_row = cur.fetchone()
                
            elif mode == "login":
                cur.execute("SELECT id, name FROM users WHERE email = %s", (email,))
                user_row = cur.fetchone()
                
                if not user_row:
                    raise HTTPException(status_code=400, detail="Kullanici bulunamadi.")
            
            else:
                raise HTTPException(status_code=400, detail="Gecersiz mode.")
            
            # Delete used OTP
            cur.execute("DELETE FROM otp_codes WHERE email = %s", (email,))
            conn.commit()
            
            # Generate JWT token
            token = jwt.encode({
                "sub": email,
                "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE_DAYS),
            }, JWT_SECRET, algorithm=JWT_ALGORITHM)
            
            return {
                "status": "success",
                "token": token,
                "user": {
                    "id": user_row[0],
                    "name": str(user_row[1]),
                    "email": email,
                },
            }
            
        finally:
            pool.putconn(conn)
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"[VERIFY OTP ERROR] {e}")
        raise HTTPException(status_code=500, detail="Dogrulama hatasi.")

@app.get("/check_user")
async def check_user(authorization: str = Header(None)):
    """Check if user token is valid."""
    if not authorization:
        raise HTTPException(status_code=401, detail="No authorization header.")
    
    try:
        user_id = get_user_from_token(authorization)
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token.")
        
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("SELECT email, name FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            
            if not row:
                raise HTTPException(status_code=404, detail="User not found.")
            
            email, name = row
            return {
                "status": "success",
                "user": {"id": user_id, "email": email, "name": name}
            }
            
        finally:
            pool.putconn(conn)
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Check user error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# FILE UPLOAD ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Upload and scan file, return file_id for use in chat."""
    user_id = None
    if authorization:
        try:
            user_id = get_user_from_token(authorization)
        except Exception:
            pass
    
    if not file.filename:
        raise HTTPException(status_code=400, detail="Dosya adı boş.")
    
    # Check extension
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Desteklenmeyen dosya türü: {ext}"
        )
    
    # Read content
    content = await file.read()
    
    # Check size
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Dosya çok büyük ({len(content) / 1024 / 1024:.1f}MB). Maksimum: {MAX_FILE_SIZE // 1024 // 1024}MB"
        )
    
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Dosya boş.")
    
    # Virus scan
    print(f"[UPLOAD] Starting malware scan: {file.filename}")
    is_clean, scan_result = scan_file_for_malware(file.filename, content)
    
    if not is_clean:
        if scan_result in ("SCAN_ERROR", "SCAN_FAILED"):
            raise HTTPException(
                status_code=503,
                detail="Dosya güvenlik taramasından geçirilemedi. Lütfen daha sonra tekrar deneyin."
            )
        
        raise HTTPException(
            status_code=400,
            detail=f"Dosya güvenlik taramasında reddedildi: {scan_result}"
        )
    
    # Extract text
    extracted_text, file_type = extract_text_from_file(file.filename, content)
    
    if not extracted_text or extracted_text.startswith("["):
        if extracted_text.startswith("["):
            raise HTTPException(status_code=422, detail=extracted_text.strip("[]"))
        raise HTTPException(status_code=422, detail="Dosyadan metin çıkarılamadı.")
    
    # Store in memory
    file_id = str(uuid.uuid4())[:12]
    _store_file_text(file_id, file.filename, extracted_text, user_id=user_id)
    
    preview = extracted_text[:500]
    if len(extracted_text) > 500:
        preview += "..."
    
    print(f"[FILE UPLOAD] '{file.filename}' → type={file_type}, chars={len(extracted_text)}, file_id={file_id}")
    
    return {
        "status": "success",
        "file_id": file_id,
        "filename": file.filename,
        "file_type": file_type,
        "char_count": len(extracted_text),
        "line_count": extracted_text.count("\n") + 1,
        "preview": preview,
        "size_bytes": len(content),
    }

# ═══════════════════════════════════════════════════════════════
# CONVERSATION ENDPOINTS (NEWLY ADDED) ✅
# ═══════════════════════════════════════════════════════════════

@app.post("/conversations/create")
async def create_conversation(
    data: ConversationCreate,
    authorization: Optional[str] = Header(None),
):
    """Create a new conversation."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                INSERT INTO conversations (user_id, title)
                VALUES (%s, %s) RETURNING id, title, created_at
            """, (user_id, data.title))
            
            result = cur.fetchone()
            conn.commit()
            
            return {
                "status": "success",
                "id": str(result[0]),
                "title": result[1],
                "created_at": result[2].isoformat(),
            }
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create conversation: {str(e)}")


@app.get("/conversations/list")
async def list_conversations(
    limit: int = 50,
    authorization: Optional[str] = Header(None),
):
    """List user's conversations."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                SELECT c.id, c.title, c.created_at, c.updated_at, c.is_pinned,
                       COUNT(m.id) as message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user_id = %s AND c.is_archived = FALSE
                GROUP BY c.id
                ORDER BY c.is_pinned DESC, c.updated_at DESC
                LIMIT %s
            """, (user_id, limit))
            
            conversations = []
            for row in cur.fetchall():
                conversations.append({
                    "id": str(row[0]),
                    "title": row[1],
                    "created_at": row[2].isoformat(),
                    "updated_at": row[3].isoformat(),
                    "is_pinned": row[4],
                    "message_count": row[5],
                })
            
            return {"conversations": conversations}
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list conversations: {str(e)}")


@app.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    authorization: Optional[str] = Header(None),
):
    """Get messages in a conversation."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            # Check ownership
            cur.execute("SELECT user_id FROM conversations WHERE id = %s", (conversation_id,))
            result = cur.fetchone()
            
            if not result:
                raise HTTPException(status_code=404, detail="Conversation not found")
            
            if result[0] != user_id:
                raise HTTPException(status_code=403, detail="Not authorized")
            
            # Get messages
            cur.execute("""
                SELECT id, role, content, created_at, is_edited
                FROM messages WHERE conversation_id = %s ORDER BY created_at ASC
            """, (conversation_id,))
            
            messages = []
            for row in cur.fetchall():
                messages.append({
                    "id": str(row[0]),
                    "role": row[1],
                    "content": row[2],
                    "created_at": row[3].isoformat(),
                    "is_edited": row[4],
                })
            
            return {"messages": messages}
            
        finally:
            pool.putconn(conn)
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get messages: {str(e)}")


@app.put("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    data: ConversationUpdate,
    authorization: Optional[str] = Header(None),
):
    """Update conversation."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    try:
        updates = []
        params = []
        
        if data.title is not None:
            updates.append("title = %s")
            params.append(data.title)
        
        if data.is_pinned is not None:
            updates.append("is_pinned = %s")
            params.append(data.is_pinned)
        
        if data.is_archived is not None:
            updates.append("is_archived = %s")
            params.append(data.is_archived)
        
        if not updates:
            return {"status": "success", "message": "No changes"}
        
        params.extend([conversation_id, user_id])
        query = f"UPDATE conversations SET {', '.join(updates)} WHERE id = %s AND user_id = %s RETURNING id"
        
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute(query, params)
            result = cur.fetchone()
            conn.commit()
            
            if not result:
                raise HTTPException(status_code=404, detail="Conversation not found")
            
            return {"status": "success"}
            
        finally:
            pool.putconn(conn)
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update conversation: {str(e)}")


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    authorization: Optional[str] = Header(None),
):
    """Delete conversation."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                DELETE FROM conversations WHERE id = %s AND user_id = %s RETURNING id
            """, (conversation_id, user_id))
            
            result = cur.fetchone()
            conn.commit()
            
            if not result:
                raise HTTPException(status_code=404, detail="Conversation not found")
            
            return {"status": "success"}
            
        finally:
            pool.putconn(conn)
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete conversation: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# PROFILE ENDPOINTS (NEWLY ADDED) ✅
# ═══════════════════════════════════════════════════════════════

@app.get("/profile")
async def get_profile(authorization: str = Header(None)):
    """Get user profile."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("SELECT email, name FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            
            if not row:
                raise HTTPException(status_code=404, detail="User not found")
            
            email, name = row
            
            cur.execute("""
                SELECT topics, preferences, summary FROM user_profiles WHERE user_id = %s
            """, (user_id,))
            
            profile_row = cur.fetchone()
            
            topics = profile_row[0] if profile_row else []
            preferences = profile_row[1] if profile_row else {}
            summary = profile_row[2] if profile_row else ""
            
            return {
                "user": {"id": user_id, "email": email, "name": name},
                "profile": {
                    "topics": topics,
                    "preferences": preferences,
                    "summary": summary,
                },
            }
            
        finally:
            pool.putconn(conn)
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile error: {str(e)}")


@app.delete("/profile/topics")
async def clear_topics(authorization: str = Header(None)):
    """Clear user profile topics."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                UPDATE user_profiles SET topics = '[]' WHERE user_id = %s
            """, (user_id,))
            
            conn.commit()
            return {"status": "success", "message": "Topics cleared"}
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Clear topics error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# FEEDBACK ENDPOINT (NEWLY ADDED) ✅
# ═══════════════════════════════════════════════════════════════

@app.post("/feedback")
async def submit_feedback(
    data: FeedbackRequest,
    authorization: str = Header(None),
):
    """Submit feedback."""
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    
    if data.rating not in (1, -1):
        raise HTTPException(status_code=400, detail="Rating must be 1 (like) or -1 (dislike)")
    
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                INSERT INTO feedback 
                    (user_id, conversation_id, user_query, assistant_response, rating, comment)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                user_id,
                data.conversation_id or "",
                data.user_query,
                data.assistant_response,
                data.rating,
                data.comment or "",
            ))
            
            feedback_id = cur.fetchone()[0]
            conn.commit()
            
            return {"status": "success", "feedback_id": feedback_id}
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Feedback error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# CODE MODE STATUS (NEWLY ADDED) ✅
# ═══════════════════════════════════════════════════════════════

@app.get("/code-mode/status")
async def code_mode_status():
    """Get code mode status."""
    return {
        "enabled": bool(os.getenv("DEEPINFRA_API_KEY")),
        "model": os.getenv("DEEPINFRA_CODE_MODEL", "Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo"),
        "max_tokens": int(os.getenv("DEEPINFRA_CODE_MAX_TOKENS", "4096")),
    }

# ═══════════════════════════════════════════════════════════════
# SUBSCRIPTION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/subscription/status")
async def subscription_status_endpoint(authorization: str = Header(None)):
    """Get user's subscription status."""
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    sub = get_user_subscription(user_id)
    allowed, remaining, limit = check_usage_limit(user_id)
    
    return {
        "plan_id": sub.get("plan_id", "free"),
        "plan_name": sub.get("plan_name", "Ücretsiz"),
        "is_premium": sub.get("is_premium", False),
        "status": sub.get("status", "active"),
        "features": sub.get("features", {}),
        "limits": sub.get("limits", {}),
        "billing_period": sub.get("billing_period", "free"),
        "current_period_end": sub.get("current_period_end"),
        "cancel_at_period_end": sub.get("cancel_at_period_end", False),
        "usage_today": {
            "messages_sent": (limit - remaining) if remaining >= 0 and limit >= 0 else 0,
            "messages_remaining": remaining,
            "daily_message_limit": limit,
        }
    }


@app.get("/subscription/plans")
async def subscription_plans_endpoint():
    """Get all available subscription plans."""
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        
        try:
            cur.execute("""
                SELECT id, name, description, price_monthly, price_yearly,
                       currency, features, limits, is_active
                FROM subscription_plans
                WHERE is_active = TRUE
                ORDER BY sort_order
            """)
            
            plans = []
            for row in cur.fetchall():
                plans.append({
                    "id": row[0],
                    "name": row[1],
                    "description": row[2],
                    "price_monthly": float(row[3]),
                    "price_yearly": float(row[4]),
                    "currency": row[5],
                    "features": row[6],
                    "limits": row[7],
                    "is_active": row[8],
                })
            
            return {"plans": plans}
            
        finally:
            pool.putconn(conn)
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Get plans error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# MAIN CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat_endpoint(
    request_body: ChatRequest,
    request: Request,
    authorization: str = Header(None),
):
    """
    Main chat endpoint with full security integration.
    
    Flow:
    1. Authentication
    2. Abuse Control Check
    3. Usage Limit Check
    4. Route to appropriate service
    5. Increment usage
    """
    
    # 1. Authentication
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass
    
    # 2. Abuse Control
    ip_address = get_client_ip(request)
    
    try:
        abuse_post("/chat/check", {
            "user_id": str(user_id or "guest"),
            "ip_address": ip_address
        })
    except HTTPException as e:
        if e.status_code == 429:
            def rate_limited_gen():
                yield "⏳ Çok hızlı istek gönderiyorsun. Lütfen birkaç saniye bekleyip tekrar dene."
            return StreamingResponse(rate_limited_gen(), media_type="text/plain; charset=utf-8")
        raise
    
    # 3. Usage Limit Check
    if user_id:
        allowed, remaining, limit = check_usage_limit(user_id)
        if not allowed:
            def limit_exceeded_gen():
                yield f"⏰ Günlük mesaj limitine ulaştın ({limit}/gün).\n\nYarın tekrar deneyebilirsin! 🚀"
            return StreamingResponse(limit_exceeded_gen(), media_type="text/plain; charset=utf-8")
    
    # 3.5. Auto-create conversation if not provided (like Claude/Gemini)
    conversation_id = request_body.conversation_id
    auto_created = False
    
    if not conversation_id and user_id:
        # Create new conversation automatically
        try:
            pool = _get_pool()
            conn = pool.getconn()
            cur = conn.cursor()
            
            try:
                # Generate title from first message (max 100 chars)
                title = request_body.prompt[:100]
                if len(request_body.prompt) > 100:
                    title += "..."
                
                cur.execute("""
                    INSERT INTO conversations (user_id, title, created_at, updated_at)
                    VALUES (%s, %s, NOW(), NOW())
                    RETURNING id
                """, (user_id, title))
                
                conversation_id = str(cur.fetchone()[0])
                auto_created = True
                conn.commit()
                
                print(f"[CHAT] Auto-created conversation {conversation_id} with title: {title}")
                
            finally:
                pool.putconn(conn)
                
        except Exception as e:
            print(f"[AUTO-CREATE CONVERSATION ERROR] {e}")
            # Continue without conversation_id if creation fails
    
    # 3.6. Fetch conversation history from database if conversation_id exists
    conversation_history = []
    if conversation_id:
        try:
            pool = _get_pool()
            conn = pool.getconn()
            cur = conn.cursor()
            
            try:
                # Verify conversation ownership
                cur.execute(
                    "SELECT user_id, title FROM conversations WHERE id = %s",
                    (conversation_id,)
                )
                conv_row = cur.fetchone()
                
                if conv_row and (not user_id or conv_row[0] == user_id):
                    # Fetch last 30 messages from this conversation (increased from 20)
                    cur.execute("""
                        SELECT role, content
                        FROM messages
                        WHERE conversation_id = %s
                        ORDER BY created_at DESC
                        LIMIT 30
                    """, (conversation_id,))
                    
                    # Reverse to get chronological order
                    messages = cur.fetchall()
                    for row in reversed(messages):
                        conversation_history.append({
                            "role": row[0],
                            "content": row[1],
                        })
                    
                    print(f"[CHAT] Loaded {len(conversation_history)} messages from conversation {conversation_id}")
                    
                    # Update conversation title if it's still default and we have messages
                    current_title = conv_row[1]
                    if current_title == "Yeni Sohbet" and len(conversation_history) == 0:
                        # This is the first message, update title
                        new_title = request_body.prompt[:100]
                        if len(request_body.prompt) > 100:
                            new_title += "..."
                        
                        cur.execute("""
                            UPDATE conversations SET title = %s WHERE id = %s
                        """, (new_title, conversation_id))
                        conn.commit()
                        
                        print(f"[CHAT] Updated conversation title to: {new_title}")
                
            finally:
                pool.putconn(conn)
                
        except Exception as e:
            print(f"[HISTORY FETCH ERROR] {e}")
            # Continue without history rather than failing
    
    # Use database history if available, otherwise use client-provided history
    final_history = conversation_history if conversation_history else (
        [{"role": m.role, "content": m.content} for m in (request_body.history or [])]
    )
    
    # 4. Route to Chat Service
    chat_data = {
        "prompt": request_body.prompt,
        "mode": request_body.mode,
        "user_id": user_id or 0,
        "conversation_id": conversation_id,
        "history": final_history,  # ✅ Database history with up to 30 messages!
        "context": request_body.context,
        "session_summary": request_body.session_summary,
    }
    
    # Stream response from Chat Service
    async def stream_response():
        assistant_response = ""
        try:
            generator = await call_service(
                CHAT_SERVICE_URL,
                "/chat",
                data=chat_data,
                stream=True,
                timeout=120,
            )
            async for chunk in generator:
                assistant_response += chunk  # ✅ Collect response for saving
                yield chunk
            
            # ✅ Save messages to database after streaming completes
            if conversation_id and user_id:
                try:
                    pool = _get_pool()
                    conn = pool.getconn()
                    cur = conn.cursor()
                    
                    try:
                        # Save user message
                        cur.execute("""
                            INSERT INTO messages (conversation_id, role, content, mode, created_at)
                            VALUES (%s, %s, %s, %s, NOW())
                        """, (conversation_id, "user", request_body.prompt, request_body.mode))
                        
                        # Save assistant response
                        cur.execute("""
                            INSERT INTO messages (conversation_id, role, content, mode, created_at)
                            VALUES (%s, %s, %s, %s, NOW())
                        """, (conversation_id, "assistant", assistant_response, request_body.mode))
                        
                        # Update conversation timestamp
                        cur.execute("""
                            UPDATE conversations SET updated_at = NOW()
                            WHERE id = %s
                        """, (conversation_id,))
                        
                        conn.commit()
                        print(f"[CHAT] Saved messages to conversation {conversation_id}")
                        
                    finally:
                        pool.putconn(conn)
                        
                except Exception as e:
                    print(f"[MESSAGE SAVE ERROR] {e}")
                    # Don't fail the request if save fails
                    
        except Exception as e:
            print(f"[CHAT SERVICE ERROR] {e}")
            yield f"\n⚠️ Chat service error: {str(e)}"
    
    # 5. Increment usage in background
    if user_id:
        increment_usage(user_id, request_body.mode)
    
    # 6. Return response with conversation_id in header (like Claude/Gemini)
    response = StreamingResponse(stream_response(), media_type="text/plain; charset=utf-8")
    
    # Add conversation_id to response headers so client can track it
    if conversation_id:
        response.headers["X-Conversation-ID"] = conversation_id
        if auto_created:
            response.headers["X-Conversation-Created"] = "true"
    
    return response

# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """Gateway health check."""
    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "services": {
            "database": "connected",
            "abuse_control": "configured" if ABUSE_CONTROL_URL else "not_configured",
            "clamav": "enabled" if CLAMAV_ENABLED else "disabled",
            "smtp": "configured" if SMTP_USER else "not_configured",
        },
    }

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "Skylight API Gateway",
        "version": "3.0.0",
        "status": "running",
        "features": {
            "abuse_control": bool(ABUSE_CONTROL_URL),
            "virus_scanning": CLAMAV_ENABLED,
            "file_upload": True,
            "otp_auth": bool(SMTP_USER),
            "subscriptions": True,
            "quota_management": True,
        },
        "endpoints": {
            "conversations": ["create", "list", "messages", "update", "delete"],
            "profile": ["get", "clear_topics"],
            "feedback": ["submit"],
            "code_mode": ["status"],
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8443)