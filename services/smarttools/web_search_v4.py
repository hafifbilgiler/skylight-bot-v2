"""
╔══════════════════════════════════════════════════════════════╗
║  ONE-BUNE  ·  Web Search Engine  v4.0                       ║
║  Intelligent multi-source retrieval with quality scoring    ║
╚══════════════════════════════════════════════════════════════╝

Architecture:
  ┌─────────────────────────────────────────────────────┐
  │  Query Enrichment  →  Parallel Search  →  Dedupe    │
  │  Quality Score     →  Rank            →  Return     │
  └─────────────────────────────────────────────────────┘

Sources (waterfall, no API keys required):
  1. SearXNG  — bing + google + brave engines (no DDG)
  2. Bing scrape  via Crawl4AI
  3. Google scrape via Crawl4AI
  4. DDG HTML — rate-limited last resort

Quality signals:
  + Trusted domain   (+0.30)
  + Query word match (+0.10 per word)
  + Recency (2025/2026) (+0.10)
  - Social/app URL   (skip entirely)
  - Short snippet    (< 60 chars, skip)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx
import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────
SEARXNG_URL    = os.getenv("SEARXNG_URL")
CRAWL4AI_URL   = os.getenv("CRAWL4AI_URL",   "http://crawl4ai:11235")
CRAWL4AI_TOKEN = os.getenv("CRAWL4AI_TOKEN", "skylight-crawl4ai-2026")

SEARXNG_ENGINES = "bing,google,brave"   # DDG intentionally excluded

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


# ── Quality scoring ───────────────────────────────────────────
def _score(result: SearchResult, query: str) -> float:
    score = 0.0
    q_words = {w for w in query.lower().split() if len(w) > 2}
    text = f"{result.title} {result.snippet}".lower()

    # Word overlap
    score += sum(0.10 for w in q_words if w in text)

    # Trusted domain
    try:
        domain = urlparse(result.url).netloc.lstrip("www.")
        if any(td in domain for td in _TRUSTED_DOMAINS):
            score += 0.30
    except Exception:
        pass

    # Recency signal
    if any(y in text for y in ("2025", "2026", "2027")):
        score += 0.10

    # Penalize short snippets
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
    """Extract [title](url) links from Crawl4AI markdown output."""
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
    seen_urls    = set()
    seen_domains = set()
    out = []
    for r in results:
        url = r.url
        try:
            domain = urlparse(url).netloc.lstrip("www.")
        except Exception:
            domain = url
        if url in seen_urls:
            continue
        # max 2 results per domain
        if seen_domains.get(domain, 0) >= 2:
            continue
        seen_urls.add(url)
        seen_domains[domain] = seen_domains.get(domain, 0) + 1
        out.append(r)
    return out


# ── Main API ──────────────────────────────────────────────────
async def web_search(
    query:      str,
    num:        int  = 8,
    language:   str  = "tr",
    use_cache:  bool = True,
) -> dict:
    """
    Primary web search function.

    Returns:
        {
            "success":  bool,
            "provider": str,
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
            return _wrap(cached, "cache", query)

    print(f"\n[SEARCH] '{query[:60]}' (lang={language})")

    # Waterfall — stop at first source with good results
    # Lazy — coroutine sadece sıra gelince oluşturulur, await edilmeden bırakılmaz
    sources = [
        ("SearXNG",        lambda: _searxng(query, num, language)),
        ("Bing/Crawl4AI",  lambda: _bing_scrape(query, num, language)),
        ("Google/Crawl4AI",lambda: _google_scrape(query, num, language)),
        ("DDG HTML",       lambda: _ddg_html(query, num)),
    ]

    for name, fn in sources:
        try:
            result = await fn()
        except Exception as e:
            print(f"[SEARCH] {name} error: {e}")
            continue

        if not result or not result.get("success"):
            print(f"[SEARCH] {name}: no results → next")
            continue

        # Dict sonuçları SearchResult'a çevir, score hesapla
        raw_dicts = result.get("data", {}).get("results", [])
        if not raw_dicts:
            continue

        results_sr = []
        for d in raw_dicts:
            sr = SearchResult(
                title=d.get("title", ""),
                url=d.get("url", ""),
                snippet=d.get("content", d.get("snippet", "")),
                source=name,
            )
            sr.score = _score(sr, query)
            results_sr.append(sr)

        results_sr = _dedupe(sorted(results_sr, key=lambda r: r.score, reverse=True))
        results_sr = [r for r in results_sr][:num]

        if results_sr:
            best = results_sr[0].score
            print(f"[SEARCH] ✅ {name}: {len(results_sr)} results (best={best:.2f})")
            if use_cache:
                _cache_set(ck, results_sr)
            return _wrap(results_sr, name.lower().replace("/", "_"), query)

    print(f"[SEARCH] ❌ All sources failed: {query[:40]}")
    return {"success": False, "provider": "none",
            "data": {"query": query, "results": []}}


def _wrap(results: list[SearchResult], provider: str, query: str) -> dict:
    return {
        "success":  True,
        "provider": provider,
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

    if SEARXNG_URL:
        try:
            r = requests.get(f"{SEARXNG_URL}/search", params={
                "q": query, "format": "json",
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
                sr.score = _score(sr, query)
                raw.append(sr)
            if raw:
                raw = sorted(raw, key=lambda r: r.score, reverse=True)[:num]
                _cache_set(ck, raw)
                return _wrap(raw, "searxng", query)
        except Exception:
            pass

    return {"success": False, "provider": "none",
            "data": {"query": query, "results": []}}