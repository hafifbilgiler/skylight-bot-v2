"""
═══════════════════════════════════════════════════════════════
SKYLIGHT — SMART ROUTER v5.0  (Hybrid + Cache)
═══════════════════════════════════════════════════════════════

v4 → v5 değişiklikleri:
  ✅ HİBRİT AKIŞ — Önce keyword (10ms), sonra LLM (sadece belirsiz)
  ✅ Redis cache (1 saat TTL) — aynı sorgu LLM'e gitmez
  ✅ JSON FORCED (response_format) — parse hatası bitti
  ✅ Sadeleştirilmiş LLM prompt — max_tokens 80
  ✅ code_execute intent — "decode et, hesapla, çevir"
  ✅ Yeni Hot Path: greeting, currency, weather, time direkt
  ✅ Timeout 5sn → 3sn

Akış:
  1. CACHE     → varsa direkt dön (~5ms)
  2. HOT PATH  → keyword direkt dön (~10ms)
  3. LLM       → sadece belirsiz mesajlar (~800-1500ms)
  4. FALLBACK  → akıllı keyword (LLM hata verirse)
═══════════════════════════════════════════════════════════════
"""

import json
import os
import re
import hashlib
from typing import Optional, List, Dict

import httpx

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
ROUTER_MODEL       = os.getenv("DEEPINFRA_ROUTER_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct")
ROUTER_TIMEOUT     = float(os.getenv("ROUTER_TIMEOUT", "3.0"))   # 5→3 sn

REDIS_URL          = os.getenv("REDIS_URL", "redis://skylight-redis:6379")
ROUTER_CACHE_DB    = int(os.getenv("ROUTER_CACHE_DB", "5"))      # DB 5 boş
CACHE_TTL          = int(os.getenv("ROUTER_CACHE_TTL", "3600"))  # 1 saat

_redis_pool = None


async def _get_cache():
    """Lazy Redis connection. Hata olursa None döner — cache susuz çalışır."""
    global _redis_pool
    if not _REDIS_AVAILABLE:
        return None
    if _redis_pool is not None:
        return _redis_pool
    try:
        _redis_pool = await aioredis.from_url(
            f"{REDIS_URL}/{ROUTER_CACHE_DB}",
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,
            socket_connect_timeout=1.0,
        )
        return _redis_pool
    except Exception as e:
        print(f"[ROUTER CACHE] Bağlanamadı: {e}")
        _redis_pool = None
        return None


def _cache_key(prompt: str, history: List[Dict]) -> str:
    """Cache key: prompt + son mesajın hash'i."""
    last_msg = ""
    if history:
        last_msg = (history[-1].get("content") or "")[:80]
    raw = f"{prompt[:200]}|{last_msg}"
    return f"router:v5:{hashlib.md5(raw.encode()).hexdigest()[:16]}"


# ─────────────────────────────────────────────────────────────
# SADELEŞTİRİLMİŞ LLM PROMPT
# ─────────────────────────────────────────────────────────────

_LLM_SYSTEM = """Sen bir AI mesaj sınıflandırıcısısın. Mesajı analiz et, SADECE JSON döndür.

INTENT seçenekleri:
- general_chat       (sohbet, selam)
- greeting           (merhaba, selam)
- emotional_support  (üzgün, yorgun, mutsuz)
- code_generate      (yaz, oluştur, kodla — yeni kod)
- code_debug         (hata var, çalışmıyor — debug)
- code_modify        (değiştir, refactor — mevcut kodu)
- code_explain       (bu kod ne yapıyor, açıkla)
- code_execute       (decode et, encode et, hesapla, çevir bunu — KOD YAZMA, YAP)
- architecture_design (mimari, system design)
- live_currency      (dolar/euro kaç)
- live_crypto        (bitcoin fiyat)
- live_weather       (hava durumu)
- live_time          (saat kaç)
- live_news          (haberler)
- web_search         (güncel araştırma)
- image_generate     (görsel oluştur, resim yap)
- how_to             (nasıl yapılır)
- concept_explain    (X nedir)
- comparison         (X vs Y)
- opinion_request    (ne düşünüyorsun)

ROUTE seçenekleri: chat, code, live_data, deep_search, image_gen, emotional
MODEL seçenekleri: qwen3 (kod), llama4 (genel), auto

ÖRNEKLER:
"merhaba" → {"intent":"greeting","route":"chat","model":"llama4","tool":"none","needs_realtime":false}
"dolar kaç" → {"intent":"live_currency","route":"live_data","model":"auto","tool":"currency","needs_realtime":true}
"FastAPI auth yaz" → {"intent":"code_generate","route":"code","model":"qwen3","tool":"none","needs_realtime":false}
"şunu base64 decode et" → {"intent":"code_execute","route":"chat","model":"llama4","tool":"none","needs_realtime":false}
"237*89 kaç" → {"intent":"code_execute","route":"chat","model":"llama4","tool":"none","needs_realtime":false}
"yine hata aldım" → {"intent":"code_debug","route":"code","model":"qwen3","tool":"none","needs_realtime":false}
"REST nedir" → {"intent":"concept_explain","route":"chat","model":"llama4","tool":"none","needs_realtime":false}

Çıktı formatı (SADECE bu):
{"intent":"...","route":"...","model":"...","tool":"...","needs_realtime":false,"confidence":"high"}"""


def _build_user_msg(prompt: str, history: List[Dict]) -> str:
    """Kısa bağlam + mesaj."""
    parts = []
    if history:
        last = history[-1] if history else {}
        if last.get("role") == "assistant":
            c = (last.get("content") or "")[:120]
            if c:
                parts.append(f"[Önceki cevap: {c}]")
    parts.append(f"Mesaj: {prompt[:300]}")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# HOT PATH — Keyword tabanlı, LLM'e gitmeden direkt karar
# %80 mesaj burada çözülür
# ─────────────────────────────────────────────────────────────

# Selamlama
_GREETING_KW = (
    "merhaba", "selam", "hey", "hi", "hello", "naber", "günaydın",
    "iyi akşamlar", "iyi geceler", "selamlar", "slm", "mrb",
)

# Duygusal
_EMOTIONAL_KW = (
    "üzgünüm", "mutsuzum", "yoruldum", "sıkıldım", "bunaldım", "depresyon",
    "yalnız hissediyorum", "ağlıyorum", "stres", "kaygılı", "endişeli",
    "moralim bozuk", "kötü hissediyorum",
)

# Code execute — "yap, çalıştır, hesapla, decode/encode et, çevir"
# KRİTİK: Bunlar kod YAZMA değil, kod ÇALIŞTIRMA niyetidir
_CODE_EXECUTE_KW = (
    # Encoding/Decoding
    "decode et", "encode et", "decode yap", "encode yap",
    "base64 decode", "base64 encode",
    "url decode", "url encode", "html decode", "html encode",
    # Hesaplama
    "hesapla şu", "hesapla bunu", "kaç eder", "ne eder", "ne yapar",
    # Çeviri
    "çevir şunu", "çevir bunu", "convert et", "ingilizceye çevir",
    "türkçeye çevir", "translate this", "translate the",
    # Format
    "json'a çevir", "yaml'a çevir", "csv'ye çevir",
    "json olarak ver", "yaml olarak ver",
)

# Code generate — "yaz, oluştur, implement"
_CODE_VERBS = (" yaz", "yaz ", "implement", "kodla", "oluştur", "geliştir", "create a", "write a", "build a")
_CODE_OBJECTS = ("fonksiyon", "function", "class", "api", "endpoint", "script", "servis", "bot", "uygulama", "method")

# Code debug
_CODE_DEBUG_KW = (
    "hata alıyorum", "hata veriyor", "hata var", "çalışmıyor", "çöktü",
    "traceback", "exception", "error:", "broken", "patladı", "sorun var",
    "düzelt", "fix", "neden çalışmıyor", "bug",
)

# Live data — currency
_CURRENCY_KW = (
    "dolar kaç", "euro kaç", "sterlin kaç", "döviz kuru", "kur ne",
    "dolar fiyat", "euro fiyat", "tl kaç", "usd kaç", "eur kaç",
    "$ kaç", "€ kaç", "exchange rate",
)

# Live data — crypto
_CRYPTO_KW = (
    "bitcoin kaç", "btc kaç", "ethereum kaç", "eth kaç", "kripto fiyat",
    "bitcoin fiyat", "btc fiyat", "doge kaç", "solana kaç",
)

# Live data — weather
_WEATHER_KW = (
    "hava durumu", "hava nasıl", "hava kaç derece", "sıcaklık nasıl",
    "yağmur yağacak", "kar yağacak", "weather", "forecast",
)

# Live data — time
_TIME_KW = (
    "saat kaç", "saat kac", "şu an saat", "şimdi saat",
    "günün tarihi", "bugün ne günü", "what time",
)

# Live data — news
_NEWS_KW = (
    "son haberler", "güncel haberler", "son dakika", "haberleri",
    "breaking news", "latest news", "gündem ne",
)

# Image generate
_IMAGE_KW = (
    "görsel oluştur", "görsel yap", "resim çiz", "resim yap", "resim oluştur",
    "görsel üret", "imagery", "illustration", "generate image", "draw a",
    "thumbnail yap", "kapak görseli", "poster yap", "logo tasarla",
)

# Concept learn (nedir, ne demek)
_CONCEPT_KW = ("nedir", "ne demek", "ne anlama gel", "what is", "what does")

# How-to (nasıl yapılır)
_HOWTO_KW = ("nasıl yapılır", "nasıl yaparım", "nasıl yapabilirim", "how to", "how do i", "how can i")


def _is_tr(prompt: str) -> bool:
    return any(c in prompt for c in "çğışöüÇĞİŞÖÜ")


def _detect_level(prompt: str) -> str:
    tech = ["api", "endpoint", "async", "kubernetes", "docker", "postgresql",
            "redis", "nginx", "fastapi", "jwt", "oauth", "kafka", "grpc"]
    n = sum(1 for t in tech if t in prompt.lower())
    return "expert" if n >= 3 else "intermediate" if n >= 1 else "unknown"


def hot_path(prompt: str, history: List[Dict]) -> Optional[Dict]:
    """
    Hızlı keyword analizi.
    Confidence yüksekse LLM'e gitmeden karar dön.
    None dönerse → LLM'e git.
    """
    p = prompt.lower().strip()
    p_words = p.split()
    word_count = len(p_words)
    lang = "tr" if _is_tr(prompt) else "en"
    level = _detect_level(prompt)

    # Sadece selam — kısa mesaj
    if word_count <= 3:
        if any(p == g or p.startswith(g + " ") or p.startswith(g + "!") for g in _GREETING_KW):
            return _mk("chat", "greeting", "llama4", lang=lang, level=level,
                       thinking="Selamlama")

    # ── 1. CODE EXECUTE — "decode et, hesapla, çevir" (öncelikli!) ─
    if any(kw in p for kw in _CODE_EXECUTE_KW):
        return _mk("chat", "code_execute", "llama4", lang=lang, level=level,
                   thinking="Kod çalıştırma/dönüştürme isteği — Llama yapsın, kod yazma")

    # ── 2. LIVE DATA — anlık veri ────────────────────────────
    if any(kw in p for kw in _CURRENCY_KW):
        return _mk("live_data", "live_currency", "auto", "currency", True,
                   lang=lang, level=level, thinking="Döviz sorgusu")

    if any(kw in p for kw in _CRYPTO_KW):
        return _mk("live_data", "live_crypto", "auto", "crypto", True,
                   lang=lang, level=level, thinking="Kripto sorgusu")

    if any(kw in p for kw in _WEATHER_KW):
        return _mk("live_data", "live_weather", "auto", "weather", True,
                   lang=lang, level=level, thinking="Hava durumu")

    if any(kw in p for kw in _TIME_KW):
        return _mk("live_data", "live_time", "auto", "time", True,
                   lang=lang, level=level, thinking="Saat/tarih")

    if any(kw in p for kw in _NEWS_KW):
        return _mk("live_data", "live_news", "auto", "news", True,
                   lang=lang, level=level, thinking="Haber")

    # ── 3. IMAGE GENERATE ─────────────────────────────────────
    if any(kw in p for kw in _IMAGE_KW):
        return _mk("image_gen", "image_generate", "auto",
                   lang=lang, level=level, thinking="Görsel üretim")

    # ── 4. EMOTIONAL ──────────────────────────────────────────
    if any(kw in p for kw in _EMOTIONAL_KW):
        return _mk("emotional", "emotional_support", "llama4",
                   lang=lang, level=level, thinking="Duygusal destek")

    # ── 5. CODE DEBUG ─────────────────────────────────────────
    if any(kw in p for kw in _CODE_DEBUG_KW):
        return _mk("code", "code_debug", "qwen3",
                   lang=lang, level=level, thinking="Kod hatası/debug")

    # ── 6. CODE GENERATE — fiil + nesne kombinasyonu ──────────
    has_code_verb = any(v in p for v in _CODE_VERBS)
    has_code_obj = any(o in p for o in _CODE_OBJECTS)
    if has_code_verb and has_code_obj:
        return _mk("code", "code_generate", "qwen3",
                   lang=lang, level=level, thinking="Kod yazma isteği")

    # ── 7. CONCEPT LEARN — "X nedir" (kısa mesaj + nedir) ──────
    if word_count <= 6 and any(kw in p for kw in _CONCEPT_KW):
        return _mk("chat", "concept_explain", "llama4",
                   lang=lang, level=level, thinking="Kavram öğrenme")

    # ── 8. HOW-TO ─────────────────────────────────────────────
    if any(kw in p for kw in _HOWTO_KW):
        # "yaz/kodla" varsa code, yoksa chat
        if has_code_verb:
            return _mk("code", "code_generate", "qwen3",
                       lang=lang, level=level, thinking="Nasıl + yaz → kod")
        return _mk("chat", "how_to", "llama4",
                   lang=lang, level=level, thinking="Nasıl yapılır → açıklama")

    # Hot path bulamadı → LLM'e git
    return None


def _mk(route: str, intent: str, model: str = "auto", tool: str = "none",
        needs_rt: bool = False, conf: str = "high",
        lang: str = "tr", level: str = "unknown", thinking: str = "") -> Dict:
    return {
        "route":          route,
        "intent":         intent,
        "needs_realtime": needs_rt,
        "tool":           tool,
        "model":          model,
        "confidence":     conf,
        "language":       lang,
        "user_level":     level,
        "ambiguous":      False,
        "clarification_needed": None,
        "thinking":       thinking,
        "_source":        "hot_path",
    }


# ─────────────────────────────────────────────────────────────
# LLM ROUTE — Sadece belirsiz mesajlar için
# ─────────────────────────────────────────────────────────────

async def llm_route(prompt: str, history: List[Dict]) -> Optional[Dict]:
    """
    Belirsiz mesajlar için tiny LLM.
    JSON forced, max_tokens 80, timeout 3sn.
    """
    if not DEEPINFRA_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT) as client:
            resp = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      ROUTER_MODEL,
                    "messages":   [
                        {"role": "system", "content": _LLM_SYSTEM},
                        {"role": "user",   "content": _build_user_msg(prompt, history)},
                    ],
                    "max_tokens":      120,
                    "temperature":     0.0,
                    "response_format": {"type": "json_object"},   # JSON FORCED
                    "stream":          False,
                },
            )

        if resp.status_code != 200:
            print(f"[ROUTER LLM] HTTP {resp.status_code}")
            return None

        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        if not raw:
            return None

        # JSON parse — response_format zaten JSON garantiliyor ama yine guard
        clean = raw.strip()
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            clean = m.group(0)

        try:
            result = json.loads(clean)
        except json.JSONDecodeError:
            print(f"[ROUTER LLM] JSON fail: {clean[:80]}")
            return None

        # Eksik alanları doldur
        result.setdefault("route", "chat")
        result.setdefault("intent", "general_chat")
        result.setdefault("model", "llama4")
        result.setdefault("tool", "none")
        result.setdefault("needs_realtime", False)
        result.setdefault("confidence", "medium")
        result.setdefault("language", "tr" if _is_tr(prompt) else "en")
        result.setdefault("user_level", _detect_level(prompt))
        result.setdefault("ambiguous", False)
        result.setdefault("clarification_needed", None)
        result.setdefault("thinking", "LLM kararı")
        result["_source"] = "llm"

        print(f"[ROUTER LLM] ✓ {result['intent']} → {result['route']} | model={result['model']}")
        return result

    except httpx.TimeoutException:
        print(f"[ROUTER LLM] Timeout {ROUTER_TIMEOUT}s")
        return None
    except Exception as e:
        print(f"[ROUTER LLM] Hata: {type(e).__name__}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# AKILLI FALLBACK — LLM hata verirse devreye girer
# ─────────────────────────────────────────────────────────────

def smart_fallback(prompt: str, history: List[Dict]) -> Dict:
    """
    Hot path bulamadı, LLM de fail oldu → akıllı fallback.
    Default: chat / general_chat
    """
    p = prompt.lower()
    lang = "tr" if _is_tr(prompt) else "en"
    level = _detect_level(prompt)

    # Bağlam kontrolü — önceki mesajda kod varsa kod modu
    has_recent_code = any("```" in m.get("content", "") for m in history[-4:])
    if has_recent_code and len(prompt.split()) <= 8:
        # Kısa cevap kod bağlamında
        if any(w in p for w in ["değiştir", "düzelt", "ekle", "kaldır"]):
            return _mk("code", "code_modify", "qwen3", lang=lang, level=level,
                       thinking="Önceki kod bağlamı + değişiklik isteği",
                       conf="medium")

    # Genel chat
    return _mk("chat", "general_chat", "llama4", lang=lang, level=level,
               thinking="Belirgin sinyal yok → genel sohbet",
               conf="low")


# ─────────────────────────────────────────────────────────────
# ANA FONKSİYON — Hibrit akış
# ─────────────────────────────────────────────────────────────

async def route_message(prompt: str, history: List[Dict] = None) -> Dict:
    """
    Hibrit routing:
      1. Cache → varsa direkt dön (5ms)
      2. Hot path → keyword (10ms)
      3. LLM → belirsiz mesaj (800-1500ms)
      4. Fallback → LLM hata verirse
    """
    history = history or []

    # 1. CACHE
    cache = await _get_cache()
    cache_key = _cache_key(prompt, history)
    if cache:
        try:
            cached = await cache.get(cache_key)
            if cached:
                result = json.loads(cached)
                result["_source"] = "cache"
                print(f"[ROUTER CACHE] ✓ {result.get('intent')} (hit)")
                return result
        except Exception:
            pass

    # 2. HOT PATH
    result = hot_path(prompt, history)
    if result:
        thinking = result.get("thinking", "")
        print(f"[ROUTER HOT] ✓ {result['intent']} → {result['route']} | {thinking[:50]}")
        if cache:
            try:
                await cache.setex(cache_key, CACHE_TTL, json.dumps(result, ensure_ascii=False))
            except Exception:
                pass
        return result

    # 3. LLM
    result = await llm_route(prompt, history)
    if result:
        if cache:
            try:
                await cache.setex(cache_key, CACHE_TTL, json.dumps(result, ensure_ascii=False))
            except Exception:
                pass
        return result

    # 4. FALLBACK
    result = smart_fallback(prompt, history)
    print(f"[ROUTER FALLBACK] ⚠ {result['intent']} | {result.get('thinking','')[:50]}")
    return result


# ─────────────────────────────────────────────────────────────
# GATEWAY ENTEGRASYON
# ─────────────────────────────────────────────────────────────

def router_to_gateway_mode(decision: Dict) -> str:
    """
    Router kararını gateway mode'una çevir.

    YENİ: code_execute → assistant (Llama-4 yapsın, Qwen kod yazma)
    """
    route  = decision.get("route",  "chat")
    intent = decision.get("intent", "general_chat")
    model  = decision.get("model",  "auto")
    level  = decision.get("user_level", "unknown")

    # Kod EXECUTION (decode/encode/hesapla) → assistant
    # Qwen kod uzmanı, ama "decode et" gerçek bir iş yapma — Llama-4 yapsın
    if intent == "code_execute":
        return "assistant"

    # Kod yazma/debug → Qwen3-Coder
    if route == "code" or model == "qwen3":
        return "code"

    # Expert + teknik → IT Expert
    if level == "expert" and route == "chat" and intent in (
        "architecture_design", "how_to", "concept_explain", "step_by_step"
    ):
        return "it_expert"

    # Canlı veri / araştırma → assistant (live data inject ile)
    if route in ("live_data", "deep_search"):
        return "assistant"

    # Görsel
    if route == "image_gen":
        return "image_gen"

    # Duygusal → assistant (empati modunda)
    if route == "emotional":
        return "assistant"

    return "assistant"


def should_ask_clarification(decision: Dict, prompt: str) -> Optional[str]:
    """Belirsiz mesajlarda ne sorulacak?"""
    if not decision.get("ambiguous"):
        return None

    clarification = decision.get("clarification_needed")
    if clarification:
        return clarification

    intent = decision.get("intent", "")
    if "code_generate" in intent:
        return "Hangi programlama dili veya framework kullanmamı istersin?"
    if "how_to" in intent:
        return "Hangi konuda yardımcı olayım, biraz daha açar mısın?"
    if "web_search" in intent:
        return "Hangi konuyu araştırayım?"
    if "image_generate" in intent:
        return "Nasıl bir görsel istiyorsun? Biraz tarif eder misin?"

    return None


# ─────────────────────────────────────────────────────────────
# CACHE TEMİZLİĞİ — Restart'ta bağlantıyı kapat
# ─────────────────────────────────────────────────────────────

async def close_router_cache():
    global _redis_pool
    if _redis_pool:
        try:
            await _redis_pool.aclose()
        except Exception:
            pass
        _redis_pool = None