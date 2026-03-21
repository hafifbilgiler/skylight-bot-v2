"""
═══════════════════════════════════════════════════════════════
SKYLIGHT API GATEWAY - VERSION 2.3 (DEEP SEARCH + SMART ROUTING)
═══════════════════════════════════════════════════════════════
v2.2 üzerine eklenenler:

DEEP SEARCH ENTEGRASYONu (YENİ):
  ✅ detect_needs_web_search() — güncel bilgi gerekiyor mu?
  ✅ call_deep_search() — smart tools /deep_search çağrısı
  ✅ Proaktif öneri — "Bu konuyu webden araştırayım mı? 🔍"
  ✅ Otomatik mod — güncel konu tespitinde context otomatik eklenir
  ✅ /web_search endpoint — frontend için doğrudan deep search

ROUTING İYİLEŞTİRMELERİ (v2.2'den):
  ✅ detect_image_generation_request — false positive tamamen çözüldü
  ✅ detect_image_modification_request — ekleyeyim/ekleyeceğim düzeltildi
  ✅ Konuşma bağlamı okuma — son mod, kod snippet, görsel geçmişi
  ✅ Kod modu: CODE_MONSTER_INJECTION — devam et + tam kod garantisi
  ✅ Görsel takip soruları — önceki görsel otomatik restore
  ✅ increment_usage stream SONRASI çağrılıyor
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

CHAT_SERVICE_URL = os.getenv("CHAT_SERVICE_URL", "http://skylight-chat:8082")
RAG_SERVICE_URL = os.getenv("RAG_SERVICE_URL", "http://skylight-rag:8084")
IMAGE_GEN_SERVICE_URL = os.getenv("IMAGE_GEN_SERVICE_URL", "http://skylight-image-gen:8083")
IMAGE_ANALYSIS_SERVICE_URL = os.getenv("IMAGE_ANALYSIS_SERVICE_URL", "http://skylight-image-analysis:8002")
SMART_TOOLS_URL = os.getenv("SMART_TOOLS_URL", "http://skylight-smart-tools:8081")
ABUSE_CONTROL_URL = os.getenv("ABUSE_CONTROL_URL", "http://skylight-bot-abuse-control:8010")

JWT_SECRET = os.getenv("JWT_SECRET", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
API_TOKEN = os.getenv("API_TOKEN", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
TOKEN_EXPIRE_DAYS = int(os.getenv("TOKEN_EXPIRE_DAYS", "7"))

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.hostinger.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "noreply@one-bune.com")

CLAMAV_ENABLED = os.getenv("CLAMAV_ENABLED", "true").lower() == "true"
CLAMAV_HOST = os.getenv("CLAMAV_HOST", "skylight-bot-antivirus")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT", "3310"))

TEST_BYPASS_EMAIL = "test@one-bune.com"
TEST_BYPASS_CODE  = "138113"
TEST_BYPASS_NAME  = "Test User"

MAX_FILE_SIZE       = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 30000
MAX_FILES_PER_USER  = 3
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
# KOD CANAVARI INJECTION
# Code modunda her prompta eklenir — model asla truncate etmez
# ═══════════════════════════════════════════════════════════════

CODE_MONSTER_INJECTION = """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KRİTİK KOD ÇIKTI KURALLARI — ASLA İHLAL ETME:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• EKSIKSIZ ve TAM çalışan kod yaz. ASLA truncate etme.
• ASLA "...", "# burası devam ediyor", "// continue" kullanma
• ASLA placeholder yorum kullanma — her şeyi gerçekten implemente et
• 500+ satır gerekiyorsa: tüm 500+ satırı dur durak olmadan yaz
• Mevcut kodu değiştirirken: tüm güncellenmiş dosyayı döndür
• Her fonksiyonun gerçek, tam implementasyonu olmalı
• Tüm import'lar en üstte, tüm class tanımları, her method gövdesi
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

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
                    minconn=2, maxconn=15,
                    host=DB_HOST, database=DB_NAME,
                    user=DB_USER, password=DB_PASSWORD, port=DB_PORT,
                )
                print("[DB POOL] Initialized (2-15 connections)")
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
    print(f"[CLAMAV] Scan start: file={filename}, enabled={CLAMAV_ENABLED}")
    if not CLAMAV_ENABLED:
        return True, "CLAMAV_DISABLED"
    try:
        cd = clamd.ClamdNetworkSocket(host=CLAMAV_HOST, port=CLAMAV_PORT, timeout=10)
        pong = cd.ping()
        print(f"[CLAMAV] Ping OK: {pong}")
        result = cd.instream(io.BytesIO(content))
        print(f"[CLAMAV] Raw result: {result}")
        stream_result = result.get("stream")
        if not stream_result:
            return False, "SCAN_FAILED"
        status, signature = stream_result
        if status == "OK":
            return True, "OK"
        if status == "FOUND":
            return False, signature or "MALWARE_FOUND"
        return False, f"UNKNOWN_SCAN_RESULT:{status}"
    except Exception as e:
        print(f"[CLAMAV ERROR] {filename}: {e}")
        return False, "SCAN_ERROR"

def extract_text_from_file(filename: str, content: bytes) -> Tuple[str, str]:
    ext = os.path.splitext(filename.lower())[1]
    if ext in (".txt", ".md", ".py", ".js", ".yaml", ".json", ".csv",
               ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs",
               ".php", ".rb", ".sql", ".sh", ".toml", ".ini", ".xml", ".html", ".css"):
        try:
            text = content.decode("utf-8", errors="replace")
            return text[:MAX_EXTRACTED_CHARS], ext[1:]
        except Exception as e:
            return f"[Text read error: {str(e)[:100]}]", "text_error"
    return f"[Unsupported file type: {ext}]", "unsupported"

def _store_file_text(file_id: str, filename: str, text: str, user_id=None):
    import time as _time
    with _file_store_lock:
        _file_store[file_id] = {
            "text": text, "filename": filename,
            "char_count": len(text), "user_id": user_id,
            "expire_time": _time.time() + FILE_TTL_SECONDS,
        }
    print(f"[FILE STORE] Stored '{filename}' as {file_id} (user={user_id}, {len(text)} chars)")

def _get_file_text(file_id: str) -> Optional[str]:
    import time as _time
    with _file_store_lock:
        entry = _file_store.get(file_id)
        if entry and _time.time() < entry.get("expire_time", 0):
            return entry["text"]
    return None

# ═══════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════

def get_user_from_token(authorization: Optional[str] = None) -> Optional[int]:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    try:
        token = authorization.replace("Bearer ", "")
        if token == API_TOKEN:
            return None
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
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
# SUBSCRIPTION & QUOTA
# ═══════════════════════════════════════════════════════════════

def get_user_subscription(user_id: int) -> dict:
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
                ORDER BY sp.sort_order DESC LIMIT 1
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                return _default_free_plan()
            plan_id, status, billing_period, period_end, cancel_at_end, plan_name, features, limits = row
            if period_end and period_end < datetime.datetime.now(datetime.timezone.utc):
                cur.execute("UPDATE user_subscriptions SET status='expired' WHERE user_id=%s AND status='active' AND plan_id!='free'", (user_id,))
                cur.execute("INSERT INTO user_subscriptions (user_id,plan_id,status,billing_period) VALUES (%s,'free','active','free') ON CONFLICT DO NOTHING", (user_id,))
                conn.commit()
                return _default_free_plan()
            return {
                "plan_id": plan_id, "plan_name": plan_name, "status": status,
                "features": features or {}, "limits": limits or {},
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
    return {
        "plan_id": "free", "plan_name": "Ücretsiz", "status": "active",
        "features": {
            "allowed_modes": ["assistant"], "image_gen": False,
            "vision": False, "web_search": True, "file_upload": True,
            "rag": True, "smart_tools": True,
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
    sub = get_user_subscription(user_id)
    daily_limit = sub.get("limits", {}).get("daily_messages", 50)
    if daily_limit == -1:
        return True, -1, -1
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT messages_sent FROM usage_tracking WHERE user_id=%s AND usage_date=CURRENT_DATE", (user_id,))
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
    prompt: str
    mode: str = "assistant"
    conversation_id: Optional[str] = None
    history: Optional[List[ChatMessage]] = None
    context: Optional[str] = None
    image_data: Optional[str] = None
    session_summary: Optional[str] = None

class ImageGenerationRequest(BaseModel):
    prompt: str
    mode: str = "assistant"
    conversation_id: Optional[str] = None
    width: Optional[int] = 1024
    height: Optional[int] = 1024
    num_images: Optional[int] = 1

class ImageAnalysisRequest(BaseModel):
    image_data: str
    query: Optional[str] = None
    mode: str = "assistant"
    conversation_id: Optional[str] = None

class OTPRequest(BaseModel):
    email: EmailStr
    mode: str

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
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "="*60)
    print("🚀 SKYLIGHT API GATEWAY v2.2 - ADVANCED SMART ROUTING")
    print("="*60)
    try:
        pool = _get_pool()
        conn = pool.getconn()
        conn.cursor().execute("SELECT 1")
        pool.putconn(conn)
        print("✅ PostgreSQL connection OK")
    except Exception as e:
        print(f"❌ PostgreSQL connection FAILED: {e}")
    if CLAMAV_ENABLED:
        print(f"✅ ClamAV enabled ({CLAMAV_HOST}:{CLAMAV_PORT})")
    else:
        print("⚠️  ClamAV disabled")
    print(f"✅ Chat Service: {CHAT_SERVICE_URL}")
    print(f"✅ Image Gen Service: {IMAGE_GEN_SERVICE_URL}")
    print(f"✅ Image Analysis Service: {IMAGE_ANALYSIS_SERVICE_URL}")
    print(f"✅ RAG Service: {RAG_SERVICE_URL}")
    print(f"✅ Smart Tools: {SMART_TOOLS_URL}")
    if ABUSE_CONTROL_URL:
        print(f"✅ Abuse Control: {ABUSE_CONTROL_URL}")
    print("="*60)
    print("✨ GATEWAY READY - ALL SYSTEMS OPERATIONAL")
    print("="*60 + "\n")
    yield
    print("\n🛑 Gateway shutting down...")
    global _db_pool
    if _db_pool and not _db_pool.closed:
        _db_pool.closeall()
        print("✅ Database pool closed")
    with _file_store_lock:
        _file_store.clear()
    print("👋 Goodbye!\n")

app = FastAPI(
    title="Skylight API Gateway",
    description="Complete Gateway v2.2 - Advanced Smart Routing",
    version="2.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════

async def call_service(service_url: str, endpoint: str, data: dict,
                       stream: bool = False, timeout: int = 30):
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
# ADVANCED SMART ROUTING — KALP ATIŞI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

def detect_image_generation_request(prompt: str) -> bool:
    """
    Görsel OLUŞTURMA isteği mi?

    ═══ SORUN (v2.1) ═══════════════════════════════════════════
    "bir fonksiyon yap"   → yanlış tetikleniyordu
    "bir API oluştur"     → yanlış tetikleniyordu
    "bir script yaz"      → yanlış tetikleniyordu

    ═══ ÇÖZÜM (v2.2) ═══════════════════════════════════════════
    1. HARD EXCLUSION: Yazılım kelimeleri varsa asla görsel değildir
    2. Görsel isimleri sadece gerçek medya isimleri
    3. "yap"/"create"/"make" SADECE görsel ismiyle birlikte geçerli
    4. "oluştur"/"generate"/"draw" güçlü fiiller, görsel ismi şart

    ═══ DOĞRU ÖRNEKLER ═════════════════════════════════════════
    "bir resim yap"          → ✅ TRUE   (resim=görsel ismi, yap=fiil)
    "logo oluştur"           → ✅ TRUE   (logo=görsel ismi, oluştur=güçlü fiil)
    "manzara çiz"            → ✅ TRUE   (manzara=konu, çiz=güçlü fiil)
    "bir fonksiyon yap"      → ❌ FALSE  (fonksiyon=yazılım kelimesi → dışlandı)
    "API oluştur"            → ❌ FALSE  (api=yazılım kelimesi → dışlandı)
    "nasıl resim yapılır"    → ❌ FALSE  (soru kalıbı → dışlandı)
    "görsel nereye ekleyeyim"→ ❌ FALSE  (nereye+ekle → dışlandı)
    """
    prompt_lower = prompt.lower()

    # ── 1. SORU KALIPLARı — asla görsel üretim değildir ────────
    question_patterns = [
        'neler yapılır', 'ne yapılır', 'nasıl yapılır', 'ne yapabilirim',
        'nerede yapılır', 'kim yapar', 'ne zaman yapılır', 'nasıl çalışır',
        'what to do', 'how to', 'where to', 'when to', 'who does',
        'what is', 'nedir', 'ne demek', 'nasıl kullanılır',
    ]
    if any(q in prompt_lower for q in question_patterns):
        return False

    # ── 2. HARD EXCLUSION — yazılım kelimeleri varsa asla görsel ─
    software_exclusions = (
        # Yazılım yapıları
        "fonksiyon", "function", "method", "metod",
        "class", "sınıf", "import", "module", "modül",
        "script", "kod yazı", "code", "api", "endpoint",
        "servis", "service", "component", "bileşen",
        "veritabanı", "database", "query", "sql",
        "algoritma", "algorithm", "loop", "döngü",
        "değişken", "variable", "array", "liste", "dict", "object",
        "paket", "package", "library", "kütüphane",
        "test", "debug", "hata", "error", "fix", "düzelt",
        "dosya", "file", "klasör", "folder",
        "proje", "project", "uygulama", "app",
        "deployment", "kubernetes", "docker",
        "middleware", "handler", "router", "gateway",
        "endpoint", "webhook", "socket",
        # Konum/yerleştirme soruları
        "nereye ekle", "nereye koy", "nereye yaz",
        "where do i", "how do i add",
    )
    if any(e in prompt_lower for e in software_exclusions):
        return False

    # ── 3. GERÇEK GÖRSEL İSİMLERİ ───────────────────────────────
    image_nouns = (
        # Türkçe — net görsel medya isimleri
        "görsel", "resim", "fotoğraf", "foto",
        "çizim", "illüstrasyon",
        "poster", "banner", "logo", "ikon",
        "kapak görseli", "thumbnail", "avatar",
        "wallpaper", "duvar kağıdı", "arka plan görseli",
        "grafik tasarım", "infografik",
        # İngilizce — net görsel medya isimleri
        "image", "picture", "photo", "photograph",
        "drawing", "illustration", "painting",
        "artwork", "render", "rendering",
        "icon", "wallpaper", "background image",
        "graphic", "infographic",
    )
    has_noun = any(n in prompt_lower for n in image_nouns)

    # ── 4. ÜRETİM FİİLLERİ ──────────────────────────────────────
    # Güçlü fiiller: görsel ismi olmadan bile bazı durumlarda geçerli
    strong_verbs = r'\b(oluştur|üret|çiz|tasarla|yarat|generate|draw|design|paint|render)\b'
    has_strong_verb = bool(re.search(strong_verbs, prompt_lower))

    # Zayıf fiiller: MUTLAKA görsel ismi lazım
    weak_verbs = r'\b(yap|create|make|produce|ver|göster|show)\b'
    has_weak_verb = bool(re.search(weak_verbs, prompt_lower))

    # Güçlü fiil + görsel ismi → kesinlikle görsel
    if has_noun and has_strong_verb:
        return True

    # Zayıf fiil + görsel ismi → görsel
    if has_noun and has_weak_verb:
        return True

    # ── 5. DOĞRUDAN ÜRETIM İFADELERİ ───────────────────────────
    # "bir X çiz/oluştur" kalıbı — güçlü fiille
    direct_with_strong = re.search(
        r'\b(bir|an?)\s+.{0,50}\s+(oluştur|çiz|tasarla|yarat|generate|draw|design|paint|render)\b',
        prompt_lower
    )
    if direct_with_strong:
        return True

    # İngilizce kalıplar — güçlü fiille
    english_direct = re.search(
        r'\b(create|generate|draw|design|paint|render)\s+(a|an|the|me)\s+\w',
        prompt_lower
    )
    if english_direct:
        return True

    # ── 6. KONU + GÜÇLÜ FİİL (görsel ismi olmasa bile) ──────────
    # "manzara çiz", "gün batımı oluştur", "şehir tasarla"
    # Görsel ismi yoksa ama güçlü fiil + konu varsa
    if has_strong_verb and not has_noun:
        # Konuyu çıkar — fiilden önce gelen 1-3 kelime
        topic_before_verb = re.search(
            r'(\w+(?:\s+\w+){0,2})\s+(oluştur|çiz|tasarla|yarat|generate|draw|design|paint|render)\b',
            prompt_lower
        )
        if topic_before_verb:
            topic = topic_before_verb.group(1)
            # Bu konu yazılım kelimesi değilse görsel isteği
            soft_exclusions = (
                "kod", "fonksiyon", "api", "script", "test", "uygulama",
                "bu", "şu", "o", "it", "this", "the", "ne", "nasıl",
            )
            if not any(e in topic for e in soft_exclusions):
                return True

    return False


def detect_image_modification_request(prompt: str) -> bool:
    """
    Mevcut görseli DEĞİŞTİRME isteği mi?
    Sadece daha önce görsel üretilmişse çağrılır.

    ═══ SORUN (v2.1) ═══════════════════════════════════════════
    "ekleyeyim" → 'ekle' substring → yanlış tetikleniyordu
    "ekleyeceğim" → 'ekle' substring → yanlış tetikleniyordu

    ═══ ÇÖZÜM (v2.2) ═══════════════════════════════════════════
    'ekle' listeden tamamen çıkarıldı.
    Zayıf fiiller sadece WORD BOUNDARY ile eşleşiyor.

    ═══ DOĞRU ÖRNEKLER ═════════════════════════════════════════
    "daha yeşil yap"         → ✅ TRUE   (güçlü görsel sinyali)
    "ağaçları kaldır"        → ✅ TRUE   (kaldır=kelime sınırı)
    "bunu daha büyük yap"    → ✅ TRUE   (güçlü + referans)
    "bunu nereye ekleyeyim"  → ❌ FALSE  (nereye dışlaması)
    "bunu kod tarafına ekle" → ❌ FALSE  (kod taraf dışlaması)
    "bu nedir"               → ❌ FALSE  (nedir dışlaması)
    """
    prompt_lower = prompt.lower()

    # ── 1. KESİN DIŞLAMALAR ─────────────────────────────────────
    exclusion_phrases = (
        # Konum/yerleştirme soruları
        'nereye ekle', 'nereye ekley', 'nereye koy', 'nereye yaz',
        'nereye yapıştır', 'nereye yerleştir', 'nereye',
        'where do i', 'where should', 'how do i add', 'how to add',
        # Kod bağlamı
        'kod taraf', 'kod kısm', 'dosya', 'fonksiyon', 'metod', 'class',
        'import', 'modül', 'paket', 'satır', 'dizin', 'klasör',
        # Görsel yükleme bildirimleri
        'görsel yükledim', 'resim yükledim', 'dosya yükledim',
        'uploaded', 'i uploaded', 'sent you',
        # Soru / açıklama
        'nedir', 'ne demek', 'what is', 'explain',
        'bilgi ver', 'anlat', 'tell me about',
        'sordum', 'soruyu', 'question about',
    )
    if any(excl in prompt_lower for excl in exclusion_phrases):
        return False

    # ── 2. GÜÇLÜ GÖRSEL DEĞİŞTİRME SİNYALLERİ ──────────────────
    # Bu ifadeler varsa kesinlikle görsel düzenlemedir
    strong_visual = (
        # Renk / boyut değiştirme
        'daha yeşil', 'daha mavi', 'daha kırmızı', 'daha sarı', 'daha turuncu',
        'daha mor', 'daha pembe', 'daha siyah', 'daha beyaz',
        'daha büyük', 'daha küçük', 'daha geniş', 'daha dar',
        'daha parlak', 'daha koyu', 'daha açık', 'daha canlı', 'daha soluk',
        'daha profesyonel', 'daha modern', 'daha minimalist', 'daha şık',
        # Renk/arka plan işlemleri
        'rengini değiştir', 'rengi değiştir', 'renklerini değiştir',
        'arka planı değiştir', 'arka planı kaldır', 'arka planı sil',
        'arka plan ekle', 'arka planı beyaz yap', 'transparan yap',
        # İngilizce güçlü komutlar
        'make it more', 'make it less', 'make it bigger', 'make it smaller',
        'make it brighter', 'make it darker', 'make it lighter',
        'make it more colorful', 'make it more realistic',
        'change the color', 'change the background', 'change the style',
        'remove the background', 'add a background',
        'delete the background', 'blur the background',
        'add a person', 'add a tree', 'add a sky', 'add a building',
        'remove the person', 'remove the tree',
        # Stil değişimi
        'anime tarzı', 'anime style', 'cartoon style', 'realistic style',
        'oil painting', 'watercolor', 'sketch style',
    )
    if any(phrase in prompt_lower for phrase in strong_visual):
        return True

    # ── 3. ZAYIF SİNYALLER — word boundary + referans ───────────
    # 'ekle' artık burada YOK — false positive'i önlemek için
    weak_verbs = [
        r'\bkaldır\b', r'\bsil\b', r'\bdeğiştir\b', r'\bayarla\b',
        r'\byenile\b', r'\bdüzenle\b', r'\bgeliştir\b',
        r'\bremove\b', r'\bdelete\b', r'\bchange\b', r'\badjust\b',
        r'\bmodify\b', r'\bupdate\b', r'\bedit\b', r'\bimprove\b',
    ]
    reference_words = [
        r'\bbunu\b', r'\bşunu\b', r'\bonu\b', r'\bbundaki\b',
        r'\bbu görseli\b', r'\bbu resmi\b',
        r'\bit\b', r'\bthis\b', r'\bthe image\b', r'\bthe picture\b',
    ]

    has_verb = any(re.search(v, prompt_lower) for v in weak_verbs)
    has_ref  = any(re.search(r, prompt_lower) for r in reference_words)

    # Kısa prompt + referans + düzenleme fiili → görsel düzenleme
    if has_verb and has_ref and len(prompt.split()) <= 10:
        return True

    return False


def get_last_image_generation_prompt(user_id: int, conversation_id: str) -> tuple:
    """Son üretilen görselin prompt bilgisini al."""
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT id, user_prompt, generated_prompt
                FROM generated_images
                WHERE user_id = %s AND conversation_id = %s::uuid AND is_deleted = FALSE
                ORDER BY created_at DESC LIMIT 1
            """, (user_id, conversation_id))
            row = cur.fetchone()
            if row:
                return (str(row[0]), row[1], row[2])
            return (None, None, None)
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[GET LAST IMAGE PROMPT ERROR] {e}")
        return (None, None, None)


def get_last_image_from_conversation(conversation_id: str) -> Optional[str]:
    """Konuşmada son yüklenen görsel verisini al (takip soruları için)."""
    if not conversation_id:
        return None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT metadata FROM messages
                WHERE conversation_id = %s::uuid
                  AND has_image = TRUE AND role = 'user'
                ORDER BY created_at DESC LIMIT 1
            """, (conversation_id,))
            row = cur.fetchone()
            if row and row[0]:
                metadata = row[0]
                if isinstance(metadata, dict):
                    return metadata.get("image_data")
                elif isinstance(metadata, str):
                    meta_dict = json.loads(metadata)
                    return meta_dict.get("image_data")
            return None
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[GET LAST IMAGE ERROR] {e}")
        return None


def build_image_analysis_context_prompt(
    user_prompt: str,
    conversation_id: Optional[str],
    conv_ctx: dict,
) -> str:
    """
    Görsel analiz için zenginleştirilmiş prompt oluştur.

    Kullanıcı metin yazmadan görsel attıysa, konuşma geçmişinden
    otomatik bağlam çıkar ve prompt'a ekle.

    Örnek:
      Önceki konu: kedi cinsleri → yeni görsel geldi, metin yok
      → "Bu görseli analiz et. Önceki konuşma bağlamı: kedi cinsleri üzerine konuşuyorduk."
    """
    base_prompt = user_prompt.strip() if user_prompt and user_prompt.strip() else ""

    # Önceki konuşmadan bağlam çıkar
    context_hint = ""
    if conversation_id and not base_prompt:
        try:
            pool = _get_pool()
            conn = pool.getconn()
            cur  = conn.cursor()
            try:
                # Son 6 mesajın içeriğini al (özet için yeterli)
                cur.execute("""
                    SELECT role, content FROM messages
                    WHERE conversation_id = %s
                    ORDER BY created_at DESC LIMIT 6
                """, (conversation_id,))
                recent = cur.fetchall()
                if recent:
                    # En son assistant mesajını bağlam olarak kullan
                    for role, content in recent:
                        if role == "assistant" and content and len(content) > 20:
                            # İlk 300 karakteri al
                            snippet = content[:300].replace("\n", " ").strip()
                            context_hint = (
                                f"\n\n[CONVERSATION CONTEXT]\n"
                                f"Previous discussion: {snippet}\n"
                                f"[/CONVERSATION CONTEXT]\n\n"
                                f"Analyze this new image in the context of our ongoing conversation."
                            )
                            break
            finally:
                pool.putconn(conn)
        except Exception as e:
            print(f"[BUILD ANALYSIS PROMPT ERROR] {e}")

    if base_prompt:
        return base_prompt + context_hint
    elif context_hint:
        return "Analyze this image." + context_hint
    else:
        return "Bu görseli detaylıca analiz et."


def get_conversation_context(conversation_id: str) -> dict:
    """
    Son 20 mesajı okuyarak konuşmanın bağlamını çıkarır.
    Döner: last_mode, had_code, last_code_snippet, had_image_gen, last_gen_prompt, last_gen_id
    """
    ctx = {
        "last_mode":         "assistant",
        "had_code":          False,
        "last_code_snippet": None,
        "had_image_gen":     False,
        "last_gen_prompt":   None,
        "last_gen_id":       None,
    }
    if not conversation_id:
        return ctx
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT role, content, mode, has_image
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC LIMIT 20
            """, (conversation_id,))
            rows = cur.fetchall()

            # ── FIX v2.2: rows DESC → ilk satır = en YENİ mesaj ──
            # last_mode'u ilk geçerli moddan al (= en yeni)
            last_mode_set = False
            for role, content, mode, has_image in rows:
                # En yeni mesajın modunu al
                if not last_mode_set and mode:
                    ctx["last_mode"] = mode
                    last_mode_set    = True

                # Kod bloğu var mı?
                if not ctx["had_code"] and content:
                    if "```" in content or re.search(
                        r'\bdef \b|\bfunction\b|\bclass \b|\bimport \b'
                        r'|\bconst \b|\bvar \b|\breturn\b',
                        content
                    ):
                        ctx["had_code"] = True
                        code_match = re.search(r'```(?:\w+)?\n([\s\S]*?)```', content)
                        if code_match and not ctx["last_code_snippet"]:
                            ctx["last_code_snippet"] = code_match.group(1)[:3000]

                # Görsel üretilmişse
                if role == "assistant" and content and not ctx["had_image_gen"]:
                    if "[IMAGE_B64]" in content or "Görsel Başarıyla Oluşturuldu" in content:
                        ctx["had_image_gen"] = True

            # En son üretilen görsel prompt'u
            cur.execute("""
                SELECT id, user_prompt, generated_prompt
                FROM generated_images
                WHERE conversation_id = %s::uuid AND is_deleted = FALSE
                ORDER BY created_at DESC LIMIT 1
            """, (conversation_id,))
            gen_row = cur.fetchone()
            if gen_row:
                ctx["last_gen_id"]     = str(gen_row[0])
                ctx["last_gen_prompt"] = gen_row[2] or gen_row[1]

        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[CTX READER ERROR] {e}")
    return ctx


def combine_prompts_for_modification(original_prompt: str, modification_request: str) -> str:
    return f"{original_prompt}\n\nMODIFICATION REQUEST: {modification_request}"


# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GATEWAY — DEEP SEARCH (Sadece araştırma sorgular için)
#
# MİMARİ:
#   Chat servisi → canlı veri (kur, hava, kripto, haberler, maç)
#                  Local keyword detection → smart_tools /unified
#
#   Gateway → deep search (araştırma, analiz, güncel olaylar)
#             Local keyword detection → smart_tools /deep_search
#
# KURAL: Her iki servis de detection'ı LOCAL yapar (0ms, network yok).
#        Network çağrısı sadece veri almak için yapılır.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

DEEP_SEARCH_TIMEOUT = int(os.getenv("DEEP_SEARCH_TIMEOUT", "35"))

# ── Chat servisi zaten hallediyor → gateway dokunmaz ────────────
# Bu sorgular için deep search tetiklenMEZ
_CHAT_HANDLES_THESE = (
    # Anlık kur/fiyat
    "dolar","euro","eur","usd","gbp","sterlin","kur","döviz","doviz",
    "bitcoin","btc","ethereum","eth","kripto","crypto","coin",
    "altın fiyatı","gram altın","petrol fiyatı",
    # Hava
    "hava durumu","havadurumu","hava nasıl","sıcaklık","derece","weather",
    # Haberler
    "son haberler","güncel haberler","son dakika","haber oku",
    # Saat
    "saat kaç","saati kaç","şimdi saat",
    # Maç/spor skorları
    "maç sonucu","maç skoru","skor kaç","gol attı","kazandı mı",
    "galatasaray maç","fenerbahçe maç","beşiktaş maç",
    "premier league","süper lig sonuç",
)

# ── Deep search tetikleyen sinyaller ────────────────────────────
_DEEP_SEARCH_SIGNALS = (
    # Açık araştırma isteği
    "araştır","araştırma yap","analiz et","incele",
    "webde ara","internette ara","bul bana","find me",
    # Güncel olaylar — haber değil araştırma
    "son gelişmeler","son açıklama","ne oldu","neler oluyor",
    "dünyada neler","dünya gündemi","gündemde ne var",
    "küresel gelişmeler","güncel durum","son durum",
    # Konu araştırması
    "hakkında bilgi ver","kimdir","ne yaptı","karar verdi mi",
    "latest developments","what happened","recent news about",
)


def _gateway_needs_deep_search(prompt: str, mode: str) -> bool:
    """
    LOCAL detection (0ms) — gateway deep search gerekiyor mu?

    False döndüren durumlar:
    - Kod/IT modu
    - Chat servisinin zaten hallettiği canlı veri sorguları
    - Kısa statik bilgi soruları

    True döndüren durumlar:
    - Açık araştırma isteği ("araştır", "analiz et")
    - Güncel olaylar ("dünya gündemi", "ne oldu")
    - Uzun ve spesifik sorgular (4+ kelime + yıl)
    """
    if mode in ("code", "it_expert"):
        return False
    q = prompt.lower().strip()
    # Chat servisi hallediyor → gateway dokunma
    if any(k in q for k in _CHAT_HANDLES_THESE):
        return False
    # Açık deep search sinyali
    if any(s in q for s in _DEEP_SEARCH_SIGNALS):
        return True
    # Yıl + uzun sorgu → araştırma
    if re.search(r'\b(202[3-9]|203\d)\b', q) and len(q.split()) >= 4:
        return True
    return False


async def call_deep_search(
    query:        str,
    context_hint: Optional[str] = None,
    language:     str = "tr",
) -> Optional[str]:
    """Smart Tools /deep_search → sentezlenmiş araştırma metni."""
    if not SMART_TOOLS_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=DEEP_SEARCH_TIMEOUT) as client:
            resp = await client.post(
                f"{SMART_TOOLS_URL}/deep_search",
                json={
                    "query":        query,
                    "num_results":  5,
                    "fetch_pages":  3,
                    "synthesize":   True,
                    "language":     language,
                    "context_hint": context_hint,
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("success"):
                return None
            synthesis = data.get("data", {}).get("synthesis", "")
            elapsed   = data.get("data", {}).get("elapsed_seconds", "?")
            print(f"[DEEP SEARCH] ✅ {len(synthesis)} chars | {elapsed}s")
            return synthesis
    except httpx.TimeoutException:
        print("[DEEP SEARCH] Timeout")
        return None
    except Exception as e:
        print(f"[DEEP SEARCH] Error: {e}")
        return None
# ═══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/request_code")
async def request_code_endpoint(req: OTPRequest, request: Request):
    try:
        ip_address = get_client_ip(request)
        abuse_post("/otp/request/check", {"email": req.email, "ip_address": ip_address})
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE email = %s", (req.email,))
            existing_user = cur.fetchone()
            if req.mode == "login" and not existing_user:
                raise HTTPException(status_code=404, detail="Kullanici bulunamadi.")
            if req.mode == "register" and existing_user:
                raise HTTPException(status_code=400, detail="E-posta kullanimda.")
            generated_otp = str(random.randint(100000, 999999))
            cur.execute("""
                INSERT INTO otp_codes (email, code, expire_at, created_at)
                VALUES (%s, %s, NOW() + INTERVAL '5 minutes', NOW())
                ON CONFLICT (email) DO UPDATE
                SET code = EXCLUDED.code, expire_at = EXCLUDED.expire_at, created_at = NOW()
            """, (req.email, generated_otp))
            conn.commit()
            msg = MIMEMultipart()
            msg["From"] = SMTP_FROM; msg["To"] = req.email
            msg["Subject"] = "ONE-BUNE Doğrulama Kodu"
            msg.attach(MIMEText(
                f"Merhaba,\n\nONE-BUNE doğrulama kodun: {generated_otp}\n\n"
                f"Bu kod 5 dakika boyunca gecerlidir.\n\nONE-BUNE AI",
                "plain", "utf-8"
            ))
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.ehlo(); server.starttls(); server.ehlo()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
            abuse_post("/otp/request/mark-sent", {"email": req.email, "ip_address": ip_address})
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
    ip_address = get_client_ip(request)
    try:
        if email == TEST_BYPASS_EMAIL and code == TEST_BYPASS_CODE:
            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
            try:
                cur.execute("SELECT id, name FROM users WHERE email = %s", (email,))
                row = cur.fetchone()
                if not row:
                    cur.execute("INSERT INTO users (email, name, google_id) VALUES (%s, %s, %s) RETURNING id, name",
                                (email, TEST_BYPASS_NAME, f"local_{email}"))
                    row = cur.fetchone(); conn.commit()
                token = jwt.encode({"sub": email, "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE_DAYS)}, JWT_SECRET, algorithm=JWT_ALGORITHM)
                return {"status": "success", "token": token, "user": {"id": row[0], "name": str(row[1]), "email": email}}
            finally:
                pool.putconn(conn)
        abuse_post("/otp/verify/check", {"email": email, "ip_address": ip_address})
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT code, expire_at FROM otp_codes WHERE email = %s LIMIT 1", (email,))
            row = cur.fetchone()
            if not row:
                abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip_address})
                raise HTTPException(status_code=400, detail="Kod bulunamadi.")
            stored_code, expire_at = row
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if expire_at is None or now_utc > expire_at:
                abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip_address})
                raise HTTPException(status_code=400, detail="Kod suresi dolmus.")
            if code != stored_code:
                abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip_address})
                raise HTTPException(status_code=400, detail="Hatali kod.")
            abuse_post("/otp/verify/clear", {"email": email, "ip_address": ip_address})
            if mode == "register":
                cur.execute("SELECT id FROM users WHERE email = %s", (email,))
                if cur.fetchone():
                    raise HTTPException(status_code=400, detail="Bu email zaten kayitli.")
                cur.execute("INSERT INTO users (email, name, google_id) VALUES (%s, %s, %s) RETURNING id, name",
                            (email, name or email.split("@")[0], f"local_{email}"))
                user_row = cur.fetchone()
            elif mode == "login":
                cur.execute("SELECT id, name FROM users WHERE email = %s", (email,))
                user_row = cur.fetchone()
                if not user_row:
                    raise HTTPException(status_code=400, detail="Kullanici bulunamadi.")
            else:
                raise HTTPException(status_code=400, detail="Gecersiz mode.")
            cur.execute("DELETE FROM otp_codes WHERE email = %s", (email,))
            conn.commit()
            token = jwt.encode({"sub": email, "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE_DAYS)}, JWT_SECRET, algorithm=JWT_ALGORITHM)
            return {"status": "success", "token": token, "user": {"id": user_row[0], "name": str(user_row[1]), "email": email}}
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
            cur.execute("SELECT email, name FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found.")
            email, name = row
            return {"status": "success", "user": {"id": user_id, "email": email, "name": name}}
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
        raise HTTPException(status_code=400, detail=f"Dosya çok büyük ({len(content)/1024/1024:.1f}MB). Maksimum: {MAX_FILE_SIZE//1024//1024}MB")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Dosya boş.")
    print(f"[UPLOAD] Starting malware scan: {file.filename}")
    is_clean, scan_result = scan_file_for_malware(file.filename, content)
    if not is_clean:
        if scan_result in ("SCAN_ERROR", "SCAN_FAILED"):
            raise HTTPException(status_code=503, detail="Dosya güvenlik taramasından geçirilemedi.")
        raise HTTPException(status_code=400, detail=f"Dosya güvenlik taramasında reddedildi: {scan_result}")
    extracted_text, file_type = extract_text_from_file(file.filename, content)
    if not extracted_text or extracted_text.startswith("["):
        if extracted_text.startswith("["):
            raise HTTPException(status_code=422, detail=extracted_text.strip("[]"))
        raise HTTPException(status_code=422, detail="Dosyadan metin çıkarılamadı.")
    file_id = str(uuid.uuid4())[:12]
    _store_file_text(file_id, file.filename, extracted_text, user_id=user_id)
    preview = extracted_text[:500] + ("..." if len(extracted_text) > 500 else "")
    print(f"[FILE UPLOAD] '{file.filename}' → type={file_type}, chars={len(extracted_text)}, file_id={file_id}")
    return {
        "status": "success", "file_id": file_id, "filename": file.filename,
        "file_type": file_type, "char_count": len(extracted_text),
        "line_count": extracted_text.count("\n") + 1,
        "preview": preview, "size_bytes": len(content),
    }

# ═══════════════════════════════════════════════════════════════
# IMAGE GENERATION ENDPOINT (direct)
# ═══════════════════════════════════════════════════════════════

@app.post("/generate-image")
async def generate_image_endpoint(request_body: ImageGenerationRequest, request: Request, authorization: str = Header(None)):
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
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT images_generated FROM usage_tracking WHERE user_id=%s AND usage_date=CURRENT_DATE", (user_id,))
            row = cur.fetchone()
            current_images = row[0] if row else 0
            daily_limit = sub.get("limits", {}).get("daily_images", 0)
            if daily_limit != -1 and current_images >= daily_limit:
                raise HTTPException(status_code=429, detail=f"Daily image generation limit reached ({daily_limit}/day)")
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[IMAGE LIMIT CHECK ERROR] {e}")
    ip_address = get_client_ip(request)
    try:
        abuse_post("/image-gen/check", {"user_id": str(user_id), "ip_address": ip_address})
    except HTTPException as e:
        if e.status_code == 429:
            raise HTTPException(status_code=429, detail="Too many image generation requests. Please wait.")
        raise
    try:
        response = await call_service(IMAGE_GEN_SERVICE_URL, "/generate", data={
            "prompt": request_body.prompt, "user_id": user_id,
            "conversation_id": request_body.conversation_id,
            "width": request_body.width, "height": request_body.height,
            "num_images": request_body.num_images,
        }, stream=False, timeout=60)
        try:
            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO usage_tracking (user_id, usage_date, images_generated)
                    VALUES (%s, CURRENT_DATE, %s)
                    ON CONFLICT (user_id, usage_date)
                    DO UPDATE SET images_generated = usage_tracking.images_generated + %s, updated_at = NOW()
                """, (user_id, request_body.num_images, request_body.num_images))
                conn.commit()
            finally:
                pool.putconn(conn)
        except Exception as e:
            print(f"[IMAGE USAGE INCREMENT ERROR] {e}")
        if request_body.conversation_id:
            try:
                pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                try:
                    cur.execute("INSERT INTO messages (conversation_id,role,content,mode,has_image,created_at) VALUES (%s,'user',%s,%s,false,NOW())",
                                (request_body.conversation_id, f"[Image Generation] {request_body.prompt}", request_body.mode))
                    image_urls = response.get("images", [])
                    cur.execute("INSERT INTO messages (conversation_id,role,content,mode,has_image,created_at) VALUES (%s,'assistant',%s,%s,true,NOW())",
                                (request_body.conversation_id, f"Generated {len(image_urls)} image(s)", request_body.mode))
                    conn.commit()
                finally:
                    pool.putconn(conn)
            except Exception as e:
                print(f"[IMAGE MESSAGE SAVE ERROR] {e}")
        return response
    except Exception as e:
        print(f"[IMAGE GEN SERVICE ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Image generation failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════
# IMAGE ANALYSIS ENDPOINT (direct)
# ═══════════════════════════════════════════════════════════════

@app.post("/analyze-image")
async def analyze_image_endpoint(request_body: ImageAnalysisRequest, request: Request, authorization: str = Header(None)):
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
    allowed, remaining, limit = check_usage_limit(user_id)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Daily message limit reached ({limit}/day)")
    ip_address = get_client_ip(request)
    try:
        abuse_post("/vision/check", {"user_id": str(user_id), "ip_address": ip_address})
    except HTTPException as e:
        if e.status_code == 429:
            raise HTTPException(status_code=429, detail="Too many vision requests. Please wait.")
        raise
    try:
        response = await call_service(IMAGE_ANALYSIS_SERVICE_URL, "/analyze", data={
            "image_data": request_body.image_data,
            "query": request_body.query or "What's in this image?",
            "user_id": user_id, "mode": request_body.mode,
        }, stream=False, timeout=30)
        increment_usage(user_id, request_body.mode)
        if request_body.conversation_id:
            try:
                pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                try:
                    user_content = request_body.query or "[Image Analysis Request]"
                    cur.execute("INSERT INTO messages (conversation_id,role,content,mode,has_image,created_at) VALUES (%s,'user',%s,%s,true,NOW())",
                                (request_body.conversation_id, user_content, request_body.mode))
                    assistant_content = response.get("analysis", "Image analysis completed.")
                    cur.execute("INSERT INTO messages (conversation_id,role,content,mode,has_image,created_at) VALUES (%s,'assistant',%s,%s,false,NOW())",
                                (request_body.conversation_id, assistant_content, request_body.mode))
                    conn.commit()
                finally:
                    pool.putconn(conn)
            except Exception as e:
                print(f"[VISION MESSAGE SAVE ERROR] {e}")
        return response
    except Exception as e:
        print(f"[VISION SERVICE ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Image analysis failed: {str(e)}")

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
            cur.execute("INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id, title, created_at", (user_id, data.title))
            result = cur.fetchone(); conn.commit()
            return {"status": "success", "id": str(result[0]), "title": result[1], "created_at": result[2].isoformat()}
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
                SELECT c.id, c.title, c.created_at, c.updated_at, c.is_pinned, COUNT(m.id) as message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user_id = %s AND c.is_archived = FALSE
                GROUP BY c.id ORDER BY c.is_pinned DESC, c.updated_at DESC LIMIT %s
            """, (user_id, limit))
            conversations = []
            for row in cur.fetchall():
                conversations.append({
                    "id": str(row[0]), "title": row[1],
                    "created_at": row[2].isoformat(), "updated_at": row[3].isoformat(),
                    "is_pinned": row[4], "message_count": row[5],
                })
            return {"conversations": conversations}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list conversations: {str(e)}")


@app.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str, authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM conversations WHERE id = %s", (conversation_id,))
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if result[0] != user_id:
                raise HTTPException(status_code=403, detail="Not authorized")
            cur.execute("SELECT id, role, content, created_at, is_edited FROM messages WHERE conversation_id = %s ORDER BY created_at ASC", (conversation_id,))
            messages = []
            for row in cur.fetchall():
                messages.append({"id": str(row[0]), "role": row[1], "content": row[2], "created_at": row[3].isoformat(), "is_edited": row[4]})
            return {"messages": messages}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get messages: {str(e)}")


@app.put("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, data: ConversationUpdate, authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="User required")
    try:
        updates = []; params = []
        if data.title is not None: updates.append("title = %s"); params.append(data.title)
        if data.is_pinned is not None: updates.append("is_pinned = %s"); params.append(data.is_pinned)
        if data.is_archived is not None: updates.append("is_archived = %s"); params.append(data.is_archived)
        if not updates:
            return {"status": "success", "message": "No changes"}
        params.extend([conversation_id, user_id])
        query = f"UPDATE conversations SET {', '.join(updates)} WHERE id = %s AND user_id = %s RETURNING id"
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute(query, params)
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
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("DELETE FROM conversations WHERE id = %s AND user_id = %s RETURNING id", (conversation_id, user_id))
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
            cur.execute("SELECT email, name FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found")
            email, name = row
            cur.execute("SELECT topics, preferences, summary FROM user_profiles WHERE user_id = %s", (user_id,))
            profile_row = cur.fetchone()
            return {
                "user": {"id": user_id, "email": email, "name": name},
                "profile": {
                    "topics": profile_row[0] if profile_row else [],
                    "preferences": profile_row[1] if profile_row else {},
                    "summary": profile_row[2] if profile_row else "",
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
            cur.execute("UPDATE user_profiles SET topics = '[]' WHERE user_id = %s", (user_id,))
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
        raise HTTPException(status_code=400, detail="Rating must be 1 (like) or -1 (dislike)")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO feedback (user_id, conversation_id, user_query, assistant_response, rating, comment)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (user_id, data.conversation_id or "", data.user_query, data.assistant_response, data.rating, data.comment or ""))
            feedback_id = cur.fetchone()[0]; conn.commit()
            return {"status": "success", "feedback_id": feedback_id}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Feedback error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# CODE MODE STATUS
# ═══════════════════════════════════════════════════════════════

@app.get("/code-mode/status")
async def code_mode_status():
    return {
        "enabled": True,
        "model": os.getenv("DEEPINFRA_CODE_MODEL", "Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo"),
        "max_tokens": int(os.getenv("DEEPINFRA_CODE_MAX_TOKENS", "16000")),
        "features": ["complete_code", "no_truncation", "code_monster_mode"],
    }

# ═══════════════════════════════════════════════════════════════
# SUBSCRIPTION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/subscription/status")
async def subscription_status_endpoint(authorization: str = Header(None)):
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
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT id,name,description,price_monthly,price_yearly,currency,features,limits,is_active FROM subscription_plans WHERE is_active=TRUE ORDER BY sort_order")
            plans = []
            for row in cur.fetchall():
                plans.append({
                    "id": row[0], "name": row[1], "description": row[2],
                    "price_monthly": float(row[3]), "price_yearly": float(row[4]),
                    "currency": row[5], "features": row[6], "limits": row[7], "is_active": row[8],
                })
            return {"plans": plans}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Get plans error: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN CHAT ENDPOINT — ADVANCED SMART ROUTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat_endpoint(
    request_body: ChatRequest,
    request: Request,
    authorization: str = Header(None),
):
    """
    Ana chat endpoint — Advanced Smart Routing v2.2

    ROUTING AKIŞI:
    ┌──────────────────────────────────────────────────────────┐
    │ 1. Auth + Abuse + Limit                                  │
    │ 2. Konuşma bağlamını oku (son mod, kod, görsel geçmişi) │
    │ 3. Görsel takip sorusu mu? → önceki görseli restore et  │
    │ 4. Görsel yüklendiyse → IMAGE ANALYSIS                  │
    │ 5. Görsel değiştirme mi? → IMAGE MODIFY                 │
    │ 6. Görsel oluşturma mı? → IMAGE GEN                     │
    │ 7. Code modu / "devam et" → KOD CANAVARI MOD            │
    │ 8. Normal → CHAT SERVICE                                 │
    └──────────────────────────────────────────────────────────┘
    """

    # ── 1. AUTH ─────────────────────────────────────────────
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass

    # ── 2. ABUSE CONTROL ────────────────────────────────────
    ip_address = get_client_ip(request)
    try:
        abuse_post("/chat/check", {"user_id": str(user_id or "guest"), "ip_address": ip_address})
    except HTTPException as e:
        if e.status_code == 429:
            def rate_limited_gen():
                yield "⏳ Çok hızlı istek gönderiyorsun. Lütfen birkaç saniye bekleyip tekrar dene."
            return StreamingResponse(rate_limited_gen(), media_type="text/plain; charset=utf-8")
        raise

    # ── 3. USAGE LIMIT ──────────────────────────────────────
    if user_id:
        allowed, remaining, limit = check_usage_limit(user_id)
        if not allowed:
            def limit_exceeded_gen():
                yield f"⏰ Günlük mesaj limitine ulaştın ({limit}/gün).\n\nYarın tekrar deneyebilirsin! 🚀"
            return StreamingResponse(limit_exceeded_gen(), media_type="text/plain; charset=utf-8")

    conversation_id = request_body.conversation_id

    # ── 4. KONUŞMA BAĞLAMINI OKU ────────────────────────────
    # Son mod, kod snippet, görsel üretim geçmişi
    conv_ctx = get_conversation_context(conversation_id) if conversation_id else {
        "last_mode": "assistant", "had_code": False,
        "last_code_snippet": None, "had_image_gen": False,
        "last_gen_prompt": None, "last_gen_id": None,
    }

    # ── 5. GÖRSEL TAKİP SORUSU KONTROLÜ ─────────────────────
    # Kullanıcı önceki görseli sormak istiyor ama yeni görsel yüklememişse
    if not request_body.image_data and conversation_id:
        follow_up_keywords = [
            'bir önceki', 'önceki', 'yukarıdaki', 'yukardaki',
            'detaylandır', 'daha fazla', 'daha detaylı',
            'görseli', 'resmi', 'bu resim', 'bu görsel',
            'image', 'picture', 'previous', 'above',
            'that image', 'the image', 'the picture',
        ]
        prompt_lower = request_body.prompt.lower() if request_body.prompt else ""
        is_followup = any(kw in prompt_lower for kw in follow_up_keywords)
        if is_followup:
            last_image_data = get_last_image_from_conversation(conversation_id)
            if last_image_data:
                print(f"[SMART ROUTING] Follow-up image question → restoring image context")
                request_body.image_data = last_image_data
            else:
                print(f"[SMART ROUTING] Follow-up image question but no image in history")

    # ── 6. GÖRSEL YÜKLENDIYSE → IMAGE ANALYSIS ──────────────
    if request_body.image_data:
        print(f"[SMART ROUTING] Image uploaded → Image Analysis Service")
        try:
            # Zenginleştirilmiş prompt: metin yoksa konuşma bağlamından otomatik oluştur
            enriched_analysis_prompt = build_image_analysis_context_prompt(
                request_body.prompt,
                conversation_id,
                conv_ctx,
            )
            # Konuşma history'sini de gönder — image analysis service daha iyi bağlam kurar
            analysis_history = []
            if conversation_id:
                try:
                    pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                    try:
                        cur.execute("""
                            SELECT role, content FROM messages
                            WHERE conversation_id = %s
                            ORDER BY created_at DESC LIMIT 10
                        """, (conversation_id,))
                        for row in reversed(cur.fetchall()):
                            # image_data içeren mesajları atla (çok büyük)
                            if row[1] and "[IMAGE_B64]" not in row[1]:
                                analysis_history.append({"role": row[0], "content": row[1]})
                    finally:
                        pool.putconn(conn)
                except Exception as e:
                    print(f"[IMAGE ANALYSIS] History fetch error: {e}")

            async with httpx.AsyncClient(timeout=60.0) as client:
                analysis_request = {
                    "image_data":      request_body.image_data,
                    "prompt":          enriched_analysis_prompt,
                    "conversation_id": str(conversation_id) if conversation_id else None,
                    "user_id":         str(user_id) if user_id else None,
                    "history":         analysis_history,  # Bağlam için geçmiş mesajlar
                }
                response = await client.post(
                    f"{IMAGE_ANALYSIS_SERVICE_URL}/analyze",
                    json=analysis_request,
                    headers={"Authorization": authorization} if authorization else {},
                    timeout=60.0
                )
                if response.status_code == 200:
                    print(f"[IMAGE ANALYSIS] Success")
                    # Auto-create conversation
                    if not conversation_id and user_id:
                        try:
                            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                            try:
                                title = (request_body.prompt[:50] if request_body.prompt else "Görsel Analizi")
                                if len(request_body.prompt or "") > 50: title += "..."
                                cur.execute("INSERT INTO conversations (user_id,title,created_at,updated_at) VALUES (%s,%s,NOW(),NOW()) RETURNING id", (user_id, title))
                                conversation_id = str(cur.fetchone()[0]); conn.commit()
                            finally:
                                pool.putconn(conn)
                        except Exception as e:
                            print(f"[IMAGE ANALYSIS] Conv creation error: {e}")
                    # Kullanıcı mesajını kaydet
                    if conversation_id and user_id:
                        try:
                            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                            try:
                                cur.execute("""
                                    INSERT INTO messages (conversation_id,role,content,has_image,metadata,created_at)
                                    VALUES (%s::uuid,%s,%s,%s,%s,NOW())
                                """, (conversation_id, "user",
                                      request_body.prompt or "Görseli analiz et",
                                      True,
                                      json.dumps({"image_uploaded": True, "analysis_mode": True,
                                                  "image_data": request_body.image_data})))
                                conn.commit()
                            finally:
                                pool.putconn(conn)
                        except Exception as e:
                            print(f"[IMAGE ANALYSIS] Message save error: {e}")
                    # Stream + kaydet
                    collected_response = ""
                    async def stream_and_collect():
                        nonlocal collected_response
                        async for chunk in response.aiter_text():
                            collected_response += chunk
                            yield chunk
                        if conversation_id and user_id and collected_response:
                            try:
                                pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                                try:
                                    cur.execute("INSERT INTO messages (conversation_id,role,content,mode,created_at) VALUES (%s::uuid,%s,%s,%s,NOW())",
                                                (conversation_id, "assistant", collected_response, "vision"))
                                    cur.execute("UPDATE conversations SET updated_at=NOW() WHERE id=%s::uuid", (conversation_id,))
                                    conn.commit()
                                finally:
                                    pool.putconn(conn)
                            except Exception as e:
                                print(f"[IMAGE ANALYSIS] Response save error: {e}")
                        # ── FIX: increment STREAM SONRASI ───
                        if user_id:
                            increment_usage(user_id, request_body.mode or "assistant")
                    return StreamingResponse(stream_and_collect(), media_type="text/plain; charset=utf-8")
                else:
                    print(f"[IMAGE ANALYSIS ERROR] Status {response.status_code}: {response.text}")
        except httpx.TimeoutException:
            print(f"[IMAGE ANALYSIS ERROR] Timeout (>60s)")
        except httpx.RequestError as e:
            print(f"[IMAGE ANALYSIS ERROR] Request failed: {e}")
        except Exception as e:
            print(f"[IMAGE ANALYSIS ERROR] Unexpected error: {e}")
        print(f"[IMAGE ANALYSIS] Failed — routing to Chat Service with image context")
        # Image analysis başarısız → chat service'e düş (image_data ile birlikte)

    # Görsel yüklendiyse modifikasyon/generation OLMAZ (analysis veya chat)
    if request_body.image_data:
        is_modification = False
        is_generation   = False
    else:
        is_modification = detect_image_modification_request(request_body.prompt)
        is_generation   = detect_image_generation_request(request_body.prompt)

    # ── 7. GÖRSEL DEĞİŞTİRME ────────────────────────────────
    modification_of_id = None
    if is_modification and conversation_id and user_id:
        print(f"[SMART ROUTING] Image modification request: {request_body.prompt[:50]}...")
        last_img_id, last_user_prompt, last_generated_prompt = get_last_image_generation_prompt(user_id, conversation_id)
        if last_generated_prompt:
            print(f"[SMART ROUTING] Found previous image prompt, combining...")
            combined_prompt = combine_prompts_for_modification(last_generated_prompt, request_body.prompt)
            original_user_prompt = request_body.prompt
            request_body.prompt = combined_prompt
            modification_of_id  = last_img_id
            is_generation       = True
            print(f"[SMART ROUTING] Combined prompt ready")
        else:
            print(f"[SMART ROUTING] No previous image → treating as new generation")

    # ── 8. GÖRSEL OLUŞTURMA ──────────────────────────────────
    if is_generation and not request_body.image_data:
        print(f"[SMART ROUTING] Image generation request: {request_body.prompt[:50]}...")
        if user_id:
            subscription = get_user_subscription(user_id)
            if not subscription.get("features", {}).get("image_gen", False):
                def premium_required_gen():
                    yield "🔒 Görsel oluşturma özelliği Premium abonelikte mevcut.\n\n"
                    yield "Premium'a geçerek:\n"
                    yield "• Sınırsız görsel oluşturabilirsin\n"
                    yield "• Görselleri analiz edebilirsin\n"
                    yield "• Tüm özelliklere erişebilirsin\n"
                return StreamingResponse(premium_required_gen(), media_type="text/plain; charset=utf-8")
            try:
                pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                try:
                    cur.execute("SELECT COUNT(*) FROM generated_images WHERE user_id=%s AND DATE(created_at)=CURRENT_DATE AND is_deleted=FALSE", (user_id,))
                    daily_count = cur.fetchone()[0]
                    daily_limit = subscription.get("limits", {}).get("image_gen_per_day", 0)
                    if daily_limit > 0 and daily_count >= daily_limit:
                        def limit_gen():
                            yield f"⏰ Günlük görsel oluşturma limitine ulaştın ({daily_limit}/gün).\n\nYarın tekrar deneyebilirsin! 🚀"
                        return StreamingResponse(limit_gen(), media_type="text/plain; charset=utf-8")
                finally:
                    pool.putconn(conn)
            except Exception as e:
                print(f"[IMAGE LIMIT CHECK ERROR] {e}")
        # Auto-create conversation
        if not conversation_id and user_id:
            try:
                pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                try:
                    title = request_body.prompt[:100] + ("..." if len(request_body.prompt) > 100 else "")
                    cur.execute("INSERT INTO conversations (user_id,title,created_at,updated_at) VALUES (%s,%s,NOW(),NOW()) RETURNING id", (user_id, title))
                    conversation_id = str(cur.fetchone()[0]); conn.commit()
                    print(f"[IMAGE GEN] Auto-created conversation {conversation_id}")
                finally:
                    pool.putconn(conn)
            except Exception as e:
                print(f"[IMAGE GEN CONVERSATION ERROR] {e}")
        # History
        conversation_history = []
        if conversation_id:
            try:
                pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                try:
                    cur.execute("SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY created_at DESC LIMIT 15", (conversation_id,))
                    for row in reversed(cur.fetchall()):
                        conversation_history.append({"role": row[0], "content": row[1]})
                finally:
                    pool.putconn(conn)
            except Exception as e:
                print(f"[IMAGE GEN HISTORY ERROR] {e}")
        print(f"[SMART ROUTING] → Image Gen Service")
        user_facing_prompt = original_user_prompt if modification_of_id else request_body.prompt

        async def stream_image_gen():
            try:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    response = await client.post(f"{IMAGE_GEN_SERVICE_URL}/generate", json={
                        "prompt":               request_body.prompt,
                        "user_id":              str(user_id or 0),
                        "conversation_id":      conversation_id,
                        "conversation_history": conversation_history,
                        "size":                 "1024x1024",
                        "save_to_db":           True,
                        "modification_of":      modification_of_id,
                        "original_user_prompt": user_facing_prompt,
                    })
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("success"):
                            yield "✨ **Görsel Başarıyla Oluşturuldu!**\n\n"
                            image_b64      = data.get("image_b64")
                            db_image_id    = data.get("db_image_id")
                            gen_prompt_out = data.get("generated_prompt", request_body.prompt)
                            if image_b64:
                                yield f"[IMAGE_B64]{image_b64}[/IMAGE_B64]"
                            # DB kayıt
                            if conversation_id and user_id:
                                try:
                                    pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                                    try:
                                        cur.execute("INSERT INTO messages (conversation_id,role,content,mode,created_at) VALUES (%s,%s,%s,%s,NOW())",
                                                    (conversation_id, "user", user_facing_prompt, request_body.mode or "assistant"))
                                        cur.execute("INSERT INTO messages (conversation_id,role,content,mode,created_at) VALUES (%s,%s,%s,%s,NOW())",
                                                    (conversation_id, "assistant", f"[Görsel oluşturuldu - DB ID: {db_image_id}]", request_body.mode or "assistant"))
                                        cur.execute("UPDATE conversations SET updated_at=NOW() WHERE id=%s", (conversation_id,))
                                        conn.commit()
                                        print(f"[IMAGE GEN] Saved to conversation {conversation_id}")
                                    finally:
                                        pool.putconn(conn)
                                except Exception as e:
                                    print(f"[IMAGE GEN SAVE ERROR] {e}")
                                # generated_images tablosuna kaydet
                                try:
                                    pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                                    try:
                                        cur.execute("""
                                            INSERT INTO generated_images
                                            (user_id,conversation_id,prompt_turkish,prompt_english,user_prompt,generated_prompt,modification_of,image_b64,created_at)
                                            VALUES (%s,%s::uuid,%s,%s,%s,%s,%s::uuid,%s,NOW()) RETURNING id
                                        """, (user_id, conversation_id, user_facing_prompt, gen_prompt_out,
                                              user_facing_prompt, gen_prompt_out, modification_of_id,
                                              image_b64[:5000] if image_b64 else None))
                                        new_img_id = cur.fetchone()[0]; conn.commit()
                                        print(f"[IMAGE GEN] Tracked generation {new_img_id} (modification_of: {modification_of_id})")
                                    finally:
                                        pool.putconn(conn)
                                except Exception as e:
                                    print(f"[IMAGE GEN INCREMENT ERROR] {e}")
                        else:
                            yield f"⚠️ {data.get('error', 'Bilinmeyen hata')}\n"
                    else:
                        yield f"⚠️ Image service error (HTTP {response.status_code})\n"
            except Exception as e:
                print(f"[IMAGE GEN SERVICE ERROR] {e}")
                yield f"⚠️ Görsel oluşturma hatası: {str(e)}\n"

        response = StreamingResponse(stream_image_gen(), media_type="text/plain; charset=utf-8")
        if conversation_id:
            response.headers["X-Conversation-ID"] = conversation_id
        return response

    # ── 9. NORMAL CHAT AKIŞI ────────────────────────────────
    conversation_id = request_body.conversation_id
    auto_created = False
    if not conversation_id and user_id:
        try:
            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
            try:
                title = request_body.prompt[:100] + ("..." if len(request_body.prompt) > 100 else "")
                cur.execute("INSERT INTO conversations (user_id,title,created_at,updated_at) VALUES (%s,%s,NOW(),NOW()) RETURNING id", (user_id, title))
                conversation_id = str(cur.fetchone()[0]); auto_created = True; conn.commit()
                print(f"[CHAT] Auto-created conversation {conversation_id}")
            finally:
                pool.putconn(conn)
        except Exception as e:
            print(f"[AUTO-CREATE CONVERSATION ERROR] {e}")

    # History
    conversation_history = []
    if conversation_id:
        try:
            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
            try:
                cur.execute("SELECT user_id, title FROM conversations WHERE id = %s", (conversation_id,))
                conv_row = cur.fetchone()
                if conv_row and (not user_id or conv_row[0] == user_id):
                    cur.execute("SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY created_at DESC LIMIT 30", (conversation_id,))
                    messages = cur.fetchall()
                    for row in reversed(messages):
                        conversation_history.append({"role": row[0], "content": row[1]})
                    print(f"[CHAT] Loaded {len(conversation_history)} messages from conversation {conversation_id}")
                    current_title = conv_row[1]
                    if current_title == "Yeni Sohbet" and len(conversation_history) == 0:
                        new_title = request_body.prompt[:100] + ("..." if len(request_body.prompt) > 100 else "")
                        cur.execute("UPDATE conversations SET title=%s WHERE id=%s", (new_title, conversation_id))
                        conn.commit()
            finally:
                pool.putconn(conn)
        except Exception as e:
            print(f"[HISTORY FETCH ERROR] {e}")

    final_history = conversation_history if conversation_history else (
        [{"role": m.role, "content": m.content} for m in (request_body.history or [])]
    )

    # ── KOD CANAVARI MODU ───────────────────────────────────
    # Code modu veya "devam et" isteğinde prompt'u zenginleştir
    prompt = request_body.prompt
    mode   = request_body.mode or conv_ctx.get("last_mode", "assistant")

    if mode == "code" or (
        conv_ctx.get("had_code") and
        any(s in prompt.lower() for s in ["devam et", "continue", "tamamla", "complete", "bitir", "devam"])
    ):
        # "devam et" ise son kod snippet'ini ekle
        if conv_ctx.get("last_code_snippet") and any(
            s in prompt.lower() for s in ["devam et", "continue", "tamamla", "complete", "bitir", "devam"]
        ):
            prompt = (
                f"{prompt}\n\n"
                f"[Tam olarak buradan devam et — kaldığın yerden sürdür:]\n"
                f"```\n{conv_ctx['last_code_snippet']}\n```"
            )
        # Her iki durumda da kod canavarı injection
        prompt = prompt + CODE_MONSTER_INJECTION

    # ── GATEWAY DEEP SEARCH ──────────────────────────────────
    # Local detection (0ms) — sadece araştırma sorgular için
    # Kur/hava/kripto/haberler chat servisi halleder, gateway dokunmaz
    web_context     = None
    web_suggest_msg = ""

    if _gateway_needs_deep_search(prompt, mode):
        print(f"[GATEWAY] Deep search tetiklendi: '{prompt[:40]}'")
        context_hint = None
        for msg in reversed(conversation_history[-6:]):
            if msg.get("role") == "assistant" and len(msg.get("content","")) > 20:
                context_hint = msg["content"][:200]
                break
        web_context = await call_deep_search(
            query        = prompt,
            context_hint = context_hint,
            language     = "tr",
        )
        if web_context:
            print(f"[GATEWAY] Deep search context: {len(web_context)} chars")

    # context birleştir (gateway deep search + gateway'den gelen RAG vs)
    final_context = ""
    if web_context:
        final_context = f"[WEB ARAŞTIRMA — GÜNCEL BİLGİ]\n{web_context}\n[/WEB ARAŞTIRMA]"
    if request_body.context:
        final_context = (final_context + "\n\n" + request_body.context).strip()

    chat_data = {
        "prompt":          prompt,
        "mode":            mode,
        "user_id":         user_id or 0,
        "conversation_id": conversation_id,
        "history":         final_history,
        "context":         final_context if final_context else None,
        "session_summary": request_body.session_summary,
    }

    # Görsel varsa (image analysis başarısız → fallback) chat service'e de gönder
    if request_body.image_data:
        chat_data["image_data"] = request_body.image_data

    async def stream_response():
        assistant_response = ""
        try:
            # Deep search önerisi varsa önce gönder
            if web_suggest_msg:
                yield f"\n💡 *{web_suggest_msg}*\n\n---\n\n"

            generator = await call_service(CHAT_SERVICE_URL, "/chat", data=chat_data, stream=True, timeout=300)
            async for chunk in generator:
                assistant_response += chunk
                yield chunk
            # ── FIX: Save + increment STREAM SONRASI ────────
            if conversation_id and user_id:
                try:
                    pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
                    try:
                        cur.execute("INSERT INTO messages (conversation_id,role,content,mode,created_at) VALUES (%s,%s,%s,%s,NOW())",
                                    (conversation_id, "user", request_body.prompt, mode))
                        cur.execute("INSERT INTO messages (conversation_id,role,content,mode,created_at) VALUES (%s,%s,%s,%s,NOW())",
                                    (conversation_id, "assistant", assistant_response, mode))
                        cur.execute("UPDATE conversations SET updated_at=NOW() WHERE id=%s", (conversation_id,))
                        conn.commit()
                        print(f"[CHAT] Saved messages to conversation {conversation_id}")
                    finally:
                        pool.putconn(conn)
                except Exception as e:
                    print(f"[MESSAGE SAVE ERROR] {e}")
            if user_id:
                increment_usage(user_id, mode)
        except Exception as e:
            print(f"[CHAT SERVICE ERROR] {e}")
            yield f"\n⚠️ Chat service error: {str(e)}"

    response = StreamingResponse(stream_response(), media_type="text/plain; charset=utf-8")
    if conversation_id:
        response.headers["X-Conversation-ID"] = conversation_id
        if auto_created:
            response.headers["X-Conversation-Created"] = "true"
    if web_context:
        response.headers["X-Web-Search"] = "true"
    return response


# ═══════════════════════════════════════════════════════════════
# WEB SEARCH ENDPOINT — Frontend için doğrudan deep search
# ═══════════════════════════════════════════════════════════════

class WebSearchGatewayRequest(BaseModel):
    query:          str
    num_results:    int  = 5
    fetch_pages:    int  = 3
    synthesize:     bool = True
    conversation_id: Optional[str] = None


@app.post("/web_search")
async def web_search_endpoint(
    request_body: WebSearchGatewayRequest,
    request: Request,
    authorization: str = Header(None),
):
    """
    Frontend bu endpoint'i çağırarak doğrudan deep search yapabilir.
    Kullanıcı "webde ara" butonuna bastığında kullanılır.

    Aynı zamanda gateway'in detect_needs_web_search() ile otomatik
    tetiklediği aramaları manuel olarak da tetikleyebilirsin.
    """
    user_id = None
    try:
        user_id = get_user_from_token(authorization)
    except Exception:
        pass

    ip = get_client_ip(request)
    try:
        abuse_post("/chat/check", {"user_id": str(user_id or "guest"), "ip_address": ip})
    except HTTPException as e:
        if e.status_code == 429:
            raise HTTPException(status_code=429, detail="Çok fazla istek. Lütfen bekleyin.")
        raise

    # Konuşma bağlamından context ipucu al
    context_hint = None
    if request_body.conversation_id:
        try:
            pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
            try:
                cur.execute("""
                    SELECT content FROM messages
                    WHERE conversation_id=%s AND role='assistant'
                    ORDER BY created_at DESC LIMIT 1
                """, (request_body.conversation_id,))
                row = cur.fetchone()
                if row and row[0]:
                    context_hint = row[0][:200]
            finally:
                pool.putconn(conn)
        except Exception:
            pass

    synthesis = await call_deep_search(
        query        = request_body.query,
        fetch_pages  = request_body.fetch_pages,
        context_hint = context_hint,
    )

    if synthesis:
        return {
            "success":   True,
            "query":     request_body.query,
            "synthesis": synthesis,
            "tool_used": "deep_search",
        }
    else:
        return {
            "success": False,
            "query":   request_body.query,
            "error":   "Deep search servisi ulaşılamaz — lütfen tekrar deneyin.",
            "tool_used": "deep_search",
        }

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADMIN ENDPOINTS — /admin/*
#
# Nginx /admin → gateway /admin/* yönlendirir.
# admin.php bu endpoint'leri Bearer token ile çağırır.
# Her endpoint is_admin = TRUE kontrolü yapar.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

def require_admin(authorization: Optional[str] = None) -> int:
    """
    Admin yetkisi kontrolü.
    İki yol:
    1. API_TOKEN (server-side bearer) → admin.php'den gelir, direkt kabul et
    2. JWT token → kullanıcı token'ı, is_admin=TRUE kontrolü yap
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header eksik")

    token = authorization.replace("Bearer ", "").strip()

    # Yol 1: Server-side API token (admin.php kullanır)
    if token == API_TOKEN:
        return 0  # admin.php'den geldi, güvenli

    # Yol 2: JWT token → is_admin kontrolü
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email   = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Geçersiz token")
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT id, is_admin FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if not row or not row[1]:
                raise HTTPException(status_code=403, detail="Admin yetkisi gerekli")
            return row[0]
        finally:
            pool.putconn(conn)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token süresi doldu")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Geçersiz token")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/stats ─────────────────────────────────────────────
@app.get("/admin/stats")
async def admin_stats(authorization: str = Header(None)):
    require_admin(authorization)
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            # Toplam kullanıcı
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]

            # Premium kullanıcı
            cur.execute("""
                SELECT COUNT(DISTINCT u.id) FROM users u
                LEFT JOIN user_subscriptions us ON us.user_id = u.id
                WHERE u.is_premium = TRUE
                   OR (us.status IN ('active','trialing') AND us.plan_id != 'free')
            """)
            premium_users = cur.fetchone()[0]

            # Banlı kullanıcı
            cur.execute("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")
            banned_users = cur.fetchone()[0]

            # Bugün mesaj sayısı
            cur.execute("""
                SELECT COALESCE(SUM(messages_sent), 0)
                FROM usage_tracking
                WHERE usage_date = CURRENT_DATE
            """)
            messages_today = cur.fetchone()[0]

            # Bu ay gelir (payment_history)
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM payment_history
                WHERE status = 'completed'
                  AND created_at >= date_trunc('month', NOW())
            """)
            revenue_this_month = float(cur.fetchone()[0])

            # Bu ay yeni kayıt
            cur.execute("""
                SELECT COUNT(*) FROM users
                WHERE created_at >= date_trunc('month', NOW())
            """)
            new_users_month = cur.fetchone()[0]

            # Son kaydolan 10 kullanıcı
            cur.execute("""
                SELECT u.id, u.name, u.email, u.is_premium, u.is_banned,
                       u.created_at, u.last_active,
                       COALESCE(ut.messages_sent, 0) AS messages_today,
                       COALESCE(
                           (SELECT SUM(messages_sent) FROM usage_tracking
                            WHERE user_id = u.id), 0
                       ) AS total_messages
                FROM users u
                LEFT JOIN usage_tracking ut
                    ON ut.user_id = u.id AND ut.usage_date = CURRENT_DATE
                ORDER BY u.created_at DESC
                LIMIT 10
            """)
            cols = ["id","name","email","is_premium","is_banned",
                    "created_at","last_active","messages_today","total_messages"]
            recent_users = [dict(zip(cols, row)) for row in cur.fetchall()]

            # created_at'ı string'e çevir
            for u in recent_users:
                if u.get("created_at"):
                    u["created_at"] = u["created_at"].isoformat()
                if u.get("last_active"):
                    u["last_active"] = u["last_active"].isoformat()

            return {
                "total_users":       total_users,
                "premium_users":     premium_users,
                "banned_users":      banned_users,
                "messages_today":    int(messages_today),
                "revenue_this_month": revenue_this_month,
                "new_users_month":   new_users_month,
                "recent_users":      recent_users,
            }
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/users ─────────────────────────────────────────────
@app.get("/admin/users")
async def admin_users(
    page:   int  = 1,
    limit:  int  = 20,
    search: str  = "",
    filter: str  = "",   # all | premium | free | banned
    authorization: str = Header(None),
):
    require_admin(authorization)
    limit  = min(limit, 100)
    offset = (page - 1) * limit

    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            # WHERE koşulları — sadece users tablosuna göre
            conditions = ["1=1"]
            params: list = []

            if search:
                conditions.append("(u.name ILIKE %s OR u.email ILIKE %s)")
                params += [f"%{search}%", f"%{search}%"]

            if filter == "premium":
                conditions.append("""(u.is_premium = TRUE OR EXISTS (
                    SELECT 1 FROM user_subscriptions s2
                    WHERE s2.user_id = u.id
                      AND s2.status IN ('active','trialing')
                      AND s2.plan_id != 'free'
                ))""")
            elif filter == "free":
                conditions.append("""u.is_premium = FALSE
                    AND NOT EXISTS (
                        SELECT 1 FROM user_subscriptions s2
                        WHERE s2.user_id = u.id
                          AND s2.status IN ('active','trialing')
                          AND s2.plan_id != 'free'
                    )
                    AND u.is_banned = FALSE""")
            elif filter == "banned":
                conditions.append("u.is_banned = TRUE")

            where = " AND ".join(conditions)

            # Toplam sayı — sadece users u
            cur.execute(f"SELECT COUNT(*) FROM users u WHERE {where}", params)
            total = cur.fetchone()[0]

            # Kullanıcılar — LEFT JOIN ile detay
            cur.execute(f"""
                SELECT u.id, u.name, u.email,
                       u.is_premium, u.is_banned, u.is_admin,
                       u.ban_reason, u.created_at, u.last_active,
                       COALESCE(ut.messages_sent, 0) AS messages_today,
                       COALESCE(
                           (SELECT SUM(messages_sent) FROM usage_tracking
                            WHERE user_id = u.id), 0
                       ) AS total_messages,
                       us.plan_id, us.status AS sub_status,
                       us.current_period_end
                FROM users u
                LEFT JOIN usage_tracking ut
                    ON ut.user_id = u.id AND ut.usage_date = CURRENT_DATE
                LEFT JOIN user_subscriptions us
                    ON us.user_id = u.id AND us.status IN ('active','trialing')
                WHERE {where}
                ORDER BY u.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])

            cols = ["id","name","email","is_premium","is_banned","is_admin",
                    "ban_reason","created_at","last_active",
                    "messages_today","total_messages",
                    "plan_id","sub_status","premium_expires_at"]
            users = []
            for row in cur.fetchall():
                u = dict(zip(cols, row))
                if u.get("created_at"):
                    u["created_at"] = u["created_at"].isoformat()
                if u.get("last_active"):
                    u["last_active"] = u["last_active"].isoformat()
                if u.get("premium_expires_at"):
                    u["premium_expires_at"] = u["premium_expires_at"].isoformat()
                users.append(u)

            return {
                "users":       users,
                "total":       total,
                "page":        page,
                "total_pages": max(1, -(-total // limit)),  # ceiling div
            }
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/users/{id} ────────────────────────────────────────
@app.get("/admin/users/{user_id}")
async def admin_get_user(
    user_id: int,
    authorization: str = Header(None),
):
    require_admin(authorization)
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                SELECT u.id, u.name, u.email,
                       u.is_premium, u.is_banned, u.is_admin,
                       u.ban_reason, u.banned_at,
                       u.created_at, u.last_active,
                       COALESCE(ut.messages_sent, 0) AS messages_today,
                       COALESCE(
                           (SELECT SUM(messages_sent) FROM usage_tracking
                            WHERE user_id = u.id), 0
                       ) AS total_messages,
                       us.plan_id, us.status AS sub_status,
                       us.current_period_end
                FROM users u
                LEFT JOIN usage_tracking ut
                    ON ut.user_id = u.id AND ut.usage_date = CURRENT_DATE
                LEFT JOIN user_subscriptions us
                    ON us.user_id = u.id AND us.status IN ('active','trialing')
                WHERE u.id = %s
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

            cols = ["id","name","email","is_premium","is_banned","is_admin",
                    "ban_reason","banned_at","created_at","last_active",
                    "messages_today","total_messages",
                    "plan_id","sub_status","premium_expires_at"]
            u = dict(zip(cols, row))
            for k in ["banned_at","created_at","last_active","premium_expires_at"]:
                if u.get(k):
                    u[k] = u[k].isoformat()
            return u
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/users/{id}/premium ────────────────────────────────
@app.post("/admin/users/{user_id}/admin")
async def admin_toggle_admin(
    user_id: int,
    body:    dict,
    authorization: str = Header(None),
):
    admin_id   = require_admin(authorization)
    make_admin = bool(body.get("is_admin", False))
    if user_id == admin_id:
        raise HTTPException(status_code=400, detail="Kendinizin admin yetkisini değiştiremezsiniz.")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE users SET is_admin = %s WHERE id = %s
                RETURNING id, name, email
            """, (make_admin, user_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

            user_id_ret, user_name, user_email = row

            if make_admin:
                # Rastgele 6 haneli şifre üret
                import random as _rnd, string as _str
                new_password = ''.join(_rnd.choices(_str.digits + _str.ascii_letters, k=8))

                # Şifreyi pgcrypto ile hash'le ve kaydet
                cur.execute(
                    "SELECT crypt(%s, gen_salt('bf', 12))", (new_password,)
                )
                pwd_hash = cur.fetchone()[0]
                cur.execute(
                    "UPDATE users SET password = %s WHERE id = %s",
                    (pwd_hash, user_id)
                )

                conn.commit()

                # Email gönder — mevcut SMTP sistemi
                admin_panel_url = "https://one-bune.com/admin/login.html"
                try:
                    msg = MIMEMultipart("alternative")
                    msg["From"]    = SMTP_FROM
                    msg["To"]      = user_email
                    msg["Subject"] = "ONE-BUNE Admin Panel Erişim Bilgileri"
                    html = f"""
                    <div style="font-family:Inter,sans-serif;max-width:500px;margin:0 auto;
                                background:#0a0a0c;padding:32px;border-radius:16px;
                                border:1px solid rgba(255,255,255,0.07)">
                        <div style="font-size:20px;font-weight:700;color:#00f2fe;margin-bottom:20px">
                            🛡️ ONE-BUNE Admin Panel
                        </div>
                        <p style="color:#eeeef0;margin-bottom:20px">
                            Merhaba <strong>{user_name}</strong>,<br>
                            Admin paneline erişim yetkiniz tanımlandı.
                        </p>
                        <div style="background:#111114;border-radius:12px;padding:20px;
                                    border:1px solid rgba(0,242,254,0.2);margin-bottom:20px">
                            <div style="margin-bottom:12px">
                                <span style="color:#55556a;font-size:12px">PANEL ADRESİ</span><br>
                                <a href="{admin_panel_url}" style="color:#00f2fe;font-size:14px">
                                    {admin_panel_url}
                                </a>
                            </div>
                            <div style="margin-bottom:12px">
                                <span style="color:#55556a;font-size:12px">KULLANICI ADI</span><br>
                                <span style="color:#eeeef0;font-size:16px;font-weight:600">{user_email}</span>
                            </div>
                            <div>
                                <span style="color:#55556a;font-size:12px">ŞİFRE</span><br>
                                <span style="color:#00f2fe;font-size:24px;font-weight:800;
                                            letter-spacing:4px">{new_password}</span>
                            </div>
                        </div>
                        <p style="color:#55556a;font-size:12px">
                            🔒 Giriş yaptıktan sonra bu adrese OTP kodu gönderilecektir.<br>
                            ⚠️ Bu bilgileri kimseyle paylaşmayın.
                        </p>
                    </div>"""
                    msg.attach(MIMEText(html, "html", "utf-8"))
                    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
                        s.ehlo(); s.starttls(); s.ehlo()
                        s.login(SMTP_USER, SMTP_PASS)
                        s.send_message(msg)
                    print(f"[ADMIN] Admin bilgileri gönderildi: {user_email}")
                except Exception as e:
                    print(f"[ADMIN] Email gönderilemedi: {e}")
            else:
                # Admin yetkisi kaldır — şifreyi sıfırla
                cur.execute("UPDATE users SET password = NULL WHERE id = %s", (user_id,))
                conn.commit()

            cur.execute("""
                INSERT INTO payment_audit_log (event_type, user_id, data)
                VALUES (%s, %s, %s)
            """, (
                "admin_grant" if make_admin else "admin_revoke",
                user_id,
                json.dumps({"by_admin": admin_id, "is_admin": make_admin}),
            ))
            conn.commit()
            return {"success": True, "user_id": user_id, "is_admin": make_admin}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/admin/users/{user_id}/premium")
async def admin_toggle_premium(
    user_id: int,
    body:    dict,
    authorization: str = Header(None),
):
    admin_id = require_admin(authorization)
    enable   = bool(body.get("premium", False))
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                UPDATE users
                SET is_premium = %s,
                    subscription_active = %s
                WHERE id = %s
                RETURNING id, name, email
            """, (enable, enable, user_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

            # Eğer premium yapılıyorsa premium plan ata
            if enable:
                cur.execute("""
                    INSERT INTO user_subscriptions
                        (user_id, plan_id, status, billing_period,
                         current_period_start, current_period_end)
                    VALUES (%s, 'premium', 'active', 'monthly',
                            NOW(), NOW() + INTERVAL '30 days')
                    ON CONFLICT (user_id) WHERE status IN ('active','trialing')
                    DO UPDATE SET
                        plan_id = 'premium',
                        status  = 'active',
                        current_period_start = NOW(),
                        current_period_end   = NOW() + INTERVAL '30 days',
                        updated_at = NOW()
                """, (user_id,))
            else:
                # Premium kaldır — subscription'ı iptal et
                cur.execute("""
                    UPDATE user_subscriptions
                    SET status = 'cancelled', cancelled_at = NOW(), updated_at = NOW()
                    WHERE user_id = %s AND status IN ('active','trialing')
                """, (user_id,))

            # Audit log
            cur.execute("""
                INSERT INTO payment_audit_log (event_type, user_id, data)
                VALUES (%s, %s, %s)
            """, (
                "admin_premium_grant" if enable else "admin_premium_revoke",
                user_id,
                json.dumps({"by_admin": admin_id, "enabled": enable}),
            ))
            conn.commit()
            return {"success": True, "user_id": user_id, "is_premium": enable}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/users/{id}/ban ────────────────────────────────────
@app.post("/admin/users/{user_id}/ban")
async def admin_ban_user(
    user_id: int,
    body:    dict,
    authorization: str = Header(None),
):
    admin_id = require_admin(authorization)
    ban      = bool(body.get("banned", True))
    reason   = str(body.get("reason", ""))[:500]
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                UPDATE users
                SET is_banned  = %s,
                    ban_reason = %s,
                    banned_at  = %s
                WHERE id = %s AND is_admin = FALSE
                RETURNING id
            """, (ban, reason if ban else None,
                  datetime.datetime.utcnow() if ban else None,
                  user_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail="Kullanıcı bulunamadı veya admin banlanamaz"
                )
            cur.execute("""
                INSERT INTO payment_audit_log (event_type, user_id, data)
                VALUES (%s, %s, %s)
            """, (
                "admin_ban" if ban else "admin_unban",
                user_id,
                json.dumps({"by_admin": admin_id, "reason": reason}),
            ))
            conn.commit()
            return {"success": True, "user_id": user_id, "is_banned": ban}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/users/{id}/reset_usage ───────────────────────────
@app.post("/admin/users/{user_id}/reset_usage")
async def admin_reset_usage(
    user_id: int,
    authorization: str = Header(None),
):
    require_admin(authorization)
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("""
                UPDATE usage_tracking
                SET messages_sent = 0,
                    updated_at    = NOW()
                WHERE user_id   = %s
                  AND usage_date = CURRENT_DATE
            """, (user_id,))
            conn.commit()
            return {"success": True, "user_id": user_id}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/payments ──────────────────────────────────────────
@app.get("/admin/payments")
async def admin_payments(
    page:  int = 1,
    limit: int = 20,
    authorization: str = Header(None),
):
    require_admin(authorization)
    limit  = min(limit, 100)
    offset = (page - 1) * limit
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM payment_history")
            total = cur.fetchone()[0]

            cur.execute("""
                SELECT ph.id, ph.user_id, u.name AS user_name, u.email AS user_email,
                       ph.plan_id AS plan_name, ph.amount, ph.currency,
                       ph.status, ph.iyzico_payment_id AS iyzico_id,
                       ph.created_at
                FROM payment_history ph
                JOIN users u ON u.id = ph.user_id
                ORDER BY ph.created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))

            cols = ["id","user_id","user_name","user_email","plan_name",
                    "amount","currency","status","iyzico_id","created_at"]
            payments = []
            for row in cur.fetchall():
                p = dict(zip(cols, row))
                if p.get("created_at"):
                    p["created_at"] = p["created_at"].isoformat()
                if p.get("amount"):
                    p["amount"] = float(p["amount"])
                payments.append(p)

            return {
                "payments":    payments,
                "total":       total,
                "page":        page,
                "total_pages": max(1, -(-total // limit)),
            }
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /admin/logs ──────────────────────────────────────────────
@app.get("/admin/logs")
async def admin_logs(
    page:  int = 1,
    limit: int = 50,
    type:  str = "",
    authorization: str = Header(None),
):
    require_admin(authorization)
    limit  = min(limit, 200)
    offset = (page - 1) * limit
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur  = conn.cursor()
        try:
            where  = "WHERE event_type ILIKE %s" if type else ""
            params = [f"%{type}%"] if type else []

            cur.execute(f"""
                SELECT pal.id, pal.event_type AS type,
                       u.email AS user_email,
                       pal.data::text AS description,
                       pal.ip_address AS ip,
                       pal.created_at
                FROM payment_audit_log pal
                LEFT JOIN users u ON u.id = pal.user_id
                {where}
                ORDER BY pal.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])

            cols = ["id","type","user_email","description","ip","created_at"]
            logs = []
            for row in cur.fetchall():
                l = dict(zip(cols, row))
                if l.get("created_at"):
                    l["created_at"] = l["created_at"].isoformat()
                logs.append(l)

            return {"logs": logs, "page": page}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




# ── /admin/send_otp + /admin/verify_otp ──────────────────────
# admin.php şifre doğruladıktan sonra buraya istek atar.
# OTP üretir, ADMIN_NOTIFICATION_EMAIL'e gönderir.

ADMIN_NOTIFICATION_EMAIL = os.getenv("ADMIN_NOTIFICATION_EMAIL", "")

class AdminOTPRequest(BaseModel):
    username: str

class AdminOTPVerifyRequest(BaseModel):
    username: str
    otp:      str


@app.post("/admin/verify_password")
async def admin_verify_password(
    body: dict,
    authorization: str = Header(None),
):
    if not authorization or authorization.replace("Bearer ", "") != API_TOKEN:
        raise HTTPException(status_code=401, detail="Yetkisiz")

    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    if not username or not password:
        return {"status": "error", "message": "Kullanıcı adı ve şifre gerekli."}

    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            # username = "admin" → sabit admin kullanıcısı (name veya email)
            # username = email → UI'dan admin yapılan kullanıcı
            cur.execute("""
                SELECT id, email, name, password
                FROM users
                WHERE is_admin = TRUE
                  AND (name = %s OR email = %s)
                LIMIT 1
            """, (username, username))
            admin = cur.fetchone()

            if not admin or not admin[3]:
                return {"status": "error", "message": "Kullanıcı bulunamadı."}

            # pgcrypto crypt doğrulama
            cur.execute(
                "SELECT (password = crypt(%s, password)) AS ok FROM users WHERE id = %s",
                (password, admin[0])
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return {"status": "error", "message": "Şifre yanlış."}

            return {"status": "success", "username": admin[2] or admin[1], "email": admin[1]}
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[ADMIN VERIFY_PASSWORD] {e}")
        raise HTTPException(status_code=500, detail="Sunucu hatası.")


@app.post("/admin/send_otp")
async def admin_send_otp(req: AdminOTPRequest, request: Request,
                          authorization: str = Header(None)):
    """
    OTP üret ve doğru adrese gönder:
    - Sabit admin (admin@one-bune.com) → ADMIN_NOTIFICATION_EMAIL
    - Diğer adminler → kendi kayıtlı email adresleri
    """
    if not authorization or authorization.replace("Bearer ","") != API_TOKEN:
        raise HTTPException(status_code=401, detail="Yetkisiz")

    import random as _rnd
    otp_code = str(_rnd.randint(100000, 999999))
    ip       = get_client_ip(request)

    # Kime gönderilecek belirle
    otp_target = ADMIN_NOTIFICATION_EMAIL  # varsayılan
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                SELECT email FROM users
                WHERE is_admin = TRUE AND (name = %s OR email = %s)
                LIMIT 1
            """, (req.username, req.username))
            row = cur.fetchone()
            if row and row[0] != "admin@one-bune.com":
                otp_target = row[0]  # diğer adminler kendi mailine

            # OTP'yi kaydet
            cur.execute("""
                INSERT INTO otp_codes (email, code, expire_at, created_at)
                VALUES (%s, %s, NOW() + INTERVAL '5 minutes', NOW())
                ON CONFLICT (email) DO UPDATE
                SET code=EXCLUDED.code, expire_at=EXCLUDED.expire_at, created_at=NOW()
            """, (f"admin_{req.username}", otp_code))
            conn.commit()
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB hatası: {e}")

    # Email gönder
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = SMTP_FROM
        msg["To"]      = otp_target
        msg["Subject"] = "ONE-BUNE Admin Giriş Kodu"
        html = f"""
        <div style="font-family:Inter,sans-serif;max-width:480px;margin:0 auto;
                    background:#0a0a0c;padding:32px;border-radius:16px;
                    border:1px solid rgba(255,255,255,0.07)">
            <div style="font-size:20px;font-weight:700;color:#00f2fe;margin-bottom:20px">
                🛡️ ONE-BUNE Admin Panel
            </div>
            <p style="color:#eeeef0">Giriş doğrulama kodunuz:</p>
            <div style="font-size:40px;font-weight:800;letter-spacing:10px;
                        color:#00f2fe;background:#111114;padding:24px;
                        border-radius:12px;text-align:center;
                        border:1px solid rgba(0,242,254,0.2);margin:20px 0">
                {otp_code}
            </div>
            <p style="color:#55556a;font-size:12px;margin-top:16px">
                ⏱ 5 dakika geçerlidir. &nbsp;|&nbsp; 🌐 IP: {ip}<br>
                🔒 Bu kodu kimseyle paylaşmayın.
            </p>
        </div>"""
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        print(f"[ADMIN OTP] → {otp_target}, IP={ip}")
        return {"status": "success"}
    except Exception as e:
        print(f"[ADMIN OTP] Email hatası: {e}")
        raise HTTPException(status_code=500, detail=f"Email gönderilemedi: {e}")


@app.post("/admin/verify_otp")
async def admin_verify_otp(req: AdminOTPVerifyRequest,
                            authorization: str = Header(None)):
    """OTP doğrula — admin.php doğru kodu buraya gönderir."""
    if not authorization or authorization.replace("Bearer ","") != API_TOKEN:
        raise HTTPException(status_code=401, detail="Yetkisiz")
    try:
        pool = _get_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                SELECT code, expire_at FROM otp_codes WHERE email=%s
            """, (f"admin_{req.username}",))
            row = cur.fetchone()
            if not row:
                return {"status":"error","message":"OTP bulunamadı. Tekrar giriş yapın."}
            code, expire_at = row
            import datetime as _dt
            if _dt.datetime.now(_dt.timezone.utc) > expire_at:
                cur.execute("DELETE FROM otp_codes WHERE email=%s", (f"admin_{req.username}",))
                conn.commit()
                return {"status":"error","expired":True,"message":"Kod süresi doldu."}
            if req.otp.strip() != code:
                return {"status":"error","message":"Kod yanlış."}
            cur.execute("DELETE FROM otp_codes WHERE email=%s", (f"admin_{req.username}",))
            conn.commit()
            print(f"[ADMIN OTP] ✅ {req.username}")
            return {"status":"success"}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": "2.2.0",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "services": {
            "database":             "connected",
            "chat_service":         bool(CHAT_SERVICE_URL),
            "image_gen_service":    bool(IMAGE_GEN_SERVICE_URL),
            "image_analysis_service": bool(IMAGE_ANALYSIS_SERVICE_URL),
            "rag_service":          bool(RAG_SERVICE_URL),
            "smart_tools":          bool(SMART_TOOLS_URL),
            "abuse_control":        "configured" if ABUSE_CONTROL_URL else "not_configured",
            "clamav":               "enabled" if CLAMAV_ENABLED else "disabled",
            "smtp":                 "configured" if SMTP_USER else "not_configured",
        },
    }

@app.get("/")
async def root():
    return {
        "service": "Skylight API Gateway",
        "version": "2.2.0",
        "status":  "running",
        "features": {
            "smart_routing":          True,
            "image_gen_detection":    "advanced_v2.2",
            "image_modify_detection": "advanced_v2.2",
            "code_monster_mode":      True,
            "conversation_context":   True,
            "follow_up_image":        True,
            "abuse_control":          bool(ABUSE_CONTROL_URL),
            "virus_scanning":         CLAMAV_ENABLED,
            "file_upload":            True,
            "otp_auth":               bool(SMTP_USER),
            "subscriptions":          True,
            "quota_management":       True,
            "image_generation":       True,
            "image_analysis":         True,
        },
        "endpoints": {
            "conversations": ["create", "list", "messages", "update", "delete"],
            "profile":       ["get", "clear_topics"],
            "feedback":      ["submit"],
            "code_mode":     ["status"],
            "image":         ["generate", "analyze"],
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8443)