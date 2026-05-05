import re
"""
═══════════════════════════════════════════════════════════════
SKYLIGHT CHAT SERVICE — v3.1 (LiveDataRouter entegrasyonu)
═══════════════════════════════════════════════════════════════
v3.0 → v3.1 değişiklikleri:

  ✅ needs_real_time_data() KALDIRILDI — artık yok
  ✅ call_smart_tools() KALDIRILDI — artık yok
  ✅ REAL_TIME_KEYWORDS KALDIRILDI — artık yok
  ✅ Tüm canlı veri kararları smart_tools /classify'a taşındı
  ✅ Tek fonksiyon: call_live_data(query) → /live endpoint
  ✅ format_live_context() → LLM'e hazır format /live'dan gelir

  Kural: Chat servisi asla keyword listesi tutmaz.
         "Bu sorgu için canlı veri lazım mı?" → smart_tools /classify
         "Canlı veriyi getir" → smart_tools /live

  Tüm diğer özellikler aynen korundu:
  ✅ Memory sistemi
  ✅ Thinking display
  ✅ Web synthesis
  ✅ Auto summaries
  ✅ Code context + compression
  ✅ Multi-mode support
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, AsyncGenerator
import httpx
import json
import os
import base64
import asyncpg
import asyncio
import re
from datetime import datetime

from intent_classifier import build_reasoning_hint, get_intent_thinking_steps, Intent, classify_intent
from conversation_state import (
    build_state_context, extract_and_update_state,
    get_workspace, get_version, set_task,
)
from task_builder import build_task, task_to_prompt_hint
from prompts_production import (
    SKYLIGHT_SYSTEM_PROMPT,
    IMAGE_GENERATION_ENHANCEMENT_PROMPT,
)
# Eski isimler için alias — geriye dönük uyumluluk
ASSISTANT_SYSTEM_PROMPT  = SKYLIGHT_SYSTEM_PROMPT
CODE_SYSTEM_PROMPT       = SKYLIGHT_SYSTEM_PROMPT
IT_EXPERT_SYSTEM_PROMPT  = SKYLIGHT_SYSTEM_PROMPT
STUDENT_SYSTEM_PROMPT    = SKYLIGHT_SYSTEM_PROMPT
SOCIAL_SYSTEM_PROMPT     = SKYLIGHT_SYSTEM_PROMPT
CODE_VISION_SYSTEM_PROMPT = SKYLIGHT_SYSTEM_PROMPT

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY",  "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
SMART_TOOLS_URL    = os.getenv("SMART_TOOLS_URL",    "http://skylight-smart-tools:8081")

# ── Gemini — canlı veri / web arama için ──────────────────────
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")      # AI Studio fallback
GEMINI_PROJECT     = os.getenv("GEMINI_PROJECT", "gen-lang-client-0907571701")       # Vertex AI proje ID
GEMINI_LOCATION    = os.getenv("GEMINI_LOCATION", "us-central1")
GEMINI_SA_KEY_PATH = "/etc/vertex-sa/key.json"             # K8s secret mount
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
# Bu live_type'lar → Gemini'ye gider, DeepInfra'ya gitmez
_GEMINI_TYPES = {"web_search", "news", "deep_search", "price_search"}
_FREE_API_TYPES = {"weather", "currency", "crypto", "time"}
# borsa → özel BIST API (daha doğru)

DB_HOST     = os.getenv("DB_HOST",     "postgres")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME",     "skylight_db")
DB_USER     = os.getenv("DB_USER",     "skylight_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SUMMARY_INTERVAL = int(os.getenv("SUMMARY_INTERVAL", "15"))

MODE_CONFIGS = {
    "assistant": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "deepseek-ai/DeepSeek-V4-Flash"),
        "max_tokens":  int(os.getenv("DEEPINFRA_ASSISTANT_MAX_TOKENS", "16384")),
        "temperature": float(os.getenv("DEEPINFRA_ASSISTANT_TEMPERATURE", "0.7")),
        "top_p":       0.9,
        "system_prompt": ASSISTANT_SYSTEM_PROMPT,
        "supports_thinking": True,   # DeepSeek V4 reasoning_effort destekli
    },
    "code": {
        "model":       os.getenv("DEEPINFRA_CODE_MODEL", "deepseek-ai/DeepSeek-V4-Flash"),
        "max_tokens":  int(os.getenv("DEEPINFRA_CODE_MAX_TOKENS", "32000")),
        "temperature": float(os.getenv("DEEPINFRA_CODE_TEMPERATURE", "0.2")),
        "top_p":       0.85,
        "system_prompt":           CODE_SYSTEM_PROMPT,
        "compression_threshold":   20,   # 20 mesajda bir sıkıştır (önceki 12'ydi)
        "large_file_threshold":    2000,
        "supports_thinking": True,
    },
    "it_expert": {
        "model":       os.getenv("DEEPINFRA_IT_EXPERT_MODEL",
                                 os.getenv("DEEPINFRA_ASSISTANT_MODEL", "deepseek-ai/DeepSeek-V4-Flash")),
        "max_tokens":  3500,
        "temperature": 0.5,
        "top_p":       0.88,
        "system_prompt": IT_EXPERT_SYSTEM_PROMPT,
        "supports_thinking": True,
    },
    "student": {
        "model":       os.getenv("DEEPINFRA_STUDENT_MODEL",
                                 os.getenv("DEEPINFRA_ASSISTANT_MODEL", "deepseek-ai/DeepSeek-V4-Flash")),
        "max_tokens":  2500,
        "temperature": 0.8,
        "top_p":       0.9,
        "system_prompt": STUDENT_SYSTEM_PROMPT,
        "supports_thinking": True,
    },
    "social": {
        "model":       os.getenv("DEEPINFRA_SOCIAL_MODEL",
                                 os.getenv("DEEPINFRA_ASSISTANT_MODEL", "deepseek-ai/DeepSeek-V4-Flash")),
        "max_tokens":  2500,
        "temperature": 0.9,
        "top_p":       0.92,
        "system_prompt": SOCIAL_SYSTEM_PROMPT,
        "supports_thinking": True,
    },
}

app = FastAPI(title="Skylight Chat Service", version="3.1.0")
db_pool: Optional[asyncpg.Pool] = None

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

def _strip_internal_tags(text: str) -> str:
    """SSE chunk'tan LLM grounding taglarini temizle."""
    if "[" not in text:
        return text
    # Bracket tag'lari string replace ile temizle
    replacements = [
        ("[WEB ARAŞTIRMA SONUÇLARI]", ""),
        ("[WEB ARAŞTIRMA SONUÇLARI — SADECE BUNLARI KULLAN]", ""),
        ("[/WEB ARAŞTIRMA SONUÇLARI]", ""),
        ("[ARAŞTIRMA SONUÇLARI]", ""),
        ("[/ARAŞTIRMA SONUÇLARI]", ""),
        ("[CANLI VERİ]", ""),
        ("[/CANLI VERİ]", ""),
        ("[Canlı Veri]", ""),
        ("[LIVE DATA]", ""),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    # [NOT: ...] ve benzeri dinamik tag'lar icin basit parser
    import re
    text = re.sub(r"\[NOT:[^\]]{0,200}\]", "", text)
    text = re.sub(r"\[SADECE[^\]]{0,100}\]", "", text)
    return text


async def startup_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            min_size=2, max_size=10, command_timeout=60,
        )
        print("[DB] Connection pool created")
    except Exception as e:
        print(f"[DB ERROR] {e}")
        db_pool = None

@app.on_event("shutdown")
async def shutdown_db():
    global db_pool
    if db_pool:
        await db_pool.close()
    from conversation_state import close_redis
    await close_redis()

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role:    str
    content: str

class ChatRequest(BaseModel):
    prompt:          str
    mode:            str
    user_id:         int
    conversation_id: Optional[str]        = None
    history:         Optional[List[Dict]] = None
    rag_context:     Optional[str]        = None
    context:         Optional[str]        = None
    session_summary: Optional[str]        = None
    file_id:         Optional[str]        = None
    # Görsel analiz
    image_data:      Optional[str]        = None  # base64 encoded image
    image_type:      Optional[str]        = None  # image/png, image/jpeg vs.
    # Smart Router v4 — Gateway'den gelir
    live_type_hint:  Optional[str]        = None
    router_intent:   Optional[str]        = None
    router_thinking: Optional[str]        = None
    user_level:      Optional[str]        = None
    needs_realtime:  Optional[bool]       = None
    # Düşünme Modu — Frontend toggle'dan gelir (none/high/max)
    thinking_mode:   Optional[str]        = "none"

class ThinkingStep(BaseModel):
    emoji:   str
    message: str

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LIVE DATA — Hızlı local detection + Smart Tools veri çekme
#
# KURAL: Detection LOCAL (0ms, network yok)
#        Veri çekme → smart_tools /unified (sadece lazımsa)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Anlık API sinyalleri — smart tools /unified'a gider ────────
_CURRENCY_KW  = ("dolar","euro","eur","usd","gbp","sterlin","pound","jpy","yen","chf",
                 "döviz","doviz","exchange rate","kaç tl","kac tl","tl kaç",
                 "dolar kaç","euro kaç","döviz kuru","kur nedir","dolar kuru","euro kuru")

# Tek kelime döviz — "euro" "dolar" tek başına yazılınca da çalışsın
_CURRENCY_SINGLE = ("dolar","euro","eur","usd","gbp","sterlin","chf","jpy","yen","pound")

# Yeniden veri çekme sinyalleri — "tekrar bak", "yanlış", "güncelle"
_RETRY_KW = ("tekrar bak","yeniden bak","güncelle","yanlış verdin","hatalı",
              "doğru değil","bir daha bak","güncel değil","eski veri")
_WEATHER_KW   = ("hava durumu","havadurumu","hava nasıl","havalar nasıl",
                 "hava kaç derece","sıcaklık","sicaklik","yağmur yağıyor",
                 "kar yağıyor","weather","forecast","bugün hava","yarın hava","derece")
_CRYPTO_KW    = ("bitcoin","btc","ethereum","eth","dogecoin","doge","solana",
                 "kripto","crypto","coin fiyat","bitcoin kaç","ethereum kaç")
_TIME_KW      = ("saat kaç","saat kac","saati kaç","şimdi saat","şu an saat",
                 "bugün ne günü","what time","günün saati")
_NEWS_KW      = ("son haberler","güncel haberler","bugünün haberleri","son dakika",
                 "breaking news","haberleri göster","haberleri getir","haber oku",
                 "gündem ne","today's news","latest news")
_PRICE_KW     = ("kaç lira","fiyatı ne kadar","fiyatı kaç","altın fiyatı",
                 "gram altın","petrol fiyatı","borsa","bist","hisse fiyatı")

# ── Borsa keywords ──────────────────────────────────────────
_BORSA_KW     = (
    "borsa","bist","hisse","teknik analiz","mum analiz","mum grafiği",
    "rsi","macd","bollinger","destek direnç","al sinyali","sat sinyali",
    "hisse analiz","thyao","garan","akbnk","eregl","kchol",
    "btc analiz","eth analiz","kripto analiz",
)
BORSA_URL = os.getenv("BORSA_URL", "http://skylight-borsa:8086")

# ── Güncel bilgi gerektiren sinyaller — deep_search'e gider ────
# Bu sinyaller varsa keyword detection yerine deep_search çağrılır
_DEEP_SEARCH_KW = (
    # Spor
    "maç sonucu","son maç","maçı kaç","playoff","eleme maçı","dünya kupası",
    "şampiyonlar ligi","süper lig","puan durumu","fikstür","transfer",
    "world cup","champions league","premier league","la liga",
    # Güncel karşılaştırma / sıralama / piyasa durumu
    "şu an en iyi","şuanda en iyi","piyasada en iyi","en iyi hangisi",
    "hangi daha iyi","hangisi daha iyi","karşılaştır","comparison",
    "en popüler","en çok kullanılan","en güncel","piyasada ne var",
    "yeni çıkan","son çıkan","2025 en iyi","2026 en iyi",
    "en iyi yapay zeka","en iyi ai","best ai","best llm",
    # Teknoloji güncel
    "son model","en yeni model","yeni sürüm","hangi model daha iyi",
    "chatgpt mi claude mu","gemini mi","hangi ai","ai karşılaştırma",
    # Mevzuat / hukuk / vergi
    "asgari ücret","asgari sermaye","vergi oranı","vergi dilimi",
    "sgk prim","kıdem tazminatı","ihbar tazminatı",
    "limited şirket","anonim şirket","şirket kuruluş",
    "kdv oranı","stopaj oranı","gelir vergisi",
    # Nüfus / istatistik
    "nüfusu kaç","nüfus kaç","kaç kişi yaşıyor",
    "istatistik","tüik","tüfe","enflasyon","işsizlik oranı",
    # Teknoloji / ürün güncelliği
    "son model","en yeni","güncel model","yeni sürüm","son sürüm",
    "hangi model","kaçıncı nesil",
    # Genel güncel soru sinyalleri
    "2024","2025","2026","bu yıl","geçen yıl","son yıl",
    "güncel","günümüzde","şu an","şu anda","bugün itibariyle",
    "en son","kaç oldu","kaça yükseldi","kaça düştü",
    "son açıklanan","yeni açıklanan","resmi rakam",
)

# ── Canlı veri gerekmez — bu kalıplar statik bilgi ─────────────
_STATIC_KW    = ("nasıl kullanılır","syntax","örnek ver","açıkla","anlat",
                 "ne demek","tanımı nedir","nasıl yapılır","tutorial")
_TECH_MODES   = {"code","it_expert"}


def _detect_live_type(
    query: str,
    mode: str,
    router_decision: Dict = None,
    history: List[Dict] = None,
) -> Optional[str]:
    """
    Akıllı detection — Router + bağlam.
    
    Öncelik sırası:
    1. Router `needs_realtime=true` dedi → web'e git
    2. Kısa mesaj + önceki tool aynıysa → aynı tool
    3. Keyword listesi → anlık API
    4. Hiçbiri → None
    """
    q     = query.lower().strip()
    words = q.split()

    # ── Kısa mesaj bağlam analizi ─────────────────────────
    if len(words) <= 4 and history:
        # Son asistan mesajından tool tipini anla
        last_live_tool = None
        for msg in reversed(history[-8:]):
            if msg.get("role") != "assistant":
                continue
            c = (msg.get("content") or "").lower()
            if any(x in c for x in ["°c", "sıcaklık", "hissedilen", "rüzgar", "nem %"]):
                last_live_tool = "weather"
                break
            if any(x in c for x in ["1 usd", "1 eur", "try =", "₺", "dolar kuru", "euro kuru"]):
                last_live_tool = "currency"
                break
            if any(x in c for x in ["bitcoin", "btc", "ethereum", "kripto", "$"]):
                last_live_tool = "crypto"
                break

        if last_live_tool == "weather":
            # Bilinen şehirleri kontrol et — suffix olmadan da yakala
            known = ["istanbul","ankara","izmir","antalya","bursa","adana","konya",
                     "berlin","london","paris","tokyo","dubai","amsterdam","madrid",
                     "rome","vienna","moscow","beijing","new york","los angeles",
                     "zurich","geneva","barcelona","milan","stockholm","oslo","helsinki"]
            # Non-weather intent guard
            non_weather_intent = ["kod", "kodu", "yaz", "düzelt", "devam", "ettir",
                                  "açıkla", "anlat", "fonksiyon", "class"]
            has_non_weather = any(w in q for w in non_weather_intent)

            if not has_non_weather:
                # q'da şehir var mı? (ekleri de kaldırarak)
                q_clean = q
                for suf in ["da","de","ta","te","'da","'de","'ta","'te",
                            "nin","nın","nun","nün","in","ın","un","ün"]:
                    q_clean = q_clean.replace(suf, " ").strip()
                for city in known:
                    if city in q or city in q_clean:
                        print(f"[DETECT] Bağlam weather → {city}")
                        return "weather"
                # "peki" "ya" "ya da" ile başlayan kısa soru → devam
                if any(q.startswith(w) for w in ["peki","ya ","ya da","orası","orada"]):
                    print(f"[DETECT] Bağlam weather devam (peki/ya)")
                    return "weather"

        if last_live_tool == "currency":
            currency_words = ["dolar","euro","sterlin","gbp","usd","eur","btc",
                              "bitcoin","frank","yen","chf","jpy","kur","fiyat"]
            # KRİTİK: Sadece currency kelimesi GERÇEKTEN varsa devam.
            # "kodu devam ettir" gibi mesajlar currency'e gitmesin.
            if any(k in q.split() or k in q for k in currency_words):
                # Ek koruma: kod/yazma/devam gibi non-currency niyetler varsa skip
                non_currency_intent = ["kod", "kodu", "yaz", "düzelt", "devam", "ettir",
                                       "açıkla", "anlat", "fonksiyon", "class", "import",
                                       "hata", "error", "bug", "test", "refactor"]
                if not any(w in q for w in non_currency_intent):
                    print(f"[DETECT] Bağlam currency devam")
                    return "currency"
                else:
                    print(f"[DETECT] Currency bağlamı var ama kod/devam niyeti → skip")
    
    q = query.lower()

    # ── Router kararı varsa öncelikli kullan ──────────────
    if router_decision:
        rt   = router_decision.get("needs_realtime", False)
        tool = router_decision.get("tool", "none")
        conf = router_decision.get("confidence", "low")

        if rt and tool and tool != "none":
            # Router spesifik tool belirledi
            tool_map = {
                "currency": "currency",
                "weather":  "weather",
                "crypto":   "crypto",
                "news":     "news",
                "web":      "deep_search",
                "borsa":    "borsa",
                "time":     "time",
            }
            mapped = tool_map.get(tool)
            if mapped:
                print(f"[DETECT] Router → {mapped} (conf={conf})")
                return mapped

        if rt and conf in ("high", "medium"):
            # Realtime lazım ama tool belirsiz → deep search
            print(f"[DETECT] Router needs_realtime=true → deep_search")
            return "deep_search"

    # ── Kod modunda canlı veri yok ────────────────────────
    if mode in _TECH_MODES:
        return None

    # ── Kesinlikle statik — web'e gitme ──────────────────
    if any(k in q for k in _STATIC_KW) and len(q.split()) <= 6:
        return None

    # Retry sinyali — önceki canlı veri yanlışsa yeniden çek
    if any(k in q for k in _RETRY_KW):
        # Hangi araç tekrar sorulacak? Context'e bak
        # Şimdilik currency ve genel olarak işle
        return "currency" if any(k in q for k in _CURRENCY_SINGLE) else "currency"

    # Önce anlık API sinyalleri — bunlar /unified'a gider
    if any(k in q for k in _CURRENCY_KW): return "currency"

    # Tek kelime döviz — "euro" veya "dolar" tek başına
    q_stripped = q.strip().rstrip("?! ")
    if q_stripped in _CURRENCY_SINGLE or len(q.split()) <= 2 and any(k in q for k in _CURRENCY_SINGLE):
        return "currency"

    if any(k in q for k in _WEATHER_KW):  return "weather"
    if any(k in q for k in _CRYPTO_KW):   return "crypto"
    if any(k in q for k in _TIME_KW):     return "time"
    if any(k in q for k in _NEWS_KW):     return "news"
    if any(k in q for k in _BORSA_KW):    return "borsa"
    if any(k in q for k in _PRICE_KW):    return "price_search"

    # Güncel bilgi sinyali — deep_search pipeline'a gider
    if any(k in q for k in _DEEP_SEARCH_KW): return "deep_search"

    return None



# ──────────────────────────────────────────────────────────────
# GEMINI LIVE STREAM
# web_search / news / price_search → Gemini direkt yanıtlar,
# DeepInfra'ya gitmez, tag enjeksiyonu yok, kullanıcıya düz gider
# ──────────────────────────────────────────────────────────────
async def gemini_live_stream(query: str, live_type: str):
    """
    Vertex AI Gemini + Google Search Grounding.
    Service Account JSON key ile kimlik doğrulama — datacenter IP sorunu yok.
    """
    instructions = {
        "weather":      "Güncel hava durumu bilgisini ver. Sıcaklık, nem, rüzgar. Türkçe.",
        "currency":     "Güncel döviz/kur bilgisini ver. Sayıları net yaz. Türkçe.",
        "crypto":       "Güncel kripto para fiyatını ver. USD ve TL karşılığını yaz. Türkçe.",
        "time":         "Güncel tarih ve saati ver. Türkçe.",
        "news":         "Son dakika haberlerini özetle. Madde madde yaz. Türkçe.",
        "price_search": "Güncel fiyat bilgisini bul ve ver. Net rakamlar yaz. Türkçe.",
        "web_search":   "Soruyu Google'da ara, güncel ve doğru yanıt ver. Türkçe.",
        "deep_search":  "Soruyu derinlemesine araştır. Kapsamlı, kaynaklı yanıt ver. Türkçe.",
    }
    instruction = instructions.get(live_type, "Güncel bilgiyi Google'dan ara ve ver. Türkçe.")

    try:
        import os as _os
        from google import genai
        from google.genai import types

        # Vertex AI — Service Account key ile
        if _os.path.exists(GEMINI_SA_KEY_PATH) and GEMINI_PROJECT:
            _os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GEMINI_SA_KEY_PATH
            client = genai.Client(
                vertexai=True,
                project=GEMINI_PROJECT,
                location=GEMINI_LOCATION,
            )
            print(f"[GEMINI] Vertex AI | project={GEMINI_PROJECT}")
        elif GEMINI_API_KEY:
            client = genai.Client(api_key=GEMINI_API_KEY)
            print(f"[GEMINI] AI Studio fallback")
        else:
            yield "⚠️ Gemini yapılandırması eksik."
            return

        def _call():
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=query,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        f"Sen ONE-BUNE AI asistanısın. {instruction} "
                        "Varsa kaynakları yanıtın sonuna ekle. Markdown kullan."
                    ),
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.1,
                    max_output_tokens=2048 if live_type == "deep_search" else 1024,
                ),
            )

        print(f"[GEMINI] ▶ {live_type} | '{query[:60]}'")
        response = await asyncio.to_thread(_call)
        text = response.text or ""

        if not text.strip():
            yield "Yanıt alınamadı."
            return

        print(f"[GEMINI] ✅ {len(text)} karakter | model={GEMINI_MODEL}")

        for i in range(0, len(text), 80):
            yield text[i:i + 80]
            await asyncio.sleep(0)

        try:
            sources = []
            if response.candidates:
                meta = response.candidates[0].grounding_metadata
                for chunk in getattr(meta, "grounding_chunks", []):
                    web = getattr(chunk, "web", None)
                    if web and getattr(web, "uri", None):
                        sources.append(f"- [{getattr(web,'title',web.uri)}]({web.uri})")
            if sources:
                yield "\n\n---\n**Kaynaklar:**\n" + "\n".join(sources[:5])
        except Exception:
            pass

    except Exception as e:
        import traceback
        print(f"[GEMINI] ❌ {type(e).__name__}: {e}")
        print(traceback.format_exc()[-400:])
        yield "Üzgünüm, şu an yanıt üretemiyorum. Lütfen tekrar deneyin."

async def load_user_memory(user_id: int) -> Optional[str]:
    if not db_pool:
        return None
    try:
        async with db_pool.acquire() as conn:
            memory = await conn.fetchval("SELECT get_user_memory_for_prompt($1)", user_id)
            if memory:
                return memory
            await conn.execute(
                "INSERT INTO user_memory (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                user_id)
            return "[USER MEMORY]\nNo memory data yet\n[/USER MEMORY]"
    except Exception as e:
        print(f"[MEMORY ERROR] {e}")
        return None


async def load_conversation_summary(conversation_id: str) -> Optional[str]:
    if not db_pool or not conversation_id:
        return None
    try:
        async with db_pool.acquire() as conn:
            summary = await conn.fetchval(
                "SELECT get_conversation_summary($1::uuid)", conversation_id)
            return summary if summary and "New conversation" not in summary else None
    except Exception as e:
        print(f"[SUMMARY ERROR] {e}")
        return None


async def should_create_summary(conversation_id: str) -> bool:
    if not db_pool or not conversation_id:
        return False
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT COUNT(*) as total_messages,
                       COALESCE(MAX(cs.messages_end), 0) as last_summary_end
                FROM messages m
                LEFT JOIN conversation_summaries cs ON m.conversation_id = cs.conversation_id
                WHERE m.conversation_id = $1::uuid
            """, conversation_id)
            if result:
                diff = result['total_messages'] - result['last_summary_end']
                return diff >= SUMMARY_INTERVAL
    except Exception as e:
        print(f"[SUMMARY CHECK ERROR] {e}")
    return False


def _score_message_importance(msg: dict) -> float:
    """
    Mesajın önemini 0-1 arasında puanla.
    Claude'un sliding window mantığına benzer önem skoru.
    """
    content = (msg.get("content") or "").lower()
    role    = msg.get("role", "user")
    score   = 0.5  # base

    # Kullanıcı mesajları genelde daha önemli
    if role == "user":
        score += 0.1

    # Kod içeren mesajlar kritik
    if "```" in content or "def " in content or "import " in content:
        score += 0.25

    # Hata mesajları kritik
    if any(w in content for w in ["error","hata","traceback","exception","failed"]):
        score += 0.2

    # Karar ifadeleri
    if any(w in content for w in ["tamam","kabul","evet","hayır","kesinlikle","anlaştık"]):
        score += 0.15

    # Kısa mesajlar genelde az önemli
    if len(content) < 30:
        score -= 0.15

    # Çok uzun mesajlar bilgi yoğun
    if len(content) > 500:
        score += 0.1

    return min(1.0, max(0.0, score))


async def create_conversation_summary(user_id: int, conversation_id: str, config: Dict):
    """
    Akıllı özetleme — Claude benzeri sliding window + önem skoru.

    Strateji:
    1. Tüm özetlenmemiş mesajları al
    2. Her mesajı önem skoruna göre değerlendir
    3. Önemli mesajları tam tut, düşük önemli olanları özet olarak sakla
    4. Hiyerarşik özet: konu → alt konular → kararlar → sonraki adımlar
    5. Kullanıcı bellek güncelleme (tercihler, teknik bilgi)
    """
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            # Özetlenmemiş mesajları al
            messages = await conn.fetch("""
                SELECT content, role, created_at, id FROM messages
                WHERE conversation_id = $1::uuid
                AND id > COALESCE(
                    (SELECT max(end_message_id::text)::uuid FROM conversation_summaries
                     WHERE conversation_id = $1::uuid),
                    '00000000-0000-0000-0000-000000000000'::uuid)
                ORDER BY created_at ASC
            """, conversation_id)

            if not messages or len(messages) < 5:
                return

        msgs = [dict(m) for m in messages]

        # ── Önem skorlama ────────────────────────────────────
        scored = [(m, _score_message_importance(m)) for m in msgs]

        # Yüksek öneme sahip mesajları tam tut (skor > 0.65)
        high_priority = [m for m, s in scored if s > 0.65]
        low_priority  = [m for m, s in scored if s <= 0.65]

        # Düşük öncelikli mesajları kısalt
        low_text  = "\n".join([
            f"{m['role'][:1].upper()}: {m['content'][:120]}..."
            for m in low_priority
        ])
        high_text = "\n".join([
            f"{m['role'].upper()}: {m['content'][:600]}"
            for m in high_priority
        ])

        # ── LLM ile akıllı özet ─────────────────────────────
        is_tr = any(
            any(c in (m.get("content") or "") for c in "çğışöüÇĞİŞÖÜ")
            for m in msgs[:5]
        )
        lang_rule = "TÜRKÇE yanıtla." if is_tr else "Respond in English."

        prompt = f"""Bir konuşmayı analiz edip yapılandırılmış özet çıkar.

ÖNEMLİ MESAJLAR (tam içerik):
{high_text}

DİĞER MESAJLAR (kısaltılmış):
{low_text if low_text else "(yok)"}

Aşağıdaki JSON formatında yanıtla. {lang_rule}
{{
  "topic": "Ana konu (max 60 karakter)",
  "subtopics": ["alt konu 1", "alt konu 2"],
  "summary": "Konuşmanın özeti (max 200 kelime, Türkçe)",
  "decisions_made": ["alınan karar 1", "alınan karar 2"],
  "next_steps": ["sonraki adım 1"],
  "user_preferences": {{
    "communication": "nasıl iletişim kuruyor (kısa/detaylı/teknik)",
    "technical_level": "beginner/intermediate/expert",
    "domain": "çalıştığı alan"
  }},
  "key_facts": ["önemli bilgi 1", "önemli bilgi 2"]
}}
SADECE JSON döndür, başka hiçbir şey yazma."""

        summary_response = ""
        async for chunk in stream_deepinfra_completion(
            messages=[
                {"role": "system",
                 "content": "Konuşma analisti. Sadece geçerli JSON döndür, markdown yok."},
                {"role": "user", "content": prompt}
            ],
            model=config["model"],
            max_tokens=600,
            temperature=0.1,
            top_p=0.9,
        ):
            summary_response += chunk

        # JSON parse
        try:
            raw = summary_response.strip()
            if "```" in raw:
                s = raw.find("{")
                e = raw.rfind("}") + 1
                raw = raw[s:e]
            data = json.loads(raw)
        except Exception:
            print(f"[SUMMARY] JSON parse hatası — atlandı")
            return

        # ── DB kaydet ────────────────────────────────────────
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO conversation_summaries
                (user_id, conversation_id, messages_start, messages_end,
                 topic, subtopics, summary_text, progress,
                 decisions_made, next_steps, learned_facts,
                 start_message_id, end_message_id, messages_summarized)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb,
                        $12::uuid, $13::uuid, $14)
                ON CONFLICT DO NOTHING
            """,
                user_id, conversation_id,
                1, len(msgs),
                data.get("topic", "Konuşma"),
                data.get("subtopics", []),
                data.get("summary", ""),
                data.get("summary", ""),
                data.get("decisions_made", []),
                data.get("next_steps", []),
                json.dumps({
                    "preferences": data.get("user_preferences", {}),
                    "key_facts":   data.get("key_facts", []),
                }),
                msgs[0]["id"],
                msgs[-1]["id"],
                len(msgs),
            )
            print(f"[SUMMARY] ✅ '{data.get('topic')}' — {len(msgs)} mesaj, "
                  f"{len(high_priority)} yüksek önem, {len(low_priority)} düşük önem")

        # ── Kullanıcı belleğini güncelle ─────────────────────
        prefs = data.get("user_preferences", {})
        if prefs:
            await update_user_memory_from_summary(user_id, {
                "preferences": prefs.get("communication", ""),
                "technical":   prefs.get("technical_level", ""),
                "domain":      prefs.get("domain", ""),
            })

    except Exception as e:
        print(f"[SUMMARY ERROR] {e}")


async def update_user_memory_from_summary(user_id: int, learned_facts: Dict):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            if 'technical' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory SET technical_preferences = technical_preferences || $1::jsonb,
                    updated_at=NOW() WHERE user_id=$2
                """, json.dumps({'learned': learned_facts['technical']}), user_id)
            if 'preferences' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory SET communication_style = communication_style || $1::jsonb,
                    updated_at=NOW() WHERE user_id=$2
                """, json.dumps({'noted': learned_facts['preferences']}), user_id)
    except Exception as e:
        print(f"[MEMORY UPDATE ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# CODE MODE
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# KULLANICI DOSYA WORKSPACE — Konuşmada dosya saklama
# Kullanıcı kodu paylaşırsa → workspace'e kaydet
# Sonraki mesajlarda otomatik bağlam olarak kullan
# ═══════════════════════════════════════════════════════════════

async def save_user_file_to_workspace(
    conversation_id: str,
    filename: str,
    content_text: str,
    language: str = "unknown"
):
    """Kullanıcının paylaştığı kodu workspace'e kaydet."""
    if not db_pool or not conversation_id:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO workspace_files
                    (conversation_id, filename, content, language,
                     size_bytes, created_at, updated_at)
                VALUES ($1::uuid, $2, $3, $4, $5, NOW(), NOW())
                ON CONFLICT (conversation_id, filename)
                DO UPDATE SET
                    content    = EXCLUDED.content,
                    language   = EXCLUDED.language,
                    size_bytes = EXCLUDED.size_bytes,
                    updated_at = NOW()
            """, conversation_id, filename,
                 content_text, language, len(content_text.encode()))
            print(f"[WORKSPACE] ✅ {filename} kaydedildi ({len(content_text)} chars)")
    except Exception as e:
        print(f"[WORKSPACE] ❌ {e}")


async def load_workspace_files(conversation_id: str) -> List[Dict]:
    """Workspace'teki dosyaları yükle — max 5, en son güncellenenler."""
    if not db_pool or not conversation_id:
        return []
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT filename, content, language, updated_at
                FROM workspace_files
                WHERE conversation_id = $1::uuid
                ORDER BY updated_at DESC
                LIMIT 5
            """, conversation_id)
            return [dict(r) for r in rows]
    except Exception:
        return []


def _extract_code_blocks(text: str) -> List[Dict]:
    """Mesajdan kod bloklarını çıkar — filename + content + language."""
    import re
    blocks = []
    # backtick python ... backtick formatı
    pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
    for match in pattern.finditer(text):
        lang    = match.group(1) or "text"
        code    = match.group(2).strip()
        if len(code) > 50:  # Çok kısa snippet'leri atla
            blocks.append({"language": lang, "content": code})
    return blocks


async def process_user_message_for_workspace(
    conversation_id: str,
    user_message: str
):
    """
    Kullanıcı mesajında kod varsa workspace'e kaydet.
    Otomatik filename üret (lang + timestamp).
    """
    blocks = _extract_code_blocks(user_message)
    for i, block in enumerate(blocks):
        lang     = block["language"]
        ext_map  = {
            "python":"py","javascript":"js","typescript":"ts",
            "jsx":"jsx","tsx":"tsx","html":"html","css":"css",
            "sql":"sql","bash":"sh","yaml":"yaml","json":"json",
            "go":"go","rust":"rs","java":"java","cpp":"cpp",
        }
        ext      = ext_map.get(lang, "txt")
        filename = f"user_code_{i+1}.{ext}"
        await save_user_file_to_workspace(
            conversation_id, filename, block["content"], lang
        )


async def load_code_context(conversation_id: str) -> Optional[Dict]:
    if not db_pool or not conversation_id:
        return None
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT last_code, last_language, last_file_name,
                       tech_stack, compressed_history, messages_since_compression
                FROM code_context WHERE conversation_id=$1::uuid
            """, conversation_id)
            if result:
                return dict(result)
            await conn.execute("""
                INSERT INTO code_context (conversation_id, user_id)
                SELECT $1::uuid, user_id FROM conversations WHERE id=$1::uuid
                ON CONFLICT DO NOTHING
            """, conversation_id)
    except Exception as e:
        print(f"[CODE CONTEXT ERROR] {e}")
    return None


def extract_code_from_message(message: str) -> Optional[Dict]:
    match = re.search(r'```(\w+)\n(.*?)```', message, re.DOTALL)
    if not match:
        return None
    language = match.group(1).lower()
    code     = match.group(2).strip()
    file_name = None
    if language == 'python':
        m = re.search(r'#\s*(?:File:|Dosya:)?\s*([a-zA-Z0-9_\-\.\/]+\.py)', code, re.IGNORECASE)
        if m: file_name = m.group(1)
    elif language in ['javascript','typescript','js','ts']:
        m = re.search(r'//\s*(?:File:|Dosya:)?\s*([a-zA-Z0-9_\-\.\/]+\.(?:js|ts))', code, re.IGNORECASE)
        if m: file_name = m.group(1)
    return {"code": code, "language": language, "file_name": file_name,
            "line_count": len(code.split('\n'))}


async def track_shared_code(conversation_id: str, code: str, language: str, file_name: Optional[str]):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE code_context SET last_code=$2, last_language=$3,
                last_file_name=COALESCE($4, last_file_name),
                messages_since_compression=messages_since_compression+1, updated_at=NOW()
                WHERE conversation_id=$1::uuid
            """, conversation_id, code, language, file_name)
    except Exception as e:
        print(f"[CODE TRACKING ERROR] {e}")


async def should_compress_code_context(conversation_id: str, threshold: int = 12) -> bool:
    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT messages_since_compression FROM code_context
                WHERE conversation_id=$1::uuid
            """, conversation_id)
            return bool(count and count >= threshold)
    except:
        return False


async def compress_code_context(conversation_id: str, messages: List[Dict], config: Dict):
    if len(messages) < 5:
        return
    conv_text = "\n\n".join([
        f"{m['role'].upper()}: {m['content'][:500]}"
        for m in messages[:-5]
    ])
    prompt = f"""Analyze this code conversation and create a BRIEF summary (max 150 words):
{conv_text}
Extract ONLY: Files discussed, Issues fixed, Technical decisions, Current project context.
Format as compact bullet points. Use Turkish if conversation is Turkish."""
    try:
        compressed = ""
        async for chunk in stream_deepinfra_completion(
            messages=[{"role":"system","content":"You summarize code conversations concisely."},
                      {"role":"user","content":prompt}],
            model=config["model"], max_tokens=400, temperature=0.3, top_p=0.85,
        ):
            compressed += chunk
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE code_context SET compressed_history=$2,
                compression_metadata=$3::jsonb,
                messages_since_compression=0, last_compression_at=NOW()
                WHERE conversation_id=$1::uuid
            """, conversation_id, compressed,
                json.dumps({"compressed_at": datetime.now().isoformat()}))
        print(f"[CODE COMPRESSION] {len(conv_text)} → {len(compressed)} chars")
    except Exception as e:
        print(f"[CODE COMPRESSION ERROR] {e}")


async def build_code_messages(
    user_id: int,
    conversation_id: str,
    user_prompt: str,
    history: List[Dict],
    config: Dict,
    **kwargs,
) -> List[Dict]:
    messages     = []
    code_context = await load_code_context(conversation_id) if conversation_id else None

    system_content = config["system_prompt"]

    # ── REASONING LAYER — Code mode intent ───────────────────────
    reasoning_hint = build_reasoning_hint(user_prompt, history or [], "code")
    print(f"[INTENT/CODE] {reasoning_hint.split(chr(10))[0]}")
    # ─────────────────────────────────────────────────────────────

    # ── TASK OBJECT — Kod cevabından önce görev tespiti ──────────
    workspace    = await get_workspace(conversation_id or "")
    intent_str   = classify_intent(user_prompt, history or [], "code").get("intent", "")
    task         = build_task(user_prompt, intent_str, workspace, history or [])
    task_hint    = task_to_prompt_hint(task)
    # task_hint system prompt'a eklenmez — history zaten bağlamı taşıyor
    # Task'ı Redis'e kaydet — follow-up'larda bağlam korunur
    if conversation_id:
        asyncio.create_task(set_task(conversation_id, task))
    print(f"[TASK] {task['task_type']} → {task['response_style']} | target={task.get('target','?')}")
    # ─────────────────────────────────────────────────────────────

    # Memory
    user_memory = await load_user_memory(user_id)
    system_content = system_content.replace(
        "{user_memory}", user_memory or "[USER MEMORY]\nNo memory yet\n[/USER MEMORY]")

    # Code context
    if code_context:
        parts = ["[CODE CONTEXT]"]
        last_code = code_context.get('last_code')
        if last_code and len(last_code) < 5000:
            parts.append(f"\nLast shared code ({code_context.get('last_language')}):")
            parts.append(f"```{code_context.get('last_language')}\n{last_code[:3000]}\n```\n")
        tech_stack = code_context.get('tech_stack')
        if tech_stack:
            parts.append(f"Project stack: {', '.join(tech_stack)}")
        compressed = code_context.get('compressed_history')
        if compressed:
            parts.append(f"\nPrevious conversation summary:\n{compressed}\n")
        parts.append("[/CODE CONTEXT]")
        system_content += "\n\n" + "\n".join(parts)

    # ── Canlı veri (code modda da çalışır) ──────────────────────

    # RAG + web context
    if kwargs.get('rag_context'):
        system_content += f"\n\n[RAG Context]\n{kwargs['rag_context']}\n[/RAG Context]"
    if kwargs.get('context'):
        system_content += f"\n\n[Web Search]\n{kwargs['context']}\n[/Web Search]"

    messages.append({"role": "system", "content": system_content})

    # Compression check
    if conversation_id and await should_compress_code_context(
        conversation_id, config.get("compression_threshold", 12)
    ):
        asyncio.create_task(compress_code_context(conversation_id, history, config))

    # History
    recent = history[-15:] if (code_context and code_context.get('compressed_history') and len(history) > 15) else history[-25:]
    for msg in recent:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_prompt})

    # Track code in background
    code_data = extract_code_from_message(user_prompt)
    if code_data and conversation_id:
        asyncio.create_task(track_shared_code(
            conversation_id, code_data['code'],
            code_data['language'], code_data.get('file_name')))

    return messages


# ═══════════════════════════════════════════════════════════════
# THINKING DISPLAY
# ═══════════════════════════════════════════════════════════════

def should_show_thinking(prompt: str, mode: str, history: list = None) -> bool:
    """
    Thinking adımlarını SADECE hata/debug/analiz durumlarında göster.
    Her şey için gösterme — sadece kullanıcı bir sorun bildiriyorsa.
    """
    p = prompt.lower().strip()

    # Önce hata/debug sinyallerini kontrol et (kelime sayısından bağımsız)
    SHOW_INDICATORS = [
        'hata alıyorum', 'hata veriyor', 'hata var',
        'çalışmıyor', 'exception', 'traceback', 'debug',
        'neden çalışmıyor', 'sorun var', 'sorun nedir',
        'crashloopback', 'oomkilled', 'imagepull',
        'analiz et', 'incele', 'kontrol et',
    ]
    if any(ind in p for ind in SHOW_INDICATORS):
        return True

    # Error kelimesi tek başına kısa mesajda da göster
    words = p.split()
    if 'error' in words or 'crash' in words:
        return True

    # Bunların dışında → asla thinking gösterme
    return False


async def generate_thinking_steps(
    prompt: str,
    mode: str,
    history: List[Dict] = None,
) -> List[ThinkingStep]:
    """
    Thinking steps — debug/analiz/karmaşık kod sorularında göster.
    Kullanıcıya botun ne yaptığını hissettir — güven artırır.
    """
    p     = prompt.lower()
    is_tr = any(c in prompt for c in "çğışöüÇĞİŞÖÜ")
    steps = []

    # Hata / debug
    if any(w in p for w in ['hata', 'error', 'crash', 'exception', 'çalışmıyor',
                              'traceback', 'failed', 'broken', 'bug', 'fix']):
        steps.append(ThinkingStep(emoji="🔍", message="Hata mesajı okunuyor..." if is_tr else "Reading the error..."))
        steps.append(ThinkingStep(emoji="🧠", message="Root cause analiz ediliyor..." if is_tr else "Finding root cause..."))
        steps.append(ThinkingStep(emoji="🔧", message="Fix hazırlanıyor..." if is_tr else "Preparing the fix..."))

    # Mimari / tasarım
    elif any(w in p for w in ['mimari', 'tasarla', 'nasıl yapılır', 'architecture',
                                'design', 'yapılandır', 'kur', 'implement']):
        steps.append(ThinkingStep(emoji="📐", message="Gereksinimler analiz ediliyor..." if is_tr else "Analyzing requirements..."))
        steps.append(ThinkingStep(emoji="🏗️", message="En uygun yaklaşım seçiliyor..." if is_tr else "Selecting best approach..."))
        steps.append(ThinkingStep(emoji="✍️",  message="Kod yazılıyor..." if is_tr else "Writing code..."))

    # Refactor / optimize
    elif any(w in p for w in ['refactor', 'optimize', 'temizle', 'iyileştir',
                                'clean', 'improve', 'performans']):
        steps.append(ThinkingStep(emoji="🔬", message="Mevcut kod inceleniyor..." if is_tr else "Reviewing existing code..."))
        steps.append(ThinkingStep(emoji="⚡", message="İyileştirme noktaları tespit ediliyor..." if is_tr else "Finding improvements..."))
        steps.append(ThinkingStep(emoji="✨", message="Temiz kod yazılıyor..." if is_tr else "Writing clean code..."))

    # Dosya / belge analizi
    elif any(w in p for w in ['analiz', 'incele', 'anla', 'özetle', 'analyze',
                                'review', 'dosya', 'file', 'pdf', 'belgeni']):
        steps.append(ThinkingStep(emoji="📄", message="Dosya okunuyor..." if is_tr else "Reading the file..."))
        steps.append(ThinkingStep(emoji="🧩", message="İçerik analiz ediliyor..." if is_tr else "Analyzing content..."))
        steps.append(ThinkingStep(emoji="📝", message="Sonuç hazırlanıyor..." if is_tr else "Preparing summary..."))

    # Test yazma
    elif any(w in p for w in ['test', 'spec', 'unit test', 'coverage']):
        steps.append(ThinkingStep(emoji="🧪", message="Fonksiyon analiz ediliyor..." if is_tr else "Analyzing function..."))
        steps.append(ThinkingStep(emoji="🎯", message="Edge case'ler belirleniyor..." if is_tr else "Identifying edge cases..."))
        steps.append(ThinkingStep(emoji="✅", message="Test yazılıyor..." if is_tr else "Writing tests..."))

    # Genel kod sorusu
    else:
        steps.append(ThinkingStep(emoji="💭", message="Problem analiz ediliyor..." if is_tr else "Analyzing the problem..."))
        steps.append(ThinkingStep(emoji="⚙️",  message="Çözüm üretiliyor..." if is_tr else "Generating solution..."))

    return steps


def get_deep_search_steps(is_tr: bool = True) -> List[ThinkingStep]:
    """Deep search pipeline adımları — Gemini ile 3 adım."""
    return [
        ThinkingStep(emoji="🔍", message="Google'da aranıyor..." if is_tr else "Searching Google..."),
        ThinkingStep(emoji="📖", message="Kaynaklar okunuyor..." if is_tr else "Reading sources..."),
        ThinkingStep(emoji="✂️",  message="İçerik parçalanıyor..." if is_tr else "Chunking content..."),
        ThinkingStep(emoji="⚡", message="En alakalı kaynaklar seçiliyor..." if is_tr else "Selecting best sources..."),
        ThinkingStep(emoji="✍️",  message="Yanıt hazırlanıyor..." if is_tr else "Generating answer..."),
        ThinkingStep(emoji="🔎", message="Yanıt doğrulanıyor..." if is_tr else "Verifying answer..."),
    ]


# ═══════════════════════════════════════════════════════════════
# WEB SEARCH SYNTHESIS
# ═══════════════════════════════════════════════════════════════

async def synthesize_web_results(raw: str, query: str, config: Dict) -> str:
    prompt = f"""Web search results for: "{query}"

{raw}

Synthesize into a clear answer:
1. Combine related info from multiple sources
2. Include dates for time-sensitive info
3. Brief attribution: "Kaynak: X" (no full URLs)
4. Resolve conflicts — prefer most recent
5. User-friendly language, add context

Same language as query (Turkish/English). Concise but comprehensive."""
    try:
        out = ""
        async for chunk in stream_deepinfra_completion(
            messages=[{"role":"system","content":"Research synthesizer. Clear, accurate summaries."},
                      {"role":"user","content":prompt}],
            model=config["model"], max_tokens=1500, temperature=0.5, top_p=0.85,
        ):
            out += chunk
        print(f"[WEB SYNTHESIS] {len(raw)} → {len(out)} chars")
        return out
    except Exception as e:
        print(f"[WEB SYNTHESIS ERROR] {e}")
        return raw


# ═══════════════════════════════════════════════════════════════
# DEEPINFRA CLIENT
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# ADAPTIVE THINKING — mesajın zorluğuna göre seviye seç
# ═══════════════════════════════════════════════════════════════
def _adaptive_thinking_level(messages: List[Dict]) -> str:
    """
    Son user mesajının uzunluk + içerik analizine göre uygun thinking seviyesi:
      - none   : selamlaşma, kısa sorular, basit komutlar
      - low    : orta uzunluk, temel sorular
      - medium : kod yazma, açıklama isteyen sorular
      - high   : karmaşık problemler, debug, analiz, mimari
      - xhigh  : çok zor algoritma, multi-step reasoning gereken
    """
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return "none"
    last = (user_msgs[-1].get("content") or "").lower().strip()
    n = len(last)

    # Çok kısa → düşünme yok
    if n < 25:
        return "none"

    # Karmaşıklık sinyalleri (içerik bazlı)
    very_hard = any(w in last for w in [
        "optimize", "algoritma", "algorithm", "mimari", "architecture",
        "refactor", "performans", "complexity", "big-o", "tasarla", "design",
    ])
    hard = any(w in last for w in [
        "debug", "hata", "error", "neden", "nasıl", "açıkla", "explain",
        "karşılaştır", "compare", "analiz", "review", "incele",
    ])
    code_like = any(w in last for w in [
        "yaz", "kod", "function", "class", "def ", "endpoint", "api",
        "fastapi", "react", "python", "javascript",
    ])

    # Karar
    if very_hard or n > 600:
        return "xhigh"
    if hard or (code_like and n > 100):
        return "high"
    if code_like or n > 200:
        return "medium"
    if n > 80:
        return "low"
    return "none"


# ═══════════════════════════════════════════════════════════════
# GEMINI VISION STREAM — Multimodal (image + text + history)
# ═══════════════════════════════════════════════════════════════
async def gemini_vision_stream(
    prompt: str,
    image_data: str,
    image_type: str,
    history: List[Dict],
    system_prompt: str,
    max_tokens: int = 4096,
    temperature: float = 0.4,
) -> AsyncGenerator[str, None]:
    """
    Gemini 2.5 Flash Lite ile multimodal vision stream.
    Görsel + soru + history → Gemini'ye gider, text stream olarak döner.
    History sayesinde bağlam korunur.
    """
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        yield "⚠️ Gemini SDK yok (google-genai)."
        return

    # Vertex AI client (Service Account key ile)
    try:
        if os.path.exists(GEMINI_SA_KEY_PATH) and GEMINI_PROJECT:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GEMINI_SA_KEY_PATH
            client = genai.Client(
                vertexai=True,
                project=GEMINI_PROJECT,
                location=GEMINI_LOCATION,
            )
        elif GEMINI_API_KEY:
            client = genai.Client(api_key=GEMINI_API_KEY)
        else:
            yield "⚠️ Gemini yapılandırması eksik (Vertex SA veya GEMINI_API_KEY)."
            return
    except Exception as e:
        print(f"[GEMINI VISION] Client init hatası: {e}")
        yield f"⚠️ Gemini istemcisi: {e}"
        return

    # History'i Gemini formatına çevir (önceki mesajlar — bağlam için)
    contents = []
    for msg in (history or [])[-20:]:  # son 20 mesaj yeterli
        role = msg.get("role")
        content = msg.get("content") or ""
        if not content or role == "system":
            continue
        gem_role = "user" if role == "user" else "model"
        contents.append(gtypes.Content(
            role=gem_role,
            parts=[gtypes.Part.from_text(text=str(content)[:4000])]
        ))

    # Yeni kullanıcı mesajı — görselli veya text-only
    img_bytes = b""
    if image_data:
        try:
            img_bytes = base64.b64decode(image_data)
            mime = image_type if image_type and image_type.startswith("image/") else f"image/{image_type or 'jpeg'}"
            new_parts = [
                gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
                gtypes.Part.from_text(text=prompt or "Bu görseli analiz et."),
            ]
        except Exception as e:
            print(f"[GEMINI VISION] Image parse hatası: {e}")
            yield f"⚠️ Görsel okunamadı: {e}"
            return
    else:
        # Text-only akış (CHAT_PROVIDER=gemini)
        new_parts = [
            gtypes.Part.from_text(text=prompt or ""),
        ]

    contents.append(gtypes.Content(role="user", parts=new_parts))

    # Vision modeli — flash-lite multimodal
    vision_model = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash-lite")

    # Stream
    try:
        gen_config_kwargs = {
            "system_instruction": system_prompt or None,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        # Gemini 2.5 thinking — opsiyonel, SDK desteklemezse skip
        thinking_enabled = False
        try:
            tc = gtypes.ThinkingConfig(thinking_budget=-1, include_thoughts=True)
            gen_config_kwargs["thinking_config"] = tc
            thinking_enabled = True
        except Exception as _te:
            print(f"[GEMINI VISION] ThinkingConfig desteklenmiyor: {_te}")

        config = gtypes.GenerateContentConfig(**gen_config_kwargs)
        print(f"[GEMINI VISION] ▶ {vision_model} | history={len(contents)-1} | image={len(img_bytes)}B | thinking={thinking_enabled}")

        def _stream_call():
            return client.models.generate_content_stream(
                model=vision_model,
                contents=contents,
                config=config,
            )

        stream = await asyncio.to_thread(_stream_call)
        emitted = False
        in_thinking = False

        for chunk in stream:
            # 1. Önce parts'a bak (thinking destekli)
            handled = False
            try:
                cands = getattr(chunk, "candidates", None) or []
                if cands:
                    content_obj = getattr(cands[0], "content", None)
                    parts = getattr(content_obj, "parts", None) if content_obj else None
                    if parts:
                        for p in parts:
                            text = getattr(p, "text", None) or ""
                            if not text:
                                continue
                            is_thought = bool(getattr(p, "thought", False))
                            if is_thought:
                                if not in_thinking:
                                    yield "<think>"
                                    in_thinking = True
                                yield text
                            else:
                                if in_thinking:
                                    yield "</think>"
                                    in_thinking = False
                                yield text
                            emitted = True
                            handled = True
            except Exception as _pe:
                print(f"[GEMINI VISION] parts parse: {_pe}")

            # 2. Fallback: chunk.text (eski/sade SDK)
            if not handled:
                try:
                    text = getattr(chunk, "text", None)
                    if text:
                        if in_thinking:
                            yield "</think>"
                            in_thinking = False
                        yield text
                        emitted = True
                except Exception:
                    pass

            await asyncio.sleep(0)

        if in_thinking:
            yield "</think>"

        if not emitted:
            print(f"[GEMINI VISION] ⚠️ Empty response")
            yield "Bu mesajla ilgili bir cevap üretemedim. Lütfen sorunu yeniden ifade eder misin?"

    except Exception as e:
        print(f"[GEMINI VISION] Stream hatası: {type(e).__name__}: {e}")
        yield f"⚠️ Stream hatası: {e}"


async def stream_deepinfra_completion(
    messages:    List[Dict],
    model:       str,
    max_tokens:  int,
    temperature: float,
    top_p:       float,
    reasoning_effort: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    DeepInfra streaming completion with reasoning support.

    reasoning_effort (DeepSeek V4 API):
      - "none"   → düşünme yok, hızlı cevap (default)
      - "low"    → hafif akıl yürütme
      - "medium" → orta düşünce
      - "high"   → derin düşünce
      - "xhigh"  → maksimum derin düşünce
      - "auto"   → mesaj zorluğuna göre otomatik seçim (kendi heuristik)

    Reasoning chunk'ları <think>...</think> tag'leri içinde yieldlanır.
    """
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
    }
    # Adaptive — mesaj zorluğuna göre seviye seç
    if reasoning_effort == "auto":
        reasoning_effort = _adaptive_thinking_level(messages)

    payload = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
        "top_p": top_p, "stream": True,
    }
    # Thinking mode — sadece geçerli seviye verildiyse
    if reasoning_effort and reasoning_effort in ("low", "medium", "high", "xhigh"):
        payload["reasoning_effort"] = reasoning_effort
        # Reasoning + cevap aynı bütçeyi paylaşır → düşünme uzunsa cevap kalmaz
        # Bu yüzden thinking modda max_tokens'ı genişlet (reasoning'e ayrı yer aç)
        extra = {"low": 2000, "medium": 4000, "high": 6000, "xhigh": 10000}.get(reasoning_effort, 0)
        payload["max_tokens"] = max_tokens + extra

    in_thinking = False  # Reasoning baloncuğu açık mı?

    # Uzun kod cevapları için generous timeout
    # connect=10s, read=900s (15dk - çok uzun kodlar için), write=30s, pool=900s
    _to_read = float(os.getenv("DEEPINFRA_READ_TIMEOUT", "900"))
    timeout_cfg = httpx.Timeout(connect=10.0, read=_to_read, write=30.0, pool=_to_read)
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        async with client.stream("POST", f"{DEEPINFRA_BASE_URL}/chat/completions",
                                 headers=headers, json=payload) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        # Eğer hâlâ thinking modundaysak kapat
                        if in_thinking:
                            yield "</think>"
                            in_thinking = False
                        break
                    try:
                        data    = json.loads(data_str)
                        delta   = data.get("choices",[{}])[0].get("delta",{})
                        # 1. Reasoning content (düşünme akışı)
                        reasoning = delta.get("reasoning_content", "")
                        if reasoning:
                            if not in_thinking:
                                yield "<think>"
                                in_thinking = True
                            yield reasoning
                            continue
                        # 2. Normal content (cevap)
                        content = delta.get("content", "")
                        if content:
                            if in_thinking:
                                yield "</think>"
                                in_thinking = False
                            yield content
                    except Exception:
                        continue


# ═══════════════════════════════════════════════════════════════
# MESSAGE BUILDER — Ana sistem prompt oluşturma
# ═══════════════════════════════════════════════════════════════


def _extract_active_commands(history: list) -> list:
    """Konuşma geçmişinden kullanıcının verdiği aktif komutları çıkar."""
    commands = []
    seen = set()
    command_signals = [
        "türkçe konuş", "türkçe yaz", "ingilizce konuş", "kısa cevap ver",
        "uzun cevap ver", "kod yazma", "bana sormadan", "markdown kullanma",
        "liste yapma", "madde madde", "sadece kod", "açıklama yapma",
    ]
    for msg in history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "").lower().strip()
        for sig in command_signals:
            if sig in content and content not in seen:
                seen.add(content)
                commands.append(content[:100])
                break
    return commands

async def build_messages(
    mode:            str,
    user_id:         int,
    conversation_id: Optional[str],
    user_prompt:     str,
    history:         List[Dict],
    rag_context:     Optional[str] = None,
    context:         Optional[str] = None,
    session_summary: Optional[str] = None,
    config:          Dict           = None,
    image_data:      Optional[str] = None,
    image_type:      Optional[str] = None,
    live_context:    Optional[str] = None,
    **kwargs,
) -> List[Dict]:
    """
    LLM'e gönderilecek mesaj dizisini oluşturur.

    Öncelik sırası:
    1. USER MEMORY       — en yüksek öncelik
    2. CONVERSATION SUMMARY
    3. LIVE DATA         — smart_tools /live (kur, hava, kripto, haber, araştırma)
    4. RAG CONTEXT       — döküman/bilgi tabanı
    5. WEB SEARCH        — gateway'den gelen context
    6. SESSION SUMMARY   — legacy destek
    """

    # Code mode özel builder'a gider
    if mode == "code":
        return await build_code_messages(
            user_id=user_id, conversation_id=conversation_id,
            user_prompt=user_prompt, history=history, config=config,
            rag_context=rag_context, context=context, session_summary=session_summary,
        )

    messages       = []
    system_content = config["system_prompt"]

    # ── Intent log (LOCAL, 0ms) — sadece loglama, prompt'a ekleme
    reasoning_hint = build_reasoning_hint(user_prompt, history or [], mode)
    print(f"[INTENT] {reasoning_hint.split(chr(10))[0]}")
    # ─────────────────────────────────────────────────────────────

    # ── USER COMMAND — konuşma boyunca aktif kural ───────────────
    # Kullanıcı bu konuşmada komut verdiyse system prompt'a ekle
    active_commands = _extract_active_commands(history or [])
    if active_commands:
        commands_str = "\n".join(f"- {cmd}" for cmd in active_commands)
        system_content += (
            f"\n\n[AKTİF KULLANICI KURALLARI — KESİNLİKLE UYGULA]\n"
            f"{commands_str}\n"
            f"[/AKTİF KULLANICI KURALLARI]"
        )
    # ─────────────────────────────────────────────────────────────

    # ── GÜNCEL TARİH — UTC+3 lokal ───────────────────────────────
    from datetime import timezone, timedelta
    _now = datetime.now(timezone(timedelta(hours=3)))
    _months = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
               "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
    _days = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
    _date_only = f"{_now.day} {_months[_now.month-1]} {_now.year}, {_days[_now.weekday()]}"
    system_content = (
        f"[Sistem Bilgisi]\nBugünün tarihi: {_date_only}\n[/Sistem Bilgisi]\n\n"
        + system_content
    )
    # ─────────────────────────────────────────────────────────────

    # 1. USER MEMORY
    user_memory = await load_user_memory(user_id)
    system_content = system_content.replace(
        "{user_memory}", user_memory or "[USER MEMORY]\nNo memory yet\n[/USER MEMORY]")

    # 2. CONVERSATION SUMMARY
    conv_summary = await load_conversation_summary(conversation_id)
    if conv_summary and len(conv_summary) > 60:  # Yeni/boş summary ekleme
        system_content += f"\n\n{conv_summary}"

    # 2b. CONVERSATION STATE — Redis'ten bağlam
    state_context = await build_state_context(
        conversation_id or "", user_id, user_prompt, mode
    )
    if state_context:
        system_content += f"\n\n{state_context}"
        print(f"[CHAT] State context injected: {len(state_context)} chars")

    # 3. LIVE DATA — Router kararı varsa direkt kullan, yoksa keyword
    _router_dec = {
        "needs_realtime": kwargs.get("needs_realtime"),
        "tool":           kwargs.get("live_type_hint", "none"),
        "confidence":     "high" if kwargs.get("live_type_hint") else "low",
        "intent":         kwargs.get("router_intent", ""),
    } if kwargs.get("live_type_hint") or kwargs.get("needs_realtime") else None


    # Live data: Gemini fast path /chat endpoint'inde handle edildi.
    live_context   = None
    _sources_block = ""


    # 4. RAG CONTEXT
    if rag_context:
        system_content += f"\n\n[RAG Context]\n{rag_context}\n[/RAG Context]"

    # 5. WEB SEARCH (gateway'den geliyor — deep search context)
    if context:
        print(f"[CHAT] Web context received ({len(context)} chars), synthesizing...")
        synthesized = await synthesize_web_results(context, user_prompt, config)
        # ÖNEMLİ: Synthesis'i başa ekle — LLM eğitim verisinden önce bunu görsün
        system_content = (
            f"[GÜNCEL BİLGİ — BUNU KULLAN, EĞİTİM VERİSİNİ DEĞİL]\n"
            f"{synthesized}\n"
            f"[/GÜNCEL BİLGİ]\n\n"
        ) + system_content

    # 6. WORKSPACE DOSYALARI — Kullanıcının paylaştığı kodlar
    if conversation_id:
        ws_files = await load_workspace_files(conversation_id)
        if ws_files:
            ws_text = "\n\n".join([
                f"📁 {f['filename']} ({f['language']}):\n```{f['language']}\n{f['content'][:1500]}\n```"
                for f in ws_files[:3]
            ])
            system_content += f"""

[KULLANICI WORKSPACE]
Bu konuşmada kullanıcının paylaştığı dosyalar:
{ws_text}
Bu dosyalara atıfta bulunabilirsin. "bu kodu düzelt" gibi ifadeler workspace'teki koda işaret eder.
[/KULLANICI WORKSPACE]"""

    # 7. KONUŞMA ÖZETİ — Akıllı özetleme sisteminden geliyor
    if session_summary and session_summary != "[CONVERSATION SUMMARY]\nNew conversation\n[/CONVERSATION SUMMARY]":
        system_content += f"""

[KONUŞMA BAĞLAMI]
{session_summary}
[/KONUŞMA BAĞLAMI]

Not: Yukarıdaki özet bu konuşmanın geçmişini özetler.
Kullanıcı bağlam sorarsa özete başvur, tekrar sormadan yanıtla."""

    messages.append({"role": "system", "content": system_content})

    # ── Akıllı History Seçimi — Önem bazlı sliding window ──────
    # Son 6 mesajı her zaman al (yakın bağlam kritik)
    # Önceki mesajlardan önemli olanları ekle (skor > 0.65)
    # Toplam max 20 mesaj — token maliyetini dengele
    all_history = history or []
    if len(all_history) <= 20:
        selected_history = all_history
    else:
        recent   = all_history[-6:]               # Son 6 kesinlikle
        older    = all_history[:-6]               # Öncekiler
        # Önemlileri seç
        important = [
            m for m in older
            if _score_message_importance(m) > 0.6
        ][-14:]  # Max 14 eski mesaj
        selected_history = important + recent

    for msg in selected_history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Live context varsa user mesajına ekle — system'e değil
    # Gemini grounding mantığı: web verisi = user turn context
    live_ctx = live_context or kwargs.get("live_context", "") or ""
    # SOURCES_BLOCK_PLACEHOLDER LLM'e gitmemeli — temizle
    if live_ctx and "SOURCES_BLOCK_PLACEHOLDER:" in live_ctx:
        live_ctx = live_ctx[:live_ctx.find("SOURCES_BLOCK_PLACEHOLDER:")].rstrip()
    if live_ctx:
        enriched_prompt = f"{live_ctx}\n\nKullanıcı sorusu: {user_prompt}"
    else:
        enriched_prompt = user_prompt

    # Görsel varsa multimodal content oluştur
    if image_data and image_type:
        media_type = image_type if image_type.startswith("image/") else f"image/{image_type}"
        user_content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{image_data}"},
            },
            {"type": "text", "text": enriched_prompt},
        ]
        print(f"[VISION] Multimodal mesaj oluşturuldu ({media_type}, {len(image_data)} chars)")
    else:
        user_content = enriched_prompt

    messages.append({"role": "user", "content": user_content})

    return messages


# ═══════════════════════════════════════════════════════════════
# CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Ana chat endpoint.

    LIVE DATA AKIŞI:
    1. build_messages() → get_live_data(prompt, mode) çağırır
    2. get_live_data() → smart_tools /classify → karar
    3. Canlı veri lazımsa → smart_tools /live → formatted context
    4. Context system prompt'a eklenir
    5. LLM cevaplar — gerçek verilerle

    ÖRNEK:
    "güncel euro kaç tl"
      → get_live_data() → /classify → live_utility/currency
      → /live → "1 EUR = 43.52 TRY"
      → system: "[Canlı Veri — CURRENCY]\n💱 1 EUR = 43.52 TRY"
      → LLM: "Euro şu an 43.52 TL" ✅
    """

    if request.mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")

    config = MODE_CONFIGS[request.mode]
    show_thinking = should_show_thinking(request.prompt, request.mode, request.history or [])

    config        = MODE_CONFIGS[request.mode]

    # ── INTENT DETECTION — Canlı veri vs sohbet ───────────────
    #
    # Mantık: Sadece NET canlı veri ifadeleri smart-tools'a yönlendirilir.
    # Şüpheli ya da kod/devam ifadeleri DeepSeek'e gider.
    #
    # Net canlı veri sinyalleri:
    #   - "hava durumu", "hava nasıl"           → weather
    #   - "dolar kaç", "kur ne", "döviz kuru"   → currency
    #   - "bitcoin fiyat", "btc ne kadar"       → crypto
    #   - "saat kaç"                            → time
    #   - "son haberler", "gündem ne"           → news
    #
    # NOT: "kodu devam ettir", "açıkla", "yaz" gibi ifadeler
    # asla canlı veri olarak yorumlanmaz.

    _q = (request.prompt or "").lower().strip()
    _words = _q.split()
    _wc = len(_words)

    # Konuşma kod/devam niyetinde mi? Bu varsa hiç canlı veri tetikleme.
    # ÖNEMLİ: Kelime SINIRI ile kontrol — "bug" "bugün" içinde tetiklemesin.
    _NON_LIVE_INTENT = (
        "kod", "kodu", "yaz", "yazsana",
        "düzelt", "fix", "refactor", "refaktör",
        "devam", "ettir", "tamamla", "bitir",
        "açıkla", "anlat",
        "fonksiyon", "function", "class", "import", "method",
        "hata", "error", "bug", "exception", "traceback",
        "test", "örnek",
    )
    # Kelime bazlı bool kontrolü (substring değil)
    import re as _re
    _q_tokens = set(_re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+", _q))
    _has_non_live = any(w in _q_tokens for w in _NON_LIVE_INTENT)
    # Çok-kelimeli phrase'ler için ayrıca substring kontrolü
    _NON_LIVE_PHRASES = ("test yaz", "test ekle", "unit test", "örnek ver",
                         "örnek göster", "nasıl yaparım", "nasıl yapılır",
                         "yazar mısın", "yazabilir mis")
    if not _has_non_live:
        _has_non_live = any(p in _q for p in _NON_LIVE_PHRASES)

    # NET canlı veri pattern'leri (cümlede gerçekten canlı veri sorulmuş)
    _NET_WEATHER  = ("hava durumu", "hava nasıl", "havalar nasıl",
                     "hava kaç derece", "kaç derece", "yağmur yağıyor",
                     "kar yağıyor", "bugün hava", "yarın hava")
    _NET_CURRENCY = ("dolar kaç", "euro kaç", "sterlin kaç", "tl kaç",
                     "dolar kuru", "euro kuru", "döviz kuru", "kur ne",
                     "kaç tl", "exchange rate", "usd kaç", "eur kaç")
    _NET_CRYPTO   = ("bitcoin kaç", "btc kaç", "bitcoin fiyat", "btc fiyat",
                     "ethereum kaç", "eth kaç", "kripto fiyat",
                     "btc ne kadar", "bitcoin ne kadar")
    _NET_TIME     = ("saat kaç", "saat kac", "şimdi saat", "şu an saat",
                     "günün saati", "what time")
    _NET_NEWS     = ("son haberler", "güncel haberler", "son dakika",
                     "gündem ne", "haberleri getir", "breaking news",
                     "latest news", "today's news")

    _lt = None
    if not _has_non_live:
        # Sadece NET ifade varsa canlı veriye yönlendir
        if   any(s in _q for s in _NET_WEATHER):  _lt = "weather"
        elif any(s in _q for s in _NET_CURRENCY): _lt = "currency"
        elif any(s in _q for s in _NET_CRYPTO):   _lt = "crypto"
        elif any(s in _q for s in _NET_TIME):     _lt = "time"
        elif any(s in _q for s in _NET_NEWS):     _lt = "news"

        # Kısa mesaj follow-up: önceki cevap canlı veriyse devam
        # ÖRN: "İstanbul ne kadar?" (önceki: "Ankara'da hava 22°C")
        if not _lt and _wc <= 4 and request.history:
            for msg in reversed(request.history[-4:]):
                if msg.get("role") != "assistant":
                    continue
                c = (msg.get("content") or "").lower()
                if any(x in c for x in ["°c", "°f", "sıcaklık", "hissedilen", "rüzgar"]):
                    # Şehir adı gerekli (yoksa devam etme)
                    cities = ["istanbul","ankara","izmir","antalya","bursa","adana",
                              "konya","berlin","london","paris","tokyo","dubai",
                              "amsterdam","madrid","rome","new york"]
                    if any(c2 in _q for c2 in cities):
                        _lt = "weather"
                    break
                if "1 usd" in c or "1 eur" in c or " try" in c:
                    if any(x in _q for x in ["dolar","euro","sterlin","kur","tl","gbp","jpy","chf"]):
                        _lt = "currency"
                    break

    if _lt:
        print(f"[DETECT] '{request.prompt[:50]}' → {_lt}")

    # Ücretsiz API: weather, currency, crypto, time → smart_tools (limit yok)
    if _lt in _FREE_API_TYPES and SMART_TOOLS_URL:
        print(f"[FREE API + RAG] '{request.prompt[:50]}' → {_lt}")
        # Yeni akış (RAG):
        #   1. Smart-tools'tan ham veri al (hava, kur, kripto, saat)
        #   2. DeepSeek'e bu veriyi context olarak ver
        #   3. DeepSeek doğal Türkçe cevap yazar
        _live_data_text = ""
        try:
            async with httpx.AsyncClient(timeout=6.0) as _c:
                _r = await _c.post(
                    f"{SMART_TOOLS_URL}/unified",
                    json={"query": request.prompt, "tool_type": _lt},
                )
                if _r.status_code == 200:
                    _d = _r.json()
                    if _d.get("success"):
                        _data = _d.get("data", {})
                        _live_data_text = (_data.get("formatted") or
                                           _data.get("formatted_tr") or
                                           _data.get("short") or "")
                        if _live_data_text:
                            print(f"[FREE API + RAG] ✅ {_lt} | {len(_live_data_text)} chars → DeepSeek yorumlayacak")
        except Exception as _e:
            print(f"[FREE API + RAG] ⚠ smart-tools fail: {_e}")

        if not _live_data_text:
            # Smart-tools başarısızsa Gemini grounding'e düş
            print(f"[FREE API + RAG] Veri yok → Gemini grounding fallback")
            async def _gemini_fallback():
                async for chunk in gemini_live_stream(request.prompt, _lt):
                    if chunk: yield chunk
            return StreamingResponse(_gemini_fallback(), media_type="text/plain; charset=utf-8")

        # Veriyi context'e koy → normal DeepSeek akışına devam et
        # request.context'a inject ediyoruz, build_messages bunu system prompt'a alacak
        _label = {
            "weather":  "🌤 GÜNCEL HAVA DURUMU",
            "currency": "💱 GÜNCEL DÖVİZ KURU",
            "crypto":   "₿ GÜNCEL KRİPTO FİYATI",
            "time":     "🕐 GÜNCEL TARİH/SAAT",
        }.get(_lt, "📊 GÜNCEL VERİ")

        _injected = (
            f"\n\n[{_label} — bu veriyi kullan, kendin uydurma]\n"
            f"{_live_data_text}\n"
            f"[BU VERİYE GÖRE KULLANICININ SORUSUNU CEVAPLA - DOĞAL TÜRKÇE]\n"
        )
        # Mevcut context'e ekle
        if request.context:
            request.context = (request.context or "") + _injected
        else:
            request.context = _injected
        # Devam et — ana DeepSeek akışına düşecek (return YOK, akış aşağı iniyor)

    # Gemini: web_search, news, deep_search, price_search → Google Search Grounding
    if _lt in _GEMINI_TYPES:
        print(f"[GEMINI FAST] '{request.prompt[:50]}' → {_lt}")
        async def _gs():
            has = False
            async for chunk in gemini_live_stream(request.prompt, _lt):
                if chunk: has = True; yield chunk
            if not has: yield "Üzgünüm, yanıt üretemiyorum."
        return StreamingResponse(_gs(), media_type="text/plain; charset=utf-8")
    # ─────────────────────────────────────────────────────────config        = MODE_CONFIGS[request.mode]
    show_thinking = should_show_thinking(request.prompt, request.mode, request.history or [])

    messages = await build_messages(
        mode=request.mode, user_id=request.user_id,
        conversation_id=request.conversation_id,
        user_prompt=request.prompt,
        history=request.history or [],
        rag_context=request.rag_context,
        context=request.context,
        session_summary=request.session_summary,
        config=config,
        image_data      = request.image_data,
        image_type      = request.image_type,
        live_type_hint  = request.live_type_hint,
        router_intent   = request.router_intent,
        router_thinking = request.router_thinking,
        user_level      = request.user_level,
        needs_realtime  = request.needs_realtime,
    )

    async def response_generator():
        try:
            buffer    = ""
            full_text = ""  # State update için tam cevabı topla

            # Kullanıcı mesajında kod varsa workspace'e kaydet
            if request.conversation_id:
                asyncio.create_task(
                    process_user_message_for_workspace(
                        request.conversation_id,
                        request.prompt
                    )
                )

            # Fake step'ler kaldırıldı (deep search + thinking_steps).
            # Gerçek reasoning için: adaptive thinking → reasoning_effort
            # → DeepSeek <think>...</think> tag'leri frontend'de baloncuk olarak gösterilir.

            # Model log — hangi model cevaplıyor
            _has_image = bool(request.image_data)
            _model_name = "gemini-2.5-flash-lite (vision)" if _has_image else config["model"]
            print(f"[MODEL] mode={request.mode} | model={_model_name} | max_tokens={config['max_tokens']} | thinking={request.thinking_mode} | vision={_has_image}")

            # Streaming response — Görsel varsa Gemini, yoksa text provider'a göre
            # CHAT_PROVIDER env: deepseek (default) | gemini
            _chat_provider = os.getenv("CHAT_PROVIDER", "deepseek").strip().lower()

            if _has_image:
                # Multimodal akış — Gemini Flash Lite (her durumda)
                _sys_prompt = config.get("system_prompt", "")
                stream_iter = gemini_vision_stream(
                    prompt=request.prompt,
                    image_data=request.image_data,
                    image_type=request.image_type or "image/jpeg",
                    history=request.history or [],
                    system_prompt=_sys_prompt,
                    max_tokens=config["max_tokens"],
                    temperature=config["temperature"],
                )
            elif _chat_provider == "gemini":
                # Text akışı — Gemini (env ile aktif)
                _sys_prompt = config.get("system_prompt", "")
                stream_iter = gemini_vision_stream(
                    prompt=request.prompt,
                    image_data="",  # boş → text-only mode
                    image_type="",
                    history=request.history or [],
                    system_prompt=_sys_prompt,
                    max_tokens=config["max_tokens"],
                    temperature=config["temperature"],
                )
            else:
                # Text akışı — DeepSeek V4 Flash (default)
                stream_iter = stream_deepinfra_completion(
                    messages=messages,
                    model=config["model"], max_tokens=config["max_tokens"],
                    temperature=config["temperature"], top_p=config["top_p"],
                    reasoning_effort=request.thinking_mode if config.get("supports_thinking") else None,
                )

            async for chunk in stream_iter:
                buffer    += chunk
                full_text += chunk
                if any(c in buffer for c in [' ','.','!','?','\n',',']) or len(buffer) > 10:
                    # SSE chunk'tan bracket tag'ları temizle — frontend'e sızmaz
                    clean_buf = _strip_internal_tags(buffer)
                    yield clean_buf
                    buffer = ""
            if buffer:
                full_text += buffer
                clean_buf = _strip_internal_tags(buffer)
                yield clean_buf

            # Kaynak listesi — LLM yanıtının hemen ardından gönder
            _sb = kwargs.get("sources_block", "") if "kwargs" in dir() else ""
            if not _sb:
                # build_messages'den gelen sources_block'u messages'dan çıkar
                for _m in messages:
                    _mc = _m.get("content", "")
                    if isinstance(_mc, str) and "SOURCES_BLOCK_PLACEHOLDER:" in _mc:
                        _idx = _mc.find("SOURCES_BLOCK_PLACEHOLDER:")
                        _sb  = _mc[_idx + len("SOURCES_BLOCK_PLACEHOLDER:"):].strip()
                        break
            if _sb:
                yield f"\n\n{_sb}"
                full_text += f"\n\n{_sb}"

            # Periodic summary (background)
            if request.conversation_id:
                asyncio.create_task(check_and_create_summary_async(
                    request.user_id, request.conversation_id, config))

            # State güncelle (background — response'u bloklamaz)
            if request.conversation_id:
                asyncio.create_task(extract_and_update_state(
                    conversation_id=request.conversation_id,
                    user_id=request.user_id,
                    prompt=request.prompt,
                    response=full_text,
                    intent=request.mode,
                    mode=request.mode,
                ))

        except Exception as e:
            yield f"\n\n⚠️ Bir hata oluştu: {str(e)}"

    return StreamingResponse(response_generator(), media_type="text/plain; charset=utf-8")


# ═══════════════════════════════════════════════════════════════
# SSE ENDPOINT — Claude gibi event stream
# ═══════════════════════════════════════════════════════════════

@app.post("/chat/sse")
async def chat_sse(request: ChatRequest):
    """
    SSE (Server-Sent Events) endpoint.
    Claude gibi event tiplerine göre ayrı kanaldan gönderir.

    Event tipleri:
      step       → araç adımı (search, scrape, chunk, rerank, generate, reflect)
      text       → gerçek cevap içeriği (streaming)
      done       → tamamlandı
      error      → hata

    Frontend kullanımı:
      const evtSource = new EventSource('/chat/sse', {method:'POST'})
      evtSource.addEventListener('step', e => showStep(JSON.parse(e.data)))
      evtSource.addEventListener('text', e => appendText(e.data))
      evtSource.addEventListener('done', e => finalize())
    """
    import json as _json

    user_id = request.user_id
    mode    = request.mode or "assistant"
    prompt  = request.prompt

    if mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    config = MODE_CONFIGS[mode]

    def sse_event(event_type: str, data) -> str:
        """SSE formatı: event: TYPE\ndata: DATA\n\n"""
        if isinstance(data, dict):
            data = _json.dumps(data, ensure_ascii=False)
        return f"event: {event_type}\ndata: {data}\n\n"

    async def sse_generator():
        try:
            # Fake step'ler kaldırıldı.
            # Gerçek reasoning için: adaptive thinking → reasoning_effort (DeepSeek)
            #                       veya Gemini thinking_config (vision/Gemini akışı)
            live_context, sources_block = None, ""
            is_deep = False
            is_tr   = any(c in prompt for c in "çğışöüÇĞİŞÖÜ") or True

            # Mesajları hazırla
            messages = await build_messages(
                mode=mode, user_id=user_id,
                conversation_id=request.conversation_id,
                user_prompt=prompt,
                history=request.history or [],
                rag_context=request.rag_context,
                context=live_context,
                session_summary=request.session_summary,
                config=config,
            )

            # ── Text streaming ────────────────────────────────────
            full_text = ""
            async for chunk in stream_deepinfra_completion(
                messages=messages,
                model=config["model"],
                max_tokens=config["max_tokens"],
                temperature=config["temperature"],
                top_p=config["top_p"],
                reasoning_effort=request.thinking_mode if config.get("supports_thinking") else None,
            ):
                full_text += chunk
                yield sse_event("text", chunk)

            # Reflect step kaldırıldı (deep search fake-step temizliği)

            # Kaynak listesi — ayrı event olarak gönder (LLM yanıtına bağımlı değil)
            if sources_block:
                yield sse_event("sources", sources_block)

            # Tüm adımları done yap
            yield sse_event("done", {
                "total_chars": len(full_text),
                "mode":        mode,
                "has_sources": bool(sources_block),
            })

            # Background tasks
            if request.conversation_id:
                asyncio.create_task(check_and_create_summary_async(
                    user_id, request.conversation_id, config))

        except Exception as e:
            yield sse_event("error", {"message": str(e)})

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",  # Nginx buffer'ı kapat
            "Connection":      "keep-alive",
        },
    )


async def check_and_create_summary_async(user_id: int, conversation_id: str, config: Dict):
    try:
        if await should_create_summary(conversation_id):
            await create_conversation_summary(user_id, conversation_id, config)
    except Exception as e:
        print(f"[SUMMARY BACKGROUND ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status":  "healthy",
        "service": "chat",
        "version": "3.1.0",
        "modes":   list(MODE_CONFIGS.keys()),
        "features": {
            "memory_system":    db_pool is not None,
            "thinking_display": True,
            "web_synthesis":    True,
            "auto_summaries":   True,
            "live_data_router": bool(SMART_TOOLS_URL),
            "code_context":     True,
        },
    }


@app.get("/")
async def root():
    return {
        "service": "Skylight Chat Service",
        "version": "3.1.0",
        "live_data": f"LiveDataRouter → {SMART_TOOLS_URL}/classify + /live",
        "modes":     list(MODE_CONFIGS.keys()),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)