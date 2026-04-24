import os, json, time, uuid, asyncio, hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="ONE-BUNE Sosyal Servisi")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── CONFIG ──────────────────────────────────────────
LLM_BASE_URL    = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
LLM_API_KEY     = os.getenv("DEEPINFRA_API_KEY", "")
LLM_MODEL       = os.getenv("SOSYAL_LLM_MODEL", "meta-llama/Llama-3.2-3B-Instruct")
OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY", "")

REDIS_URL = os.getenv("REDIS_URL", "redis://skylight-redis:6379")
REDIS_DB  = int(os.getenv("REDIS_DB", "5"))    # Sosyal → DB 5

# ── TRAVELPAYOUTS (Aviasales Flight Data API) ──────
TP_API_TOKEN    = os.getenv("TRAVELPAYOUTS_TOKEN", "")
TP_MARKER_ID    = os.getenv("TRAVELPAYOUTS_MARKER", "716107")
TP_BASE_URL     = "https://api.travelpayouts.com"
TP_CACHE_TTL    = 60 * 60  # 1 saat

# ═══════════════════════════════════════════════════════
# REDIS STORE
# ═══════════════════════════════════════════════════════
# Key şeması (namespace: sos:):
#   sos:u:{user_id}:travel_plans      → JSON list   TTL 30 gün
#   sos:u:{user_id}:companion_history → JSON list   TTL 7 gün
#   sos:u:{user_id}:companion_mood    → JSON list   TTL 30 gün
#   sos:u:{user_id}:saved_trips       → JSON list   TTL 90 gün (kayıtlı planlar)
# ═══════════════════════════════════════════════════════

TRAVEL_TTL      = 60 * 60 * 24 * 30   # 30 gün (eski travel plans)
COMPANION_TTL   = 60 * 60 * 24 * 7    # 7 gün
MOOD_TTL        = 60 * 60 * 24 * 30   # 30 gün
SAVED_TRIPS_TTL = 60 * 60 * 24 * 90   # 90 gün (her erişimde TTL yenilenir — aktif kullanıcı için kalıcı)

_redis: Optional[aioredis.Redis] = None

async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            REDIS_URL,
            db=REDIS_DB,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
        )
    return _redis

def _k(user_id: str, field: str) -> str:
    return f"sos:u:{user_id}:{field}"

async def redis_get_json(key: str, default=None):
    try:
        r = await get_redis()
        v = await r.get(key)
        return json.loads(v) if v else default
    except Exception as e:
        print(f"[REDIS GET] {key}: {e}")
        return default

async def redis_set_json(key: str, value, ttl: int = None):
    try:
        r = await get_redis()
        if ttl:
            await r.setex(key, ttl, json.dumps(value, ensure_ascii=False))
        else:
            await r.set(key, json.dumps(value, ensure_ascii=False))
    except Exception as e:
        print(f"[REDIS SET] {key}: {e}")

# ─── Kullanıcı state'i (Redis destekli) ─────────────────
async def get_travel_plans(user_id: str) -> list:
    return await redis_get_json(_k(user_id, "travel_plans"), [])

async def add_travel_plan(user_id: str, plan: dict):
    plans = await get_travel_plans(user_id)
    plans.append(plan)
    plans = plans[-10:]
    await redis_set_json(_k(user_id, "travel_plans"), plans, TRAVEL_TTL)

async def get_companion_history_raw(user_id: str) -> list:
    return await redis_get_json(_k(user_id, "companion_history"), [])

async def push_companion_message(user_id: str, msg: dict):
    hist = await get_companion_history_raw(user_id)
    hist.append(msg)
    hist = hist[-100:]
    await redis_set_json(_k(user_id, "companion_history"), hist, COMPANION_TTL)

async def get_companion_moods(user_id: str) -> list:
    return await redis_get_json(_k(user_id, "companion_mood"), [])

async def push_companion_mood(user_id: str, mood_entry: dict):
    moods = await get_companion_moods(user_id)
    moods.append(mood_entry)
    moods = moods[-30:]
    await redis_set_json(_k(user_id, "companion_mood"), moods, MOOD_TTL)

# ─── Kayıtlı Seyahat Planları (yeni) ───────────────────
async def get_saved_trips(user_id: str) -> list:
    return await redis_get_json(_k(user_id, "saved_trips"), [])

async def set_saved_trips(user_id: str, trips: list):
    await redis_set_json(_k(user_id, "saved_trips"), trips, SAVED_TRIPS_TTL)


@app.on_event("startup")
async def startup():
    try:
        r = await get_redis()
        await r.ping()
        print(f"[REDIS] ✅ Bağlandı → DB {REDIS_DB}")
    except Exception as e:
        print(f"[REDIS] ❌ Bağlantı hatası: {e}")

@app.on_event("shutdown")
async def shutdown():
    global _redis
    if _redis:
        await _redis.aclose()

@app.get("/health")
async def health():
    redis_ok = False
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if redis_ok else "degraded",
        "service": "sosyal",
        "redis": "ok" if redis_ok else "down",
        "redis_db": REDIS_DB,
    }

# ── LLM YARDIMCISI ──────────────────────────────────
async def llm_chat(messages: list, system: str, max_tokens=600, temperature=0.8) -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "system", "content": system}] + messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
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

    budget_text = "kişi başı günlük ~50€" if budget == "düşük" else ("~150€" if budget == "orta" else "~400€+")
    prompt = f"""{city}, {country} için {days} günlük seyahat planı:
Bütçe: {budget} ({budget_text})
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

    reply = await smart_llm([{"role": "user", "content": prompt}], system, max_tokens=1200)

    plan = {
        "id": str(uuid.uuid4())[:8],
        "city": city, "country": country, "days": days, "budget": budget,
        "plan": reply,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=TRAVEL_TTL)).isoformat(),
    }
    await add_travel_plan(user_id, plan)

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

    reply = await smart_llm(history + [{"role": "user", "content": message}], system, max_tokens=400)
    return {"reply": reply, "city": city}


@app.get("/sosyal/travel/plans/{user_id}")
async def list_travel_plans(user_id: str):
    plans = await get_travel_plans(user_id)
    return {"plans": plans}


@app.get("/sosyal/travel/city-info")
async def get_city_info(city: str, country: str = "", lat: float = 0, lon: float = 0):
    system = "Sen bir seyahat ansiklopedisisin. Kısa ve bilgi dolu Türkçe özetler yazarsın."
    prompt = f"{city}, {country} hakkında: 1) En önemli 3 özellik 2) İdeal ziyaret süresi 3) Bütçe sınıfı (ucuz/orta/pahalı) 4) En iyi mevsim — toplam 5-6 cümle."
    reply = await smart_llm([{"role": "user", "content": prompt}], system, max_tokens=300)

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
# DERT ORTAĞI — Selin v2.0
# ══════════════════════════════════════════════════════

COMPANION_SYSTEM = """Sen Selin'sin — ONE-BUNE'nun dert ortağısın.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KİMSİN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
35 yaşlarında, hayat tecrübesi olan, sıcakkanlı bir kadınsın.
Kullanıcıya yakın bir arkadaş gibi yaklaşırsın — terapist DEĞİLSİN.
Türkçe'yi akıcı ve doğal konuşursun. Klişe cümlelerden nefret edersin.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NASIL KONUŞURSUN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1) ÖNCE HİSSETTİĞİNİ YANSIT, SONRA KONUŞ.
   "Gerçekten yorulmuşsun anlıyorum..."
   "Bu söylediğin ağır bir şey..."
   "Öfkelenmen çok normal görünüyor..."

2) HER MESAJDA SORU SORMA.
   Her 3 mesajın en fazla 1'inde soru sor.
   Diğerlerinde: yansıt, destekle, yanında ol.

3) KISA KAL, ama KURU OLMA.
   2-4 cümle ideal. Uzun paragraflar yazma.

4) SUSMAYI BİL.
   Bazen kullanıcı sadece duyulmak ister. Çözüm önerme.
   "Burası zor bir an. Yanındayım." yeterli olabilir.

5) SOMUT OL, GENELLEMEDEN KAÇIN.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YAPMADIKLARIN (ASLA)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✗ "Her şey yoluna girecek" gibi boş iyimserlik
✗ "Bu çok üzücü" gibi klişe teselli
✗ Madde madde liste (bu bir SOHBET, rapor değil)
✗ Sürekli "profesyonel destek al" demesi
✗ Emoji yağmuru
✗ Büyük harf, ünlem işareti bombardımanı

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KRİTİK UYARI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Kullanıcı "kendime zarar", "ölmek istiyorum", "dayanamıyorum artık" ifadeleri kullanırsa:
  → Önce yanında olduğunu söyle, sonra 182 (Psikiyatri Danışma Hattı)
İstismar, şiddet bahsi: 183, 155

Bu ifadeler OLMADAN asla telefon numarası verme.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ÖRNEK DİYALOGLAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Kullanıcı: "Bugün yine işte patladım"
Sen: "Of, bugün gerçekten yıpratıcı olmuş. Patlayacak kadar birikmişse, çok şey birikmiş demektir. İstersen biraz boşal, dinlerim."

Kullanıcı: "Kimse beni anlamıyor"
Sen: "Duydum seni. Anlaşılmamak, insanın içinde sessiz bir yalnızlık bırakır. Buradayım şu an."

Kullanıcı: "Kendimi öldürmek istiyorum"
Sen: "Durakla bir saniye. Bu söylediğin şu an gerçekten ağır bir his — ve sen yalnız değilsin. 182 Psikiyatri Danışma Hattı'nı aramanı çok istiyorum şimdi; 7/24 açık, doğrudan biri dinler. Ben de buradayım."

Şimdi gerçekten dinle. Acele yok."""


@app.post("/sosyal/companion/chat")
async def companion_chat(request: Request):
    body      = await request.json()
    raw_token = body.get("token") or body.get("user_id") or "anonymous"
    user_id   = hashlib.sha256(str(raw_token).encode()).hexdigest()[:16]
    message   = body.get("message", "")
    history   = body.get("history", [])
    mood      = body.get("mood", "")

    system = COMPANION_SYSTEM
    if mood:
        system += f"\n\n[Kullanıcı bu mesajdan önce kendini '{mood}' olarak işaretledi. Bunu biliyorsun ama açıkça söylemeden konuşmana yansıt.]"

    trimmed_history = history[-20:] if len(history) > 20 else history

    reply = await smart_llm(
        trimmed_history + [{"role": "user", "content": message}],
        system,
        max_tokens=400,
    )

    now_ts = time.time()
    await push_companion_message(user_id, {"role": "user",      "content": message, "ts": now_ts})
    await push_companion_message(user_id, {"role": "assistant", "content": reply,   "ts": now_ts})

    detected_mood = await _analyze_mood(message)
    await push_companion_mood(user_id, {"mood": detected_mood, "ts": now_ts})

    return {"reply": reply, "mood": detected_mood}


async def _analyze_mood(text: str) -> str:
    keywords = {
        "mutlu":   ["güzel","harika","süper","sevindim","mutlu","başardım","iyi","mükemmel","keyifli","eğlendim"],
        "üzgün":   ["üzgün","ağladım","acı","kötü","mutsuz","zor","berbat","çöktüm","kederli","hüzün"],
        "kaygılı": ["endişe","korku","stres","panik","huzursuz","gergin","baskı","kaygı","tedirgin"],
        "sinirli": ["sinir","kızgın","öfke","rahatsız","sıkıldım","bezdim","patladım","sinirlendi"],
        "yorgun":  ["yorgun","bitik","tükendim","uyuyamadım","ağır","zor","yoruldum","bitap"],
        "umutlu":  ["umut","belki","denerim","olur","iyileşir","çalışır","olacak","deneyeceğim"],
        "yalnız":  ["yalnız","kimsesiz","yapayalnız","tek başıma","anlaşılmıyorum"],
    }
    text_lower = text.lower()
    for mood, words in keywords.items():
        if any(w in text_lower for w in words):
            return mood
    return "nötr"


@app.get("/sosyal/companion/history/{user_id}")
async def get_companion_history(user_id: str, limit: int = 50, token: str = ""):
    if token and token != "guest":
        real_id = hashlib.sha256(token.encode()).hexdigest()[:16]
    else:
        real_id = user_id
    hist   = await get_companion_history_raw(real_id)
    moods  = await get_companion_moods(real_id)
    cutoff = time.time() - COMPANION_TTL
    recent = [m for m in hist if m.get("ts", 0) > cutoff]
    return {
        "history":      recent[-limit:],
        "mood_history": moods[-14:],
        "total":        len(recent),
    }

# ══════════════════════════════════════════════════════
# TRAVELPAYOUTS — UÇUŞ ARAMA & FİYAT
# ══════════════════════════════════════════════════════
# Marker ID: 716107 (Hafifbilgiler affiliate)
# API: Aviasales Flight Data API
# ══════════════════════════════════════════════════════

AIRLINE_NAMES = {
    "TK": "Turkish Airlines", "PC": "Pegasus", "VF": "AJet",
    "XQ": "SunExpress", "LH": "Lufthansa", "BA": "British Airways",
    "AF": "Air France", "KL": "KLM", "EK": "Emirates",
    "QR": "Qatar Airways", "TB": "TUI", "FR": "Ryanair",
    "W6": "Wizz Air", "U2": "easyJet", "AZ": "ITA Airways",
    "IB": "Iberia", "OS": "Austrian", "SU": "Aeroflot",
    "W9": "Wizz Air Malta",
}

def airline_name(code: str) -> str:
    return AIRLINE_NAMES.get(code, code)


async def tp_request(endpoint: str, params: dict) -> dict:
    """Travelpayouts API wrapper — redis cache'li."""
    if not TP_API_TOKEN:
        return {"success": False, "error": "TP_API_TOKEN tanımsız"}

    cache_key = f"tp:{endpoint}:" + ":".join(f"{k}={v}" for k, v in sorted(params.items()))
    cached = await redis_get_json(cache_key)
    if cached:
        cached["_cached"] = True
        return cached

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{TP_BASE_URL}{endpoint}",
                headers={"X-Access-Token": TP_API_TOKEN},
                params=params,
            )
            if r.status_code == 200:
                data = r.json()
                await redis_set_json(cache_key, data, TP_CACHE_TTL)
                return data
            return {"success": False, "error": f"HTTP {r.status_code}", "detail": r.text[:200]}
    except Exception as e:
        print(f"[TP] {endpoint} error: {e}")
        return {"success": False, "error": str(e)}


def build_affiliate_link(origin: str, destination: str, depart_date: str = "", return_date: str = "") -> str:
    """Aviasales deep-link üretir — marker ID otomatik eklenir."""
    def fmt(d: str) -> str:
        if not d or len(d) < 10:
            return ""
        return d[8:10] + d[5:7]

    search_code = f"{origin.upper()}{fmt(depart_date)}{destination.upper()}"
    if return_date:
        search_code += fmt(return_date)

    return f"https://www.aviasales.com/search/{search_code}?marker={TP_MARKER_ID}"


def format_flight(raw: dict, origin: str, destination: str) -> dict:
    airline_code = raw.get("airline", "")
    dep = raw.get("departure_at", "")
    ret = raw.get("return_at", "")

    dep_date = dep[:10] if dep else ""
    dep_time = dep[11:16] if dep else ""
    ret_date = ret[:10] if ret else ""
    ret_time = ret[11:16] if ret else ""

    duration_min = raw.get("duration", 0) or raw.get("duration_to", 0)
    hours = duration_min // 60
    mins  = duration_min % 60
    duration_text = f"{hours}s {mins}dk" if hours else f"{mins}dk"

    return {
        "airline_code": airline_code,
        "airline_name": airline_name(airline_code),
        "flight_number": f"{airline_code}{raw.get('flight_number', '')}",
        "price": raw.get("price", 0),
        "origin": origin,
        "destination": destination,
        "depart_date": dep_date,
        "depart_time": dep_time,
        "return_date": ret_date,
        "return_time": ret_time,
        "duration": duration_text,
        "duration_min": duration_min,
        "direct": raw.get("number_of_changes", 1) == 0,
        "affiliate_url": build_affiliate_link(origin, destination, dep_date, ret_date),
    }


@app.post("/sosyal/travel/flight_search")
async def flight_search(request: Request):
    body = await request.json()
    origin      = str(body.get("origin", "")).upper()
    destination = str(body.get("destination", "")).upper()
    depart_date = body.get("depart_date", "")
    return_date = body.get("return_date", "")
    currency    = body.get("currency", "try")
    one_way     = bool(body.get("one_way", False))

    if not origin or not destination:
        return {"success": False, "error": "origin ve destination zorunlu"}

    # ── 3 endpoint'i paralel çağır ──────────────────────
    # 1. /v1/prices/cheap — spesifik tarih için
    params_cheap = {"origin": origin, "destination": destination, "currency": currency}
    if depart_date:
        params_cheap["depart_date"] = depart_date
    if return_date and not one_way:
        params_cheap["return_date"] = return_date

    # 2. /v1/prices/cheap — sadece ay için (gevşek)
    params_month = {"origin": origin, "destination": destination, "currency": currency}
    if depart_date and len(depart_date) >= 7:
        params_month["depart_date"] = depart_date[:7]  # YYYY-MM

    # 3. /v2/prices/latest — cache'deki son uçuşlar
    params_latest = {
        "origin": origin,
        "destination": destination,
        "currency": currency,
        "limit": 30,
        "period_type": "year",
    }

    cheap_task  = tp_request("/v1/prices/cheap", params_cheap)
    month_task  = tp_request("/v1/prices/cheap", params_month) if params_month.get("depart_date") else None
    latest_task = tp_request("/v2/prices/latest", params_latest)

    results = await asyncio.gather(
        cheap_task,
        month_task if month_task else asyncio.sleep(0, result={"success": False}),
        latest_task,
        return_exceptions=True,
    )
    cheap_data, month_data, latest_data = results

    flights = []
    seen_keys = set()

    def add_flight(f_raw):
        flight = format_flight(f_raw, origin, destination)
        # Aynı uçuş aynı gün? Skip.
        k = f"{flight['flight_number']}:{flight['depart_date']}"
        if k in seen_keys:
            return
        seen_keys.add(k)
        flights.append(flight)

    # Cheap (spesifik tarih) sonuçları
    if isinstance(cheap_data, dict) and cheap_data.get("success"):
        raw = cheap_data.get("data", {}).get(destination, {})
        for _, f in (raw.items() if isinstance(raw, dict) else []):
            add_flight(f)

    # Month (ay için) sonuçları
    if isinstance(month_data, dict) and month_data.get("success"):
        raw = month_data.get("data", {}).get(destination, {})
        for _, f in (raw.items() if isinstance(raw, dict) else []):
            add_flight(f)

    # Latest (son fiyatlar) sonuçları — format farklı
    if isinstance(latest_data, dict) and latest_data.get("success"):
        data_items = latest_data.get("data", [])
        if isinstance(data_items, list):
            for f in data_items:
                if f.get("origin") != origin or f.get("destination") != destination:
                    continue
                add_flight(f)

    flights.sort(key=lambda f: f["price"])

    return {
        "success": True,
        "origin": origin,
        "destination": destination,
        "currency": currency.upper(),
        "flights": flights[:15],
        "total_found": len(flights),
        "sources": {
            "cheap":  isinstance(cheap_data, dict)  and cheap_data.get("success",  False),
            "month":  isinstance(month_data, dict)  and month_data.get("success",  False),
            "latest": isinstance(latest_data, dict) and latest_data.get("success", False),
        },
    }


@app.post("/sosyal/travel/flight_calendar")
async def flight_calendar(request: Request):
    body = await request.json()
    origin      = str(body.get("origin", "")).upper()
    destination = str(body.get("destination", "")).upper()
    depart_date = body.get("depart_date", "")
    currency    = body.get("currency", "try")

    if not origin or not destination or not depart_date:
        return {"success": False, "error": "origin, destination ve depart_date zorunlu"}

    params = {
        "origin": origin, "destination": destination, "depart_date": depart_date,
        "calendar_type": "departure_date", "currency": currency,
    }

    data = await tp_request("/v1/prices/calendar", params)

    if not data.get("success"):
        return {"success": False, "error": data.get("error", "API hatası")}

    raw = data.get("data", {})
    days = []
    for day_key, flight in raw.items():
        days.append({
            "date": day_key,
            "price": flight.get("value") or flight.get("price", 0),
            "airline": airline_name(flight.get("airline", "")),
            "direct": flight.get("number_of_changes", 1) == 0,
        })
    days.sort(key=lambda d: d["date"])

    cheapest = min(days, key=lambda d: d["price"]) if days else None

    return {
        "success": True,
        "origin": origin,
        "destination": destination,
        "currency": currency.upper(),
        "days": days,
        "cheapest_day": cheapest,
        "cached": data.get("_cached", False),
    }


@app.post("/sosyal/travel/flight_click")
async def flight_click(request: Request):
    body = await request.json()
    origin      = str(body.get("origin", "")).upper()
    destination = str(body.get("destination", "")).upper()
    depart_date = body.get("depart_date", "")
    return_date = body.get("return_date", "")
    user_id     = str(body.get("user_id", "guest"))

    url = build_affiliate_link(origin, destination, depart_date, return_date)

    try:
        r = await get_redis()
        log_key = f"sos:clicks:flight:{datetime.now().strftime('%Y%m%d')}"
        await r.incr(log_key)
        await r.expire(log_key, 60 * 60 * 24 * 90)
    except Exception:
        pass

    return {
        "success": True,
        "url": url,
        "origin": origin,
        "destination": destination,
    }


@app.post("/sosyal/travel/flight_parse")
async def flight_parse(request: Request):
    body = await request.json()
    query = body.get("query", "")

    if not query:
        return {"success": False, "error": "query gerekli"}

    today = datetime.now().strftime("%Y-%m-%d")

    system = f"""Sen uçuş arama isteğini parse eden bir asistansın.
Bugünün tarihi: {today}.

Kullanıcı bir uçuş tarifi yazar. Senin görevin şu bilgileri JSON olarak çıkarmak:

{{
  "origin": "IATA kodu (IST, SAW, ESB, AYT, ADB vs.)",
  "destination": "IATA kodu",
  "depart_date": "YYYY-MM-DD formatında (belirsizse YYYY-MM yeter)",
  "return_date": "YYYY-MM-DD formatında (tek yön ise boş)",
  "passengers": 1,
  "one_way": false
}}

Türkiye şehirleri IATA: İstanbul=IST (SAW), Ankara=ESB, İzmir=ADB, Antalya=AYT,
  Bodrum=BJV, Dalaman=DLM, Trabzon=TZX, Gaziantep=GZT, Kayseri=ASR,
  Konya=KYA, Adana=ADA, Malatya=MLX, Samsun=SZF, Van=VAN

Yurt dışı: Londra=LON, Paris=PAR, Roma=ROM, Amsterdam=AMS, Berlin=BER,
  Madrid=MAD, Dubai=DXB, New York=NYC, Tokyo=TYO

Ay: Ocak=01...Aralık=12. "Mayıs sonu" → 25-30. "Mayıs başı" → 01-10.

SADECE JSON döndür, açıklama yazma."""

    reply = await smart_llm(
        [{"role": "user", "content": query}],
        system, max_tokens=300, temperature=0.2,
    )

    try:
        cleaned = reply.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        return {"success": True, "parsed": parsed, "raw_query": query}
    except Exception as e:
        return {"success": False, "error": "parse_failed", "raw_reply": reply[:200]}


# ══════════════════════════════════════════════════════
# AUTOCOMPLETE — Şehir / Havalimanı arama
# ══════════════════════════════════════════════════════

@app.get("/sosyal/travel/places")
async def places_autocomplete(term: str = "", locale: str = "tr", types: str = "city,airport"):
    if len(term) < 2:
        return {"success": True, "results": []}

    cache_key = f"tp:autocomplete:{term.lower()}:{locale}:{types}"
    cached = await redis_get_json(cache_key)
    if cached:
        return cached

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            type_list = [t.strip() for t in types.split(",") if t.strip()]
            url = "https://autocomplete.travelpayouts.com/places2"
            query_string = "&".join([f"types[]={t}" for t in type_list])
            full_url = f"{url}?term={term}&locale={locale}&{query_string}"

            r = await client.get(full_url)
            if r.status_code != 200:
                return {"success": False, "error": f"HTTP {r.status_code}", "results": []}

            data = r.json()
            results = []
            for item in data if isinstance(data, list) else []:
                if item.get("type") == "city":
                    results.append({
                        "type": "city",
                        "code": item.get("code", ""),
                        "name": item.get("name", ""),
                        "country_name": item.get("country_name", ""),
                        "country_code": item.get("country_code", ""),
                        "main_airport_name": item.get("main_airport_name"),
                        "coordinates": item.get("coordinates"),
                        "weight": item.get("weight", 0),
                    })
                elif item.get("type") == "airport":
                    results.append({
                        "type": "airport",
                        "code": item.get("code", ""),
                        "name": item.get("name", ""),
                        "city_code": item.get("city_code", ""),
                        "city_name": item.get("city_name", ""),
                        "country_name": item.get("country_name", ""),
                        "country_code": item.get("country_code", ""),
                        "coordinates": item.get("coordinates"),
                        "weight": item.get("weight", 0),
                    })
                elif item.get("type") == "country":
                    results.append({
                        "type": "country",
                        "code": item.get("code", ""),
                        "name": item.get("name", ""),
                    })

            results.sort(key=lambda x: x.get("weight", 0), reverse=True)
            results = results[:15]

            output = {"success": True, "results": results}
            await redis_set_json(cache_key, output, 60 * 60 * 24)
            return output

    except Exception as e:
        print(f"[AUTOCOMPLETE] {e}")
        return {"success": False, "error": str(e), "results": []}

# ══════════════════════════════════════════════════════
# DESTINATION BİLGİSİ — AI + Görsel
# ══════════════════════════════════════════════════════

@app.post("/sosyal/travel/destination_info")
async def destination_info(request: Request):
    body = await request.json()
    city    = body.get("city", "")
    country = body.get("country", "")
    code    = body.get("code", "")

    if not city:
        return {"success": False, "error": "city gerekli"}

    cache_key = f"sos:dest:{city.lower()}:{country.lower()}"
    cached = await redis_get_json(cache_key)
    if cached:
        return cached

    system = """Sen deneyimli bir seyahat yazarısın. Türkçe, sıcak, pratik destinasyon bilgileri veriyorsun.
JSON formatında döndüreceksin, sadece JSON — başka açıklama YOK."""

    prompt = f"""{city}{', '+country if country else ''} hakkında JSON üret:

{{
  "summary": "Şehri 2-3 cümlede özetle — ne için meşhur, hangi hissi veriyor",
  "highlights": [
    {{"name": "Yer adı", "desc": "Kısa tanım (1 cümle)", "emoji": "📍"}}
  ],
  "best_time": "En iyi ziyaret mevsimi ve sebebi (1-2 cümle)",
  "local_tip": "Sadece yerel birisinin bileceği bir ipucu (1 cümle)",
  "budget": {{"class": "ucuz/orta/pahalı", "daily_usd": 120, "note": "Günlük ortalama"}},
  "must_try_food": "3-4 yerel yemek, virgülle ayır",
  "language_tips": "Dil + temel selamlaşma (Merhaba=???, Teşekkürler=???)"
}}

SADECE JSON DÖNDÜR. Markdown kullanma."""

    reply = await smart_llm(
        [{"role": "user", "content": prompt}],
        system, max_tokens=1000, temperature=0.6,
    )

    info = None
    try:
        cleaned = reply.strip().replace("```json", "").replace("```", "").strip()
        info = json.loads(cleaned)
    except:
        info = {
            "summary": reply[:300],
            "highlights": [],
            "best_time": "",
            "local_tip": "",
            "budget": {"class": "orta", "daily_usd": 100, "note": ""},
        }

    image_url = await fetch_wikipedia_image(city, country)

    result = {
        "success": True,
        "city": city,
        "country": country,
        "code": code,
        "info": info,
        "image_url": image_url,
    }

    await redis_set_json(cache_key, result, 60 * 60 * 24 * 7)
    return result


async def fetch_wikipedia_image(city: str, country: str = "") -> str:
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            for lang in ["tr", "en"]:
                url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{city}"
                r = await client.get(url, headers={"User-Agent": "ONE-BUNE/1.0"})
                if r.status_code == 200:
                    data = r.json()
                    thumb = data.get("thumbnail", {}).get("source", "")
                    orig  = data.get("originalimage", {}).get("source", "")
                    if orig or thumb:
                        return orig or thumb
    except Exception as e:
        print(f"[WIKI IMAGE] {e}")
    return ""


# ══════════════════════════════════════════════════════
# AYNI GÜN TÜM UÇUŞLAR
# ══════════════════════════════════════════════════════

@app.post("/sosyal/travel/flights_same_day")
async def flights_same_day(request: Request):
    body = await request.json()
    origin      = str(body.get("origin", "")).upper()
    destination = str(body.get("destination", "")).upper()
    date        = body.get("date", "")
    currency    = body.get("currency", "try")

    if not origin or not destination or not date:
        return {"success": False, "error": "origin, destination ve date zorunlu"}

    params_direct = {
        "origin": origin, "destination": destination,
        "depart_date": date, "currency": currency,
    }
    data = await tp_request("/v1/prices/direct", params_direct)

    flights = []
    if data.get("success"):
        raw = data.get("data", {}).get(destination, {})
        for k, f in raw.items():
            flights.append({**format_flight(f, origin, destination), "is_direct": True})

    params_cheap = {
        "origin": origin, "destination": destination,
        "depart_date": date, "currency": currency,
    }
    data2 = await tp_request("/v1/prices/cheap", params_cheap)

    if data2.get("success"):
        raw2 = data2.get("data", {}).get(destination, {})
        for k, f in raw2.items():
            flight = format_flight(f, origin, destination)
            if not any(existing["flight_number"] == flight["flight_number"] for existing in flights):
                flights.append({**flight, "is_direct": f.get("number_of_changes", 1) == 0})

    flights.sort(key=lambda f: f.get("depart_time", "00:00"))

    return {
        "success": True,
        "origin": origin,
        "destination": destination,
        "date": date,
        "currency": currency.upper(),
        "flights": flights,
        "total": len(flights),
    }


# ══════════════════════════════════════════════════════
# KAYITLI SEYAHAT PLANLARI (CRUD)
# ══════════════════════════════════════════════════════

@app.post("/sosyal/travel/trip_save")
async def trip_save(request: Request):
    """Yeni plan kaydet veya mevcut planı güncelle."""
    body    = await request.json()
    user_id = str(body.get("user_id", "guest"))
    trip_id = body.get("id", "")

    if user_id == "guest":
        return {"success": False, "error": "Kaydetmek için giriş gerekli"}

    trips = await get_saved_trips(user_id)

    # Güncelleme mi yeni mi?
    if trip_id:
        # Güncelle
        found = False
        for i, t in enumerate(trips):
            if t.get("id") == trip_id:
                trips[i] = {
                    **t,
                    "from_code":    body.get("from_code", t.get("from_code", "")),
                    "from_name":    body.get("from_name", t.get("from_name", "")),
                    "from_country": body.get("from_country", t.get("from_country", "")),
                    "to_code":      body.get("to_code", t.get("to_code", "")),
                    "to_name":      body.get("to_name", t.get("to_name", "")),
                    "to_country":   body.get("to_country", t.get("to_country", "")),
                    "depart_date":  body.get("depart_date", t.get("depart_date", "")),
                    "return_date":  body.get("return_date", t.get("return_date", "")),
                    "one_way":      bool(body.get("one_way", t.get("one_way", False))),
                    "passengers":   int(body.get("passengers", t.get("passengers", 1))),
                    "notes":        body.get("notes", t.get("notes", "")),
                    "pinned":       bool(body.get("pinned", t.get("pinned", False))),
                    "updated_at":   datetime.now(timezone.utc).isoformat(),
                }
                found = True
                break
        if not found:
            return {"success": False, "error": "Plan bulunamadı"}
    else:
        # Yeni
        trip_id = str(uuid.uuid4())[:12]
        new_trip = {
            "id": trip_id,
            "from_code":    body.get("from_code", ""),
            "from_name":    body.get("from_name", ""),
            "from_country": body.get("from_country", ""),
            "to_code":      body.get("to_code", ""),
            "to_name":      body.get("to_name", ""),
            "to_country":   body.get("to_country", ""),
            "depart_date":  body.get("depart_date", ""),
            "return_date":  body.get("return_date", ""),
            "one_way":      bool(body.get("one_way", False)),
            "passengers":   int(body.get("passengers", 1)),
            "notes":        body.get("notes", ""),
            "pinned":       bool(body.get("pinned", False)),
            "created_at":   datetime.now(timezone.utc).isoformat(),
            "updated_at":   datetime.now(timezone.utc).isoformat(),
        }
        trips.append(new_trip)

    # Sabitlenenler önce, sonra güncellenme tarihi (yeni → eski)
    trips.sort(key=lambda t: (not t.get("pinned", False), t.get("updated_at", "")), reverse=False)
    pinned_first = [t for t in trips if t.get("pinned")]
    rest         = sorted([t for t in trips if not t.get("pinned")], key=lambda t: t.get("updated_at", ""), reverse=True)
    trips = pinned_first + rest

    # Max 50 plan tut
    trips = trips[:50]

    await set_saved_trips(user_id, trips)

    saved = next((t for t in trips if t.get("id") == trip_id), None)
    return {"success": True, "trip": saved}


@app.get("/sosyal/travel/trips/{user_id}")
async def trip_list(user_id: str):
    trips = await get_saved_trips(user_id)
    # Kullanıcı her listelemesinde TTL yenilenir → aktif kullanıcı için planlar kalıcı
    if trips:
        await set_saved_trips(user_id, trips)
    return {"success": True, "trips": trips, "total": len(trips)}


@app.post("/sosyal/travel/trip_delete")
async def trip_delete(request: Request):
    body    = await request.json()
    user_id = str(body.get("user_id", "guest"))
    trip_id = body.get("id", "")

    if not trip_id:
        return {"success": False, "error": "id gerekli"}

    trips = await get_saved_trips(user_id)
    before = len(trips)
    trips = [t for t in trips if t.get("id") != trip_id]

    if len(trips) == before:
        return {"success": False, "error": "Plan bulunamadı"}

    await set_saved_trips(user_id, trips)
    return {"success": True, "deleted_id": trip_id}


@app.post("/sosyal/travel/trip_note")
async def trip_note(request: Request):
    """Plana not ekle/güncelle."""
    body    = await request.json()
    user_id = str(body.get("user_id", "guest"))
    trip_id = body.get("id", "")
    notes   = body.get("notes", "")

    if not trip_id:
        return {"success": False, "error": "id gerekli"}

    trips = await get_saved_trips(user_id)
    for t in trips:
        if t.get("id") == trip_id:
            t["notes"] = notes
            t["updated_at"] = datetime.now(timezone.utc).isoformat()
            await set_saved_trips(user_id, trips)
            return {"success": True, "trip": t}

    return {"success": False, "error": "Plan bulunamadı"}

# ══════════════════════════════════════════════════════
# AI SEYAHAT PLANLAYICI (Trip Planner)
# ══════════════════════════════════════════════════════

@app.post("/sosyal/travel/trip_planner")
async def trip_planner(request: Request):
    """
    Varış şehri için AI günlük plan üretir.
    Body: { destination_city, destination_country, days, passengers, style }
    """
    body = await request.json()
    city       = body.get("destination_city", "")
    country    = body.get("destination_country", "")
    days       = int(body.get("days", 3))
    passengers = int(body.get("passengers", 1))
    style      = body.get("style", "dengeli")

    if not city:
        return {"success": False, "error": "destination_city gerekli"}

    days = max(1, min(7, days))  # 7 gün max (JSON çok büyüme sorunu için)

    cache_key = f"sos:trip_plan:{city.lower()}:{country.lower()}:{days}:{style}:{passengers}"
    cached = await redis_get_json(cache_key)
    if cached:
        cached["_cached"] = True
        return cached

    system = """Sen deneyimli bir seyahat planlayıcısın. Türkçe, pratik, günlük plan üretirsin.
SADECE GEÇERLİ JSON döndür. Açıklama yazma, markdown kullanma, ```json bloğu koyma."""

    prompt = f"""{city}{', '+country if country else ''} için {days} günlük seyahat planı üret.
Yolcu sayısı: {passengers}
Stil: {style}

JSON şeması (TAM şuna uygun döndür):
{{
  "overview": "Plan özeti 1 cümle",
  "plan": [
    {{
      "day_num": 1,
      "title": "Kısa başlık",
      "activities": [
        {{"time":"09:00","icon":"🏛","title":"Kısa başlık","desc":"1 cümle","tip":"Kısa ipucu"}}
      ]
    }}
  ],
  "must_do": ["Madde 1","Madde 2","Madde 3"],
  "food_to_try": ["Yemek 1","Yemek 2","Yemek 3"],
  "what_to_pack": ["Eşya 1","Eşya 2"],
  "local_tips": ["İpucu 1","İpucu 2"],
  "avoid": ["Kaçınılacak 1"],
  "estimated_budget": {{"per_day_usd":80,"breakdown":"Konaklama ~30$, yemek ~25$, ulaşım ~15$, aktivite ~10$"}}
}}

KURALLAR:
- Her günde 4 aktivite (sabah/öğle/öğleden sonra/akşam)
- Tüm metinler KISA olsun (desc max 15 kelime, tip max 10 kelime)
- Çift tırnak içinde çift tırnak KULLANMA — apostrof kullan
- JSON'u EKSİKSİZ tamamla, yarıda kesme
- SADECE JSON, başka hiçbir şey yazma"""

    reply = await smart_llm(
        [{"role": "user", "content": prompt}],
        system,
        max_tokens=4000,
        temperature=0.6,
    )

    parsed = _safe_json_parse(reply)

    if not parsed:
        print(f"[TRIP PLANNER] parse failed tamamen. Raw (first 500): {reply[:500]}")
        return {
            "success": False,
            "error": "plan_parse_failed",
            "raw_reply": reply[:300],
            "hint": "AI yanıtı JSON olarak parse edilemedi. Tekrar dene.",
        }

    result = {
        "success": True,
        "city": city,
        "country": country,
        "days": days,
        "style": style,
        **parsed,
    }

    await redis_set_json(cache_key, result, 60 * 60 * 24 * 3)
    return result


def _safe_json_parse(text: str):
    """AI JSON çıktısını toleranslı şekilde parse eder."""
    if not text:
        return None

    # ```json ``` blokları temizle
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.replace("```", "").strip()

    # 1. Direkt parse dene
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 2. İlk { 'dan son } 'a kadar al
    try:
        start = cleaned.find("{")
        end   = cleaned.rfind("}")
        if start >= 0 and end > start:
            candidate = cleaned[start:end+1]
            return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # 3. Yarım kesilmişse — dengeli parantezlere kadar trunc
    try:
        start = cleaned.find("{")
        if start < 0:
            return None
        depth = 0
        last_valid = -1
        in_string = False
        escape_next = False
        for i, ch in enumerate(cleaned[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    last_valid = i
                    break
        if last_valid > 0:
            return json.loads(cleaned[start:last_valid+1])
    except (json.JSONDecodeError, IndexError):
        pass

    # 4. Bozuk tırnak temizle ve son } kadar kes
    try:
        start = cleaned.find("{")
        end   = cleaned.rfind("}")
        if start >= 0 and end > start:
            candidate = cleaned[start:end+1]
            # Trailing comma'ları temizle
            import re as _re
            candidate = _re.sub(r",(\s*[}\]])", r"\1", candidate)
            return json.loads(candidate)
    except (json.JSONDecodeError, Exception):
        pass

    return None


# Geriye uyumluluk için eski /itinerary endpoint'i (frontend'in eski action'ı)
@app.post("/sosyal/travel/itinerary")
async def itinerary_legacy(request: Request):
    """Eski /itinerary çağrısı — yeni /trip_planner'a yönlendir."""
    body = await request.json()
    # Eski formatı yeni formata çevir
    mapped = {
        "destination_city": body.get("destination") or body.get("destination_city", ""),
        "destination_country": body.get("country") or body.get("destination_country", ""),
        "days": body.get("days", 3),
        "passengers": body.get("passengers", 1),
        "style": body.get("travel_style") or body.get("style", "dengeli"),
    }

    # Manuel call
    class FakeRequest:
        async def json(self):
            return mapped
    return await trip_planner(FakeRequest())


# ══════════════════════════════════════════════════════
# HAVA DURUMU TAHMİNİ — Vertex AI + Open-Meteo
# ══════════════════════════════════════════════════════

VERTEX_PROJECT  = os.getenv("GEMINI_PROJECT", "")
VERTEX_LOCATION = os.getenv("GEMINI_LOCATION", "us-central1")
VERTEX_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
VERTEX_SA_KEY   = "/etc/vertex-sa/key.json"
USE_VERTEX      = bool(VERTEX_PROJECT)
_vtok: dict = {"token": "", "expires": 0}

async def get_vertex_token() -> str:
    import json, time
    now = time.time()
    if _vtok["token"] and now < _vtok["expires"] - 60:
        return _vtok["token"]
    try:
        import jwt as pyjwt
        with open(VERTEX_SA_KEY) as f:
            sa = json.load(f)
        claim = {
            "iss": sa["client_email"], "sub": sa["client_email"],
            "aud": "https://oauth2.googleapis.com/token",
            "iat": int(now), "exp": int(now) + 3600,
            "scope": "https://www.googleapis.com/auth/cloud-platform",
        }
        signed = pyjwt.encode(claim, sa["private_key"], algorithm="RS256")
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://oauth2.googleapis.com/token",
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signed}
            )
            data = r.json()
            t = data["access_token"]
            _vtok["token"] = t
            _vtok["expires"] = int(now) + 3590
            return t
    except Exception as e:
        print(f"[VERTEX TOKEN] {e}")
        return ""

async def smart_llm(messages: list, system: str, max_tokens: int = 600, temperature: float = 0.8) -> str:
    """Vertex AI gemini-2.5-flash-lite — DeepInfra fallback."""
    if not USE_VERTEX:
        return await llm_chat(messages, system, max_tokens, temperature)
    try:
        token = await get_vertex_token()
        if not token:
            return await llm_chat(messages, system, max_tokens, temperature)
        contents = []
        if system:
            contents.append({"role": "user", "parts": [{"text": "System: " + system}]})
            contents.append({"role": "model", "parts": [{"text": "Anladım."}]})
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        url = (
            f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/"
            f"projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}/"
            f"publishers/google/models/{VERTEX_MODEL}:generateContent"
        )
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "contents": contents,
                    "generationConfig": {
                        "maxOutputTokens": max_tokens,
                        "temperature": temperature,
                    }
                }
            )
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"[VERTEX] {r.status_code}: {r.text[:150]}")
    except Exception as e:
        print(f"[VERTEX] {e}")
    return await llm_chat(messages, system, max_tokens, temperature)


WMO_CODES = {
    0:"Açık",1:"Hafif bulutlu",2:"Parçalı bulutlu",3:"Kapalı",
    45:"Sisli",48:"Kırağılı sis",51:"Hafif çiseleme",53:"Çiseleme",55:"Yoğun çiseleme",
    61:"Hafif yağmur",63:"Yağmur",65:"Şiddetli yağmur",
    71:"Hafif kar",73:"Kar",75:"Yoğun kar",77:"Kar tanesi",
    80:"Hafif sağanak",81:"Sağanak",82:"Şiddetli sağanak",
    85:"Kar yağışı",86:"Yoğun kar yağışı",
    95:"Fırtına",96:"Fırtına/dolu",99:"Şiddetli fırtına"
}

def wmo_icon(code: int) -> str:
    if code == 0: return "01d"
    if code in [1,2]: return "02d"
    if code == 3: return "04d"
    if code in [45,48]: return "50d"
    if code in [51,53,55,61,63]: return "10d"
    if code == 65: return "09d"
    if code in [71,73,75,77,85,86]: return "13d"
    if code in [80,81,82]: return "09d"
    if code in [95,96,99]: return "11d"
    return "03d"

async def fetch_open_meteo(lat: float, lon: float, days: int = 7) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,weathercode,precipitation_sum,windspeed_10m_max,precipitation_probability_max",
                    "current_weather": "true",
                    "timezone": "auto",
                    "forecast_days": days
                }
            )
            if r.status_code != 200:
                return {}
            d = r.json()
            current_raw = d.get("current_weather", {})
            daily = d.get("daily", {})
            dates = daily.get("time", [])

            current = {
                "temp": round(current_raw.get("temperature", 0)),
                "feels_like": round(current_raw.get("temperature", 0) - 2),
                "humidity": 60,
                "description": WMO_CODES.get(int(current_raw.get("weathercode", 0)), "Bilinmiyor"),
                "icon": wmo_icon(int(current_raw.get("weathercode", 0))),
                "wind_speed": round(current_raw.get("windspeed", 0)),
            }

            forecast = []
            for i, date in enumerate(dates):
                code = int(daily["weathercode"][i]) if i < len(daily.get("weathercode",[])) else 0
                forecast.append({
                    "date": date,
                    "temp_min": round(daily["temperature_2m_min"][i]) if i < len(daily.get("temperature_2m_min",[])) else 0,
                    "temp_max": round(daily["temperature_2m_max"][i]) if i < len(daily.get("temperature_2m_max",[])) else 0,
                    "temp_avg": round((daily["temperature_2m_min"][i]+daily["temperature_2m_max"][i])/2) if i < len(daily.get("temperature_2m_min",[])) else 0,
                    "description": WMO_CODES.get(code, "Bilinmiyor"),
                    "icon": wmo_icon(code),
                    "humidity": 60,
                    "wind_speed": round(daily["windspeed_10m_max"][i]) if i < len(daily.get("windspeed_10m_max",[])) else 0,
                    "rain_chance": round(daily["precipitation_probability_max"][i]) if i < len(daily.get("precipitation_probability_max",[])) else 0,
                    "rain_mm": round(daily["precipitation_sum"][i], 1) if i < len(daily.get("precipitation_sum",[])) else 0,
                })
            return {"current": current, "forecast": forecast}
    except Exception as e:
        print(f"[OPEN-METEO] {e}")
        return {}


@app.get("/sosyal/travel/weather-forecast")
async def get_weather_forecast(city: str, country: str = "", lat: float = 0, lon: float = 0, days: int = 7):
    result = {"city": city, "country": country, "forecast": [], "current": None, "source": "open-meteo"}
    if lat and lon:
        data = await fetch_open_meteo(lat, lon, min(days, 7))
        if data:
            result["current"]  = data.get("current")
            result["forecast"] = data.get("forecast", [])
    if not result["forecast"]:
        system = "Sen meteoroloji uzmanısın. Türkçe yanıt ver."
        prompt = f"{city}, {country} için mevsimsel hava tahmini: sıcaklık aralığı, yağış, giyim önerisi (3 cümle)."
        result["ai_forecast"] = await smart_llm([{"role":"user","content":prompt}], system, max_tokens=200)
    return result


@app.get("/sosyal/travel/weather-date")
async def get_weather_for_date(city: str, country: str = "", lat: float = 0, lon: float = 0, target_date: str = ""):
    from datetime import datetime
    result = {"city": city, "date": target_date, "forecast": None, "ai_forecast": None}
    if not target_date: return result
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        diff_days  = (target_dt - datetime.now()).days
    except:
        return result
    if 0 <= diff_days <= 6 and lat and lon:
        data = await fetch_open_meteo(lat, lon, 7)
        if data:
            day = next((f for f in data.get("forecast",[]) if f["date"]==target_date), None)
            if day:
                result["forecast"] = {**day, "source": "open-meteo"}
    if not result["forecast"]:
        month_tr = ["","Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
        m = month_tr[target_dt.month]
        prompt = f"{city}, {country} - {m} ayı tipik hava: sıcaklık, yağış, öneri (2 cümle, Türkçe)."
        result["ai_forecast"] = await smart_llm([{"role":"user","content":prompt}], "Meteoroloji uzmanısın. Kısa Türkçe.", max_tokens=120)
    return result