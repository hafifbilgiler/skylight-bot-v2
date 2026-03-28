"""
═══════════════════════════════════════════════════════════════
SKYLIGHT — SMART ROUTER  v1.0
LLM tabanlı mesaj ön-işlemci — Claude/Gemini mantığı
═══════════════════════════════════════════════════════════════

Çalışma akışı:
  1. Kullanıcı mesajı gelir
  2. Llama-3.1-8B (~50ms) mesajı analiz eder → JSON karar
  3. Karar gateway'e yön verir

Keyword'den farkı:
  "Python'da redis nasıl kurulur"
    keyword → "kur" = currency ❌
    LLM     → how_to_technical, needs_realtime=false ✅

  "dolar kuru ne kadar"
    keyword → currency ✅
    LLM     → live_data, tool=currency ✅

Kararlar:
  route        : chat | code | live_data | search | image
  intent       : general_chat | how_to | code_generate | code_debug |
                 code_modify | code_explain | live_currency | live_weather |
                 live_crypto | live_news | web_search | image_gen | image_analyze
  needs_realtime: true/false
  tool         : currency | weather | crypto | news | web | none
  model        : llama4 | qwen3 | none (gateway seçer)
  confidence   : high | medium | low
═══════════════════════════════════════════════════════════════
"""

import json
import os
import asyncio
from typing import Optional, List, Dict

import httpx

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")

# Classifier için küçük ve hızlı model
ROUTER_MODEL = os.getenv(
    "DEEPINFRA_ROUTER_MODEL",
    "meta-llama/Meta-Llama-3.1-8B-Instruct"
)

# Timeout — router hızlı olmalı, uzun sürerse keyword fallback'e düş
ROUTER_TIMEOUT = float(os.getenv("ROUTER_TIMEOUT", "3.0"))

# ─────────────────────────────────────────────────────────────
# ROUTER PROMPT — JSON döndürmeye zorluyoruz
# ─────────────────────────────────────────────────────────────

_ROUTER_SYSTEM = """Sen bir mesaj sınıflandırıcısısın. Kullanıcı mesajını analiz et ve SADECE JSON döndür.

Karar alanları:
- route: "chat" | "code" | "live_data" | "search" | "image_gen" | "image_analyze"
- intent: "general_chat" | "how_to" | "code_generate" | "code_debug" | "code_modify" | "code_explain" | "code_review" | "live_currency" | "live_weather" | "live_crypto" | "live_news" | "live_time" | "web_search" | "image_gen" | "image_analyze"
- needs_realtime: true veya false
- tool: "currency" | "weather" | "crypto" | "news" | "time" | "web" | "none"
- model: "qwen3" (karmaşık kod için) | "llama4" (genel) | "auto"
- confidence: "high" | "medium" | "low"
- language: "tr" | "en" | "other"

Kurallar:
- "nasıl yapılır/kurulur/çalışır" → route=chat, intent=how_to, ASLA code_generate değil
- "yaz/implement et/kodla" → route=code, intent=code_generate
- "dolar/euro/kur/exchange" → route=live_data, tool=currency
- "hava/sıcaklık/yağmur" → route=live_data, tool=weather
- "bitcoin/ethereum/kripto" → route=live_data, tool=crypto
- "haber/gündem/son dakika" → route=live_data, tool=news
- "resim çiz/görsel oluştur/image generate" → route=image_gen
- kod içeren görsel paylaşıldıysa → route=image_analyze
- güncel/2024/2025/bugün itibariyle → needs_realtime=true
- Türkçe mesaj → language=tr

Sadece JSON döndür, başka hiçbir şey yazma."""


def _build_router_prompt(prompt: str, history: List[Dict]) -> str:
    """Router için kısa ve net prompt oluştur."""
    # Son 2 mesajı bağlam olarak ekle (daha fazlası gereksiz)
    context = ""
    if history:
        last = history[-2:] if len(history) >= 2 else history
        context = "\n".join([
            f"{m['role'].upper()}: {m['content'][:100]}"
            for m in last
        ])
        context = f"\n\nÖnceki mesajlar:\n{context}"

    return f"Mesaj: {prompt[:300]}{context}"


# ─────────────────────────────────────────────────────────────
# LLM ROUTER
# ─────────────────────────────────────────────────────────────

async def llm_route(prompt: str, history: List[Dict]) -> Optional[Dict]:
    """
    Llama-3.1-8B ile mesajı sınıflandır.
    Hata veya timeout durumunda None döner → keyword fallback devreye girer.
    """
    if not DEEPINFRA_API_KEY:
        return None

    try:
        payload = {
            "model": ROUTER_MODEL,
            "messages": [
                {"role": "system", "content": _ROUTER_SYSTEM},
                {"role": "user",   "content": _build_router_prompt(prompt, history)},
            ],
            "max_tokens": 150,       # JSON kısa, 150 yeterli
            "temperature": 0.0,      # Deterministik — her seferinde aynı karar
            "top_p": 1.0,
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT) as client:
            response = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                },
                json=payload,
            )

            if response.status_code != 200:
                print(f"[ROUTER] HTTP {response.status_code} — fallback'e geçiliyor")
                return None

            data    = response.json()
            content = data["choices"][0]["message"]["content"].strip()

            # JSON parse
            # Bazen ```json ... ``` içinde gelir, temizle
            if "```" in content:
                start = content.find("{")
                end   = content.rfind("}") + 1
                content = content[start:end]

            result = json.loads(content)

            # Gerekli alanlar var mı?
            if "route" not in result or "intent" not in result:
                print(f"[ROUTER] Eksik alan — fallback: {result}")
                return None

            print(f"[ROUTER] ✅ {result.get('intent')} → {result.get('route')} "
                  f"(model={result.get('model','auto')}, "
                  f"realtime={result.get('needs_realtime',False)})")
            return result

    except asyncio.TimeoutError:
        print(f"[ROUTER] Timeout ({ROUTER_TIMEOUT}s) — keyword fallback")
        return None
    except json.JSONDecodeError as e:
        print(f"[ROUTER] JSON parse hatası: {e} — fallback")
        return None
    except Exception as e:
        print(f"[ROUTER] Hata: {e} — fallback")
        return None


# ─────────────────────────────────────────────────────────────
# KEYWORD FALLBACK — LLM başarısız olursa
# ─────────────────────────────────────────────────────────────

_KEYWORD_LIVE = {
    # crypto ÖNCE — "bitcoin kaç dolar" currency değil crypto
    "crypto":   ("bitcoin", "btc", "ethereum", "eth", "kripto", "crypto",
                 "coin fiyat", "dogecoin", "solana", "bnb"),
    "currency": ("dolar kuru", "euro kuru", "döviz kuru", "döviz", "doviz",
                 "exchange rate", "sterlin", "gbp", "usd kaç", "eur kaç",
                 "kaç tl", "tl kaç", "dolar ne kadar", "euro ne kadar"),
    "weather":  ("hava durumu", "hava nasıl", "sıcaklık", "yağmur", "kar yağ",
                 "weather", "forecast", "derece", "hava kaç"),
    "news":     ("son haberler", "güncel haber", "son dakika", "breaking news",
                 "haberleri getir", "gündem ne"),
    "time":     ("saat kaç", "bugün ne günü", "tarih ne", "what time"),
}

_KEYWORD_CODE = (
    "yaz bana", "kod yaz", "fonksiyon yaz", "class yaz", "implement et",
    "kodla", "örnek kod yaz", "script yaz", "api yaz", "endpoint yaz",
    "test yaz", "migration yaz", "connection pool yaz",
    "ile implement", "ile kodla",
)

# Cümle sonunda "yaz" ile biten kod istekleri — "FastAPI ile JWT yaz"
def _ends_with_yaz(p: str) -> bool:
    return p.rstrip("?.! ").endswith("yaz") and len(p.split()) >= 3

_KEYWORD_HOW = (
    "nasıl yapılır", "nasıl yapabilirim", "nasıl yaparım",
    "nasıl kurulur", "nasıl çalışır", "nasıl entegre",
    "nasıl hesaplanır", "nasıl bağlanır", "how to", "how do i",
)

_KEYWORD_SEARCH = (
    "2025", "2024", "güncel", "bugün itibariyle", "son açıklanan",
    "resmi rakam", "asgari ücret", "enflasyon", "vergi oranı",
)


def keyword_fallback(prompt: str, history: List[Dict]) -> Dict:
    """
    LLM timeout/hata durumunda keyword tabanlı karar ver.
    Hızlı ama daha az akıllı.
    """
    p = prompt.lower()

    # Live data
    for tool, keywords in _KEYWORD_LIVE.items():
        if any(kw in p for kw in keywords):
            return {
                "route": "live_data",
                "intent": f"live_{tool}",
                "needs_realtime": True,
                "tool": tool,
                "model": "auto",
                "confidence": "medium",
                "language": "tr" if any(c in prompt for c in "çğışöüÇĞİŞÖÜ") else "en",
                "_source": "keyword_fallback",
            }

    # Açık kod isteği — "FastAPI ile JWT yaz" gibi cümle sonu "yaz" ile bitenleri de yakala
    if any(kw in p for kw in _KEYWORD_CODE) or _ends_with_yaz(p):
        return {
            "route": "code",
            "intent": "code_generate",
            "needs_realtime": False,
            "tool": "none",
            "model": "qwen3",
            "confidence": "high",
            "language": "tr" if any(c in prompt for c in "çğışöüÇĞİŞÖÜ") else "en",
            "_source": "keyword_fallback",
        }

    # Nasıl yapılır
    if any(kw in p for kw in _KEYWORD_HOW):
        return {
            "route": "chat",
            "intent": "how_to",
            "needs_realtime": False,
            "tool": "none",
            "model": "llama4",
            "confidence": "high",
            "language": "tr" if any(c in prompt for c in "çğışöüÇĞİŞÖÜ") else "en",
            "_source": "keyword_fallback",
        }

    # Güncel bilgi
    if any(kw in p for kw in _KEYWORD_SEARCH):
        return {
            "route": "search",
            "intent": "web_search",
            "needs_realtime": True,
            "tool": "web",
            "model": "llama4",
            "confidence": "medium",
            "language": "tr" if any(c in prompt for c in "çğışöüÇĞİŞÖÜ") else "en",
            "_source": "keyword_fallback",
        }

    # Default
    return {
        "route": "chat",
        "intent": "general_chat",
        "needs_realtime": False,
        "tool": "none",
        "model": "llama4",
        "confidence": "low",
        "language": "tr" if any(c in prompt for c in "çğışöüÇĞİŞÖÜ") else "en",
        "_source": "keyword_fallback",
    }


# ─────────────────────────────────────────────────────────────
# ANA FONKSİYON — Gateway buraya çağırır
# ─────────────────────────────────────────────────────────────

async def route_message(prompt: str, history: List[Dict] = None) -> Dict:
    """
    Mesajı sınıflandır.
    1. LLM ile dene (hızlı, akıllı)
    2. Başarısızsa keyword fallback

    Gateway kullanımı:
        decision = await route_message(request_body.prompt, history)
        mode     = decision["route"]   # chat / code / live_data / search
        intent   = decision["intent"]
        realtime = decision["needs_realtime"]
    """
    history = history or []

    # LLM router dene
    result = await llm_route(prompt, history)

    # Başarısız → keyword fallback
    if result is None:
        result = keyword_fallback(prompt, history)
        print(f"[ROUTER] Keyword fallback: {result['intent']}")

    return result


# ─────────────────────────────────────────────────────────────
# GATEWAY ENTEGRASYON YARDIMCISI
# ─────────────────────────────────────────────────────────────

def router_to_gateway_mode(decision: Dict) -> str:
    """
    Router kararını gateway mode'una çevir.
    Mevcut gateway MODE_CONFIGS ile uyumlu.
    """
    route  = decision.get("route", "chat")
    intent = decision.get("intent", "general_chat")
    model  = decision.get("model", "auto")

    if route == "code" or model == "qwen3":
        return "code"          # Qwen3-Coder 480B
    if route == "live_data":
        return "assistant"     # Llama-4 + live data inject
    if route == "search":
        return "assistant"     # Llama-4 + deep search
    if "it_" in intent or "server" in intent or "kubernetes" in intent:
        return "it_expert"

    return "assistant"         # Default: Llama-4