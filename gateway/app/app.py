"""
═══════════════════════════════════════════════════════════════
SKYLIGHT API GATEWAY - VERSION 3.1 (INTELLIGENT CONTEXT ROUTING)
═══════════════════════════════════════════════════════════════
v3.1 FIXES & ADDITIONS:
  ✅ JWT_SECRET zorunlu — default yok, güvenli
  ✅ ChatRequest.prompt max_length=10000
  ✅ UUID validation — SQL injection önlemi
  ✅ increment_usage stream SONRASI çağrılıyor (önceden önce çağrılıyordu)
  ✅ get_conversation_context last_mode bug düzeltildi
  ✅ _detect_image_gen "kod" exclusion bug düzeltildi
  ✅ Code mode → prompt zenginleştirme (kod canavarı modu)
  ✅ GITHUB_ANALYSIS intent eklendi
  ✅ DIAGRAM_GEN intent eklendi
  ✅ Nginx security header kontrolü
═══════════════════════════════════════════════════════════════
"""

import os
import io
import re
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
from enum import Enum

import jwt
import httpx
import clamd
import psycopg2
import psycopg2.pool
from fastapi import FastAPI, HTTPException, Header, Request, File, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr, validator

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

CHAT_SERVICE_URL           = os.getenv("CHAT_SERVICE_URL",           "http://skylight-chat:8082")
RAG_SERVICE_URL            = os.getenv("RAG_SERVICE_URL",            "http://skylight-rag:8084")
IMAGE_GEN_SERVICE_URL      = os.getenv("IMAGE_GEN_SERVICE_URL",      "http://skylight-image-gen:8083")
IMAGE_ANALYSIS_SERVICE_URL = os.getenv("IMAGE_ANALYSIS_SERVICE_URL", "http://skylight-image-analysis:8002")
SMART_TOOLS_URL            = os.getenv("SMART_TOOLS_URL",            "http://skylight-smart-tools:8081")
ABUSE_CONTROL_URL          = os.getenv("ABUSE_CONTROL_URL",          "http://skylight-bot-abuse-control:8010")
GITHUB_ANALYSIS_URL        = os.getenv("GITHUB_ANALYSIS_URL",        "http://skylight-github-analysis:8086")

# ── SECURITY: JWT_SECRET zorunlu, default YOK ──────────────────
_JWT_SECRET_RAW = os.getenv("JWT_SECRET", "")
if not _JWT_SECRET_RAW:
    # Geliştirme ortamında uyarı ver, production'da crash et
    import sys
    if os.getenv("ENV", "production") == "production":
        print("❌ FATAL: JWT_SECRET environment variable is required in production!")
        sys.exit(1)
    else:
        _JWT_SECRET_RAW = "dev-only-insecure-secret-do-not-use-in-prod"
        print("⚠️  WARNING: JWT_SECRET not set, using insecure dev default!")

JWT_SECRET        = _JWT_SECRET_RAW
API_TOKEN         = os.getenv("API_TOKEN",         "")
JWT_ALGORITHM     = os.getenv("JWT_ALGORITHM",     "HS256")
TOKEN_EXPIRE_DAYS = int(os.getenv("TOKEN_EXPIRE_DAYS", "7"))

DB_HOST     = os.getenv("DB_HOST",     "postgres")
DB_PORT     = os.getenv("DB_PORT",     "5432")
DB_NAME     = os.getenv("DB_NAME",     "")
DB_USER     = os.getenv("DB_USER",     "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.hostinger.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
SMTP_FROM   = os.getenv("SMTP_FROM", SMTP_USER or "noreply@one-bune.com")

CLAMAV_ENABLED = os.getenv("CLAMAV_ENABLED", "true").lower() == "true"
CLAMAV_HOST    = os.getenv("CLAMAV_HOST", "skylight-bot-antivirus")
CLAMAV_PORT    = int(os.getenv("CLAMAV_PORT", "3310"))

TEST_BYPASS_EMAIL = "test@one-bune.com"
TEST_BYPASS_CODE  = "138113"
TEST_BYPASS_NAME  = "Test User"

MAX_FILE_SIZE       = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 30000
FILE_TTL_SECONDS    = 600

ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".tsv",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".r", ".sql", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".cmd",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".properties", ".xml", ".html", ".css",
    ".dockerfile", ".tf", ".hcl", ".j2", ".jinja2", ".log",
}

# ═══════════════════════════════════════════════════════════════
# ROUTING INTENT ENUM
# ═══════════════════════════════════════════════════════════════

class RoutingIntent(str, Enum):
    CHAT             = "chat"              # Normal sohbet
    CODE_DEBUG       = "code_debug"        # Ekran görüntüsünden kod debug
    IMAGE_ANALYSIS   = "image_analysis"    # Genel görsel analiz
    IMAGE_GEN        = "image_gen"         # Görsel oluşturma
    IMAGE_MODIFY     = "image_modify"      # Mevcut görseli değiştir
    CODE_CONTINUE    = "code_continue"     # Kodu devam ettir
    GITHUB_ANALYSIS  = "github_analysis"   # GitHub repo analizi (YENİ)
    DIAGRAM_GEN      = "diagram_gen"       # Diyagram üretme (YENİ)

# ═══════════════════════════════════════════════════════════════
# DATABASE CONNECTION POOL
# ═══════════════════════════════════════════════════════════════

_db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()

def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None or _db_pool.closed:
        with _pool_lock:
            if _db_pool is None or _db_pool.closed:
                _db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2, maxconn=20,
                    host=DB_HOST, database=DB_NAME,
                    user=DB_USER, password=DB_PASSWORD, port=DB_PORT,
                    connect_timeout=10,
                )
                print("[DB POOL] Initialized (2-20 connections)")
    return _db_pool

def get_db():
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
# UUID VALIDATION — SQL Injection önlemi
# ═══════════════════════════════════════════════════════════════

def _validate_uuid(val: Optional[str]) -> Optional[str]:
    """conversation_id gibi UUID alanlarını doğrula. Geçersizse None döner."""
    if not val:
        return None
    try:
        uuid.UUID(str(val))
        return str(val)
    except (ValueError, AttributeError):
        return None

# ═══════════════════════════════════════════════════════════════
# ABUSE CONTROL
# ═══════════════════════════════════════════════════════════════

def abuse_post(path: str, payload: dict):
    if not ABUSE_CONTROL_URL:
        return
    try:
        r = httpx.post(f"{ABUSE_CONTROL_URL}{path}", json=payload, timeout=5)
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
    print(f"[CLAMAV] Scan start: {filename}, enabled={CLAMAV_ENABLED}")
    if not CLAMAV_ENABLED:
        return True, "CLAMAV_DISABLED"
    try:
        cd = clamd.ClamdNetworkSocket(host=CLAMAV_HOST, port=CLAMAV_PORT, timeout=10)
        cd.ping()
        result = cd.instream(io.BytesIO(content))
        sr     = result.get("stream")
        if not sr:
            return False, "SCAN_FAILED"
        status, sig = sr
        if status == "OK":
            return True, "OK"
        if status == "FOUND":
            return False, sig or "MALWARE_FOUND"
        return False, f"UNKNOWN:{status}"
    except Exception as e:
        print(f"[CLAMAV ERROR] {filename}: {e}")
        return False, "SCAN_ERROR"

def extract_text_from_file(filename: str, content: bytes) -> Tuple[str, str]:
    ext = os.path.splitext(filename.lower())[1]
    text_exts = {
        ".txt", ".md", ".py", ".js", ".ts", ".yaml", ".yml",
        ".json", ".csv", ".html", ".css", ".sql", ".sh",
        ".go", ".rs", ".java", ".cs", ".php", ".rb",
        ".tsx", ".jsx", ".toml", ".ini", ".cfg", ".env",
        ".xml", ".dockerfile", ".tf", ".hcl",
    }
    if ext in text_exts:
        try:
            text = content.decode("utf-8", errors="replace")
            return text[:MAX_EXTRACTED_CHARS], ext[1:]
        except Exception as e:
            return f"[Text read error: {str(e)[:100]}]", "text_error"
    return f"[Unsupported file type: {ext}]", "unsupported"

def _store_file_text(file_id: str, filename: str, text: str, user_id=None):
    import time as _t
    with _file_store_lock:
        _file_store[file_id] = {
            "text": text, "filename": filename,
            "char_count": len(text), "user_id": user_id,
            "expire_time": _t.time() + FILE_TTL_SECONDS,
        }
    print(f"[FILE STORE] '{filename}' → {file_id} ({len(text)} chars)")

def _get_file_text(file_id: str) -> Optional[str]:
    import time as _t
    with _file_store_lock:
        entry = _file_store.get(file_id)
        if entry and _t.time() < entry.get("expire_time", 0):
            return entry["text"]
    return None

# ═══════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════

def get_user_from_token(authorization: Optional[str] = None) -> Optional[int]:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    try:
        token = authorization.replace("Bearer ", "").strip()
        if API_TOKEN and token == API_TOKEN:
            return None
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email   = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
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
# SUBSCRIPTION & QUOTA
# ═══════════════════════════════════════════════════════════════

def get_user_subscription(user_id: int) -> dict:
    if not user_id:
        return _default_free_plan()
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                SELECT us.plan_id, us.status, us.billing_period,
                       us.current_period_end, us.cancel_at_period_end,
                       sp.name, sp.features, sp.limits
                FROM user_subscriptions us
                JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = %s AND us.status IN ('active','trialing')
                ORDER BY sp.sort_order DESC LIMIT 1
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                return _default_free_plan()
            plan_id, status, billing_period, period_end, cancel_at_end, plan_name, features, limits = row
            if period_end and period_end < datetime.datetime.now(datetime.timezone.utc):
                cur.execute(
                    "UPDATE user_subscriptions SET status='expired' WHERE user_id=%s AND status='active' AND plan_id!='free'",
                    (user_id,)
                )
                cur.execute(
                    "INSERT INTO user_subscriptions (user_id,plan_id,status,billing_period) VALUES (%s,'free','active','free') ON CONFLICT DO NOTHING",
                    (user_id,)
                )
                conn.commit()
                return _default_free_plan()
            return {
                "plan_id": plan_id, "plan_name": plan_name,
                "status": status, "features": features or {},
                "limits": limits or {}, "billing_period": billing_period,
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
    return {
        "plan_id": "free", "plan_name": "Ücretsiz", "status": "active",
        "features": {
            "allowed_modes": ["assistant"], "image_gen": False,
            "vision": False, "web_search": True, "file_upload": True,
            "rag": True, "smart_tools": True, "github_analysis": False,
        },
        "limits": {
            "daily_messages": 50, "daily_images": 0,
            "max_history": 20, "max_file_size_mb": 5, "max_conversations": 10,
        },
        "billing_period": "free", "current_period_end": None,
        "cancel_at_period_end": False, "is_premium": False,
    }

def check_usage_limit(user_id: int) -> Tuple[bool, int, int]:
    if not user_id:
        return True, 999, 999
    sub         = get_user_subscription(user_id)
    daily_limit = sub.get("limits", {}).get("daily_messages", 50)
    if daily_limit == -1:
        return True, -1, -1
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute(
                "SELECT messages_sent FROM usage_tracking WHERE user_id=%s AND usage_date=CURRENT_DATE",
                (user_id,)
            )
            row       = cur.fetchone()
            current   = row[0] if row else 0
            remaining = max(0, daily_limit - current)
            return current < daily_limit, remaining, daily_limit
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[USAGE CHECK ERROR] {e}")
        return True, daily_limit, daily_limit

def increment_usage(user_id: int, mode: str = "assistant"):
    if not user_id:
        return
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO usage_tracking (user_id, usage_date, messages_sent, modes_used)
                VALUES (%s, CURRENT_DATE, 1, %s::jsonb)
                ON CONFLICT (user_id, usage_date)
                DO UPDATE SET
                    messages_sent = usage_tracking.messages_sent + 1,
                    modes_used    = usage_tracking.modes_used || %s::jsonb,
                    updated_at    = NOW()
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
    role: str = Field(..., max_length=20)
    content: str = Field(..., max_length=50000)

class ChatRequest(BaseModel):
    # ── Güvenlik: max_length ekli ───────────────────────────
    prompt: str = Field(..., max_length=10000)
    mode: str = Field(default="assistant", max_length=30)
    conversation_id: Optional[str] = Field(None, max_length=36)
    history: Optional[List[ChatMessage]] = None
    context: Optional[str] = Field(None, max_length=50000)
    image_data: Optional[str] = None
    image_type: Optional[str] = Field(None, max_length=50)
    session_summary: Optional[str] = Field(None, max_length=5000)

    @validator("conversation_id")
    def validate_conversation_id(cls, v):
        if v is None:
            return v
        try:
            uuid.UUID(str(v))
            return str(v)
        except ValueError:
            raise ValueError("Invalid conversation_id format")

class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., max_length=2000)
    mode: str = Field(default="assistant", max_length=30)
    conversation_id: Optional[str] = Field(None, max_length=36)
    width: Optional[int] = Field(default=1024, ge=256, le=2048)
    height: Optional[int] = Field(default=1024, ge=256, le=2048)
    num_images: Optional[int] = Field(default=1, ge=1, le=4)

class ImageAnalysisRequest(BaseModel):
    image_data: str
    query: Optional[str] = Field(None, max_length=5000)
    mode: str = Field(default="assistant", max_length=30)
    conversation_id: Optional[str] = Field(None, max_length=36)

class OTPRequest(BaseModel):
    email: EmailStr
    mode: str = Field(..., max_length=20)

class OTPVerify(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6)
    mode: str = Field(..., max_length=20)
    name: Optional[str] = Field(None, max_length=100)

class ConversationCreate(BaseModel):
    title: Optional[str] = Field(default="Yeni Sohbet", max_length=200)

class ConversationUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    is_pinned: Optional[bool] = None
    is_archived: Optional[bool] = None

class FeedbackRequest(BaseModel):
    conversation_id: Optional[str] = Field(None, max_length=36)
    user_query: str = Field(default="", max_length=5000)
    assistant_response: str = Field(default="", max_length=20000)
    rating: int = Field(..., description="1 = like, -1 = dislike")
    comment: Optional[str] = Field(None, max_length=2000)

# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "="*60)
    print("🚀 SKYLIGHT API GATEWAY v3.1 - INTELLIGENT ROUTING")
    print("="*60)
    try:
        pool = _get_pool()
        conn = pool.getconn()
        conn.cursor().execute("SELECT 1")
        pool.putconn(conn)
        print("✅ PostgreSQL OK")
    except Exception as e:
        print(f"❌ PostgreSQL FAILED: {e}")
    print(f"✅ Chat:            {CHAT_SERVICE_URL}")
    print(f"✅ Image Gen:       {IMAGE_GEN_SERVICE_URL}")
    print(f"✅ Image Analysis:  {IMAGE_ANALYSIS_SERVICE_URL}")
    print(f"✅ GitHub Analysis: {GITHUB_ANALYSIS_URL}")
    print(f"✅ Smart Tools:     {SMART_TOOLS_URL}")
    print("="*60 + "\n")
    yield
    print("\n🛑 Gateway shutting down...")
    global _db_pool
    if _db_pool and not _db_pool.closed:
        _db_pool.closeall()
    with _file_store_lock:
        _file_store.clear()
    print("👋 Goodbye!\n")

app = FastAPI(
    title="Skylight API Gateway",
    description="v3.1 — Intelligent Context Routing",
    version="3.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://one-bune.com", "https://www.one-bune.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ── Security Headers Middleware ─────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]         = "DENY"
        response.headers["X-XSS-Protection"]        = "1; mode=block"
        response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# ═══════════════════════════════════════════════════════════════
# HELPER: CALL MICROSERVICE
# ═══════════════════════════════════════════════════════════════

async def call_service(service_url: str, endpoint: str, data: dict,
                       stream: bool = False, timeout: int = 120):
    url = f"{service_url}{endpoint}"
    if stream:
        async def stream_generator():
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=data) as response:
                    async for chunk in response.aiter_text():
                        yield chunk
        return stream_generator()
    else:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=data)
            return response.json()

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INTELLIGENT CONTEXT ROUTER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

def get_conversation_context(conversation_id: str) -> dict:
    """
    Son 20 mesajı okuyarak konuşmanın bağlamını çıkarır.

    FIX v3.1: last_mode artık gerçekten en son modu yansıtıyor.
    Önceki versiyonda "assistant" dışındaki ilk modu bulunca duruyordu,
    bu yüzden mod geçişleri yanlış algılanıyordu.
    """
    ctx = {
        "last_mode":        "assistant",
        "had_code":         False,
        "had_image_upload": False,
        "had_image_gen":    False,
        "last_code_snippet": None,
        "last_image_data":  None,
        "last_gen_prompt":  None,
        "last_gen_id":      None,
        "message_count":    0,
        "github_url":       None,  # Son analiz edilen GitHub URL
    }
    if not conversation_id:
        return ctx

    safe_id = _validate_uuid(conversation_id)
    if not safe_id:
        return ctx

    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                SELECT role, content, mode, has_image, metadata
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (safe_id,))
            rows = cur.fetchall()
            ctx["message_count"] = len(rows)

            # ── FIX: last_mode en son mesajdan alınmalı ──────────
            # rows DESC sıralı, ilk row en yeni mesaj
            last_mode_found = False
            for role, content, mode, has_image, metadata in rows:
                # İlk geçerli modu al (= en son mesajın modu)
                if not last_mode_found and mode:
                    ctx["last_mode"] = mode
                    last_mode_found  = True

                # Kod bloğu var mı?
                if not ctx["had_code"] and content:
                    if "```" in content or re.search(
                        r'\bdef \b|\bfunction\b|\bclass \b|\bimport \b|\bconst \b|\bvar \b',
                        content
                    ):
                        ctx["had_code"] = True
                        code_match = re.search(r'```(?:\w+)?\n([\s\S]*?)```', content)
                        if code_match and not ctx["last_code_snippet"]:
                            ctx["last_code_snippet"] = code_match.group(1)[:3000]

                # Kullanıcı görsel yüklediyse
                if role == "user" and has_image and not ctx["had_image_upload"]:
                    ctx["had_image_upload"] = True
                    if metadata and not ctx["last_image_data"]:
                        meta = (
                            metadata if isinstance(metadata, dict)
                            else json.loads(metadata) if isinstance(metadata, str)
                            else {}
                        )
                        if meta.get("image_data"):
                            ctx["last_image_data"] = meta["image_data"]

                # Görsel üretilmişse
                if role == "assistant" and content and not ctx["had_image_gen"]:
                    if "[IMAGE_B64]" in content or "Görsel Başarıyla Oluşturuldu" in content:
                        ctx["had_image_gen"] = True

                # GitHub URL geçtiyse
                if not ctx["github_url"] and content:
                    gh_match = re.search(r'github\.com/[\w\-\.]+/[\w\-\.]+', content)
                    if gh_match:
                        ctx["github_url"] = gh_match.group(0)

            # En son üretilen görsel promptu
            cur.execute("""
                SELECT id, user_prompt, generated_prompt
                FROM generated_images
                WHERE conversation_id = %s::uuid AND is_deleted = FALSE
                ORDER BY created_at DESC LIMIT 1
            """, (safe_id,))
            gen_row = cur.fetchone()
            if gen_row:
                ctx["last_gen_id"]     = str(gen_row[0])
                ctx["last_gen_prompt"] = gen_row[2] or gen_row[1]

        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[CTX READER ERROR] {e}")
    return ctx


def determine_routing_intent(
    prompt: str,
    has_image: bool,
    mode: str,
    conv_ctx: dict,
) -> RoutingIntent:
    """
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    KARAR AĞACI v3.1

    ÖNCE: GitHub URL veya diyagram mı?
    1. Görsel VARSA:
       a) Önceki görsel üretilmişse  → IMAGE_MODIFY
       b) Kod bağlamı varsa          → CODE_DEBUG
       c) Diğer                      → IMAGE_ANALYSIS
    2. Görsel YOKSA:
       a) GitHub URL var             → GITHUB_ANALYSIS
       b) Diyagram isteği            → DIAGRAM_GEN
       c) Görsel oluşturma           → IMAGE_GEN
       d) Görsel değiştirme          → IMAGE_MODIFY
       e) Kod devam                  → CODE_CONTINUE
       f) Diğer                      → CHAT
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    prompt_lower = (prompt or "").lower().strip()

    # ── GitHub URL her zaman öncelikli ──────────────────────
    if _detect_github_request(prompt_lower, prompt):
        print(f"[ROUTER] GitHub URL → GITHUB_ANALYSIS")
        return RoutingIntent.GITHUB_ANALYSIS

    # ── GÖRSEL VAR ──────────────────────────────────────────
    if has_image:
        if conv_ctx.get("had_image_gen") and conv_ctx.get("last_gen_id"):
            print(f"[ROUTER] Image + prev gen → IMAGE_MODIFY")
            return RoutingIntent.IMAGE_MODIFY

        if mode == "code" or conv_ctx.get("had_code") or conv_ctx.get("last_mode") == "code":
            debug_signals = [
                "hata", "error", "bug", "çalışmıyor", "düzelt", "neden",
                "wrong", "fix", "debug", "fail", "exception", "output",
                "sonuç", "çıktı", "result", "nerede yanlış", "neden çıkmıyor",
                "neden çalışmıyor", "neden hata", "şu hata",
            ]
            if not prompt_lower or any(s in prompt_lower for s in debug_signals):
                print(f"[ROUTER] Image + code ctx → CODE_DEBUG")
                return RoutingIntent.CODE_DEBUG

        print(f"[ROUTER] Image → IMAGE_ANALYSIS")
        return RoutingIntent.IMAGE_ANALYSIS

    # ── GÖRSEL YOK ──────────────────────────────────────────

    # Diyagram isteği — görsel üretme dışlamak için image_gen'den önce
    if _detect_diagram_request(prompt_lower):
        print(f"[ROUTER] Text → DIAGRAM_GEN")
        return RoutingIntent.DIAGRAM_GEN

    # Görsel oluşturma isteği
    if _detect_image_gen(prompt_lower):
        print(f"[ROUTER] Text → IMAGE_GEN")
        return RoutingIntent.IMAGE_GEN

    # Görsel değiştirme (sadece daha önce görsel üretilmişse)
    if conv_ctx.get("had_image_gen") and _detect_image_modify(prompt_lower):
        print(f"[ROUTER] Text + prev gen → IMAGE_MODIFY")
        return RoutingIntent.IMAGE_MODIFY

    # Kod devam ettirme
    if conv_ctx.get("had_code") or conv_ctx.get("last_mode") == "code":
        continue_signals = [
            "devam et", "continue", "tamamla", "complete", "bitir",
            "kalan", "rest of", "finish", "eksik", "missing",
            "geri kalan", "devam", "sürdür",
        ]
        if any(s in prompt_lower for s in continue_signals):
            print(f"[ROUTER] Code continue → CODE_CONTINUE")
            return RoutingIntent.CODE_CONTINUE

    print(f"[ROUTER] Default → CHAT")
    return RoutingIntent.CHAT


def _detect_github_request(prompt_lower: str, prompt_original: str) -> bool:
    """GitHub repo analiz isteği mi?"""
    # Direkt URL eşleşmesi
    if re.search(r'github\.com/[\w\-\.]+/[\w\-\.]+', prompt_original):
        return True
    # Türkçe/İngilizce repo analiz ifadeleri
    repo_phrases = [
        "bu repoyu analiz", "bu repository", "repo analiz",
        "github reposu", "git reposu", "repoyu incele",
        "analyze this repo", "analyze the repo", "github link",
        "bu projeyi analiz", "kodu analiz et",
    ]
    return any(p in prompt_lower for p in repo_phrases)


def _detect_diagram_request(prompt_lower: str) -> bool:
    """Diyagram/akış şeması üretme isteği mi?"""
    # Önce kod bağlam dışlamalarını kontrol et
    # (bu diyagram için gerekli değil, diyagram isteklerinde "kod" kelimesi olabilir)
    diagram_keywords = [
        "diyagram", "diagram", "flowchart", "akış şeması", "akış diyagramı",
        "mimari çiz", "mimari diyagram", "şema çiz", "şema oluştur",
        "mermaid", "sequence diagram", "class diagram", "er diagram",
        "flow chart", "akış haritası", "görselleştir",
        "architecture diagram", "system diagram",
    ]
    return any(k in prompt_lower for k in diagram_keywords)


def _detect_image_gen(prompt_lower: str) -> bool:
    """
    Görsel oluşturma isteği mi?

    FIX v3.1: "kod" exclusion kaldırıldı çünkü çok agresifti.
    "kodun diyagramını çiz" gibi ifadeleri yanlış dışlıyordu.
    Bunun yerine daha spesifik kod bağlam ifadeleri kullanıyoruz.
    """
    # Soru/kod bağlamı dışlamaları — daha spesifik
    exclusions = [
        "nereye ekle", "nereye koy", "nasıl ekle", "where do i add",
        "nedir", "ne demek", "what is", "nasıl çalışır", "nasıl kullanılır",
        "fonksiyon nereye", "import nereye", "hangi dosya",
    ]
    if any(e in prompt_lower for e in exclusions):
        return False

    # Görsel anahtar kelimeleri
    image_nouns = [
        "görsel", "resim", "fotoğraf", "foto", "çizim", "illüstrasyon",
        "grafik", "poster", "banner", "logo", "ikon", "icon",
        "image", "picture", "photo", "drawing", "illustration",
        "artwork", "render", "painting", "wallpaper", "background",
        "thumbnail", "avatar", "kapak görseli",
    ]
    creation_verbs_pattern = r'\b(yap|oluştur|üret|çiz|tasarla|çek|yarat|create|generate|make|draw|design|produce|paint)\b'

    has_noun     = any(n in prompt_lower for n in image_nouns)
    has_verb     = bool(re.search(creation_verbs_pattern, prompt_lower))
    direct_match = bool(re.search(
        r'\b(bir .{1,40} (yap|oluştur|çiz)|create an?|generate an?|make an?|draw an?|paint an?)\b',
        prompt_lower
    ))

    return (has_noun and has_verb) or direct_match


def _detect_image_modify(prompt_lower: str) -> bool:
    """Görsel değiştirme isteği mi? (Yalnızca önceden görsel üretilmişse çağrılır)"""
    exclusions = [
        "nereye ekle", "nereye koy", "kod taraf", "dosya", "fonksiyon",
        "import", "nerede", "where do i", "nasıl ekle", "how to add",
        "nedir", "ne demek",
    ]
    if any(e in prompt_lower for e in exclusions):
        return False

    strong = [
        "daha yeşil", "daha mavi", "daha büyük", "daha küçük",
        "daha parlak", "daha koyu", "daha açık", "daha canlı",
        "rengini değiştir", "arka planı değiştir", "arka planı kaldır",
        "make it more", "make it less", "change the color",
        "change the background", "remove the background",
        "add a person", "add trees", "blur the background",
        "daha profesyonel", "daha modern", "daha minimalist",
    ]
    if any(s in prompt_lower for s in strong):
        return True

    weak_verbs = [r'\bkaldır\b', r'\bsil\b', r'\bdeğiştir\b', r'\bayarla\b',
                  r'\bremove\b', r'\bdelete\b', r'\bchange\b', r'\bmodify\b']
    ref_words  = [r'\bbunu\b', r'\bşunu\b', r'\bonu\b', r'\bit\b', r'\bthis\b']

    has_verb = any(re.search(v, prompt_lower) for v in weak_verbs)
    has_ref  = any(re.search(r, prompt_lower) for r in ref_words)

    if has_verb and has_ref and len(prompt_lower.split()) <= 8:
        return True

    return False

# ═══════════════════════════════════════════════════════════════
# CODE MODE PROMPT ENRICHMENT — Kod Canavarı Modu
# ═══════════════════════════════════════════════════════════════

CODE_MONSTER_INJECTION = """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL CODE OUTPUT RULES — NEVER VIOLATE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Write COMPLETE, fully functional code. NEVER truncate.
2. NEVER use "...", "# rest of code", "// continue", "# TODO: implement"
3. NEVER write placeholder comments — implement everything fully
4. If code needs 500+ lines: write ALL 500+ lines without stopping
5. When modifying: return the ENTIRE updated file, not just changed parts
6. Every function must have a real, complete implementation
7. Always include: all imports, full class definitions, every method body
8. Add proper error handling, type hints, and docstrings
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

def _enrich_code_prompt(prompt: str, conv_ctx: dict, intent: RoutingIntent) -> str:
    """
    Code modu için prompt'u zenginleştir.
    - CODE_CONTINUE: son kod snippet'ini ekle
    - Tüm code istekleri: CODE_MONSTER_INJECTION ekle
    """
    enriched = prompt

    # Devam isteğinde son kodu ekle
    if intent == RoutingIntent.CODE_CONTINUE and conv_ctx.get("last_code_snippet"):
        enriched = (
            f"{enriched}\n\n"
            f"[Continue from this exact code — pick up exactly where it stopped:]\n"
            f"```\n{conv_ctx['last_code_snippet']}\n```"
        )

    # Kod canavarı injection'ı ekle
    enriched = enriched + CODE_MONSTER_INJECTION

    return enriched

# ═══════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════

def _auto_create_conversation(user_id: int, title: str) -> Optional[str]:
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            t = title[:100] + ("..." if len(title) > 100 else "")
            cur.execute("""
                INSERT INTO conversations (user_id, title, created_at, updated_at)
                VALUES (%s, %s, NOW(), NOW()) RETURNING id
            """, (user_id, t))
            conv_id = str(cur.fetchone()[0])
            conn.commit()
            print(f"[DB] Auto-created conversation {conv_id}")
            return conv_id
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[DB] Auto-create conv error: {e}")
        return None


def _save_messages(conversation_id: str, user_msg: str, assistant_msg: str,
                   mode: str, has_image: bool = False, image_metadata: dict = None):
    safe_id = _validate_uuid(conversation_id)
    if not safe_id:
        return
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            meta_json = json.dumps(image_metadata) if image_metadata else None
            cur.execute("""
                INSERT INTO messages (conversation_id, role, content, mode, has_image, metadata, created_at)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, NOW())
            """, (safe_id, "user", user_msg, mode, has_image, meta_json))
            cur.execute("""
                INSERT INTO messages (conversation_id, role, content, mode, created_at)
                VALUES (%s::uuid, %s, %s, %s, NOW())
            """, (safe_id, "assistant", assistant_msg, mode))
            cur.execute("UPDATE conversations SET updated_at=NOW() WHERE id=%s::uuid", (safe_id,))
            conn.commit()
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[DB] Save messages error: {e}")


def _load_history(conversation_id: str, user_id: int, limit: int = 30) -> List[dict]:
    safe_id = _validate_uuid(conversation_id)
    if not safe_id:
        return []
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM conversations WHERE id=%s", (safe_id,))
            row = cur.fetchone()
            if not row or (user_id and row[0] != user_id):
                return []
            cur.execute("""
                SELECT role, content FROM messages
                WHERE conversation_id=%s ORDER BY created_at DESC LIMIT %s
            """, (safe_id, limit))
            rows = cur.fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[DB] Load history error: {e}")
        return []


def _save_generated_image(user_id: int, conversation_id: str,
                          user_prompt: str, gen_prompt: str,
                          image_b64: str, modification_of: str = None) -> Optional[str]:
    safe_id  = _validate_uuid(conversation_id)
    safe_mod = _validate_uuid(modification_of)
    if not safe_id:
        return None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO generated_images
                (user_id, conversation_id, prompt_turkish, prompt_english,
                 user_prompt, generated_prompt, modification_of, image_b64, created_at)
                VALUES (%s, %s::uuid, %s, %s, %s, %s, %s::uuid, %s, NOW())
                RETURNING id
            """, (user_id, safe_id, user_prompt, gen_prompt,
                  user_prompt, gen_prompt, safe_mod,
                  image_b64[:5000] if image_b64 else None))
            new_id = str(cur.fetchone()[0])
            conn.commit()
            return new_id
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[DB] Save gen image error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/request_code")
async def request_code_endpoint(req: OTPRequest, request: Request):
    try:
        ip = get_client_ip(request)
        abuse_post("/otp/request/check", {"email": req.email, "ip_address": ip})
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE email=%s", (req.email,))
            existing = cur.fetchone()
            if req.mode == "login" and not existing:
                raise HTTPException(status_code=404, detail="Kullanici bulunamadi.")
            if req.mode == "register" and existing:
                raise HTTPException(status_code=400, detail="E-posta kullanimda.")
            otp = str(random.randint(100000, 999999))
            cur.execute("""
                INSERT INTO otp_codes (email, code, expire_at, created_at)
                VALUES (%s, %s, NOW()+INTERVAL '5 minutes', NOW())
                ON CONFLICT (email) DO UPDATE
                SET code=EXCLUDED.code, expire_at=EXCLUDED.expire_at, created_at=NOW()
            """, (req.email, otp))
            conn.commit()
            msg = MIMEMultipart()
            msg["From"]    = SMTP_FROM
            msg["To"]      = req.email
            msg["Subject"] = "ONE-BUNE Doğrulama Kodu"
            msg.attach(MIMEText(
                f"Doğrulama kodun: {otp}\n\nBu kod 5 dakika geçerlidir.\n\nONE-BUNE AI",
                "plain", "utf-8"
            ))
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.ehlo(); server.starttls(); server.ehlo()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
            abuse_post("/otp/request/mark-sent", {"email": req.email, "ip_address": ip})
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
    email = body.email.lower().strip()
    code  = body.code.strip()
    mode  = body.mode.strip()
    name  = (body.name or "").strip() if mode == "register" else None
    ip    = get_client_ip(request)
    try:
        if email == TEST_BYPASS_EMAIL and code == TEST_BYPASS_CODE:
            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
            try:
                cur.execute("SELECT id, name FROM users WHERE email=%s", (email,))
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        "INSERT INTO users (email,name,google_id) VALUES (%s,%s,%s) RETURNING id,name",
                        (email, TEST_BYPASS_NAME, f"local_{email}")
                    )
                    row = cur.fetchone(); conn.commit()
                token = jwt.encode(
                    {"sub": email, "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE_DAYS)},
                    JWT_SECRET, algorithm=JWT_ALGORITHM
                )
                return {"status": "success", "token": token,
                        "user": {"id": row[0], "name": str(row[1]), "email": email}}
            finally:
                pool.putconn(conn)

        abuse_post("/otp/verify/check", {"email": email, "ip_address": ip})
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT code, expire_at FROM otp_codes WHERE email=%s LIMIT 1", (email,))
            row = cur.fetchone()
            if not row:
                abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip})
                raise HTTPException(status_code=400, detail="Kod bulunamadi.")
            stored_code, expire_at = row
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if expire_at is None or now_utc > expire_at:
                abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip})
                raise HTTPException(status_code=400, detail="Kod suresi dolmus.")
            if code != stored_code:
                abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip})
                raise HTTPException(status_code=400, detail="Hatali kod.")
            abuse_post("/otp/verify/clear", {"email": email, "ip_address": ip})
            if mode == "register":
                cur.execute("SELECT id FROM users WHERE email=%s", (email,))
                if cur.fetchone():
                    raise HTTPException(status_code=400, detail="Bu email zaten kayitli.")
                cur.execute(
                    "INSERT INTO users (email,name,google_id) VALUES (%s,%s,%s) RETURNING id,name",
                    (email, name or email.split("@")[0], f"local_{email}")
                )
                user_row = cur.fetchone()
            elif mode == "login":
                cur.execute("SELECT id, name FROM users WHERE email=%s", (email,))
                user_row = cur.fetchone()
                if not user_row:
                    raise HTTPException(status_code=400, detail="Kullanici bulunamadi.")
            else:
                raise HTTPException(status_code=400, detail="Gecersiz mode.")
            cur.execute("DELETE FROM otp_codes WHERE email=%s", (email,))
            conn.commit()
            token = jwt.encode(
                {"sub": email, "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE_DAYS)},
                JWT_SECRET, algorithm=JWT_ALGORITHM
            )
            return {"status": "success", "token": token,
                    "user": {"id": user_row[0], "name": str(user_row[1]), "email": email}}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[VERIFY OTP ERROR] {e}")
        raise HTTPException(status_code=500, detail="Dogrulama hatasi.")


@app.get("/check_user")
async def check_user(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="No authorization header.")
    try:
        user_id = get_user_from_token(authorization)
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token.")
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT email, name FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found.")
            return {"status": "success", "user": {"id": user_id, "email": row[0], "name": row[1]}}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Check user error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# FILE UPLOAD
# ═══════════════════════════════════════════════════════════════

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), authorization: Optional[str] = Header(None)):
    user_id = None
    if authorization:
        try:
            user_id = get_user_from_token(authorization)
        except Exception:
            pass
    if not file.filename:
        raise HTTPException(status_code=400, detail="Dosya adı boş.")
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Desteklenmeyen dosya türü: {ext}")
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"Dosya çok büyük. Max: {MAX_FILE_SIZE//1024//1024}MB")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Dosya boş.")
    is_clean, scan_result = scan_file_for_malware(file.filename, content)
    if not is_clean:
        if scan_result in ("SCAN_ERROR", "SCAN_FAILED"):
            raise HTTPException(status_code=503, detail="Güvenlik taraması başarısız.")
        raise HTTPException(status_code=400, detail=f"Güvenlik taramasında reddedildi: {scan_result}")
    extracted_text, file_type = extract_text_from_file(file.filename, content)
    if not extracted_text or extracted_text.startswith("["):
        raise HTTPException(status_code=422,
                            detail=extracted_text.strip("[]") if extracted_text else "Metin çıkarılamadı.")
    file_id = str(uuid.uuid4())[:12]
    _store_file_text(file_id, file.filename, extracted_text, user_id=user_id)
    preview = extracted_text[:500] + ("..." if len(extracted_text) > 500 else "")
    return {
        "status": "success", "file_id": file_id, "filename": file.filename,
        "file_type": file_type, "char_count": len(extracted_text),
        "line_count": extracted_text.count("\n") + 1,
        "preview": preview, "size_bytes": len(content),
    }

# ═══════════════════════════════════════════════════════════════
# CONVERSATION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/conversations/create")
async def create_conversation(data: ConversationCreate, authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO conversations (user_id,title) VALUES (%s,%s) RETURNING id,title,created_at",
                (user_id, data.title)
            )
            result = cur.fetchone(); conn.commit()
            return {"status": "success", "id": str(result[0]),
                    "title": result[1], "created_at": result[2].isoformat()}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create conversation: {str(e)}")


@app.get("/conversations/list")
async def list_conversations(limit: int = 50, authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                SELECT c.id, c.title, c.created_at, c.updated_at, c.is_pinned, COUNT(m.id)
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user_id=%s AND c.is_archived=FALSE
                GROUP BY c.id ORDER BY c.is_pinned DESC, c.updated_at DESC LIMIT %s
            """, (user_id, min(limit, 100)))
            return {"conversations": [
                {"id": str(r[0]), "title": r[1], "created_at": r[2].isoformat(),
                 "updated_at": r[3].isoformat(), "is_pinned": r[4], "message_count": r[5]}
                for r in cur.fetchall()
            ]}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list conversations: {str(e)}")


@app.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str, authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    safe_id = _validate_uuid(conversation_id)
    if not safe_id:
        raise HTTPException(status_code=400, detail="Invalid conversation_id")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM conversations WHERE id=%s", (safe_id,))
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if result[0] != user_id:
                raise HTTPException(status_code=403, detail="Not authorized")
            cur.execute(
                "SELECT id,role,content,created_at,is_edited FROM messages WHERE conversation_id=%s ORDER BY created_at ASC",
                (safe_id,)
            )
            return {"messages": [
                {"id": str(r[0]), "role": r[1], "content": r[2],
                 "created_at": r[3].isoformat(), "is_edited": r[4]}
                for r in cur.fetchall()
            ]}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get messages: {str(e)}")


@app.put("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, data: ConversationUpdate,
                               authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    safe_id = _validate_uuid(conversation_id)
    if not safe_id:
        raise HTTPException(status_code=400, detail="Invalid conversation_id")
    try:
        updates = []; params = []
        if data.title      is not None: updates.append("title=%s");       params.append(data.title)
        if data.is_pinned  is not None: updates.append("is_pinned=%s");   params.append(data.is_pinned)
        if data.is_archived is not None: updates.append("is_archived=%s"); params.append(data.is_archived)
        if not updates:
            return {"status": "success", "message": "No changes"}
        params.extend([safe_id, user_id])
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE conversations SET {', '.join(updates)} WHERE id=%s AND user_id=%s RETURNING id",
                params
            )
            result = cur.fetchone(); conn.commit()
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
async def delete_conversation(conversation_id: str, authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    safe_id = _validate_uuid(conversation_id)
    if not safe_id:
        raise HTTPException(status_code=400, detail="Invalid conversation_id")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("DELETE FROM conversations WHERE id=%s AND user_id=%s RETURNING id",
                        (safe_id, user_id))
            result = cur.fetchone(); conn.commit()
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
# PROFILE ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/profile")
async def get_profile(authorization: str = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT email, name FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found")
            cur.execute("SELECT topics, preferences, summary FROM user_profiles WHERE user_id=%s", (user_id,))
            p = cur.fetchone()
            return {
                "user": {"id": user_id, "email": row[0], "name": row[1]},
                "profile": {
                    "topics": p[0] if p else [],
                    "preferences": p[1] if p else {},
                    "summary": p[2] if p else "",
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
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("UPDATE user_profiles SET topics='[]' WHERE user_id=%s", (user_id,))
            conn.commit()
            return {"status": "success", "message": "Topics cleared"}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Clear topics error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# FEEDBACK
# ═══════════════════════════════════════════════════════════════

@app.post("/feedback")
async def submit_feedback(data: FeedbackRequest, authorization: str = Header(None)):
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    if data.rating not in (1, -1):
        raise HTTPException(status_code=400, detail="Rating must be 1 or -1")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO feedback (user_id,conversation_id,user_query,assistant_response,rating,comment)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """, (user_id, data.conversation_id or "", data.user_query,
                  data.assistant_response, data.rating, data.comment or ""))
            fid = cur.fetchone()[0]; conn.commit()
            return {"status": "success", "feedback_id": fid}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Feedback error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# SUBSCRIPTION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/code-mode/status")
async def code_mode_status():
    return {
        "enabled":    True,
        "model":      os.getenv("DEEPINFRA_CODE_MODEL", "Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo"),
        "max_tokens": int(os.getenv("DEEPINFRA_CODE_MAX_TOKENS", "16000")),
        "features":   ["complete_code", "no_truncation", "full_file_output", "error_handling"],
    }


@app.get("/subscription/status")
async def subscription_status_endpoint(authorization: str = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    sub = get_user_subscription(user_id)
    allowed, remaining, limit = check_usage_limit(user_id)
    return {
        "plan_id":              sub.get("plan_id", "free"),
        "plan_name":            sub.get("plan_name", "Ücretsiz"),
        "is_premium":           sub.get("is_premium", False),
        "status":               sub.get("status", "active"),
        "features":             sub.get("features", {}),
        "limits":               sub.get("limits", {}),
        "billing_period":       sub.get("billing_period", "free"),
        "current_period_end":   sub.get("current_period_end"),
        "cancel_at_period_end": sub.get("cancel_at_period_end", False),
        "usage_today": {
            "messages_sent":      (limit - remaining) if remaining >= 0 and limit >= 0 else 0,
            "messages_remaining": remaining,
            "daily_message_limit": limit,
        }
    }


@app.get("/subscription/plans")
async def subscription_plans_endpoint():
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                SELECT id,name,description,price_monthly,price_yearly,
                       currency,features,limits,is_active
                FROM subscription_plans WHERE is_active=TRUE ORDER BY sort_order
            """)
            return {"plans": [
                {"id": r[0], "name": r[1], "description": r[2],
                 "price_monthly": float(r[3]), "price_yearly": float(r[4]),
                 "currency": r[5], "features": r[6], "limits": r[7], "is_active": r[8]}
                for r in cur.fetchall()
            ]}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Get plans error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN CHAT ENDPOINT — INTELLIGENT ROUTER v3.1
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat_endpoint(
    request_body: ChatRequest,
    request: Request,
    authorization: str = Header(None),
):
    # ── 1. AUTH ─────────────────────────────────────────────
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass

    # ── 2. ABUSE CONTROL ────────────────────────────────────
    ip = get_client_ip(request)
    try:
        abuse_post("/chat/check", {"user_id": str(user_id or "guest"), "ip_address": ip})
    except HTTPException as e:
        if e.status_code == 429:
            def _rate_lim():
                yield "⏳ Çok hızlı istek gönderiyorsun. Lütfen birkaç saniye bekle."
            return StreamingResponse(_rate_lim(), media_type="text/plain; charset=utf-8")
        raise

    # ── 3. USAGE LIMIT ──────────────────────────────────────
    if user_id:
        allowed, remaining, limit = check_usage_limit(user_id)
        if not allowed:
            def _limit_exc():
                yield f"⏰ Günlük mesaj limitine ulaştın ({limit}/gün). Yarın tekrar deneyebilirsin! 🚀"
            return StreamingResponse(_limit_exc(), media_type="text/plain; charset=utf-8")

    # ── 4. CONVERSATION CONTEXT ─────────────────────────────
    conversation_id = _validate_uuid(request_body.conversation_id)
    conv_ctx = get_conversation_context(conversation_id) if conversation_id else {
        "last_mode": "assistant", "had_code": False,
        "had_image_upload": False, "had_image_gen": False,
        "last_code_snippet": None, "last_image_data": None,
        "last_gen_prompt": None, "last_gen_id": None,
        "message_count": 0, "github_url": None,
    }

    has_image = bool(request_body.image_data)
    prompt    = request_body.prompt or ""
    mode      = request_body.mode or conv_ctx.get("last_mode", "assistant")

    # ── 5. DETERMINE INTENT ─────────────────────────────────
    intent = determine_routing_intent(prompt, has_image, mode, conv_ctx)

    # ── 6. SUBSCRIPTION CHECK ───────────────────────────────
    if user_id and intent in (
        RoutingIntent.IMAGE_GEN, RoutingIntent.IMAGE_MODIFY,
        RoutingIntent.IMAGE_ANALYSIS, RoutingIntent.CODE_DEBUG,
        RoutingIntent.GITHUB_ANALYSIS,
    ):
        sub = get_user_subscription(user_id)
        if intent in (RoutingIntent.IMAGE_GEN, RoutingIntent.IMAGE_MODIFY):
            if not sub.get("features", {}).get("image_gen", False):
                def _prem():
                    yield "🔒 Görsel oluşturma/değiştirme özelliği Premium abonelikte mevcut.\n"
                    yield "Premium'a geçerek tüm özellikleri kullanabilirsin! ✨"
                return StreamingResponse(_prem(), media_type="text/plain; charset=utf-8")

    print(f"[GATEWAY v3.1] intent={intent.value} | mode={mode} | has_image={has_image} | conv={conversation_id}")

    # ── 7. ROUTE ────────────────────────────────────────────

    if intent == RoutingIntent.IMAGE_ANALYSIS:
        return await _handle_image_analysis(
            request_body, user_id, conversation_id, conv_ctx, authorization, ip, mode
        )

    if intent == RoutingIntent.CODE_DEBUG:
        return await _handle_code_debug(
            request_body, user_id, conversation_id, conv_ctx, authorization, ip
        )

    if intent == RoutingIntent.IMAGE_GEN:
        return await _handle_image_gen(
            request_body, user_id, conversation_id, conv_ctx, ip, mode, modification_of=None
        )

    if intent == RoutingIntent.IMAGE_MODIFY:
        return await _handle_image_modify(
            request_body, user_id, conversation_id, conv_ctx, ip, mode
        )

    if intent == RoutingIntent.GITHUB_ANALYSIS:
        return await _handle_github_analysis(
            request_body, user_id, conversation_id, conv_ctx, authorization, ip, mode
        )

    if intent == RoutingIntent.DIAGRAM_GEN:
        return await _handle_diagram_gen(
            request_body, user_id, conversation_id, conv_ctx, ip, mode
        )

    # Default: CHAT (kod devam da buraya girer, _handle_chat içinde ayrılır)
    return await _handle_chat(
        request_body, user_id, conversation_id, conv_ctx, ip, mode, intent
    )


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: IMAGE ANALYSIS ─────────────────────────────────
# ═══════════════════════════════════════════════════════════════

async def _handle_image_analysis(request_body, user_id, conversation_id,
                                  conv_ctx, authorization, ip, mode):
    print(f"[HANDLER] IMAGE_ANALYSIS")

    if not conversation_id and user_id:
        title = request_body.prompt[:50] if request_body.prompt else "Görsel Analizi"
        conversation_id = _auto_create_conversation(user_id, title)

    analysis_prompt = request_body.prompt or "Bu görseli detaylıca analiz et."

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{IMAGE_ANALYSIS_SERVICE_URL}/analyze",
                json={
                    "image_data":      request_body.image_data,
                    "prompt":          analysis_prompt,
                    "conversation_id": conversation_id,
                    "user_id":         str(user_id) if user_id else None,
                },
                headers={"Authorization": authorization} if authorization else {},
                timeout=90.0,
            )

            if response.status_code != 200:
                raise Exception(f"Analysis service error: {response.status_code}")

            # Kullanıcı mesajını kaydet
            if conversation_id and user_id:
                try:
                    pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                    try:
                        cur.execute("""
                            INSERT INTO messages
                            (conversation_id,role,content,mode,has_image,metadata,created_at)
                            VALUES (%s::uuid,%s,%s,%s,%s,%s,NOW())
                        """, (conversation_id, "user", analysis_prompt, mode, True,
                              json.dumps({"image_uploaded": True, "image_data": request_body.image_data})))
                        conn.commit()
                    finally:
                        pool.putconn(conn)
                except Exception as e:
                    print(f"[IMG ANAL] Save user msg error: {e}")

            collected = []

            async def _stream_and_save():
                async for chunk in response.aiter_text():
                    collected.append(chunk)
                    yield chunk
                full_response = "".join(collected)
                if conversation_id and user_id and full_response:
                    try:
                        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                        try:
                            cur.execute("""
                                INSERT INTO messages
                                (conversation_id,role,content,mode,created_at)
                                VALUES (%s::uuid,%s,%s,%s,NOW())
                            """, (conversation_id, "assistant", full_response, "vision"))
                            cur.execute(
                                "UPDATE conversations SET updated_at=NOW() WHERE id=%s::uuid",
                                (conversation_id,)
                            )
                            conn.commit()
                        finally:
                            pool.putconn(conn)
                    except Exception as e:
                        print(f"[IMG ANAL] Save assistant msg error: {e}")
                # ── FIX: increment STREAM SONRASI ───────────────
                if user_id:
                    increment_usage(user_id, mode)

            return StreamingResponse(_stream_and_save(), media_type="text/plain; charset=utf-8")

    except Exception as e:
        print(f"[IMAGE ANALYSIS ERROR] {e}")
        return await _handle_chat_with_image_fallback(
            request_body, user_id, conversation_id, conv_ctx, ip, mode
        )


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: CODE DEBUG ─────────────────────────────────────
# Ekran görüntüsü + kod bağlamı → debug & fix
# ═══════════════════════════════════════════════════════════════

async def _handle_code_debug(request_body, user_id, conversation_id,
                              conv_ctx, authorization, ip):
    print(f"[HANDLER] CODE_DEBUG")

    last_code   = conv_ctx.get("last_code_snippet", "")
    base_prompt = request_body.prompt or ""

    if not base_prompt:
        base_prompt = "Bu ekran görüntüsündeki kodu analiz et, hataları bul ve düzeltilmiş tam kodu yaz."

    if last_code:
        enriched_prompt = f"""Ekran görüntüsünü analiz et ve hataları bul.

Son paylaşılan kod:
```
{last_code}
```

{base_prompt}

Lütfen:
1. Görüntüdeki hatayı/sonucu analiz et
2. Neden hata olduğunu açıkla (root cause)
3. Düzeltilmiş TAM kodu yaz — hiçbir satır atlanmayacak, hiçbir placeholder kullanılmayacak
{CODE_MONSTER_INJECTION}"""
    else:
        enriched_prompt = f"""{base_prompt}

Bu ekran görüntüsündeki kodu analiz et:
1. Kodun ne yaptığını anla
2. Hata veya iyileştirme noktalarını belirle
3. Düzeltilmiş/geliştirilmiş TAM kodu yaz — hiçbir kısım atlanmayacak
{CODE_MONSTER_INJECTION}"""

    if not conversation_id and user_id:
        conversation_id = _auto_create_conversation(user_id, "Kod Debug")

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{IMAGE_ANALYSIS_SERVICE_URL}/analyze",
                json={
                    "image_data":      request_body.image_data,
                    "prompt":          enriched_prompt,
                    "mode":            "code",
                    "conversation_id": conversation_id,
                    "user_id":         str(user_id) if user_id else None,
                },
                headers={"Authorization": authorization} if authorization else {},
                timeout=180.0,
            )

            if response.status_code != 200:
                raise Exception(f"Analysis service error: {response.status_code}")

            if conversation_id and user_id:
                try:
                    pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                    try:
                        cur.execute("""
                            INSERT INTO messages
                            (conversation_id,role,content,mode,has_image,metadata,created_at)
                            VALUES (%s::uuid,%s,%s,%s,%s,%s,NOW())
                        """, (conversation_id, "user",
                              request_body.prompt or "Kod debug ekran görüntüsü",
                              "code", True,
                              json.dumps({"image_uploaded": True, "debug_mode": True,
                                          "image_data": request_body.image_data})))
                        conn.commit()
                    finally:
                        pool.putconn(conn)
                except Exception as e:
                    print(f"[CODE DEBUG] Save user msg error: {e}")

            collected = []

            async def _stream_debug():
                async for chunk in response.aiter_text():
                    collected.append(chunk)
                    yield chunk
                full_response = "".join(collected)
                if conversation_id and user_id and full_response:
                    try:
                        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                        try:
                            cur.execute("""
                                INSERT INTO messages
                                (conversation_id,role,content,mode,created_at)
                                VALUES (%s::uuid,%s,%s,%s,NOW())
                            """, (conversation_id, "assistant", full_response, "code"))
                            cur.execute(
                                "UPDATE conversations SET updated_at=NOW() WHERE id=%s::uuid",
                                (conversation_id,)
                            )
                            conn.commit()
                        finally:
                            pool.putconn(conn)
                    except Exception as e:
                        print(f"[CODE DEBUG] Save assistant msg error: {e}")
                if user_id:
                    increment_usage(user_id, "code")

            return StreamingResponse(_stream_debug(), media_type="text/plain; charset=utf-8")

    except Exception as e:
        print(f"[CODE DEBUG ERROR] {e}")
        request_body.prompt = enriched_prompt
        return await _handle_chat(request_body, user_id, conversation_id,
                                   conv_ctx, ip, "code", RoutingIntent.CHAT)


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: IMAGE GEN ──────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

async def _handle_image_gen(request_body, user_id, conversation_id,
                             conv_ctx, ip, mode, modification_of=None):
    print(f"[HANDLER] IMAGE_GEN (modification_of={modification_of})")

    if not conversation_id and user_id:
        conversation_id = _auto_create_conversation(user_id, request_body.prompt)

    history            = _load_history(conversation_id, user_id, limit=10) if conversation_id else []
    user_facing_prompt = request_body.prompt

    async def _stream_gen():
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    f"{IMAGE_GEN_SERVICE_URL}/generate",
                    json={
                        "prompt":               request_body.prompt,
                        "user_id":              str(user_id or 0),
                        "conversation_id":      conversation_id,
                        "conversation_history": history,
                        "size":                 "1024x1024",
                        "save_to_db":           True,
                        "modification_of":      modification_of,
                        "original_user_prompt": user_facing_prompt,
                    },
                    timeout=180.0,
                )

                if response.status_code != 200:
                    yield f"⚠️ Görsel servisi hatası (HTTP {response.status_code})\n"
                    return

                data = response.json()

                if not data.get("success"):
                    yield f"⚠️ {data.get('error', 'Görsel oluşturulamadı')}\n"
                    return

                yield "✨ **Görsel Başarıyla Oluşturuldu!**\n\n"

                image_b64      = data.get("image_b64")
                gen_prompt_out = data.get("generated_prompt", request_body.prompt)
                db_image_id    = data.get("db_image_id")

                if image_b64:
                    yield f"[IMAGE_B64]{image_b64}[/IMAGE_B64]"

                if conversation_id and user_id:
                    try:
                        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                        try:
                            cur.execute("""
                                INSERT INTO messages (conversation_id,role,content,mode,created_at)
                                VALUES (%s,%s,%s,%s,NOW())
                            """, (conversation_id, "user", user_facing_prompt, mode))
                            cur.execute("""
                                INSERT INTO messages (conversation_id,role,content,mode,created_at)
                                VALUES (%s,%s,%s,%s,NOW())
                            """, (conversation_id, "assistant",
                                  f"[Görsel oluşturuldu - ID: {db_image_id}]", mode))
                            cur.execute(
                                "UPDATE conversations SET updated_at=NOW() WHERE id=%s",
                                (conversation_id,)
                            )
                            conn.commit()
                        finally:
                            pool.putconn(conn)
                    except Exception as e:
                        print(f"[IMG GEN] DB save error: {e}")

                    _save_generated_image(
                        user_id, conversation_id,
                        user_facing_prompt, gen_prompt_out,
                        image_b64, modification_of
                    )

                if user_id:
                    increment_usage(user_id, mode)

        except Exception as e:
            print(f"[IMG GEN ERROR] {e}")
            yield f"⚠️ Görsel oluşturma hatası: {str(e)}\n"

    resp = StreamingResponse(_stream_gen(), media_type="text/plain; charset=utf-8")
    if conversation_id:
        resp.headers["X-Conversation-ID"] = conversation_id
    return resp


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: IMAGE MODIFY ───────────────────────────────────
# ═══════════════════════════════════════════════════════════════

async def _handle_image_modify(request_body, user_id, conversation_id, conv_ctx, ip, mode):
    print(f"[HANDLER] IMAGE_MODIFY")

    last_gen_prompt = conv_ctx.get("last_gen_prompt", "")
    last_gen_id     = conv_ctx.get("last_gen_id")

    if last_gen_prompt:
        combined_prompt = (
            f"{last_gen_prompt}\n\n"
            f"MODIFICATION REQUEST: {request_body.prompt}\n\n"
            f"Apply the modification while keeping the rest of the image intact."
        )
        print(f"[IMG MODIFY] Combining: '{last_gen_prompt[:50]}...' + '{request_body.prompt}'")
        request_body.prompt = combined_prompt
    else:
        print(f"[IMG MODIFY] No previous gen found, treating as new gen")

    return await _handle_image_gen(
        request_body, user_id, conversation_id,
        conv_ctx, ip, mode,
        modification_of=last_gen_id
    )


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: GITHUB ANALYSIS (YENİ) ─────────────────────────
# ═══════════════════════════════════════════════════════════════

async def _handle_github_analysis(request_body, user_id, conversation_id,
                                   conv_ctx, authorization, ip, mode):
    print(f"[HANDLER] GITHUB_ANALYSIS")

    # GitHub URL'ini prompt'tan çıkar
    url_match = re.search(r'(https?://)?github\.com/[\w\-\.]+/[\w\-\.]+', request_body.prompt)
    github_url = url_match.group(0) if url_match else None

    if not github_url:
        # URL bulunamadıysa normal chat'e düşür
        return await _handle_chat(request_body, user_id, conversation_id,
                                   conv_ctx, ip, mode, RoutingIntent.CHAT)

    if not github_url.startswith("http"):
        github_url = "https://" + github_url

    if not conversation_id and user_id:
        repo_name = github_url.split("github.com/")[-1]
        conversation_id = _auto_create_conversation(user_id, f"GitHub: {repo_name}")

    # Kullanıcı mesajını kaydet
    if conversation_id and user_id:
        try:
            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO messages
                    (conversation_id,role,content,mode,created_at)
                    VALUES (%s::uuid,%s,%s,%s,NOW())
                """, (conversation_id, "user", request_body.prompt, mode))
                conn.commit()
            finally:
                pool.putconn(conn)
        except Exception as e:
            print(f"[GITHUB ANAL] Save user msg error: {e}")

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{GITHUB_ANALYSIS_URL}/analyze",
                json={
                    "github_url":      github_url,
                    "user_prompt":     request_body.prompt,
                    "user_id":         str(user_id) if user_id else None,
                    "conversation_id": conversation_id,
                    "mode":            mode,
                },
                headers={"Authorization": authorization} if authorization else {},
                timeout=180.0,
            )

            if response.status_code != 200:
                raise Exception(f"GitHub analysis service error: {response.status_code}")

            collected = []

            async def _stream_github():
                async for chunk in response.aiter_text():
                    collected.append(chunk)
                    yield chunk
                full_response = "".join(collected)
                if conversation_id and user_id and full_response:
                    try:
                        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                        try:
                            cur.execute("""
                                INSERT INTO messages
                                (conversation_id,role,content,mode,created_at)
                                VALUES (%s::uuid,%s,%s,%s,NOW())
                            """, (conversation_id, "assistant", full_response, mode))
                            cur.execute(
                                "UPDATE conversations SET updated_at=NOW() WHERE id=%s::uuid",
                                (conversation_id,)
                            )
                            conn.commit()
                        finally:
                            pool.putconn(conn)
                    except Exception as e:
                        print(f"[GITHUB ANAL] Save assistant msg error: {e}")
                if user_id:
                    increment_usage(user_id, mode)

            resp = StreamingResponse(_stream_github(), media_type="text/plain; charset=utf-8")
            if conversation_id:
                resp.headers["X-Conversation-ID"] = conversation_id
            return resp

    except Exception as e:
        print(f"[GITHUB ANALYSIS ERROR] {e}")
        # Fallback: normal chat ile yanıtla
        fallback_prompt = (
            f"Kullanıcı şu GitHub reposunu analiz etmemi istedi: {github_url}\n\n"
            f"Orijinal istek: {request_body.prompt}\n\n"
            f"GitHub analiz servisi şu an erişilemiyor. "
            f"Repo URL'sine bakarak genel bilgi verebilirsin."
        )
        request_body.prompt = fallback_prompt
        return await _handle_chat(request_body, user_id, conversation_id,
                                   conv_ctx, ip, mode, RoutingIntent.CHAT)


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: DIAGRAM GEN (YENİ) ─────────────────────────────
# Mermaid diyagramı üretme — chat service ile, özel prompt
# ═══════════════════════════════════════════════════════════════

async def _handle_diagram_gen(request_body, user_id, conversation_id, conv_ctx, ip, mode):
    print(f"[HANDLER] DIAGRAM_GEN")

    # Önceki kodu bağlama ekle
    last_code = conv_ctx.get("last_code_snippet", "")

    diagram_prompt = request_body.prompt

    if last_code:
        diagram_prompt = f"""{request_body.prompt}

Mevcut kod:
```
{last_code}
```
"""

    diagram_prompt += """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DİYAGRAM ÜRETME TALİMATLARI:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mermaid syntax kullanarak diyagram üret.
Yanıtın SADECE şu formatta olsun:

```mermaid
[buraya mermaid kodu]
```

Ardından kısa bir açıklama ekle (5-10 cümle).

Desteklenen diyagram türleri:
- flowchart TD (akış diyagramı, default)
- sequenceDiagram (API akışları için)
- classDiagram (OOP için)
- erDiagram (veritabanı şeması için)
- gitGraph (git flow için)

Mermaid sözdizimi kuralları:
- Türkçe karakterlerde tırnak kullan: A["Türkçe"]
- Ok işaretleri: --> veya --- veya ==>
- Düğüm şekilleri: [] kare, () yuvarlak, {} elmas
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    # Chat service'e özel diagram modu ile gönder
    modified_request           = ChatRequest(
        prompt          = diagram_prompt,
        mode            = mode,
        conversation_id = conversation_id,
        history         = request_body.history,
        context         = request_body.context,
        session_summary = request_body.session_summary,
    )

    return await _handle_chat(
        modified_request, user_id, conversation_id,
        conv_ctx, ip, mode, RoutingIntent.DIAGRAM_GEN
    )


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: NORMAL CHAT ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

async def _handle_chat(request_body, user_id, conversation_id,
                        conv_ctx, ip, mode, intent):
    print(f"[HANDLER] CHAT (intent={intent.value}, mode={mode})")

    auto_created = False
    if not conversation_id and user_id:
        conversation_id = _auto_create_conversation(user_id, request_body.prompt or "Yeni Sohbet")
        auto_created    = True

    history = _load_history(conversation_id, user_id, limit=30) if conversation_id else (
        [{"role": m.role, "content": m.content} for m in (request_body.history or [])]
    )

    prompt = request_body.prompt

    # ── KOD CANAVARΙ MOD ────────────────────────────────────
    # Code modu veya code intent ise prompt'u zenginleştir
    if mode == "code" or intent in (RoutingIntent.CODE_CONTINUE, RoutingIntent.DIAGRAM_GEN):
        prompt = _enrich_code_prompt(prompt, conv_ctx, intent)

    chat_data = {
        "prompt":          prompt,
        "mode":            mode,
        "user_id":         user_id or 0,
        "conversation_id": conversation_id,
        "history":         history,
        "context":         request_body.context,
        "session_summary": request_body.session_summary,
    }

    if request_body.image_data:
        chat_data["image_data"] = request_body.image_data
        chat_data["image_type"] = request_body.image_type or "image/jpeg"

    collected_response = []

    async def _stream_chat():
        try:
            generator = await call_service(
                CHAT_SERVICE_URL, "/chat",
                data=chat_data, stream=True, timeout=300
            )
            async for chunk in generator:
                collected_response.append(chunk)
                yield chunk

            # ── FIX: Save + increment STREAM SONRASI ─────────
            assistant_response = "".join(collected_response)
            if conversation_id and user_id:
                _save_messages(
                    conversation_id,
                    request_body.prompt or "",
                    assistant_response,
                    mode,
                    has_image=bool(request_body.image_data),
                    image_metadata=(
                        {"image_data": request_body.image_data}
                        if request_body.image_data else None
                    ),
                )

            # ── FIX: increment STREAM SONRASI (önceden önce çağrılıyordu) ──
            if user_id:
                increment_usage(user_id, mode)

        except Exception as e:
            print(f"[CHAT SERVICE ERROR] {e}")
            yield f"\n⚠️ Chat service error: {str(e)}"

    resp = StreamingResponse(_stream_chat(), media_type="text/plain; charset=utf-8")
    if conversation_id:
        resp.headers["X-Conversation-ID"] = conversation_id
        if auto_created:
            resp.headers["X-Conversation-Created"] = "true"
    return resp


# ═══════════════════════════════════════════════════════════════
# ── HANDLER: CHAT WITH IMAGE FALLBACK ───────────────────────
# ═══════════════════════════════════════════════════════════════

async def _handle_chat_with_image_fallback(request_body, user_id, conversation_id,
                                            conv_ctx, ip, mode):
    print(f"[HANDLER] CHAT_IMAGE_FALLBACK")
    return await _handle_chat(request_body, user_id, conversation_id,
                               conv_ctx, ip, mode, RoutingIntent.CHAT)


# ═══════════════════════════════════════════════════════════════
# DIRECT ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/generate-image")
async def generate_image_endpoint(
    request_body: ImageGenerationRequest,
    request: Request,
    authorization: str = Header(None),
):
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    sub = get_user_subscription(user_id)
    if not sub.get("features", {}).get("image_gen", False):
        raise HTTPException(status_code=403, detail="Image generation requires Premium subscription")

    chat_req = ChatRequest(
        prompt=request_body.prompt,
        mode=request_body.mode,
        conversation_id=request_body.conversation_id,
    )
    conv_ctx = get_conversation_context(request_body.conversation_id) if request_body.conversation_id else {}
    ip = get_client_ip(request)
    return await _handle_image_gen(
        chat_req, user_id, request_body.conversation_id,
        conv_ctx, ip, request_body.mode
    )


@app.post("/analyze-image")
async def analyze_image_endpoint(
    request_body: ImageAnalysisRequest,
    request: Request,
    authorization: str = Header(None),
):
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    sub = get_user_subscription(user_id)
    if not sub.get("features", {}).get("vision", False):
        raise HTTPException(status_code=403, detail="Image analysis requires Premium subscription")

    chat_req = ChatRequest(
        prompt=request_body.query or "Bu görseli analiz et.",
        mode=request_body.mode,
        conversation_id=request_body.conversation_id,
        image_data=request_body.image_data,
    )
    conv_ctx = get_conversation_context(request_body.conversation_id) if request_body.conversation_id else {}
    ip = get_client_ip(request)
    return await _handle_image_analysis(
        chat_req, user_id, request_body.conversation_id,
        conv_ctx, authorization, ip, request_body.mode
    )


# ═══════════════════════════════════════════════════════════════
# HEALTH & ROOT
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    return {
        "status":    "healthy",
        "version":   "3.1.0",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "routing":   "intelligent-context-aware",
        "intents":   [i.value for i in RoutingIntent],
        "services": {
            "database":         "connected",
            "chat_service":     bool(CHAT_SERVICE_URL),
            "image_gen":        bool(IMAGE_GEN_SERVICE_URL),
            "image_analysis":   bool(IMAGE_ANALYSIS_SERVICE_URL),
            "github_analysis":  bool(GITHUB_ANALYSIS_URL),
            "smart_tools":      bool(SMART_TOOLS_URL),
            "abuse_control":    "configured" if ABUSE_CONTROL_URL else "not_configured",
            "clamav":           "enabled" if CLAMAV_ENABLED else "disabled",
            "smtp":             "configured" if SMTP_USER else "not_configured",
        },
    }


@app.get("/")
async def root():
    return {
        "service": "Skylight API Gateway",
        "version": "3.1.0",
        "status":  "running",
        "routing_intents": [i.value for i in RoutingIntent],
        "features": {
            "intelligent_routing":       True,
            "code_debug_from_screenshot": True,
            "image_modify":              True,
            "github_analysis":           True,
            "diagram_gen":               True,
            "code_monster_mode":         True,
            "context_aware":             True,
            "security_headers":          True,
            "uuid_validation":           True,
            "abuse_control":             bool(ABUSE_CONTROL_URL),
            "virus_scanning":            CLAMAV_ENABLED,
            "file_upload":               True,
            "otp_auth":                  bool(SMTP_USER),
            "subscriptions":             True,
            "quota_management":          True,
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8443)