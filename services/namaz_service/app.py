"""
═══════════════════════════════════════════════════════════════
NAMAZ-CHAT SERVICE v1.0
═══════════════════════════════════════════════════════════════
OneBune Namaz Vakitleri Android uygulamasi icin basit, izole
chatbot servisi. Dini konularda kapsamli destek verir.

Mimari:
  Android -> gateway /namaz/chat -> BU SERVIS -> Vertex AI Gemini -> stream

Ozellikler:
  - Tek sabit sistem promptu (dini asistan)
  - Vertex AI Gemini Flash, Google Search grounding ile
    (guncel bilgi gerektiren sorularda internetten arama yapabilir)
  - Konusma gecmisi: namaz_app_conversations / namaz_app_messages
    tablolarinda saklanir (chatbot'un kendi tablolarindan TAMAMEN AYRI)
  - Basit, tek dosya, kolay bakim

Mevcut buyuk chat-service'teki (intent classifier, task builder,
code context, conversation_state/Redis vb.) hicbir karmasik
mekanizmasini KULLANMAZ - namaz app'in ihtiyaci bu kadar basit
oldugu icin kasitli olarak minimal tutulmustur.
═══════════════════════════════════════════════════════════════
"""

import os
import json
import asyncio
import datetime
from typing import Optional, List, Dict, AsyncGenerator

import asyncpg
import jwt
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

JWT_SECRET    = os.getenv("JWT_SECRET", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

DB_HOST     = os.getenv("DB_HOST", "postgres")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME", "skylight_db")
DB_USER     = os.getenv("DB_USER", "skylight_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Vertex AI - mevcut chat-service ile AYNI Service Account key kullanılır
GEMINI_PROJECT     = os.getenv("GEMINI_PROJECT", "gen-lang-client-0907571701")
GEMINI_LOCATION    = os.getenv("GEMINI_LOCATION", "us-central1")
GEMINI_SA_KEY_PATH = "/etc/vertex-sa/key.json"
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")  # AI Studio fallback (SA key yoksa)
# Namaz app icin model - flash (grounding/arama destekli, guvenilir)
NAMAZ_GEMINI_MODEL = os.getenv("NAMAZ_GEMINI_MODEL", "gemini-2.5-flash")

MAX_HISTORY_MESSAGES = 20  # Gemini'ye gonderilecek son N mesaj

app = FastAPI(title="OneBune Namaz Chat Service", version="1.0.0")
db_pool: Optional[asyncpg.Pool] = None

# ═══════════════════════════════════════════════════════════════
# SISTEM PROMPTU — Dini Asistan
# ═══════════════════════════════════════════════════════════════

NAMAZ_SYSTEM_PROMPT = """Sen OneBune Namaz Vakitleri uygulamasının dini asistanısın.

GÖREV TANIMIN:
Kullanıcılara İslami konularda yardımcı olan, kapsamlı, güvenilir ve nazik bir asistansın.
Namaz, oruç, zekat, hac, dualar, hadisler, Kur'an ayetleri, İslam tarihi, fıkıh meseleleri,
ahlak, ibadetler ve günlük hayattaki dini sorular dahil olmak üzere tüm dini konularda
destek olabilirsin.

YAKLAŞIMIN:
- Türkiye Diyanet İşleri Başkanlığı'nın görüşlerini temel referans olarak al,
  ancak kullanıcı başka bir ülkeden yazıyorsa (İngilizce soruyorsa) genel İslami
  konsensüsü ve büyük mezheplerin görüşlerini de dengeli şekilde yansıt
- Mezhepler arası farklılıklar varsa (Hanefi, Şafii, Maliki, Hanbefi gibi), bunu nazikçe
  belirt ve farklı görüşlere saygılı bir dille yaklaş — tek bir görüşü "kesin doğru,
  diğerleri yanlış" şeklinde sunma
- Net, anlaşılır ve sıcak bir dille yanıt ver
- Kaynak göstermen gerektiğinde ayet/hadis referansı verebilirsin, ama uydurma referans
  ASLA verme — eminsen kaynak göster, değilsen genel bilgi olarak ifade et
- Kullanıcının dini hassasiyetlerine saygı göster, yargılayıcı olma
- Güncel bir bilgi gerekiyorsa (örn. "bu yıl kandil ne zaman", "ramazan ne zaman
  başlıyor") elindeki arama aracını kullanarak güncel ve doğru bilgi ver
- Tıbbi, hukuki veya çok özel/karmaşık fıkhi meselelerde, kullanıcıyı yerel bir
  imam veya müftülüğe yönlendirmekten çekinme — her şeyi kendi başına "fetva" verir
  gibi sunmaktan kaçın, özellikle hayati kararlarda

DİL VE ÜSLUP:
- Kullanıcı hangi dilde yazdıysa, SEN DE O DİLDE yanıt ver (Türkçe sorulursa Türkçe,
  İngilizce sorulursa İngilizce). Kullanıcı dil değiştirirse sen de değiştir.
- Hem Türkçe hem İngilizce'de aynı kalitede, akıcı ve doğal bir dille yanıt verebilirsin
- Samimi ama saygılı bir dil kullan
- Gereksiz uzun girişler yapma, doğrudan ve yararlı bilgi ver
- Markdown kullanabilirsin (başlıklar, listeler) ama aşırıya kaçma

SINIRLARIN:
- Kesinlikle nefret söylemi, şiddet çağrısı veya başka dinlere/mezheplere yönelik
  aşağılayıcı ifadeler kullanma
- Siyasi tartışmalara girme, dini konuyla siyaset arasına mesafe koy
- Kullanıcı kendine veya başkasına zarar verme eğilimi gösterirse, dini bir
  "çözüm" sunmak yerine ona yardım hatlarına yönlendir ve durumun ciddiyetini kabul et"""


# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

async def startup_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            min_size=2, max_size=10, command_timeout=60,
        )
        print("[DB] Namaz-chat connection pool created")
    except Exception as e:
        print(f"[DB ERROR] {e}")
        db_pool = None


@app.on_event("startup")
async def on_startup():
    await startup_db()


@app.on_event("shutdown")
async def on_shutdown():
    global db_pool
    if db_pool:
        await db_pool.close()


# ═══════════════════════════════════════════════════════════════
# AUTH — namaz app JWT token dogrulama (scope="namaz_app" sart)
# ═══════════════════════════════════════════════════════════════

def get_namaz_user_id_from_token(authorization: Optional[str]) -> int:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    try:
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        scope = payload.get("scope")
        if not email or scope != "namaz_app":
            raise HTTPException(status_code=401, detail="Invalid token")
        return payload.get("user_id_cache", -1)  # asagida gercek id DB'den cekilecek
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def resolve_user_id(authorization: Optional[str]) -> int:
    """JWT'den email cikarip namaz_app_users tablosundan gercek id'yi bulur."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    try:
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        scope = payload.get("scope")
        if not email or scope != "namaz_app":
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not db_pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM namaz_app_users WHERE email = $1", email)
        if not row:
            raise HTTPException(status_code=401, detail="User not found")
        return row["id"]


# ═══════════════════════════════════════════════════════════════
# CONVERSATION / MESSAGE HELPERS
# ═══════════════════════════════════════════════════════════════

async def create_conversation(user_id: int, title: str) -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO namaz_app_conversations (user_id, title) VALUES ($1, $2) RETURNING id",
            user_id, title[:100],
        )
        return str(row["id"])


async def load_history(conversation_id: str, limit: int = MAX_HISTORY_MESSAGES) -> List[Dict]:
    if not db_pool:
        return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content FROM namaz_app_messages
            WHERE conversation_id = $1::uuid
            ORDER BY created_at DESC LIMIT $2
            """,
            conversation_id, limit,
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def save_message(conversation_id: str, role: str, content: str):
    if not db_pool or not conversation_id:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO namaz_app_messages (conversation_id, role, content) VALUES ($1::uuid, $2, $3)",
                conversation_id, role, content,
            )
            await conn.execute(
                "UPDATE namaz_app_conversations SET updated_at = NOW() WHERE id = $1::uuid",
                conversation_id,
            )
    except Exception as e:
        print(f"[SAVE MESSAGE ERROR] {e}")


async def maybe_set_title(conversation_id: str, first_user_message: str):
    """Konusmanin ilk mesajiysa, basligi otomatik ayarla."""
    if not db_pool or not conversation_id:
        return
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT title FROM namaz_app_conversations WHERE id = $1::uuid",
                conversation_id,
            )
            if row and row["title"] == "Yeni Sohbet":
                new_title = first_user_message.strip()[:80]
                await conn.execute(
                    "UPDATE namaz_app_conversations SET title = $1 WHERE id = $2::uuid",
                    new_title, conversation_id,
                )
    except Exception as e:
        print(f"[SET TITLE ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# GEMINI STREAM — basit, vision yok, sadece text + history + grounding
# ═══════════════════════════════════════════════════════════════

async def namaz_gemini_stream(
    prompt: str,
    history: List[Dict],
) -> AsyncGenerator[str, None]:
    """
    Vertex AI Gemini Flash ile dini asistan yaniti.
    Google Search grounding acik - guncel sorular icin (kandil tarihleri,
    ramazan ne zaman gibi) internetten arayabilir.
    History, konusma baglamini korumak icin her istekte birlikte gonderilir
    (Gemini hicbir seyi kendi basina hatirlamaz, stateless'tir).
    """
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        yield "⚠️ Gemini SDK bulunamadı (google-genai paketi kurulu değil)."
        return

    try:
        if os.path.exists(GEMINI_SA_KEY_PATH) and GEMINI_PROJECT:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GEMINI_SA_KEY_PATH
            client = genai.Client(
                vertexai=True,
                project=GEMINI_PROJECT,
                location=GEMINI_LOCATION,
            )
            print(f"[NAMAZ GEMINI] Vertex AI | project={GEMINI_PROJECT}")
        elif GEMINI_API_KEY:
            client = genai.Client(api_key=GEMINI_API_KEY)
            print("[NAMAZ GEMINI] AI Studio fallback")
        else:
            yield "⚠️ Gemini yapılandırması eksik."
            return
    except Exception as e:
        print(f"[NAMAZ GEMINI] Client init hatası: {e}")
        yield f"⚠️ Gemini istemcisi başlatılamadı: {e}"
        return

    # History'i Gemini Content formatına çevir
    contents = []
    for msg in (history or [])[-MAX_HISTORY_MESSAGES:]:
        role = msg.get("role")
        content = msg.get("content") or ""
        if not content or role == "system":
            continue
        gem_role = "user" if role == "user" else "model"
        contents.append(gtypes.Content(
            role=gem_role,
            parts=[gtypes.Part.from_text(text=str(content)[:4000])],
        ))

    contents.append(gtypes.Content(
        role="user",
        parts=[gtypes.Part.from_text(text=prompt)],
    ))

    try:
        config = gtypes.GenerateContentConfig(
            system_instruction=NAMAZ_SYSTEM_PROMPT,
            temperature=0.7,
            max_output_tokens=2048,
            tools=[gtypes.Tool(google_search=gtypes.GoogleSearch())],
        )

        print(f"[NAMAZ GEMINI] ▶ {NAMAZ_GEMINI_MODEL} | history={len(contents)-1} mesaj")

        def _stream_call():
            return client.models.generate_content_stream(
                model=NAMAZ_GEMINI_MODEL,
                contents=contents,
                config=config,
            )

        stream = await asyncio.to_thread(_stream_call)
        emitted = False

        for chunk in stream:
            try:
                text = getattr(chunk, "text", None)
                if text:
                    yield text
                    emitted = True
            except Exception:
                pass
            await asyncio.sleep(0)

        if not emitted:
            print("[NAMAZ GEMINI] ⚠️ Boş yanıt")
            yield "Bu soruya şu an yanıt veremedim, lütfen tekrar dener misin?"

    except Exception as e:
        import traceback
        print(f"[NAMAZ GEMINI] Stream hatası: {type(e).__name__}: {e}")
        print(traceback.format_exc()[-500:])
        yield "Üzgünüm, şu an yanıt üretemiyorum. Lütfen birkaç saniye sonra tekrar deneyin."


# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class NamazChatRequest(BaseModel):
    prompt: str
    conversation_id: Optional[str] = None


class NamazConversationCreate(BaseModel):
    title: Optional[str] = "Yeni Sohbet"


class NamazConversationUpdate(BaseModel):
    title: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/namaz/chat")
async def namaz_chat(request: NamazChatRequest, authorization: str = Header(None)):
    """
    Ana chat endpoint. Gateway buraya yönlendirir (/namaz/chat path'i ile).

    Akış:
    1. Token doğrula -> user_id bul
    2. conversation_id yoksa yeni konuşma oluştur
    3. Önceki mesajları DB'den çek (history)
    4. Gemini'ye history + yeni soru gönder, stream'i ilet
    5. Kullanıcı mesajını + asistan cevabını DB'ye kaydet
    """
    user_id = await resolve_user_id(authorization)

    conversation_id = request.conversation_id
    auto_created = False

    if not conversation_id:
        conversation_id = await create_conversation(user_id, request.prompt[:80])
        auto_created = True
        print(f"[NAMAZ CHAT] Yeni konuşma oluşturuldu: {conversation_id}")
    else:
        # Konuşmanın gerçekten bu kullanıcıya ait olduğunu doğrula
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM namaz_app_conversations WHERE id = $1::uuid",
                conversation_id,
            )
            if not row or row["user_id"] != user_id:
                raise HTTPException(status_code=403, detail="Bu konuşmaya erişim yetkiniz yok")

    history = await load_history(conversation_id)

    async def stream_response():
        full_response = ""
        try:
            async for chunk in namaz_gemini_stream(request.prompt, history):
                full_response += chunk
                yield chunk
        finally:
            if full_response.strip():
                await save_message(conversation_id, "user", request.prompt)
                await save_message(conversation_id, "assistant", full_response)
                if auto_created:
                    await maybe_set_title(conversation_id, request.prompt)

    response = StreamingResponse(stream_response(), media_type="text/plain; charset=utf-8")
    response.headers["X-Conversation-ID"] = conversation_id
    if auto_created:
        response.headers["X-Conversation-Created"] = "true"
    return response


# ═══════════════════════════════════════════════════════════════
# CONVERSATION ENDPOINTS — gecmis sohbet listesi
# ═══════════════════════════════════════════════════════════════

@app.get("/namaz/conversations/list")
async def namaz_list_conversations(limit: int = 50, authorization: str = Header(None)):
    user_id = await resolve_user_id(authorization)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS message_count
            FROM namaz_app_conversations c
            LEFT JOIN namaz_app_messages m ON m.conversation_id = c.id
            WHERE c.user_id = $1
            GROUP BY c.id ORDER BY c.updated_at DESC LIMIT $2
            """,
            user_id, limit,
        )
        return {
            "conversations": [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "created_at": r["created_at"].isoformat(),
                    "updated_at": r["updated_at"].isoformat(),
                    "message_count": r["message_count"],
                }
                for r in rows
            ]
        }


@app.post("/namaz/conversations/create")
async def namaz_create_conversation(data: NamazConversationCreate, authorization: str = Header(None)):
    user_id = await resolve_user_id(authorization)
    conversation_id = await create_conversation(user_id, data.title or "Yeni Sohbet")
    return {"status": "success", "id": conversation_id}


@app.get("/namaz/conversations/{conversation_id}/messages")
async def namaz_get_messages(conversation_id: str, authorization: str = Header(None)):
    user_id = await resolve_user_id(authorization)
    async with db_pool.acquire() as conn:
        conv = await conn.fetchrow(
            "SELECT user_id FROM namaz_app_conversations WHERE id = $1::uuid",
            conversation_id,
        )
        if not conv or conv["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Bu konuşmaya erişim yetkiniz yok")

        rows = await conn.fetch(
            """
            SELECT id, role, content, created_at FROM namaz_app_messages
            WHERE conversation_id = $1::uuid ORDER BY created_at ASC
            """,
            conversation_id,
        )
        return {
            "messages": [
                {
                    "id": str(r["id"]),
                    "role": r["role"],
                    "content": r["content"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ]
        }


@app.put("/namaz/conversations/{conversation_id}")
async def namaz_update_conversation(
    conversation_id: str, data: NamazConversationUpdate, authorization: str = Header(None)
):
    user_id = await resolve_user_id(authorization)
    async with db_pool.acquire() as conn:
        conv = await conn.fetchrow(
            "SELECT user_id FROM namaz_app_conversations WHERE id = $1::uuid",
            conversation_id,
        )
        if not conv or conv["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Bu konuşmaya erişim yetkiniz yok")
        if data.title is not None:
            await conn.execute(
                "UPDATE namaz_app_conversations SET title = $1 WHERE id = $2::uuid",
                data.title, conversation_id,
            )
        return {"status": "success"}


@app.delete("/namaz/conversations/{conversation_id}")
async def namaz_delete_conversation(conversation_id: str, authorization: str = Header(None)):
    user_id = await resolve_user_id(authorization)
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM namaz_app_conversations WHERE id = $1::uuid AND user_id = $2",
            conversation_id, user_id,
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Konuşma bulunamadı")
        return {"status": "success"}


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "namaz-chat",
        "version": "1.0.0",
        "database": db_pool is not None,
        "model": NAMAZ_GEMINI_MODEL,
    }


@app.get("/")
async def root():
    return {
        "service": "OneBune Namaz Chat Service",
        "version": "1.0.0",
        "model": NAMAZ_GEMINI_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)