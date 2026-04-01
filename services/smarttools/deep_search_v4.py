"""
╔══════════════════════════════════════════════════════════════╗
║  ONE-BUNE  ·  Deep Search Pipeline  v4.0                    ║
║  Grounding-based retrieval — no synthesis hallucination     ║
╚══════════════════════════════════════════════════════════════╝

Design principle (Gemini grounding model):
  Web results are injected as CONTEXT into the main LLM call.
  No intermediate synthesis LLM. Single LLM, zero hallucination risk.

Pipeline:
  Query Expansion
      ↓
  Parallel Search  (web_search_v4)
      ↓
  Parallel Scrape  (Crawl4AI → Direct fallback)
      ↓
  Chunk + Tag      (sliding window, source-aware)
      ↓
  Rerank           (BGE-Reranker with retry)
      ↓
  Format Context   (grounding block for main LLM)
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from web_search_v4 import web_search, SearchResult

# ── Config ────────────────────────────────────────────────────
CRAWL4AI_URL    = os.getenv("CRAWL4AI_URL",    "http://crawl4ai:11235")
CRAWL4AI_TOKEN  = os.getenv("CRAWL4AI_TOKEN",  "skylight-crawl4ai-2026")
RERANKER_URL    = os.getenv("RERANKER_URL",     "http://skylight-reranker:8087")
DEEPINFRA_URL   = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
DEEPINFRA_KEY   = os.getenv("DEEPINFRA_API_KEY", "")
SYNTHESIS_MODEL = os.getenv("SYNTHESIS_MODEL",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8")

CHUNK_SIZE    = 180   # words — fits reranker 512 token window
CHUNK_OVERLAP = 25
TOP_K         = 8
MAX_PAGES     = 4
PAGE_CHARS    = 6000

_SKIP_DOMAINS = frozenset({
    "youtube.com", "youtu.be", "twitter.com", "x.com",
    "instagram.com", "facebook.com", "apps.apple.com",
    "play.google.com", "amazon.com", "ebay.com",
})


# ── Data model ────────────────────────────────────────────────
@dataclass
class Chunk:
    text:   str
    title:  str = ""
    url:    str = ""
    kind:   str = "page"   # page | snippet
    score:  float = 0.0


@dataclass
class DeepSearchResult:
    query:         str
    chunks:        list[Chunk] = field(default_factory=list)
    search_hits:   int = 0
    pages_scraped: int = 0
    elapsed:       float = 0.0
    provider:      str = ""


# ── Page fetcher ──────────────────────────────────────────────
async def _fetch_crawl4ai(url: str) -> Optional[str]:
    if not CRAWL4AI_URL:
        return None
    try:
        headers = {"Authorization": f"Bearer {CRAWL4AI_TOKEN}"} if CRAWL4AI_TOKEN else {}
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.post(f"{CRAWL4AI_URL}/crawl", headers=headers, json={
                "urls": [url],
                "crawler_params": {
                    "headless": True,
                    "word_count_threshold": 15,
                    "excluded_tags": ["nav", "footer", "header", "aside", "script", "style"],
                },
            })
            if r.status_code != 200:
                return None
            for res in r.json().get("results", []):
                md = res.get("markdown") or res.get("extracted_content") or ""
                if len(md.strip()) > 150:
                    return md[:PAGE_CHARS]
    except Exception as e:
        print(f"[CRAWL4AI] {url[:50]}: {e}")
    return None


async def _fetch_direct(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as c:
            r = await c.get(url)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                return None
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            blocks = [t.get_text(" ", strip=True)
                      for t in soup.find_all(["article", "main", "p", "h1", "h2", "h3"])
                      if len(t.get_text(strip=True)) > 50]
            text = re.sub(r"\s+", " ", " ".join(blocks)).strip()
            return text[:PAGE_CHARS] if len(text) > 150 else None
    except Exception as e:
        print(f"[DIRECT] {url[:50]}: {e}")
    return None


async def _fetch_page(url: str) -> Optional[str]:
    if not url or not url.startswith(("http://", "https://")):
        return None
    from urllib.parse import urlparse
    try:
        domain = urlparse(url).netloc.lstrip("www.")
        if any(sd in domain for sd in _SKIP_DOMAINS):
            return None
    except Exception:
        pass
    content = await _fetch_crawl4ai(url)
    if content:
        return content
    return await _fetch_direct(url)


# ── Chunker ───────────────────────────────────────────────────
def _chunk(text: str, title: str, url: str, kind: str) -> list[Chunk]:
    words = text.split()
    if len(words) <= CHUNK_SIZE:
        return [Chunk(text=text, title=title, url=url, kind=kind)]
    chunks = []
    step   = CHUNK_SIZE - CHUNK_OVERLAP
    for i in range(0, len(words), step):
        chunk_text = " ".join(words[i:i + CHUNK_SIZE])
        if chunk_text.strip():
            chunks.append(Chunk(text=chunk_text, title=title, url=url, kind=kind))
        if i + CHUNK_SIZE >= len(words):
            break
    return chunks


# ── Reranker (with retry) ─────────────────────────────────────
async def _rerank(query: str, chunks: list[Chunk]) -> list[Chunk]:
    if not chunks:
        return []

    texts = [c.text[:512] for c in chunks]

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=35.0, write=10.0, pool=5.0)
            ) as c:
                r = await c.post(f"{RERANKER_URL}/rerank", json={
                    "query":     query,
                    "documents": texts,
                    "top_k":     min(TOP_K, len(chunks)),
                    "normalize": True,
                })
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    ranked  = []
                    for item in results:
                        idx = texts.index(item["document"]) if item["document"] in texts else -1
                        if idx >= 0:
                            chunks[idx].score = item.get("score", 0.0)
                            ranked.append(chunks[idx])
                    best = ranked[0].score if ranked else 0
                    print(f"[RERANK] ✅ {len(chunks)} → {len(ranked)} | best={best:.3f}")
                    return ranked
        except httpx.ConnectError:
            print(f"[RERANK] Connect error (attempt {attempt+1}/3)")
            if attempt < 2: await asyncio.sleep(1.0)
        except httpx.TimeoutException:
            print(f"[RERANK] Timeout (attempt {attempt+1}/3)")
            if attempt < 2: await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[RERANK] Error: {e}")
            break

    # Keyword fallback
    q_words = set(query.lower().split())
    for c in chunks:
        c.score = sum(1.0 for w in q_words if w in c.text.lower()) / (len(q_words) + 1)
    return sorted(chunks, key=lambda c: c.score, reverse=True)[:TOP_K]


# ── Grounding formatter ───────────────────────────────────────
def _format_grounding(query: str, chunks: list[Chunk],
                      search_results: list[dict]) -> str:
    """
    Format retrieved context as a grounding block for the main LLM.
    The main LLM uses this as context, NOT a separate synthesis LLM.
    This eliminates the hallucination layer entirely.
    """
    lines = [
        "[WEB ARAŞTIRMA SONUÇLARI]",
        f"Sorgu: {query}",
        f"Kaynaklar: {len(set(c.url for c in chunks))} sayfa analiz edildi",
        "",
    ]

    # Best chunks (from reranker)
    seen_urls = set()
    if chunks:
        lines.append("## Bulunan Bilgiler")
        for chunk in chunks[:TOP_K]:
            if chunk.url not in seen_urls:
                seen_urls.add(chunk.url)
                domain = chunk.url.split("/")[2] if chunk.url else "?"
                lines.append(f"\n**{chunk.title or domain}**")
                lines.append(f"{chunk.text[:500]}")
                lines.append(f"*{chunk.url}*")

    # Snippet fallback if no scraped content
    remaining = [r for r in search_results
                 if r.get("url") not in seen_urls and r.get("content")][:4]
    if remaining:
        lines.append("\n## Ek Kaynaklar")
        for r in remaining:
            lines.append(f"• **{r.get('title','')[:80]}**")
            lines.append(f"  {r.get('content','')[:300]}")
            lines.append(f"  *{r.get('url','')}*")

    lines.extend([
        "",
        "[/WEB ARAŞTIRMA SONUÇLARI]",
        "",
        "GÖREV: Yukarıdaki web kaynaklarını kullanarak soruyu yanıtla.",
        "- SADECE kaynaklardaki bilgiyi kullan",
        "- Kaynaklarda olmayan veri ekleme, tahmin etme",
        "- Sayı/tarih/skor → kaynaktan al",
        "- Bilgi eksikse: 'Web kaynaklarında bulunamadı' de",
    ])
    return "\n".join(lines)


# ── Main pipeline ─────────────────────────────────────────────
async def deep_search(
    query:    str,
    language: str = "tr",
    num:      int = 8,
) -> tuple[str, dict]:
    """
    Full deep search pipeline.

    Returns:
        (grounding_context: str, metadata: dict)

    The grounding_context is injected directly into the main LLM's
    system/user message — no intermediate LLM synthesis.
    """
    t0 = time.time()
    print(f"\n{'━'*56}")
    print(f"[DEEP SEARCH] {query}")
    print(f"{'━'*56}")

    # 1. Query expansion for better recall
    queries = [query]
    if language == "tr":
        queries.append(f"{query} son dakika güncel")
    else:
        queries.append(f"{query} latest 2025 2026")

    # 2. Parallel search
    search_tasks   = [web_search(q, num, language) for q in queries[:2]]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # Merge & dedupe results
    seen_urls  = set()
    merged     = []
    provider   = "unknown"
    for sr in search_results:
        if isinstance(sr, dict) and sr.get("success"):
            provider = sr["data"].get("provider", provider)
            for r in sr["data"].get("results", []):
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    merged.append(r)

    print(f"[DEEP SEARCH] 1/4 SEARCH ✅ {len(merged)} unique ({provider})")

    if not merged:
        return "Web kaynaklarında sonuç bulunamadı.", {"elapsed": time.time() - t0}

    # 3. Parallel page scraping
    urls_to_fetch = [r["url"] for r in merged[:MAX_PAGES] if r.get("url")]
    fetch_results = await asyncio.gather(
        *[_fetch_page(u) for u in urls_to_fetch],
        return_exceptions=True,
    )
    pages = []
    for item, text in zip(merged[:MAX_PAGES], fetch_results):
        if isinstance(text, str) and len(text) > 150:
            pages.append({"url": item["url"], "title": item.get("title", ""), "text": text})

    print(f"[DEEP SEARCH] 2/4 SCRAPE ✅ {len(pages)}/{MAX_PAGES} pages")

    # 4. Chunk
    all_chunks: list[Chunk] = []
    for p in pages:
        all_chunks.extend(_chunk(p["text"], p["title"], p["url"], "page"))
    for r in merged[:num]:
        if r.get("content") and len(r["content"]) > 80:
            all_chunks.append(Chunk(
                text=r["content"][:500],
                title=r.get("title", ""),
                url=r.get("url", ""),
                kind="snippet",
            ))

    print(f"[DEEP SEARCH] 3/4 CHUNK ✅ {len(all_chunks)} chunks")

    # 5. Rerank
    top_chunks = await _rerank(query, all_chunks)
    print(f"[DEEP SEARCH] 4/4 RERANK ✅ {len(top_chunks)} chunks selected")

    elapsed = round(time.time() - t0, 2)
    print(f"[DEEP SEARCH] Done in {elapsed}s\n")

    context = _format_grounding(query, top_chunks, merged)
    meta    = {
        "elapsed":      elapsed,
        "provider":     provider,
        "search_hits":  len(merged),
        "pages_scraped": len(pages),
        "chunks_total": len(all_chunks),
        "chunks_used":  len(top_chunks),
    }
    return context, meta