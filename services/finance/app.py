"""
ONE-BUNE Finans Servisi
=======================
Binance WebSocket → Hareket tespiti → DeepInfra LLM yorum
"""

import asyncio, json, os, time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
import jwt as _jwt

import httpx, websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────
BINANCE_WS_URL   = "wss://stream.binance.com:9443/ws"
BINANCE_REST_URL = "https://api.binance.com/api/v3"
WHALE_USD_THRESH = float(os.getenv("WHALE_USD_THRESH", "500000"))

DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
# En ucuz + yeterince akıllı model — $0.05/1M token
FINANS_LLM_MODEL   = os.getenv("FINANS_LLM_MODEL", "Qwen/Qwen3.5-4B")

JWT_SECRET    = os.getenv("JWT_SECRET", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
JWT_ALGORITHM = "HS256"

def verify_token(token: str) -> bool:
    """Token geçerli mi kontrol et."""
    if not token:
        return False
    try:
        _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return True
    except Exception:
        return False

SUPPORTED_COINS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT",
    "XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT",
    "LINKUSDT","DOTUSDT","MATICUSDT","UNIUSDT",
]
INTERVALS = ["1m","5m","15m","1h","4h","1d"]

app = FastAPI(title="ONE-BUNE Finans", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── State ─────────────────────────────────────────────────────
price_cache:   Dict[str, float]           = {}
kline_cache:   Dict[str, Dict]            = {}
whale_history: Dict[str, deque]           = defaultdict(lambda: deque(maxlen=100))
signal_history:Dict[str, deque]           = defaultdict(lambda: deque(maxlen=50))
subscribers:   Dict[str, Set[WebSocket]]  = defaultdict(set)
all_clients:   Set[WebSocket]             = set()

# ──────────────────────────────────────────────────────────────
# TEKNİK ANALİZ
# ──────────────────────────────────────────────────────────────

def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)

def calc_ema(values: List[float], period: int) -> List[float]:
    if len(values) < period: return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema

def calc_macd(closes: List[float]) -> Dict:
    e12 = calc_ema(closes, 12)
    e26 = calc_ema(closes, 26)
    if not e12 or not e26: return {}
    n = min(len(e12), len(e26))
    line = [e12[-(n-i)] - e26[-(n-i)] for i in range(n)]
    sig  = calc_ema(line, 9)
    if not sig: return {}
    hist = line[-1] - sig[-1]
    return {"macd": round(line[-1],6), "signal": round(sig[-1],6),
            "histogram": round(hist,6),
            "trend": "bullish" if hist > 0 else "bearish"}

def calc_bollinger(closes: List[float], period: int = 20) -> Dict:
    if len(closes) < period: return {}
    w   = closes[-period:]
    mid = sum(w) / period
    std = (sum((x - mid)**2 for x in w) / period) ** 0.5
    return {"upper": round(mid + 2*std, 4), "mid": round(mid, 4),
            "lower": round(mid - 2*std, 4), "width": round(4*std/mid*100, 2)}

def detect_candle_patterns(klines: List[Dict]) -> List[Dict]:
    """Son 3 mum üzerinden formasyon tespiti."""
    if len(klines) < 3: return []
    patterns = []
    c  = klines[-1]
    p  = klines[-2]
    pp = klines[-3]

    o,h,l,cl   = float(c["o"]),float(c["h"]),float(c["l"]),float(c["c"])
    po,pcl     = float(p["o"]),float(p["c"])
    ppo,ppcl   = float(pp["o"]),float(pp["c"])

    body   = abs(cl - o)
    candle = h - l
    upper  = h - max(o, cl)
    lower  = min(o, cl) - l

    if candle < 0.0001: return patterns

    # Çekiç
    if lower > body * 2 and upper < body * 0.5:
        patterns.append({"name":"Çekiç","direction":"bullish","strength":"medium",
                          "emoji":"🔨","desc":"Dip formasyonu — alım sinyali"})
    # Ters Çekiç
    if upper > body * 2 and lower < body * 0.5:
        patterns.append({"name":"Ters Çekiç","direction":"neutral","strength":"weak",
                          "emoji":"🔨","desc":"Tersine dönüş adayı"})
    # Doji
    if body < candle * 0.1:
        patterns.append({"name":"Doji","direction":"neutral","strength":"weak",
                          "emoji":"〒","desc":"Kararsızlık — trend değişimi olabilir"})
    # Ayı Yutan
    if o > po and cl < po and cl < pcl and o > pcl:
        patterns.append({"name":"Ayı Yutan","direction":"bearish","strength":"strong",
                          "emoji":"🐻","desc":"Güçlü satış sinyali"})
    # Boğa Yutan
    if o < po and cl > po and cl > pcl and o < pcl:
        patterns.append({"name":"Boğa Yutan","direction":"bullish","strength":"strong",
                          "emoji":"🐂","desc":"Güçlü alım sinyali"})
    # Shooting Star
    if upper > body * 2 and lower < body * 0.3 and cl < o:
        patterns.append({"name":"Shooting Star","direction":"bearish","strength":"medium",
                          "emoji":"⭐","desc":"Tepe formasyonu — satış sinyali"})
    # Morning Star
    if (ppcl < ppo and body < candle*0.2 and
        cl > (ppo + ppcl)/2):
        patterns.append({"name":"Morning Star","direction":"bullish","strength":"strong",
                          "emoji":"🌅","desc":"3 mum alım formasyonu"})
    # Marubozu
    if upper < candle*0.02 and lower < candle*0.02:
        if cl > o:
            patterns.append({"name":"Boğa Marubozu","direction":"bullish","strength":"strong",
                              "emoji":"📈","desc":"Güçlü alım baskısı"})
        else:
            patterns.append({"name":"Ayı Marubozu","direction":"bearish","strength":"strong",
                              "emoji":"📉","desc":"Güçlü satış baskısı"})
    return patterns

def detect_signals(symbol: str, interval: str = "1h") -> Dict:
    """RSI, MACD, BB, EMA, hacim + mum formasyonları → sinyal üret."""
    klines_raw = list(kline_cache.get(symbol, {}).get(interval, []))
    if len(klines_raw) < 30:
        return {"error": "Yetersiz veri", "symbol": symbol}

    closes  = [float(k["c"]) for k in klines_raw]
    volumes = [float(k["v"]) for k in klines_raw]

    rsi  = calc_rsi(closes)
    macd = calc_macd(closes)
    bb   = calc_bollinger(closes)
    e9   = calc_ema(closes, 9)
    e21  = calc_ema(closes, 21)
    e50  = calc_ema(closes, 50)
    patterns = detect_candle_patterns(klines_raw[-3:])

    price     = closes[-1]
    avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes)/len(volumes)
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else 1.0

    # EMA trend
    trend = "nötr"
    if e9 and e21 and e50:
        if e9[-1] > e21[-1] > e50[-1]: trend = "güçlü yükseliş"
        elif e9[-1] < e21[-1] < e50[-1]: trend = "güçlü düşüş"
        elif e9[-1] > e21[-1]: trend = "kısa vadeli yükseliş"
        else: trend = "kısa vadeli düşüş"

    # BB konumu
    bb_pos = "orta"
    if bb:
        if price >= bb["upper"]: bb_pos = "üst band üstü — aşırı alım"
        elif price <= bb["lower"]: bb_pos = "alt band altı — aşırı satım"
        elif price > bb["mid"]: bb_pos = "üst bölge"
        else: bb_pos = "alt bölge"

    # RSI yorumu
    rsi_comment = "nötr"
    if rsi:
        if rsi > 70: rsi_comment = "aşırı alım"
        elif rsi < 30: rsi_comment = "aşırı satım"
        elif rsi > 55: rsi_comment = "güçlü"
        elif rsi < 45: rsi_comment = "zayıf"

    # Genel sinyal skoru
    score = 0
    reasons = []
    if rsi:
        if rsi < 30: score += 2; reasons.append("RSI aşırı satım")
        elif rsi < 40: score += 1; reasons.append("RSI zayıf")
        elif rsi > 70: score -= 2; reasons.append("RSI aşırı alım")
        elif rsi > 60: score -= 1; reasons.append("RSI güçlü")
    if macd.get("trend") == "bullish": score += 1; reasons.append("MACD bullish")
    elif macd.get("trend") == "bearish": score -= 1; reasons.append("MACD bearish")
    if "yükseliş" in trend: score += 1; reasons.append(f"Trend: {trend}")
    elif "düşüş" in trend: score -= 1; reasons.append(f"Trend: {trend}")
    if vol_ratio > 1.5: reasons.append(f"Yüksek hacim ({vol_ratio}x)")
    for p in patterns:
        if p["direction"] == "bullish": score += (2 if p["strength"]=="strong" else 1)
        elif p["direction"] == "bearish": score -= (2 if p["strength"]=="strong" else 1)
        reasons.append(f"{p['emoji']} {p['name']}")

    if score >= 3: overall = "GÜÇLÜ ALIIM"
    elif score >= 1: overall = "ZAYIF ALIM"
    elif score <= -3: overall = "GÜÇLÜ SATIŞ"
    elif score <= -1: overall = "ZAYIF SATIŞ"
    else: overall = "BEKLE"

    return {
        "symbol": symbol, "interval": interval, "price": price,
        "trend": trend,
        "rsi": {"value": rsi, "comment": rsi_comment},
        "macd": macd,
        "bollinger": {**bb, "position": bb_pos} if bb else {},
        "ema": {
            "ema9":  round(e9[-1], 4)  if e9  else None,
            "ema21": round(e21[-1], 4) if e21 else None,
            "ema50": round(e50[-1], 4) if e50 else None,
        },
        "volume": {"current": volumes[-1], "avg20": round(avg_vol,2),
                   "ratio": vol_ratio,
                   "alert": vol_ratio > 2.0},
        "candle_patterns": patterns,
        "signal": {"score": score, "overall": overall, "reasons": reasons},
        "whales_recent": list(whale_history[symbol])[-5:],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ──────────────────────────────────────────────────────────────
# LLM YORUM — DeepInfra Qwen3.5-4B (ucuz, hızlı)
# ──────────────────────────────────────────────────────────────

async def llm_interpret(symbol: str, analysis: Dict) -> str:
    """DeepInfra Qwen3.5-4B ile teknik analiz yorumu."""
    if not DEEPINFRA_API_KEY:
        return _rule_based_comment(analysis)

    sig     = analysis.get("signal", {})
    rsi     = analysis.get("rsi", {})
    macd    = analysis.get("macd", {})
    vol     = analysis.get("volume", {})
    pat     = analysis.get("candle_patterns", [])
    whales  = analysis.get("whales_recent", [])

    prompt = f"""{symbol} kripto teknik analizi — kısa Türkçe yorum yaz, max 5 madde, emoji kullan.

Fiyat: ${analysis.get('price','?')} | Trend: {analysis.get('trend','?')}
RSI: {rsi.get('value','?')} ({rsi.get('comment','?')})
MACD: {macd.get('trend','?')} (hist: {macd.get('histogram','?')})
BB: {analysis.get('bollinger',{}).get('position','?')}
Hacim: {vol.get('ratio','?')}x ortalama {'⚠️ YÜK HACIM' if vol.get('alert') else ''}
Formasyonlar: {', '.join(p['name'] for p in pat) or 'Yok'}
Whale işlem: {len(whales)} adet
Sinyal: {sig.get('overall','?')} (skor: {sig.get('score',0)})
Sebepler: {', '.join(sig.get('reasons',[]))}

Kısa değerlendirme ve AL/SAT/BEKLE önerisi:"""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": FINANS_LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.3,
                }
            )
            data = r.json()
            text = data["choices"][0]["message"]["content"].strip()
            print(f"[LLM] ✅ {symbol} | {len(text)} chars | model={FINANS_LLM_MODEL}")
            return text
    except Exception as e:
        print(f"[LLM] ❌ {e} — kural tabanlı yorum kullanılıyor")
        return _rule_based_comment(analysis)

def _rule_based_comment(analysis: Dict) -> str:
    """LLM yoksa kural tabanlı yorum."""
    sig = analysis.get("signal", {})
    lines = [
        f"📊 **{analysis.get('symbol')}** — ${analysis.get('price',0):,.4f}",
        f"📈 Trend: {analysis.get('trend','?')}",
        f"⚡ RSI: {analysis.get('rsi',{}).get('value','?')} — {analysis.get('rsi',{}).get('comment','?')}",
        f"📉 MACD: {analysis.get('macd',{}).get('trend','?')}",
    ]
    if analysis.get("candle_patterns"):
        lines.append("🕯 " + " | ".join(p["emoji"]+" "+p["name"] for p in analysis["candle_patterns"]))
    lines.append(f"{'✅' if 'ALIM' in sig.get('overall','') else '⚠️' if 'SATIŞ' in sig.get('overall','') else '⏳'} **{sig.get('overall','BEKLE')}**")
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────
# OTOMATİK FİNANS YORUMCUSU
# Kullanıcı sormaz — sistem her N dakikada bir yorum üretir.
# Kesin öneri değil, olasılıksal yorum.
# ──────────────────────────────────────────────────────────────

COMMENTARY_PROMPT = """Sen ONE-BUNE'nin kripto finans yorumcususun.
Sana gelen SADECE bu sistem verilerini kullan — dışarıdan bilgi ekleme.
Yorumunu şu kurallara göre yap:

DÜRÜSTLÜK KURALLARI:
• "Kesinlikle" veya "mutlaka" kullanma — piyasa tahmin edilemez
• "Yüksek ihtimalle", "%X olasılıkla", "güçlü sinyal" gibi ifadeler kullan
• Çelişkili sinyaller varsa bunu belirt
• Yorumun kısa olsun — max 4 madde, emoji ile başlasın

FORMAT:
📊 [Genel Durum — 1 cümle]
📈 veya 📉 [Trend yorumu — ihtimalle]
⚡ [RSI/MACD yorumu — ne anlama geliyor]
🐋 [Whale aktivitesi varsa — varsa belirt, yoksa yazma]
🎯 [Olası senaryo — "eğer X olursa Y ihtimali artar" formatında]

Türkçe yaz. Finansal tavsiye değil, veri yorumu yap."""

async def generate_commentary(symbol: str, interval: str = "1h") -> str:
    """
    Mevcut teknik analizi DeepInfra ile yorumla.
    Otomatik olarak çalışır — kullanıcı sormaz.
    """
    if not DEEPINFRA_API_KEY:
        return _auto_commentary(symbol, interval)

    analysis = detect_signals(symbol, interval)
    if "error" in analysis:
        return "⏳ Veri yükleniyor..."

    sig      = analysis.get("signal", {})
    rsi      = analysis.get("rsi", {})
    macd     = analysis.get("macd", {})
    vol      = analysis.get("volume", {})
    patterns = analysis.get("candle_patterns", [])
    whales   = analysis.get("whales_recent", [])
    bb       = analysis.get("bollinger", {})

    data_summary = f"""
{symbol} | {interval} | Fiyat: ${analysis.get('price', 0):,.4f}
Trend: {analysis.get('trend')}
RSI: {rsi.get('value')} ({rsi.get('comment')})
MACD: {macd.get('trend')} | Histogram: {macd.get('histogram', 0):+.6f}
Bollinger: {bb.get('position', 'N/A')} | Genişlik: {bb.get('width', 0):.1f}%
Hacim: {vol.get('ratio', 1):.1f}x ortalama {'⚠️ ANOMALİ' if vol.get('alert') else ''}
Mum Formasyonları: {', '.join(p['name'] + '(' + p['direction'] + ')' for p in patterns) or 'Yok'}
Sinyal Skoru: {sig.get('score', 0):+d} → {sig.get('overall')}
Sebepler: {', '.join(sig.get('reasons', []))}
Son Whale: {len(whales)} işlem {'| En büyük: $' + f"{max((w.get('usd',0) for w in whales), default=0):,.0f}" if whales else ''}
"""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model":       FINANS_LLM_MODEL,
                    "messages":    [
                        {"role": "system", "content": COMMENTARY_PROMPT},
                        {"role": "user",   "content": f"/no_think\nŞu anki {symbol} piyasa verilerini yorumla:\n{data_summary}"},
                    ],
                    "max_tokens":  350,
                    "temperature": 0.4,
                }
            )
            text = r.json()["choices"][0]["message"]["content"].strip()
            print(f"[COMMENTARY] ✅ {symbol} | {len(text)} chars")
            return text
    except Exception as e:
        print(f"[COMMENTARY] ❌ {e}")
        return _auto_commentary(symbol, interval)


def _auto_commentary(symbol: str, interval: str) -> str:
    """LLM yoksa kural tabanlı otomatik yorum."""
    analysis = detect_signals(symbol, interval)
    if "error" in analysis:
        return "⏳ Veri bekleniyor..."

    sig      = analysis.get("signal", {})
    rsi      = analysis.get("rsi", {})
    vol      = analysis.get("volume", {})
    whales   = analysis.get("whales_recent", [])
    patterns = analysis.get("candle_patterns", [])
    price    = analysis.get("price", 0)
    trend    = analysis.get("trend", "nötr")
    score    = sig.get("score", 0)

    lines = [f"📊 **{symbol}** ${price:,.4f} — {trend}"]

    # Trend ihtimali
    if "güçlü yükseliş" in trend:
        lines.append("📈 Yüksek ihtimalle (%70+) yükseliş momentum'u devam ediyor")
    elif "güçlü düşüş" in trend:
        lines.append("📉 %65+ ihtimalle düşüş baskısı sürüyor")
    elif "kısa vadeli yükseliş" in trend:
        lines.append("📈 Kısa vadede %55-60 ihtimalle alıcı baskısı var")
    else:
        lines.append("↔️ Trend belirsiz, kararsızlık dönemi (%50-50)")

    # RSI
    rsi_val = rsi.get("value", 50) or 50
    if rsi_val > 70:
        lines.append(f"⚡ RSI {rsi_val} — Aşırı alım bölgesi, düzeltme ihtimali %60-70")
    elif rsi_val < 30:
        lines.append(f"⚡ RSI {rsi_val} — Aşırı satım, toparlanma ihtimali %65+")
    else:
        lines.append(f"⚡ RSI {rsi_val} — Nötr bölge, yön için ek sinyal gerekiyor")

    # Whale
    if whales:
        buy  = sum(1 for w in whales if w.get("side") == "BUY")
        sell = len(whales) - buy
        if buy > sell:
            lines.append(f"🐋 {buy} büyük alım vs {sell} satım — kurumsal ilgi sinyali")
        elif sell > buy:
            lines.append(f"🐋 {sell} büyük satım — dikkatli ol, baskı artabilir")

    # Senaryo
    if score >= 2:
        lines.append("🎯 Eğer hacim artmaya devam ederse yükseliş senaryosu güçlenir (%60-65)")
    elif score <= -2:
        lines.append("🎯 Destek kırılırsa düşüş hızlanabilir — stop-loss önemli")
    else:
        lines.append("🎯 Net yön için mum kapanışı beklenmeli")

    return "\n".join(lines)



# ──────────────────────────────────────────────────────────────
# WHALE TESPİTİ
# ──────────────────────────────────────────────────────────────

async def detect_whale(symbol: str, trade: Dict) -> Optional[Dict]:
    try:
        price = float(trade.get("p", 0))
        qty   = float(trade.get("q", 0))
        usd   = price * qty
        if usd >= WHALE_USD_THRESH:
            side  = "SELL" if trade.get("m", False) else "BUY"
            whale = {
                "type": "whale", "symbol": symbol, "side": side,
                "usd": round(usd, 0), "price": price, "qty": qty,
                "emoji": "🐋 WHALE ALIM" if side == "BUY" else "🐋 WHALE SATIŞ",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            whale_history[symbol].append(whale)
            print(f"[WHALE] {whale['emoji']} | {symbol} | ${usd:,.0f}")
            return whale
    except Exception as e:
        print(f"[WHALE ERROR] {e}")
    return None

# ──────────────────────────────────────────────────────────────
# BİNANCE WEBSOCKET
# ──────────────────────────────────────────────────────────────

async def binance_stream(symbol: str):
    sym = symbol.lower()
    streams = [
        f"{sym}@kline_1m", f"{sym}@kline_5m",
        f"{sym}@kline_15m", f"{sym}@kline_1h",
        f"{sym}@kline_4h",  f"{sym}@kline_1d",
        f"{sym}@aggTrade",  f"{sym}@bookTicker",
    ]
    url = f"{BINANCE_WS_URL}/{'/'.join(streams)}"
    retry = 5
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                print(f"[BINANCE WS] ✅ {symbol}")
                retry = 5
                async for msg in ws:
                    await process_msg(symbol, json.loads(msg))
        except Exception as e:
            print(f"[BINANCE WS] {symbol} hata: {e} — {retry}s")
        await asyncio.sleep(retry)
        retry = min(retry * 2, 60)

async def process_msg(symbol: str, data: Dict):
    d     = data.get("data", data)
    event = d.get("e", "")
    push  = None

    if event == "kline":
        k = d["k"]
        iv = k["i"]
        candle = {"t":k["t"],"o":k["o"],"h":k["h"],"l":k["l"],"c":k["c"],"v":k["v"],"x":k["x"]}
        if symbol not in kline_cache:       kline_cache[symbol] = {}
        if iv not in kline_cache[symbol]:   kline_cache[symbol][iv] = deque(maxlen=300)
        cache = kline_cache[symbol][iv]
        if cache and cache[-1]["t"] == candle["t"]: cache[-1] = candle
        else: cache.append(candle)

        push = {"type":"kline","symbol":symbol,"interval":iv,"candle":candle,"closed":k["x"]}

        # Kapanan mumda sinyal kontrolü
        if k["x"] and iv in ("15m", "1h"):
            sig = detect_signals(symbol, iv)
            if sig.get("signal",{}).get("score",0) != 0:
                signal_event = {"type":"signal","symbol":symbol,"interval":iv,"signal":sig["signal"],
                                "patterns":sig["candle_patterns"],"rsi":sig["rsi"],"macd":sig["macd"]}
                signal_history[symbol].append(signal_event)
                await broadcast(symbol, signal_event)

    elif event == "aggTrade":
        price_cache[symbol] = float(d.get("p", 0))
        whale = await detect_whale(symbol, d)
        if whale: push = whale

    elif event == "bookTicker":
        bid = float(d.get("b", 0))
        ask = float(d.get("a", 0))
        price_cache[symbol] = (bid + ask) / 2
        push = {"type":"ticker","symbol":symbol,"bid":bid,"ask":ask,
                "price":round((bid+ask)/2, 6)}

    if push:
        await broadcast(symbol, push)

async def broadcast(symbol: str, data: Dict):
    global all_clients
    msg = json.dumps(data, default=str)
    if symbol not in subscribers:
        return
    # Kopyasını al — iterasyon sırasında set değişirse hata vermesin
    current_subscribers = list(subscribers[symbol])
    for ws in current_subscribers:
        try:
            await ws.send_text(msg)
        except Exception:
            subscribers[symbol].discard(ws)
            all_clients.discard(ws)

# ──────────────────────────────────────────────────────────────
# GEÇMİŞ VERİ
# ──────────────────────────────────────────────────────────────

async def fetch_historical(symbol: str, interval: str = "1h", limit: int = 300):
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{BINANCE_REST_URL}/klines",
                params={"symbol":symbol,"interval":interval,"limit":limit})
            r.raise_for_status()
            klines = [{"t":k[0],"o":k[1],"h":k[2],"l":k[3],"c":k[4],"v":k[5],"x":True}
                      for k in r.json()]
            if symbol not in kline_cache:    kline_cache[symbol] = {}
            kline_cache[symbol][interval] = deque(klines, maxlen=300)
            return klines
    except Exception as e:
        print(f"[HISTORY] {symbol} {interval}: {e}")
        return []

# ──────────────────────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────────────────────



@app.get("/commentary/{symbol}")
async def get_commentary(
    symbol:   str,
    interval: str = Query("1h", enum=INTERVALS),
):
    """
    Otomatik finans yorumu — kullanıcı sormaz, sistem üretir.
    Olasılıksal dil kullanır, kesin tavsiye vermez.
    """
    symbol = symbol.upper()
    if symbol not in SUPPORTED_COINS:
        raise HTTPException(404, f"{symbol} desteklenmiyor")

    if symbol not in kline_cache or interval not in kline_cache.get(symbol, {}):
        await fetch_historical(symbol, interval, 300)

    commentary = await generate_commentary(symbol, interval)
    return {
        "symbol":     symbol,
        "interval":   interval,
        "commentary": commentary,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "disclaimer": "Bu yorum otomatik oluşturulmuştur. Yatırım tavsiyesi değildir.",
    }


@app.get("/commentary/{symbol}/stream")
async def stream_commentary(
    symbol:   str,
    interval: str = Query("1h", enum=INTERVALS),
):
    """
    Yorum akışı — frontend'e token token gönderir.
    Kullanıcı sormadan otomatik çalışır.
    """
    symbol = symbol.upper()
    if symbol not in SUPPORTED_COINS:
        raise HTTPException(404)

    if symbol not in kline_cache or interval not in kline_cache.get(symbol, {}):
        await fetch_historical(symbol, interval, 300)

    analysis = detect_signals(symbol, interval)
    if "error" in analysis or not DEEPINFRA_API_KEY:
        commentary = _auto_commentary(symbol, interval)
        return StreamingResponse(
            iter([commentary]),
            media_type="text/plain; charset=utf-8"
        )

    sig      = analysis.get("signal", {})
    rsi      = analysis.get("rsi", {})
    macd     = analysis.get("macd", {})
    vol      = analysis.get("volume", {})
    patterns = analysis.get("candle_patterns", [])
    whales   = analysis.get("whales_recent", [])
    bb       = analysis.get("bollinger", {})

    data_summary = f"""{symbol} | {interval} | ${analysis.get('price', 0):,.4f}
Trend: {analysis.get('trend')} | RSI: {rsi.get('value')} ({rsi.get('comment')})
MACD: {macd.get('trend')} | Histogram: {macd.get('histogram', 0):+.6f}
Bollinger: {bb.get('position', 'N/A')} | Genişlik: {bb.get('width', 0):.1f}%
Hacim: {vol.get('ratio', 1):.1f}x {'ANOMALİ' if vol.get('alert') else 'normal'}
Formasyonlar: {', '.join(p['name'] for p in patterns) or 'Yok'}
Sinyal: {sig.get('score', 0):+d} ({sig.get('overall')}) | Sebepler: {', '.join(sig.get('reasons', []))}
Whale: {len(whales)} büyük işlem"""

    async def _stream():
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream(
                    "POST",
                    f"{DEEPINFRA_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                             "Content-Type": "application/json"},
                    json={
                        "model":       FINANS_LLM_MODEL,
                        "messages":    [
                            {"role": "system", "content": COMMENTARY_PROMPT},
                            {"role": "user",   "content": f"/no_think\n{symbol} piyasa verisi:\n{data_summary}"},
                        ],
                        "max_tokens":  350,
                        "temperature": 0.4,
                        "stream":      True,
                    }
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                chunk = json.loads(line[6:])
                                text  = chunk["choices"][0]["delta"].get("content", "")
                                if text:
                                    yield text
                            except Exception:
                                pass
        except Exception as e:
            print(f"[COMMENTARY STREAM] ❌ {e}")
            yield _auto_commentary(symbol, interval)

    return StreamingResponse(_stream(), media_type="text/plain; charset=utf-8")

@app.on_event("startup")
async def startup():
    print("[FINANS] Başlıyor...")
    tasks = [fetch_historical(s, iv, 300)
             for s in SUPPORTED_COINS for iv in ["15m","1h","4h","1d"]]
    await asyncio.gather(*tasks)
    print(f"[FINANS] ✅ Geçmiş veri yüklendi")
    for s in SUPPORTED_COINS:
        asyncio.create_task(binance_stream(s))
    print(f"[FINANS] ✅ {len(SUPPORTED_COINS)} coin stream aktif")

# ──────────────────────────────────────────────────────────────
# REST ENDPOINTS
# ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status":"ok","coins":len(price_cache),"model":FINANS_LLM_MODEL}

@app.get("/coins")
async def get_coins():
    return {"coins":[{"symbol":s,"price":price_cache.get(s,0)} for s in SUPPORTED_COINS]}

@app.get("/signals/{symbol}")
async def get_signals(symbol: str, interval: str = Query("1h", enum=INTERVALS)):
    symbol = symbol.upper()
    if symbol not in SUPPORTED_COINS:
        raise HTTPException(404, f"{symbol} desteklenmiyor")
    if symbol not in kline_cache or interval not in kline_cache.get(symbol,{}):
        await fetch_historical(symbol, interval, 300)
    return detect_signals(symbol, interval)

@app.get("/signals/{symbol}/interpret")
async def interpret(symbol: str, interval: str = Query("1h", enum=INTERVALS)):
    symbol = symbol.upper()
    if symbol not in SUPPORTED_COINS:
        raise HTTPException(404)
    analysis = detect_signals(symbol, interval)
    if "error" in analysis:
        return analysis
    comment = await llm_interpret(symbol, analysis)
    return {**analysis, "llm_comment": comment}

@app.get("/history/{symbol}")
async def get_history(symbol: str,
                      interval: str = Query("1h", enum=INTERVALS),
                      limit: int = Query(300, ge=10, le=1000)):
    symbol = symbol.upper()
    return {"symbol":symbol,"interval":interval,
            "klines": await fetch_historical(symbol, interval, limit)}


@app.get("/fng")
async def fear_greed_index():
    """
    Korku & Açgözlülük Endeksi — Binance verilerinden hesaplanır.
    RSI, volatilite, hacim, momentum kullanılır.
    """
    scores = []
    labels = []

    btc_klines = list(kline_cache.get("BTCUSDT", {}).get("1d", []))
    if len(btc_klines) < 30:
        return {"value": 50, "label": "Nötr", "components": {}}

    closes  = [float(k["c"]) for k in btc_klines]
    volumes = [float(k["v"]) for k in btc_klines]

    # 1. RSI (0-100 → 0-100)
    rsi = calc_rsi(closes, 14)
    if rsi:
        scores.append(rsi)
        labels.append(f"RSI: {rsi:.1f}")

    # 2. Fiyat Momentum — son 7 günlük değişim
    if len(closes) >= 8:
        mom = (closes[-1] - closes[-8]) / closes[-8] * 100
        mom_score = 50 + min(max(mom * 3, -50), 50)
        scores.append(mom_score)
        labels.append(f"Momentum: {mom:.1f}%")

    # 3. Volatilite — düşük volatilite = daha az korku
    if len(closes) >= 30:
        import statistics
        std = statistics.stdev(closes[-30:])
        mean = sum(closes[-30:]) / 30
        vol_pct = std / mean * 100
        vol_score = max(0, 100 - vol_pct * 5)
        scores.append(vol_score)
        labels.append(f"Vol: {vol_pct:.1f}%")

    # 4. Hacim Momentum
    if len(volumes) >= 10:
        avg_vol = sum(volumes[-30:]) / 30 if len(volumes) >= 30 else sum(volumes) / len(volumes)
        vol_ratio = volumes[-1] / avg_vol if avg_vol else 1
        vol_mom_score = min(50 + (vol_ratio - 1) * 25, 100)
        scores.append(max(0, vol_mom_score))
        labels.append(f"HacimRatio: {vol_ratio:.2f}x")

    # 5. EMA Trend — fiyat EMA50 üstünde mi?
    ema50 = calc_ema(closes, 50)
    if ema50:
        ema_score = 75 if closes[-1] > ema50[-1] else 25
        scores.append(ema_score)
        labels.append(f"EMA50: {'Üstünde' if closes[-1] > ema50[-1] else 'Altında'}")

    value = round(sum(scores) / len(scores)) if scores else 50

    if value < 20:   label = "Aşırı Korku"
    elif value < 40: label = "Korku"
    elif value < 60: label = "Nötr"
    elif value < 80: label = "Açgözlülük"
    else:            label = "Aşırı Açgözlülük"

    return {
        "value": value,
        "label": label,
        "components": dict(zip(["rsi","momentum","volatility","volume","ema"], scores)),
        "details": labels,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/whales/{symbol}")
async def get_whales(symbol: str):
    symbol = symbol.upper()
    return {"symbol":symbol,"whales":list(whale_history[symbol]),
            "threshold_usd":WHALE_USD_THRESH}

@app.get("/signals/history/{symbol}")
async def get_signal_history(symbol: str):
    symbol = symbol.upper()
    return {"symbol":symbol,"signals":list(signal_history[symbol])}

# ──────────────────────────────────────────────────────────────
# WEBSOCKET
# ──────────────────────────────────────────────────────────────

@app.websocket("/ws/{symbol}")
async def ws_endpoint(ws: WebSocket, symbol: str):
    await ws.accept()
    symbol = symbol.upper()

    # Token kontrolü — nginx bypass edildiği için burada yapıyoruz
    token = ws.query_params.get("token", "")
    if not verify_token(token):
        await ws.send_text(json.dumps({"error": "Yetkisiz"}))
        await ws.close(); return

    if symbol not in SUPPORTED_COINS:
        await ws.send_text(json.dumps({"error":"Desteklenmiyor"}))
        await ws.close(); return

    subscribers[symbol].add(ws)
    all_clients.add(ws)
    print(f"[WS] +{symbol} | toplam={len(all_clients)}")

    try:
        # İlk snapshot
        snap = {"type":"snapshot","symbol":symbol,
                "price":price_cache.get(symbol,0),
                "signals":detect_signals(symbol,"1h"),
                "whales":list(whale_history[symbol])[-10:]}
        await ws.send_text(json.dumps(snap, default=str))

        # Ping/keepalive — her 20 saniyede ping gönder, nginx timeout önler
        async def send_ping():
            while True:
                await asyncio.sleep(20)
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
        asyncio.create_task(send_ping())

        while True:
            msg = await ws.receive_text()
            d   = json.loads(msg)
            if d.get("action") == "subscribe":
                new = d.get("symbol","").upper()
                if new in SUPPORTED_COINS:
                    subscribers[symbol].discard(ws)
                    symbol = new
                    subscribers[symbol].add(ws)
                    snap = {"type":"snapshot","symbol":symbol,
                            "price":price_cache.get(symbol,0),
                            "signals":detect_signals(symbol,"1h"),
                            "whales":list(whale_history[symbol])[-10:]}
                    await ws.send_text(json.dumps(snap, default=str))

    except WebSocketDisconnect:
        print(f"[WS] -{symbol}")
    except Exception as e:
        print(f"[WS] Hata: {e}")
    finally:
        subscribers[symbol].discard(ws)
        all_clients.discard(ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8090, reload=False)