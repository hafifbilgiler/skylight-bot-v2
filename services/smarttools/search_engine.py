"""
Web Agent Arama Motoru v3.0 — API Key Gerekmez
═══════════════════════════════════════════════════════════════
Waterfall:
  1. SearXNG  (bing+google+brave engine — DDG YOK)
  2. Crawl4AI ile Bing direkt scrape
  3. Crawl4AI ile Google direkt scrape
  4. DDG HTML son çare (rate limit ile)

Hiçbiri için API key gerekmez.
═══════════════════════════════════════════════════════════════
"""

import os, time, asyncio, re, httpx, requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from typing import Optional, List, Dict

SEARXNG_URL    = os.getenv("SEARXNG_URL",    None)
CRAWL4AI_URL   = os.getenv("CRAWL4AI_URL",   "http://crawl4ai:11235")
CRAWL4AI_TOKEN = os.getenv("CRAWL4AI_TOKEN", "skylight-crawl4ai-2026")

# DDG kaldırıldı — CAPTCHA yiyor
SEARXNG_ENGINES_PRIMARY = "bing,google,brave"
SEARXNG_ENGINES_NEWS    = "bing news,google news,brave news"

_cache: Dict[str, tuple] = {}
_CACHE_TTL = 180

def _cget(k):
    if k in _cache:
        v, e = _cache[k]
        if time.time() < e: return v
        del _cache[k]
    return None

def _cset(k, v):
    if len(_cache) > 300:
        now = time.time()
        for dk in [x for x,(_, e) in list(_cache.items()) if now >= e]:
            del _cache[dk]
    _cache[k] = (v, time.time() + _CACHE_TTL)

def _ok(provider, results, query):
    return {"success": True, "provider": provider,
            "data": {"query": query, "provider": provider, "results": results[:8]}}


# ══════════════════════════════════════════════════════════════
# KATMAN 1: SearXNG — bing+google+brave (API key yok)
# ══════════════════════════════════════════════════════════════

async def _searxng(query: str, num: int, language: str, news: bool = False) -> Optional[Dict]:
    if not SEARXNG_URL:
        return None
    engines = SEARXNG_ENGINES_NEWS if news else SEARXNG_ENGINES_PRIMARY
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.get(f"{SEARXNG_URL}/search", params={
                "q":          query,
                "format":     "json",
                "engines":    engines,
                "language":   language,
                "categories": "news" if news else "general",
                "safesearch": "0",
            })
            if r.status_code != 200: return None
            results = r.json().get("results", [])[:num]
            if not results: return None
            mapped = [{"title": x.get("title","")[:120], "url": x.get("url",""),
                       "content": x.get("content", x.get("snippet",""))[:500],
                       "engine": x.get("engine","")}
                      for x in results if x.get("url")]
            if not mapped: return None
            print(f"[SEARXNG] ✅ {len(mapped)} sonuç | {engines}")
            return _ok("searxng", mapped, query)
    except Exception as e:
        print(f"[SEARXNG] ❌ {e}")
        return None


# ══════════════════════════════════════════════════════════════
# KATMAN 2: Crawl4AI ile Bing scrape
# ══════════════════════════════════════════════════════════════

async def _crawl4ai_fetch(url: str) -> Optional[str]:
    """Crawl4AI ile sayfa çek."""
    if not CRAWL4AI_URL:
        return None
    try:
        headers = {"Authorization": f"Bearer {CRAWL4AI_TOKEN}"} if CRAWL4AI_TOKEN else {}
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(f"{CRAWL4AI_URL}/crawl", headers=headers, json={
                "urls": [url],
                "crawler_params": {
                    "headless": True,
                    "word_count_threshold": 10,
                    "excluded_tags": ["nav","footer","header","aside","script","style"],
                }
            })
            if r.status_code != 200: return None
            data = r.json()
            results = data.get("results", [data] if data.get("success") else [])
            for res in results:
                md = res.get("markdown") or res.get("extracted_content") or ""
                if md and len(md.strip()) > 100:
                    return md[:6000]
    except Exception as e:
        print(f"[CRAWL4AI] fetch error: {e}")
    return None


def _parse_bing_html(html: str, num: int) -> List[Dict]:
    """Bing HTML sonuçlarını parse et."""
    soup    = BeautifulSoup(html, "html.parser")
    results = []
    for li in soup.find_all("li", class_=re.compile(r"b_algo"), limit=num + 2):
        h2  = li.find("h2")
        a   = h2.find("a") if h2 else None
        p   = li.find("p") or li.find("div", class_="b_caption")
        if a and a.get("href"):
            url = a["href"]
            if url.startswith("http"):
                results.append({
                    "title":   a.get_text(strip=True)[:120],
                    "url":     url,
                    "content": p.get_text(strip=True)[:500] if p else "",
                })
    return results[:num]


def _parse_google_html(html: str, num: int) -> List[Dict]:
    """Google HTML sonuçlarını parse et."""
    soup    = BeautifulSoup(html, "html.parser")
    results = []
    for div in soup.find_all("div", class_=re.compile(r"^(g|tF2Cxc|yuRUbf)")):
        a       = div.find("a")
        h3      = div.find("h3")
        snippet = div.find("div", class_=re.compile(r"VwiC3b|s3v9rd|st"))
        if a and h3 and a.get("href","").startswith("http"):
            results.append({
                "title":   h3.get_text(strip=True)[:120],
                "url":     a["href"],
                "content": snippet.get_text(strip=True)[:500] if snippet else "",
            })
        if len(results) >= num:
            break
    return results[:num]


async def _bing_scrape(query: str, num: int, language: str) -> Optional[Dict]:
    """Crawl4AI ile Bing'i direkt scrape et."""
    bing_url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang={language}&cc=TR&count={num+2}"
    print(f"[BING SCRAPE] Deneniyor: {query[:40]}")
    html = await _crawl4ai_fetch(bing_url)
    if not html:
        return None
    # Markdown formatında gelirse link çıkar
    results = []
    lines   = html.split("\n")
    for line in lines:
        # Markdown link formatı: [title](url)
        m = re.search(r'\[([^\]]+)\]\((https?://[^)]+)\)', line)
        if m:
            title = m.group(1)[:120]
            url   = m.group(2)
            # Bing kendi URL'lerini filtrele
            if "bing.com" not in url and "microsoft.com" not in url:
                snippet = line.replace(m.group(0), "").strip()[:300]
                results.append({"title": title, "url": url, "content": snippet})
        if len(results) >= num:
            break

    if not results:
        # HTML parse dene
        results = _parse_bing_html(html, num)

    if results:
        print(f"[BING SCRAPE] ✅ {len(results)} sonuç")
        return _ok("bing_scrape", results, query)
    return None


async def _google_scrape(query: str, num: int, language: str) -> Optional[Dict]:
    """Crawl4AI ile Google'ı direkt scrape et."""
    google_url = f"https://www.google.com/search?q={quote_plus(query)}&hl={language}&gl=TR&num={num+2}"
    print(f"[GOOGLE SCRAPE] Deneniyor: {query[:40]}")
    html = await _crawl4ai_fetch(google_url)
    if not html:
        return None
    results = []
    lines   = html.split("\n")
    for line in lines:
        m = re.search(r'\[([^\]]+)\]\((https?://[^)]+)\)', line)
        if m:
            title = m.group(1)[:120]
            url   = m.group(2)
            if "google.com" not in url and url.startswith("http"):
                snippet = line.replace(m.group(0), "").strip()[:300]
                results.append({"title": title, "url": url, "content": snippet})
        if len(results) >= num:
            break

    if not results:
        results = _parse_google_html(html, num)

    if results:
        print(f"[GOOGLE SCRAPE] ✅ {len(results)} sonuç")
        return _ok("google_scrape", results, query)
    return None


# ══════════════════════════════════════════════════════════════
# KATMAN 3: DDG HTML — Son çare
# ══════════════════════════════════════════════════════════════

_ddg_last = 0.0

async def _ddg_html(query: str, num: int) -> Optional[Dict]:
    global _ddg_last
    wait = 4.0 - (time.time() - _ddg_last)
    if wait > 0:
        await asyncio.sleep(wait)
    import random
    ua = random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15",
    ])
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
                    headers={"User-Agent": ua, "Accept-Language": "tr-TR,tr;q=0.9"}) as c:
            r = await c.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
            _ddg_last = time.time()
            if "captcha" in r.text.lower(): return None
            soup = BeautifulSoup(r.text, "html.parser")
            results = []
            for div in soup.find_all("div", class_="result", limit=num + 2):
                a = div.find("a", class_="result__a")
                s = div.find("a", class_="result__snippet")
                if a and a.get("href","").startswith("http"):
                    results.append({"title": a.get_text(strip=True)[:120],
                                    "url": a["href"],
                                    "content": s.get_text(strip=True)[:400] if s else ""})
            if results:
                print(f"[DDG HTML] ✅ {len(results)} sonuç")
                return _ok("duckduckgo", results[:num], query)
    except Exception as e:
        print(f"[DDG HTML] ❌ {e}")
    return None


# ══════════════════════════════════════════════════════════════
# ANA FONKSİYON
# ══════════════════════════════════════════════════════════════

async def web_search(query: str, num: int = 6, language: str = "tr",
                     news: bool = False, use_cache: bool = True) -> Dict:
    """
    Waterfall arama — API key gerekmez.
    SearXNG → Bing scrape → Google scrape → DDG HTML
    """
    ck = f"ws_{query.lower().strip()}_{language}_{news}"
    if use_cache:
        cached = _cget(ck)
        if cached:
            print(f"[SEARCH] Cache hit: {query[:40]}")
            return cached

    print(f"\n[SEARCH] '{query[:50]}' (lang={language})")

    steps = [
        ("SearXNG",       lambda: _searxng(query, num, language, news)),
        ("Bing Scrape",   lambda: _bing_scrape(query, num, language)),
        ("Google Scrape", lambda: _google_scrape(query, num, language)),
        ("DDG HTML",      lambda: _ddg_html(query, num)),
    ]

    for name, fn in steps:
        try:
            result = await fn()
            if result and result.get("success"):
                r = result.get("data", {}).get("results", [])
                if r:
                    print(f"[SEARCH] ✅ {name}: {len(r)} sonuç")
                    if use_cache: _cset(ck, result)
                    return result
                print(f"[SEARCH] {name}: sonuç yok → sonraki")
        except Exception as e:
            print(f"[SEARCH] {name} hata: {e}")

    print(f"[SEARCH] ❌ Tüm kaynaklar başarısız")
    return {"success": False, "provider": "none",
            "error": "Arama başarısız", "data": {"query": query, "results": []}}


def web_search_sync(query: str, num: int = 5, language: str = "tr") -> Dict:
    """Sync wrapper — asyncio olmayan yerlerde kullan."""
    ck = f"ws_{query.lower().strip()}_{language}"
    cached = _cget(ck)
    if cached: return cached

    if SEARXNG_URL:
        try:
            data = requests.get(f"{SEARXNG_URL}/search", params={
                "q": query, "format": "json",
                "engines": SEARXNG_ENGINES_PRIMARY, "language": language,
            }, timeout=10).json()
            results = data.get("results", [])[:num]
            if results:
                mapped = [{"title": r.get("title",""), "url": r.get("url",""),
                           "content": r.get("content", r.get("snippet",""))[:400]}
                          for r in results if r.get("url")]
                if mapped:
                    r = _ok("searxng", mapped, query)
                    _cset(ck, r)
                    return r
        except Exception:
            pass

    return {"success": False, "provider": "none",
            "data": {"query": query, "results": []}}