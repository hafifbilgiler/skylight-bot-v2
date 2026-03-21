"""
═══════════════════════════════════════════════════════════════
SKYLIGHT SMART TOOLS SERVICE — v6.1 (Jina Deep Search)
═══════════════════════════════════════════════════════════════
v6.0 → v6.1 değişiklikleri:
  ✅ fetch_page_content → Jina.ai Reader (ücretsiz, sınırsız, API key yok)
  ✅ fetch_page_content → Crawl4AI fallback (local pod, gelecek için hazır)
  ✅ fetch_page_content → Direkt scrape son çare
  ✅ is_in_scope → zararlı içerik hariç HERŞEYİ kabul eder
  ✅ Tam async — çoklu kullanıcı desteği
  ✅ AsyncCache — asyncio.Lock ile thread-safe

SAYFA ÇEKME SIRALAMA:
  1. Jina.ai   → r.jina.ai/URL (ücretsiz, temiz markdown, JS render)
  2. Crawl4AI  → local pod, 11235 port (henüz yok, eklenince devreye girer)
  3. Direkt    → httpx ile direkt scrape (son çare)

ARAMA SIRALAMA (waterfall):
  1. SearXNG   → kendi pod'unda, sınırsız
  2. Brave     → 2000/ay ücretsiz (API key varsa)
  3. DuckDuckGo → her zaman çalışır ama yavaş

DEEP SEARCH PIPELINE:
  SearXNG/DDG → URL listesi
      ↓
  Jina.ai → Her URL'den temiz markdown (paralel, asyncio.gather)
      ↓
  DeepInfra LLM → Sentez + yorumlama
      ↓
  Kullanıcıya kaynaklı, güncel, derin yanıt
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
from urllib.parse import quote_plus
import pytz
import requests             # sync fonksiyonlar için
import httpx                # async fonksiyonlar için
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
    description="Real-time data + Deep Search (Jina.ai + SearXNG + LLM)",
    version="6.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# Arama sağlayıcıları
SEARXNG_URL    = os.getenv("SEARXNG_URL", None)
BRAVE_API_KEY  = os.getenv("BRAVE_API_KEY", None)

# Crawl4AI — henüz yok, gelince bu env'i set et
CRAWL4AI_URL   = os.getenv("CRAWL4AI_URL", None)  # "http://skylight-crawl4ai:11235"

# Jina.ai — ücretsiz, API key gerekmez, direkt kullan
JINA_BASE_URL  = "https://r.jina.ai"

# LLM sentezi için DeepInfra
DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
SYNTHESIS_MODEL    = os.getenv(
    "SYNTHESIS_MODEL",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
)

# Sayfa çekme limitleri
MAX_PAGE_CHARS     = 3000   # sayfa başına max karakter
MAX_PAGES_TO_FETCH = 3      # kaç sayfa çekilsin
JINA_TIMEOUT       = 12     # saniye
CRAWL4AI_TIMEOUT   = 15     # saniye
DIRECT_TIMEOUT     = 8      # saniye
DEEP_SEARCH_TOTAL  = 40     # toplam pipeline timeout

# ═══════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════

class SimpleCache:
    """Sync TTL cache — mevcut sync fonksiyonlar için."""
    def __init__(self, ttl: int = 300):
        self._store: Dict[str, tuple] = {}
        self._ttl = ttl

    def get(self, key: str) -> Optional[dict]:
        if key in self._store:
            val, exp = self._store[key]
            if time.time() < exp:
                return val
            del self._store[key]
        return None

    def set(self, key: str, val: dict):
        if len(self._store) > 200:
            now = time.time()
            dead = [k for k, (_, e) in self._store.items() if now >= e]
            for k in dead:
                del self._store[k]
            if len(self._store) > 200:
                for k in list(self._store.keys())[:50]:
                    del self._store[k]
        self._store[key] = (val, time.time() + self._ttl)


class AsyncCache:
    """
    Async thread-safe TTL cache.
    asyncio.Lock — aynı anda 50+ kullanıcı güvenle okuyabilir.
    """
    def __init__(self, ttl: int = 300):
        self._store: Dict[str, tuple] = {}
        self._ttl   = ttl
        self._lock  = asyncio.Lock()

    async def get(self, key: str) -> Optional[dict]:
        async with self._lock:
            if key in self._store:
                val, exp = self._store[key]
                if time.time() < exp:
                    return val
                del self._store[key]
        return None

    async def set(self, key: str, val: dict):
        async with self._lock:
            if len(self._store) > 500:
                now  = time.time()
                dead = [k for k, (_, e) in self._store.items() if now >= e]
                for k in dead:
                    del self._store[k]
            self._store[key] = (val, time.time() + self._ttl)


weather_cache    = SimpleCache(ttl=300)   # 5 dk
currency_cache   = SimpleCache(ttl=120)   # 2 dk
search_cache     = SimpleCache(ttl=180)   # 3 dk
deep_cache       = AsyncCache(ttl=300)    # 5 dk

# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

class ToolType(str, Enum):
    TIME              = "time"
    WEATHER           = "weather"
    NEWS              = "news"
    CURRENCY          = "currency"
    CRYPTO            = "crypto"
    PRICE_SEARCH      = "price_search"
    DEFINITION        = "definition"
    GENERAL_KNOWLEDGE = "general_knowledge"
    WEB_SEARCH        = "web_search"
    DEEP_SEARCH       = "deep_search"


class UnifiedRequest(BaseModel):
    query:     str
    tool_type: Optional[ToolType] = None


class WebSearchRequest(BaseModel):
    query:       str
    num_results: int = 5


class DeepSearchRequest(BaseModel):
    query:        str
    num_results:  int  = 5
    fetch_pages:  int  = 3
    synthesize:   bool = True
    language:     str  = "tr"
    context_hint: Optional[str] = None


class SuggestRequest(BaseModel):
    query:           str
    conversation_id: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# SCOPE — zararlı içerik hariç HERŞEYİ kabul et
# ═══════════════════════════════════════════════════════════════

def is_in_scope(query: str) -> bool:
    """
    Zararlı içerik ve argo hariç her konuyu kabul eder.
    Yemek, seyahat, spor, sağlık, teknoloji... hepsi scope içinde.
    """
    q = query.lower()
    harmful = (
        "bomba yap", "patlayıcı yap", "silah yap", "uyuşturucu yap",
        "bomb make", "drug make", "weapon make",
        "hack into", "ddos saldır", "malware yaz", "ransomware",
        "çocuk istismar", "child abuse",
        "nasıl öldürülür", "how to kill",
    )
    if any(h in q for h in harmful):
        print(f"[SCOPE] Blocked: '{query[:50]}'")
        return False
    return True


def detect_current_events_query(query: str) -> bool:
    """Güncel bilgi gerektiren sorgu mu?"""
    q = query.lower()
    recency = (
        "son", "güncel", "yeni", "bugün", "bu hafta", "bu ay",
        "şimdiki", "en son", "son dakika", "breaking",
        "latest", "recent", "current", "new", "today", "this week",
        "haberleri", "gündem", "gelişme", "haber", "duyuru",
        "news", "update", "announcement", "2024", "2025",
    )
    broad = (
        "yapay zeka", "ai model", "chatgpt", "gemini", "claude",
        "teknoloji", "kripto", "bitcoin", "borsa", "hisse",
        "siyaset", "seçim", "election", "uzay", "nasa", "spacex",
    )
    has_recency = any(s in q for s in recency)
    has_broad   = any(t in q for t in broad)
    return has_recency or (has_broad and len(q.split()) >= 3)


def auto_detect_tool(query: str) -> ToolType:
    """Akıllı tool tespiti — sıralama kritik."""
    q = query.lower()

    currency_kw = ["dolar", "euro", "kur", "döviz", "doviz", "sterlin",
                   "exchange", "pound", "gbp", "usd", "eur", "try"]
    if any(kw in q for kw in currency_kw):
        return ToolType.CURRENCY

    crypto_kw = ["bitcoin", "ethereum", "kripto", "crypto", "btc", "eth",
                 "dogecoin", "doge", "solana"]
    if any(kw in q for kw in crypto_kw):
        return ToolType.CRYPTO

    weather_kw = ["hava", "havadurumu", "havalar", "weather", "sıcaklık",
                  "sicaklik", "derece", "yağmur", "yagmur", "kar", "rüzgar"]
    if any(kw in q for kw in weather_kw):
        return ToolType.WEATHER

    price_kw = ["fiyat", "price", "kaç lira", "ne kadar", "cost", "kilosu"]
    if any(kw in q for kw in price_kw):
        return ToolType.PRICE_SEARCH

    time_patterns = ["saat kaç", "saat kac", "kaçta", "kacta", "saat ",
                     "tarih", "şimdi saat", "what time"]
    is_time = any(kw in q for kw in time_patterns) or q.strip() in ["saat", "date"]
    if not is_time and "kaç" in q:
        if not any(kw in q for kw in currency_kw + crypto_kw + weather_kw):
            is_time = True
    if is_time and "hava" not in q:
        return ToolType.TIME

    if any(kw in q for kw in ["nedir", "what is", "ne demek", "kimdir", "who is"]):
        return ToolType.DEFINITION

    if any(kw in q for kw in ["haber", "news", "gündem", "son dakika"]):
        return ToolType.NEWS

    if any(kw in q for kw in ["nerede", "where", "gezilecek", "önerir misin"]):
        return ToolType.GENERAL_KNOWLEDGE

    if detect_current_events_query(query):
        return ToolType.DEEP_SEARCH

    return ToolType.WEB_SEARCH


# ═══════════════════════════════════════════════════════════════
# WMO WEATHER CODES
# ═══════════════════════════════════════════════════════════════

WEATHER_CODES = {
    0: "Açık", 1: "Genellikle açık", 2: "Parçalı bulutlu", 3: "Kapalı",
    45: "Sisli", 48: "Kırağılı sis",
    51: "Hafif çisenti", 53: "Orta çisenti", 55: "Yoğun çisenti",
    61: "Hafif yağmur", 63: "Orta yağmur", 65: "Şiddetli yağmur",
    71: "Hafif kar", 73: "Orta kar", 75: "Şiddetli kar",
    80: "Hafif sağanak", 81: "Orta sağanak", 82: "Şiddetli sağanak",
    95: "Gök gürültülü fırtına", 99: "Şiddetli dolu fırtına",
}

# ═══════════════════════════════════════════════════════════════
# SYNC UTILITY TOOLS (hava, saat, kur, kripto, haber)
# ═══════════════════════════════════════════════════════════════

def extract_location_from_query(query: str) -> Optional[str]:
    noise = {
        'hava', 'havadurumu', 'havalar', 'weather', 'sıcaklık',
        'bugün', 'nasıl', 'nedir', 'kaç', 'derece', 'today', 'how',
        'için', 'de', 'da', 'şuan', 'şimdi', 'ne', 'nerede',
        'in', 'at', 'the', 'is', 'current', 'now', 'durumu',
    }
    words       = re.findall(r'\b[a-zA-ZçğıöşüÇĞİÖŞÜ]+\b', query)
    clean       = [w for w in words if w.lower() not in noise and len(w) > 2]
    if not clean:
        return None
    first = clean[0]
    for suf in ['daki','deki','dan','den','da','de','ta','te',
                'nın','nin','nun','nün','ın','in','un','ün']:
        if first.lower().endswith(suf) and len(first) > len(suf) + 2:
            first = first[:-len(suf)]
            break
    if len(clean) > 1:
        two = f"{clean[0]} {clean[1]}".lower()
        multi = {"new york","los angeles","san francisco","hong kong",
                 "kuala lumpur","buenos aires","cape town","tel aviv","abu dhabi"}
        if two in multi:
            return two.title()
    return first.title()


def get_current_time(timezone: str = "Europe/Istanbul") -> Dict:
    try:
        tz  = pytz.timezone(timezone)
        now = datetime.now(tz)
        days = {"Monday":"Pazartesi","Tuesday":"Salı","Wednesday":"Çarşamba",
                "Thursday":"Perşembe","Friday":"Cuma","Saturday":"Cumartesi","Sunday":"Pazar"}
        months = {"January":"Ocak","February":"Şubat","March":"Mart","April":"Nisan",
                  "May":"Mayıs","June":"Haziran","July":"Temmuz","August":"Ağustos",
                  "September":"Eylül","October":"Ekim","November":"Kasım","December":"Aralık"}
        day_tr   = days.get(now.strftime("%A"), now.strftime("%A"))
        month_tr = months.get(now.strftime("%B"), now.strftime("%B"))
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
    key    = f"weather_{city.lower()}"
    cached = weather_cache.get(key)
    if cached:
        return cached
    try:
        geo = requests.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(city)}&count=1",
            timeout=5).json()
        if not geo.get("results"):
            return {"success": False, "error": f"Şehir bulunamadı: {city}"}
        loc  = geo["results"][0]
        lat, lon   = loc["latitude"], loc["longitude"]
        city_name  = loc.get("name", city)
        country    = loc.get("country", "")
        w = requests.get(
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,wind_speed_10m", timeout=5).json()
        c    = w["current"]
        desc = WEATHER_CODES.get(c.get("weather_code", 0), "Bilinmiyor")
        wind = c.get("wind_speed_10m", 0)
        result = {
            "success": True, "tool_used": "weather",
            "data": {
                "city": city_name, "country": country,
                "temperature": round(c["temperature_2m"], 1),
                "feels_like":  round(c["apparent_temperature"], 1),
                "humidity":    c["relative_humidity_2m"],
                "description": desc,
                "wind_speed":  round(wind, 1),
                "formatted":   f"{city_name}, {country}: {round(c['temperature_2m'])}°C, {desc}",
            },
        }
        weather_cache.set(key, result)
        return result
    except Exception as e:
        return {"success": False, "error": f"Hava durumu alınamadı: {str(e)}"}


def get_currency_rate(from_c: str = "USD", to_c: str = "TRY") -> Dict:
    key    = f"currency_{from_c}_{to_c}"
    cached = currency_cache.get(key)
    if cached:
        return cached
    try:
        resp = requests.get(f"https://api.exchangerate-api.com/v4/latest/{from_c}", timeout=5)
        resp.raise_for_status()
        rate = resp.json()["rates"].get(to_c)
        if rate:
            result = {"success": True, "data": {
                "from": from_c, "to": to_c, "rate": rate,
                "formatted": f"1 {from_c} = {rate:.2f} {to_c}",
            }}
            currency_cache.set(key, result)
            return result
    except Exception:
        pass
    try:
        resp = requests.get(
            f"https://www.google.com/finance/quote/{from_c}-{to_c}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        soup = BeautifulSoup(resp.text, 'html.parser')
        el   = soup.find("div", {"class": "YMlKec fxKbKc"})
        if el:
            rate   = float(el.text.replace(',', '.'))
            result = {"success": True, "data": {
                "from": from_c, "to": to_c, "rate": round(rate, 4),
                "formatted": f"1 {from_c} = {round(rate, 2)} {to_c}",
            }}
            currency_cache.set(key, result)
            return result
    except Exception:
        pass
    return {"success": False, "error": "Kur bilgisi alınamadı"}


def extract_currencies(query: str) -> tuple:
    q   = query.lower()
    cmap = {"dolar":"USD","usd":"USD","euro":"EUR","eur":"EUR",
             "pound":"GBP","sterlin":"GBP","gbp":"GBP",
             "tl":"TRY","try":"TRY","lira":"TRY","yen":"JPY","frank":"CHF"}
    found = [v for k, v in cmap.items() if k in q]
    return (found[0] if found else "USD", found[1] if len(found) > 1 else "TRY")


def get_news(query: Optional[str] = None) -> Dict:
    try:
        url  = (f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=tr&gl=TR"
                if query and len(query) > 2
                else "https://news.google.com/rss?hl=tr&gl=TR")
        resp = requests.get(url, timeout=5)
        soup = BeautifulSoup(resp.text, 'xml')
        items = soup.find_all('item', limit=5)
        articles = [
            {"title":     i.title.text if i.title else "Başlık yok",
             "link":      i.link.text  if i.link  else "",
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
        return {"success": True, "data": {
            "coin": coin, "price": price, "change_24h": round(change, 2),
            "formatted": f"{coin.title()}: ${price:,.2f}",
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════════════════════════════
# SYNC WEB SEARCH (waterfall — /unified için)
# ═══════════════════════════════════════════════════════════════

def _search_searxng(query: str, num: int = 5) -> Dict:
    if not SEARXNG_URL:
        return {"success": False}
    try:
        data = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json",
                    "engines": "bing,duckduckgo", "language": "tr"},
            timeout=10).json()
        results = data.get("results", [])[:num]
        if results:
            return {"success": True, "data": {
                "query": query, "provider": "searxng",
                "results": [{"title": r.get("title",""), "url": r.get("url",""),
                             "content": r.get("content","")[:500]}
                            for r in results]
            }}
    except Exception as e:
        print(f"[SEARCH] SearXNG: {e}")
    return {"success": False}


def _search_brave(query: str, num: int = 5) -> Dict:
    if not BRAVE_API_KEY:
        return {"success": False}
    try:
        resp    = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": num, "search_lang": "tr"},
            headers={"Accept": "application/json",
                     "X-Subscription-Token": BRAVE_API_KEY},
            timeout=10).json()
        results = resp.get("web", {}).get("results", [])[:num]
        if results:
            return {"success": True, "data": {
                "query": query, "provider": "brave",
                "results": [{"title": r.get("title",""), "url": r.get("url",""),
                             "content": r.get("description","")[:500]}
                            for r in results]
            }}
    except Exception as e:
        print(f"[SEARCH] Brave: {e}")
    return {"success": False}


def _search_duckduckgo(query: str, num: int = 5) -> Dict:
    ua_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    ]
    for ua in ua_list:
        try:
            resp    = requests.get(
                f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                headers={"User-Agent": ua}, timeout=10)
            soup    = BeautifulSoup(resp.text, 'html.parser')
            results = []
            for r in soup.find_all('div', class_='result', limit=num):
                t = r.find('a', class_='result__a')
                s = r.find('a', class_='result__snippet')
                if t:
                    results.append({
                        "title":   t.get_text(strip=True),
                        "url":     t.get('href',''),
                        "content": s.get_text(strip=True)[:500] if s else "",
                    })
            if results:
                return {"success": True, "data": {
                    "query": query, "provider": "duckduckgo", "results": results
                }}
        except Exception as e:
            print(f"[SEARCH] DDG: {e}")
            time.sleep(0.5)
    return {"success": False, "error": "Sonuç bulunamadı"}


def web_search(query: str, num: int = 5) -> Dict:
    """Sync waterfall: SearXNG → Brave → DuckDuckGo + 3dk cache."""
    key    = f"search_{query.lower().strip()}"
    cached = search_cache.get(key)
    if cached:
        return cached
    for fn in [
        lambda: _search_searxng(query, num),
        lambda: _search_brave(query, num),
        lambda: _search_duckduckgo(query, num),
    ]:
        r = fn()
        if r.get("success"):
            search_cache.set(key, r)
            return r
    return {"success": False, "error": "Tüm sağlayıcılar başarısız"}

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ASYNC DEEP SEARCH ENGINE
# SearXNG/DDG → Jina.ai (+ Crawl4AI fallback) → LLM sentezi
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

async def async_web_search(query: str, num: int = 5) -> Dict:
    """
    Async waterfall arama — non-blocking, çoklu kullanıcı güvenli.
    SearXNG → Brave → DuckDuckGo
    """
    key    = f"async_{query.lower().strip()}"
    cached = await deep_cache.get(key)
    if cached:
        print(f"[ASYNC SEARCH] Cache: '{query}'")
        return cached

    async with httpx.AsyncClient(timeout=12.0) as client:

        # 1. SearXNG
        if SEARXNG_URL:
            try:
                resp = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={"q": query, "format": "json",
                            "engines": "bing,duckduckgo", "language": "tr"},
                )
                results = resp.json().get("results", [])[:num]
                if results:
                    r = {"success": True, "data": {
                        "query": query, "provider": "searxng",
                        "results": [{"title": x.get("title",""),
                                     "url":   x.get("url",""),
                                     "content": x.get("content","")[:500]}
                                    for x in results]
                    }}
                    await deep_cache.set(key, r)
                    print(f"[ASYNC SEARCH] SearXNG: {len(results)} sonuç")
                    return r
            except Exception as e:
                print(f"[ASYNC SEARCH] SearXNG hata: {e}")

        # 2. Brave
        if BRAVE_API_KEY:
            try:
                resp    = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": num, "search_lang": "tr"},
                    headers={"Accept": "application/json",
                             "X-Subscription-Token": BRAVE_API_KEY},
                )
                results = resp.json().get("web", {}).get("results", [])[:num]
                if results:
                    r = {"success": True, "data": {
                        "query": query, "provider": "brave",
                        "results": [{"title": x.get("title",""),
                                     "url":   x.get("url",""),
                                     "content": x.get("description","")[:500]}
                                    for x in results]
                    }}
                    await deep_cache.set(key, r)
                    print(f"[ASYNC SEARCH] Brave: {len(results)} sonuç")
                    return r
            except Exception as e:
                print(f"[ASYNC SEARCH] Brave hata: {e}")

        # 3. DuckDuckGo async
        for ua in [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        ]:
            try:
                resp    = await client.get(
                    f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                    headers={"User-Agent": ua},
                )
                soup    = BeautifulSoup(resp.text, 'html.parser')
                results = []
                for div in soup.find_all('div', class_='result', limit=num):
                    t = div.find('a', class_='result__a')
                    s = div.find('a', class_='result__snippet')
                    if t:
                        results.append({
                            "title":   t.get_text(strip=True),
                            "url":     t.get('href', ''),
                            "content": s.get_text(strip=True)[:500] if s else "",
                        })
                if results:
                    r = {"success": True, "data": {
                        "query": query, "provider": "duckduckgo", "results": results
                    }}
                    await deep_cache.set(key, r)
                    print(f"[ASYNC SEARCH] DDG: {len(results)} sonuç")
                    return r
                break
            except Exception as e:
                print(f"[ASYNC SEARCH] DDG hata: {e}")

    return {"success": False, "error": "Tüm arama sağlayıcıları başarısız"}


async def fetch_via_jina(url: str) -> Optional[str]:
    """
    Jina.ai Reader ile sayfa içeriği çek.
    https://r.jina.ai/URL → temiz markdown döndürür.
    Ücretsiz, sınırsız, API key yok, JS render ediyor.
    """
    try:
        jina_url = f"{JINA_BASE_URL}/{url}"
        async with httpx.AsyncClient(timeout=JINA_TIMEOUT) as client:
            resp = await client.get(
                jina_url,
                headers={
                    "Accept":          "text/plain",
                    "X-Return-Format": "markdown",
                    "X-Timeout":       "8",
                    # İstersen gelecekte API key ekleyebilirsin:
                    # "Authorization": f"Bearer {JINA_API_KEY}"
                },
            )
            if resp.status_code != 200:
                return None
            text = resp.text.strip()
            if not text or len(text) < 100:
                return None
            print(f"[JINA] ✅ {url[:60]} → {len(text)} chars")
            return text[:MAX_PAGE_CHARS]
    except Exception as e:
        print(f"[JINA] ❌ {url[:60]}: {e}")
        return None


async def fetch_via_crawl4ai(url: str) -> Optional[str]:
    """
    Crawl4AI ile sayfa içeriği çek — local pod.
    Henüz kurulu değil, CRAWL4AI_URL set edilince devreye girer.
    K8s'e eklenince: CRAWL4AI_URL=http://skylight-crawl4ai:11235
    """
    if not CRAWL4AI_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=CRAWL4AI_TIMEOUT) as client:
            # Crawl4AI sync endpoint
            resp = await client.post(
                f"{CRAWL4AI_URL}/crawl",
                json={
                    "urls":     [url],
                    "priority": 8,
                    "crawler_params": {
                        "headless": True,
                        "word_count_threshold": 50,
                    },
                    "extra": {
                        "word_count_threshold": 50,
                    },
                },
            )
            if resp.status_code != 200:
                return None
            data    = resp.json()
            results = data.get("results", [])
            if results and results[0].get("success"):
                md = results[0].get("markdown", "") or results[0].get("extracted_content", "")
                if md and len(md) > 100:
                    print(f"[CRAWL4AI] ✅ {url[:60]} → {len(md)} chars")
                    return md[:MAX_PAGE_CHARS]
            return None
    except Exception as e:
        print(f"[CRAWL4AI] ❌ {url[:60]}: {e}")
        return None


async def fetch_via_direct(url: str) -> Optional[str]:
    """
    Direkt httpx scrape — son çare.
    JS render yok, bloklama olabilir ama bazen yeterli.
    """
    skip = ("youtube.com", "youtu.be", "twitter.com", "x.com",
            "instagram.com", "facebook.com", "tiktok.com", "reddit.com")
    if any(d in url for d in skip):
        return None
    try:
        async with httpx.AsyncClient(timeout=DIRECT_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 Chrome/120.0.0.0"},
            )
            if resp.status_code != 200:
                return None
            ct = resp.headers.get("content-type", "")
            if "text/html" not in ct:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            for tag in soup(["script","style","nav","footer","header",
                              "aside","form","button","iframe"]):
                tag.decompose()
            blocks = []
            for tag in soup.find_all(["article","main","section","p","h1","h2","h3"]):
                text = tag.get_text(separator=" ", strip=True)
                if len(text) > 60:
                    blocks.append(text)
            full = re.sub(r'\s+', ' ', " ".join(blocks)).strip()
            if len(full) > 100:
                print(f"[DIRECT] ✅ {url[:60]} → {len(full)} chars")
                return full[:MAX_PAGE_CHARS]
    except Exception as e:
        print(f"[DIRECT] ❌ {url[:60]}: {e}")
    return None


async def fetch_page_content(url: str) -> Optional[str]:
    """
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    SAYFA İÇERİĞİ ÇEKME — Üç Katmanlı Fallback
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Katman 1: Jina.ai Reader
      → r.jina.ai/URL
      → Ücretsiz, sınırsız, JS render, temiz markdown
      → Şu an aktif olan katman

    Katman 2: Crawl4AI (local pod)
      → CRAWL4AI_URL set edilince devreye girer
      → K8s'e ekleyince: CRAWL4AI_URL=http://skylight-crawl4ai:11235
      → Daha hızlı, daha kontrollü, tamamen local

    Katman 3: Direkt scrape
      → Son çare, JS render yok
      → En düşük kalite ama her zaman dener
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    if not url or not url.startswith(("http://", "https://")):
        return None

    # Katman 1: Jina.ai (her zaman dene — ücretsiz ve hızlı)
    content = await fetch_via_jina(url)
    if content:
        return content

    # Katman 2: Crawl4AI (varsa)
    if CRAWL4AI_URL:
        content = await fetch_via_crawl4ai(url)
        if content:
            return content

    # Katman 3: Direkt scrape
    content = await fetch_via_direct(url)
    return content


async def synthesize_with_llm(
    query:          str,
    search_results: List[dict],
    page_contents:  List[dict],
    language:       str = "tr",
    context_hint:   Optional[str] = None,
) -> str:
    """
    Ham arama sonuçları + sayfa içeriklerini LLM ile sentezler.
    DeepInfra API — zaten kullandığın servis.

    API key yoksa ham sonuçları düzenli şekilde döndürür.
    """
    if not DEEPINFRA_API_KEY:
        # API key yok → ham sonuçları güzel formatta döndür
        lines = []
        for i, r in enumerate(search_results[:5], 1):
            lines.append(f"**{i}. {r.get('title','')}**\n{r.get('content','')[:300]}")
        return "\n\n".join(lines)

    # Arama sonuçları özeti
    search_txt = "\n".join([
        f"[{i+1}] {r.get('title','')}\n    {r.get('content','')[:400]}"
        for i, r in enumerate(search_results[:5])
    ])

    # Sayfa içerikleri
    page_txt = ""
    if page_contents:
        parts = []
        for pc in page_contents:
            if pc.get("content"):
                parts.append(
                    f"━━ Kaynak: {pc.get('title', pc.get('url','?'))} ━━\n"
                    f"{pc['content']}"
                )
        page_txt = "\n\n".join(parts)

    lang_rule = "YANITI TÜRKÇE yaz." if language == "tr" else "Respond in ENGLISH."
    ctx_block = f"\nÖnceki konuşma bağlamı: {context_hint}" if context_hint else ""

    prompt = f"""Kullanıcı sorusu: "{query}"{ctx_block}

WEB ARAMA SONUÇLARI:
{search_txt}

SAYFA İÇERİKLERİ (Jina.ai ile çekildi):
{page_txt if page_txt else "(Sayfa içeriği çekilemedi — sadece arama özeti kullanılıyor)"}

Bu gerçek web verilerini kullanarak yanıt ver:

1. {lang_rule}
2. Doğrudan ve özlü başla — "Merhaba", "Tabii ki" gibi giriş cümlesi yok
3. Güncel ve spesifik bilgileri öne çıkar — tarih, sayı, isim varsa ekle
4. Çelişkili bilgiler varsa en güncel/güvenilir kaynağı tercih et
5. Kaynakları doğal belirt: "X'e göre..." veya "(Kaynak: Y)"
6. Yanıt sonunda 1 cümle öneri: "Daha fazla bilgi için [konu] araştırabilirsin"
7. Maksimum 400 kelime — öz ve bilgi dolu ol

SADECE gerçek web verilerini kullan. Uydurmak yasak."""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                },
                json={
                    "model":       SYNTHESIS_MODEL,
                    "messages":    [
                        {"role": "system",
                         "content": "Sen web araması sonuçlarını analiz eden, "
                                    "kullanıcıya net ve kaynaklı yanıtlar sunan bir asistansın. "
                                    "Sadece verilen web verilerini kullan."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens":  900,
                    "temperature": 0.25,
                    "stream":      False,
                },
            )
            content = resp.json().get("choices",[{}])[0].get("message",{}).get("content","")
            if content:
                print(f"[LLM] ✅ Sentez: {len(content)} chars")
                return content
    except Exception as e:
        print(f"[LLM] ❌ Sentez hatası: {e}")

    # Fallback: ham sonuçlar
    return "\n\n".join([
        f"**{r.get('title','')}**\n{r.get('content','')[:250]}"
        for r in search_results[:4]
    ])


async def deep_search_pipeline(req: DeepSearchRequest) -> Dict:
    """
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    DEEP SEARCH PIPELINE — Tam Akış

    ADIM 1: Arama
      SearXNG/Brave/DDG → URL listesi + snippet'ler

    ADIM 2: Paralel Sayfa Çekme (asyncio.gather)
      Jina.ai → URL başına temiz markdown (aynı anda hepsi)
      Crawl4AI → Jina başarısızsa (local pod gelince)
      Direkt → Son çare

    ADIM 3: LLM Sentezi
      DeepInfra → Tüm içerikleri analiz et, sentezle

    ADIM 4: Yapılandırılmış Yanıt
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    t0       = time.time()
    cache_key = f"deep_{req.query.lower().strip()}_{req.num_results}_{req.fetch_pages}"

    cached = await deep_cache.get(cache_key)
    if cached:
        print(f"[DEEP SEARCH] Cache: '{req.query}'")
        return cached

    print(f"\n{'━'*60}")
    print(f"[DEEP SEARCH] '{req.query}'")
    print(f"[DEEP SEARCH] results={req.num_results} pages={req.fetch_pages} "
          f"synth={req.synthesize} lang={req.language}")
    print(f"{'━'*60}")

    # ── ADIM 1: Arama ─────────────────────────────────────────
    search_res = await async_web_search(req.query, req.num_results)
    if not search_res.get("success"):
        return {"success": False, "error": "Arama başarısız",
                "query": req.query, "tool_used": "deep_search"}

    results  = search_res.get("data", {}).get("results", [])
    provider = search_res.get("data", {}).get("provider", "?")
    print(f"[DEEP SEARCH] Adım 1 ✅: {len(results)} sonuç ({provider})")

    # ── ADIM 2: Paralel sayfa çekme ───────────────────────────
    page_contents: List[dict] = []
    if results and req.fetch_pages > 0:
        urls = [r.get("url","") for r in results[:req.fetch_pages] if r.get("url")]

        # asyncio.gather — hepsi aynı anda çekilir, seri değil
        print(f"[DEEP SEARCH] Adım 2: {len(urls)} sayfa paralel çekiliyor (Jina.ai)...")
        tasks   = [fetch_page_content(url) for url in urls]
        fetched = await asyncio.gather(*tasks, return_exceptions=True)

        for i, (result_item, page_text) in enumerate(zip(results[:req.fetch_pages], fetched)):
            if isinstance(page_text, str) and page_text:
                page_contents.append({
                    "url":     result_item.get("url", ""),
                    "title":   result_item.get("title", ""),
                    "content": page_text,
                })
                print(f"[DEEP SEARCH]   [{i+1}] ✅ {result_item.get('title','')[:50]} "
                      f"→ {len(page_text)} chars")
            else:
                print(f"[DEEP SEARCH]   [{i+1}] ❌ atlandı")

    print(f"[DEEP SEARCH] Adım 2 ✅: {len(page_contents)}/{req.fetch_pages} sayfa alındı")

    # ── ADIM 3: LLM Sentezi ───────────────────────────────────
    synthesis = None
    if req.synthesize:
        print(f"[DEEP SEARCH] Adım 3: LLM sentezi ({SYNTHESIS_MODEL})...")
        synthesis = await synthesize_with_llm(
            query          = req.query,
            search_results = results,
            page_contents  = page_contents,
            language       = req.language,
            context_hint   = req.context_hint,
        )
        print(f"[DEEP SEARCH] Adım 3 ✅: {len(synthesis or '')} chars")

    elapsed = round(time.time() - t0, 2)
    print(f"[DEEP SEARCH] Toplam süre: {elapsed}s\n")

    result = {
        "success":   True,
        "tool_used": "deep_search",
        "query":     req.query,
        "provider":  provider,
        "data": {
            "synthesis": synthesis,
            "search_results": [
                {"title":   r.get("title", ""),
                 "url":     r.get("url", ""),
                 "snippet": r.get("content", "")[:200]}
                for r in results[:req.num_results]
            ],
            "pages_fetched":   len(page_contents),
            "total_results":   len(results),
            "elapsed_seconds": elapsed,
            "fetch_method":    "jina.ai" + ("+crawl4ai_ready" if CRAWL4AI_URL else ""),
        },
    }
    await deep_cache.set(cache_key, result)
    return result

# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "service": "Skylight Smart Tools",
        "version": "6.1.0",
        "scope":   "Evrensel — zararlı içerik hariç her konu",
        "features": {
            "weather":     "open-meteo.com (FREE, UNLIMITED)",
            "time":        "Europe/Istanbul",
            "news":        "Google News RSS (FREE)",
            "currency":    "exchangerate-api + Google Finance (FREE)",
            "crypto":      "CoinGecko (FREE)",
            "web_search":  "SearXNG → Brave → DuckDuckGo (waterfall)",
            "deep_search": "Arama + Jina.ai sayfa çekme + LLM sentezi",
        },
        "page_fetching": {
            "layer_1": "Jina.ai Reader (aktif — ücretsiz, sınırsız)",
            "layer_2": f"Crawl4AI ({'aktif: ' + CRAWL4AI_URL if CRAWL4AI_URL else 'hazır — CRAWL4AI_URL set edilince devreye girer'})",
            "layer_3": "Direkt scrape (son çare)",
        },
        "search_providers": {
            "searxng":    bool(SEARXNG_URL),
            "brave":      bool(BRAVE_API_KEY),
            "duckduckgo": True,
        },
        "llm_synthesis": bool(DEEPINFRA_API_KEY),
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "6.1.0",
        "jina":      "active",
        "crawl4ai":  CRAWL4AI_URL or "not_configured",
        "searxng":   SEARXNG_URL  or "not_configured",
    }


@app.post("/unified")
async def unified_endpoint(request: UnifiedRequest):
    """Ana endpoint — tüm sorguları yönetir."""
    query     = request.query
    tool_type = request.tool_type or auto_detect_tool(query)

    print(f"\n{'─'*50}")
    print(f"[UNIFIED] '{query}' → {tool_type.value}")
    print(f"{'─'*50}")

    if not is_in_scope(query):
        return {"success": False, "error": "Bu içeriğe yardımcı olamam.",
                "tool_used": "blocked", "query": query}

    try:
        if tool_type == ToolType.TIME:
            result = get_current_time()

        elif tool_type == ToolType.WEATHER:
            result = get_weather_dynamic(query)

        elif tool_type == ToolType.CURRENCY:
            fc, tc = extract_currencies(query)
            result = get_currency_rate(fc, tc)

        elif tool_type == ToolType.NEWS:
            topic  = query.replace("haber","").replace("news","").strip()
            result = get_news(topic if len(topic) > 2 else None)

        elif tool_type == ToolType.CRYPTO:
            coin = "bitcoin"
            q    = query.lower()
            if "ethereum" in q or " eth" in q: coin = "ethereum"
            elif "dogecoin" in q or "doge" in q: coin = "dogecoin"
            elif "solana"   in q or " sol" in q: coin = "solana"
            result = get_crypto_price(coin)

        elif tool_type == ToolType.DEEP_SEARCH:
            result = await deep_search_pipeline(DeepSearchRequest(
                query        = query,
                num_results  = 5,
                fetch_pages  = 3,
                synthesize   = True,
                language     = "tr",
            ))

        else:
            result = web_search(query, 5)

        result["tool_used"] = tool_type.value
        result["query"]     = query
        return result

    except Exception as e:
        print(f"[UNIFIED ERROR] {e}")
        return {"success": False, "error": str(e),
                "tool_used": tool_type.value, "query": query}


@app.post("/search")
async def search_endpoint(request: WebSearchRequest):
    """Direkt web arama."""
    r = web_search(request.query, request.num_results)
    r["tool_used"] = "web_search"
    return r


@app.post("/deep_search")
async def deep_search_endpoint(request: DeepSearchRequest):
    """
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    DEEP SEARCH — Gateway bu endpoint'i çağırır

    Örnek request:
    {
        "query": "son yapay zeka modelleri 2025",
        "num_results": 5,
        "fetch_pages": 3,
        "synthesize": true,
        "language": "tr",
        "context_hint": "kullanıcı AI ile ilgileniyor"
    }

    Örnek response:
    {
        "success": true,
        "data": {
            "synthesis": "GPT-5 Mart 2025'te...",
            "search_results": [...],
            "pages_fetched": 3,
            "elapsed_seconds": 5.2
        }
    }
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    return await deep_search_pipeline(request)


@app.post("/suggest")
async def suggest_endpoint(request: SuggestRequest):
    """Gateway web araması önermeli mi?"""
    query = request.query
    q     = query.lower()

    if detect_current_events_query(query):
        return {"should_suggest": True, "reason": "current_events",
                "suggested_query": query,
                "message": "Bu konu güncel bilgi gerektiriyor. Webden araştırayım mı? 🔍"}

    if any(kw in q for kw in ["haber","news","gündem","son dakika","araştır","bul bana"]):
        return {"should_suggest": True, "reason": "explicit_search",
                "suggested_query": query,
                "message": "Webden arama yapayım mı? 🔍"}

    if any(kw in q for kw in ["nedir","kimdir","ne demek"]) and len(query.split()) <= 5:
        return {"should_suggest": True, "reason": "definition_maybe_current",
                "suggested_query": query,
                "message": "Güncel bilgiye bakayım mı? 🔍"}

    return {"should_suggest": False, "reason": "not_needed",
            "suggested_query": query, "message": ""}


@app.get("/fetch")
async def fetch_page_endpoint(url: str):
    """
    Tek URL'nin içeriğini çek — test için.
    Örnek: GET /fetch?url=https://example.com
    """
    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "Geçersiz URL"}
    content = await fetch_page_content(url)
    if content:
        return {"success": True, "url": url,
                "char_count": len(content), "content": content}
    return {"success": False, "url": url, "error": "İçerik çekilemedi"}

# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*70)
    print("SKYLIGHT SMART TOOLS — v6.1 (Jina Deep Search)")
    print("="*70)
    print(f"Search:     SearXNG({'ON' if SEARXNG_URL else 'OFF'}) | "
          f"Brave({'ON' if BRAVE_API_KEY else 'OFF'}) | DDG(ON)")
    print(f"Sayfa çekme: Jina.ai(ON) | "
          f"Crawl4AI({'ON: '+CRAWL4AI_URL if CRAWL4AI_URL else 'OFF - hazır'})")
    print(f"LLM sentez: {'ON — '+SYNTHESIS_MODEL if DEEPINFRA_API_KEY else 'OFF'}")
    print("Scope:      Evrensel — zararlı içerik hariç her konu")
    print("="*70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8081, workers=4)