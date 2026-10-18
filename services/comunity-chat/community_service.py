"""
OneBune Community Chat Service
FastAPI + WebSocket + Redis + Vertex AI Moderasyon
Port: 8001
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from jose import jwt, JWTError

# ── Config ─────────────────────────────────────────────────────
SECRET_KEY    = os.getenv("JWT_SECRET", "onebune-secret")
DATABASE_URL  = os.getenv("COMMUNITY_DB_URL")
MAIN_DB_URL   = os.getenv("MAIN_DB_URL")
REDIS_URL     = os.getenv("REDIS_URL")
GEMINI_PROJECT  = os.getenv("GEMINI_PROJECT", "gen-lang-client-0907571701")
GEMINI_LOCATION = os.getenv("GEMINI_LOCATION", "us-central1")
GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_ROOM_SIZE = 1000
MSG_HISTORY   = 50

# Vertex AI init
vertexai.init(project=GEMINI_PROJECT, location=GEMINI_LOCATION)
ai_model = GenerativeModel(GEMINI_MODEL)

app = FastAPI(title="OneBune Community Chat")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db_pool: asyncpg.Pool = None
main_db_pool: asyncpg.Pool = None
redis_client: aioredis.Redis = None

@app.on_event("startup")
async def startup():
    global db_pool, main_db_pool, redis_client
    db_pool      = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    main_db_pool = await asyncpg.create_pool(MAIN_DB_URL, min_size=1, max_size=5)
    redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    asyncio.create_task(daily_cleanup())

async def daily_cleanup():
    """Her gece 00:00'da 24 saatten eski mesajları sil"""
    while True:
        now = datetime.utcnow()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((midnight - now).total_seconds())
        try:
            async with db_pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM community_messages WHERE created_at < NOW() - INTERVAL '24 hours'")
        except Exception:
            pass

@app.on_event("shutdown")
async def shutdown():
    await db_pool.close()
    await main_db_pool.close()
    await redis_client.close()

# ── JWT ────────────────────────────────────────────────────────
def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Geçersiz token")

# ── AI Moderasyon ──────────────────────────────────────────────
AI_PROMPT = """Sen OneBune İslami sohbet uygulamasının moderatörüsün.
Türkçe mesajları analiz et ve SADECE JSON formatında yanıtla.

Mesaj: "{message}"
Oda: "{room_name}"

Yanıt (sadece JSON):
{{"safe": true/false, "action": "none|warn|delete|mute|ban", "reason": "kısa açıklama", "user_message": "kullanıcıya Türkçe mesaj"}}

Kurallar:
- Küfür/hakaret → delete + mute (10 dk)
- Ağır tehdit → ban (1 gün)
- Spam → delete + warn
- Konu dışı → warn
- Normal → none"""

async def ai_moderate(message: str, room_name: str) -> dict:
    try:
        prompt   = AI_PROMPT.format(message=message[:500], room_name=room_name)
        response = await asyncio.to_thread(
            ai_model.generate_content,
            prompt,
            generation_config=GenerationConfig(temperature=0.1, max_output_tokens=200)
        )
        text = response.text.strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        return json.loads(text)
    except Exception:
        return {"safe": True, "action": "none", "reason": "", "user_message": ""}

# ── Connection Manager ─────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.rooms: dict[int, dict[int, WebSocket]] = {}

    async def connect(self, ws: WebSocket, room_id: int, user_id: int):
        await ws.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][user_id] = ws

    def disconnect(self, room_id: int, user_id: int):
        if room_id in self.rooms:
            self.rooms[room_id].pop(user_id, None)

    async def broadcast(self, room_id: int, message: dict, exclude: int = None):
        if room_id not in self.rooms:
            return
        dead = []
        for uid, ws in self.rooms[room_id].items():
            if uid == exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.rooms[room_id].pop(uid, None)

    async def send_to(self, room_id: int, user_id: int, message: dict):
        ws = self.rooms.get(room_id, {}).get(user_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    def room_size(self, room_id: int) -> int:
        return len(self.rooms.get(room_id, {}))

manager = ConnectionManager()

# ── DB Yardımcılar ─────────────────────────────────────────────
async def get_room(slug: str) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM community_rooms WHERE slug=$1 AND is_active=TRUE", slug)
        return dict(row) if row else None

async def is_banned(user_id: int, room_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id FROM community_bans
            WHERE user_id=$1 AND (room_id=$2 OR room_id IS NULL)
            AND (banned_until IS NULL OR banned_until > NOW()) AND ban_type='ban'
        """, user_id, room_id)
        return row is not None

async def is_muted(user_id: int, room_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id FROM community_bans
            WHERE user_id=$1 AND (room_id=$2 OR room_id IS NULL)
            AND banned_until > NOW() AND ban_type='mute'
        """, user_id, room_id)
        return row is not None

async def get_history(room_id: int) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, user_id, username, content, created_at, reply_to_id
            FROM community_messages
            WHERE room_id=$1 AND is_deleted=FALSE
            ORDER BY created_at DESC LIMIT $2
        """, room_id, MSG_HISTORY)
        return [dict(r) for r in reversed(rows)]

async def save_message(room_id, user_id, username, content, reply_to=None) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO community_messages (room_id, user_id, username, content, reply_to_id)
            VALUES ($1,$2,$3,$4,$5) RETURNING id
        """, room_id, user_id, username, content, reply_to)
        return row["id"]

async def delete_message(msg_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE community_messages SET is_deleted=TRUE, deleted_by='ai' WHERE id=$1", msg_id)

async def apply_ban(user_id, room_id, ban_type, reason, duration_min=None):
    banned_until = datetime.utcnow() + timedelta(minutes=duration_min) if duration_min else None
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO community_bans (user_id, room_id, ban_type, reason, banned_until)
            VALUES ($1,$2,$3,$4,$5)
        """, user_id, room_id, ban_type, reason, banned_until)

# ── Redis Presence ─────────────────────────────────────────────
async def set_presence(user_id: int, room_id: int, username: str):
    key = f"presence:{user_id}"
    old_room = await redis_client.hget(key, "room_id")
    if old_room and old_room != str(room_id):
        await redis_client.srem(f"room:members:{old_room}", user_id)
        await manager.broadcast(int(old_room), {
            "type": "presence", "user_id": user_id,
            "username": username, "status": "left",
            "room_count": await redis_client.scard(f"room:members:{old_room}")
        })
    await redis_client.hset(key, mapping={"room_id": room_id, "username": username, "joined_at": str(time.time())})
    await redis_client.expire(key, 3600)
    await redis_client.sadd(f"room:members:{room_id}", user_id)

async def remove_presence(user_id: int, room_id: int, username: str):
    await redis_client.delete(f"presence:{user_id}")
    await redis_client.srem(f"room:members:{room_id}", user_id)
    count = await redis_client.scard(f"room:members:{room_id}")
    await manager.broadcast(room_id, {
        "type": "presence", "user_id": user_id,
        "username": username, "status": "left", "room_count": count
    })

# ── WebSocket ──────────────────────────────────────────────────
@app.websocket("/ws/{room_slug}")
async def websocket_endpoint(ws: WebSocket, room_slug: str, token: str):
    try:
        payload = verify_token(token)
        email   = payload.get("sub", "")
    except Exception:
        await ws.close(code=4001, reason="Geçersiz token")
        return

    # DB'den kullanıcıyı bul (ana DB)
    async with main_db_pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, name, is_premium FROM namaz_app_users WHERE email=$1", email)
    if not user_row:
        await ws.close(code=4002, reason="Kullanıcı bulunamadı")
        return

    user_id    = user_row["id"]
    username   = user_row["name"] or "Kullanıcı"
    is_premium = user_row["is_premium"]

    if not is_premium:
        async with main_db_pool.acquire() as conn:
            sub_row = await conn.fetchrow(
                "SELECT id FROM namaz_app_subscriptions WHERE user_id=$1 AND status='active' AND (current_period_end IS NULL OR current_period_end > NOW())",
                user_id)
            if sub_row:
                is_premium = True

    if not is_premium:
        await ws.close(code=4003, reason="Premium gerekli")
        return

    room = await get_room(room_slug)
    if not room:
        await ws.close(code=4004, reason="Oda bulunamadı")
        return
    room_id = room["id"]

    if await is_banned(user_id, room_id):
        await ws.close(code=4005, reason="Yasaklandınız")
        return

    if manager.room_size(room_id) >= MAX_ROOM_SIZE:
        await ws.close(code=4006, reason="Oda dolu")
        return

    await manager.connect(ws, room_id, user_id)
    await set_presence(user_id, room_id, username)
    count = await redis_client.scard(f"room:members:{room_id}")

    # Geçmiş mesajlar
    history = await get_history(room_id)
    await ws.send_json({
        "type": "history",
        "messages": [{
            "id": str(m["id"]),
            "user_id": m["user_id"],
            "username": m["username"],
            "content": m["content"],
            "created_at": m["created_at"].isoformat(),
            "reply_to_id": str(m["reply_to_id"]) if m["reply_to_id"] else None,
        } for m in history],
        "room": {"id": room_id, "name": room["name"], "icon": room["icon"]},
        "room_count": count
    })

    await manager.broadcast(room_id, {
        "type": "presence", "user_id": user_id,
        "username": username, "status": "joined", "room_count": count
    }, exclude=user_id)

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") != "message":
                continue

            content  = data.get("content", "").strip()
            reply_to = data.get("reply_to_id")

            if not content or len(content) > 500:
                continue

            if await is_muted(user_id, room_id):
                await ws.send_json({"type": "error", "message": "⚠️ Susturuldunuz."})
                continue

            # Spam kontrolü
            spam_key = f"lastmsg:{user_id}:{room_id}"
            if await redis_client.get(spam_key) == content:
                await ws.send_json({"type": "warn", "message": "⚠️ Aynı mesajı tekrar göndermeyin."})
                continue
            await redis_client.setex(spam_key, 5, content)

            msg_id = await save_message(room_id, user_id, username, content, reply_to)

            await manager.broadcast(room_id, {
                "type": "message",
                "id": str(msg_id),
                "user_id": user_id,
                "username": username,
                "content": content,
                "created_at": datetime.utcnow().isoformat(),
                "reply_to_id": reply_to,
            })

            asyncio.create_task(
                ai_act(msg_id, content, room["name"], user_id, username, room_id)
            )

    except WebSocketDisconnect:
        manager.disconnect(room_id, user_id)
        await remove_presence(user_id, room_id, username)

# ── AI Aksiyon ─────────────────────────────────────────────────
async def ai_act(msg_id, content, room_name, user_id, username, room_id):
    result = await ai_moderate(content, room_name)
    action = result.get("action", "none")
    if action == "none":
        return

    user_msg = result.get("user_message", "")
    reason   = result.get("reason", "")

    if action in ("delete", "mute", "ban"):
        await delete_message(msg_id)
        await manager.broadcast(room_id, {"type": "message_deleted", "id": str(msg_id)})

    if action == "warn":
        await manager.send_to(room_id, user_id, {"type": "warn", "message": f"⚠️ {user_msg}"})

    elif action == "mute":
        await apply_ban(user_id, room_id, "mute", reason, 10)
        await manager.send_to(room_id, user_id, {"type": "muted", "message": f"🔇 10 dakika susturuldunuz. {user_msg}"})

    elif action == "ban":
        await apply_ban(user_id, room_id, "ban", reason, 60*24)
        await manager.send_to(room_id, user_id, {"type": "banned", "message": f"🚫 24 saat yasaklandınız. {user_msg}"})

# ── REST ───────────────────────────────────────────────────────
@app.get("/rooms")
async def get_rooms():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, slug, name, description, icon FROM community_rooms WHERE is_active=TRUE ORDER BY order_index")
        rooms = []
        for r in rows:
            d = dict(r)
            d["online_count"] = await redis_client.scard(f"room:members:{r['id']}")
            rooms.append(d)
        return {"rooms": rooms}

@app.get("/rooms/{slug}/messages")
async def room_messages(slug: str):
    room = await get_room(slug)
    if not room:
        raise HTTPException(404, "Oda bulunamadı")
    return {"messages": await get_history(room["id"])}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "community-chat"}

@app.get("/online")
async def get_online_users():
    """Tüm odalardaki online kullanıcıları döndür"""
    async with db_pool.acquire() as conn:
        rooms = await conn.fetch(
            "SELECT id, slug, name, icon FROM community_rooms WHERE is_active=TRUE ORDER BY order_index")

    result = []
    for room in rooms:
        room_id = room["id"]
        member_ids = await redis_client.smembers(f"room:members:{room_id}")
        users = []
        for uid_str in member_ids:
            key = f"presence:{uid_str}"
            username = await redis_client.hget(key, "username")
            if username:
                users.append({"user_id": int(uid_str), "username": username})
        result.append({
            "room_id": room_id,
            "room_slug": room["slug"],
            "room_name": room["name"],
            "room_icon": room["icon"],
            "users": users,
            "count": len(users),
        })

    total = sum(r["count"] for r in result)
    return {"total_online": total, "rooms": result}

# ── Rüya Tabiri ───────────────────────────────────────────────
RUYA_PROMPT = """Sen deneyimli bir İslami rüya tabircisisin. İbn-i Sirin, Nablusi ve Cafer-i Sadık'ın eserlerine dayanarak yorum yaparsın.

Rüya: "{dream}"

Şu formatta Türkçe yorum yap:

1. GENEL YORUM: Rüyanın genel anlamını açıkla
2. SEMBOLLER: Rüyadaki her önemli sembolü tek tek açıkla
3. KAYNAK: Varsa Kuran ayeti veya hadis referansı ver
4. TAVSİYE: Rüyayı gören kişiye tavsiye ver
5. Sonunda mutlaka şunu yaz: "En doğrusunu Allah bilir."

Olumlu ve umut verici bir dil kullan. Yanıtı mutlaka tamamla, yarıda bırakma."""

from pydantic import BaseModel

class RuyaRequest(BaseModel):
    dream: str

class QuizRequest(BaseModel):
    category: str = "Genel"
    exclude: str = ""

QUIZ_PROMPT = """Sen İslami bilgi yarışması sorusu üreten bir AI'sın.
Kategori: {category}

Bir adet 4 şıklı Türkçe soru üret. Sadece JSON döndür, başka bir şey yazma.
Tırnak işareti kullanma, düz metin yaz.

Tam olarak bu formatta yaz:
{{"question": "soru metni buraya", "options": ["A sıkkı", "B sıkkı", "C sıkkı", "D sıkkı"], "correct_index": 0, "explanation": "kisa aciklama"}}
"""

@app.post("/quiz")
async def quiz(req: QuizRequest):
    last_error = ""
    for attempt in range(3):
        try:
            exclude_note = ""
            if req.exclude:
                exclude_note = f"\nBu soruları SORMA (zaten soruldu): {req.exclude}"
            prompt = QUIZ_PROMPT.format(category=req.category) + exclude_note
            response = await asyncio.to_thread(
                ai_model.generate_content,
                prompt,
                generation_config=GenerationConfig(temperature=0.9, max_output_tokens=500)
            )
            text = response.text.strip()
            if "```" in text:
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else parts[0]
                text = text.replace("json", "").strip()
            text = " ".join(text.split())
            last_brace = text.rfind("}")
            if last_brace > 0:
                text = text[:last_brace + 1]
            first_brace = text.find("{")
            if first_brace > 0:
                text = text[first_brace:]
            parsed = json.loads(text)
            if "question" in parsed and "options" in parsed and len(parsed["options"]) == 4:
                return parsed
        except Exception as e:
            last_error = str(e)
            continue
    raise HTTPException(503, f"Quiz oluşturulamadı: {last_error}")

class QuizBatchRequest(BaseModel):
    category: str = "Genel"
    count: int = 10

QUIZ_BATCH_PROMPT = """Sen İslami bilgi yarışması sorusu üreten bir AI'sın.
Kategori: {category}

{count} adet birbirinden FARKLI 4 şıklı Türkçe soru üret.
Sadece JSON array döndür, başka bir şey yazma.
Her soru farklı konuda olsun, tekrar etmesin.

Tam olarak bu formatta yaz:
[{{"question": "soru 1", "options": ["A", "B", "C", "D"], "correct_index": 0, "explanation": "aciklama"}}, {{"question": "soru 2", "options": ["A", "B", "C", "D"], "correct_index": 1, "explanation": "aciklama"}}]
"""

@app.post("/quiz/batch")
async def quiz_batch(req: QuizBatchRequest):
    count = min(req.count, 40)
    all_questions = []
    batch_size = min(count, 10)  # Gemini'ye max 10'ar sor
    
    remaining = count
    while remaining > 0:
        current_batch = min(remaining, batch_size)
        for attempt in range(3):
            try:
                prompt = QUIZ_BATCH_PROMPT.format(category=req.category, count=current_batch)
                response = await asyncio.to_thread(
                    ai_model.generate_content,
                    prompt,
                    generation_config=GenerationConfig(temperature=0.9, max_output_tokens=4000)
                )
                text = response.text.strip()
                if "```" in text:
                    parts = text.split("```")
                    text = parts[1] if len(parts) > 1 else parts[0]
                    text = text.replace("json", "").strip()
                text = " ".join(text.split())
                # Son ] bul
                last_bracket = text.rfind("]")
                if last_bracket > 0:
                    text = text[:last_bracket + 1]
                first_bracket = text.find("[")
                if first_bracket > 0:
                    text = text[first_bracket:]
                
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    valid = [q for q in parsed if "question" in q and "options" in q and len(q.get("options",[])) == 4]
                    all_questions.extend(valid)
                    remaining -= len(valid)
                    break
            except Exception:
                continue
        else:
            break  # 3 deneme de başarısız
    
    return {"questions": all_questions[:count], "total": len(all_questions[:count])}


@app.post("/ruya-tabiri")
async def ruya_tabiri(req: RuyaRequest):
    if len(req.dream) < 10:
        raise HTTPException(400, "Rüya çok kısa")
    if len(req.dream) > 1000:
        raise HTTPException(400, "Rüya çok uzun")

    try:
        prompt = RUYA_PROMPT.format(dream=req.dream[:1000])
        response = await asyncio.to_thread(
            ai_model.generate_content,
            prompt,
            generation_config=GenerationConfig(temperature=0.7, max_output_tokens=4096)
        )
        return {"interpretation": response.text.strip()}
    except Exception as e:
        raise HTTPException(503, f"Tabir yapılamadı: {str(e)}")


# ── Kişisel Dua Üretici ──────────────────────────────────────
class DuaRequest(BaseModel):
    konu: str

DUA_PROMPT = """Sen İslami dua uzmanısın. Kullanıcının durumuna uygun Kuran ve hadislerden dua öner.

Konu: "{konu}"

Şu formatta Türkçe yanıt ver:

1. Konuya uygun 1-2 dua öner
2. Her dua için:
   - Arapça yazılışı
   - Türkçe okunuşu
   - Türkçe anlamı
   - Kaynağı (Kuran sure:ayet veya Hadis kaynağı)
3. Duanın ne zaman okunacağını açıkla
4. Yanıtı mutlaka tamamla, yarıda bırakma"""

@app.post("/dua-uret")
async def dua_uret(req: DuaRequest):
    if len(req.konu) < 3:
        raise HTTPException(400, "Konu çok kısa")
    try:
        prompt = DUA_PROMPT.format(konu=req.konu[:500])
        response = await asyncio.to_thread(
            ai_model.generate_content,
            prompt,
            generation_config=GenerationConfig(temperature=0.7, max_output_tokens=4096)
        )
        return {"dua": response.text.strip()}
    except Exception as e:
        raise HTTPException(503, f"Dua oluşturulamadı: {str(e)}")

# ── AI Manevi Koç ─────────────────────────────────────────────
class ManeviKocRequest(BaseModel):
    stats: dict = {}

@app.post("/manevi-koc")
async def manevi_koc(req: ManeviKocRequest):
    stats = req.stats
    gun = stats.get("gun", "")
    tarih = stats.get("tarih", "")
    toplam_zikir = stats.get("toplam_zikir", 0)
    quiz_skor = stats.get("quiz_skor", 0)

    prompt = f"""Sen kişisel bir İslami manevi koçsun. Kullanıcıya günlük rehberlik yapıyorsun.

Bugün: {tarih} ({gun})
Kullanıcı istatistikleri:
- Toplam zikir: {toplam_zikir}
- Quiz en iyi seri: {quiz_skor}

Şu JSON formatında yanıt ver (sadece JSON, başka bir şey yazma):
{{"motivasyon": "Bugüne özel 2-3 cümlelik kişiselleştirilmiş selamlama ve motivasyon mesajı. Cuma ise Cumaya özel, Pazartesi ise haftaya başlangıç vs.", "ayet": "Bugüne uygun bir Kuran ayeti (sure adı ve ayet numarası ile birlikte). Arapça + Türkçe meali.", "dua": "Bugüne uygun kısa bir dua. Arapça okunuşu + Türkçe anlamı.", "hedefler": "Bugün için 3-4 maddelik pratik ibadet hedefi listesi (emoji ile). Kullanıcının zikir ve quiz istatistiklerine göre kişiselleştir.", "mesaj": "2-3 cümlelik genel tavsiye veya hatırlatma. Hadis referansı ile destekle."}}

Kurallar:
- Samimi, sıcak, motive edici ol
- Günün özelliğine göre içerik üret (Cuma: Kehf suresi, Pazartesi: yeni başlangıç vs.)
- İstatistiklere göre kişiselleştir
- Her seferinde farklı ayet ve dua seç
- Sadece JSON döndür"""

    try:
        response = await asyncio.to_thread(
            ai_model.generate_content,
            prompt,
            generation_config=GenerationConfig(temperature=0.9, max_output_tokens=2048)
        )
        text = response.text.strip()
        if "```" in text:
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else parts[0]
            text = text.replace("json", "").strip()
        text = text.strip()
        result = json.loads(text)
        return result
    except json.JSONDecodeError:
        return {
            "motivasyon": "Hayırlı günler! Bugün de Allah'a yakınlaşmak için güzel bir fırsat.",
            "ayet": "\"Şüphesiz her güçlükle bir kolaylık vardır.\" (İnşirah, 94:6)",
            "dua": "Allahümme inni es'elüke ilmen nafia - Allah'ım senden faydalı ilim isterim.",
            "hedefler": "🕌 5 vakit namazı kaçırma\n📿 100 zikir çek\n📖 1 sayfa Kuran oku\n🤲 Sabah-akşam dualarını oku",
            "mesaj": "Hz. Peygamber (s.a.v.) buyurdu: 'Allah'a en sevimli amel, az da olsa devamlı olanıdır.' (Buhari)"
        }
    except Exception as e:
        raise HTTPException(503, f"Manevi koç hatası: {str(e)}")


# ── AI Arapça Kamera (Gemini Multimodal) ──────────────────────
import base64
from vertexai.generative_models import Part, Image as VertexImage

class ArapceKameraRequest(BaseModel):
    image_base64: str

@app.post("/arapca-kamera")
async def arapca_kamera(req: ArapceKameraRequest):
    if not req.image_base64:
        raise HTTPException(400, "Resim boş")
    if len(req.image_base64) > 10_000_000:
        raise HTTPException(400, "Resim çok büyük (max 7MB)")

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = Part.from_data(data=image_bytes, mime_type="image/jpeg")

        prompt = """Bu resimdeki Arapça metni oku ve analiz et.

Şu JSON formatında Türkçe yanıt ver (sadece JSON, başka bir şey yazma):
{"arapca": "Resimdeki Arapça metnin tam yazılışı (Arapça harflerle)", "okunusu": "Metnin Türkçe harflerle okunuşu (transliterasyon)", "ceviri": "Metnin Türkçe çevirisi/meali", "aciklama": "Bu metin hakkında kısa açıklama: Kuran ayeti mi, hadis mi, dua mı? Hangi sureden, hangi bağlamda? 2-3 cümle."}

Kurallar:
- Eğer Kuran ayeti ise sure adı ve ayet numarasını belirt
- Eğer hadis ise kaynağını belirt
- Eğer dua ise ne zaman okunduğunu belirt
- Okunuşu Türkçe'ye uygun yaz (örn: Bismillahirrahmanirrahim)
- Resimde Arapça metin yoksa: {"arapca": "", "okunusu": "", "ceviri": "", "aciklama": "Resimde Arapça metin bulunamadı."}
- Sadece JSON döndür"""

        response = await asyncio.to_thread(
            ai_model.generate_content,
            [image_part, prompt],
            generation_config=GenerationConfig(temperature=0.3, max_output_tokens=1024)
        )
        text = response.text.strip()
        if "```" in text:
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else parts[0]
            text = text.replace("json", "").strip()
        text = text.strip()
        result = json.loads(text)
        return result
    except json.JSONDecodeError:
        return {
            "arapca": "",
            "okunusu": "",
            "ceviri": "Metin okunamadı. Lütfen daha net bir fotoğraf çekin.",
            "aciklama": ""
        }
    except Exception as e:
        raise HTTPException(503, f"Kamera hatası: {str(e)}")


# ══════════════════════════════════════════════════════════════
# ── Google Play Abonelik Doğrulama ────────────────────────────
# Uygulama satın alma token'ını gönderir → Google Play Developer API'ye
# sorulur → gerçekse namaz_app_subscriptions tablosuna yazılır.
# Servis hesabı: GOOGLE_APPLICATION_CREDENTIALS (/secrets/key.json)
# Play Console'da bu hesaba "Finansal verileri görüntüleme" +
# "Siparişleri ve abonelikleri yönetme" izni verilmiş olmalı.
# ══════════════════════════════════════════════════════════════
import re as _re
import urllib.parse as _urlparse
from datetime import timezone as _tz
from fastapi import Header

PLAY_PACKAGE_NAME = os.getenv("PLAY_PACKAGE_NAME", "com.onebune.namazvakitleri")
PLAY_CREDENTIALS  = os.getenv("PLAY_CREDENTIALS_FILE",
                              os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/key.json"))
PLAY_PRODUCTS = {
    "namaz_premium_monthly": "monthly",
    "namaz_premium_yearly":  "yearly",
}
PLAY_ACTIVE_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    "SUBSCRIPTION_STATE_CANCELED",   # iptal edildi ama dönem sonuna kadar geçerli
}
_play_creds = None


def _play_access_token() -> str:
    global _play_creds
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as _GRequest
    if _play_creds is None:
        _play_creds = service_account.Credentials.from_service_account_file(
            PLAY_CREDENTIALS, scopes=["https://www.googleapis.com/auth/androidpublisher"])
    if not _play_creds.valid:
        _play_creds.refresh(_GRequest())
    return _play_creds.token


def _play_get_subscription(purchase_token: str) -> dict:
    import requests
    url = ("https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
           f"{PLAY_PACKAGE_NAME}/purchases/subscriptionsv2/tokens/"
           f"{_urlparse.quote(purchase_token, safe='')}")
    r = requests.get(url, headers={"Authorization": f"Bearer {_play_access_token()}"}, timeout=15)
    if r.status_code in (400, 404, 410):
        print(f"[PLAY] Geçersiz token ({r.status_code}): {r.text[:300]}")
        raise HTTPException(400, "Geçersiz satın alma")
    if r.status_code in (401, 403):
        print(f"[PLAY] Yetki hatası ({r.status_code}): {r.text[:300]}")
        raise HTTPException(503, "Satın alma şu an doğrulanamıyor")
    r.raise_for_status()
    return r.json()


def _play_parse_time(value: str):
    if not value:
        return None
    # "2026-10-28T20:15:30.123456789Z" → mikro saniye fazlasını at
    value = _re.sub(r"\.(\d{6})\d+", r".\1", value.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _play_parse_subscription(data: dict) -> dict:
    state = data.get("subscriptionState", "")
    items = data.get("lineItems") or []
    product_id, expiry = "", None
    for item in items:
        t = _play_parse_time(item.get("expiryTime", ""))
        if t and (expiry is None or t > expiry):
            expiry = t
            product_id = item.get("productId", "")
    now = datetime.now(_tz.utc)
    is_active = state in PLAY_ACTIVE_STATES and expiry is not None and expiry > now
    return {
        "state": state,
        "product_id": product_id,
        "expiry": expiry,
        "is_active": is_active,
        "is_test": "testPurchase" in data,
    }


def _play_email_from_auth(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Giriş gerekli")
    payload = verify_token(authorization[7:])
    if payload.get("scope") != "namaz_app" or not payload.get("sub"):
        raise HTTPException(401, "Geçersiz token")
    return payload["sub"]


async def _play_user_id(email: str) -> int:
    async with main_db_pool.acquire() as conn:
        user_id = await conn.fetchval("SELECT id FROM namaz_app_users WHERE email=$1", email)
    if not user_id:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    return user_id


async def _play_save(user_id: int, purchase_token: str, info: dict):
    period = PLAY_PRODUCTS.get(info["product_id"], "monthly")
    meta = json.dumps({
        "source": "google_play",
        "purchase_token": purchase_token,
        "product_id": info["product_id"],
        "state": info["state"],
        "test": info["is_test"],
    })
    short_token = purchase_token[:255]

    async with main_db_pool.acquire() as conn:
        async with conn.transaction():
            # Aynı satın alma başka bir hesapta aktif mi? (hesap paylaşımını engeller)
            other = await conn.fetchval("""
                SELECT user_id FROM namaz_app_subscriptions
                WHERE metadata->>'purchase_token' = $1 AND user_id <> $2
                  AND status IN ('active','trialing')
                LIMIT 1
            """, purchase_token, user_id)
            if other:
                raise HTTPException(409, "Bu satın alma başka bir hesaba bağlı")

            existing = await conn.fetchrow("""
                SELECT id, plan_id FROM namaz_app_subscriptions
                WHERE user_id = $1 AND status IN ('active','trialing')
                LIMIT 1
            """, user_id)

            if info["is_active"]:
                if existing and existing["plan_id"] == "lifetime":
                    return  # ömür boyu / test hesabına dokunma
                if existing:
                    await conn.execute("""
                        UPDATE namaz_app_subscriptions
                        SET plan_id='premium', billing_period=$2, current_period_end=$3,
                            google_play_token=$4, metadata=$5::jsonb
                        WHERE id=$1
                    """, existing["id"], period, info["expiry"], short_token, meta)
                else:
                    await conn.execute("""
                        INSERT INTO namaz_app_subscriptions
                            (user_id, plan_id, status, billing_period,
                             current_period_start, current_period_end, google_play_token, metadata)
                        VALUES ($1, 'premium', 'active', $2, NOW(), $3, $4, $5::jsonb)
                    """, user_id, period, info["expiry"], short_token, meta)
            else:
                # Süresi dolmuş / iade edilmiş → premium kapat
                await conn.execute("""
                    UPDATE namaz_app_subscriptions
                    SET status='expired', metadata=$2::jsonb
                    WHERE user_id=$1 AND status IN ('active','trialing')
                      AND metadata->>'purchase_token' = $3
                """, user_id, meta, purchase_token)


async def _play_verify_and_save(user_id: int, purchase_token: str, expected_product: str = "") -> dict:
    data = await asyncio.to_thread(_play_get_subscription, purchase_token)
    info = _play_parse_subscription(data)
    if info["product_id"] not in PLAY_PRODUCTS:
        raise HTTPException(400, "Bilinmeyen ürün")
    if expected_product and expected_product != info["product_id"]:
        raise HTTPException(400, "Ürün uyuşmuyor")
    await _play_save(user_id, purchase_token, info)
    print(f"[PLAY] user={user_id} product={info['product_id']} state={info['state']} "
          f"active={info['is_active']} test={info['is_test']}")
    return {
        "is_premium": info["is_active"],
        "product_id": info["product_id"],
        "state": info["state"],
        "expiry": info["expiry"].isoformat() if info["expiry"] else None,
    }


class PlayVerifyRequest(BaseModel):
    purchase_token: str
    product_id: str = ""


@app.post("/subscription/verify")
async def play_subscription_verify(req: PlayVerifyRequest, authorization: Optional[str] = Header(None)):
    """Satın alma sonrası (ve uygulama açılışında) çağrılır."""
    email = _play_email_from_auth(authorization)
    if len(req.purchase_token) < 10:
        raise HTTPException(400, "Geçersiz satın alma")
    user_id = await _play_user_id(email)
    return await _play_verify_and_save(user_id, req.purchase_token, req.product_id)


@app.post("/subscription/sync")
async def play_subscription_sync(authorization: Optional[str] = Header(None)):
    """Kayıtlı Google Play aboneliğini Google'a tekrar sorar (yenileme / iptal / iade)."""
    email = _play_email_from_auth(authorization)
    user_id = await _play_user_id(email)
    async with main_db_pool.acquire() as conn:
        token = await conn.fetchval("""
            SELECT metadata->>'purchase_token' FROM namaz_app_subscriptions
            WHERE user_id=$1 AND metadata->>'source'='google_play'
            ORDER BY updated_at DESC LIMIT 1
        """, user_id)
    if not token:
        return {"synced": False, "is_premium": None}
    result = await _play_verify_and_save(user_id, token)
    result["synced"] = True
    return result


# ══════════════════════════════════════════════════════════════
# ── Yakındaki Camiler (Overpass proxy + Redis önbellek) ───────
# Uygulama camileri doğrudan Overpass'tan çekiyordu; bu sunucular
# sık sık 429/5xx veriyor. Sunucu tarafında:
#   - ~1 km'lik ızgaraya göre 7 gün önbellek (camiler yer değiştirmez)
#   - tüm Overpass sunucuları hata verirse son başarılı sonuç (stale)
# Yanıt Overpass ile aynı biçimde: {"elements": [...]}
# ══════════════════════════════════════════════════════════════
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_UA = "DuaNamazApp/1.2 (info@duavenamaz.com)"
MOSQUE_CACHE_TTL = 7 * 24 * 3600


_overpass_down_until = 0.0   # tüm sunucular çökünce 10 dk doğrudan Nominatim'e geç


def _overpass_fetch(lat: float, lng: float, radius: int) -> Optional[dict]:
    global _overpass_down_until
    import requests
    if time.time() < _overpass_down_until:
        return _nominatim_fetch(lat, lng, radius)
    query = f"""
[out:json][timeout:25];
(
  node["amenity"="place_of_worship"]["religion"="muslim"](around:{radius},{lat},{lng});
  way["amenity"="place_of_worship"]["religion"="muslim"](around:{radius},{lat},{lng});
);
out center 30;
"""
    started = time.time()
    for mirror in OVERPASS_MIRRORS:
        # Uygulama ~35 sn bekliyor → Overpass için en fazla ~15 sn harca, sonra Nominatim
        if time.time() - started > 15:
            print("[MOSQUE] Overpass süre sınırı doldu → Nominatim")
            break
        try:
            r = requests.post(mirror, data={"data": query},
                              headers={"User-Agent": OVERPASS_UA}, timeout=(4, 8))
            if r.status_code != 200:
                print(f"[MOSQUE] {mirror} → HTTP {r.status_code}")
                continue
            data = r.json()
            if isinstance(data.get("elements"), list):
                return {"elements": data["elements"]}
        except Exception as e:
            print(f"[MOSQUE] {mirror} → {e}")
    _overpass_down_until = time.time() + 600
    print("[MOSQUE] Overpass çökük → 10 dk Nominatim kullanılacak")
    return _nominatim_fetch(lat, lng, radius)


def _nominatim_fetch(lat: float, lng: float, radius: int) -> Optional[dict]:
    """Overpass çökükse yedek: OSM Nominatim (ayrı altyapı, anahtar gerekmez).
    Kullanım kuralı: User-Agent zorunlu, saniyede en fazla 1 istek → önbellek bunu sağlıyor."""
    import math
    import requests
    dlat = radius / 111000.0
    dlng = radius / (111000.0 * max(math.cos(math.radians(lat)), 0.01))
    params = {
        "q": "mosque", "format": "jsonv2", "limit": 30, "bounded": 1,
        "viewbox": f"{lng - dlng},{lat + dlat},{lng + dlng},{lat - dlat}",
    }
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", params=params,
                         headers={"User-Agent": OVERPASS_UA, "Accept-Language": "tr"}, timeout=20)
        if r.status_code != 200:
            print(f"[MOSQUE] nominatim → HTTP {r.status_code}")
            return None
        elements = []
        for p in r.json():
            try:
                name = (p.get("name") or "").strip() or (p.get("display_name") or "Cami").split(",")[0]
                elements.append({"type": "node", "lat": float(p["lat"]), "lon": float(p["lon"]),
                                 "tags": {"name": name}})
            except Exception:
                continue
        print(f"[MOSQUE] nominatim → {len(elements)} cami")
        return {"elements": elements}
    except Exception as e:
        print(f"[MOSQUE] nominatim → {e}")
        return None


GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")


def _places_fetch(lat: float, lng: float, radius: int) -> Optional[dict]:
    """Google Places API (New) Nearby Search — type=mosque. Anahtar yoksa atlanır."""
    if not GOOGLE_PLACES_API_KEY:
        return None
    import requests
    body = {
        "includedTypes": ["mosque"],
        "maxResultCount": 20,
        "rankPreference": "DISTANCE",
        "languageCode": "tr",
        "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                           "radius": float(min(radius, 50000))}},
    }
    try:
        r = requests.post("https://places.googleapis.com/v1/places:searchNearby", json=body,
                          headers={"X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                                   "X-Goog-FieldMask": "places.id,places.displayName,places.location"},
                          timeout=(4, 10))
        if r.status_code != 200:
            print(f"[MOSQUE] places → HTTP {r.status_code}: {r.text[:200]}")
            return None
        elements = []
        for p in r.json().get("places", []):
            loc = p.get("location") or {}
            if "latitude" not in loc:
                continue
            name = (p.get("displayName") or {}).get("text") or "Cami"
            elements.append({"type": "node", "lat": loc["latitude"], "lon": loc["longitude"],
                             "tags": {"name": name}, "place_id": p.get("id", "")})
        print(f"[MOSQUE] places → {len(elements)} cami")
        return {"elements": elements}
    except Exception as e:
        print(f"[MOSQUE] places → {e}")
        return None


@app.get("/mosques")
async def nearby_mosques(lat: float, lng: float, radius: int = 3000):
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "Geçersiz konum")
    radius = max(500, min(radius, 10000))
    cell = f"{round(lat, 2)}:{round(lng, 2)}:{radius}"
    key, stale_key = f"mosq:{cell}", f"mosq_stale:{cell}"

    try:
        cached = await redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    result = await asyncio.to_thread(_places_fetch, lat, lng, radius)
    if result is None or not result["elements"]:
        result = await asyncio.to_thread(_overpass_fetch, lat, lng, radius)
    if result is not None:
        try:
            payload = json.dumps(result)
            # Boş sonucu kısa süre tut, dolu sonucu 7 gün
            ttl = MOSQUE_CACHE_TTL if result["elements"] else 3600
            await redis_client.setex(key, ttl, payload)
            if result["elements"]:
                await redis_client.set(stale_key, payload)
        except Exception:
            pass
        return result

    # Tüm sunucular hata verdi → eski sonuç varsa onu dön
    try:
        stale = await redis_client.get(stale_key)
        if stale:
            return json.loads(stale)
    except Exception:
        pass
    raise HTTPException(503, "Cami verisi şu an alınamıyor")