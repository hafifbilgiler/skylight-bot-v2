import os, json, time, uuid, asyncio, hashlib
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

@app.get("/health")
async def health():
    return {"status": "ok", "service": "sosyal", "users": len(_store)}

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

    user = get_user(user_id)
    plan = {
        "id": str(uuid.uuid4())[:8],
        "city": city, "country": country, "days": days, "budget": budget,
        "plan": reply,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=TRAVEL_TTL)).isoformat(),
    }
    user["travel_plans"].append(plan)
    user["travel_plans"] = user["travel_plans"][-10:]
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

    reply = await smart_llm(history + [{"role": "user", "content": message}], system, max_tokens=400)
    return {"reply": reply, "city": city}


@app.get("/sosyal/travel/plans/{user_id}")
async def get_travel_plans(user_id: str):
    user = get_user(user_id)
    return {"plans": user["travel_plans"]}


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

    # Geçmişe kaydet
    user = get_user(user_id)
    user["companion_history"].append({
        "role": "user", "content": message, "ts": time.time()
    })
    user["companion_history"].append({
        "role": "assistant", "content": reply, "ts": time.time()
    })
    user["companion_history"] = user["companion_history"][-100:]

    # Mood analizi
    detected_mood = await _analyze_mood(message)
    user["companion_mood"].append({"mood": detected_mood, "ts": time.time()})
    user["companion_mood"] = user["companion_mood"][-30:]

    save_user(user_id, user)
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
    user = get_user(real_id)
    cutoff = time.time() - COMPANION_TTL
    recent = [m for m in user["companion_history"] if m.get("ts", 0) > cutoff]
    return {
        "history": recent[-limit:],
        "mood_history": user["companion_mood"][-14:],
        "total": len(recent)
    }


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