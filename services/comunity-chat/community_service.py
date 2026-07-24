"""
OneBune Community Chat Service
FastAPI + WebSocket + Redis + AI Moderasyon
Port: 8001 (ayrı servis)
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
import google.generativeai as genai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from jose import jwt, JWTError

# ── Config ────────────────────────────────────────────────────
SECRET_KEY    = os.getenv("JWT_SECRET", "onebune-secret")
DATABASE_URL  = os.getenv("COMMUNITY_DB_URL", "postgresql://user:pass@localhost/onebune_community")
REDIS_URL     = os.getenv("REDIS_URL", "redis://localhost:6379")
GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")
MAX_ROOM_SIZE = 1000   # oda başı max kullanıcı
MSG_HISTORY   = 50     # bağlanınca kaç mesaj gönderilsin

genai.configure(api_key=GEMINI_KEY)
ai_model = genai.GenerativeModel("gemini-1.5-flash")

app = FastAPI(title="OneBune Community Chat")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── DB & Redis bağlantıları ───────────────────────────────────
db_pool: asyncpg.Pool = None
redis: aioredis.Redis = None

@app.on_event("startup")
async def startup():
    global db_pool, redis
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    redis   = await aioredis.from_url(REDIS_URL, decode_responses=True)

@app.on_event("shutdown")
async def shutdown():
    await db_pool.close()
    await redis.close()

# ── JWT Doğrulama ─────────────────────────────────────────────
def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except JWTError:
        raise HTTPException(401, "Geçersiz token")

# ── AI Moderasyon ─────────────────────────────────────────────
AI_PROMPT = """Sen OneBune İslami sohbet uygulamasının moderatörüsün.
Türkçe mesajları analiz et ve JSON formatında yanıtla.

Mesaj: "{message}"
Oda: "{room_name}"

Yanıt formatı (sadece JSON):
{{
  "safe": true/false,
  "action": "none|warn|delete|mute|ban",
  "reason": "kısa açıklama",
  "user_message": "kullanıcıya gösterilecek Türkçe mesaj (action none ise boş)"
}}

Kurallar:
- Küfür/hakaret → delete + mute (10 dk)
- Ağır hakaret/tehdit → ban
- Spam (aynı mesaj tekrarı) → delete + warn
- Konu dışı → warn, user_message ile yönlendir
- Dini soru → none, ama öner
- Normal sohbet → none
"""

async def ai_moderate(message: str, room_name: str, username: str) -> dict:
    """Mesajı AI ile denetle. Hızlı ve ucuz — Gemini Flash kullanır."""
    try:
        prompt = AI_PROMPT.format(message=message[:500], room_name=room_name)
        response = await asyncio.to_thread(
            ai_model.generate_content,
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.1,
                max_output_tokens=200,
            )
        )
        text = response.text.strip()
        # JSON parse
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        return json.loads(text)
    except Exception as e:
        # AI hata verirse mesajı geçir
        return {"safe": True, "action": "none", "reason": "", "user_message": ""}

# ── Connection Manager ────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        # room_id → {user_id: websocket}
        self.rooms: dict[int, dict[int, WebSocket]] = {}

    async def connect(self, ws: WebSocket, room_id: int, user_id: int):
        await ws.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][user_id] = ws

    def disconnect(self, room_id: int, user_id: int):
        if room_id in self.rooms:
            self.rooms[room_id].pop(user_id, None)

    async def broadcast(self, room_id: int, message: dict, exclude_user: int = None):
        if room_id not in self.rooms:
            return
        dead = []
        for uid, ws in self.rooms[room_id].items():
            if uid == exclude_user:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.rooms[room_id].pop(uid, None)

    async def send_to_user(self, room_id: int, user_id: int, message: dict):
        ws = self.rooms.get(room_id, {}).get(user_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    def room_size(self, room_id: int) -> int:
        return len(self.rooms.get(room_id, {}))

manager = ConnectionManager()

# ── Yardımcı fonksiyonlar ─────────────────────────────────────
async def get_room(slug: str) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM community_rooms WHERE slug=$1 AND is_active=TRUE", slug)
        return dict(row) if row else None

async def is_banned(user_id: int, room_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id FROM community_bans
            WHERE user_id=$1
              AND (room_id=$2 OR room_id IS NULL)
              AND (banned_until IS NULL OR banned_until > NOW())
              AND ban_type='ban'
            LIMIT 1
        """, user_id, room_id)
        return row is not None

async def is_muted(user_id: int, room_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id FROM community_bans
            WHERE user_id=$1
              AND (room_id=$2 OR room_id IS NULL)
              AND banned_until > NOW()
              AND ban_type='mute'
            LIMIT 1
        """, user_id, room_id)
        return row is not None

async def get_history(room_id: int, limit: int = MSG_HISTORY) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT m.id, m.user_id, m.username, m.content,
                   m.created_at, m.reply_to_id
            FROM community_messages m
            WHERE m.room_id=$1 AND m.is_deleted=FALSE
            ORDER BY m.created_at DESC LIMIT $2
        """, room_id, limit)
        return [dict(r) for r in reversed(rows)]

async def save_message(room_id: int, user_id: int, username: str,
                       content: str, reply_to: int = None) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO community_messages (room_id, user_id, username, content, reply_to_id)
            VALUES ($1, $2, $3, $4, $5) RETURNING id
        """, room_id, user_id, username, content, reply_to)
        return row["id"]

async def delete_message(msg_id: int, by: str = "ai"):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE community_messages SET is_deleted=TRUE, deleted_by=$2
            WHERE id=$1
        """, msg_id, by)

async def apply_ban(user_id: int, room_id: int, ban_type: str,
                    reason: str, duration_min: int = None):
    banned_until = None
    if duration_min:
        banned_until = datetime.utcnow() + timedelta(minutes=duration_min)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO community_bans (user_id, room_id, ban_type, reason, banned_until)
            VALUES ($1, $2, $3, $4, $5)
        """, user_id, room_id, ban_type, reason, banned_until)

async def log_mod_action(msg_id: int, user_id: int, room_id: int,
                         action: str, reason: str, ai_reason: str, duration: int = None):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO community_mod_log
              (message_id, user_id, room_id, action, reason, ai_reason, duration_min)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, msg_id, user_id, room_id, action, reason, ai_reason, duration)

# ── Redis Presence ────────────────────────────────────────────
async def set_presence(user_id: int, room_id: int, username: str):
    key = f"presence:{user_id}"
    # Önce eski odadan çıkar
    old = await redis.hget(key, "room_id")
    if old and old != str(room_id):
        await redis.srem(f"room:members:{old}", user_id)
        # Eski odaya "çıktı" bildirimi
        await manager.broadcast(int(old), {
            "type": "presence",
            "user_id": user_id,
            "username": username,
            "status": "left",
            "room_count": await redis.scard(f"room:members:{old}")
        })
    await redis.hset(key, mapping={
        "room_id": room_id,
        "username": username,
        "joined_at": str(time.time())
    })
    await redis.expire(key, 3600)
    await redis.sadd(f"room:members:{room_id}", user_id)

async def remove_presence(user_id: int, room_id: int, username: str):
    await redis.delete(f"presence:{user_id}")
    await redis.srem(f"room:members:{room_id}", user_id)
    await manager.broadcast(room_id, {
        "type": "presence",
        "user_id": user_id,
        "username": username,
        "status": "left",
        "room_count": await redis.scard(f"room:members:{room_id}")
    })

async def get_room_count(room_id: int) -> int:
    return await redis.scard(f"room:members:{room_id}")

# ── WebSocket Endpoint ────────────────────────────────────────
@app.websocket("/ws/{room_slug}")
async def websocket_endpoint(ws: WebSocket, room_slug: str, token: str):
    # 1. Token doğrula
    try:
        payload  = verify_token(token)
        user_id  = int(payload["sub"])
        username = payload.get("username", "Kullanıcı")
        is_premium = payload.get("is_premium", False)
    except Exception:
        await ws.close(code=4001, reason="Geçersiz token")
        return

    # 2. Premium kontrolü
    if not is_premium:
        await ws.close(code=4003, reason="Premium gerekli")
        return

    # 3. Oda kontrolü
    room = await get_room(room_slug)
    if not room:
        await ws.close(code=4004, reason="Oda bulunamadı")
        return
    room_id = room["id"]

    # 4. Ban kontrolü
    if await is_banned(user_id, room_id):
        await ws.close(code=4005, reason="Bu odadan yasaklandınız")
        return

    # 5. Oda doluluk kontrolü
    if manager.room_size(room_id) >= MAX_ROOM_SIZE:
        await ws.close(code=4006, reason="Oda dolu")
        return

    # 6. Bağlan
    await manager.connect(ws, room_id, user_id)
    await set_presence(user_id, room_id, username)
    room_count = await get_room_count(room_id)

    # 7. Geçmiş mesajları gönder
    history = await get_history(room_id)
    await ws.send_json({
        "type": "history",
        "messages": [
            {
                "id": str(m["id"]),
                "user_id": m["user_id"],
                "username": m["username"],
                "content": m["content"],
                "created_at": m["created_at"].isoformat(),
                "reply_to_id": str(m["reply_to_id"]) if m["reply_to_id"] else None,
            }
            for m in history
        ],
        "room": {"id": room_id, "name": room["name"], "icon": room["icon"]},
        "room_count": room_count
    })

    # 8. Odaya katıldı bildirimi
    await manager.broadcast(room_id, {
        "type": "presence",
        "user_id": user_id,
        "username": username,
        "status": "joined",
        "room_count": room_count
    }, exclude_user=user_id)

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "message")

            # ── Mesaj gönderme ────────────────────────────────
            if msg_type == "message":
                content   = data.get("content", "").strip()
                reply_to  = data.get("reply_to_id")

                if not content or len(content) > 500:
                    continue

                # Susturulmuş mu?
                if await is_muted(user_id, room_id):
                    await ws.send_json({
                        "type": "error",
                        "message": "⚠️ Şu an susturuldunuz. Biraz bekleyin."
                    })
                    continue

                # Spam kontrolü — Redis'te son mesajı kontrol et
                spam_key = f"lastmsg:{user_id}:{room_id}"
                last_msg = await redis.get(spam_key)
                if last_msg == content:
                    await ws.send_json({
                        "type": "warn",
                        "message": "⚠️ Aynı mesajı tekrar göndermeyin."
                    })
                    continue
                await redis.setex(spam_key, 5, content)  # 5 sn cooldown

                # Mesajı kaydet
                msg_id = await save_message(room_id, user_id, username, content, reply_to)

                # Önce odaya yayınla (hızlı)
                msg_payload = {
                    "type": "message",
                    "id": str(msg_id),
                    "user_id": user_id,
                    "username": username,
                    "content": content,
                    "created_at": datetime.utcnow().isoformat(),
                    "reply_to_id": reply_to,
                }
                await manager.broadcast(room_id, msg_payload)

                # Arka planda AI moderasyon
                asyncio.create_task(
                    ai_moderate_and_act(
                        msg_id, content, room["name"],
                        user_id, username, room_id
                    )
                )

    except WebSocketDisconnect:
        manager.disconnect(room_id, user_id)
        await remove_presence(user_id, room_id, username)

# ── AI Moderasyon Görevi ──────────────────────────────────────
async def ai_moderate_and_act(msg_id: int, content: str, room_name: str,
                               user_id: int, username: str, room_id: int):
    result = await ai_moderate(content, room_name, username)
    action = result.get("action", "none")

    if action == "none":
        return

    ai_reason   = result.get("reason", "")
    user_msg    = result.get("user_message", "")
    duration    = None

    if action == "delete":
        await delete_message(msg_id)
        await manager.broadcast(room_id, {
            "type": "message_deleted",
            "id": str(msg_id)
        })

    elif action == "warn":
        await manager.send_to_user(room_id, user_id, {
            "type": "warn",
            "message": f"⚠️ AI Moderatör: {user_msg}"
        })

    elif action == "mute":
        duration = 10
        await delete_message(msg_id)
        await apply_ban(user_id, room_id, "mute", ai_reason, duration)
        await manager.broadcast(room_id, {"type": "message_deleted", "id": str(msg_id)})
        await manager.send_to_user(room_id, user_id, {
            "type": "muted",
            "message": f"🔇 {duration} dakika susturuldunuz. Sebep: {user_msg}"
        })

    elif action == "ban":
        duration = 60 * 24  # 1 gün
        await delete_message(msg_id)
        await apply_ban(user_id, room_id, "ban", ai_reason, duration)
        await manager.broadcast(room_id, {"type": "message_deleted", "id": str(msg_id)})
        await manager.send_to_user(room_id, user_id, {
            "type": "banned",
            "message": f"🚫 24 saat yasaklandınız. Sebep: {user_msg}"
        })

    await log_mod_action(msg_id, user_id, room_id, action,
                         result.get("reason", ""), ai_reason, duration)

# ── REST Endpointler ──────────────────────────────────────────
@app.get("/rooms")
async def get_rooms():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, slug, name, description, icon FROM community_rooms "
            "WHERE is_active=TRUE ORDER BY order_index")
        rooms = []
        for r in rows:
            d = dict(r)
            d["online_count"] = await get_room_count(r["id"])
            rooms.append(d)
        return {"rooms": rooms}

@app.get("/rooms/{slug}/messages")
async def get_room_messages(slug: str, limit: int = 50):
    room = await get_room(slug)
    if not room:
        raise HTTPException(404, "Oda bulunamadı")
    messages = await get_history(room["id"], limit)
    return {"messages": messages}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "community-chat"}