"""
═══════════════════════════════════════════════════════════════
SKYLIGHT SMART TOOLS SERVICE — v6.0 (Deep Search + LLM Synthesis)
═══════════════════════════════════════════════════════════════
v5.0 üzerine eklenenler:
  ✅ Tam async (httpx.AsyncClient) — çoklu kullanıcı desteği
  ✅ /deep_search endpoint — arama + sayfa içeriği + LLM sentezi
  ✅ fetch_page_content() — gerçek sayfa metnini çeker
  ✅ AsyncCache — asyncio.Lock ile thread-safe, concurrent-safe
  ✅ detect_current_events_query() — güncel sorgu tespiti
  ✅ DeepInfra LLM sentezi — ham sonuçları anlamlı yanıta dönüştürür
  ✅ /suggest endpoint — gateway için "web araması önerir misin?" desteği
  ✅ Concurrent waterfall: SearXNG → Brave → DuckDuckGo
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
from urllib.parse import quote_plus
import pytz
import requests                  # sync (mevcut fonksiyonlar için)
import httpx                     # async (yeni fonksiyonlar için)
from bs4 import BeautifulSoup
import re
import os
import time
import asyncio
import json
from enum import Enum

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Skylight Smart Tools",
    description="Real-time data + Deep Search + LLM Synthesis",
    version="6.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

SEARXNG_URL          = os.getenv("SEARXNG_URL", None)
BRAVE_API_KEY        = os.getenv("BRAVE_API_KEY", None)
DEEPINFRA_API_KEY    = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL   = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")

# Deep search için kullanılacak model — hafif + hızlı
SYNTHESIS_MODEL = os.getenv(
    "SYNTHESIS_MODEL",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
)

# Sayfa içeriği çekme sınırları
MAX_PAGE_CONTENT_CHARS = 2500    # sayfa başına max karakter
MAX_PAGES_TO_FETCH     = 3       # kaç sayfanın içeriği çekilsin
PAGE_FETCH_TIMEOUT     = 8       # saniye
DEEP_SEARCH_TIMEOUT    = 35      # toplam pipeline timeout

# ═══════════════════════════════════════════════════════════════
# CACHE — Sync ve Async versiyonlar
# ═══════════════════════════════════════════════════════════════

class SimpleCache:
    """Sync in-memory TTL cache (mevcut sync fonksiyonlar için)."""
    def __init__(self, ttl_seconds: int = 300):
        self._store: Dict[str, tuple] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[dict]:
        if key in self._store:
            value, exp = self._store[key]
            if time.time() < exp:
                return value
            del self._store[key]
        return None

    def set(self, key: str, value: dict):
        if len(self._store) > 200:
            now = time.time()
            expired = [k for k, (_, e) in self._store.items() if now >= e]
            for k in expired:
                del self._store[k]
            if len(self._store) > 200:
                for k in list(self._store.keys())[:50]:
                    del self._store[k]
        self._store[key] = (value, time.time() + self._ttl)


class AsyncCache:
    """
    Async thread-safe TTL cache (concurrent request'ler için).
    asyncio.Lock ile aynı anda birden fazla kullanıcı güvenle okuyabilir.
    """
    def __init__(self, ttl_seconds: int = 300):
        self._store: Dict[str, tuple] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[dict]:
        async with self._lock:
            if key in self._store:
                value, exp = self._store[key]
                if time.time() < exp:
                    return value
                del self._store[key]
        return None

    async def set(self, key: str, value: dict):
        async with self._lock:
            if len(self._store) > 500:
                now = time.time()
                expired = [k for k, (_, e) in self._store.items() if now >= e]
                for k in expired:
                    del self._store[k]
            self._store[key] = (value, time.time() + self._ttl)

    async def delete(self, key: str):
        async with self._lock:
            self._store.pop(key, None)


# Cache instance'ları
weather_cache    = SimpleCache(ttl_seconds=300)   # 5 dk
currency_cache   = SimpleCache(ttl_seconds=120)   # 2 dk
search_cache     = SimpleCache(ttl_seconds=180)   # 3 dk
deep_search_cache = AsyncCache(ttl_seconds=300)   # 5 dk (async)

# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

class ToolType(str, Enum):
    TIME             = "time"
    WEATHER          = "weather"
    NEWS             = "news"
    CURRENCY         = "currency"
    CRYPTO           = "crypto"
    PRICE_SEARCH     = "price_search"
    DEFINITION       = "definition"
    GENERAL_KNOWLEDGE = "general_knowledge"
    WEB_SEARCH       = "web_search"
    DEEP_SEARCH      = "deep_search"


class UnifiedRequest(BaseModel):
    query: str
    tool_type: Optional[ToolType] = None


class WebSearchRequest(BaseModel):
    query: str
    num_results: int = 5


class DeepSearchRequest(BaseModel):
    query:           str
    num_results:     int  = 5      # kaç arama sonucu
    fetch_pages:     int  = 3      # kaç sayfanın içeriği çekilsin
    synthesize:      bool = True   # LLM ile sentezle mi?
    language:        str  = "tr"   # yanıt dili
    context_hint:    Optional[str] = None  # önceki konuşmadan bağlam ipucu


class SuggestRequest(BaseModel):
    query:           str
    conversation_id: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# INTELLIGENT QUERY PARSING
# ═══════════════════════════════════════════════════════════════

def extract_location_from_query(query: str) -> Optional[str]:
    """Dinamik şehir çıkarma — Türkçe sonek temizleme dahil."""
    noise_words = {
        'hava', 'havadurumu', 'havalar', 'weather', 'sıcaklık', 'temperature',
        'bugün', 'nasıl', 'nedir', 'kaç', 'derece', 'today', 'how', 'what',
        'için', 'de', 'da', 'ta', 'te', 'şuan', 'şimdi', 'ne', 'nerede',
        'gibi', 'olan', 'var', 'yok', 'mi', 'mı', 'mu', 'mü',
        'in', 'at', 'the', 'is', 'current', 'now', 'forecast',
        'durumu', 'nasil', 'sicaklik',
    }
    words = re.findall(r'\b[a-zA-ZçğıöşüÇĞİÖŞÜ]+\b', query)
    clean_words = [w for w in words if w.lower() not in noise_words and len(w) > 2]
    if not clean_words:
        return None
    first_word = clean_words[0]
    first_word_lower = first_word.lower()
    for suffix in ['daki', 'deki', 'dan', 'den', 'da', 'de', 'ta', 'te',
                   'nın', 'nin', 'nun', 'nün', 'ın', 'in', 'un', 'ün']:
        if first_word_lower.endswith(suffix) and len(first_word_lower) > len(suffix) + 2:
            first_word = first_word[:-len(suffix)]
            break
    if len(clean_words) > 1:
        two_word = f"{clean_words[0]} {clean_words[1]}".lower()
        known_multi = {
            "new york", "los angeles", "san francisco", "las vegas",
            "hong kong", "kuala lumpur", "buenos aires", "sao paulo",
            "rio janeiro", "cape town", "tel aviv", "abu dhabi",
        }
        if two_word in known_multi:
            return two_word.title()
    return first_word.title()


def is_in_scope(query: str) -> bool:
    """
    Kapsam kontrolü — zararlı içerik ve argo hariç HERŞEYİ kabul eder.
    Yemek, seyahat, tarih, spor, bilim, sağlık, eğlence... hepsi scope içinde.
    """
    query_lower = query.lower()

    # ── Zararlı içerik dışlaması ────────────────────────────────
    # Silah yapımı, uyuşturucu, illegal aktiviteler
    harmful_keywords = (
        "bomba yap", "patlayıcı yap", "silah yap", "uyuşturucu yap",
        "bomb make", "drug make", "weapon make", "exploit",
        "hack into", "ddos", "malware yaz", "ransomware",
        "çocuk istismar", "child abuse", "cp ",
        "nasıl öldürülür", "how to kill",
    )
    if any(kw in query_lower for kw in harmful_keywords):
        print(f"[SCOPE] Blocked harmful query: '{query[:50]}'")
        return False

    # ── Geri kalan HERŞEYİ kabul et ────────────────────────────
    return True


def detect_current_events_query(query: str) -> bool:
    """
    Güncel bilgi gerektiren sorgu mu?
    Bu sorgular için deep search öncelikli olarak tetiklenir.

    Örnekler:
    - "son yapay zeka modelleri" → True
    - "arap ülkeleri haberleri" → True
    - "tesla hisse fiyatı" → True (utility ile de yakalanır)
    - "python nedir" → False (sabit bilgi)
    """
    q = query.lower()

    # Güncel bilgi sinyalleri
    recency_signals = [
        "son", "güncel", "yeni", "bugün", "bu hafta", "bu ay", "bu yıl",
        "2024", "2025", "şimdiki", "en son", "son dakika", "breaking",
        "latest", "recent", "current", "new", "today", "this week",
        "haberleri", "gündem", "gelişme", "haber", "duyuru", "açıklama",
        "news", "update", "announcement",
    ]

    # Genel konu sinyalleri (bunlar + recency = kesinlikle güncel)
    broad_topics = [
        "yapay zeka", "ai", "teknoloji", "kripto", "bitcoin",
        "ekonomi", "borsa", "hisse", "stock", "market",
        "siyaset", "politika", "seçim", "election",
        "sağlık", "covid", "pandemi",
        "uzay", "nasa", "spacex",
        "araç", "araba", "otomobil", "car",
    ]

    has_recency  = any(s in q for s in recency_signals)
    has_topic    = any(t in q for t in broad_topics)

    return has_recency or (has_topic and len(q.split()) >= 3)


def auto_detect_tool(query: str) -> ToolType:
    """Akıllı tool tespiti — SIRALAMA KRİTİK."""
    q = query.lower()

    # ── 1. CURRENCY ──
    currency_kw = ["dolar", "euro", "kur", "döviz", "doviz", "sterlin",
                   "exchange", "pound", "gbp", "usd", "eur", "try"]
    if any(kw in q for kw in currency_kw):
        print(f"[TOOL DETECT] '{query}' → CURRENCY")
        return ToolType.CURRENCY

    # ── 2. CRYPTO ──
    crypto_kw = ["bitcoin", "ethereum", "kripto", "crypto", "btc", "eth",
                 "dogecoin", "doge", "solana", "sol coin"]
    if any(kw in q for kw in crypto_kw):
        print(f"[TOOL DETECT] '{query}' → CRYPTO")
        return ToolType.CRYPTO

    # ── 3. WEATHER ──
    weather_kw = ["hava", "havadurumu", "havalar", "weather", "sıcaklık",
                  "sicaklik", "derece", "yağmur", "yagmur", "kar", "rüzgar"]
    if any(kw in q for kw in weather_kw):
        print(f"[TOOL DETECT] '{query}' → WEATHER")
        return ToolType.WEATHER

    # ── 4. PRICE ──
    price_kw = ["fiyat", "price", "kaç lira", "kac lira", "ne kadar",
                "cost", "kilosu", "kg fiyat", "ucuz", "pahalı"]
    if any(kw in q for kw in price_kw):
        print(f"[TOOL DETECT] '{query}' → PRICE_SEARCH")
        return ToolType.PRICE_SEARCH

    # ── 5. TIME ──
    time_patterns = ["saat kaç", "saat kac", "kaçta", "kacta", "saat ",
                     "saati", " time", "tarih", "şimdi saat", "bugün günlerden", "what time"]
    is_time = any(kw in q for kw in time_patterns) or q.strip() in ["saat", "date"]
    if not is_time and "kaç" in q:
        other_kw = currency_kw + crypto_kw + weather_kw + ["haber", "news", "fiyat", "nedir"]
        if not any(kw in q for kw in other_kw):
            is_time = True
    if is_time and "hava" not in q:
        print(f"[TOOL DETECT] '{query}' → TIME")
        return ToolType.TIME

    # ── 6. DEFINITION ──
    if any(kw in q for kw in ["nedir", "what is", "ne demek", "kimdir", "who is", "definition"]):
        print(f"[TOOL DETECT] '{query}' → DEFINITION")
        return ToolType.DEFINITION

    # ── 7. NEWS ──
    if any(kw in q for kw in ["haber", "news", "gündem", "gundem", "son dakika"]):
        print(f"[TOOL DETECT] '{query}' → NEWS")
        return ToolType.NEWS

    # ── 8. GENERAL KNOWLEDGE ──
    if any(kw in q for kw in ["nerede", "where", "ne yapılır", "what to do",
                               "gezilecek", "görülecek", "ziyaret", "önerir misin", "recommend"]):
        print(f"[TOOL DETECT] '{query}' → GENERAL_KNOWLEDGE")
        return ToolType.GENERAL_KNOWLEDGE

    # ── 9. DEEP SEARCH — güncel konu tespiti ──
    if detect_current_events_query(query):
        print(f"[TOOL DETECT] '{query}' → DEEP_SEARCH (current events)")
        return ToolType.DEEP_SEARCH

    print(f"[TOOL DETECT] '{query}' → WEB_SEARCH (fallback)")
    return ToolType.WEB_SEARCH


# ═══════════════════════════════════════════════════════════════
# WMO WEATHER CODES
# ═══════════════════════════════════════════════════════════════

WEATHER_CODES = {
    0: "Açık", 1: "Genellikle açık", 2: "Parçalı bulutlu", 3: "Kapalı",
    45: "Sisli", 48: "Kırağılı sis",
    51: "Hafif çisenti", 53: "Orta çisenti", 55: "Yoğun çisenti",
    56: "Dondurucu hafif çisenti", 57: "Dondurucu yoğun çisenti",
    61: "Hafif yağmur", 63: "Orta yağmur", 65: "Şiddetli yağmur",
    66: "Dondurucu hafif yağmur", 67: "Dondurucu şiddetli yağmur",
    71: "Hafif kar yağışı", 73: "Orta kar yağışı", 75: "Şiddetli kar yağışı",
    77: "Kar taneleri",
    80: "Hafif sağanak", 81: "Orta sağanak", 82: "Şiddetli sağanak",
    85: "Hafif kar sağanağı", 86: "Şiddetli kar sağanağı",
    95: "Gök gürültülü fırtına",
    96: "Dolu ile gök gürültülü fırtına",
    99: "Şiddetli dolu ile fırtına",
}

# ═══════════════════════════════════════════════════════════════
# SYNC TOOL FUNCTIONS (v5.0'dan korundu)
# ═══════════════════════════════════════════════════════════════

def get_current_time(timezone: str = "Europe/Istanbul") -> Dict:
    try:
        tz  = pytz.timezone(timezone)
        now = datetime.now(tz)
        day_names_tr = {
            "Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba",
            "Thursday": "Perşembe", "Friday": "Cuma",
            "Saturday": "Cumartesi", "Sunday": "Pazar",
        }
        month_names_tr = {
            "January": "Ocak", "February": "Şubat", "March": "Mart",
            "April": "Nisan", "May": "Mayıs", "June": "Haziran",
            "July": "Temmuz", "August": "Ağustos", "September": "Eylül",
            "October": "Ekim", "November": "Kasım", "December": "Aralık",
        }
        day_tr   = day_names_tr.get(now.strftime("%A"), now.strftime("%A"))
        month_tr = month_names_tr.get(now.strftime("%B"), now.strftime("%B"))
        return {
            "success": True,
            "data": {
                "time": now.strftime("%H:%M:%S"),
                "date": now.strftime("%Y-%m-%d"),
                "day_tr": day_tr, "month_tr": month_tr,
                "formatted_tr": f"{now.day} {month_tr} {now.year}, {day_tr}, saat {now.strftime('%H:%M')}",
                "short": f"Saat {now.strftime('%H:%M')}, {day_tr}",
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_weather_dynamic(query: str) -> Dict:
    city = extract_location_from_query(query)
    if not city:
        return {"success": False, "error": "city_not_specified",
                "message": "Hangi şehir için hava durumu öğrenmek istersiniz?"}
    cache_key = f"weather_{city.lower()}"
    cached    = weather_cache.get(cache_key)
    if cached:
        print(f"[WEATHER] Cache hit: {city}")
        return cached
    try:
        geo = requests.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(city)}&count=1",
            timeout=5).json()
        if not geo.get("results"):
            return {"success": False, "error": f"Şehir bulunamadı: {city}"}
        loc = geo["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        city_name = loc.get("name", city)
        country   = loc.get("country", "")
        w = requests.get(
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,wind_speed_10m", timeout=5).json()
        c    = w["current"]
        desc = WEATHER_CODES.get(c.get("weather_code", 0), "Bilinmiyor")
        wind = c.get("wind_speed_10m", "")
        print(f"[WEATHER] {city_name}, {country} = {c['temperature_2m']}°C, {desc}")
        result = {
            "success": True, "tool_used": "weather",
            "data": {
                "city": city_name, "country": country,
                "temperature": round(c["temperature_2m"], 1),
                "feels_like": round(c["apparent_temperature"], 1),
                "humidity": c["relative_humidity_2m"],
                "description": desc,
                "wind_speed": round(wind, 1) if wind else "",
                "formatted": f"{city_name}, {country}: {round(c['temperature_2m'])}°C, {desc}",
            },
        }
        weather_cache.set(cache_key, result)
        return result
    except Exception as e:
        print(f"[WEATHER] Error: {e}")
        return {"success": False, "error": f"Hava durumu alınamadı: {str(e)}"}


def get_currency_rate(from_currency: str = "USD", to_currency: str = "TRY") -> Dict:
    cache_key = f"currency_{from_currency}_{to_currency}"
    cached    = currency_cache.get(cache_key)
    if cached:
        return cached
    try:
        resp = requests.get(
            f"https://api.exchangerate-api.com/v4/latest/{from_currency}", timeout=5)
        resp.raise_for_status()
        rate = resp.json()["rates"].get(to_currency)
        if rate:
            result = {
                "success": True,
                "data": {"from": from_currency, "to": to_currency, "rate": rate,
                         "formatted": f"1 {from_currency} = {rate:.2f} {to_currency}"},
            }
            currency_cache.set(cache_key, result)
            return result
    except Exception as e:
        print(f"[CURRENCY] API failed, trying Google Finance: {e}")
    try:
        resp = requests.get(
            f"https://www.google.com/finance/quote/{from_currency}-{to_currency}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        soup     = BeautifulSoup(resp.text, 'html.parser')
        rate_elem = soup.find("div", {"class": "YMlKec fxKbKc"})
        if rate_elem:
            rate   = float(rate_elem.text.replace(',', '.'))
            result = {
                "success": True,
                "data": {"from": from_currency, "to": to_currency,
                         "rate": round(rate, 4),
                         "formatted": f"1 {from_currency} = {round(rate, 2)} {to_currency}"},
            }
            currency_cache.set(cache_key, result)
            return result
    except Exception:
        pass
    return {"success": False, "error": "Kur bilgisi alınamadı"}


def extract_currencies(query: str) -> tuple:
    q        = query.lower()
    curr_map = {
        "dolar": "USD", "usd": "USD", "euro": "EUR", "eur": "EUR",
        "pound": "GBP", "sterlin": "GBP", "gbp": "GBP",
        "tl": "TRY", "try": "TRY", "lira": "TRY",
        "yen": "JPY", "frank": "CHF",
    }
    found = [v for k, v in curr_map.items() if k in q]
    return (found[0] if found else "USD", found[1] if len(found) > 1 else "TRY")


def get_news(query: Optional[str] = None) -> Dict:
    try:
        if query and len(query) > 2:
            url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=tr&gl=TR"
        else:
            url = "https://news.google.com/rss?hl=tr&gl=TR"
        resp  = requests.get(url, timeout=5)
        soup  = BeautifulSoup(resp.text, 'xml')
        items = soup.find_all('item', limit=5)
        articles = [
            {"title": i.title.text if i.title else "Başlık yok",
             "link": i.link.text if i.link else "",
             "published": i.pubDate.text if i.pubDate else ""}
            for i in items
        ]
        if articles:
            return {"success": True, "data": {"articles": articles, "total": len(articles)}}
        return {"success": False, "error": "Haber bulunamadı"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_crypto_price(coin: str = "bitcoin") -> Dict:
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?"
            f"ids={coin}&vs_currencies=usd&include_24hr_change=true",
            timeout=5).json()
        if coin not in resp:
            return {"success": False, "error": f"Coin bulunamadı: {coin}"}
        price  = resp[coin]["usd"]
        change = resp[coin].get("usd_24h_change", 0)
        return {
            "success": True,
            "data": {"coin": coin, "price": price, "change_24h": round(change, 2),
                     "formatted": f"{coin.title()}: ${price:,.2f}"},
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# SYNC WEB SEARCH (v5.0'dan korundu — /unified için)
# ═══════════════════════════════════════════════════════════════

def web_search_searxng(query: str, num: int = 5) -> Dict:
    if not SEARXNG_URL:
        return {"success": False}
    try:
        data = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json",
                    "engines": "google,bing,duckduckgo", "language": "tr"},
            timeout=10).json()
        results = data.get("results", [])[:num]
        if results:
            print(f"[WEB SEARCH] SearXNG: {len(results)} results")
            return {
                "success": True,
                "data": {"query": query, "provider": "searxng", "results": [
                    {"title": r.get("title", ""), "url": r.get("url", ""),
                     "content": r.get("content", ""), "engine": r.get("engine", "")}
                    for r in results
                ]},
            }
    except Exception as e:
        print(f"[SEARCH] SearXNG error: {e}")
    return {"success": False}


def web_search_brave(query: str, num: int = 5) -> Dict:
    if not BRAVE_API_KEY:
        return {"success": False}
    try:
        resp    = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": num, "search_lang": "tr"},
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            timeout=10).json()
        results = resp.get("web", {}).get("results", [])[:num]
        if results:
            return {
                "success": True,
                "data": {"query": query, "provider": "brave", "results": [
                    {"title": r.get("title", ""), "url": r.get("url", ""),
                     "content": r.get("description", ""), "engine": "brave"}
                    for r in results
                ]},
            }
    except Exception as e:
        print(f"[SEARCH] Brave error: {e}")
    return {"success": False}


def web_search_duckduckgo(query: str, num: int = 5) -> Dict:
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    ]
    for attempt, ua in enumerate(user_agents):
        try:
            resp    = requests.get(
                f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                headers={"User-Agent": ua}, timeout=10)
            soup    = BeautifulSoup(resp.text, 'html.parser')
            results = []
            for r in soup.find_all('div', class_='result', limit=num):
                title_elem   = r.find('a', class_='result__a')
                snippet_elem = r.find('a', class_='result__snippet')
                if title_elem:
                    results.append({
                        "title": title_elem.get_text(strip=True),
                        "url":   title_elem.get('href', ''),
                        "content": snippet_elem.get_text(strip=True) if snippet_elem else "",
                        "engine": "duckduckgo",
                    })
            if results:
                return {"success": True,
                        "data": {"query": query, "results": results, "provider": "duckduckgo"}}
        except Exception as e:
            if attempt < len(user_agents) - 1:
                time.sleep(0.5)
                continue
            print(f"[SEARCH] DuckDuckGo error: {e}")
    return {"success": False, "error": "Sonuç bulunamadı"}


def web_search(query: str, num_results: int = 5) -> Dict:
    """Sync waterfall: SearXNG → Brave → DuckDuckGo + 3dk cache."""
    cache_key = f"search_{query.lower().strip()}"
    cached    = search_cache.get(cache_key)
    if cached:
        return cached
    for fn in [
        lambda: web_search_searxng(query, num_results) if SEARXNG_URL else {"success": False},
        lambda: web_search_brave(query, num_results) if BRAVE_API_KEY else {"success": False},
        lambda: web_search_duckduckgo(query, num_results),
    ]:
        result = fn()
        if result.get("success"):
            search_cache.set(cache_key, result)
            return result
    return {"success": False, "error": "Tüm arama sağlayıcıları başarısız"}


# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ASYNC DEEP SEARCH — v6.0 YENİ KALP ATIŞI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

async def async_web_search(query: str, num_results: int = 5) -> Dict:
    """
    Async waterfall web arama.
    Çoklu kullanıcı için non-blocking — hiçbir user diğerini bloklamaz.
    SearXNG → Brave → DuckDuckGo
    """
    cache_key = f"async_search_{query.lower().strip()}"
    cached    = await deep_search_cache.get(cache_key)
    if cached:
        print(f"[ASYNC SEARCH] Cache hit: '{query}'")
        return cached

    async with httpx.AsyncClient(timeout=12.0) as client:

        # 1. SearXNG
        if SEARXNG_URL:
            try:
                resp = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={"q": query, "format": "json",
                            "engines": "google,bing,duckduckgo", "language": "tr"},
                )
                data    = resp.json()
                results = data.get("results", [])[:num_results]
                if results:
                    result = {
                        "success": True,
                        "data": {"query": query, "provider": "searxng", "results": [
                            {"title": r.get("title", ""), "url": r.get("url", ""),
                             "content": r.get("content", "")[:500]}
                            for r in results
                        ]},
                    }
                    await deep_search_cache.set(cache_key, result)
                    print(f"[ASYNC SEARCH] SearXNG: {len(results)} results")
                    return result
            except Exception as e:
                print(f"[ASYNC SEARCH] SearXNG failed: {e}")

        # 2. Brave
        if BRAVE_API_KEY:
            try:
                resp    = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": num_results, "search_lang": "tr"},
                    headers={"Accept": "application/json",
                             "X-Subscription-Token": BRAVE_API_KEY},
                )
                results = resp.json().get("web", {}).get("results", [])[:num_results]
                if results:
                    result = {
                        "success": True,
                        "data": {"query": query, "provider": "brave", "results": [
                            {"title": r.get("title", ""), "url": r.get("url", ""),
                             "content": r.get("description", "")[:500]}
                            for r in results
                        ]},
                    }
                    await deep_search_cache.set(cache_key, result)
                    print(f"[ASYNC SEARCH] Brave: {len(results)} results")
                    return result
            except Exception as e:
                print(f"[ASYNC SEARCH] Brave failed: {e}")

        # 3. DuckDuckGo async
        ua_list = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        ]
        for ua in ua_list:
            try:
                resp    = await client.get(
                    f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                    headers={"User-Agent": ua},
                )
                soup    = BeautifulSoup(resp.text, 'html.parser')
                results = []
                for r in soup.find_all('div', class_='result', limit=num_results):
                    title_elem   = r.find('a', class_='result__a')
                    snippet_elem = r.find('a', class_='result__snippet')
                    if title_elem:
                        results.append({
                            "title":   title_elem.get_text(strip=True),
                            "url":     title_elem.get('href', ''),
                            "content": snippet_elem.get_text(strip=True)[:500] if snippet_elem else "",
                        })
                if results:
                    result = {
                        "success": True,
                        "data": {"query": query, "provider": "duckduckgo", "results": results},
                    }
                    await deep_search_cache.set(cache_key, result)
                    print(f"[ASYNC SEARCH] DuckDuckGo: {len(results)} results")
                    return result
                break
            except Exception as e:
                print(f"[ASYNC SEARCH] DuckDuckGo failed: {e}")

    return {"success": False, "error": "Tüm arama sağlayıcıları başarısız oldu"}


async def fetch_page_content(url: str) -> Optional[str]:
    """
    URL'den sayfa içeriğini çeker, temizler ve döndürür.
    Concurrent — asyncio ile aynı anda birden fazla sayfa çekilebilir.

    Güvenlik: sadece http/https URL'leri kabul eder.
    Hatalı/yavaş sayfaları sessizce atlar.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    # Sosyal medya ve video platformları genellikle scrape edilemez
    skip_domains = ("youtube.com", "youtu.be", "twitter.com", "x.com",
                    "instagram.com", "facebook.com", "tiktok.com",
                    "reddit.com", "linkedin.com")
    if any(d in url for d in skip_domains):
        return None
    try:
        async with httpx.AsyncClient(timeout=PAGE_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
                },
            )
            if resp.status_code != 200:
                return None
            # Sadece metin içerikli sayfalar
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            # Script, style, nav, footer kaldır
            for tag in soup(["script", "style", "nav", "footer", "header",
                              "aside", "form", "button", "iframe", "noscript"]):
                tag.decompose()
            # Anlamlı içerik blokları — article, main, p, h1-h3
            content_blocks = []
            for tag in soup.find_all(["article", "main", "section", "p", "h1", "h2", "h3"]):
                text = tag.get_text(separator=" ", strip=True)
                if len(text) > 50:   # çok kısa metinleri atla
                    content_blocks.append(text)
            if not content_blocks:
                # Fallback: tüm metin
                text = soup.get_text(separator="\n", strip=True)
                content_blocks = [line for line in text.split("\n") if len(line.strip()) > 50]
            full_text = " ".join(content_blocks)
            # Tekrar eden boşlukları temizle
            full_text = re.sub(r'\s+', ' ', full_text).strip()
            # Karakter sınırı
            return full_text[:MAX_PAGE_CONTENT_CHARS] if full_text else None
    except Exception as e:
        print(f"[FETCH PAGE] Error for {url}: {e}")
        return None


async def synthesize_with_llm(
    query: str,
    search_results: List[dict],
    page_contents: List[dict],
    language: str = "tr",
    context_hint: Optional[str] = None,
) -> str:
    """
    Ham arama sonuçlarını ve sayfa içeriklerini LLM ile sentezler.
    DeepInfra API kullanır.

    page_contents: [{"url": ..., "title": ..., "content": ...}]
    """
    if not DEEPINFRA_API_KEY:
        # API key yoksa ham sonuçları döndür
        summaries = []
        for r in search_results[:5]:
            summaries.append(f"• {r.get('title', '')}: {r.get('content', '')[:200]}")
        return "\n".join(summaries)

    # Arama sonuçlarını birleştir
    search_summary = "\n".join([
        f"[{i+1}] {r.get('title', 'Başlıksız')}\n    {r.get('content', '')[:300]}"
        for i, r in enumerate(search_results[:5])
    ])

    # Sayfa içeriklerini birleştir
    page_summary = ""
    if page_contents:
        page_parts = []
        for pc in page_contents:
            if pc.get("content"):
                page_parts.append(
                    f"--- Kaynak: {pc.get('title', pc.get('url', 'Bilinmiyor'))} ---\n"
                    f"{pc['content'][:MAX_PAGE_CONTENT_CHARS]}"
                )
        page_summary = "\n\n".join(page_parts)

    lang_instruction = (
        "YANITI TÜRKÇE yaz." if language == "tr"
        else "Respond in ENGLISH."
    )

    context_section = ""
    if context_hint:
        context_section = f"\n\nKonuşma bağlamı: {context_hint}"

    synthesis_prompt = f"""Kullanıcı sorusu: "{query}"
{context_section}

WEB ARAMA SONUÇLARI (özet):
{search_summary}

SAYFA İÇERİKLERİ (detaylı):
{page_summary if page_summary else "(Sayfa içeriği çekilemedi — sadece özet kullanılıyor)"}

Yukarıdaki gerçek web verilerini kullanarak şu kurallarla yanıt ver:

1. {lang_instruction}
2. Doğrudan ve özlü bir yanıt ver — gereksiz giriş cümlesi yok
3. Güncel ve spesifik bilgileri öne çıkar (tarih, sayı, isim varsa ekle)
4. Çelişkili bilgiler varsa en güncel/güvenilir kaynağı tercih et
5. Kaynakları doğal şekilde belirt: "X'e göre..." veya "(Kaynak: Y)" formatında
6. Yanıt sonunda 1-2 cümle ile "Daha fazlası için:" öner — URL listesi verme
7. Maksimum 400 kelime

ÖNEMLİ: Sadece gerçek web verilerini kullan, uydurma."""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                },
                json={
                    "model":       SYNTHESIS_MODEL,
                    "messages":    [
                        {"role": "system",
                         "content": "Sen web araması sonuçlarını analiz eden ve kullanıcıya net, doğru yanıtlar sunan bir asistansın."},
                        {"role": "user", "content": synthesis_prompt},
                    ],
                    "max_tokens":  800,
                    "temperature": 0.3,
                    "stream":      False,
                },
            )
            data    = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                print(f"[LLM SYNTHESIS] Success: {len(content)} chars")
                return content
            return "Sentez yapılamadı — ham sonuçlar kullanılıyor."
    except Exception as e:
        print(f"[LLM SYNTHESIS ERROR] {e}")
        # Fallback: ham sonuçları döndür
        return "\n".join([
            f"• **{r.get('title', '')}**: {r.get('content', '')[:200]}"
            for r in search_results[:4]
        ])


async def deep_search_pipeline(req: DeepSearchRequest) -> Dict:
    """
    Deep Search tam pipeline:
    1. Web araması yap (async)
    2. İlk N sayfanın içeriğini paralel çek (asyncio.gather)
    3. LLM ile sentezle
    4. Yapılandırılmış yanıt döndür

    Tüm adımlar non-blocking — aynı anda 50+ kullanıcı çalışabilir.
    """
    start_time = time.time()
    cache_key  = f"deep_{req.query.lower().strip()}_{req.num_results}_{req.fetch_pages}"

    # Cache kontrolü
    cached = await deep_search_cache.get(cache_key)
    if cached:
        print(f"[DEEP SEARCH] Cache hit: '{req.query}'")
        return cached

    print(f"\n{'━' * 60}")
    print(f"[DEEP SEARCH] Query: '{req.query}'")
    print(f"[DEEP SEARCH] Results: {req.num_results} | Pages: {req.fetch_pages} | Synthesize: {req.synthesize}")
    print(f"{'━' * 60}")

    # ── ADIM 1: Web araması ─────────────────────────────────────
    search_result = await async_web_search(req.query, req.num_results)
    if not search_result.get("success"):
        return {
            "success": False,
            "error":   "Web araması başarısız oldu",
            "query":   req.query,
            "tool_used": "deep_search",
        }

    results       = search_result.get("data", {}).get("results", [])
    provider_used = search_result.get("data", {}).get("provider", "unknown")
    print(f"[DEEP SEARCH] Step 1 done: {len(results)} results from {provider_used}")

    # ── ADIM 2: Sayfa içeriklerini paralel çek ──────────────────
    page_contents: List[dict] = []
    if results and req.fetch_pages > 0:
        urls_to_fetch = [
            r.get("url", "") for r in results[:req.fetch_pages]
            if r.get("url")
        ]
        # asyncio.gather ile paralel fetch — tüm sayfalar aynı anda
        fetch_tasks    = [fetch_page_content(url) for url in urls_to_fetch]
        fetched_pages  = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for i, (result_item, page_text) in enumerate(zip(results[:req.fetch_pages], fetched_pages)):
            if isinstance(page_text, str) and page_text:
                page_contents.append({
                    "url":     result_item.get("url", ""),
                    "title":   result_item.get("title", ""),
                    "content": page_text,
                })
                print(f"[DEEP SEARCH] Fetched page {i+1}: {len(page_text)} chars")
            else:
                print(f"[DEEP SEARCH] Page {i+1}: skipped or failed")

    print(f"[DEEP SEARCH] Step 2 done: {len(page_contents)} pages fetched")

    # ── ADIM 3: LLM Sentezi ─────────────────────────────────────
    synthesis = None
    if req.synthesize:
        synthesis = await synthesize_with_llm(
            query          = req.query,
            search_results = results,
            page_contents  = page_contents,
            language       = req.language,
            context_hint   = req.context_hint,
        )
        print(f"[DEEP SEARCH] Step 3 done: synthesis {len(synthesis or '')} chars")

    elapsed = round(time.time() - start_time, 2)
    print(f"[DEEP SEARCH] Total time: {elapsed}s")

    # ── Sonuç ────────────────────────────────────────────────────
    final_result = {
        "success":    True,
        "tool_used":  "deep_search",
        "query":      req.query,
        "provider":   provider_used,
        "data": {
            "synthesis":    synthesis,
            "search_results": [
                {"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("content", "")[:200]}
                for r in results[:req.num_results]
            ],
            "pages_fetched": len(page_contents),
            "total_results": len(results),
            "elapsed_seconds": elapsed,
        },
    }

    # Cache'e ekle (5 dk)
    await deep_search_cache.set(cache_key, final_result)
    return final_result


# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "service": "Skylight Smart Tools",
        "version": "6.0.0",
        "status":  "running",
        "features": {
            "weather":     "open-meteo.com (FREE, UNLIMITED) + 5min cache",
            "time":        "Local system (Europe/Istanbul)",
            "news":        "Google News RSS (FREE)",
            "currency":    "exchangerate-api + Google Finance (FREE) + 2min cache",
            "crypto":      "CoinGecko (FREE)",
            "web_search":  "SearXNG → Brave → DuckDuckGo (waterfall) + 3min cache",
            "deep_search": "Web araması + sayfa içeriği + LLM sentezi + 5min cache",
        },
        "search_providers": {
            "searxng":    bool(SEARXNG_URL),
            "brave":      bool(BRAVE_API_KEY),
            "duckduckgo": True,
        },
        "llm_synthesis": {
            "enabled": bool(DEEPINFRA_API_KEY),
            "model":   SYNTHESIS_MODEL if DEEPINFRA_API_KEY else "disabled",
        },
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "6.0.0"}


@app.post("/unified")
async def unified_endpoint(request: UnifiedRequest):
    """Ana unified endpoint — tüm sorguları yönetir."""
    query     = request.query
    tool_type = request.tool_type or auto_detect_tool(query)

    print(f"\n{'─' * 50}")
    print(f"[SMART TOOLS] Query: '{query}'")
    print(f"[SMART TOOLS] Detected Tool: {tool_type.value}")
    print(f"{'─' * 50}")

    # Zararlı içerik kontrolü
    if not is_in_scope(query):
        return {
            "success":   False,
            "error":     "Bu tür içeriklere yardımcı olamam.",
            "tool_used": "blocked",
            "query":     query,
        }

    try:
        if tool_type == ToolType.TIME:
            result = get_current_time()

        elif tool_type == ToolType.WEATHER:
            result = get_weather_dynamic(query)

        elif tool_type == ToolType.CURRENCY:
            from_c, to_c = extract_currencies(query)
            result = get_currency_rate(from_c, to_c)

        elif tool_type == ToolType.NEWS:
            topic  = query.replace("haber", "").replace("news", "").strip()
            result = get_news(topic if len(topic) > 2 else None)

        elif tool_type == ToolType.CRYPTO:
            coin = "bitcoin"
            q    = query.lower()
            if "ethereum" in q or "eth" in q: coin = "ethereum"
            elif "dogecoin" in q or "doge" in q: coin = "dogecoin"
            elif "solana" in q or "sol" in q: coin = "solana"
            result = get_crypto_price(coin)

        elif tool_type == ToolType.DEEP_SEARCH:
            # /unified'dan da deep search çağrılabilir
            deep_req = DeepSearchRequest(query=query, num_results=5, fetch_pages=3)
            result   = await deep_search_pipeline(deep_req)

        elif tool_type in (ToolType.PRICE_SEARCH, ToolType.DEFINITION,
                            ToolType.GENERAL_KNOWLEDGE, ToolType.WEB_SEARCH):
            result = web_search(query, 5)

        else:
            result = web_search(query, 5)

        result["tool_used"] = tool_type.value
        result["query"]     = query

        # Log
        success = result.get("success", False)
        data    = result.get("data", {})
        preview = (
            data.get("formatted") or
            data.get("short") or
            f"{len(data.get('results', data.get('articles', [])))} items"
        )
        print(f"[SMART TOOLS] Result: success={success} | {preview}")
        return result

    except Exception as e:
        print(f"[SMART TOOLS ERROR] {e}")
        return {"success": False, "error": str(e),
                "tool_used": tool_type.value, "query": query}


@app.post("/search")
async def search_endpoint(request: WebSearchRequest):
    """Direkt web arama endpoint'i (sync)."""
    result             = web_search(request.query, request.num_results)
    result["tool_used"] = "web_search"
    return result


@app.post("/deep_search")
async def deep_search_endpoint(request: DeepSearchRequest):
    """
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    DEEP SEARCH — Akıllı Web Araştırma
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Gateway bu endpoint'i çağırır. Tam pipeline:
    1. Web araması (SearXNG/Brave/DDG)
    2. İlk N sayfanın içeriğini paralel çek
    3. LLM ile sentezle (DeepInfra)
    4. Yapılandırılmış yanıt döndür

    Örnek request:
    {
        "query": "son yapay zeka gelişmeleri 2025",
        "num_results": 5,
        "fetch_pages": 3,
        "synthesize": true,
        "language": "tr",
        "context_hint": "kullanıcı AI teknolojileriyle ilgileniyor"
    }

    Örnek response:
    {
        "success": true,
        "data": {
            "synthesis": "OpenAI GPT-5... Meta Llama 4...",
            "search_results": [...],
            "pages_fetched": 3,
            "elapsed_seconds": 4.2
        }
    }
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    return await deep_search_pipeline(request)


@app.post("/suggest")
async def suggest_web_search(request: SuggestRequest):
    """
    Gateway bu endpoint'i çağırarak kullanıcıya web araması önermeli mi diye sorar.

    Döner:
    {
        "should_suggest": true/false,
        "reason": "current_events" / "factual_query" / ...,
        "suggested_query": "optimize edilmiş arama sorgusu",
        "message": "Bu konuyu webden araştırayım mı? 🔍"
    }
    """
    query = request.query
    q     = query.lower()

    # Güncel olaylar
    if detect_current_events_query(query):
        return {
            "should_suggest": True,
            "reason":         "current_events",
            "suggested_query": query,
            "message":        "Bu konu güncel bilgi gerektiriyor. Webden araştırayım mı? 🔍",
        }

    # "haber", "son dakika" gibi açık arama niyeti
    if any(kw in q for kw in ["haber", "news", "gündem", "son dakika", "araştır", "bul", "search"]):
        return {
            "should_suggest": True,
            "reason":         "explicit_search",
            "suggested_query": query.replace("haber", "").replace("news", "").strip() or query,
            "message":        "Webden arama yapayım mı? 🔍",
        }

    # Tanım soruları — ama popüler/güncel kişi/şey ise
    if any(kw in q for kw in ["nedir", "kimdir", "ne demek"]):
        # Kısa ve spesifik ise muhtemelen güncel bir şey
        if len(query.split()) <= 5:
            return {
                "should_suggest": True,
                "reason":         "definition_maybe_current",
                "suggested_query": query,
                "message":        "Web'de güncel bilgiye bakayım mı? 🔍",
            }

    return {
        "should_suggest": False,
        "reason":         "not_needed",
        "suggested_query": query,
        "message":        "",
    }


# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 70)
    print("SKYLIGHT SMART TOOLS SERVICE — v6.0 (Deep Search + LLM Synthesis)")
    print("=" * 70)
    print(f"Search: SearXNG({'ON' if SEARXNG_URL else 'OFF'}) | "
          f"Brave({'ON' if BRAVE_API_KEY else 'OFF'}) | DDG(ON)")
    print(f"LLM Synthesis: {'ON — ' + SYNTHESIS_MODEL if DEEPINFRA_API_KEY else 'OFF (no API key)'}")
    print("Scope: Evrensel — zararlı içerik hariç her konu desteklenir")
    print("Cache: Weather(5min) | Currency(2min) | Search(3min) | Deep(5min)")
    print("Async: Fully async — concurrent multi-user supported")
    print("=" * 70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8081, workers=4)