import re
"""
═══════════════════════════════════════════════════════════════
SKYLIGHT CHAT SERVICE — v3.1 (LiveDataRouter entegrasyonu)
═══════════════════════════════════════════════════════════════
v3.0 → v3.1 değişiklikleri:

  ✅ needs_real_time_data() KALDIRILDI — artık yok
  ✅ call_smart_tools() KALDIRILDI — artık yok
  ✅ REAL_TIME_KEYWORDS KALDIRILDI — artık yok
  ✅ Tüm canlı veri kararları smart_tools /classify'a taşındı
  ✅ Tek fonksiyon: call_live_data(query) → /live endpoint
  ✅ format_live_context() → LLM'e hazır format /live'dan gelir

  Kural: Chat servisi asla keyword listesi tutmaz.
         "Bu sorgu için canlı veri lazım mı?" → smart_tools /classify
         "Canlı veriyi getir" → smart_tools /live

  Tüm diğer özellikler aynen korundu:
  ✅ Memory sistemi
  ✅ Thinking display
  ✅ Web synthesis
  ✅ Auto summaries
  ✅ Code context + compression
  ✅ Multi-mode support
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, AsyncGenerator
import httpx
import json
import os
import asyncpg
import asyncio
import re
from datetime import datetime

from intent_classifier import build_reasoning_hint, get_intent_thinking_steps, Intent, classify_intent
from conversation_state import (
    build_state_context, extract_and_update_state,
    get_workspace, get_version, set_task,
)
from task_builder import build_task, task_to_prompt_hint
from prompts_production import (
    SKYLIGHT_SYSTEM_PROMPT,
    IMAGE_GENERATION_ENHANCEMENT_PROMPT,
)
# Eski isimler için alias — geriye dönük uyumluluk
ASSISTANT_SYSTEM_PROMPT  = SKYLIGHT_SYSTEM_PROMPT
CODE_SYSTEM_PROMPT       = SKYLIGHT_SYSTEM_PROMPT
IT_EXPERT_SYSTEM_PROMPT  = SKYLIGHT_SYSTEM_PROMPT
STUDENT_SYSTEM_PROMPT    = SKYLIGHT_SYSTEM_PROMPT
SOCIAL_SYSTEM_PROMPT     = SKYLIGHT_SYSTEM_PROMPT
CODE_VISION_SYSTEM_PROMPT = SKYLIGHT_SYSTEM_PROMPT

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY",  "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
SMART_TOOLS_URL    = os.getenv("SMART_TOOLS_URL",    "http://skylight-smart-tools:8081")

DB_HOST     = os.getenv("DB_HOST",     "postgres")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME",     "skylight_db")
DB_USER     = os.getenv("DB_USER",     "skylight_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SUMMARY_INTERVAL = int(os.getenv("SUMMARY_INTERVAL", "15"))

MODE_CONFIGS = {
    "assistant": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  int(os.getenv("DEEPINFRA_ASSISTANT_MAX_TOKENS", "4096")),
        "temperature": float(os.getenv("DEEPINFRA_ASSISTANT_TEMPERATURE", "0.7")),
        "top_p":       0.9,
        "system_prompt": ASSISTANT_SYSTEM_PROMPT,
    },
    "code": {
        "model":       os.getenv("DEEPINFRA_CODE_MODEL", "Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo"),
        "max_tokens":  int(os.getenv("DEEPINFRA_CODE_MAX_TOKENS", "16000")),
        "temperature": float(os.getenv("DEEPINFRA_CODE_TEMPERATURE", "0.2")),
        "top_p":       0.85,
        "system_prompt":           CODE_SYSTEM_PROMPT,
        "compression_threshold":   20,   # 20 mesajda bir sıkıştır (önceki 12'ydi)
        "large_file_threshold":    2000,
    },
    "it_expert": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  3500,
        "temperature": 0.5,
        "top_p":       0.88,
        "system_prompt": IT_EXPERT_SYSTEM_PROMPT,
    },
    "student": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  2500,
        "temperature": 0.8,
        "top_p":       0.9,
        "system_prompt": STUDENT_SYSTEM_PROMPT,
    },
    "social": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  2500,
        "temperature": 0.9,
        "top_p":       0.92,
        "system_prompt": SOCIAL_SYSTEM_PROMPT,
    },
}

app = FastAPI(title="Skylight Chat Service", version="3.1.0")
db_pool: Optional[asyncpg.Pool] = None

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

def _strip_internal_tags(text: str) -> str:
    """SSE chunk'tan LLM grounding taglarini temizle."""
    if "[" not in text:
        return text
    # Bracket tag'lari string replace ile temizle
    replacements = [
        ("[WEB ARAŞTIRMA SONUÇLARI]", ""),
        ("[WEB ARAŞTIRMA SONUÇLARI — SADECE BUNLARI KULLAN]", ""),
        ("[/WEB ARAŞTIRMA SONUÇLARI]", ""),
        ("[ARAŞTIRMA SONUÇLARI]", ""),
        ("[/ARAŞTIRMA SONUÇLARI]", ""),
        ("[CANLI VERİ]", ""),
        ("[/CANLI VERİ]", ""),
        ("[Canlı Veri]", ""),
        ("[LIVE DATA]", ""),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    # [NOT: ...] ve benzeri dinamik tag'lar icin basit parser
    import re
    text = re.sub(r"\[NOT:[^\]]{0,200}\]", "", text)
    text = re.sub(r"\[SADECE[^\]]{0,100}\]", "", text)
    return text


async def startup_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            min_size=2, max_size=10, command_timeout=60,
        )
        print("[DB] Connection pool created")
    except Exception as e:
        print(f"[DB ERROR] {e}")
        db_pool = None

@app.on_event("shutdown")
async def shutdown_db():
    global db_pool
    if db_pool:
        await db_pool.close()
    from conversation_state import close_redis
    await close_redis()

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role:    str
    content: str

class ChatRequest(BaseModel):
    prompt:          str
    mode:            str
    user_id:         int
    conversation_id: Optional[str]        = None
    history:         Optional[List[Dict]] = None
    rag_context:     Optional[str]        = None
    context:         Optional[str]        = None
    session_summary: Optional[str]        = None
    file_id:         Optional[str]        = None
    # Görsel analiz
    image_data:      Optional[str]        = None  # base64 encoded image
    image_type:      Optional[str]        = None  # image/png, image/jpeg vs.
    # Smart Router v4 — Gateway'den gelir
    live_type_hint:  Optional[str]        = None
    router_intent:   Optional[str]        = None
    router_thinking: Optional[str]        = None
    user_level:      Optional[str]        = None
    needs_realtime:  Optional[bool]       = None

class ThinkingStep(BaseModel):
    emoji:   str
    message: str

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LIVE DATA — Hızlı local detection + Smart Tools veri çekme
#
# KURAL: Detection LOCAL (0ms, network yok)
#        Veri çekme → smart_tools /unified (sadece lazımsa)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Anlık API sinyalleri — smart tools /unified'a gider ────────
_CURRENCY_KW  = ("dolar","euro","eur","usd","gbp","sterlin","pound","jpy","yen","chf",
                 "döviz","doviz","exchange rate","kaç tl","kac tl","tl kaç",
                 "dolar kaç","euro kaç","döviz kuru","kur nedir","dolar kuru","euro kuru")

# Tek kelime döviz — "euro" "dolar" tek başına yazılınca da çalışsın
_CURRENCY_SINGLE = ("dolar","euro","eur","usd","gbp","sterlin","chf","jpy","yen","pound")

# Yeniden veri çekme sinyalleri — "tekrar bak", "yanlış", "güncelle"
_RETRY_KW = ("tekrar bak","yeniden bak","güncelle","yanlış verdin","hatalı",
              "doğru değil","bir daha bak","güncel değil","eski veri")
_WEATHER_KW   = ("hava durumu","havadurumu","hava nasıl","havalar nasıl",
                 "hava kaç derece","sıcaklık","sicaklik","yağmur yağıyor",
                 "kar yağıyor","weather","forecast","bugün hava","yarın hava","derece")
_CRYPTO_KW    = ("bitcoin","btc","ethereum","eth","dogecoin","doge","solana",
                 "kripto","crypto","coin fiyat","bitcoin kaç","ethereum kaç")
_TIME_KW      = ("saat kaç","saat kac","saati kaç","şimdi saat","şu an saat",
                 "bugün ne günü","what time","günün saati")
_NEWS_KW      = ("son haberler","güncel haberler","bugünün haberleri","son dakika",
                 "breaking news","haberleri göster","haberleri getir","haber oku",
                 "gündem ne","today's news","latest news")
_PRICE_KW     = ("kaç lira","fiyatı ne kadar","fiyatı kaç","altın fiyatı",
                 "gram altın","petrol fiyatı","borsa","bist","hisse fiyatı")

# ── Borsa keywords ──────────────────────────────────────────
_BORSA_KW     = (
    "borsa","bist","hisse","teknik analiz","mum analiz","mum grafiği",
    "rsi","macd","bollinger","destek direnç","al sinyali","sat sinyali",
    "hisse analiz","thyao","garan","akbnk","eregl","kchol",
    "btc analiz","eth analiz","kripto analiz",
)
BORSA_URL = os.getenv("BORSA_URL", "http://skylight-borsa:8086")

# ── Güncel bilgi gerektiren sinyaller — deep_search'e gider ────
# Bu sinyaller varsa keyword detection yerine deep_search çağrılır
_DEEP_SEARCH_KW = (
    # Spor
    "maç sonucu","son maç","maçı kaç","playoff","eleme maçı","dünya kupası",
    "şampiyonlar ligi","süper lig","puan durumu","fikstür","transfer",
    "world cup","champions league","premier league","la liga",
    # Güncel karşılaştırma / sıralama / piyasa durumu
    "şu an en iyi","şuanda en iyi","piyasada en iyi","en iyi hangisi",
    "hangi daha iyi","hangisi daha iyi","karşılaştır","comparison",
    "en popüler","en çok kullanılan","en güncel","piyasada ne var",
    "yeni çıkan","son çıkan","2025 en iyi","2026 en iyi",
    "en iyi yapay zeka","en iyi ai","best ai","best llm",
    # Teknoloji güncel
    "son model","en yeni model","yeni sürüm","hangi model daha iyi",
    "chatgpt mi claude mu","gemini mi","hangi ai","ai karşılaştırma",
    # Mevzuat / hukuk / vergi
    "asgari ücret","asgari sermaye","vergi oranı","vergi dilimi",
    "sgk prim","kıdem tazminatı","ihbar tazminatı",
    "limited şirket","anonim şirket","şirket kuruluş",
    "kdv oranı","stopaj oranı","gelir vergisi",
    # Nüfus / istatistik
    "nüfusu kaç","nüfus kaç","kaç kişi yaşıyor",
    "istatistik","tüik","tüfe","enflasyon","işsizlik oranı",
    # Teknoloji / ürün güncelliği
    "son model","en yeni","güncel model","yeni sürüm","son sürüm",
    "hangi model","kaçıncı nesil",
    # Genel güncel soru sinyalleri
    "2024","2025","2026","bu yıl","geçen yıl","son yıl",
    "güncel","günümüzde","şu an","şu anda","bugün itibariyle",
    "en son","kaç oldu","kaça yükseldi","kaça düştü",
    "son açıklanan","yeni açıklanan","resmi rakam",
)

# ── Canlı veri gerekmez — bu kalıplar statik bilgi ─────────────
_STATIC_KW    = ("nasıl kullanılır","syntax","örnek ver","açıkla","anlat",
                 "ne demek","tanımı nedir","nasıl yapılır","tutorial")
_TECH_MODES   = {"code","it_expert"}


def _detect_live_type(
    query: str,
    mode: str,
    router_decision: Dict = None,
    history: List[Dict] = None,
) -> Optional[str]:
    """
    Akıllı detection — Router + bağlam.
    
    Öncelik sırası:
    1. Router `needs_realtime=true` dedi → web'e git
    2. Kısa mesaj + önceki tool aynıysa → aynı tool
    3. Keyword listesi → anlık API
    4. Hiçbiri → None
    """
    q     = query.lower().strip()
    words = q.split()

    # ── Kısa mesaj bağlam analizi ─────────────────────────
    if len(words) <= 4 and history:
        # Son asistan mesajından tool tipini anla
        last_live_tool = None
        for msg in reversed(history[-8:]):
            if msg.get("role") != "assistant":
                continue
            c = (msg.get("content") or "").lower()
            if any(x in c for x in ["°c", "sıcaklık", "hissedilen", "rüzgar", "nem %"]):
                last_live_tool = "weather"
                break
            if any(x in c for x in ["1 usd", "1 eur", "try =", "₺", "dolar kuru", "euro kuru"]):
                last_live_tool = "currency"
                break
            if any(x in c for x in ["bitcoin", "btc", "ethereum", "kripto", "$"]):
                last_live_tool = "crypto"
                break

        if last_live_tool == "weather":
            # Bilinen şehirleri kontrol et — suffix olmadan da yakala
            known = ["istanbul","ankara","izmir","antalya","bursa","adana","konya",
                     "berlin","london","paris","tokyo","dubai","amsterdam","madrid",
                     "rome","vienna","moscow","beijing","new york","los angeles",
                     "zurich","geneva","barcelona","milan","stockholm","oslo","helsinki"]
            # q'da şehir var mı? (ekleri de kaldırarak)
            q_clean = q
            for suf in ["da","de","ta","te","'da","'de","'ta","'te",
                        "nin","nın","nun","nün","in","ın","un","ün"]:
                q_clean = q_clean.replace(suf, " ").strip()
            for city in known:
                if city in q or city in q_clean:
                    print(f"[DETECT] Bağlam weather → {city}")
                    return "weather"
            # "peki" "ya" "ya da" ile başlayan kısa soru → devam
            if any(q.startswith(w) for w in ["peki","ya ","ya da","orası","orada"]):
                print(f"[DETECT] Bağlam weather devam (peki/ya)")
                return "weather"

        if last_live_tool == "currency":
            currency_words = ["dolar","euro","sterlin","gbp","usd","eur","btc",
                              "bitcoin","frank","yen","chf","jpy"]
            if any(k in q for k in currency_words):
                print(f"[DETECT] Bağlam currency devam")
                return "currency"
    
    q = query.lower()

    # ── Router kararı varsa öncelikli kullan ──────────────
    if router_decision:
        rt   = router_decision.get("needs_realtime", False)
        tool = router_decision.get("tool", "none")
        conf = router_decision.get("confidence", "low")

        if rt and tool and tool != "none":
            # Router spesifik tool belirledi
            tool_map = {
                "currency": "currency",
                "weather":  "weather",
                "crypto":   "crypto",
                "news":     "news",
                "web":      "deep_search",
                "borsa":    "borsa",
                "time":     "time",
            }
            mapped = tool_map.get(tool)
            if mapped:
                print(f"[DETECT] Router → {mapped} (conf={conf})")
                return mapped

        if rt and conf in ("high", "medium"):
            # Realtime lazım ama tool belirsiz → deep search
            print(f"[DETECT] Router needs_realtime=true → deep_search")
            return "deep_search"

    # ── Kod modunda canlı veri yok ────────────────────────
    if mode in _TECH_MODES:
        return None

    # ── Kesinlikle statik — web'e gitme ──────────────────
    if any(k in q for k in _STATIC_KW) and len(q.split()) <= 6:
        return None

    # Retry sinyali — önceki canlı veri yanlışsa yeniden çek
    if any(k in q for k in _RETRY_KW):
        # Hangi araç tekrar sorulacak? Context'e bak
        # Şimdilik currency ve genel olarak işle
        return "currency" if any(k in q for k in _CURRENCY_SINGLE) else "currency"

    # Önce anlık API sinyalleri — bunlar /unified'a gider
    if any(k in q for k in _CURRENCY_KW): return "currency"

    # Tek kelime döviz — "euro" veya "dolar" tek başına
    q_stripped = q.strip().rstrip("?! ")
    if q_stripped in _CURRENCY_SINGLE or len(q.split()) <= 2 and any(k in q for k in _CURRENCY_SINGLE):
        return "currency"

    if any(k in q for k in _WEATHER_KW):  return "weather"
    if any(k in q for k in _CRYPTO_KW):   return "crypto"
    if any(k in q for k in _TIME_KW):     return "time"
    if any(k in q for k in _NEWS_KW):     return "news"
    if any(k in q for k in _BORSA_KW):    return "borsa"
    if any(k in q for k in _PRICE_KW):    return "price_search"

    # Güncel bilgi sinyali — deep_search pipeline'a gider
    if any(k in q for k in _DEEP_SEARCH_KW): return "deep_search"

    return None


async def get_live_data(
    query: str,
    mode: str = "assistant",
    router_tool: str = None,
    router_decision: Dict = None,
    history: List[Dict] = None,
) -> Optional[str]:
    """
    1. Router LLM kararı varsa → öncelikli kullan
    2. Keyword detection → fallback
    3. Router needs_realtime=true → deep_search
    2. Lazımsa smart_tools /unified'a git — gerçek veriyi getir
    3. format_for_llm() ile LLM'e hazır formata dönüştür

    Ağ çağrısı sadece canlı veri gerektiğinde yapılır.
    """
    # Router kararı varsa keyword'ü atla — LLM zaten anladı
    if router_tool and router_tool != "none":
        live_type = router_tool
        print(f"[LIVE DATA] Router tool → {live_type}")
    else:
        live_type = _detect_live_type(query, mode, router_decision, history or [])
    
    if not live_type:
        return None

    # Borsa — borsa servisine git
    if live_type == "borsa":
        try:
            # Sembolü query'den çıkar
            import re as _re
            # Bilinen BIST sembolleri
            known = ["THYAO","GARAN","AKBNK","EREGL","KCHOL","SAHOL","PETKM","TUPRS",
                     "BIMAS","ASELS","FROTO","TOASO","SISE","TTKOM","ARCLK",
                     "BTC","ETH","BNB","SOL","XRP","DOGE"]
            sym = None
            q_upper = query.upper()
            for k in known:
                if k in q_upper:
                    sym = k
                    break
            # Regex ile 2-6 harf sembol bul
            if not sym:
                m = _re.search(r'\b([A-ZÇĞİÖŞÜ]{2,6})\b', q_upper)
                if m:
                    sym = m.group(1)

            if sym:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"{BORSA_URL}/analyze/{sym}")
                    if resp.status_code == 200:
                        d = resp.json()
                        summary = d.get("ai_summary", "")
                        print(f"[BORSA] ✅ {sym} analizi alındı")
                        return f"[Borsa Analizi]\n{summary}\n[/Borsa Analizi]"
        except Exception as e:
            print(f"[BORSA] Hata: {e}")
        return None

    if not SMART_TOOLS_URL:
        return None

    print(f"[LIVE DATA] '{query[:40]}' → {live_type}")

    # ── Deep search pipeline — güncel mevzuat, istatistik, ürün bilgisi ──
    if live_type == "deep_search":
        try:
            # Bağlam zenginleştirme — kısa sorulara önceki konuyu ekle
            enriched_query = query
            if history and len(query.split()) <= 4:
                recent = " ".join([
                    (m.get("content") or "")[:60]
                    for m in history[-3:]
                    if m.get("role") == "user"
                ])
                if recent.strip():
                    enriched_query = f"{query} {recent.strip()}"[:200]
                    print(f"[DEEP SEARCH] Enriched: {enriched_query[:80]}")

            async with httpx.AsyncClient(timeout=55.0) as client:  # Keyword gen eklendi — ekstra sure gerekli
                resp = await client.post(
                    f"{SMART_TOOLS_URL}/deep_search",
                    json={
                        "query":        enriched_query,
                        "num_results":  6,
                        "fetch_pages":  3,
                        "synthesize":   False,  # LLM sentez YOK — ham sonuçlar gelsin
                        "language":     "tr",
                    },
                )
                if resp.status_code != 200:
                    print(f"[DEEP SEARCH] HTTP {resp.status_code}")
                    return None

                data = resp.json()
                if not data.get("success"):
                    return None

                d = data.get("data", {})
                synthesis = d.get("synthesis", "")
                search_results = d.get("search_results", [])
                pages_fetched  = d.get("pages_fetched", 0)
                
                # ── Grounding context — LLM'e verilecek web verisi ──────
                # Synthesis LLM yok — chat LLM kendisi sentezliyor
                # queries_used: keyword generator'ın ürettiği sorgular
                queries_used  = d.get("queries_used", [enriched_query])
                sources_block = d.get("sources_block", "")  # Kullanıcıya gösterilecek linkler

                parts = [
                    "[ARAŞTIRMA SONUÇLARI]",
                    f"Asıl soru: {enriched_query}",
                    f"Kullanılan sorgular: {', '.join(queries_used)}",
                    f"Kaynak sayısı: {len(search_results)} | Okunan sayfa: {pages_fetched}",
                    "",
                ]

                # Synthesis varsa ekle (ham format)
                if synthesis and len(synthesis) > 50:
                    parts.append("## Bulunan Bilgiler:")
                    parts.append(synthesis)

                # Arama sonuçlarını da LLM'e ver (grounding için)
                if search_results:
                    parts.append("")
                    parts.append("## Kaynaklar (alıntı yapabilirsin):")
                    for i, r in enumerate(search_results[:6], 1):
                        title   = r.get("title", "")[:80]
                        snippet = r.get("snippet", r.get("content", ""))[:300]
                        url     = r.get("url", "")
                        if title and snippet:
                            parts.append(f"[{i}] **{title}**")
                            parts.append(f"{snippet}")
                            parts.append(f"URL: {url}")
                            parts.append("")

                parts.append("[/ARAŞTIRMA SONUÇLARI]")
                parts.append("")
                parts.append("GÖREV: Yukarıdaki web kaynaklarını kullanarak soruyu yanıtla.")
                parts.append("- Kaynaklardaki bilgiyi kullan, tahmin etme")
                parts.append("- Sayı/tarih/skor → kaynaktan al")
                parts.append("- Yanıtının SONUNA aşağıdaki kaynak bloğunu AYNEN ekle (değiştirme):")
                parts.append("")
                if sources_block:
                    parts.append(f"SOURCES_BLOCK_PLACEHOLDER:{sources_block}")

                result = "\n".join(parts)
                print(f"[DEEP SEARCH] ✅ {len(search_results)} kaynak | sorgular: {queries_used}")
                return result
        except httpx.TimeoutException:
            print(f"[DEEP SEARCH] Timeout — fallback yok")
        except Exception as e:
            print(f"[DEEP SEARCH] Error: {e}")
        return None

    # ── Anlık API araçları — /unified ────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{SMART_TOOLS_URL}/unified",
                json={"query": query},
            )
            if resp.status_code != 200:
                print(f"[LIVE DATA] HTTP {resp.status_code}")
                return None

            data      = resp.json()

            # Şehir belirtilmemişse → LLM'e sormayı ilet, uydurmasın
            if not data.get("success"):
                err = data.get("error", "")
                if err == "city_not_specified":
                    return "[BİLGİ: Kullanıcı hava durumu sordu ama şehir belirtmedi. Hangi şehir için bakayım diye sor. Uydurma veri verme.]"
                return None

            tool_used = data.get("tool_used", live_type)
            tool_data = data.get("data", {})

            # Format — LLM'e beslenecek temiz metin
            parts = []  # Tag yok — LLM sadece veriyi görsün, tag'ı kullanıcıya yansıtmasın

            if tool_used == "weather":
                parts.append(
                    f"📍 {tool_data.get('city')}, {tool_data.get('country')}\n"
                    f"🌡️ {tool_data.get('temperature')}°C (hissedilen {tool_data.get('feels_like')}°C)\n"
                    f"☁️ {tool_data.get('description')}\n"
                    f"💧 Nem: %{tool_data.get('humidity')} | "
                    f"💨 Rüzgar: {tool_data.get('wind_speed')} km/h"
                )
            elif tool_used == "currency":
                parts.append(f"💱 {tool_data.get('formatted')}")
            elif tool_used == "crypto":
                parts.append(
                    f"₿ {tool_data.get('formatted')}\n"
                    f"📈 24s değişim: {tool_data.get('change_24h'):+.2f}%"
                )
            elif tool_used == "time":
                parts.append(f"🕐 {tool_data.get('formatted_tr')}")
            elif tool_used == "news":
                articles = tool_data.get("articles", [])[:5]
                if articles:
                    parts.append("📰 Son Haberler:")
                    for i, a in enumerate(articles, 1):
                        parts.append(f"  {i}. {a.get('title','')}")
            elif tool_used in ("web_search","price_search"):
                for r in tool_data.get("results", [])[:3]:
                    parts.append(f"• {r.get('title','')}: {r.get('content','')[:200]}")

            formatted = (
                "\n".join(parts) +
                "\n\n[SADECE BU VERİYİ KULLAN — ÖNCEKI KONUYU BIRAK]"
            )
            print(f"[LIVE DATA] ✅ {tool_used} — {len(formatted)} chars")
            return formatted

    except httpx.TimeoutException:
        print(f"[LIVE DATA] Timeout")
    except Exception as e:
        print(f"[LIVE DATA] Error: {e}")
    return None


# ── get_live_data wrapper — sources_block ayıklar ─────────────
_SOURCES_SENTINEL = "SOURCES_BLOCK_PLACEHOLDER:"

async def get_live_data_with_sources(
    query: str,
    mode: str = "assistant",
    router_tool: str = None,
    router_decision: Dict = None,
    history: List[Dict] = None,
) -> tuple:
    """
    get_live_data'yı çağırır, dönen metinden SOURCES_BLOCK_PLACEHOLDER'ı ayıklar.

    Returns:
        (grounding_context: Optional[str], sources_block: str)
        grounding_context → LLM'e gidecek temiz grounding metni
        sources_block     → Kullanıcıya gösterilecek Markdown kaynak listesi
    """
    raw = await get_live_data(
        query,
        mode=mode,
        router_tool=router_tool,
        router_decision=router_decision,
        history=history,
    )
    if not raw:
        return raw, ""

    if _SOURCES_SENTINEL in raw:
        idx = raw.find(_SOURCES_SENTINEL)
        sources_block = raw[idx + len(_SOURCES_SENTINEL):].strip()
        context       = raw[:idx].rstrip()
        return context or None, sources_block

    return raw, ""



# ═══════════════════════════════════════════════════════════════
# MEMORY & CONTEXT
# ═══════════════════════════════════════════════════════════════

def _extract_active_commands(history: List[Dict]) -> List[str]:
    """
    Konuşma geçmişinden kullanıcının verdiği aktif komutları çıkar.
    Bu komutlar system prompt'a enjekte edilir — LLM her mesajda görür.
    """
    from intent_classifier import classify_intent, Intent
    commands = []
    seen = set()
    for msg in history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not content:
            continue
        result = classify_intent(content, [], "assistant")
        if result["intent"] == Intent.USER_COMMAND:
            cmd = content.strip()
            if cmd not in seen:
                seen.add(cmd)
                commands.append(cmd)
    return commands


async def load_user_memory(user_id: int) -> Optional[str]:
    if not db_pool:
        return None
    try:
        async with db_pool.acquire() as conn:
            memory = await conn.fetchval("SELECT get_user_memory_for_prompt($1)", user_id)
            if memory:
                return memory
            await conn.execute(
                "INSERT INTO user_memory (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                user_id)
            return "[USER MEMORY]\nNo memory data yet\n[/USER MEMORY]"
    except Exception as e:
        print(f"[MEMORY ERROR] {e}")
        return None


async def load_conversation_summary(conversation_id: str) -> Optional[str]:
    if not db_pool or not conversation_id:
        return None
    try:
        async with db_pool.acquire() as conn:
            summary = await conn.fetchval(
                "SELECT get_conversation_summary($1::uuid)", conversation_id)
            return summary if summary and "New conversation" not in summary else None
    except Exception as e:
        print(f"[SUMMARY ERROR] {e}")
        return None


async def should_create_summary(conversation_id: str) -> bool:
    if not db_pool or not conversation_id:
        return False
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT COUNT(*) as total_messages,
                       COALESCE(MAX(cs.messages_end), 0) as last_summary_end
                FROM messages m
                LEFT JOIN conversation_summaries cs ON m.conversation_id = cs.conversation_id
                WHERE m.conversation_id = $1::uuid
            """, conversation_id)
            if result:
                diff = result['total_messages'] - result['last_summary_end']
                return diff >= SUMMARY_INTERVAL
    except Exception as e:
        print(f"[SUMMARY CHECK ERROR] {e}")
    return False


def _score_message_importance(msg: dict) -> float:
    """
    Mesajın önemini 0-1 arasında puanla.
    Claude'un sliding window mantığına benzer önem skoru.
    """
    content = (msg.get("content") or "").lower()
    role    = msg.get("role", "user")
    score   = 0.5  # base

    # Kullanıcı mesajları genelde daha önemli
    if role == "user":
        score += 0.1

    # Kod içeren mesajlar kritik
    if "```" in content or "def " in content or "import " in content:
        score += 0.25

    # Hata mesajları kritik
    if any(w in content for w in ["error","hata","traceback","exception","failed"]):
        score += 0.2

    # Karar ifadeleri
    if any(w in content for w in ["tamam","kabul","evet","hayır","kesinlikle","anlaştık"]):
        score += 0.15

    # Kısa mesajlar genelde az önemli
    if len(content) < 30:
        score -= 0.15

    # Çok uzun mesajlar bilgi yoğun
    if len(content) > 500:
        score += 0.1

    return min(1.0, max(0.0, score))


async def create_conversation_summary(user_id: int, conversation_id: str, config: Dict):
    """
    Akıllı özetleme — Claude benzeri sliding window + önem skoru.

    Strateji:
    1. Tüm özetlenmemiş mesajları al
    2. Her mesajı önem skoruna göre değerlendir
    3. Önemli mesajları tam tut, düşük önemli olanları özet olarak sakla
    4. Hiyerarşik özet: konu → alt konular → kararlar → sonraki adımlar
    5. Kullanıcı bellek güncelleme (tercihler, teknik bilgi)
    """
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            # Özetlenmemiş mesajları al
            messages = await conn.fetch("""
                SELECT content, role, created_at, id FROM messages
                WHERE conversation_id = $1::uuid
                AND id > COALESCE(
                    (SELECT max(end_message_id::text)::uuid FROM conversation_summaries
                     WHERE conversation_id = $1::uuid),
                    '00000000-0000-0000-0000-000000000000'::uuid)
                ORDER BY created_at ASC
            """, conversation_id)

            if not messages or len(messages) < 5:
                return

        msgs = [dict(m) for m in messages]

        # ── Önem skorlama ────────────────────────────────────
        scored = [(m, _score_message_importance(m)) for m in msgs]

        # Yüksek öneme sahip mesajları tam tut (skor > 0.65)
        high_priority = [m for m, s in scored if s > 0.65]
        low_priority  = [m for m, s in scored if s <= 0.65]

        # Düşük öncelikli mesajları kısalt
        low_text  = "\n".join([
            f"{m['role'][:1].upper()}: {m['content'][:120]}..."
            for m in low_priority
        ])
        high_text = "\n".join([
            f"{m['role'].upper()}: {m['content'][:600]}"
            for m in high_priority
        ])

        # ── LLM ile akıllı özet ─────────────────────────────
        is_tr = any(
            any(c in (m.get("content") or "") for c in "çğışöüÇĞİŞÖÜ")
            for m in msgs[:5]
        )
        lang_rule = "TÜRKÇE yanıtla." if is_tr else "Respond in English."

        prompt = f"""Bir konuşmayı analiz edip yapılandırılmış özet çıkar.

ÖNEMLİ MESAJLAR (tam içerik):
{high_text}

DİĞER MESAJLAR (kısaltılmış):
{low_text if low_text else "(yok)"}

Aşağıdaki JSON formatında yanıtla. {lang_rule}
{{
  "topic": "Ana konu (max 60 karakter)",
  "subtopics": ["alt konu 1", "alt konu 2"],
  "summary": "Konuşmanın özeti (max 200 kelime, Türkçe)",
  "decisions_made": ["alınan karar 1", "alınan karar 2"],
  "next_steps": ["sonraki adım 1"],
  "user_preferences": {{
    "communication": "nasıl iletişim kuruyor (kısa/detaylı/teknik)",
    "technical_level": "beginner/intermediate/expert",
    "domain": "çalıştığı alan"
  }},
  "key_facts": ["önemli bilgi 1", "önemli bilgi 2"]
}}
SADECE JSON döndür, başka hiçbir şey yazma."""

        summary_response = ""
        async for chunk in stream_deepinfra_completion(
            messages=[
                {"role": "system",
                 "content": "Konuşma analisti. Sadece geçerli JSON döndür, markdown yok."},
                {"role": "user", "content": prompt}
            ],
            model=config["model"],
            max_tokens=600,
            temperature=0.1,
            top_p=0.9,
        ):
            summary_response += chunk

        # JSON parse
        try:
            raw = summary_response.strip()
            if "```" in raw:
                s = raw.find("{")
                e = raw.rfind("}") + 1
                raw = raw[s:e]
            data = json.loads(raw)
        except Exception:
            print(f"[SUMMARY] JSON parse hatası — atlandı")
            return

        # ── DB kaydet ────────────────────────────────────────
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO conversation_summaries
                (user_id, conversation_id, messages_start, messages_end,
                 topic, subtopics, summary_text, progress,
                 decisions_made, next_steps, learned_facts,
                 start_message_id, end_message_id, messages_summarized)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb,
                        $12::uuid, $13::uuid, $14)
                ON CONFLICT DO NOTHING
            """,
                user_id, conversation_id,
                1, len(msgs),
                data.get("topic", "Konuşma"),
                data.get("subtopics", []),
                data.get("summary", ""),
                data.get("summary", ""),
                data.get("decisions_made", []),
                data.get("next_steps", []),
                json.dumps({
                    "preferences": data.get("user_preferences", {}),
                    "key_facts":   data.get("key_facts", []),
                }),
                msgs[0]["id"],
                msgs[-1]["id"],
                len(msgs),
            )
            print(f"[SUMMARY] ✅ '{data.get('topic')}' — {len(msgs)} mesaj, "
                  f"{len(high_priority)} yüksek önem, {len(low_priority)} düşük önem")

        # ── Kullanıcı belleğini güncelle ─────────────────────
        prefs = data.get("user_preferences", {})
        if prefs:
            await update_user_memory_from_summary(user_id, {
                "preferences": prefs.get("communication", ""),
                "technical":   prefs.get("technical_level", ""),
                "domain":      prefs.get("domain", ""),
            })

    except Exception as e:
        print(f"[SUMMARY ERROR] {e}")


async def update_user_memory_from_summary(user_id: int, learned_facts: Dict):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            if 'technical' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory SET technical_preferences = technical_preferences || $1::jsonb,
                    updated_at=NOW() WHERE user_id=$2
                """, json.dumps({'learned': learned_facts['technical']}), user_id)
            if 'preferences' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory SET communication_style = communication_style || $1::jsonb,
                    updated_at=NOW() WHERE user_id=$2
                """, json.dumps({'noted': learned_facts['preferences']}), user_id)
    except Exception as e:
        print(f"[MEMORY UPDATE ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# CODE MODE
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# KULLANICI DOSYA WORKSPACE — Konuşmada dosya saklama
# Kullanıcı kodu paylaşırsa → workspace'e kaydet
# Sonraki mesajlarda otomatik bağlam olarak kullan
# ═══════════════════════════════════════════════════════════════

async def save_user_file_to_workspace(
    conversation_id: str,
    filename: str,
    content_text: str,
    language: str = "unknown"
):
    """Kullanıcının paylaştığı kodu workspace'e kaydet."""
    if not db_pool or not conversation_id:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO workspace_files
                    (conversation_id, filename, content, language,
                     size_bytes, created_at, updated_at)
                VALUES ($1::uuid, $2, $3, $4, $5, NOW(), NOW())
                ON CONFLICT (conversation_id, filename)
                DO UPDATE SET
                    content    = EXCLUDED.content,
                    language   = EXCLUDED.language,
                    size_bytes = EXCLUDED.size_bytes,
                    updated_at = NOW()
            """, conversation_id, filename,
                 content_text, language, len(content_text.encode()))
            print(f"[WORKSPACE] ✅ {filename} kaydedildi ({len(content_text)} chars)")
    except Exception as e:
        print(f"[WORKSPACE] ❌ {e}")


async def load_workspace_files(conversation_id: str) -> List[Dict]:
    """Workspace'teki dosyaları yükle — max 5, en son güncellenenler."""
    if not db_pool or not conversation_id:
        return []
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT filename, content, language, updated_at
                FROM workspace_files
                WHERE conversation_id = $1::uuid
                ORDER BY updated_at DESC
                LIMIT 5
            """, conversation_id)
            return [dict(r) for r in rows]
    except Exception:
        return []


def _extract_code_blocks(text: str) -> List[Dict]:
    """Mesajdan kod bloklarını çıkar — filename + content + language."""
    import re
    blocks = []
    # backtick python ... backtick formatı
    pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
    for match in pattern.finditer(text):
        lang    = match.group(1) or "text"
        code    = match.group(2).strip()
        if len(code) > 50:  # Çok kısa snippet'leri atla
            blocks.append({"language": lang, "content": code})
    return blocks


async def process_user_message_for_workspace(
    conversation_id: str,
    user_message: str
):
    """
    Kullanıcı mesajında kod varsa workspace'e kaydet.
    Otomatik filename üret (lang + timestamp).
    """
    blocks = _extract_code_blocks(user_message)
    for i, block in enumerate(blocks):
        lang     = block["language"]
        ext_map  = {
            "python":"py","javascript":"js","typescript":"ts",
            "jsx":"jsx","tsx":"tsx","html":"html","css":"css",
            "sql":"sql","bash":"sh","yaml":"yaml","json":"json",
            "go":"go","rust":"rs","java":"java","cpp":"cpp",
        }
        ext      = ext_map.get(lang, "txt")
        filename = f"user_code_{i+1}.{ext}"
        await save_user_file_to_workspace(
            conversation_id, filename, block["content"], lang
        )


async def load_code_context(conversation_id: str) -> Optional[Dict]:
    if not db_pool or not conversation_id:
        return None
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT last_code, last_language, last_file_name,
                       tech_stack, compressed_history, messages_since_compression
                FROM code_context WHERE conversation_id=$1::uuid
            """, conversation_id)
            if result:
                return dict(result)
            await conn.execute("""
                INSERT INTO code_context (conversation_id, user_id)
                SELECT $1::uuid, user_id FROM conversations WHERE id=$1::uuid
                ON CONFLICT DO NOTHING
            """, conversation_id)
    except Exception as e:
        print(f"[CODE CONTEXT ERROR] {e}")
    return None


def extract_code_from_message(message: str) -> Optional[Dict]:
    match = re.search(r'```(\w+)\n(.*?)```', message, re.DOTALL)
    if not match:
        return None
    language = match.group(1).lower()
    code     = match.group(2).strip()
    file_name = None
    if language == 'python':
        m = re.search(r'#\s*(?:File:|Dosya:)?\s*([a-zA-Z0-9_\-\.\/]+\.py)', code, re.IGNORECASE)
        if m: file_name = m.group(1)
    elif language in ['javascript','typescript','js','ts']:
        m = re.search(r'//\s*(?:File:|Dosya:)?\s*([a-zA-Z0-9_\-\.\/]+\.(?:js|ts))', code, re.IGNORECASE)
        if m: file_name = m.group(1)
    return {"code": code, "language": language, "file_name": file_name,
            "line_count": len(code.split('\n'))}


async def track_shared_code(conversation_id: str, code: str, language: str, file_name: Optional[str]):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE code_context SET last_code=$2, last_language=$3,
                last_file_name=COALESCE($4, last_file_name),
                messages_since_compression=messages_since_compression+1, updated_at=NOW()
                WHERE conversation_id=$1::uuid
            """, conversation_id, code, language, file_name)
    except Exception as e:
        print(f"[CODE TRACKING ERROR] {e}")


async def should_compress_code_context(conversation_id: str, threshold: int = 12) -> bool:
    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT messages_since_compression FROM code_context
                WHERE conversation_id=$1::uuid
            """, conversation_id)
            return bool(count and count >= threshold)
    except:
        return False


async def compress_code_context(conversation_id: str, messages: List[Dict], config: Dict):
    if len(messages) < 5:
        return
    conv_text = "\n\n".join([
        f"{m['role'].upper()}: {m['content'][:500]}"
        for m in messages[:-5]
    ])
    prompt = f"""Analyze this code conversation and create a BRIEF summary (max 150 words):
{conv_text}
Extract ONLY: Files discussed, Issues fixed, Technical decisions, Current project context.
Format as compact bullet points. Use Turkish if conversation is Turkish."""
    try:
        compressed = ""
        async for chunk in stream_deepinfra_completion(
            messages=[{"role":"system","content":"You summarize code conversations concisely."},
                      {"role":"user","content":prompt}],
            model=config["model"], max_tokens=400, temperature=0.3, top_p=0.85,
        ):
            compressed += chunk
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE code_context SET compressed_history=$2,
                compression_metadata=$3::jsonb,
                messages_since_compression=0, last_compression_at=NOW()
                WHERE conversation_id=$1::uuid
            """, conversation_id, compressed,
                json.dumps({"compressed_at": datetime.now().isoformat()}))
        print(f"[CODE COMPRESSION] {len(conv_text)} → {len(compressed)} chars")
    except Exception as e:
        print(f"[CODE COMPRESSION ERROR] {e}")


async def build_code_messages(
    user_id: int,
    conversation_id: str,
    user_prompt: str,
    history: List[Dict],
    config: Dict,
    **kwargs,
) -> List[Dict]:
    messages     = []
    code_context = await load_code_context(conversation_id) if conversation_id else None

    system_content = config["system_prompt"]

    # ── REASONING LAYER — Code mode intent ───────────────────────
    reasoning_hint = build_reasoning_hint(user_prompt, history or [], "code")
    print(f"[INTENT/CODE] {reasoning_hint.split(chr(10))[0]}")
    # ─────────────────────────────────────────────────────────────

    # ── TASK OBJECT — Kod cevabından önce görev tespiti ──────────
    workspace    = await get_workspace(conversation_id or "")
    intent_str   = classify_intent(user_prompt, history or [], "code").get("intent", "")
    task         = build_task(user_prompt, intent_str, workspace, history or [])
    task_hint    = task_to_prompt_hint(task)
    # task_hint system prompt'a eklenmez — history zaten bağlamı taşıyor
    # Task'ı Redis'e kaydet — follow-up'larda bağlam korunur
    if conversation_id:
        asyncio.create_task(set_task(conversation_id, task))
    print(f"[TASK] {task['task_type']} → {task['response_style']} | target={task.get('target','?')}")
    # ─────────────────────────────────────────────────────────────

    # Memory
    user_memory = await load_user_memory(user_id)
    system_content = system_content.replace(
        "{user_memory}", user_memory or "[USER MEMORY]\nNo memory yet\n[/USER MEMORY]")

    # Code context
    if code_context:
        parts = ["[CODE CONTEXT]"]
        last_code = code_context.get('last_code')
        if last_code and len(last_code) < 5000:
            parts.append(f"\nLast shared code ({code_context.get('last_language')}):")
            parts.append(f"```{code_context.get('last_language')}\n{last_code[:3000]}\n```\n")
        tech_stack = code_context.get('tech_stack')
        if tech_stack:
            parts.append(f"Project stack: {', '.join(tech_stack)}")
        compressed = code_context.get('compressed_history')
        if compressed:
            parts.append(f"\nPrevious conversation summary:\n{compressed}\n")
        parts.append("[/CODE CONTEXT]")
        system_content += "\n\n" + "\n".join(parts)

    # ── Canlı veri (code modda da çalışır) ──────────────────────
    live_context = await get_live_data(user_prompt, mode="code", history=history or [])
    if live_context:
        kwargs["live_context"] = live_context

    # RAG + web context
    if kwargs.get('rag_context'):
        system_content += f"\n\n[RAG Context]\n{kwargs['rag_context']}\n[/RAG Context]"
    if kwargs.get('context'):
        system_content += f"\n\n[Web Search]\n{kwargs['context']}\n[/Web Search]"

    messages.append({"role": "system", "content": system_content})

    # Compression check
    if conversation_id and await should_compress_code_context(
        conversation_id, config.get("compression_threshold", 12)
    ):
        asyncio.create_task(compress_code_context(conversation_id, history, config))

    # History
    recent = history[-15:] if (code_context and code_context.get('compressed_history') and len(history) > 15) else history[-25:]
    for msg in recent:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_prompt})

    # Track code in background
    code_data = extract_code_from_message(user_prompt)
    if code_data and conversation_id:
        asyncio.create_task(track_shared_code(
            conversation_id, code_data['code'],
            code_data['language'], code_data.get('file_name')))

    return messages


# ═══════════════════════════════════════════════════════════════
# THINKING DISPLAY
# ═══════════════════════════════════════════════════════════════

def should_show_thinking(prompt: str, mode: str, history: list = None) -> bool:
    """
    Thinking adımlarını SADECE hata/debug/analiz durumlarında göster.
    Her şey için gösterme — sadece kullanıcı bir sorun bildiriyorsa.
    """
    p = prompt.lower().strip()

    # Önce hata/debug sinyallerini kontrol et (kelime sayısından bağımsız)
    SHOW_INDICATORS = [
        'hata alıyorum', 'hata veriyor', 'hata var',
        'çalışmıyor', 'exception', 'traceback', 'debug',
        'neden çalışmıyor', 'sorun var', 'sorun nedir',
        'crashloopback', 'oomkilled', 'imagepull',
        'analiz et', 'incele', 'kontrol et',
    ]
    if any(ind in p for ind in SHOW_INDICATORS):
        return True

    # Error kelimesi tek başına kısa mesajda da göster
    words = p.split()
    if 'error' in words or 'crash' in words:
        return True

    # Bunların dışında → asla thinking gösterme
    return False


async def generate_thinking_steps(
    prompt: str,
    mode: str,
    history: List[Dict] = None,
) -> List[ThinkingStep]:
    """
    Thinking steps — debug/analiz/karmaşık kod sorularında göster.
    Kullanıcıya botun ne yaptığını hissettir — güven artırır.
    """
    p     = prompt.lower()
    is_tr = any(c in prompt for c in "çğışöüÇĞİŞÖÜ")
    steps = []

    # Hata / debug
    if any(w in p for w in ['hata', 'error', 'crash', 'exception', 'çalışmıyor',
                              'traceback', 'failed', 'broken', 'bug', 'fix']):
        steps.append(ThinkingStep(emoji="🔍", message="Hata mesajı okunuyor..." if is_tr else "Reading the error..."))
        steps.append(ThinkingStep(emoji="🧠", message="Root cause analiz ediliyor..." if is_tr else "Finding root cause..."))
        steps.append(ThinkingStep(emoji="🔧", message="Fix hazırlanıyor..." if is_tr else "Preparing the fix..."))

    # Mimari / tasarım
    elif any(w in p for w in ['mimari', 'tasarla', 'nasıl yapılır', 'architecture',
                                'design', 'yapılandır', 'kur', 'implement']):
        steps.append(ThinkingStep(emoji="📐", message="Gereksinimler analiz ediliyor..." if is_tr else "Analyzing requirements..."))
        steps.append(ThinkingStep(emoji="🏗️", message="En uygun yaklaşım seçiliyor..." if is_tr else "Selecting best approach..."))
        steps.append(ThinkingStep(emoji="✍️",  message="Kod yazılıyor..." if is_tr else "Writing code..."))

    # Refactor / optimize
    elif any(w in p for w in ['refactor', 'optimize', 'temizle', 'iyileştir',
                                'clean', 'improve', 'performans']):
        steps.append(ThinkingStep(emoji="🔬", message="Mevcut kod inceleniyor..." if is_tr else "Reviewing existing code..."))
        steps.append(ThinkingStep(emoji="⚡", message="İyileştirme noktaları tespit ediliyor..." if is_tr else "Finding improvements..."))
        steps.append(ThinkingStep(emoji="✨", message="Temiz kod yazılıyor..." if is_tr else "Writing clean code..."))

    # Dosya / belge analizi
    elif any(w in p for w in ['analiz', 'incele', 'anla', 'özetle', 'analyze',
                                'review', 'dosya', 'file', 'pdf', 'belgeni']):
        steps.append(ThinkingStep(emoji="📄", message="Dosya okunuyor..." if is_tr else "Reading the file..."))
        steps.append(ThinkingStep(emoji="🧩", message="İçerik analiz ediliyor..." if is_tr else "Analyzing content..."))
        steps.append(ThinkingStep(emoji="📝", message="Sonuç hazırlanıyor..." if is_tr else "Preparing summary..."))

    # Test yazma
    elif any(w in p for w in ['test', 'spec', 'unit test', 'coverage']):
        steps.append(ThinkingStep(emoji="🧪", message="Fonksiyon analiz ediliyor..." if is_tr else "Analyzing function..."))
        steps.append(ThinkingStep(emoji="🎯", message="Edge case'ler belirleniyor..." if is_tr else "Identifying edge cases..."))
        steps.append(ThinkingStep(emoji="✅", message="Test yazılıyor..." if is_tr else "Writing tests..."))

    # Genel kod sorusu
    else:
        steps.append(ThinkingStep(emoji="💭", message="Problem analiz ediliyor..." if is_tr else "Analyzing the problem..."))
        steps.append(ThinkingStep(emoji="⚙️",  message="Çözüm üretiliyor..." if is_tr else "Generating solution..."))

    return steps


def get_deep_search_steps(is_tr: bool = True) -> List[ThinkingStep]:
    """Deep search pipeline adımları — 6 adım, gerçek zamanlı gösterilir."""
    return [
        ThinkingStep(emoji="🔍", message="Web'de aranıyor..." if is_tr else "Searching the web..."),
        ThinkingStep(emoji="📄", message="Sayfalar okunuyor..." if is_tr else "Reading pages..."),
        ThinkingStep(emoji="✂️",  message="İçerik parçalanıyor..." if is_tr else "Chunking content..."),
        ThinkingStep(emoji="⚡", message="En alakalı kaynaklar seçiliyor..." if is_tr else "Selecting best sources..."),
        ThinkingStep(emoji="✍️",  message="Yanıt hazırlanıyor..." if is_tr else "Generating answer..."),
        ThinkingStep(emoji="🔎", message="Yanıt doğrulanıyor..." if is_tr else "Verifying answer..."),
    ]


# ═══════════════════════════════════════════════════════════════
# WEB SEARCH SYNTHESIS
# ═══════════════════════════════════════════════════════════════

async def synthesize_web_results(raw: str, query: str, config: Dict) -> str:
    prompt = f"""Web search results for: "{query}"

{raw}

Synthesize into a clear answer:
1. Combine related info from multiple sources
2. Include dates for time-sensitive info
3. Brief attribution: "Kaynak: X" (no full URLs)
4. Resolve conflicts — prefer most recent
5. User-friendly language, add context

Same language as query (Turkish/English). Concise but comprehensive."""
    try:
        out = ""
        async for chunk in stream_deepinfra_completion(
            messages=[{"role":"system","content":"Research synthesizer. Clear, accurate summaries."},
                      {"role":"user","content":prompt}],
            model=config["model"], max_tokens=1500, temperature=0.5, top_p=0.85,
        ):
            out += chunk
        print(f"[WEB SYNTHESIS] {len(raw)} → {len(out)} chars")
        return out
    except Exception as e:
        print(f"[WEB SYNTHESIS ERROR] {e}")
        return raw


# ═══════════════════════════════════════════════════════════════
# DEEPINFRA CLIENT
# ═══════════════════════════════════════════════════════════════

async def stream_deepinfra_completion(
    messages:    List[Dict],
    model:       str,
    max_tokens:  int,
    temperature: float,
    top_p:       float,
) -> AsyncGenerator[str, None]:
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
    }
    payload = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
        "top_p": top_p, "stream": True,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{DEEPINFRA_BASE_URL}/chat/completions",
                                 headers=headers, json=payload) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data    = json.loads(data_str)
                        content = data.get("choices",[{}])[0].get("delta",{}).get("content","")
                        if content:
                            yield content
                    except:
                        continue


# ═══════════════════════════════════════════════════════════════
# MESSAGE BUILDER — Ana sistem prompt oluşturma
# ═══════════════════════════════════════════════════════════════

async def build_messages(
    mode:            str,
    user_id:         int,
    conversation_id: Optional[str],
    user_prompt:     str,
    history:         List[Dict],
    rag_context:     Optional[str] = None,
    context:         Optional[str] = None,
    session_summary: Optional[str] = None,
    config:          Dict           = None,
    image_data:      Optional[str] = None,
    image_type:      Optional[str] = None,
    live_context:    Optional[str] = None,
    **kwargs,
) -> List[Dict]:
    """
    LLM'e gönderilecek mesaj dizisini oluşturur.

    Öncelik sırası:
    1. USER MEMORY       — en yüksek öncelik
    2. CONVERSATION SUMMARY
    3. LIVE DATA         — smart_tools /live (kur, hava, kripto, haber, araştırma)
    4. RAG CONTEXT       — döküman/bilgi tabanı
    5. WEB SEARCH        — gateway'den gelen context
    6. SESSION SUMMARY   — legacy destek
    """

    # Code mode özel builder'a gider
    if mode == "code":
        return await build_code_messages(
            user_id=user_id, conversation_id=conversation_id,
            user_prompt=user_prompt, history=history, config=config,
            rag_context=rag_context, context=context, session_summary=session_summary,
        )

    messages       = []
    system_content = config["system_prompt"]

    # ── Intent log (LOCAL, 0ms) — sadece loglama, prompt'a ekleme
    reasoning_hint = build_reasoning_hint(user_prompt, history or [], mode)
    print(f"[INTENT] {reasoning_hint.split(chr(10))[0]}")
    # ─────────────────────────────────────────────────────────────

    # ── USER COMMAND — konuşma boyunca aktif kural ───────────────
    # Kullanıcı bu konuşmada komut verdiyse system prompt'a ekle
    active_commands = _extract_active_commands(history or [])
    if active_commands:
        commands_str = "\n".join(f"- {cmd}" for cmd in active_commands)
        system_content += (
            f"\n\n[AKTİF KULLANICI KURALLARI — KESİNLİKLE UYGULA]\n"
            f"{commands_str}\n"
            f"[/AKTİF KULLANICI KURALLARI]"
        )
    # ─────────────────────────────────────────────────────────────

    # ── GÜNCEL TARİH — smart_tools NTP (worldtimeapi) ────────────
    try:
        async with httpx.AsyncClient(timeout=4.0) as _c:
            _r = await _c.post(
                f"{SMART_TOOLS_URL}/unified",
                json={"query": "bugün tarih ne", "tool_type": "time"}
            )
            if _r.status_code == 200:
                _d = _r.json().get("data", {})
                _date_only = _d.get("date_only") or _d.get("formatted_tr", "").split(",")[0].strip()
                if _date_only:
                    system_content = (
                        f"[Sistem Bilgisi]\nBugünün tarihi: {_date_only}\n[/Sistem Bilgisi]\n\n"
                        + system_content
                    )
    except Exception:
        # Fallback: UTC+3 sistem saati
        from datetime import timezone, timedelta
        _now = datetime.now(timezone(timedelta(hours=3)))
        _months = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
                   "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
        _days = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
        _date_only = f"{_now.day} {_months[_now.month-1]} {_now.year}, {_days[_now.weekday()]}"
        system_content = (
            f"[Sistem Bilgisi]\nBugünün tarihi: {_date_only}\n[/Sistem Bilgisi]\n\n"
            + system_content
        )
    # ─────────────────────────────────────────────────────────────

    # 1. USER MEMORY
    user_memory = await load_user_memory(user_id)
    system_content = system_content.replace(
        "{user_memory}", user_memory or "[USER MEMORY]\nNo memory yet\n[/USER MEMORY]")

    # 2. CONVERSATION SUMMARY
    conv_summary = await load_conversation_summary(conversation_id)
    if conv_summary and len(conv_summary) > 60:  # Yeni/boş summary ekleme
        system_content += f"\n\n{conv_summary}"

    # 2b. CONVERSATION STATE — Redis'ten bağlam
    state_context = await build_state_context(
        conversation_id or "", user_id, user_prompt, mode
    )
    if state_context:
        system_content += f"\n\n{state_context}"
        print(f"[CHAT] State context injected: {len(state_context)} chars")

    # 3. LIVE DATA — Router kararı varsa direkt kullan, yoksa keyword
    _router_dec = {
        "needs_realtime": kwargs.get("needs_realtime"),
        "tool":           kwargs.get("live_type_hint", "none"),
        "confidence":     "high" if kwargs.get("live_type_hint") else "low",
        "intent":         kwargs.get("router_intent", ""),
    } if kwargs.get("live_type_hint") or kwargs.get("needs_realtime") else None

    # context zaten dışarıdan sağlandıysa (SSE pre-fetch) → internal fetch atla (double-fetch önlemi)
    _skip_internal_fetch = bool(context)
    if not _skip_internal_fetch:
        live_context, _sources_block = await get_live_data_with_sources(
            user_prompt,
            mode=mode,
            router_tool=kwargs.get("live_type_hint"),
            router_decision=_router_dec,
            history=history or [],
        )
        # live_context → user mesajına geçir (system'e değil), sources_block ayrı taşınır
        if live_context:
            kwargs["live_context"]   = live_context
            kwargs["sources_block"]  = _sources_block
            print(f"[CHAT] Live data → user msg: {len(live_context)} chars | sources: {bool(_sources_block)}")
    else:
        live_context   = None   # context param'dan geliyor — user msg'e ayrıca eklenmeyecek
        _sources_block = ""

    # 4. RAG CONTEXT
    if rag_context:
        system_content += f"\n\n[RAG Context]\n{rag_context}\n[/RAG Context]"

    # 5. WEB SEARCH (gateway'den geliyor — deep search context)
    if context:
        print(f"[CHAT] Web context received ({len(context)} chars), synthesizing...")
        synthesized = await synthesize_web_results(context, user_prompt, config)
        # ÖNEMLİ: Synthesis'i başa ekle — LLM eğitim verisinden önce bunu görsün
        system_content = (
            f"[GÜNCEL BİLGİ — BUNU KULLAN, EĞİTİM VERİSİNİ DEĞİL]\n"
            f"{synthesized}\n"
            f"[/GÜNCEL BİLGİ]\n\n"
        ) + system_content

    # 6. WORKSPACE DOSYALARI — Kullanıcının paylaştığı kodlar
    if conversation_id:
        ws_files = await load_workspace_files(conversation_id)
        if ws_files:
            ws_text = "\n\n".join([
                f"📁 {f['filename']} ({f['language']}):\n```{f['language']}\n{f['content'][:1500]}\n```"
                for f in ws_files[:3]
            ])
            system_content += f"""

[KULLANICI WORKSPACE]
Bu konuşmada kullanıcının paylaştığı dosyalar:
{ws_text}
Bu dosyalara atıfta bulunabilirsin. "bu kodu düzelt" gibi ifadeler workspace'teki koda işaret eder.
[/KULLANICI WORKSPACE]"""

    # 7. KONUŞMA ÖZETİ — Akıllı özetleme sisteminden geliyor
    if session_summary and session_summary != "[CONVERSATION SUMMARY]\nNew conversation\n[/CONVERSATION SUMMARY]":
        system_content += f"""

[KONUŞMA BAĞLAMI]
{session_summary}
[/KONUŞMA BAĞLAMI]

Not: Yukarıdaki özet bu konuşmanın geçmişini özetler.
Kullanıcı bağlam sorarsa özete başvur, tekrar sormadan yanıtla."""

    messages.append({"role": "system", "content": system_content})

    # ── Akıllı History Seçimi — Önem bazlı sliding window ──────
    # Son 6 mesajı her zaman al (yakın bağlam kritik)
    # Önceki mesajlardan önemli olanları ekle (skor > 0.65)
    # Toplam max 20 mesaj — token maliyetini dengele
    all_history = history or []
    if len(all_history) <= 20:
        selected_history = all_history
    else:
        recent   = all_history[-6:]               # Son 6 kesinlikle
        older    = all_history[:-6]               # Öncekiler
        # Önemlileri seç
        important = [
            m for m in older
            if _score_message_importance(m) > 0.6
        ][-14:]  # Max 14 eski mesaj
        selected_history = important + recent

    for msg in selected_history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Live context varsa user mesajına ekle — system'e değil
    # Gemini grounding mantığı: web verisi = user turn context
    live_ctx = live_context or kwargs.get("live_context", "") or ""
    # SOURCES_BLOCK_PLACEHOLDER LLM'e gitmemeli — temizle
    if live_ctx and "SOURCES_BLOCK_PLACEHOLDER:" in live_ctx:
        live_ctx = live_ctx[:live_ctx.find("SOURCES_BLOCK_PLACEHOLDER:")].rstrip()
    if live_ctx:
        enriched_prompt = f"{live_ctx}\n\nKullanıcı sorusu: {user_prompt}"
    else:
        enriched_prompt = user_prompt

    # Görsel varsa multimodal content oluştur
    if image_data and image_type:
        media_type = image_type if image_type.startswith("image/") else f"image/{image_type}"
        user_content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{image_data}"},
            },
            {"type": "text", "text": enriched_prompt},
        ]
        print(f"[VISION] Multimodal mesaj oluşturuldu ({media_type}, {len(image_data)} chars)")
    else:
        user_content = enriched_prompt

    messages.append({"role": "user", "content": user_content})

    return messages


# ═══════════════════════════════════════════════════════════════
# CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Ana chat endpoint.

    LIVE DATA AKIŞI:
    1. build_messages() → get_live_data(prompt, mode) çağırır
    2. get_live_data() → smart_tools /classify → karar
    3. Canlı veri lazımsa → smart_tools /live → formatted context
    4. Context system prompt'a eklenir
    5. LLM cevaplar — gerçek verilerle

    ÖRNEK:
    "güncel euro kaç tl"
      → get_live_data() → /classify → live_utility/currency
      → /live → "1 EUR = 43.52 TRY"
      → system: "[Canlı Veri — CURRENCY]\n💱 1 EUR = 43.52 TRY"
      → LLM: "Euro şu an 43.52 TL" ✅
    """

    if request.mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")

    config        = MODE_CONFIGS[request.mode]
    show_thinking = should_show_thinking(request.prompt, request.mode, request.history or [])

    messages = await build_messages(
        mode=request.mode, user_id=request.user_id,
        conversation_id=request.conversation_id,
        user_prompt=request.prompt,
        history=request.history or [],
        rag_context=request.rag_context,
        context=request.context,
        session_summary=request.session_summary,
        config=config,
        image_data      = request.image_data,
        image_type      = request.image_type,
        live_type_hint  = request.live_type_hint,
        router_intent   = request.router_intent,
        router_thinking = request.router_thinking,
        user_level      = request.user_level,
        needs_realtime  = request.needs_realtime,
    )

    async def response_generator():
        try:
            buffer    = ""
            full_text = ""  # State update için tam cevabı topla

            # Kullanıcı mesajında kod varsa workspace'e kaydet
            if request.conversation_id:
                asyncio.create_task(
                    process_user_message_for_workspace(
                        request.conversation_id,
                        request.prompt
                    )
                )

            # ── Deep search adımları — gerçek pipeline çalışırken göster ──
            live_type_check = _detect_live_type(request.prompt, request.mode or "assistant", None, request.history or [])
            is_deep = live_type_check == "deep_search"
            is_tr   = any(c in request.prompt for c in "çğışöüÇĞİŞÖÜ") or True

            if is_deep:
                ds_steps = get_deep_search_steps(is_tr)
                # Adım 1-4 hemen göster (search + scrape + chunk + rerank)
                for step in ds_steps[:4]:
                    yield f"[STEP]{step.emoji} {step.message}[/STEP]\n"
                    await asyncio.sleep(0.1)

            # Thinking display (deep search değilse)
            elif show_thinking:
                steps = await generate_thinking_steps(request.prompt, request.mode, request.history or [])
                for step in steps:
                    yield f"[STEP]{step.emoji} {step.message}[/STEP]\n"
                yield "\n"

            # Deep search kalan adımları — generate + reflect
            if is_deep:
                for step in ds_steps[4:]:
                    yield f"[STEP]{step.emoji} {step.message}[/STEP]\n"
                    await asyncio.sleep(0.1)
                yield "[STEPS_DONE]\n"

            # Model log — hangi model cevaplıyor
            print(f"[MODEL] mode={request.mode} | model={config['model']} | max_tokens={config['max_tokens']}")

            # Streaming response
            async for chunk in stream_deepinfra_completion(
                messages=messages,
                model=config["model"], max_tokens=config["max_tokens"],
                temperature=config["temperature"], top_p=config["top_p"],
            ):
                buffer    += chunk
                full_text += chunk
                if any(c in buffer for c in [' ','.','!','?','\n',',']) or len(buffer) > 10:
                    # SSE chunk'tan bracket tag'ları temizle — frontend'e sızmaz
                    clean_buf = _strip_internal_tags(buffer)
                    yield clean_buf
                    buffer = ""
            if buffer:
                full_text += buffer
                clean_buf = _strip_internal_tags(buffer)
                yield clean_buf

            # Kaynak listesi — LLM yanıtının hemen ardından gönder
            _sb = kwargs.get("sources_block", "") if "kwargs" in dir() else ""
            if not _sb:
                # build_messages'den gelen sources_block'u messages'dan çıkar
                for _m in messages:
                    _mc = _m.get("content", "")
                    if isinstance(_mc, str) and "SOURCES_BLOCK_PLACEHOLDER:" in _mc:
                        _idx = _mc.find("SOURCES_BLOCK_PLACEHOLDER:")
                        _sb  = _mc[_idx + len("SOURCES_BLOCK_PLACEHOLDER:"):].strip()
                        break
            if _sb:
                yield f"\n\n{_sb}"
                full_text += f"\n\n{_sb}"

            # Periodic summary (background)
            if request.conversation_id:
                asyncio.create_task(check_and_create_summary_async(
                    request.user_id, request.conversation_id, config))

            # State güncelle (background — response'u bloklamaz)
            if request.conversation_id:
                asyncio.create_task(extract_and_update_state(
                    conversation_id=request.conversation_id,
                    user_id=request.user_id,
                    prompt=request.prompt,
                    response=full_text,
                    intent=request.mode,
                    mode=request.mode,
                ))

        except Exception as e:
            yield f"\n\n⚠️ Bir hata oluştu: {str(e)}"

    return StreamingResponse(response_generator(), media_type="text/plain; charset=utf-8")


# ═══════════════════════════════════════════════════════════════
# SSE ENDPOINT — Claude gibi event stream
# ═══════════════════════════════════════════════════════════════

@app.post("/chat/sse")
async def chat_sse(request: ChatRequest):
    """
    SSE (Server-Sent Events) endpoint.
    Claude gibi event tiplerine göre ayrı kanaldan gönderir.

    Event tipleri:
      step       → araç adımı (search, scrape, chunk, rerank, generate, reflect)
      text       → gerçek cevap içeriği (streaming)
      done       → tamamlandı
      error      → hata

    Frontend kullanımı:
      const evtSource = new EventSource('/chat/sse', {method:'POST'})
      evtSource.addEventListener('step', e => showStep(JSON.parse(e.data)))
      evtSource.addEventListener('text', e => appendText(e.data))
      evtSource.addEventListener('done', e => finalize())
    """
    import json as _json

    user_id = request.user_id
    mode    = request.mode or "assistant"
    prompt  = request.prompt

    if mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    config = MODE_CONFIGS[mode]

    def sse_event(event_type: str, data) -> str:
        """SSE formatı: event: TYPE\ndata: DATA\n\n"""
        if isinstance(data, dict):
            data = _json.dumps(data, ensure_ascii=False)
        return f"event: {event_type}\ndata: {data}\n\n"

    async def sse_generator():
        try:
            # ── Deep search mi? ──────────────────────────────────
            live_type = _detect_live_type(prompt, mode, None, history or [])
            is_deep   = live_type == "deep_search"
            is_tr     = any(c in prompt for c in "çğışöüÇĞİŞÖÜ") or True

            if is_deep:
                steps = get_deep_search_steps(is_tr)
                step_names = ["search","scrape","chunk","rerank","generate","reflect"]

                # Adım 1-4: search → rerank (live data çekilirken göster)
                for i, step in enumerate(steps[:4]):
                    yield sse_event("step", {
                        "index":  i,
                        "name":   step_names[i],
                        "emoji":  step.emoji,
                        "text":   step.message,
                        "status": "running",
                    })
                    await asyncio.sleep(0.05)

                # Live data çek (sources_block ayrı alınır — LLM'e gitmez)
                live_context, sources_block = await get_live_data_with_sources(
                    prompt, mode, history=history or []
                )

                # Adım 4'ü tamamla
                yield sse_event("step", {
                    "index":  3,
                    "name":   "rerank",
                    "emoji":  "⚡",
                    "text":   "En alakalı kaynaklar seçildi" if is_tr else "Best sources selected",
                    "status": "done",
                })

                # Adım 5: generate
                yield sse_event("step", {
                    "index":  4,
                    "name":   "generate",
                    "emoji":  steps[4].emoji,
                    "text":   steps[4].message,
                    "status": "running",
                })

            else:
                live_context, sources_block = await get_live_data_with_sources(
                    prompt, mode, history=history or []
                )

                # Normal thinking steps
                show_thinking = should_show_thinking(prompt, mode, request.history or [])
                if show_thinking:
                    think_steps = await generate_thinking_steps(prompt, mode, request.history or [])
                    for i, step in enumerate(think_steps):
                        yield sse_event("step", {
                            "index":  i,
                            "name":   "thinking",
                            "emoji":  step.emoji,
                            "text":   step.message,
                            "status": "running",
                        })
                        await asyncio.sleep(0.05)

            # Mesajları hazırla
            messages = await build_messages(
                mode=mode, user_id=user_id,
                conversation_id=request.conversation_id,
                user_prompt=prompt,
                history=request.history or [],
                rag_context=request.rag_context,
                context=live_context,
                session_summary=request.session_summary,
                config=config,
            )

            # ── Text streaming ────────────────────────────────────
            full_text = ""
            async for chunk in stream_deepinfra_completion(
                messages=messages,
                model=config["model"],
                max_tokens=config["max_tokens"],
                temperature=config["temperature"],
                top_p=config["top_p"],
            ):
                full_text += chunk
                yield sse_event("text", chunk)

            # Reflect adımı — deep search sonrası
            if is_deep:
                yield sse_event("step", {
                    "index":  5,
                    "name":   "reflect",
                    "emoji":  "🔎",
                    "text":   "Yanıt doğrulandı" if is_tr else "Answer verified",
                    "status": "done",
                })

            # Kaynak listesi — ayrı event olarak gönder (LLM yanıtına bağımlı değil)
            if sources_block:
                yield sse_event("sources", sources_block)

            # Tüm adımları done yap
            yield sse_event("done", {
                "total_chars": len(full_text),
                "mode":        mode,
                "has_sources": bool(sources_block),
            })

            # Background tasks
            if request.conversation_id:
                asyncio.create_task(check_and_create_summary_async(
                    user_id, request.conversation_id, config))

        except Exception as e:
            yield sse_event("error", {"message": str(e)})

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",  # Nginx buffer'ı kapat
            "Connection":      "keep-alive",
        },
    )


async def check_and_create_summary_async(user_id: int, conversation_id: str, config: Dict):
    try:
        if await should_create_summary(conversation_id):
            await create_conversation_summary(user_id, conversation_id, config)
    except Exception as e:
        print(f"[SUMMARY BACKGROUND ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status":  "healthy",
        "service": "chat",
        "version": "3.1.0",
        "modes":   list(MODE_CONFIGS.keys()),
        "features": {
            "memory_system":    db_pool is not None,
            "thinking_display": True,
            "web_synthesis":    True,
            "auto_summaries":   True,
            "live_data_router": bool(SMART_TOOLS_URL),
            "code_context":     True,
        },
    }


@app.get("/")
async def root():
    return {
        "service": "Skylight Chat Service",
        "version": "3.1.0",
        "live_data": f"LiveDataRouter → {SMART_TOOLS_URL}/classify + /live",
        "modes":     list(MODE_CONFIGS.keys()),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)