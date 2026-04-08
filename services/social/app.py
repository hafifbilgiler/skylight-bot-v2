import os, json, time, uuid, asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="ONE-BUNE Sosyal Servisi")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── CONFIG ──────────────────────────────────────────
LLM_BASE_URL  = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
LLM_API_KEY   = os.getenv("DEEPINFRA_API_KEY", "")
LLM_MODEL     = os.getenv("SOSYAL_LLM_MODEL", "meta-llama/Llama-3.2-3B-Instruct")
OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY", "")

# ── IN-MEMORY STORE (TTL'li) ─────────────────────────
# { user_id: { "travel_plans": [...], "companion_history": [...], "ts": float } }
_store: Dict[str, dict] = {}
TRAVEL_TTL    = 60 * 60 * 24 * 30   # 30 gün
COMPANION_TTL = 60 * 60 * 24 * 7    # 7 gün

def get_user(user_id: str) -> dict:
    now = time.time()
    if user_id not in _store:
        _store[user_id] = {
            "travel_plans": [],
            "companion_history": [],
            "companion_mood": [],
            "habits": [],
            "tasks": {"todo": [], "doing": [], "done": []},
            "ts": now,
        }
    return _store[user_id]

def save_user(user_id: str, data: dict):
    data["ts"] = time.time()
    _store[user_id] = data

# Periyodik TTL temizlik
async def cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        expired = [uid for uid, d in _store.items() if now - d.get("ts", 0) > TRAVEL_TTL]
        for uid in expired:
            del _store[uid]

@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_loop())

# ── HEALTH ──────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "sosyal", "users": len(_store)}

# ── LLM YARDIMCISI ──────────────────────────────────
async def llm_chat(messages: list, system: str, max_tokens=600) -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "system", "content": system}] + messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.8,
                }
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            return "Şu an yanıt üretilemedi."
    except Exception as e:
        print(f"[LLM] {e}")
        return "Bağlantı hatası."

# ══════════════════════════════════════════════════════
# SEYAHAT
# ══════════════════════════════════════════════════════

@app.post("/sosyal/travel/plan")
async def create_travel_plan(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id", "guest"))
    city    = body.get("city", "")
    country = body.get("country", "")
    days    = body.get("days", 5)
    budget  = body.get("budget", "orta")
    prefs   = body.get("preferences", "")

    system = """Sen deneyimli bir seyahat rehberisin. Türkçe, detaylı ve pratik gezi planları hazırlarsın.
Planlarında günlük aktiviteler, yemek önerileri, ulaşım ipuçları ve bütçe tahminleri bulunur.
Kültürel bilgiler ve yerel tavsiyeler eklersin."""

    prompt = f"""{city}, {country} için {days} günlük seyahat planı:
Bütçe: {budget} ({budget=='düşük' and 'kişi başı günlük ~50€' or budget=='orta' and '~150€' or '~400€+'})
Tercihler: {prefs or 'genel turist'}

Şu formatta hazırla:
GÜN 1: Başlık
- Sabah: ...
- Öğle: ...
- Akşam: ...
💰 Günlük tahmini: ~X€

(Her gün için devam et)

GENEL TAVSİYELER:
- Para birimi, dil, vize bilgisi
- Ulaşım ipuçları
- Mutlaka denenmesi gereken yemekler"""

    reply = await llm_chat([{"role": "user", "content": prompt}], system, max_tokens=1200)

    # Kaydet
    user = get_user(user_id)
    plan = {
        "id": str(uuid.uuid4())[:8],
        "city": city,
        "country": country,
        "days": days,
        "budget": budget,
        "plan": reply,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=TRAVEL_TTL)).isoformat(),
    }
    user["travel_plans"].append(plan)
    user["travel_plans"] = user["travel_plans"][-10:]  # max 10 plan
    save_user(user_id, user)

    return {"plan": reply, "plan_id": plan["id"], "city": city}


@app.post("/sosyal/travel/chat")
async def travel_chat(request: Request):
    body = await request.json()
    user_id  = str(body.get("user_id", "guest"))
    message  = body.get("message", "")
    city     = body.get("city", "")
    country  = body.get("country", "")
    history  = body.get("history", [])

    system = f"""Sen {city}, {country} konusunda uzman bir seyahat rehberisin.
Kullanıcıya bu şehir hakkında kısa, pratik ve samimi Türkçe cevaplar ver.
Yerel ipuçları, gizli köşeler, bütçe tavsiyeleri konularında yardım et.
Cevaplarını kısa tut (3-5 cümle)."""

    reply = await llm_chat(history + [{"role": "user", "content": message}], system, max_tokens=400)
    return {"reply": reply, "city": city}


@app.get("/sosyal/travel/plans/{user_id}")
async def get_travel_plans(user_id: str):
    user = get_user(user_id)
    return {"plans": user["travel_plans"]}


@app.get("/sosyal/travel/city-info")
async def get_city_info(city: str, country: str = "", lat: float = 0, lon: float = 0):
    """Şehir hakkında hızlı AI özeti + hava durumu."""
    system = "Sen bir seyahat ansiklopedisisin. Kısa ve bilgi dolu Türkçe özetler yazarsın."
    prompt = f"{city}, {country} hakkında: 1) En önemli 3 özellik 2) İdeal ziyaret süresi 3) Bütçe sınıfı (ucuz/orta/pahalı) 4) En iyi mevsim — toplam 5-6 cümle."
    reply = await llm_chat([{"role": "user", "content": prompt}], system, max_tokens=300)

    result = {"city": city, "country": country, "summary": reply, "weather": None}

    if OPENWEATHER_KEY and lat and lon:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    "https://api.openweathermap.org/data/2.5/weather",
                    params={"lat": lat, "lon": lon, "appid": OPENWEATHER_KEY, "units": "metric", "lang": "tr"}
                )
                if r.status_code == 200:
                    d = r.json()
                    result["weather"] = {
                        "temp": round(d["main"]["temp"]),
                        "feels_like": round(d["main"]["feels_like"]),
                        "description": d["weather"][0]["description"],
                        "humidity": d["main"]["humidity"],
                        "icon": d["weather"][0]["icon"],
                    }
        except Exception as e:
            print(f"[WEATHER] {e}")

    return result

# ══════════════════════════════════════════════════════
# DERT ORTAĞI
# ══════════════════════════════════════════════════════

COMPANION_SYSTEM = """Sen ONE-BUNE'nun empatik dert ortağısın. Adın Selin.

KİŞİLİĞİN:
- Gerçekten dinleyen, yargılamayan biri
- Empati kurarsın, hissettiklerini yansıtırsın
- Çözüm dayatmazsın, önce anlamaya çalışırsın
- Sıcak ve samimi bir dil kullanırsın
- Bazen hafif mizah katar, ama ağır anlarda ciddi kalırsın

KURALLAR:
- Klişe teselli cümlelerinden kaçın ("her şey yoluna girecek" gibi)
- Aktif dinleme: ne hissettiklerini yansıt, soru sor
- Kısa cevaplar (3-5 cümle max) — diyaloğu canlı tut
- Asla klinik tanı koyma, ilaç önerme
- Profesyonel destek gereken durumlarda nazikçe yönlendir
- Türkçe, samimi, günlük dil kullan"""

@app.post("/sosyal/companion/chat")
async def companion_chat(request: Request):
    body     = await request.json()
    user_id  = str(body.get("user_id", "guest"))
    message  = body.get("message", "")
    history  = body.get("history", [])

    reply = await llm_chat(
        history + [{"role": "user", "content": message}],
        COMPANION_SYSTEM,
        max_tokens=300
    )

    # Geçmişe kaydet (son 50 mesaj, 7 gün TTL)
    user = get_user(user_id)
    user["companion_history"].append({
        "role": "user",
        "content": message,
        "ts": time.time()
    })
    user["companion_history"].append({
        "role": "assistant",
        "content": reply,
        "ts": time.time()
    })
    # Max 100 mesaj tut
    user["companion_history"] = user["companion_history"][-100:]

    # Mood analizi
    mood = await _analyze_mood(message)
    user["companion_mood"].append({"mood": mood, "ts": time.time()})
    user["companion_mood"] = user["companion_mood"][-30:]

    save_user(user_id, user)
    return {"reply": reply, "mood": mood}


async def _analyze_mood(text: str) -> str:
    keywords = {
        "mutlu": ["güzel","harika","süper","sevindim","mutlu","başardım","iyi","mükemmel"],
        "üzgün": ["üzgün","ağladım","acı","kötü","mutsuz","zor","berbat","çöktüm"],
        "kaygılı": ["endişe","korku","stres","panik","huzursuz","gergin","baskı"],
        "sinirli": ["sinir","kızgın","öfke","rahatsız","sıkıldım","bezdim"],
        "yorgun": ["yorgun","bitik","tükendim","uyuyamadım","ağır","zor"],
        "umutlu": ["umut","belki","denerim","olur","iyileşir","çalışır"],
    }
    text_lower = text.lower()
    for mood, words in keywords.items():
        if any(w in text_lower for w in words):
            return mood
    return "nötr"


@app.get("/sosyal/companion/history/{user_id}")
async def get_companion_history(user_id: str, limit: int = 50):
    user = get_user(user_id)
    # 7 günden eski mesajları filtrele
    cutoff = time.time() - COMPANION_TTL
    recent = [m for m in user["companion_history"] if m.get("ts", 0) > cutoff]
    return {
        "history": recent[-limit:],
        "mood_history": user["companion_mood"][-14:],
        "total": len(recent)
    }


# ══════════════════════════════════════════════════════
# YEMEK
# ══════════════════════════════════════════════════════

@app.post("/sosyal/food/recipe")
async def get_recipe(request: Request):
    body    = await request.json()
    dish    = body.get("dish", "")
    prefs   = body.get("preferences", "")  # vegan, glutensiz vb
    history = body.get("history", [])

    system = """Sen profesyonel bir aşçısın ve yemek yazarısın.
Detaylı, uygulanabilir Türkçe tarifler yazarsın.
Malzeme listelerini gram/ölçü birimleriyle verirsin.
Pişirme süresi, zorluk derecesi ve kalori bilgisi eklersin.
Alternatif malzeme önerileri sunarsın."""

    prompt = f"{dish} tarifi{' ('+prefs+')' if prefs else ''}"
    reply  = await llm_chat(history + [{"role": "user", "content": prompt}], system, max_tokens=800)
    return {"recipe": reply, "dish": dish}


@app.post("/sosyal/food/suggest")
async def suggest_food(request: Request):
    body = await request.json()
    ingredients = body.get("ingredients", [])
    mood        = body.get("mood", "")
    time_avail  = body.get("time", 30)

    system = "Sen yaratıcı bir ev aşçısısın. Eldeki malzemelerle yapılabilecek pratik tarifler önerirsin."
    prompt = f"""Elimde şunlar var: {', '.join(ingredients) if ingredients else 'belirsiz'}
Sürem: {time_avail} dakika
Ruh halim: {mood or 'normal'}

3 farklı tarif öner (isim + 2 cümle açıklama)."""

    reply = await llm_chat([{"role": "user", "content": prompt}], system, max_tokens=400)
    return {"suggestions": reply}


# ══════════════════════════════════════════════════════
# GÜNLÜK PLANLAYICI
# ══════════════════════════════════════════════════════

@app.post("/sosyal/daily/coach")
async def daily_coach(request: Request):
    body     = await request.json()
    user_id  = str(body.get("user_id", "guest"))
    message  = body.get("message", "")
    tasks    = body.get("tasks", {})
    mood     = body.get("mood", "")
    history  = body.get("history", [])

    task_summary = f"Görevler — Yapılacak:{len(tasks.get('todo',[]))}, Yapılıyor:{len(tasks.get('doing',[]))}, Tamamlandı:{len(tasks.get('done',[]))}"

    system = f"""Sen motive edici bir yaşam koçusun. Adın Can.
Kullanıcıya günlük planlamasında yardım ediyorsun.
{task_summary}
Kullanıcının ruh hali: {mood or 'belirsiz'}

Kısa, motive edici, pratik Türkçe cevaplar ver (3-4 cümle).
Görev önceliklendirme, zaman yönetimi ve enerji yönetimi konularında uzmansın."""

    reply = await llm_chat(history + [{"role": "user", "content": message}], system, max_tokens=300)

    # Görevleri kaydet
    if tasks:
        user = get_user(user_id)
        user["tasks"] = tasks
        save_user(user_id, user)

    return {"reply": reply}


@app.get("/sosyal/daily/data/{user_id}")
async def get_daily_data(user_id: str):
    user = get_user(user_id)
    return {
        "tasks": user.get("tasks", {"todo": [], "doing": [], "done": []}),
        "habits": user.get("habits", []),
    }


@app.post("/sosyal/daily/save")
async def save_daily_data(request: Request):
    body    = await request.json()
    user_id = str(body.get("user_id", "guest"))
    user    = get_user(user_id)
    if "tasks"  in body: user["tasks"]  = body["tasks"]
    if "habits" in body: user["habits"] = body["habits"]
    save_user(user_id, user)
    return {"status": "ok"}