"""
╔══════════════════════════════════════════════════════════════╗
║  ONE-BUNE  ·  Web Search Engine  v4.1                       ║
║  AI-powered keyword generation + multi-source retrieval     ║
╚══════════════════════════════════════════════════════════════╝

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  AI Keyword Generator  →  Parallel Search  →  Dedupe   │
  │  Quality Score         →  Rank             →  Return   │
  └─────────────────────────────────────────────────────────┘

Keyword Generator:
  Kullanıcının ham mesajından optimize edilmiş 2-3 sorgu üretir.
  Örnek:
    "elektrik faturamı nasıl azaltabilirim?"
    → ["elektrik faturası azaltma yöntemleri 2025",
       "evde enerji tasarrufu önerileri",
       "elektrik tasarrufu pratik ipuçları"]

Sources (waterfall):
  1. SearXNG  — bing + google + brave engines
  2. Bing scrape  via Crawl4AI
  3. Google scrape via Crawl4AI
  4. DDG HTML — rate-limited last resort

FIX v4.1:
  - Waterfall tip uyumsuzluğu düzeltildi (list[SearchResult] doğru işleniyor)
  - AI keyword generator eklendi (DeepInfra LLM)
  - Keyword heuristic fallback (LLM yoksa)
  - format_sources_for_user() → kullanıcıya link gösterimi
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx
import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────
SEARXNG_URL    = os.getenv("SEARXNG_URL")
CRAWL4AI_URL   = os.getenv("CRAWL4AI_URL",    "http://crawl4ai:11235")
CRAWL4AI_TOKEN = os.getenv("CRAWL4AI_TOKEN",  "skylight-crawl4ai-2026")
DEEPINFRA_URL  = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
DEEPINFRA_KEY  = os.getenv("DEEPINFRA_API_KEY", "")
KEYWORD_MODEL  = os.getenv("KEYWORD_MODEL",   "meta-llama/Meta-Llama-3.1-8B-Instruct")

SEARXNG_ENGINES = "bing,startpage"   # google=403 brave=429 ddg=parser error — bunlar kapalı

_SKIP_DOMAINS = frozenset({
    "youtube.com", "youtu.be", "twitter.com", "x.com",
    "instagram.com", "facebook.com", "tiktok.com", "reddit.com",
    "apps.apple.com", "play.google.com", "linkedin.com",
    "pinterest.com", "amazon.com", "ebay.com",
})

_TRUSTED_DOMAINS = frozenset({
    "wikipedia.org", "bbc.com", "reuters.com", "bloomberg.com",
    "anadoluajansi.com.tr", "aa.com.tr", "ntv.com.tr", "cnn.com",
    "sabah.com.tr", "hurriyet.com.tr", "milliyet.com.tr",
    "bloomberght.com", "haberturk.com", "sozcu.com.tr",
    "goal.com", "transfermarkt.com", "sofascore.com", "flashscore.com",
    "tff.org", "uefa.com", "fifa.com",
    "techcrunch.com", "theverge.com", "wired.com", "arstechnica.com",
    "github.com", "stackoverflow.com", "docs.python.org",
    "openai.com", "anthropic.com", "deepmind.com",
    "tuik.gov.tr", "tcmb.gov.tr", "resmigazete.gov.tr",
})


# ── Data model ────────────────────────────────────────────────
@dataclass
class SearchResult:
    title:   str
    url:     str
    snippet: str
    source:  str = ""
    score:   float = 0.0

    def to_dict(self) -> dict:
        return {
            "title":   self.title,
            "url":     self.url,
            "content": self.snippet,
            "score":   round(self.score, 3),
            "source":  self.source,
        }


# ── Cache ─────────────────────────────────────────────────────
_cache: dict[str, tuple[list[SearchResult], float]] = {}
_CACHE_TTL = 180  # 3 min


def _cache_key(query: str, lang: str) -> str:
    return hashlib.md5(f"{query.lower().strip()}:{lang}".encode()).hexdigest()


def _cache_get(key: str) -> Optional[list[SearchResult]]:
    if key in _cache:
        results, exp = _cache[key]
        if time.time() < exp:
            return results
        del _cache[key]
    return None


def _cache_set(key: str, results: list[SearchResult]) -> None:
    if len(_cache) > 500:
        now = time.time()
        expired = [k for k, (_, e) in list(_cache.items()) if now >= e]
        for k in expired:
            del _cache[k]
    _cache[key] = (results, time.time() + _CACHE_TTL)


# ── AI Keyword Generator ──────────────────────────────────────
_KW_SYSTEM = """\
Sen bir arama motoru uzmanısın. Kullanıcının mesajını analiz edip,
arama motorunda en iyi sonucu getirecek 2-3 adet optimize edilmiş arama sorgusu üretiyorsun.

KURALLAR:
- Her sorgu arama motoruna uygun kısa ve öz olsun (2-6 kelime ideal)
- Doğal dil yerine anahtar kelime formatı kullan
- Farklı açıları kapsayan çeşitli sorgular üret
- Güncellik gerektiriyorsa yıl ekle (2025 veya 2026)
- Türkçe soru → Türkçe sorgular, İngilizce soru → İngilizce sorgular
- Spesifik isimler/markalar/yerler varsa koru

SADECE JSON döndür, başka hiçbir şey yazma:
{"queries": ["sorgu1", "sorgu2", "sorgu3"]}

ÖRNEKLER:
Kullanıcı: "elektrik faturamı nasıl azaltabilirim"
{"queries": ["elektrik faturası azaltma yöntemleri 2025", "evde enerji tasarrufu önerileri", "elektrik tasarrufu ipuçları"]}

Kullanıcı: "Galatasaray son maç skoru"
{"queries": ["Galatasaray son maç sonucu 2025", "Galatasaray maç skoru bugün"]}

Kullanıcı: "best python web framework 2025"
{"queries": ["best python web framework 2025", "fastapi vs django vs flask comparison", "python backend framework benchmarks 2025"]}

Kullanıcı: "yapay zeka nedir"
{"queries": ["yapay zeka nedir açıklama", "artificial intelligence temel kavramlar Türkçe"]}
"""


async def generate_search_queries(
    user_message: str,
    language: str = "tr",
    max_queries: int = 3,
) -> list[str]:
    """
    Kullanıcının ham mesajından optimize edilmiş arama sorguları üretir.
    LLM yoksa veya hata olursa heuristic fallback kullanır.
    Her zaman en az 1 sorgu döndürür.
    """
    if not user_message.strip():
        return [user_message]

    # LLM ile akıllı keyword üretimi
    if DEEPINFRA_KEY:
        try:
            async with httpx.AsyncClient(timeout=2.5) as c:
                r = await c.post(
                    f"{DEEPINFRA_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {DEEPINFRA_KEY}",
                        "Content-Type":  "application/json",
                    },
                    json={
                        "model":       KEYWORD_MODEL,
                        "messages":    [
                            {"role": "system", "content": _KW_SYSTEM},
                            {"role": "user",   "content": user_message[:300]},
                        ],
                        "max_tokens":  150,
                        "temperature": 0.2,
                    },
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                    # JSON'u bul ve parse et
                    m = re.search(r'\{.*?"queries".*?\}', text, re.DOTALL)
                    if m:
                        data = json.loads(m.group())
                        queries = [q.strip() for q in data.get("queries", [])
                                   if isinstance(q, str) and q.strip()]
                        if queries:
                            print(f"[KEYWORD GEN] ✅ LLM → {queries}")
                            return queries[:max_queries]
        except Exception as e:
            print(f"[KEYWORD GEN] LLM hata: {e} → heuristic fallback")

    return _heuristic_keywords(user_message, language, max_queries)


def _heuristic_keywords(query: str, language: str = "tr", max_queries: int = 3) -> list[str]:
    """LLM olmadan kural tabanlı keyword çıkarma."""
    cleaned = query.strip()

    tr_q_words = r"\b(nasıl|neden|nerede|ne zaman|kim|kaç|hangisi|mı|mi|mu|mü|acaba|ne)\b"
    en_q_words = r"\b(how|why|where|when|who|what|which|is|are|was|were|can|could|do|does)\b"
    core = re.sub(tr_q_words if language == "tr" else en_q_words, "", cleaned, flags=re.IGNORECASE)
    core = re.sub(r"\s+", " ", core).strip()

    recency_words_tr = ["bugün", "şu an", "şimdi", "son", "güncel", "2025", "2026", "yeni", "haber"]
    recency_words_en = ["today", "now", "current", "latest", "2025", "2026", "new", "news"]
    needs_recency = any(w in query.lower() for w in (recency_words_tr if language == "tr" else recency_words_en))
    year_tag = "2025" if needs_recency and not any(y in query for y in ["2025", "2026"]) else ""

    queries = [query]
    if core and core.lower() != query.lower():
        q2 = f"{core} {year_tag}".strip() if year_tag else core
        if q2 not in queries:
            queries.append(q2)

    # Varyant
    if len(queries) < max_queries:
        if language == "tr" and "nedir" not in core.lower():
            queries.append(f"{core} nedir nasıl kullanılır")
        elif language == "en":
            queries.append(f"{core} guide tutorial")

    result = [q for q in queries if q.strip()][:max_queries]
    print(f"[KEYWORD GEN] Heuristic → {result}")
    return result


# ── Quality scoring ───────────────────────────────────────────
def _score(result: SearchResult, query: str) -> float:
    score = 0.0
    q_words = {w for w in query.lower().split() if len(w) > 2}
    text = f"{result.title} {result.snippet}".lower()

    score += sum(0.10 for w in q_words if w in text)

    try:
        domain = urlparse(result.url).netloc.lstrip("www.")
        if any(td in domain for td in _TRUSTED_DOMAINS):
            score += 0.30
    except Exception:
        pass

    if any(y in text for y in ("2025", "2026", "2027")):
        score += 0.10

    if len(result.snippet) < 60:
        score -= 0.20

    return max(0.0, min(score, 2.0))


def _should_skip(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return True
    try:
        domain = urlparse(url).netloc.lstrip("www.")
        return any(sd in domain for sd in _SKIP_DOMAINS)
    except Exception:
        return True


# ── Source 1: SearXNG ─────────────────────────────────────────
async def _searxng(query: str, num: int, lang: str) -> list[SearchResult]:
    if not SEARXNG_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{SEARXNG_URL}/search", params={
                "q":          query,
                "format":     "json",
                "engines":    SEARXNG_ENGINES,
                "language":   lang,
                "categories": "general",
                "safesearch": "0",
            })
            if r.status_code != 200:
                return []
            items = r.json().get("results", [])[:num + 2]
            results = []
            for x in items:
                url = x.get("url", "")
                if _should_skip(url):
                    continue
                results.append(SearchResult(
                    title=x.get("title", "")[:120],
                    url=url,
                    snippet=x.get("content", x.get("snippet", ""))[:500],
                    source="searxng",
                ))
            print(f"[SEARXNG] ✅ {len(results)} results | {SEARXNG_ENGINES}")
            return results
    except Exception as e:
        print(f"[SEARXNG] ❌ {e}")
        return []


# ── Source 2 & 3: Crawl4AI scraping ──────────────────────────
async def _crawl4ai_get(url: str) -> Optional[str]:
    if not CRAWL4AI_URL:
        return None
    try:
        headers = {"Authorization": f"Bearer {CRAWL4AI_TOKEN}"} if CRAWL4AI_TOKEN else {}
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{CRAWL4AI_URL}/crawl", headers=headers, json={
                "urls": [url],
                "crawler_params": {
                    "headless": True,
                    "word_count_threshold": 10,
                    "excluded_tags": ["nav", "footer", "header", "aside", "script", "style"],
                },
            })
            if r.status_code != 200:
                return None
            for res in r.json().get("results", []):
                md = res.get("markdown") or res.get("extracted_content") or ""
                if len(md.strip()) > 100:
                    return md[:6000]
    except Exception as e:
        print(f"[CRAWL4AI] {e}")
    return None


def _extract_links_from_markdown(md: str, num: int, query: str, source: str) -> list[SearchResult]:
    results = []
    for line in md.split("\n"):
        m = re.search(r'\[([^\]]{5,120})\]\((https?://[^)]{10,})\)', line)
        if m:
            title   = m.group(1).strip()
            url     = m.group(2)
            snippet = re.sub(r'\[.*?\]\(.*?\)', '', line).replace(title, "").strip()[:400]
            if _should_skip(url) or len(title) < 5:
                continue
            results.append(SearchResult(title=title, url=url,
                                        snippet=snippet or title, source=source))
        if len(results) >= num:
            break
    return results


async def _bing_scrape(query: str, num: int, lang: str) -> list[SearchResult]:
    url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang={lang}&cc=TR&count={num+3}"
    md = await _crawl4ai_get(url)
    if not md:
        return []
    results = _extract_links_from_markdown(md, num, query, "bing_scrape")
    if results:
        print(f"[BING SCRAPE] ✅ {len(results)} results")
    return results


async def _google_scrape(query: str, num: int, lang: str) -> list[SearchResult]:
    url = f"https://www.google.com/search?q={quote_plus(query)}&hl={lang}&gl=TR&num={num+3}"
    md = await _crawl4ai_get(url)
    if not md:
        return []
    results = _extract_links_from_markdown(md, num, query, "google_scrape")
    if results:
        print(f"[GOOGLE SCRAPE] ✅ {len(results)} results")
    return results


# ── Source 4: DDG HTML (last resort) ─────────────────────────
_ddg_last_call = 0.0
_DDG_INTERVAL  = 4.0

_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15",
]


async def _ddg_html(query: str, num: int) -> list[SearchResult]:
    global _ddg_last_call
    wait = _DDG_INTERVAL - (time.time() - _ddg_last_call)
    if wait > 0:
        await asyncio.sleep(wait)

    import random
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": random.choice(_UAS), "Accept-Language": "tr-TR,tr;q=0.9"},
        ) as c:
            r = await c.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
            _ddg_last_call = time.time()

            if "captcha" in r.text.lower():
                print("[DDG] CAPTCHA — skipping")
                return []

            soup    = BeautifulSoup(r.text, "html.parser")
            results = []
            for div in soup.find_all("div", class_="result", limit=num + 2):
                a = div.find("a", class_="result__a")
                s = div.find("a", class_="result__snippet")
                if a and not _should_skip(a.get("href", "")):
                    results.append(SearchResult(
                        title=a.get_text(strip=True)[:120],
                        url=a["href"],
                        snippet=s.get_text(strip=True)[:400] if s else "",
                        source="ddg",
                    ))
            if results:
                print(f"[DDG HTML] ✅ {len(results)} results")
            return results[:num]
    except Exception as e:
        print(f"[DDG HTML] ❌ {e}")
        return []


# ── Deduplication ─────────────────────────────────────────────
def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    seen_urls: set[str] = set()
    seen_domains: dict[str, int] = {}
    out = []
    for r in results:
        url = r.url
        try:
            domain = urlparse(url).netloc.lstrip("www.")
        except Exception:
            domain = url
        if url in seen_urls:
            continue
        if seen_domains.get(domain, 0) >= 2:
            continue
        seen_urls.add(url)
        seen_domains[domain] = seen_domains.get(domain, 0) + 1
        out.append(r)
    return out


# ── Main API ──────────────────────────────────────────────────
async def web_search(
    query:         str,
    num:           int  = 8,
    language:      str  = "tr",
    use_cache:     bool = True,
    original_msg:  str  = "",    # Kullanıcının ham mesajı (keyword gen için)
    skip_keywords: bool = False,  # True → keyword gen atla, query'yi direkt kullan
) -> dict:
    """
    Primary web search function.

    Akış:
      1. AI Keyword Generator → optimize edilmiş sorgular üret
      2. Her sorgu için waterfall (SearXNG → Bing → Google → DDG)
      3. Tüm sonuçları birleştir, dedupe, sıralama
      4. En iyi num adet sonucu döndür

    Returns:
        {
            "success":      bool,
            "provider":     str,
            "queries_used": [str, ...],
            "data": {
                "query":   str,
                "results": [{"title", "url", "content", "score", "source"}, ...]
            }
        }
    """
    ck = _cache_key(query, language)
    if use_cache:
        cached = _cache_get(ck)
        if cached:
            print(f"[SEARCH] Cache hit: {query[:40]}")
            return _wrap(cached, "cache", query, [query])

    # ── 1. Keyword generation ─────────────────────────────────
    source_msg = original_msg or query
    if skip_keywords:
        search_queries = [query]
    else:
        search_queries = await generate_search_queries(source_msg, language)
        if query not in search_queries:
            search_queries.insert(0, query)
        search_queries = search_queries[:3]

    print(f"\n[SEARCH] '{query[:60]}' (lang={language})")
    print(f"[SEARCH] Sorgular: {search_queries}")

    # ── 2. Waterfall — her sorgu için kaynak dene ─────────────
    all_results: list[SearchResult] = []
    winning_provider = "none"

    for sq in search_queries:
        if len(all_results) >= num:
            break

        sources = [
            ("SearXNG",         lambda q=sq: _searxng(q, num, language)),
            ("Bing/Crawl4AI",   lambda q=sq: _bing_scrape(q, num, language)),
            ("Google/Crawl4AI", lambda q=sq: _google_scrape(q, num, language)),
            ("DDG HTML",        lambda q=sq: _ddg_html(q, num)),
        ]

        for name, fn in sources:
            try:
                # ✅ FIX v4.1: list[SearchResult] direkt alınır — dict değil
                raw: list[SearchResult] = await fn()
            except Exception as e:
                print(f"[SEARCH] {name} error ({sq[:30]}): {e}")
                continue

            # ✅ FIX v4.1: Boş liste kontrolü — .get() çağrısı yok
            if not raw:
                print(f"[SEARCH] {name}: 0 sonuç → sonraki kaynak")
                continue

            for r in raw:
                r.score = _score(r, sq)

            all_results.extend(raw)
            winning_provider = name.lower().replace("/", "_")
            print(f"[SEARCH] ✅ {name} ({sq[:30]}): {len(raw)} sonuç")
            break  # Bu sorgu için kaynak bulundu, sonraki sorguya geç

    if not all_results:
        print(f"[SEARCH] ❌ Tüm kaynaklar başarısız: {query[:40]}")
        return {"success": False, "provider": "none", "queries_used": search_queries,
                "data": {"query": query, "results": []}}

    # ── 3. Dedupe + sıralama ─────────────────────────────────
    final = _dedupe(sorted(all_results, key=lambda r: r.score, reverse=True))[:num]
    best  = final[0].score if final else 0.0

    print(f"[SEARCH] ✅ Toplam {len(final)} sonuç (best={best:.2f}, provider={winning_provider})")

    if use_cache:
        _cache_set(ck, final)

    return _wrap(final, winning_provider, query, search_queries)


def _wrap(
    results:      list[SearchResult],
    provider:     str,
    query:        str,
    queries_used: list[str] | None = None,
) -> dict:
    return {
        "success":      True,
        "provider":     provider,
        "queries_used": queries_used or [query],
        "data": {
            "query":    query,
            "provider": provider,
            "results":  [r.to_dict() for r in results],
        },
    }


# ── Sync wrapper ──────────────────────────────────────────────
def web_search_sync(query: str, num: int = 6, language: str = "tr") -> dict:
    """Synchronous wrapper for non-async callers."""
    ck = _cache_key(query, language)
    cached = _cache_get(ck)
    if cached:
        return _wrap(cached, "cache", query)

    # Sync'te LLM keyword gen yapılamaz — heuristic kullan
    search_queries = _heuristic_keywords(query, language, max_queries=2)

    if SEARXNG_URL:
        for sq in search_queries:
            try:
                r = requests.get(f"{SEARXNG_URL}/search", params={
                    "q": sq, "format": "json",
                    "engines": SEARXNG_ENGINES, "language": language,
                }, timeout=10).json()
                raw = []
                for x in r.get("results", [])[:num + 2]:
                    url = x.get("url", "")
                    if _should_skip(url):
                        continue
                    sr = SearchResult(
                        title=x.get("title", "")[:120],
                        url=url,
                        snippet=x.get("content", x.get("snippet", ""))[:500],
                        source="searxng",
                    )
                    sr.score = _score(sr, sq)
                    raw.append(sr)
                if raw:
                    raw = sorted(raw, key=lambda r: r.score, reverse=True)[:num]
                    _cache_set(ck, raw)
                    return _wrap(raw, "searxng", query, search_queries)
            except Exception:
                pass

    return {"success": False, "provider": "none", "queries_used": search_queries,
            "data": {"query": query, "results": []}}


# ── Kullanıcıya gösterilecek kaynak formatı ───────────────────
def format_sources_for_user(
    results:      list[dict],
    max_show:     int  = 5,
    show_snippet: bool = True,
) -> str:
    """
    Arama sonuçlarını kullanıcıya gösterilecek Markdown formatında döndürür.
    Chat yanıtının sonuna eklenir.

    Örnek çıktı:
        ---
        📎 **Kaynaklar**
        1. [Başlık](url)
           *domain.com* · Kısa açıklama...
    """
    if not results:
        return ""

    lines = ["\n---", "📎 **Kaynaklar**"]
    for i, r in enumerate(results[:max_show], 1):
        title   = r.get("title", "")[:80]
        url     = r.get("url", "")
        snippet = r.get("content", r.get("snippet", ""))[:120]
        try:
            domain = urlparse(url).netloc.lstrip("www.")
        except Exception:
            domain = url

        lines.append(f"{i}. [{title}]({url})")
        if show_snippet and snippet:
            lines.append(f"   *{domain}* · {snippet}")

    return "\n".join(lines)