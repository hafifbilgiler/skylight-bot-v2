"""
═══════════════════════════════════════════════════════════════
DEĞERLİ METALLER — Altın/Gümüş Haber + AI Tahmin (Finans Eklentisi)
═══════════════════════════════════════════════════════════════
Mevcut app.py'ye eklenir (JOHN gibi register pattern).
Yeni endpoint:
  GET /metals/predict?metal=gold   — haber çeker + AI tahmin döner

app.py'nin sonuna şunu ekle (john'un yanına):
  from metals_addon import register_metals
  register_metals(app, _sys.modules[__name__])

Dockerfile'a ekle:
  COPY metals_addon.py .
═══════════════════════════════════════════════════════════════
"""
import json
import time as _time
from datetime import datetime, timezone
from typing import Optional, List, Dict

import httpx
from fastapi import Query


# ─────────────────────────────────────────────────────────────
# State + config
# ─────────────────────────────────────────────────────────────
_app_module = None  # register sırasında set edilir

_metal_predict_cache: Dict[str, Dict] = {}   # metal -> {data, ts}
_METAL_PREDICT_TTL = 30 * 60                  # 30 dk cache

METAL_CONFIG = {
    "gold": {
        "name": "Altın", "symbol": "XAU", "emoji": "🥇",
        "tr_name": "altın", "yahoo": "GC=F",
    },
    "silver": {
        "name": "Gümüş", "symbol": "XAG", "emoji": "🥈",
        "tr_name": "gümüş", "yahoo": "SI=F",
    },
}

_POS_WORDS = ["surge", "rally", "gain", "record", "high", "rise", "soar", "boom",
              "climb", "jump", "strengthen", "safe haven", "demand", "bullish"]
_NEG_WORDS = ["drop", "fall", "decline", "crash", "plunge", "weak", "slump",
              "tumble", "sink", "pressure", "sell-off", "bearish"]


def _sentiment(text: str) -> str:
    text = text.lower()
    p = sum(1 for w in _POS_WORDS if w in text)
    n = sum(1 for w in _NEG_WORDS if w in text)
    return "pozitif" if p > n else "negatif" if n > p else "nötr"


# ─────────────────────────────────────────────────────────────
# Haber çekme
# ─────────────────────────────────────────────────────────────
async def _fetch_metal_news(metal: str) -> List[Dict]:
    """Altın/gümüş haberlerini çeker (Yahoo Finance + CryptoCompare Commodity)."""
    cfg = METAL_CONFIG.get(metal, METAL_CONFIG["gold"])
    news: List[Dict] = []

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (compatible; ONE-BUNE/1.0)"}) as client:

        # ── Kaynak 1: Yahoo Finance news ────────────────
        try:
            r = await client.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": cfg["yahoo"], "newsCount": 8, "quotesCount": 0},
            )
            if r.status_code == 200:
                for item in r.json().get("news", [])[:8]:
                    title = item.get("title", "")
                    if not title:
                        continue
                    news.append({
                        "title": title, "body": "",
                        "source": item.get("publisher", "Yahoo Finance"),
                        "sentiment": _sentiment(title),
                        "url": item.get("link", ""),
                        "published_at": item.get("providerPublishTime", 0),
                        "image": "",
                    })
                print(f"[METAL NEWS] Yahoo {metal}: {len(news)}")
        except Exception as e:
            print(f"[METAL NEWS] Yahoo hata: {e}")

        # ── Kaynak 2: CryptoCompare Commodity (yedek) ──
        if len(news) < 4:
            try:
                r = await client.get(
                    "https://min-api.cryptocompare.com/data/v2/news/",
                    params={"categories": "Commodity", "lang": "EN", "sortOrder": "latest"},
                )
                if r.status_code == 200:
                    kw = "gold" if metal == "gold" else "silver"
                    for item in r.json().get("Data", [])[:8]:
                        title = item.get("title", "")
                        if kw not in title.lower():
                            continue
                        news.append({
                            "title": title, "body": item.get("body", "")[:400],
                            "source": item.get("source_info", {}).get("name", "CryptoCompare"),
                            "sentiment": _sentiment(title),
                            "url": item.get("url", ""),
                            "published_at": item.get("published_on", 0),
                            "image": item.get("imageurl", ""),
                        })
                    print(f"[METAL NEWS] CryptoCompare toplam: {len(news)}")
            except Exception as e:
                print(f"[METAL NEWS] CryptoCompare hata: {e}")

    # Sırala + dedupe
    news.sort(key=lambda x: x.get("published_at", 0), reverse=True)
    seen, unique = set(), []
    for n in news:
        k = n["title"][:80].lower()
        if k not in seen:
            seen.add(k)
            unique.append(n)
    return unique[:8]


# ─────────────────────────────────────────────────────────────
# AI tahmin
# ─────────────────────────────────────────────────────────────
_PREDICT_SYSTEM = """Sen ONE-BUNE'nin değerli metal analiz uzmanısın.
Altın ve gümüş piyasalarını haberlere dayanarak yorumlarsın.

DÜRÜSTLÜK KURALLARI:
• ASLA kesin fiyat tahmini yapma ("X olacak" deme)
• "Yüksek ihtimalle", "%X olasılıkla", "olabilir" gibi olasılıksal dil kullan
• Neden'i açıkla (hangi haber/faktör neyi etkiliyor)
• Riskleri belirt (izlenecek faktörler)
• Kısa ve net ol — 3-4 cümle

ÇIKTI FORMATI — SADECE JSON:
{
  "direction": "yükseliş" | "düşüş" | "nötr",
  "probability": 55-75 arası sayı,
  "summary": "3-4 cümle Türkçe analiz, olasılıksal dil, neden + risk",
  "key_factors": ["faktör 1", "faktör 2"]
}
SADECE JSON döndür."""


async def _predict_metal(metal: str, news: List[Dict], price_info: Dict) -> Dict:
    """Haber + fiyat → AI tahmin. AI yoksa sentiment fallback."""
    cfg = METAL_CONFIG.get(metal, METAL_CONFIG["gold"])
    api_key = getattr(_app_module, "DEEPINFRA_API_KEY", "")
    base_url = getattr(_app_module, "DEEPINFRA_BASE_URL", "")
    model = getattr(_app_module, "FINANS_LLM_MODEL", "")

    if not api_key or not news:
        return _predict_fallback(metal, news)

    headlines = "\n".join(f"- {n['title']} ({n['sentiment']})" for n in news[:6])
    prompt = (
        f"{cfg['tr_name'].capitalize()} piyasası analizi:\n\n"
        f"Güncel fiyat: ${price_info.get('usd', 0)}/ons (₺{price_info.get('try', 0)}/gram)\n\n"
        f"Son haberler:\n{headlines}\n\n"
        f"Bu haberlere ve fiyata dayanarak {cfg['tr_name']} için kısa vadeli tahmin yap. "
        f"Kesin fiyat verme, olasılık ve yön belirt. Neden'i ve riskleri açıkla."
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _PREDICT_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 400, "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                },
            )
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"].strip()
                import re as _re
                m = _re.search(r'\{.*\}', raw, _re.DOTALL)
                if m:
                    pred = json.loads(m.group(0))
                    return {
                        "direction": pred.get("direction", "nötr"),
                        "probability": int(pred.get("probability", 55)),
                        "summary": pred.get("summary", ""),
                        "key_factors": (pred.get("key_factors") or [])[:4],
                    }
    except Exception as e:
        print(f"[METAL PREDICT] {e}")

    return _predict_fallback(metal, news)


def _predict_fallback(metal: str, news: List[Dict]) -> Dict:
    """AI yoksa haber sentiment'ine göre basit tahmin."""
    cfg = METAL_CONFIG.get(metal, METAL_CONFIG["gold"])
    pos = sum(1 for n in news if n.get("sentiment") == "pozitif")
    neg = sum(1 for n in news if n.get("sentiment") == "negatif")
    if pos > neg:
        direction, prob = "yükseliş", min(50 + (pos - neg) * 5, 70)
        summary = (f"Haberler ağırlıklı olumlu görünüyor. {cfg['tr_name'].capitalize()} kısa vadede "
                   f"yukarı eğilimli olabilir, ancak kesin değil — piyasa koşullarını izleyin.")
    elif neg > pos:
        direction, prob = "düşüş", min(50 + (neg - pos) * 5, 70)
        summary = (f"Haberlerde satış baskısı öne çıkıyor. {cfg['tr_name'].capitalize()} kısa vadede "
                   f"geri çekilebilir, dikkatli olun.")
    else:
        direction, prob = "nötr", 50
        summary = (f"Haberler karışık sinyaller veriyor. {cfg['tr_name'].capitalize()} için belirgin "
                   f"bir yön yok, kararsızlık dönemi.")
    return {"direction": direction, "probability": prob, "summary": summary,
            "key_factors": ["Haber sentimenti", "Dolar endeksi"]}


# ─────────────────────────────────────────────────────────────
# REGISTER — app.py'den çağrılır
# ─────────────────────────────────────────────────────────────
def register_metals(app, app_module):
    """Metal tahmin endpoint'ini app'e bağla."""
    global _app_module
    _app_module = app_module

    @app.get("/metals/predict")
    async def metal_predict(metal: str = Query("gold", enum=["gold", "silver"])):
        # Cache kontrolü
        cached = _metal_predict_cache.get(metal)
        if cached and (_time.time() - cached["ts"]) < _METAL_PREDICT_TTL:
            return {**cached["data"], "_cached": True}

        cfg = METAL_CONFIG.get(metal, METAL_CONFIG["gold"])

        # 1. Fiyatı mevcut get_metals()'tan al
        price_info = {"usd": 0, "try": 0}
        try:
            metals_data = await app_module.get_metals()
            mdict = metals_data.get("metals", {})
            base = mdict.get(cfg["symbol"], {})
            if metal == "gold":
                gram = mdict.get("XAU_GR", {})
                price_info = {"usd": base.get("usd", 0), "try": gram.get("try", 0)}
            else:
                # gümüş gram = ons/31.1035
                gram_try = (base.get("try", 0) / 31.1035) if base.get("try") else 0
                price_info = {"usd": base.get("usd", 0), "try": round(gram_try, 2)}
        except Exception as e:
            print(f"[METAL PREDICT] fiyat alınamadı: {e}")

        # 2. Haber çek
        news = await _fetch_metal_news(metal)

        # 3. AI tahmin
        prediction = await _predict_metal(metal, news, price_info)

        result = {
            "metal": metal,
            "name": cfg["name"],
            "emoji": cfg["emoji"],
            "price": price_info,
            "prediction": prediction,
            "news": news,
            "news_count": len(news),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "disclaimer": ("Bu analiz haberlere dayalı otomatik bir tahmindir. "
                           "Yatırım tavsiyesi değildir. Kararı kendiniz verin."),
        }
        _metal_predict_cache[metal] = {"data": result, "ts": _time.time()}
        return result

    print("[METALS] ✅ Endpoint register edildi: /metals/predict")