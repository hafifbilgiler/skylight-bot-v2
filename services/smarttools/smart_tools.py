"""
═══════════════════════════════════════════════════════════════
SKYLIGHT SMART TOOLS SERVICE — v7.0 (LiveDataRouter)
═══════════════════════════════════════════════════════════════
v6.1 → v7.0: Sistematik canlı veri yönlendirme

TEK KAYNAK İLKESİ:
  Tüm "bu sorgu nereye gider?" kararları SADECE buradadır.
  Chat servisi ve gateway bu servisi sorgular — kendi keyword
  listelerini tutmazlar.

LiveDataRouter — Sorgu Kategorileri:
  ┌─────────────────────────────────────────────────────────┐
  │ LIVE_UTILITY  → Anlık API (kur, hava, kripto, saat)    │
  │ LIVE_NEWS     → Haber RSS (gerçek başlıklar)           │
  │ LIVE_SEARCH   → Deep Search (araştırma + Jina + LLM)   │
  │ STATIC        → LLM kendi bilgisi yeterli              │
  │ TECHNICAL     → Kod/IT modu, LLM direkt               │
  └─────────────────────────────────────────────────────────┘

/classify endpoint:
  Her servis buraya sorar, karar burada verilir.
  Yeni konu eklemek = tek yerde bir satır.

SAYFA ÇEKME (Deep Search için):
  Katman 1: Jina.ai Reader (ücretsiz, sınırsız, JS render)
  Katman 2: Crawl4AI (CRAWL4AI_URL set edilince devreye girer)
  Katman 3: Direkt scrape (son çare)

ARAMA (waterfall):
  SearXNG → Brave → DuckDuckGo
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
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
from enum import Enum

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Skylight Smart Tools",
    description="LiveDataRouter + Deep Search (Jina.ai + LLM)",
    version="7.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

SEARXNG_URL        = os.getenv("SEARXNG_URL",        None)
BRAVE_API_KEY      = os.getenv("BRAVE_API_KEY",      None)
CRAWL4AI_URL       = os.getenv("CRAWL4AI_URL",       None)
JINA_BASE_URL      = "https://r.jina.ai"
DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY",  "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
SYNTHESIS_MODEL    = os.getenv(
    "SYNTHESIS_MODEL",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
)

MAX_PAGE_CHARS    = 3000
MAX_PAGES         = 3
JINA_TIMEOUT      = 12
CRAWL4AI_TIMEOUT  = 15
DIRECT_TIMEOUT    = 8

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


weather_cache  = SimpleCache(ttl=300)
currency_cache = SimpleCache(ttl=120)
search_cache   = SimpleCache(ttl=180)
deep_cache     = AsyncCache(ttl=300)

# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

class DataCategory(str, Enum):
    """
    Sorgu kategorileri — LiveDataRouter tarafından belirlenir.
    Tüm servisler bu enum'u kullanır.
    """
    LIVE_UTILITY  = "live_utility"   # Anlık API: kur, hava, kripto, saat, fiyat
    LIVE_NEWS     = "live_news"      # Haber RSS: son haberler, gündem
    LIVE_SEARCH   = "live_search"    # Deep Search: araştırma, analiz, güncel olaylar
    STATIC        = "static"         # LLM yeterli: genel bilgi, sabit konular
    TECHNICAL     = "technical"      # Kod/IT: programlama, DevOps


class ToolType(str, Enum):
    TIME              = "time"
    WEATHER           = "weather"
    NEWS              = "news"
    CURRENCY          = "currency"
    CRYPTO            = "crypto"
    PRICE_SEARCH      = "price_search"
    WEB_SEARCH        = "web_search"
    DEEP_SEARCH       = "deep_search"


class ClassifyRequest(BaseModel):
    query:   str
    mode:    str  = "assistant"  # chat mode: assistant, code, it_expert, student, social


class ClassifyResponse(BaseModel):
    category:      DataCategory
    tool:          Optional[ToolType] = None   # LIVE_UTILITY için hangi tool
    confidence:    float = 1.0
    reason:        str   = ""
    action:        str   = ""   # chat servisine ne yapması gerektiği


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
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LIVE DATA ROUTER — TEK KAYNAK, TÜM SERVİSLER BUNA SORAR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

class LiveDataRouter:
    """
    Sorgu → Kategori + Tool eşleştirmesi.

    KURAL: Her keyword sadece BİR kategoride olur.
    KURAL: Chat servisi ve gateway bu sınıfı kullanır, kendi listeleri olmaz.
    KURAL: Yeni konu eklemek = ilgili listeye bir kelime eklemek.

    Öncelik sırası (üstteki önce kontrol edilir):
      1. TECHNICAL  — kod/IT modu ise direkt
      2. LIVE_UTILITY — kur, hava, kripto, saat (API'den anlık)
      3. LIVE_NEWS    — haberler (RSS'den gerçek)
      4. LIVE_SEARCH  — araştırma (Deep Search pipeline)
      5. STATIC       — LLM kendi bilgisi yeterli
    """

    # ── TECHNICAL — kod/IT mod veya teknik sorular ──────────────
    TECHNICAL_MODES = {"code", "it_expert"}

    TECHNICAL_KEYWORDS = (
        "nasıl kullanılır", "syntax nedir", "kod yaz", "örnek ver",
        "açıkla", "ne demek", "tanımı nedir",
        "python", "javascript", "typescript", "golang", "rust",
        "docker", "kubernetes", "terraform", "ansible",
        "nasıl yapılır", "tutorial", "döküman",
        "fonksiyon yaz", "class yaz", "script yaz",
    )

    # ── LIVE_UTILITY — Anlık API sorguları ──────────────────────
    # Bu sorgular smart tools'un CURRENCY/WEATHER/CRYPTO/TIME tool'larına gider
    # Asla deep search'e gitmez — API'den direkt, 0.1 saniye

    CURRENCY_SIGNALS = (
        "dolar", "euro", "eur", "usd", "gbp", "sterlin", "pound",
        "jpy", "yen", "chf", "frank", "cad",
        "kur", "döviz", "doviz", "exchange rate",
        "kaç tl", "kac tl", "tl kaç", "kaç dolar", "kaç euro",
        "dolar kaç", "euro kaç", "kur nedir", "döviz kuru",
    )

    WEATHER_SIGNALS = (
        "hava durumu", "havadurumu", "hava nasıl", "havalar nasıl",
        "hava kaç derece", "sıcaklık kaç", "sicaklik",
        "yağmur yağıyor mu", "kar yağıyor mu",
        "weather", "temperature", "forecast",
        "bugün hava", "yarın hava",
        "derece", "nem oranı", "rüzgar hızı",
    )

    CRYPTO_SIGNALS = (
        "bitcoin", "btc", "ethereum", "eth",
        "dogecoin", "doge", "solana", "sol",
        "kripto", "crypto", "coin fiyat", "altcoin",
        "bitcoin kaç", "ethereum kaç", "btc usd",
    )

    TIME_SIGNALS = (
        "saat kaç", "saat kac", "saati kaç", "şimdi saat",
        "şu an saat", "günün saati", "what time is it",
        "tarih ne", "bugün ne günü", "bugün hangi gün",
    )

    PRICE_SIGNALS = (
        "kaç lira", "fiyatı ne kadar", "fiyatı kaç", "ne kadar",
        "altın fiyatı", "petrol fiyatı", "gram altın",
        "borsa", "bist", "hisse fiyatı", "stock price",
    )

    # ── LIVE_NEWS — Haber RSS sorguları ─────────────────────────
    # Smart tools'un NEWS tool'una gider — Google News RSS, gerçek başlıklar

    NEWS_SIGNALS = (
        "son haberler", "güncel haberler", "bugünün haberleri",
        "son dakika", "breaking news", "haberleri göster",
        "haberleri getir", "haber var mı", "haber oku",
        "gündem ne", "gündeme ne geldi",
        "today's news", "latest news", "news today",
        "türkiye haberleri", "dünya haberleri",
        "ekonomi haberleri", "spor haberleri",
    )

    # ── LIVE_SEARCH — Deep Search pipeline ──────────────────────
    # SearXNG → Jina.ai → LLM sentezi — araştırma gerektiren sorgular

    RESEARCH_SIGNALS = (
        # Açık araştırma isteği
        "araştır", "araştırma yap", "analiz et", "incele",
        "hakkında bilgi ver", "bul bana", "find me",
        "webde ara", "internette ara", "google'la",
        "araştırır mısın", "bakabilir misin",
        # Güncel olaylar — haber değil araştırma
        "son gelişmeler", "son açıklamalar", "son rapor",
        "ne oldu", "neler oluyor", "dünyada neler var",
        "gündemde ne var", "dünya gündemi", "küresel gelişmeler",
        "güncel durum", "son durum",
        # Kişi/şirket araştırması
        "kimdir", "ne yaptı", "açıkladı mı", "duyurdu mu",
        # Spesifik konu araştırması
        "son çalışma", "yeni keşif", "yeni buluş",
        "latest developments", "what happened",
        "recent news about",
    )

    RESEARCH_TOPIC_PATTERNS = (
        # "X'in son haberleri" ama genel "son haberler" değil
        r'.{2,20} haberleri$',
        r'.{2,20} son (haber|gelişme|durum)',
        r'(2024|2025).{0,30}(nedir|ne oldu|nasıl)',
        r'.{2,20} (açıkladı|duyurdu|karar verdi)',
    )

    # ── HARMFUL — engellenenler ──────────────────────────────────
    HARMFUL = (
        "bomba yap", "patlayıcı yap", "silah yap", "uyuşturucu yap",
        "bomb make", "drug make", "weapon make",
        "hack into", "ddos saldır", "malware yaz",
        "çocuk istismar", "child abuse",
        "nasıl öldürülür", "how to kill",
    )

    @classmethod
    def classify(cls, query: str, mode: str = "assistant") -> ClassifyResponse:
        """
        Sorguyu kategorize et.

        Bu metod tüm sistemin beynidir. Chat servisi ve gateway
        başka hiçbir keyword listesi tutmaz — sadece bunu çağırır.
        """
        q = query.lower().strip()

        # ── 0. Zararlı içerik ────────────────────────────────────
        if any(h in q for h in cls.HARMFUL):
            return ClassifyResponse(
                category=DataCategory.STATIC,
                reason="harmful_blocked",
                action="block",
            )

        # ── 1. TECHNICAL — kod/IT mod ────────────────────────────
        if mode in cls.TECHNICAL_MODES:
            return ClassifyResponse(
                category=DataCategory.TECHNICAL,
                reason=f"technical_mode:{mode}",
                action="llm_direct",
            )
        if any(kw in q for kw in cls.TECHNICAL_KEYWORDS) and len(q.split()) <= 7:
            return ClassifyResponse(
                category=DataCategory.TECHNICAL,
                reason="technical_keyword",
                action="llm_direct",
            )

        # ── 2. LIVE_UTILITY — anlık API ──────────────────────────
        # Döviz
        if any(s in q for s in cls.CURRENCY_SIGNALS):
            return ClassifyResponse(
                category=DataCategory.LIVE_UTILITY,
                tool=ToolType.CURRENCY,
                reason="currency_signal",
                action="smart_tools_api",
            )
        # Hava
        if any(s in q for s in cls.WEATHER_SIGNALS):
            return ClassifyResponse(
                category=DataCategory.LIVE_UTILITY,
                tool=ToolType.WEATHER,
                reason="weather_signal",
                action="smart_tools_api",
            )
        # Kripto
        if any(s in q for s in cls.CRYPTO_SIGNALS):
            return ClassifyResponse(
                category=DataCategory.LIVE_UTILITY,
                tool=ToolType.CRYPTO,
                reason="crypto_signal",
                action="smart_tools_api",
            )
        # Saat
        if any(s in q for s in cls.TIME_SIGNALS):
            return ClassifyResponse(
                category=DataCategory.LIVE_UTILITY,
                tool=ToolType.TIME,
                reason="time_signal",
                action="smart_tools_api",
            )
        # Fiyat
        if any(s in q for s in cls.PRICE_SIGNALS):
            return ClassifyResponse(
                category=DataCategory.LIVE_UTILITY,
                tool=ToolType.PRICE_SEARCH,
                reason="price_signal",
                action="smart_tools_api",
            )

        # ── 3. LIVE_NEWS — haber RSS ─────────────────────────────
        if any(s in q for s in cls.NEWS_SIGNALS):
            return ClassifyResponse(
                category=DataCategory.LIVE_NEWS,
                tool=ToolType.NEWS,
                reason="news_signal",
                action="smart_tools_news",
            )

        # ── 4. LIVE_SEARCH — deep search ─────────────────────────
        # Açık araştırma isteği
        if any(s in q for s in cls.RESEARCH_SIGNALS):
            return ClassifyResponse(
                category=DataCategory.LIVE_SEARCH,
                tool=ToolType.DEEP_SEARCH,
                reason="research_signal",
                action="deep_search",
            )
        # Pattern tabanlı araştırma (regex)
        for pattern in cls.RESEARCH_TOPIC_PATTERNS:
            if re.search(pattern, q):
                return ClassifyResponse(
                    category=DataCategory.LIVE_SEARCH,
                    tool=ToolType.DEEP_SEARCH,
                    confidence=0.8,
                    reason=f"research_pattern:{pattern}",
                    action="deep_search",
                )

        # ── 5. STATIC — LLM yeterli ──────────────────────────────
        return ClassifyResponse(
            category=DataCategory.STATIC,
            reason="no_live_data_needed",
            action="llm_direct",
        )

    @classmethod
    def get_smart_tools_query(cls, query: str, tool: ToolType) -> str:
        """
        Smart tools'a gönderilecek sorguyu optimize et.
        Örneğin: "güncel euro kaç tl" → "EUR TRY"
        """
        q = query.lower()

        if tool == ToolType.WEATHER:
            # Şehir adını çıkar
            city = _extract_city(query)
            return f"{city} hava durumu" if city else query

        if tool == ToolType.CURRENCY:
            # Para birimlerini normalize et
            pairs = _extract_currency_pair(query)
            return f"{pairs[0]} {pairs[1]} kuru"

        if tool == ToolType.CRYPTO:
            # Coin adını normalize et
            coin = _extract_coin(query)
            return f"{coin} fiyatı"

        return query


# ═══════════════════════════════════════════════════════════════
# QUERY EXTRACTION HELPERS
# ═══════════════════════════════════════════════════════════════

def _extract_city(query: str) -> Optional[str]:
    """Sorgudaki şehir adını çıkar."""
    noise = {
        'hava', 'durumu', 'havadurumu', 'weather', 'sıcaklık',
        'bugün', 'nasıl', 'nedir', 'kaç', 'derece', 'için',
        'de', 'da', 'şuan', 'şimdi', 'ne', 'nerede', 'olan',
    }
    words = re.findall(r'\b[a-zA-ZçğıöşüÇĞİÖŞÜ]+\b', query)
    clean = [w for w in words if w.lower() not in noise and len(w) > 2]
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
                 "kuala lumpur","buenos aires","cape town","tel aviv"}
        if two in multi:
            return two.title()
    return first.title()


def _extract_currency_pair(query: str) -> tuple:
    q = query.lower()
    cmap = {
        "dolar":"USD", "usd":"USD", "euro":"EUR", "eur":"EUR",
        "pound":"GBP", "sterlin":"GBP", "gbp":"GBP",
        "tl":"TRY", "try":"TRY", "lira":"TRY",
        "yen":"JPY", "frank":"CHF",
    }
    found = [v for k, v in cmap.items() if k in q]
    return (found[0] if found else "USD", found[1] if len(found) > 1 else "TRY")


def _extract_coin(query: str) -> str:
    q = query.lower()
    if "ethereum" in q or " eth" in q: return "ethereum"
    if "dogecoin" in q or "doge" in q: return "dogecoin"
    if "solana" in q or " sol " in q:  return "solana"
    return "bitcoin"


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
# LIVE UTILITY TOOL FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def get_current_time(tz_str: str = "Europe/Istanbul") -> Dict:
    try:
        tz  = pytz.timezone(tz_str)
        now = datetime.now(tz)
        days = {"Monday":"Pazartesi","Tuesday":"Salı","Wednesday":"Çarşamba",
                "Thursday":"Perşembe","Friday":"Cuma",
                "Saturday":"Cumartesi","Sunday":"Pazar"}
        months = {"January":"Ocak","February":"Şubat","March":"Mart","April":"Nisan",
                  "May":"Mayıs","June":"Haziran","July":"Temmuz","August":"Ağustos",
                  "September":"Eylül","October":"Ekim","November":"Kasım","December":"Aralık"}
        day_tr   = days.get(now.strftime("%A"),   now.strftime("%A"))
        month_tr = months.get(now.strftime("%B"), now.strftime("%B"))
        return {"success": True, "data": {
            "time": now.strftime("%H:%M:%S"), "date": now.strftime("%Y-%m-%d"),
            "day_tr": day_tr, "month_tr": month_tr,
            "formatted_tr": f"{now.day} {month_tr} {now.year}, {day_tr}, saat {now.strftime('%H:%M')}",
            "short": f"Saat {now.strftime('%H:%M')}, {day_tr}",
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
            "city": city_name, "country": country,
            "temperature":  round(c["temperature_2m"],     1),
            "feels_like":   round(c["apparent_temperature"],1),
            "humidity":     c["relative_humidity_2m"],
            "description":  desc,
            "wind_speed":   round(c.get("wind_speed_10m", 0), 1),
            "formatted":    f"{city_name}, {country}: {round(c['temperature_2m'])}°C, {desc}",
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
    # Method 2: Google Finance
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
            {"title":     i.title.text  if i.title  else "",
             "link":      i.link.text   if i.link   else "",
             "published": i.pubDate.text if i.pubDate else ""}
            for i in soup.find_all('item', limit=5)
        ]
        if articles:
            return {"success": True, "data": {"articles": articles}}
        return {"success": False, "error": "Haber bulunamadı"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_live_utility(tool: ToolType, query: str) -> Dict:
    """
    LIVE_UTILITY sorgularını çalıştırır.
    Tüm tool dispatch buradan yapılır.
    """
    if tool == ToolType.WEATHER:
        return get_weather(query)
    if tool == ToolType.CURRENCY:
        fc, tc = _extract_currency_pair(query)
        return get_currency(fc, tc)
    if tool == ToolType.CRYPTO:
        return get_crypto(_extract_coin(query))
    if tool == ToolType.TIME:
        return get_current_time()
    if tool == ToolType.PRICE_SEARCH:
        # Fiyat sorguları web search ile
        return sync_web_search(query, 3)
    if tool == ToolType.NEWS:
        topic = re.sub(r'\b(haber|haberleri|news)\b', '', query).strip()
        return get_news(topic if len(topic) > 2 else None)
    return {"success": False, "error": f"Bilinmeyen tool: {tool}"}


# ═══════════════════════════════════════════════════════════════
# SYNC WEB SEARCH
# ═══════════════════════════════════════════════════════════════

def sync_web_search(query: str, num: int = 5) -> Dict:
    """Sync waterfall: SearXNG → Brave → DuckDuckGo"""
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
# ASYNC DEEP SEARCH ENGINE
# ═══════════════════════════════════════════════════════════════

async def async_web_search(query: str, num: int = 5) -> Dict:
    """Async waterfall: SearXNG → Brave → DuckDuckGo"""
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


async def fetch_via_jina(url: str) -> Optional[str]:
    """Jina.ai Reader — ücretsiz, sınırsız, JS render, temiz markdown."""
    try:
        async with httpx.AsyncClient(timeout=JINA_TIMEOUT) as client:
            resp = await client.get(
                f"{JINA_BASE_URL}/{url}",
                headers={"Accept": "text/plain", "X-Return-Format": "markdown", "X-Timeout": "8"},
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
    """Crawl4AI — local pod. CRAWL4AI_URL set edilince aktif."""
    if not CRAWL4AI_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=CRAWL4AI_TIMEOUT) as client:
            resp = await client.post(f"{CRAWL4AI_URL}/crawl",
                json={"urls": [url], "priority": 8,
                      "crawler_params": {"headless": True, "word_count_threshold": 50}})
            if resp.status_code != 200:
                return None
            results = resp.json().get("results", [])
            if results and results[0].get("success"):
                md = results[0].get("markdown", "") or results[0].get("extracted_content", "")
                if md and len(md) > 100:
                    print(f"[CRAWL4AI] ✅ {url[:60]} → {len(md)} chars")
                    return md[:MAX_PAGE_CHARS]
    except Exception as e:
        print(f"[CRAWL4AI] ❌ {url[:60]}: {e}")
    return None


async def fetch_via_direct(url: str) -> Optional[str]:
    """Direkt httpx scrape — son çare."""
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
    Üç katmanlı sayfa çekme:
    1. Jina.ai   (aktif — ücretsiz, sınırsız)
    2. Crawl4AI  (CRAWL4AI_URL gelince devreye girer)
    3. Direkt    (son çare)
    """
    if not url or not url.startswith(("http://","https://")):
        return None
    content = await fetch_via_jina(url)
    if content:
        return content
    if CRAWL4AI_URL:
        content = await fetch_via_crawl4ai(url)
        if content:
            return content
    return await fetch_via_direct(url)


async def synthesize_with_llm(
    query:          str,
    search_results: List[dict],
    page_contents:  List[dict],
    language:       str = "tr",
    context_hint:   Optional[str] = None,
) -> str:
    """Ham sonuçları LLM ile sentezler."""
    if not DEEPINFRA_API_KEY:
        return "\n\n".join([
            f"**{r.get('title','')}**\n{r.get('content','')[:300]}"
            for r in search_results[:4]
        ])

    search_txt = "\n".join([
        f"[{i+1}] {r.get('title','')}\n    {r.get('content','')[:400]}"
        for i, r in enumerate(search_results[:5])
    ])
    page_txt = "\n\n".join([
        f"━━ {pc.get('title', pc.get('url','?'))} ━━\n{pc['content']}"
        for pc in page_contents if pc.get("content")
    ]) if page_contents else ""

    lang_rule = "YANITI TÜRKÇE yaz." if language == "tr" else "Respond in ENGLISH."
    ctx       = f"\nBağlam: {context_hint}" if context_hint else ""

    prompt = f"""Kullanıcı: "{query}"{ctx}

ARAMA SONUÇLARI:
{search_txt}

SAYFA İÇERİKLERİ:
{page_txt if page_txt else "(sayfa içeriği alınamadı — özet kullanılıyor)"}

Kurallar:
1. {lang_rule}
2. Doğrudan başla, giriş cümlesi yok
3. Gerçek veriler: tarih, sayı, isim varsa ekle
4. Kaynak belirt: "X'e göre..." veya "(Kaynak: Y)"
5. Maksimum 350 kelime
6. SADECE verilen web verilerini kullan"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {DEEPINFRA_API_KEY}"},
                json={"model": SYNTHESIS_MODEL,
                      "messages": [
                          {"role": "system", "content":
                           "Web arama sonuçlarını analiz eden, net ve kaynaklı yanıtlar sunan asistansın."},
                          {"role": "user", "content": prompt}
                      ],
                      "max_tokens": 800, "temperature": 0.2, "stream": False})
            content = resp.json().get("choices",[{}])[0].get("message",{}).get("content","")
            if content:
                print(f"[LLM] ✅ Sentez: {len(content)} chars")
                return content
    except Exception as e:
        print(f"[LLM] ❌ {e}")

    return "\n\n".join([
        f"**{r.get('title','')}**\n{r.get('content','')[:250]}"
        for r in search_results[:4]
    ])


async def deep_search_pipeline(req: DeepSearchRequest) -> Dict:
    """
    LIVE_SEARCH pipeline:
    1. Arama (SearXNG/Brave/DDG)
    2. Paralel sayfa çekme (Jina.ai)
    3. LLM sentezi
    """
    t0        = time.time()
    cache_key = f"ds_{req.query.lower().strip()}_{req.num_results}_{req.fetch_pages}"
    cached    = await deep_cache.get(cache_key)
    if cached:
        return cached

    print(f"\n{'━'*60}")
    print(f"[DEEP SEARCH] '{req.query}'")
    print(f"{'━'*60}")

    # Adım 1: Arama
    sr = await async_web_search(req.query, req.num_results)
    if not sr.get("success"):
        return {"success": False, "error": "Arama başarısız",
                "query": req.query, "tool_used": "deep_search"}

    results  = sr.get("data", {}).get("results", [])
    provider = sr.get("data", {}).get("provider", "?")
    print(f"[DEEP SEARCH] Adım 1 ✅ {len(results)} sonuç ({provider})")

    # Adım 2: Paralel sayfa çekme
    page_contents = []
    if results and req.fetch_pages > 0:
        urls    = [r.get("url","") for r in results[:req.fetch_pages] if r.get("url")]
        fetched = await asyncio.gather(*[fetch_page_content(u) for u in urls],
                                       return_exceptions=True)
        for item, text in zip(results[:req.fetch_pages], fetched):
            if isinstance(text, str) and text:
                page_contents.append({"url": item.get("url",""),
                                      "title": item.get("title",""), "content": text})
                print(f"[DEEP SEARCH]   ✅ {item.get('title','')[:50]} → {len(text)} chars")
    print(f"[DEEP SEARCH] Adım 2 ✅ {len(page_contents)}/{req.fetch_pages} sayfa")

    # Adım 3: LLM sentezi
    synthesis = None
    if req.synthesize:
        synthesis = await synthesize_with_llm(
            req.query, results, page_contents, req.language, req.context_hint)
        print(f"[DEEP SEARCH] Adım 3 ✅ {len(synthesis or '')} chars")

    elapsed = round(time.time() - t0, 2)
    print(f"[DEEP SEARCH] Toplam: {elapsed}s\n")

    result = {
        "success": True, "tool_used": "deep_search",
        "query": req.query, "provider": provider,
        "data": {
            "synthesis":      synthesis,
            "search_results": [{"title": r.get("title",""), "url": r.get("url",""),
                                 "snippet": r.get("content","")[:200]}
                                for r in results[:req.num_results]],
            "pages_fetched":  len(page_contents),
            "elapsed_seconds": elapsed,
        },
    }
    await deep_cache.set(cache_key, result)
    return result


# ═══════════════════════════════════════════════════════════════
# FORMAT HELPERS — Çıktıyı LLM'e hazırla
# ═══════════════════════════════════════════════════════════════

def format_for_llm(tool_used: str, data: dict) -> str:
    """
    Smart tools çıktısını LLM'e beslenecek formata dönüştürür.
    Chat servisi bu metodu kullanır — kendi format kodu yazmaz.
    """
    parts = [f"[Canlı Veri — {tool_used.upper()}]"]

    if tool_used == "weather":
        parts.append(
            f"📍 {data.get('city')}, {data.get('country')}\n"
            f"🌡️ {data.get('temperature')}°C (hissedilen {data.get('feels_like')}°C)\n"
            f"☁️ {data.get('description')}\n"
            f"💧 Nem: %{data.get('humidity')} | 💨 Rüzgar: {data.get('wind_speed')} km/h"
        )

    elif tool_used == "currency":
        parts.append(f"💱 {data.get('formatted')}")

    elif tool_used == "crypto":
        parts.append(
            f"₿ {data.get('formatted')}\n"
            f"📈 24 saatlik değişim: {data.get('change_24h'):+.2f}%"
        )

    elif tool_used == "time":
        parts.append(f"🕐 {data.get('formatted_tr')}")

    elif tool_used == "news":
        articles = data.get("articles", [])[:5]
        if articles:
            parts.append("📰 Son Haberler:")
            for i, a in enumerate(articles, 1):
                parts.append(f"  {i}. {a.get('title','')}")

    elif tool_used == "deep_search":
        synthesis = data.get("synthesis", "")
        if synthesis:
            parts.append(synthesis)
        else:
            for r in data.get("search_results", [])[:3]:
                parts.append(f"• {r.get('title','')}: {r.get('snippet','')[:200]}")

    elif tool_used in ("web_search", "price_search"):
        results = data.get("results", [])[:3]
        for r in results:
            parts.append(f"• {r.get('title','')}: {r.get('content','')[:200]}")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "service": "Skylight Smart Tools",
        "version": "7.0.0",
        "architecture": "LiveDataRouter — tek kaynak, tüm servisler buna sorar",
        "categories": {
            "LIVE_UTILITY":  "Anlık API: kur, hava, kripto, saat, fiyat",
            "LIVE_NEWS":     "Haber RSS: son haberler, gündem",
            "LIVE_SEARCH":   "Deep Search: araştırma + Jina.ai + LLM",
            "STATIC":        "LLM kendi bilgisi yeterli",
            "TECHNICAL":     "Kod/IT modu",
        },
        "page_fetching": {
            "layer_1": "Jina.ai Reader (aktif — ücretsiz, sınırsız)",
            "layer_2": f"Crawl4AI ({'aktif: '+CRAWL4AI_URL if CRAWL4AI_URL else 'hazır — CRAWL4AI_URL set edilince'})",
            "layer_3": "Direkt scrape (son çare)",
        },
        "providers": {
            "searxng": bool(SEARXNG_URL),
            "brave":   bool(BRAVE_API_KEY),
            "ddg":     True,
        },
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "version": "7.0.0",
            "jina": "active",
            "crawl4ai": CRAWL4AI_URL or "not_configured",
            "searxng":  SEARXNG_URL  or "not_configured"}


@app.post("/classify")
async def classify_endpoint(request: ClassifyRequest):
    """
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ANA KARAR ENDPOINT — Chat servisi ve gateway bunu çağırır

    Chat servisi ve gateway bu endpointe sorguyu gönderir,
    ne yapacağını öğrenir. Kendi keyword listesi tutmaz.

    Request:
    { "query": "güncel euro kaç tl", "mode": "assistant" }

    Response:
    {
        "category":   "live_utility",
        "tool":       "currency",
        "action":     "smart_tools_api",
        "reason":     "currency_signal",
        "confidence": 1.0
    }
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    result = LiveDataRouter.classify(request.query, request.mode)
    print(f"[CLASSIFY] '{request.query}' → {result.category} / {result.tool} / {result.action}")
    return result


@app.post("/live")
async def live_endpoint(request: UnifiedRequest):
    """
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    LIVE DATA ENDPOINT — Canlı veri + format

    Chat servisi bu endpointi çağırır.
    Sınıflandırma + çalıştırma + LLM'e hazır format döner.

    Request:  { "query": "istanbul hava durumu" }

    Response:
    {
        "success": true,
        "category": "live_utility",
        "tool_used": "weather",
        "data": { ... },
        "formatted": "[Canlı Veri — WEATHER]\n📍 İstanbul..."
    }
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    query      = request.query
    classified = LiveDataRouter.classify(query)
    category   = classified.category
    tool       = classified.tool

    print(f"[LIVE] '{query}' → {category} / {tool}")

    # STATIC veya TECHNICAL → canlı veri yok
    if category in (DataCategory.STATIC, DataCategory.TECHNICAL):
        return {"success": False, "category": category,
                "reason": "no_live_data_needed", "query": query}

    # LIVE_NEWS
    if category == DataCategory.LIVE_NEWS:
        topic  = re.sub(r'\b(son|güncel|bugünün|haberleri|haberler|news)\b', '', query).strip()
        result = get_news(topic if len(topic) > 2 else None)
        if result.get("success"):
            result["formatted"] = format_for_llm("news", result.get("data", {}))
        result["category"] = category
        return result

    # LIVE_SEARCH → Deep Search pipeline
    if category == DataCategory.LIVE_SEARCH:
        result = await deep_search_pipeline(DeepSearchRequest(
            query=query, num_results=5, fetch_pages=3, synthesize=True, language="tr"))
        if result.get("success"):
            result["formatted"] = format_for_llm("deep_search", result.get("data", {}))
        result["category"] = category
        return result

    # LIVE_UTILITY → Anlık API
    if category == DataCategory.LIVE_UTILITY and tool:
        opt_query = LiveDataRouter.get_smart_tools_query(query, tool)
        result    = run_live_utility(tool, opt_query)
        if result.get("success"):
            result["formatted"] = format_for_llm(tool.value, result.get("data", {}))
        result["category"] = category
        return result

    return {"success": False, "category": category,
            "reason": "unhandled", "query": query}


@app.post("/unified")
async def unified_endpoint(request: UnifiedRequest):
    """Eski uyumluluk — /live ile aynı."""
    return await live_endpoint(request)


@app.post("/deep_search")
async def deep_search_endpoint(request: DeepSearchRequest):
    """Gateway ve /live tarafından çağrılır."""
    return await deep_search_pipeline(request)


@app.post("/search")
async def search_endpoint(request: WebSearchRequest):
    """Direkt web arama."""
    r = sync_web_search(request.query, request.num_results)
    r["tool_used"] = "web_search"
    return r


@app.get("/fetch")
async def fetch_endpoint(url: str):
    """Tek URL içeriği çek — test için."""
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
    print("SKYLIGHT SMART TOOLS — v7.0 (LiveDataRouter)")
    print("="*70)
    print(f"Search:  SearXNG({'ON' if SEARXNG_URL else 'OFF'}) | "
          f"Brave({'ON' if BRAVE_API_KEY else 'OFF'}) | DDG(ON)")
    print(f"Pages:   Jina.ai(ON) | "
          f"Crawl4AI({'ON' if CRAWL4AI_URL else 'HAZIR'})")
    print(f"LLM:     {'ON — '+SYNTHESIS_MODEL if DEEPINFRA_API_KEY else 'OFF'}")
    print("="*70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8081, workers=4)