"""
═══════════════════════════════════════════════════════════════
SKYLIGHT SMART TOOLS SERVICE — v7.1
═══════════════════════════════════════════════════════════════
Görev:
  - Canlı veri API'leri (kur, hava, kripto, saat, haberler)
  - Deep Search pipeline (SearXNG → Jina.ai → LLM sentezi)
  - Web arama (SearXNG → Brave → DuckDuckGo waterfall)

Detection (canlı veri lazım mı?) → chat_service ve gateway'de LOCAL yapılır.
Bu servis sadece veriyi getirir, karar vermez.

Endpoints:
  POST /unified      → Canlı veri + web arama (chat servisi çağırır)
  POST /deep_search  → Araştırma pipeline (gateway çağırır)
  POST /search       → Direkt web arama
  GET  /fetch        → Tek URL içeriği (test)
  GET  /health
  GET  /

Sayfa çekme:
  1. Jina.ai   → r.jina.ai/URL (ücretsiz, sınırsız, JS render)
  2. Crawl4AI  → CRAWL4AI_URL set edilince devreye girer
  3. Direkt    → Son çare

Arama:
  SearXNG → Brave → DuckDuckGo
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime
from urllib.parse import quote_plus
import pytz
import requests
import httpx
from bs4 import BeautifulSoup
import re
import os
import time
import asyncio
import json

# ── Web Search Engine v3 (API key gerekmez) ─────────────────
try:
    from search_engine_v3 import web_search as _web_search_v3
    from search_engine_v3 import web_search_sync as _web_search_sync_v3
    _SEARCH_V3 = True
    print("[SEARCH] ✅ search_engine_v3 yüklendi")
except ImportError:
    _SEARCH_V3 = False
    print("[SEARCH] ⚠️ search_engine_v3 bulunamadı — eski sistem")

from enum import Enum

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="Skylight Smart Tools", version="7.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

SEARXNG_URL        = os.getenv("SEARXNG_URL",        None)
BRAVE_API_KEY      = os.getenv("BRAVE_API_KEY",      None)
CRAWL4AI_URL       = os.getenv("CRAWL4AI_URL",       "http://crawl4ai:11235")
CRAWL4AI_TOKEN     = os.getenv("CRAWL4AI_TOKEN",     "skylight-crawl4ai-2026")
DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY",  "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
SYNTHESIS_MODEL    = os.getenv("SYNTHESIS_MODEL",    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8")
RERANK_MODEL       = os.getenv("RERANK_MODEL",       "BAAI/bge-reranker-v2-m3")
RERANKER_URL       = os.getenv("RERANKER_URL",       "http://skylight-reranker:8087")

MAX_PAGE_CHARS   = 8000   # Crawl4AI daha temiz çıktı verir, daha fazla alabiliriz
CHUNK_SIZE       = 200    # Kelime — reranker 512 token sınırı var, küçük chunk daha iyi
CHUNK_OVERLAP    = 30     # Örtüşme
TOP_CHUNKS       = 8      # Rerank sonrası kaç chunk — küçük chunk, daha fazla alınabilir
CRAWL4AI_TIMEOUT = 20
DIRECT_TIMEOUT   = 8

# ═══════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════

class SimpleCache:
    def __init__(self, ttl: int = 300):
        self._s: Dict[str, tuple] = {}
        self._ttl = ttl

    def get(self, k: str) -> Optional[dict]:
        if k in self._s:
            v, e = self._s[k]
            if time.time() < e:
                return v
            del self._s[k]
        return None

    def set(self, k: str, v: dict):
        if len(self._s) > 200:
            now = time.time()
            for dk in [x for x, (_, e) in self._s.items() if now >= e]:
                del self._s[dk]
        self._s[k] = (v, time.time() + self._ttl)


class AsyncCache:
    def __init__(self, ttl: int = 300):
        self._s: Dict[str, tuple] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    async def get(self, k: str) -> Optional[dict]:
        async with self._lock:
            if k in self._s:
                v, e = self._s[k]
                if time.time() < e:
                    return v
                del self._s[k]
        return None

    async def set(self, k: str, v: dict):
        async with self._lock:
            if len(self._s) > 500:
                now = time.time()
                for dk in [x for x, (_, e) in self._s.items() if now >= e]:
                    del self._s[dk]
            self._s[k] = (v, time.time() + self._ttl)


weather_cache  = SimpleCache(ttl=300)   # 5 dk
currency_cache = SimpleCache(ttl=120)   # 2 dk
search_cache   = SimpleCache(ttl=180)   # 3 dk
deep_cache     = AsyncCache(ttl=300)    # 5 dk

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class ToolType(str, Enum):
    TIME         = "time"
    WEATHER      = "weather"
    NEWS         = "news"
    CURRENCY     = "currency"
    CRYPTO       = "crypto"
    PRICE_SEARCH = "price_search"
    WEB_SEARCH   = "web_search"
    DEEP_SEARCH  = "deep_search"


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


# ═══════════════════════════════════════════════════════════════
# AUTO DETECT — /unified için hangi tool?
# ═══════════════════════════════════════════════════════════════

def auto_detect_tool(query: str) -> ToolType:
    """
    Chat servisi zaten ne istediğini biliyor (local detection).
    Ama /unified'a tool_type gönderilmezse burada belirlenir.
    Sıralama kritik — spesifik önce, genel sonra.
    """
    q = query.lower()

    # Döviz
    if any(k in q for k in ("dolar","euro","eur","usd","gbp","sterlin","pound",
                              "kur","döviz","doviz","kaç tl","tl kaç")):
        return ToolType.CURRENCY

    # Kripto
    if any(k in q for k in ("bitcoin","btc","ethereum","eth","dogecoin",
                              "doge","solana","kripto","crypto","coin")):
        return ToolType.CRYPTO

    # Hava
    if any(k in q for k in ("hava","havadurumu","weather","sıcaklık",
                              "sicaklik","derece","yağmur","kar","rüzgar","forecast")):
        return ToolType.WEATHER

    # Fiyat
    if any(k in q for k in ("fiyat","price","kaç lira","ne kadar",
                              "altın","petrol","gram altın")):
        return ToolType.PRICE_SEARCH

    # Saat
    if any(k in q for k in ("saat kaç","saati kaç","şimdi saat",
                              "what time","bugün ne günü")):
        return ToolType.TIME

    # Haberler
    if any(k in q for k in ("haber","news","gündem","son dakika","breaking")):
        return ToolType.NEWS

    # Varsayılan
    return ToolType.WEB_SEARCH


# ═══════════════════════════════════════════════════════════════
# WMO WEATHER CODES
# ═══════════════════════════════════════════════════════════════

WEATHER_CODES = {
    0:"Açık", 1:"Genellikle açık", 2:"Parçalı bulutlu", 3:"Kapalı",
    45:"Sisli", 48:"Kırağılı sis",
    51:"Hafif çisenti", 53:"Orta çisenti", 55:"Yoğun çisenti",
    61:"Hafif yağmur", 63:"Orta yağmur", 65:"Şiddetli yağmur",
    71:"Hafif kar", 73:"Orta kar", 75:"Şiddetli kar",
    80:"Hafif sağanak", 81:"Orta sağanak", 82:"Şiddetli sağanak",
    95:"Gök gürültülü fırtına", 99:"Şiddetli dolu fırtına",
}

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _extract_city(query: str) -> Optional[str]:
    noise = {
        'hava','durumu','weather','sıcaklık','bugün','nasıl','nedir','kaç',
        'derece','için','de','da','şuan','şimdi','ne','nerede','olan',
        'havalar','nasıl','bugünkü','anlık','şuanki','mevcut',
    }
    # Önce bilinen şehir listesine bak
    known_cities = [
        'istanbul','ankara','izmir','antalya','bursa','adana','konya',
        'berlin','london','paris','new york','tokyo','dubai','moscow',
        'new','york','los','angeles','san','francisco',
        'amsterdam','madrid','rome','vienna','barcelona',
    ]
    q_lower = query.lower()
    for city in known_cities:
        if city in q_lower:
            return city.title()

    words = re.findall(r'\b[a-zA-ZçğıöşüÇĞİÖŞÜ]+\b', query)
    clean = [w for w in words if w.lower() not in noise and len(w) > 2]
    if not clean:
        return None
    first = clean[0]
    for suf in ['daki','deki','dan','den','da','de','ta','te',
                'nın','nin','nun','nün','ın','in','un','ün',
                'da','de','nin','nun']:
        if first.lower().endswith(suf) and len(first) > len(suf) + 2:
            first = first[:-len(suf)]
            break
    if len(clean) > 1:
        two = f"{clean[0]} {clean[1]}".lower()
        if two in {"new york","los angeles","san francisco","hong kong",
                   "kuala lumpur","buenos aires","cape town","tel aviv","abu dhabi"}:
            return two.title()
    return first.title()


def _extract_currency_pair(query: str) -> tuple:
    q = query.lower()
    cmap = {
        "dolar":"USD","usd":"USD","euro":"EUR","eur":"EUR",
        "pound":"GBP","sterlin":"GBP","gbp":"GBP",
        "tl":"TRY","try":"TRY","lira":"TRY",
        "yen":"JPY","frank":"CHF",
    }
    found = [v for k, v in cmap.items() if k in q]
    return (found[0] if found else "USD", found[1] if len(found) > 1 else "TRY")


def _extract_coin(query: str) -> str:
    q = query.lower()
    if "ethereum" in q or " eth" in q: return "ethereum"
    if "dogecoin" in q or "doge" in q: return "dogecoin"
    if "solana"   in q or " sol" in q: return "solana"
    return "bitcoin"


# ═══════════════════════════════════════════════════════════════
# LIVE UTILITY TOOLS
# ═══════════════════════════════════════════════════════════════

def get_time(tz_str: str = "Europe/Istanbul") -> Dict:
    """Sistem saati — UTC+3 Türkiye (WorldTimeAPI kaldırıldı, güvenilmez)."""
    from datetime import datetime, timezone, timedelta
    tz  = timezone(timedelta(hours=3))
    now = datetime.now(tz)
    days = {
        "Monday":"Pazartesi","Tuesday":"Salı","Wednesday":"Çarşamba",
        "Thursday":"Perşembe","Friday":"Cuma",
        "Saturday":"Cumartesi","Sunday":"Pazar",
    }
    months = {
        "January":"Ocak","February":"Şubat","March":"Mart","April":"Nisan",
        "May":"Mayıs","June":"Haziran","July":"Temmuz","August":"Ağustos",
        "September":"Eylül","October":"Ekim","November":"Kasım","December":"Aralık",
    }
    day_tr   = days.get(now.strftime("%A"),   now.strftime("%A"))
    month_tr = months.get(now.strftime("%B"), now.strftime("%B"))
    formatted = f"🕐 Saat {now.strftime('%H:%M')} | 📅 {now.day} {month_tr} {now.year}, {day_tr}"
    print(f"[TIME] ✅ {now.strftime('%H:%M')} {day_tr}")
    return {"success": True, "tool_used": "time", "data": {
        "time":         now.strftime("%H:%M:%S"),
        "date":         now.strftime("%Y-%m-%d"),
        "day_tr":       day_tr,
        "month_tr":     month_tr,
        "formatted":    formatted,
        "formatted_tr": formatted,
        "short":        f"Saat {now.strftime('%H:%M')}, {day_tr}",
        "date_only":    f"{now.day} {month_tr} {now.year}, {day_tr}",
        "timezone":     "Europe/Istanbul (UTC+3)",
    }}

def get_weather(query: str) -> Dict:
    city = _extract_city(query)
    if not city:
        return {"success": False, "error": "city_not_specified",
                "message": "Hangi şehrin hava durumunu öğrenmek istersiniz?"}
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
        loc       = geo["results"][0]
        lat, lon  = loc["latitude"], loc["longitude"]
        city_name = loc.get("name", city)
        country   = loc.get("country", "")
        w = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,wind_speed_10m", timeout=5).json()
        c    = w["current"]
        desc = WEATHER_CODES.get(c.get("weather_code", 0), "Bilinmiyor")
        result = {"success": True, "tool_used": "weather", "data": {
            "city":        city_name,
            "country":     country,
            "temperature": round(c["temperature_2m"],      1),
            "feels_like":  round(c["apparent_temperature"], 1),
            "humidity":    c["relative_humidity_2m"],
            "description": desc,
            "wind_speed":  round(c.get("wind_speed_10m", 0), 1),
            "formatted": (
                f"🌍 {city_name}, {country}\n"
                f"🌡️ Sıcaklık: {round(c['temperature_2m'])}°C "
                f"(hissedilen {round(c['apparent_temperature'])}°C)\n"
                f"🌤️ Durum: {desc}\n"
                f"💧 Nem: %{c['relative_humidity_2m']} | "
                f"💨 Rüzgar: {round(c.get('wind_speed_10m', 0))} km/h"
            ),
        }}
        weather_cache.set(key, result)
        print(f"[WEATHER] {city_name}: {c['temperature_2m']}°C, {desc}")
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_currency(from_c: str = "USD", to_c: str = "TRY") -> Dict:
    key    = f"currency_{from_c}_{to_c}"
    cached = currency_cache.get(key)
    if cached:
        return cached
    # Method 1: exchangerate-api
    try:
        r    = requests.get(f"https://api.exchangerate-api.com/v4/latest/{from_c}", timeout=5)
        rate = r.json()["rates"].get(to_c)
        if rate:
            result = {"success": True, "data": {
                "from": from_c, "to": to_c, "rate": rate,
                "formatted": f"1 {from_c} = {rate:.4f} {to_c}",
            }}
            currency_cache.set(key, result)
            print(f"[CURRENCY] {from_c}/{to_c} = {rate}")
            return result
    except Exception:
        pass
    # Method 2: Google Finance fallback
    try:
        r    = requests.get(
            f"https://www.google.com/finance/quote/{from_c}-{to_c}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        soup = BeautifulSoup(r.text, 'html.parser')
        el   = soup.find("div", {"class": "YMlKec fxKbKc"})
        if el:
            rate   = float(el.text.replace(',', '.'))
            result = {"success": True, "data": {
                "from": from_c, "to": to_c, "rate": round(rate, 4),
                "formatted": f"1 {from_c} = {round(rate, 4)} {to_c}",
            }}
            currency_cache.set(key, result)
            return result
    except Exception:
        pass
    return {"success": False, "error": "Kur bilgisi alınamadı"}


def get_crypto(coin: str = "bitcoin") -> Dict:
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?"
            f"ids={coin}&vs_currencies=usd&include_24hr_change=true",
            timeout=5).json()
        if coin not in r:
            return {"success": False, "error": f"Coin bulunamadı: {coin}"}
        price  = r[coin]["usd"]
        change = r[coin].get("usd_24h_change", 0)
        print(f"[CRYPTO] {coin} = ${price:,.2f} ({change:+.2f}%)")
        return {"success": True, "data": {
            "coin": coin, "price": price, "change_24h": round(change, 2),
            "formatted": f"{coin.title()}: ${price:,.2f} (24s: {change:+.2f}%)",
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_news(query: Optional[str] = None) -> Dict:
    try:
        url = (f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=tr&gl=TR"
               if query and len(query) > 2
               else "https://news.google.com/rss?hl=tr&gl=TR")
        soup     = BeautifulSoup(requests.get(url, timeout=5).text, 'xml')
        articles = [
            {"title":     i.title.text   if i.title   else "",
             "link":      i.link.text    if i.link    else "",
             "published": i.pubDate.text if i.pubDate else ""}
            for i in soup.find_all('item', limit=5)
        ]
        if articles:
            return {"success": True, "data": {"articles": articles}}
        return {"success": False, "error": "Haber bulunamadı"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# SYNC WEB SEARCH (waterfall)
# ═══════════════════════════════════════════════════════════════

def sync_web_search(query: str, num: int = 5) -> Dict:
    if _SEARCH_V3:
        return _web_search_sync_v3(query, num)

    """SearXNG → Brave → DuckDuckGo + 3dk cache."""
    key    = f"s_{query.lower().strip()}"
    cached = search_cache.get(key)
    if cached:
        return cached

    # SearXNG
    if SEARXNG_URL:
        try:
            data    = requests.get(f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json",
                        "engines": "bing,duckduckgo", "language": "tr"},
                timeout=10).json()
            results = data.get("results", [])[:num]
            if results:
                r = {"success": True, "data": {"query": query, "provider": "searxng",
                    "results": [{"title": x.get("title",""), "url": x.get("url",""),
                                 "content": x.get("content","")[:400]} for x in results]}}
                search_cache.set(key, r)
                return r
        except Exception:
            pass

    # Brave
    if BRAVE_API_KEY:
        try:
            data    = requests.get("https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": num, "search_lang": "tr"},
                headers={"Accept": "application/json",
                         "X-Subscription-Token": BRAVE_API_KEY},
                timeout=10).json()
            results = data.get("web", {}).get("results", [])[:num]
            if results:
                r = {"success": True, "data": {"query": query, "provider": "brave",
                    "results": [{"title": x.get("title",""), "url": x.get("url",""),
                                 "content": x.get("description","")[:400]} for x in results]}}
                search_cache.set(key, r)
                return r
        except Exception:
            pass

    # DuckDuckGo
    for ua in ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
               "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"]:
        try:
            soup    = BeautifulSoup(
                requests.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                             headers={"User-Agent": ua}, timeout=10).text, 'html.parser')
            results = []
            for div in soup.find_all('div', class_='result', limit=num):
                t = div.find('a', class_='result__a')
                s = div.find('a', class_='result__snippet')
                if t:
                    results.append({"title": t.get_text(strip=True), "url": t.get('href',''),
                                    "content": s.get_text(strip=True)[:400] if s else ""})
            if results:
                r = {"success": True, "data": {"query": query,
                     "provider": "duckduckgo", "results": results}}
                search_cache.set(key, r)
                return r
            break
        except Exception:
            time.sleep(0.5)

    return {"success": False, "error": "Tüm arama sağlayıcıları başarısız"}


# ═══════════════════════════════════════════════════════════════
# ASYNC WEB SEARCH (deep search için)
# ═══════════════════════════════════════════════════════════════

async def async_web_search(query: str, num: int = 5) -> Dict:
    if _SEARCH_V3:
        return await _web_search_v3(query, num)

    """Async waterfall — non-blocking."""
    key    = f"as_{query.lower().strip()}"
    cached = await deep_cache.get(key)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=12.0) as client:

        # SearXNG
        if SEARXNG_URL:
            try:
                resp    = await client.get(f"{SEARXNG_URL}/search",
                    params={"q": query, "format": "json",
                            "engines": "bing,duckduckgo", "language": "tr"})
                results = resp.json().get("results", [])[:num]
                if results:
                    r = {"success": True, "data": {"query": query, "provider": "searxng",
                        "results": [{"title": x.get("title",""), "url": x.get("url",""),
                                     "content": x.get("content","")[:400]} for x in results]}}
                    await deep_cache.set(key, r)
                    print(f"[SEARCH] SearXNG: {len(results)} sonuç")
                    return r
            except Exception as e:
                print(f"[SEARCH] SearXNG: {e}")

        # Brave
        if BRAVE_API_KEY:
            try:
                resp    = await client.get("https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": num, "search_lang": "tr"},
                    headers={"Accept": "application/json",
                             "X-Subscription-Token": BRAVE_API_KEY})
                results = resp.json().get("web", {}).get("results", [])[:num]
                if results:
                    r = {"success": True, "data": {"query": query, "provider": "brave",
                        "results": [{"title": x.get("title",""), "url": x.get("url",""),
                                     "content": x.get("description","")[:400]} for x in results]}}
                    await deep_cache.set(key, r)
                    print(f"[SEARCH] Brave: {len(results)} sonuç")
                    return r
            except Exception as e:
                print(f"[SEARCH] Brave: {e}")

        # DuckDuckGo
        for ua in ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"]:
            try:
                resp    = await client.get(
                    f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                    headers={"User-Agent": ua})
                soup    = BeautifulSoup(resp.text, 'html.parser')
                results = []
                for div in soup.find_all('div', class_='result', limit=num):
                    t = div.find('a', class_='result__a')
                    s = div.find('a', class_='result__snippet')
                    if t:
                        results.append({"title": t.get_text(strip=True),
                                        "url": t.get('href',''),
                                        "content": s.get_text(strip=True)[:400] if s else ""})
                if results:
                    r = {"success": True, "data": {"query": query,
                         "provider": "duckduckgo", "results": results}}
                    await deep_cache.set(key, r)
                    print(f"[SEARCH] DDG: {len(results)} sonuç")
                    return r
                break
            except Exception as e:
                print(f"[SEARCH] DDG: {e}")

    return {"success": False, "error": "Tüm arama sağlayıcıları başarısız"}


# ═══════════════════════════════════════════════════════════════
# SAYFA ÇEKME — Jina → Crawl4AI → Direkt
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# CHUNK — Metni yönetilebilir parçalara böl
# LangChain gerekmez — sliding window ile yapılır
# ═══════════════════════════════════════════════════════════════

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> List[str]:
    """
    Sliding window chunk.
    chunk_size: kelime sayısı
    overlap:    örtüşen kelime sayısı (bağlamı korur)
    """
    words  = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks = []
    step   = chunk_size - overlap
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
        if i + chunk_size >= len(words):
            break
    return chunks


# ═══════════════════════════════════════════════════════════════
# RERANK — DeepInfra BGE Reranker ile en iyi chunkları seç
# ═══════════════════════════════════════════════════════════════

async def rerank_chunks(query: str, chunks: List[str],
                        top_k: int = TOP_CHUNKS) -> List[str]:
    """
    Lokal BGE Reranker servisi ile query-chunk alaka puanı hesapla.
    Servis yoksa keyword fallback.
    """
    if not chunks:
        return []

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{RERANKER_URL}/rerank",
                json={
                    "query":     query,
                    "documents": [c[:512] for c in chunks],
                    "top_k":     top_k,
                    "normalize": True,
                },
            )
            if resp.status_code == 200:
                data    = resp.json()
                results = data.get("results", [])
                model   = data.get("model", "?")
                top     = [r["document"] for r in results]
                print(f"[RERANK] ✅ {len(chunks)} → {len(top)} chunk | model={model} | best={results[0]['score']:.3f}" if results else f"[RERANK] ✅ boş sonuç")
                return top
    except Exception as e:
        print(f"[RERANK] ❌ Servis ulaşılamıyor: {e} — keyword fallback")

    return _keyword_rerank(query, chunks, top_k)


def _keyword_rerank(query: str, chunks: List[str], top_k: int) -> List[str]:
    """Basit keyword overlap skoru — API olmadan rerank."""
    q_words = set(query.lower().split())
    scored  = []
    for chunk in chunks:
        c_words = set(chunk.lower().split())
        score   = len(q_words & c_words) / (len(q_words) + 1)
        scored.append((chunk, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored[:top_k]]


# ═══════════════════════════════════════════════════════════════
# SAYFA ÇEKME — Crawl4AI → Direkt
# ═══════════════════════════════════════════════════════════════

async def fetch_via_crawl4ai(url: str) -> Optional[str]:
    """
    Crawl4AI lokal pod — JS render, temiz markdown.
    Jina.ai kaldırıldı — tüm scraping buradan.
    """
    if not CRAWL4AI_URL:
        return None
    skip = ("youtube.com","youtu.be","twitter.com","x.com",
            "instagram.com","facebook.com","tiktok.com","reddit.com")
    if any(d in url for d in skip):
        return None
    try:
        headers = {}
        if CRAWL4AI_TOKEN:
            headers["Authorization"] = f"Bearer {CRAWL4AI_TOKEN}"

        async with httpx.AsyncClient(timeout=CRAWL4AI_TIMEOUT) as client:
            # Crawl4AI v0.4+ API — token ile
            resp = await client.post(
                f"{CRAWL4AI_URL}/crawl",
                headers=headers,
                json={
                    "urls": [url],
                    "crawler_params": {
                        "headless": True,
                        "word_count_threshold": 30,
                        "excluded_tags": ["nav","footer","header","aside","script","style"],
                        "remove_overlay_elements": True,
                    }
                }
            )
            if resp.status_code != 200:
                return None
            data    = resp.json()
            results = data.get("results", [data] if data.get("success") else [])
            for r in results:
                md = (r.get("markdown") or
                      r.get("fit_markdown") or
                      r.get("extracted_content") or "")
                if md and len(md.strip()) > 100:
                    print(f"[CRAWL4AI] ✅ {url[:60]} → {len(md)} chars")
                    return md[:MAX_PAGE_CHARS]
    except Exception as e:
        print(f"[CRAWL4AI] ❌ {url[:60]}: {e}")
    return None


async def fetch_via_direct(url: str) -> Optional[str]:
    """Direkt httpx scrape — son çare, JS render yok."""
    skip = ("youtube.com","youtu.be","twitter.com","x.com",
            "instagram.com","facebook.com","tiktok.com","reddit.com")
    if any(d in url for d in skip):
        return None
    try:
        async with httpx.AsyncClient(timeout=DIRECT_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type",""):
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            for t in soup(["script","style","nav","footer","header","aside","form","button"]):
                t.decompose()
            blocks = [t.get_text(" ", strip=True) for t in
                      soup.find_all(["article","main","p","h1","h2","h3"])
                      if len(t.get_text(strip=True)) > 60]
            full = re.sub(r'\s+', ' ', " ".join(blocks)).strip()
            if len(full) > 100:
                print(f"[DIRECT] ✅ {url[:60]} → {len(full)} chars")
                return full[:MAX_PAGE_CHARS]
    except Exception as e:
        print(f"[DIRECT] ❌ {url[:60]}: {e}")
    return None


async def fetch_page_content(url: str) -> Optional[str]:
    """
    Katman 1: Crawl4AI  (lokal pod — JS render, temiz markdown)
    Katman 2: Direkt    (son çare — JS render yok)
    """
    if not url or not url.startswith(("http://","https://")):
        return None
    content = await fetch_via_crawl4ai(url)
    if content:
        return content
    return await fetch_via_direct(url)


# ═══════════════════════════════════════════════════════════════
# LLM SENTEZİ
# ═══════════════════════════════════════════════════════════════

def _score_result_relevance(query: str, result: dict) -> float:
    """Bir web sonucunun sorguyla alakasını 0-1 arası skorla."""
    q_words = set(query.lower().split())
    text    = (result.get("title","") + " " + result.get("content","")).lower()
    
    # Kelime örtüşmesi
    overlap = sum(1 for w in q_words if w in text and len(w) > 2)
    score   = overlap / (len(q_words) + 1)
    
    # Güvenilir domain boost
    url = result.get("url", "").lower()
    trusted = ["wikipedia","bbc","reuters","anadolu","ntv","cnn","goal.com",
               "transfermarkt","sofascore","flashscore","tff.org","uefa.com",
               "apple.com","microsoft.com","openai.com","anthropic.com",
               "techcrunch","theverge","wired","bloomberg","forbes"]
    if any(d in url for d in trusted):
        score += 0.3
    
    # Güncel içerik boost (2025, 2026 içeriyorsa)
    if "2025" in text or "2026" in text:
        score += 0.1
    
    # Placeholder ceza
    if "[" in text and "]" in text:
        score -= 0.5
        
    return min(score, 1.0)


def _format_results_directly(
    query: str,
    search_results: List[dict],
    page_contents: List[dict],
    language: str = "tr",
) -> str:
    """
    Web sonuçlarını akıllıca seç ve formatla.
    - Reranker'dan geçmiş top chunk'lar varsa onları kullan
    - Yoksa en alakalı arama sonuçlarını skorla, doğru olanı seç
    """
    lines = []

    # Sayfa içerikleri — bunlar zaten reranker'dan geçti, en iyi içerik
    if page_contents:
        best_pages = []
        for pc in page_contents:
            score = _score_result_relevance(query, {
                "title":   pc.get("title",""),
                "content": pc.get("content","")[:300],
                "url":     pc.get("url",""),
            })
            best_pages.append((score, pc))
        
        # Skora göre sırala, en iyi 2'yi al
        best_pages.sort(key=lambda x: x[0], reverse=True)
        for score, pc in best_pages[:2]:
            if score < 0.1:
                continue
            title   = pc.get("title", "")[:80]
            url     = pc.get("url", "")[:70]
            # İçeriğin en alakalı paragrafını bul
            content = pc.get("content", "")
            paragraphs = [p.strip() for p in content.split("\n") if len(p.strip()) > 80]
            q_words = set(query.lower().split())
            best_para = max(
                paragraphs[:10],
                key=lambda p: sum(1 for w in q_words if w in p.lower()),
                default=content[:400]
            )
            lines.append(f"**{title}**")
            lines.append(best_para[:500])
            lines.append(f"*Kaynak: {url}*")
            lines.append("")

    # Arama snippet'leri — skora göre filtrele
    if search_results:
        scored = []
        for r in search_results:
            score = _score_result_relevance(query, r)
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        
        # Sadece skor > 0.15 olanları göster, max 4
        good = [(s, r) for s, r in scored if s > 0.15][:4]
        
        if good:
            if lines:
                lines.append("---")
            for score, r in good:
                title   = r.get("title","")[:80]
                snippet = r.get("content","")[:250].strip()
                url     = r.get("url","")[:70]
                if snippet:
                    lines.append(f"• **{title}**")
                    lines.append(f"  {snippet}")
                    lines.append(f"  *{url}*")

    return "\n".join(lines) if lines else "Web kaynaklarında güncel bilgi bulunamadı."


def _has_hallucination(text: str) -> bool:
    """LLM placeholder veya uydurma içeriği tespit et."""
    hallucination_signs = [
        "[takım adı]", "[skor]", "[tarih]", "[rakip]",
        "[stad]", "[isim]", r"\[.*\]",
        "bilgi bulunamadı ama", "kesin söyleyemem ama",
        "tahminim", "muhtemelen",
    ]
    text_lower = text.lower()
    import re
    for sign in hallucination_signs:
        if "[" in sign and "]" in sign and re.search(sign, text):
            return True
        if sign in text_lower:
            return True
    return False


async def synthesize_with_llm(
    query:          str,
    search_results: List[dict],
    page_contents:  List[dict],
    language:       str = "tr",
    context_hint:   Optional[str] = None,
) -> str:
    """
    Güvenli sentez:
    1. LLM ile dene — ama sıkı kontrol et
    2. Hallucination tespit ederse → direkt web sonuçlarını döndür
    3. LLM yoksa → direkt web sonuçları
    """
    # Ham web verisi her zaman hazır
    raw_output = _format_results_directly(query, search_results, page_contents, language)

    if not DEEPINFRA_API_KEY:
        return raw_output

    # Sadece spesifik olgusal verileri içeren kısa snippetleri LLM'e ver
    # Sayfa içeriğinin sadece en alakalı ilk 300 kelimesini al
    best_content = ""
    if page_contents:
        best_content = page_contents[0].get("content", "")[:1500]
    
    snippets = "\n".join([
        f"KAYNAK {i+1}: {r.get('title','')}\n{r.get('content','')[:400]}"
        for i, r in enumerate(search_results[:4])
    ])

    lang_rule = "Türkçe yanıtla." if language == "tr" else "Respond in English."
    ctx = f" (Bağlam: {context_hint})" if context_hint else ""

    prompt = f"""{lang_rule}

Soru: {query}{ctx}

Aşağıdaki web kaynaklarından KOPYALAYARAK yanıtla.
Kendi bilgini EKLEME. Kaynaklarda yoksa "Bulunamadı" de.

{snippets}

{"SAYFA İÇERİĞİ:" + chr(10) + best_content if best_content else ""}

Kurallar:
- Sadece yukarıdaki kaynaklardan bilgi kullan
- Olgusal veri (skor, tarih, isim, fiyat) için KAYNAKTAN kopyala
- Kaynaklarda yoksa: "Web kaynaklarında bu bilgi bulunamadı" yaz
- [placeholder] içeren yanıt YAZMA
- 200 kelimeden kısa tut"""

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {DEEPINFRA_API_KEY}"},
                json={
                    "model":    SYNTHESIS_MODEL,
                    "messages": [
                        {"role": "system", "content":
                         "Sen bir web araştırma asistanısın. "
                         "SADECE sana verilen kaynaklardan bilgi kullanırsın. "
                         "Kaynaklarda olmayan hiçbir olgusal bilgiyi (skor, tarih, isim, fiyat) üretmezsin. "
                         "Eğer bilgi kaynaklarda yoksa 'Bulunamadı' dersin."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 500, "temperature": 0.0, "stream": False,
                })

        result = resp.json().get("choices",[{}])[0].get("message",{}).get("content","")

        if not result:
            print("[LLM] Boş yanıt → raw output")
            return raw_output

        # Hallucination kontrolü
        if _has_hallucination(result):
            print(f"[LLM] ⚠️ Hallucination tespit edildi → raw output")
            return raw_output

        print(f"[LLM] ✅ Sentez: {len(result)} chars")
        return result

    except Exception as e:
        print(f"[LLM] ❌ {e} → ham web sonuçları gösteriliyor")
        return raw_output


# ═══════════════════════════════════════════════════════════════
# REFLECTION — Cevabı LLM ile kontrol et ve iyileştir
# ═══════════════════════════════════════════════════════════════

async def reflect_and_improve(query: str, draft: str,
                               chunks: List[str], language: str = "tr") -> str:
    """
    Self-reflection: LLM kendi cevabını değerlendirir.
    Eksik, yanlış veya belirsiz kısım varsa düzeltir.
    API yoksa draft'ı aynen döndür.
    """
    if not DEEPINFRA_API_KEY or not draft:
        return draft

    lang_rule = "TÜRKÇE yanıtla." if language == "tr" else "Respond in ENGLISH."
    context   = "\n\n".join(chunks[:3]) if chunks else ""

    prompt = f"""Aşağıda bir kullanıcı sorusu ve bir taslak cevap var.
Taslak cevabı eleştirel gözle değerlendir:

SORU: {query}

TASLAK CEVAP:
{draft}

KAYNAK İÇERİK (doğrulama için):
{context[:2000] if context else "(kaynak yok)"}

DEĞERLENDİRME KRİTERLERİ:
1. Soru tam olarak yanıtlanmış mı?
2. Sayısal değerler (tarih, fiyat, oran) doğru mu?
3. Eksik önemli bilgi var mı?
4. Çelişkili bir ifade var mı?

GÖREV:
- Taslak yeterliyse: "ONAYLANDI" yaz, ardından taslağı aynen döndür.
- Eksik/yanlış varsa: Düzeltilmiş versiyonu yaz. "ONAYLANDI" yazma.
- {lang_rule}
- Maksimum 350 kelime."""

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": SYNTHESIS_MODEL,
                    "messages": [
                        {"role": "system",
                         "content": "Eleştirel düşünen, doğruluğa önem veren bir AI editörüsün."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 600, "temperature": 0.05, "stream": False,
                }
            )
            result = resp.json().get("choices",[{}])[0].get("message",{}).get("content","")
            if result:
                if result.startswith("ONAYLANDI"):
                    # Taslak onaylandı — temizle ve döndür
                    clean = result.replace("ONAYLANDI","").strip()
                    print(f"[REFLECT] ✅ Onaylandı ({len(draft)} chars)")
                    return clean if clean else draft
                else:
                    print(f"[REFLECT] ✅ İyileştirildi ({len(draft)} → {len(result)} chars)")
                    return result
    except Exception as e:
        print(f"[REFLECT] ❌ {e} — draft kullanılıyor")

    return draft


# ═══════════════════════════════════════════════════════════════
# DEEP SEARCH PIPELINE
# ═══════════════════════════════════════════════════════════════

async def deep_search_pipeline(req: DeepSearchRequest) -> Dict:
    """
    Agent-level Deep Search — Gemini benzeri çoklu kaynak
    1. SEARCH    — Paralel çoklu sorgu
    2. SCRAPE    — Crawl4AI ile paralel sayfa çekme
    3. CHUNK     — Sliding window
    4. RERANK    — BGE Reranker
    5. GENERATE  — Sadece web verisi kullan
    6. REFLECT   — Doğrulama
    """
    t0        = time.time()
    cache_key = f"ds_{req.query.lower().strip()}"
    cached    = await deep_cache.get(cache_key)
    if cached:
        print(f"[DEEP SEARCH] Cache hit")
        return cached

    print(f"\n{chr(9473)*60}\n[DEEP SEARCH] '{req.query}'\n{chr(9473)*60}")

    # ── 1. SEARCH — Paralel çoklu sorgu ─────────────────────
    search_queries = [req.query]
    is_tr = any(c in req.query for c in "çğışöüÇĞİŞÖÜ")
    if is_tr:
        search_queries.append(f"{req.query} son dakika güncel")
    else:
        search_queries.append(f"{req.query} latest 2025 2026")

    search_tasks = [async_web_search(q, req.num_results) for q in search_queries[:2]]
    search_results_list = await asyncio.gather(*search_tasks, return_exceptions=True)

    seen_urls = set()
    results   = []
    provider  = "unknown"
    for sr in search_results_list:
        if isinstance(sr, dict) and sr.get("success"):
            provider = sr.get("data", {}).get("provider", provider)
            for r in sr.get("data", {}).get("results", []):
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    results.append(r)

    if not results:
        return {"success": False, "error": "Arama başarısız", "query": req.query}
    print(f"[DEEP SEARCH] 1/6 SEARCH ✅ {len(results)} unique sonuç ({provider})")

    # ── 2. SCRAPE — Paralel sayfa çekme ─────────────────────
    page_contents = []
    fetch_count   = min(req.fetch_pages + 1, len(results), 5)
    if results and fetch_count > 0:
        urls    = [r.get("url","") for r in results[:fetch_count] if r.get("url")]
        fetched = await asyncio.gather(*[fetch_page_content(u) for u in urls], return_exceptions=True)
        for item, text in zip(results[:fetch_count], fetched):
            if isinstance(text, str) and len(text) > 200:
                page_contents.append({
                    "url":     item.get("url",""),
                    "title":   item.get("title",""),
                    "content": text,
                })
    print(f"[DEEP SEARCH] 2/6 SCRAPE ✅ {len(page_contents)}/{fetch_count} sayfa")

    # ── 3. CHUNK ─────────────────────────────────────────────
    all_chunks: List[str] = []
    for pc in page_contents:
        chunks = chunk_text(pc["content"], CHUNK_SIZE, CHUNK_OVERLAP)
        for c in chunks:
            all_chunks.append({
                "text":  f"[Kaynak: {pc['title'][:60]} | {pc['url'][:50]}]\n{c}",
                "title": pc.get("title",""),
                "url":   pc.get("url",""),
                "type":  "page",
            })
    for r in results[:req.num_results]:
        snippet = r.get("content","")
        if snippet and len(snippet) > 80:
            all_chunks.append({
                "text":  f"[Kaynak: {r.get('title','')[:60]}]\n{snippet}",
                "title": r.get("title",""),
                "url":   r.get("url",""),
                "type":  "snippet",
            })
    print(f"[DEEP SEARCH] 3/6 CHUNK ✅ {len(all_chunks)} chunk")

    # ── 4. RERANK ────────────────────────────────────────────
    top_chunks: List[dict] = []
    if all_chunks:
        # Reranker'a sadece text gönder
        texts = [c["text"] for c in all_chunks]
        top_k = min(TOP_CHUNKS + 2, len(texts))
        ranked_texts = await rerank_chunks(req.query, texts, top_k)
        
        # Sıralı textlere metadata'yı geri eşle
        text_to_meta = {c["text"]: c for c in all_chunks}
        for t in ranked_texts:
            meta = text_to_meta.get(t, {"text": t, "title":"", "url":"", "type":"unknown"})
            top_chunks.append(meta)

    print(f"[DEEP SEARCH] 4/6 RERANK ✅ {len(top_chunks)} chunk seçildi")

    # ── 5. GENERATE ──────────────────────────────────────────
    synthesis = None
    if req.synthesize:
        # Reranker'ın seçtiklerini kaynak bilgisiyle synthesis'e ver
        top_pc_reranked = []
        seen_urls = set()
        for chunk in top_chunks:
            url = chunk.get("url","")
            if url and url not in seen_urls:
                seen_urls.add(url)
                top_pc_reranked.append({
                    "title":   chunk.get("title",""),
                    "url":     url,
                    "content": chunk.get("text",""),
                })
        
        # top_pc yoksa page_contents kullan
        top_pc = top_pc_reranked if top_pc_reranked else page_contents
        
        # Synthesis'e reranked chunks'ları ver — ham results değil
        reranked_snippets = [
            {"title": c.get("title",""), "url": c.get("url",""),
             "content": c.get("text","")[:400]}
            for c in top_chunks
        ]
        synthesis = await synthesize_with_llm(
            req.query, reranked_snippets, top_pc, req.language, req.context_hint
        )
        print(f"[DEEP SEARCH] 5/6 GENERATE ✅ {len(synthesis or '')} chars")

    # ── 6. REFLECT — hallucination check zaten synthesis'te yapılıyor ──
    # reflect_and_improve kaldırıldı — ikinci LLM çağrısı daha fazla hallucination riski
    print(f"[DEEP SEARCH] 6/6 REFLECT ✅ (synthesis'te kontrol edildi)")

    elapsed = round(time.time() - t0, 2)
    print(f"[DEEP SEARCH] ✅ {elapsed}s | {len(results)} kaynak | {len(page_contents)} sayfa\n")

    result = {
        "success": True, "tool_used": "deep_search", "query": req.query, "provider": provider,
        "data": {
            "synthesis":       synthesis,
            "search_results":  [{"title": r.get("title",""), "url": r.get("url",""),
                                  "snippet": r.get("content","")[:200]} for r in results[:req.num_results]],
            "pages_fetched":   len(page_contents),
            "sources_count":   len(results),
            "elapsed_seconds": elapsed,
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
        "version": "7.1.0",
        "endpoints": {
            "POST /unified":     "Canlı veri — kur, hava, kripto, saat, haberler, web arama",
            "POST /deep_search": "Araştırma — web arama + Jina.ai + LLM sentezi",
            "POST /search":      "Direkt web arama",
            "GET  /fetch":       "Tek URL içeriği çek",
        },
        "page_fetching": {
            "layer_1": "Jina.ai Reader (aktif — ücretsiz, sınırsız)",
            "layer_2": f"Crawl4AI ({'aktif: '+CRAWL4AI_URL if CRAWL4AI_URL else 'hazır — CRAWL4AI_URL set edilince'})",
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
        "status":   "healthy",
        "version":  "7.1.0",
        "jina":     "active",
        "crawl4ai": CRAWL4AI_URL or "not_configured",
        "searxng":  SEARXNG_URL  or "not_configured",
    }


@app.post("/unified")
async def unified_endpoint(request: UnifiedRequest):
    """
    Ana endpoint — chat servisi bu endpointi çağırır.
    tool_type gönderilmezse auto_detect_tool() ile belirlenir.
    """
    query     = request.query
    tool_type = request.tool_type or auto_detect_tool(query)

    print(f"\n{'─'*50}")
    print(f"[UNIFIED] '{query}' → {tool_type}")
    print(f"{'─'*50}")

    try:
        if tool_type == ToolType.TIME:
            result = get_time()

        elif tool_type == ToolType.WEATHER:
            result = get_weather(query)

        elif tool_type == ToolType.CURRENCY:
            fc, tc = _extract_currency_pair(query)
            result = get_currency(fc, tc)

        elif tool_type == ToolType.NEWS:
            topic  = re.sub(r'\b(haber|haberleri|news|son|güncel)\b', '', query).strip()
            result = get_news(topic if len(topic) > 2 else None)

        elif tool_type == ToolType.CRYPTO:
            result = get_crypto(_extract_coin(query))

        elif tool_type == ToolType.PRICE_SEARCH:
            result = sync_web_search(query, 5)

        else:  # WEB_SEARCH
            result = sync_web_search(query, 5)

        result["tool_used"] = tool_type.value
        result["query"]     = query

        # Log
        data    = result.get("data", {})
        preview = (data.get("formatted") or data.get("short") or
                   f"{len(data.get('results', data.get('articles', [])))} items")
        print(f"[UNIFIED] ✅ success={result.get('success')} | {preview}")
        return result

    except Exception as e:
        print(f"[UNIFIED ERROR] {e}")
        return {"success": False, "error": str(e),
                "tool_used": tool_type.value, "query": query}


@app.post("/deep_search")
async def deep_search_endpoint(request: DeepSearchRequest):
    """Gateway bu endpointi çağırır — araştırma sorgular için."""
    return await deep_search_pipeline(request)


@app.post("/search")
async def search_endpoint(request: WebSearchRequest):
    """Direkt web arama."""
    r = sync_web_search(request.query, request.num_results)
    r["tool_used"] = "web_search"
    return r


@app.get("/fetch")
async def fetch_endpoint(url: str):
    """Tek URL içeriği çek — test için. Örnek: GET /fetch?url=https://example.com"""
    if not url.startswith(("http://","https://")):
        return {"success": False, "error": "Geçersiz URL"}
    content = await fetch_page_content(url)
    if content:
        return {"success": True, "url": url,
                "char_count": len(content), "content": content}
    return {"success": False, "url": url, "error": "İçerik alınamadı"}


# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*70)
    print("SKYLIGHT SMART TOOLS — v7.1")
    print("="*70)
    print(f"Search:  SearXNG({'ON' if SEARXNG_URL else 'OFF'}) | "
          f"Brave({'ON' if BRAVE_API_KEY else 'OFF'}) | DDG(ON)")
    print(f"Pages:   Jina.ai(ON) | Crawl4AI({'ON' if CRAWL4AI_URL else 'HAZIR'})")
    print(f"LLM:     {'ON — '+SYNTHESIS_MODEL if DEEPINFRA_API_KEY else 'OFF'}")
    print("="*70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8081, workers=4)