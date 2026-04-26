"""
═══════════════════════════════════════════════════════════════
JOHN AI — Proaktif Trader Asistan (Finans Servisi Eklentisi)
═══════════════════════════════════════════════════════════════
Mevcut app.py'ye eklenir. Yeni endpoint'ler:
  - GET  /john/intro/{symbol}    — Sayfa açılışı yorumu
  - GET  /john/alerts            — Son uyarıları çek
  - GET  /john/alerts/stream     — SSE canlı uyarı akışı
  - POST /john/ask               — Kullanıcı sorusu
  - POST /john/dismiss           — Uyarı kapatma

Background task: Her 2 dakikada coinleri tarar, anomali bulursa
push eder.
═══════════════════════════════════════════════════════════════
"""
import asyncio
import json
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, List, Dict

import httpx
from fastapi import HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


# ─────────────────────────────────────────────────────────────
# JOHN — Karakter ve davranış
# ─────────────────────────────────────────────────────────────

JOHN_SYSTEM = """Sen John'sun. 35 yaşında, 12 yıllık eski bir trader, şimdi ONE-BUNE'de finans yorumcusu.

KARAKTERİN:
• Sakin, samimi, dostça konuşursun — "ben olsam", "bizim için" gibi ifadeler
• Risk hafifletici dil: "olabilir", "ihtimali var", "görünüyor"
• ASLA "kesin al/sat" demezsin — hep ihtimal verirsin
• Türk piyasasını da bilirsin (dolar etkisi, BIST vs.)
• Emoji KULLANMA aşırıya kaçma, max 1-2 tane

YAZIM TARZI:
• Kısa, net, 2-3 cümle
• Profesyonel ama soğuk değil
• "Şu an gözüm BTC'de" gibi kişisel ifadeler

YASAKLAR:
• Yatırım tavsiyesi vermek
• "Mutlaka", "kesinlikle" gibi sözler
• Piyasayı tahmin etmek (sadece olasılık)
• 4'ten fazla cümle"""


# Modül-seviyesi state (background scanner için)
_app_module = None  # register sırasında set edilir
_alert_cooldowns: Dict[str, float] = {}
_alert_history: deque = deque(maxlen=50)
_sse_clients: List[asyncio.Queue] = []

# Alarm tipleri
ALERT_RULES = {
    "rsi_oversold":     {"threshold": 28, "cooldown": 1800},
    "rsi_overbought":   {"threshold": 72, "cooldown": 1800},
    "volume_spike":     {"threshold": 3.0, "cooldown": 600},
    "whale_big":        {"threshold": 1_000_000, "cooldown": 300},
    "pattern_strong":   {"cooldown": 1800},
    "price_breakout":   {"threshold": 2.0, "cooldown": 900},
}


def _can_alert(key: str, cooldown: int) -> bool:
    """Cooldown kontrolü."""
    now = _time.time()
    last = _alert_cooldowns.get(key, 0)
    if now - last < cooldown:
        return False
    _alert_cooldowns[key] = now
    return True


def _gen_alert_text(alert_type: str, data: Dict) -> str:
    """John tarzında kısa alarm metni."""
    symbol = data.get("symbol", "")
    short = symbol.replace("USDT", "")
    templates = {
        "rsi_oversold":   f"{short} RSI {data.get('rsi', 0):.0f}'e düştü, aşırı satım bölgesinde. Toparlanma ihtimali artıyor olabilir.",
        "rsi_overbought": f"{short} RSI {data.get('rsi', 0):.0f}, aşırı alımda. Düzeltme gelmesi şaşırtmazdı.",
        "volume_spike":   f"{short} hacmi {data.get('ratio', 0):.1f}x patladı. Bir hareket pişiyor olabilir, dikkat.",
        "whale_big":      f"🐋 {short} {data.get('side', 'BUY')} — ${data.get('usd', 0):,.0f}'lık büyük işlem. Kurumsal ilgi olabilir.",
        "pattern_strong": f"{short} mum grafiğinde {data.get('pattern', '')} formasyonu — {data.get('direction', 'nötr')} sinyal.",
        "price_breakout": f"{short} son saat içinde %{data.get('change', 0):+.1f} hareket etti. Fiyatta breakout görünüyor.",
    }
    return templates.get(alert_type, f"{short}'de dikkat çeken bir gelişme var.")


async def _scan_for_alerts():
    """Tüm coinleri tara, anomali varsa alert üret."""
    if _app_module is None:
        return

    try:
        detect_signals  = _app_module.detect_signals
        SUPPORTED_COINS = _app_module.SUPPORTED_COINS
        whale_history   = _app_module.whale_history
        kline_cache     = _app_module.kline_cache
    except AttributeError:
        return

    new_alerts = []

    for symbol in SUPPORTED_COINS:
        try:
            # 1. Whale alarmı
            whales = list(whale_history.get(symbol, []))[-5:]
            for w in whales:
                usd = w.get("usd", 0)
                if usd >= ALERT_RULES["whale_big"]["threshold"]:
                    cooldown_key = f"whale:{symbol}:{w.get('timestamp', '')}"
                    if _can_alert(cooldown_key, ALERT_RULES["whale_big"]["cooldown"]):
                        new_alerts.append({
                            "id": f"whale:{symbol}:{int(_time.time())}",
                            "type": "whale_big", "symbol": symbol, "severity": "high",
                            "text": _gen_alert_text("whale_big", {"symbol": symbol, "side": w.get("side"), "usd": usd}),
                            "data": {"usd": usd, "side": w.get("side")},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            # 2. Teknik analiz (15m bazlı)
            sig = detect_signals(symbol, "15m")
            if sig.get("error"):
                continue

            rsi_val = sig.get("rsi", {}).get("value")
            vol = sig.get("volume", {})
            patterns = sig.get("candle_patterns", [])
            price = sig.get("price", 0)

            # RSI extreme
            if rsi_val is not None:
                if rsi_val < ALERT_RULES["rsi_oversold"]["threshold"]:
                    if _can_alert(f"rsi_low:{symbol}", ALERT_RULES["rsi_oversold"]["cooldown"]):
                        new_alerts.append({
                            "id": f"rsi:{symbol}:{int(_time.time())}",
                            "type": "rsi_oversold", "symbol": symbol, "severity": "medium",
                            "text": _gen_alert_text("rsi_oversold", {"symbol": symbol, "rsi": rsi_val}),
                            "data": {"rsi": rsi_val, "price": price},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                elif rsi_val > ALERT_RULES["rsi_overbought"]["threshold"]:
                    if _can_alert(f"rsi_high:{symbol}", ALERT_RULES["rsi_overbought"]["cooldown"]):
                        new_alerts.append({
                            "id": f"rsi:{symbol}:{int(_time.time())}",
                            "type": "rsi_overbought", "symbol": symbol, "severity": "medium",
                            "text": _gen_alert_text("rsi_overbought", {"symbol": symbol, "rsi": rsi_val}),
                            "data": {"rsi": rsi_val, "price": price},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            # Hacim spike
            ratio = vol.get("ratio", 1)
            if ratio >= ALERT_RULES["volume_spike"]["threshold"]:
                if _can_alert(f"vol:{symbol}", ALERT_RULES["volume_spike"]["cooldown"]):
                    new_alerts.append({
                        "id": f"vol:{symbol}:{int(_time.time())}",
                        "type": "volume_spike", "symbol": symbol, "severity": "medium",
                        "text": _gen_alert_text("volume_spike", {"symbol": symbol, "ratio": ratio}),
                        "data": {"ratio": ratio, "price": price},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

            # Güçlü formasyon
            for p in patterns:
                if p.get("strength") == "strong":
                    pname = p.get("name", "")
                    pdir = p.get("direction", "")
                    if _can_alert(f"pat:{symbol}:{pname}", ALERT_RULES["pattern_strong"]["cooldown"]):
                        new_alerts.append({
                            "id": f"pat:{symbol}:{int(_time.time())}",
                            "type": "pattern_strong", "symbol": symbol,
                            "severity": "high" if pdir != "neutral" else "low",
                            "text": _gen_alert_text("pattern_strong", {"symbol": symbol, "pattern": pname, "direction": pdir}),
                            "data": {"pattern": pname, "direction": pdir, "price": price},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            # Fiyat breakout
            klines_1h = list(kline_cache.get(symbol, {}).get("1h", []))
            if len(klines_1h) >= 2:
                prev_close = float(klines_1h[-2]["c"])
                if prev_close > 0:
                    change = (price - prev_close) / prev_close * 100
                    if abs(change) >= ALERT_RULES["price_breakout"]["threshold"]:
                        direction = "up" if change > 0 else "down"
                        if _can_alert(f"break:{symbol}:{direction}", ALERT_RULES["price_breakout"]["cooldown"]):
                            new_alerts.append({
                                "id": f"break:{symbol}:{int(_time.time())}",
                                "type": "price_breakout", "symbol": symbol, "severity": "medium",
                                "text": _gen_alert_text("price_breakout", {"symbol": symbol, "change": change}),
                                "data": {"change": change, "price": price, "direction": direction},
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })

        except Exception as e:
            print(f"[JOHN SCAN] {symbol}: {e}")

    # Yeni alarmları kaydet ve SSE'e push et
    for alert in new_alerts:
        _alert_history.append(alert)
        for queue in list(_sse_clients):
            try:
                queue.put_nowait(alert)
            except Exception:
                pass

    if new_alerts:
        print(f"[JOHN] {len(new_alerts)} yeni alarm üretildi")


async def john_scanner_loop():
    """Background task — her 2dk tarama."""
    print("[JOHN] Scanner başladı (her 2dk)")
    await asyncio.sleep(30)  # Servis hazırlansın
    while True:
        try:
            await _scan_for_alerts()
        except Exception as e:
            print(f"[JOHN SCAN] Hata: {e}")
        await asyncio.sleep(120)


def john_startup_task():
    """Background scanner'ı başlat — app.py startup içinde çağrılır."""
    asyncio.create_task(john_scanner_loop())


def _john_intro_fallback(symbol: str, sig: Dict) -> str:
    """LLM yoksa kural tabanlı intro."""
    short = symbol.replace("USDT", "")
    price = sig.get("price", 0)
    trend = sig.get("trend", "nötr")
    rsi = sig.get("rsi", {}).get("value")
    overall = sig.get("signal", {}).get("overall", "BEKLE")

    parts = [f"Selam, ben John. {short}'a şöyle bir göz attım:"]
    parts.append(f"Şu an ${price:,.2f} seviyesinde, {trend}.")
    if rsi is not None:
        if rsi < 35:
            parts.append(f"RSI {rsi:.0f} — aşırı satım, ben olsam takipte tutardım.")
        elif rsi > 65:
            parts.append(f"RSI {rsi:.0f} — biraz yüksek, dikkatli ol.")
        else:
            parts.append(f"RSI {rsi:.0f} — sağlıklı bölgede.")
    if "ALIM" in overall:
        parts.append("Genel sinyaller olumlu görünüyor.")
    elif "SATIŞ" in overall:
        parts.append("Sinyaller karışık, acele etmem.")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────
# REGISTER — app.py'den çağrılır
# ─────────────────────────────────────────────────────────────

def register_john(app, app_module):
    """
    John endpoint'lerini app'e bağla.
    app_module: app.py'nin sys.modules referansı (tüm globals erişimi için)
    """
    global _app_module
    _app_module = app_module

    # ── /john/intro/{symbol} ───────────────────────────────
    @app.get("/john/intro/{symbol}")
    async def john_intro(symbol: str, interval: str = Query("1h")):
        symbol = symbol.upper()
        if symbol not in app_module.SUPPORTED_COINS:
            raise HTTPException(404)

        sig = app_module.detect_signals(symbol, interval)
        if "error" in sig:
            return {"text": "Selam, ben John. Veri henüz yüklenmedi, bir kahve içip dönelim ☕"}

        if not app_module.DEEPINFRA_API_KEY:
            return {"text": _john_intro_fallback(symbol, sig)}

        rsi = sig.get("rsi", {}).get("value")
        ctx = (
            f"Coin: {symbol.replace('USDT','')}\n"
            f"Fiyat: ${sig.get('price', 0):,.4f}\n"
            f"Trend: {sig.get('trend')}\n"
            f"RSI: {rsi}\n"
            f"Sinyal skoru: {sig.get('signal',{}).get('score', 0):+d}\n"
            f"Sinyal: {sig.get('signal',{}).get('overall')}"
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"{app_module.DEEPINFRA_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {app_module.DEEPINFRA_API_KEY}"},
                    json={
                        "model": app_module.FINANS_LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": JOHN_SYSTEM},
                            {"role": "user", "content": (
                                f"Yeni kullanıcı sayfaya girdi, {symbol.replace('USDT','')} "
                                f"verilerini açıyor. Onu kısa karşıla, mevcut durumu "
                                f"2-3 cümleyle özetle.\n\n{ctx}"
                            )},
                        ],
                        "max_tokens": 200,
                        "temperature": 0.6,
                    }
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                    return {"text": text, "symbol": symbol, "price": sig.get("price", 0)}
        except Exception as e:
            print(f"[JOHN INTRO] {e}")

        return {"text": _john_intro_fallback(symbol, sig)}

    # ── /john/alerts ──────────────────────────────────────
    @app.get("/john/alerts")
    async def john_alerts(limit: int = Query(20, ge=1, le=50)):
        items = list(_alert_history)[-limit:]
        items.reverse()
        return {
            "alerts": items,
            "count": len(items),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── /john/alerts/stream — SSE ─────────────────────────
    @app.get("/john/alerts/stream")
    async def john_alerts_stream():
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        _sse_clients.append(queue)

        async def event_gen():
            try:
                # İlk bağlantıda son 5 alarmı yolla
                recent = list(_alert_history)[-5:]
                for alert in recent:
                    yield f"data: {json.dumps(alert, ensure_ascii=False, default=str)}\n\n"

                while True:
                    try:
                        alert = await asyncio.wait_for(queue.get(), timeout=25.0)
                        yield f"data: {json.dumps(alert, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                if queue in _sse_clients:
                    _sse_clients.remove(queue)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── /john/ask ──────────────────────────────────────────
    class JohnAskRequest(BaseModel):
        question: str
        symbol: Optional[str] = None
        interval: Optional[str] = "1h"

    @app.post("/john/ask")
    async def john_ask(req: JohnAskRequest):
        if not app_module.DEEPINFRA_API_KEY:
            return {"text": "Şu an konuşamıyorum, biraz sonra tekrar dene 🤝"}

        ctx = ""
        if req.symbol:
            sym = req.symbol.upper()
            if sym in app_module.SUPPORTED_COINS:
                sig = app_module.detect_signals(sym, req.interval or "1h")
                if "error" not in sig:
                    rsi = sig.get("rsi", {})
                    macd = sig.get("macd", {})
                    ctx = (
                        f"\n\nMevcut {sym.replace('USDT','')} verisi:\n"
                        f"Fiyat: ${sig.get('price', 0):,.4f}\n"
                        f"Trend: {sig.get('trend')}\n"
                        f"RSI: {rsi.get('value')} ({rsi.get('comment')})\n"
                        f"MACD: {macd.get('trend')}\n"
                        f"Sinyal: {sig.get('signal', {}).get('overall')}"
                    )

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"{app_module.DEEPINFRA_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {app_module.DEEPINFRA_API_KEY}"},
                    json={
                        "model": app_module.FINANS_LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": JOHN_SYSTEM},
                            {"role": "user", "content": req.question + ctx},
                        ],
                        "max_tokens": 400,
                        "temperature": 0.6,
                    }
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                    return {"text": text}
        except Exception as e:
            print(f"[JOHN ASK] {e}")

        return {"text": "Şu an cevap üretemiyorum, biraz sonra tekrar dene 🤝"}

    # ── /john/dismiss ──────────────────────────────────────
    class DismissRequest(BaseModel):
        alert_id: str

    @app.post("/john/dismiss")
    async def john_dismiss(req: DismissRequest):
        return {"success": True, "id": req.alert_id}

    print("[JOHN] ✅ Endpoints register edildi: /john/intro, /john/alerts, /john/ask, /john/dismiss")