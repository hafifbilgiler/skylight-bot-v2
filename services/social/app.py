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
TP_CACHE_TTL    = 60 * 60  # 1 saat — cache'den geldiği için bu yeterli

# ═══════════════════════════════════════════════════════
# REDIS STORE
# ═══════════════════════════════════════════════════════
# Key şeması (namespace: sos:):
#   sos:u:{user_id}:travel_plans      → JSON list   TTL 30 gün
#   sos:u:{user_id}:companion_history → JSON list   TTL 7 gün
#   sos:u:{user_id}:companion_mood    → JSON list   TTL 30 gün
#   sos:u:{user_id}:tasks             → JSON dict   TTL 30 gün
#   sos:u:{user_id}:habits            → JSON list   TTL 30 gün
# ═══════════════════════════════════════════════════════

TRAVEL_TTL    = 60 * 60 * 24 * 30   # 30 gün
COMPANION_TTL = 60 * 60 * 24 * 7    # 7 gün
DAILY_TTL     = 60 * 60 * 24 * 30   # 30 gün
MOOD_TTL      = 60 * 60 * 24 * 30   # 30 gün

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
    plans = plans[-10:]   # son 10
    await redis_set_json(_k(user_id, "travel_plans"), plans, TRAVEL_TTL)

async def get_companion_history_raw(user_id: str) -> list:
    return await redis_get_json(_k(user_id, "companion_history"), [])

async def push_companion_message(user_id: str, msg: dict):
    hist = await get_companion_history_raw(user_id)
    hist.append(msg)
    hist = hist[-100:]    # son 100 mesaj (≈ 50 turn)
    await redis_set_json(_k(user_id, "companion_history"), hist, COMPANION_TTL)

async def get_companion_moods(user_id: str) -> list:
    return await redis_get_json(_k(user_id, "companion_mood"), [])

async def push_companion_mood(user_id: str, mood_entry: dict):
    moods = await get_companion_moods(user_id)
    moods.append(mood_entry)
    moods = moods[-30:]
    await redis_set_json(_k(user_id, "companion_mood"), moods, MOOD_TTL)

async def get_daily_tasks(user_id: str) -> dict:
    return await redis_get_json(_k(user_id, "tasks"), {"todo": [], "doing": [], "done": []})

async def set_daily_tasks(user_id: str, tasks: dict):
    await redis_set_json(_k(user_id, "tasks"), tasks, DAILY_TTL)

async def get_daily_habits(user_id: str) -> list:
    return await redis_get_json(_k(user_id, "habits"), [])

async def set_daily_habits(user_id: str, habits: list):
    await redis_set_json(_k(user_id, "habits"), habits, DAILY_TTL)


@app.on_event("startup")
async def startup():
    # Redis'e bağlan, warmup
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
# DERT ORTAĞI  —  YENİ SİSTEM PROMPT v2.0
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
   Kullanıcı bir şey söyleyince, önce o hissi kelimeye dök.
   "Gerçekten yorulmuşsun anlıyorum..."
   "Bu söylediğin ağır bir şey..."
   "Öfkelenmen çok normal görünüyor..."

2) HER MESAJDA SORU SORMA.
   Sürekli soru sormak dert ortağını sorgu hakimine çevirir.
   Kural: Her 3 mesajın en fazla 1'inde soru sor.
   Diğerlerinde: yansıt, destekle, yanında ol.
   Bazen sadece "seni duyuyorum" demek yeterli.

3) KISA KAL, ama KURU OLMA.
   2-4 cümle ideal. Uzun paragraflar yazma.
   Kullanıcı anlatırken hızlı sözünü kesme — önce tam dinlemişsin hissini ver.

4) SUSMAYI BİL.
   Bazen kullanıcı sadece duyulmak ister. Çözüm önerme.
   "Burası zor bir an. Yanındayım." yeterli olabilir.

5) SOMUT OL, GENELLEMEDEN KAÇIN.
   ❌ "İnsanlar bazen böyle hisseder" (genelleme)
   ✓ "Bu tarifin — kendini yalnız hissediş — çok tanıdık geldi"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YAPMADIKLARIN (ASLA)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✗ "Her şey yoluna girecek" gibi boş iyimserlik
✗ "Bu çok üzücü" gibi klişe teselli
✗ "Nasıl hissediyorsun?" peş peşe birden fazla
✗ Madde madde liste (bu bir SOHBET, rapor değil)
✗ Uzman ayak satma ("psikolojik olarak şöyle oluyor...")
✗ Sürekli "profesyonel destek al" demesi
✗ Kullanıcının sözünü düzeltme, yorum katma
✗ Emoji yağmuru (bazen 1 tane yeter, çoğu zaman hiç)
✗ Büyük harf, ünlem işareti bombardımanı

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YAPTIKLARIN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✓ Duyguyu yansıt → destekle → gerekirse soru
✓ Kısa, sıcak, doğal Türkçe
✓ Bazen kendi küçük tecrübelerinden bahset ("Benim bir dönem böyle olmuştu...")
✓ Pratik ipucu ver AMA dayatmadan: "Belki şey işe yarar, bakarız"
✓ Sessiz destekleri kullan: "Oradayım, acele yok"
✓ Kullanıcının kelimelerini geri yansıt — aynen değil, dokunarak

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KRİTİK UYARI — SADECE BU DURUMLARDA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Kullanıcı aşağıdaki ifadeleri kullanırsa NAZİKÇE yönlendir:
  - "kendime zarar", "kendimi öldürmek", "dayanamıyorum artık", "ölmek istiyorum"
    → Önce yanında olduğunu söyle, sonra 182 (Psikiyatri Danışma Hattı)
  - İstismar, şiddet bahsi
    → 183 (Sosyal Destek Hattı), 155 (Polis)

Bu ifadeler OLMADAN asla telefon numarası verme, "uzman ara" deme.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ÖRNEK DİYALOGLAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Kullanıcı: "Bugün yine işte patladım, sinirlerim çok bozuldu"
Sen: "Of, bugün gerçekten yıpratıcı olmuş. Patlayacak kadar birikmişse,
     çok şey birikmiş demektir. İstersen biraz boşal, dinlerim."

Kullanıcı: "Annemle sürekli kavga ediyoruz, yoruldum"
Sen: "Anne kavgaları en zoru — çünkü en yakın olmak istediğin insanla
     çatışıyorsun. Ne zamandır böyle?"

Kullanıcı: "Kimse beni anlamıyor"
Sen: "Duydum seni. Anlaşılmamak, insanın içinde sessiz bir yalnızlık
     bırakır. Buradayım şu an."

Kullanıcı: "Arkadaşım beni terk etti"
Sen: "Ah, bu sert. Yakın biri kaybı — adını koymak bile zor bazen.
     Ne kadar zamandır yaşıyorsun bunu?"

Kullanıcı: "Kendimi öldürmek istiyorum"
Sen: "Durakla bir saniye. Bu söylediğin şu an gerçekten ağır bir his —
     ve sen yalnız değilsin. 182 Psikiyatri Danışma Hattı'nı aramanı
     çok istiyorum şimdi; 7/24 açık, doğrudan biri dinler.
     Ben de buradayım, konuşmak istersen."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Şimdi gerçekten dinle. Acele yok.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


@app.post("/sosyal/companion/chat")
async def companion_chat(request: Request):
    body     = await request.json()
    raw_token = body.get("token") or body.get("user_id") or "anonymous"
    user_id   = hashlib.sha256(str(raw_token).encode()).hexdigest()[:16]
    message  = body.get("message", "")
    history  = body.get("history", [])
    mood     = body.get("mood", "")

    # Mood bilgisini system prompt'a context olarak ekle
    system = COMPANION_SYSTEM
    if mood:
        system += f"\n\n[Kullanıcı bu mesajdan önce kendini '{mood}' olarak işaretledi. Bunu biliyorsun ama açıkça söylemeden konuşmana yansıt.]"

    # History uzunluğu: 20 mesaj (10 turn) — daha iyi bağlam
    trimmed_history = history[-20:] if len(history) > 20 else history

    reply = await smart_llm(
        trimmed_history + [{"role": "user", "content": message}],
        system,
        max_tokens=400,   # 300 → 400: daha doğal cevaplar için
    )

    # Redis'e kaydet
    now_ts = time.time()
    await push_companion_message(user_id, {"role": "user",      "content": message, "ts": now_ts})
    await push_companion_message(user_id, {"role": "assistant", "content": reply,   "ts": now_ts})

    # Mood analizi
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
# Rate limit: 10 req/sec (Redis cache ile altına iniyoruz)
# Cache: 1 saat (veri zaten Aviasales cache'inden geliyor, 7 günlük)
# ══════════════════════════════════════════════════════

# Havayolu kod → isim map (görünür hale getirmek için)
AIRLINE_NAMES = {
    "TK": "Turkish Airlines",
    "PC": "Pegasus",
    "VF": "AJet",
    "XQ": "SunExpress",
    "LH": "Lufthansa",
    "BA": "British Airways",
    "AF": "Air France",
    "KL": "KLM",
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "TB": "TUI",
    "FR": "Ryanair",
    "W6": "Wizz Air",
    "U2": "easyJet",
    "AZ": "ITA Airways",
    "IB": "Iberia",
    "OS": "Austrian",
    "SU": "Aeroflot",
}

def airline_name(code: str) -> str:
    return AIRLINE_NAMES.get(code, code)


async def tp_request(endpoint: str, params: dict) -> dict:
    """Travelpayouts API wrapper — redis cache'li."""
    if not TP_API_TOKEN:
        return {"success": False, "error": "TP_API_TOKEN tanımsız"}

    # Cache key
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
                # 1 saat cache
                await redis_set_json(cache_key, data, TP_CACHE_TTL)
                return data
            return {"success": False, "error": f"HTTP {r.status_code}", "detail": r.text[:200]}
    except Exception as e:
        print(f"[TP] {endpoint} error: {e}")
        return {"success": False, "error": str(e)}


def build_affiliate_link(
    origin: str,
    destination: str,
    depart_date: str = "",
    return_date: str = "",
) -> str:
    """
    Aviasales deep-link üretir — marker ID otomatik eklenir.
    Format örneği:
      https://www.aviasales.com/search/IST28052026LON05062026?marker=716107
    """
    def fmt(d: str) -> str:
        # "2026-05-28" → "2805"
        if not d or len(d) < 10:
            return ""
        return d[8:10] + d[5:7]

    search_code = f"{origin.upper()}{fmt(depart_date)}{destination.upper()}"
    if return_date:
        search_code += fmt(return_date)

    url = f"https://www.aviasales.com/search/{search_code}?marker={TP_MARKER_ID}"
    return url


def format_flight(raw: dict, origin: str, destination: str) -> dict:
    """API yanıtından UI-friendly uçuş objesi üretir."""
    airline_code = raw.get("airline", "")
    dep = raw.get("departure_at", "")
    ret = raw.get("return_at", "")

    # Tarihleri ayır (T'den önce tarih, sonra saat)
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
    """
    En ucuz uçuşları getir.
    Body: { origin, destination, depart_date?, return_date?, currency?, one_way? }
    """
    body = await request.json()
    origin      = str(body.get("origin", "")).upper()
    destination = str(body.get("destination", "")).upper()
    depart_date = body.get("depart_date", "")   # "2026-05" veya "2026-05-28"
    return_date = body.get("return_date", "")
    currency    = body.get("currency", "try")
    one_way     = bool(body.get("one_way", False))

    if not origin or not destination:
        return {"success": False, "error": "origin ve destination zorunlu"}

    params = {
        "origin": origin,
        "destination": destination,
        "currency": currency,
    }
    if depart_date:
        params["depart_date"] = depart_date
    if return_date and not one_way:
        params["return_date"] = return_date

    data = await tp_request("/v1/prices/cheap", params)

    if not data.get("success"):
        return {"success": False, "error": data.get("error", "API hatası"), "flights": []}

    # Yanıt formatı: { "data": { "DESTINATION": { "0": {...}, "1": {...} } } }
    raw_flights = data.get("data", {}).get(destination, {})
    flights = []
    for key, flight in raw_flights.items():
        flights.append(format_flight(flight, origin, destination))

    # Fiyata göre sırala
    flights.sort(key=lambda f: f["price"])

    return {
        "success": True,
        "origin": origin,
        "destination": destination,
        "currency": currency.upper(),
        "flights": flights[:10],   # En ucuz 10 tane
        "cached": data.get("_cached", False),
    }


@app.post("/sosyal/travel/flight_calendar")
async def flight_calendar(request: Request):
    """
    Ay içinde günlük fiyat takvimi.
    Body: { origin, destination, depart_date, currency? }
    depart_date: "2026-05" formatında
    """
    body = await request.json()
    origin      = str(body.get("origin", "")).upper()
    destination = str(body.get("destination", "")).upper()
    depart_date = body.get("depart_date", "")
    currency    = body.get("currency", "try")

    if not origin or not destination or not depart_date:
        return {"success": False, "error": "origin, destination ve depart_date zorunlu"}

    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date,
        "calendar_type": "departure_date",
        "currency": currency,
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

    # En ucuz günü bul
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
    """
    Kullanıcı uçuş kartındaki 'Bileti Al' butonuna tıkladığında
    çağrılır — affiliate URL'yi döner + DB'ye log atar.
    """
    body = await request.json()
    origin      = str(body.get("origin", "")).upper()
    destination = str(body.get("destination", "")).upper()
    depart_date = body.get("depart_date", "")
    return_date = body.get("return_date", "")
    user_id     = str(body.get("user_id", "guest"))

    url = build_affiliate_link(origin, destination, depart_date, return_date)

    # Redis'e click log (analytics için)
    try:
        r = await get_redis()
        log_key = f"sos:clicks:flight:{datetime.now().strftime('%Y%m%d')}"
        await r.incr(log_key)
        await r.expire(log_key, 60 * 60 * 24 * 90)  # 90 gün
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
    """
    Kullanıcının doğal dilde yazdığı isteği Gemini ile parse eder.
    Örn: "İstanbul'dan Londra'ya Mayıs sonu 2 kişi"
    →    { origin: "IST", destination: "LON", depart: "2026-05-28", return: "2026-06-05", passengers: 2 }
    """
    body = await request.json()
    query = body.get("query", "")

    if not query:
        return {"success": False, "error": "query gerekli"}

    today = datetime.now().strftime("%Y-%m-%d")

    system = f"""Sen uçuş arama isteğini parse eden bir asistansın.
Bugünün tarihi: {today}.

Kullanıcı bir uçuş tarifi yazar. Senin görevin şu bilgileri JSON olarak çıkarmak:

{{
  "origin": "IATA kodu (IST, SAW, ESB, AYT, ADB, TZX vs.)",
  "destination": "IATA kodu",
  "depart_date": "YYYY-MM-DD formatında (belirsizse YYYY-MM yeter)",
  "return_date": "YYYY-MM-DD formatında (tek yön ise boş)",
  "passengers": 1,
  "one_way": false
}}

Türkiye şehirleri IATA kodları:
  İstanbul=IST (Pegasus: SAW), Ankara=ESB, İzmir=ADB, Antalya=AYT,
  Bodrum=BJV, Dalaman=DLM, Trabzon=TZX, Gaziantep=GZT, Kayseri=ASR,
  Konya=KYA, Adana=ADA, Malatya=MLX, Samsun=SZF, Van=VAN

Yurt dışı: Londra=LON, Paris=PAR, Roma=ROM, Amsterdam=AMS, Berlin=BER,
  Madrid=MAD, Dubai=DXB, New York=NYC, Tokyo=TYO

Ay adları: Ocak=01, Şubat=02, Mart=03, Nisan=04, Mayıs=05, Haziran=06,
  Temmuz=07, Ağustos=08, Eylül=09, Ekim=10, Kasım=11, Aralık=12.
"Mayıs sonu" → ayın 25-30'u arası seç.
"Mayıs başı" → 01-10.
"Gelecek hafta" → bugün + 7 gün.

SADECE JSON döndür, açıklama yazma."""

    reply = await smart_llm(
        [{"role": "user", "content": query}],
        system,
        max_tokens=300,
        temperature=0.2,
    )

    # JSON parse et
    try:
        # Bazen model ```json ile sarıyor
        cleaned = reply.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        return {"success": True, "parsed": parsed, "raw_query": query}
    except Exception as e:
        return {"success": False, "error": "parse_failed", "raw_reply": reply[:200]}


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
    """Vertex AI gemini-2.5-flash-lite (grounding yok) — DeepInfra fallback."""
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


# ══════════════════════════════════════════════════════
# YEMEK
# ══════════════════════════════════════════════════════

@app.post("/sosyal/food/recipe")
async def get_recipe(request: Request):
    body    = await request.json()
    dish    = body.get("dish", "")
    prefs   = body.get("preferences", "")
    history = body.get("history", [])

    system = """Sen profesyonel bir aşçısın ve yemek yazarısın.
Detaylı, uygulanabilir Türkçe tarifler yazarsın.
Malzeme listelerini gram/ölçü birimleriyle verirsin.
Pişirme süresi, zorluk derecesi ve kalori bilgisi eklersin.
Alternatif malzeme önerileri sunarsın."""

    prompt = f"{dish} tarifi{' ('+prefs+')' if prefs else ''}"
    reply  = await smart_llm(history + [{"role": "user", "content": prompt}], system, max_tokens=800)
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

    reply = await smart_llm([{"role": "user", "content": prompt}], system, max_tokens=400)
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

    reply = await smart_llm(history + [{"role": "user", "content": message}], system, max_tokens=300)

    if tasks:
        await set_daily_tasks(user_id, tasks)

    return {"reply": reply}


@app.get("/sosyal/daily/data/{user_id}")
async def get_daily_data(user_id: str):
    tasks  = await get_daily_tasks(user_id)
    habits = await get_daily_habits(user_id)
    return {"tasks": tasks, "habits": habits}


@app.post("/sosyal/daily/save")
async def save_daily_data(request: Request):
    body    = await request.json()
    user_id = str(body.get("user_id", "guest"))
    if "tasks"  in body: await set_daily_tasks(user_id,  body["tasks"])
    if "habits" in body: await set_daily_habits(user_id, body["habits"])
    return {"status": "ok"}