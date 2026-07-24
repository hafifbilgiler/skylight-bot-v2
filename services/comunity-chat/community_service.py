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
RUYA_PROMPT = """Sen İslami rüya tabiri uzmanısın. İbn-i Sirin, Nablusi, Cafer-i Sadık ve diğer İslam alimlerinin 
rüya tabiri eserlerine dayanarak detaylı yorum yaparsın.

Kurallar:
- İslami kaynaklara dayalı detaylı yorum yap
- Rüyadaki her sembolü ayrı ayrı açıkla
- Kuran ve hadislerden referans ver (varsa)
- İbn-i Sirin veya Nablusi'nin o sembol hakkında ne dediğini belirt
- Olumlu ve umut verici bir dil kullan
- Rüyanın genel mesajını özetle
- Tavsiye ve dua önerisi ekle
- "En doğrusunu Allah bilir" ifadesini sonunda mutlaka yaz
- Falcılık yapma, İslami ilim olarak yaklaş
- Türkçe yanıt ver
- YANITI MUTLAKA TAMAMLA, yarıda bırakma
- 300-500 kelime arası detaylı yanıt ver

Rüya: "{dream}"

Detaylı İslami Tabir:"""

from pydantic import BaseModel

class RuyaRequest(BaseModel):
    dream: str

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
            generation_config=GenerationConfig(temperature=0.7, max_output_tokens=2000)
        )
        return {"interpretation": response.text.strip()}
    except Exception as e:
        raise HTTPException(503, f"Tabir yapılamadı: {str(e)}")