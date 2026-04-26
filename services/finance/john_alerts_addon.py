"""
═══════════════════════════════════════════════════════════════
JOHN AI — Proaktif Trader Asistan (Finans Servisi Eklentisi)
═══════════════════════════════════════════════════════════════
Mevcut app.py'nin SONUNA eklenmek üzere hazırlandı.
Yeni endpoint'ler:
  - GET  /john/intro/{symbol}    — Sayfa açılışı yorumu
  - GET  /john/alerts            — Son uyarıları çek (polling)
  - GET  /john/alerts/stream     — SSE ile canlı uyarı akışı
  - POST /john/alerts/dismiss    — Kullanıcı uyarıyı kapadı
  - GET  /john/portfolio/{user}  — Kullanıcı portföyü
  - POST /john/portfolio/{user}  — Portföy güncelle/kaydet

Background task: Her 2 dakikada bir tüm coinleri tarar,
anomali bulursa Redis'e atar, frontend'e canlı yayınlar.
═══════════════════════════════════════════════════════════════
"""

import asyncio
import json
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional, List, Dict

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


# ─────────────────────────────────────────────────────────────
# ALARM UPRETIMI — Background scanner
# ─────────────────────────────────────────────────────────────

# Alarm tipleri ve threshold'lar
ALERT_RULES = {
    "rsi_oversold":     {"threshold": 28, "cooldown": 1800},   # 30dk
    "rsi_overbought":   {"threshold": 72, "cooldown": 1800},
    "volume_spike":     {"threshold": 3.0, "cooldown": 600},    # 10dk
    "whale_big":        {"threshold": 1_000_000, "cooldown": 300},  # 5dk
    "macd_cross_up":    {"cooldown": 1800},
    "macd_cross_down":  {"cooldown": 1800},
    "pattern_strong":   {"cooldown": 1800},
    "price_breakout":   {"threshold": 2.0, "cooldown": 900},   # son 1h %2+
}

# Cooldown — aynı alarm tekrar gönderilmesin
_alert_cooldowns: Dict[str, float] = {}

# Alarm geçmişi (Redis yerine RAM, basit tutalım)
_alert_history: deque = deque(maxlen=50)

# SSE clients
_sse_clients: List[asyncio.Queue] = []


def _can_alert(key: str, cooldown: int) -> bool:
    """Cooldown kontrolü — aynı alarm spam etmesin."""
    import time as _t
    now = _t.time()
    last = _alert_cooldowns.get(key, 0)
    if now - last < cooldown:
        return False
    _alert_cooldowns[key] = now
    return True


async def _generate_john_alert_text(alert_type: str, data: Dict) -> str:
    """John tarzında kısa alarm metni üret."""
    symbol = data.get("symbol", "")
    short = symbol.replace("USDT", "")

    # Kural tabanlı hızlı template
    templates = {
        "rsi_oversold": f"{short} RSI {data.get('rsi', 0):.0f}'e düştü, aşırı satım bölgesinde. Toparlanma ihtimali artıyor olabilir.",
        "rsi_overbought": f"{short} RSI {data.get('rsi', 0):.0f}, aşırı alımda. Düzeltme gelmesi şaşırtmazdı.",
        "volume_spike": f"{short} hacmi {data.get('ratio', 0):.1f}x patladı. Bir hareket pişiyor olabilir, dikkat.",
        "whale_big": f"🐋 {short} {data.get('side', 'BUY')} — ${data.get('usd', 0):,.0f}'lık büyük işlem. Kurumsal ilgi olabilir.",
        "macd_cross_up": f"{short} MACD bullish kesişti. Kısa vadeli yükseliş momentum'u oluşuyor.",
        "macd_cross_down": f"{short} MACD bearish kesişti. Kısa vadede satış baskısı görebiliriz.",
        "pattern_strong": f"{short} mum grafiğinde {data.get('pattern', '')} formasyonu — {data.get('direction', 'nötr')} sinyal.",
        "price_breakout": f"{short} son saat içinde %{data.get('change', 0):.1f} hareket etti. Fiyatta breakout görünüyor.",
    }

    return templates.get(alert_type, f"{short}'de dikkat çeken bir gelişme var.")


async def _scan_for_alerts():
    """
    Tüm coinleri tara, anomali bulursa alert üret.
    Bu fonksiyon background'da her 2dk çalışır.
    """
    try:
        # detect_signals fonksiyonu mevcut app.py'de tanımlı
        # whale_history, kline_cache de oradan
        from __main__ import (
            detect_signals, SUPPORTED_COINS, whale_history,
            kline_cache, price_cache,
        )
    except ImportError:
        # Test ortamında veya import edilemezse skip
        return

    new_alerts = []

    for symbol in SUPPORTED_COINS:
        try:
            # 1. Whale alarmı (en taze, son 5dk)
            whales = list(whale_history.get(symbol, []))[-5:]
            for w in whales:
                usd = w.get("usd", 0)
                if usd >= ALERT_RULES["whale_big"]["threshold"]:
                    cooldown_key = f"whale:{symbol}:{w.get('timestamp', '')}"
                    if _can_alert(cooldown_key, ALERT_RULES["whale_big"]["cooldown"]):
                        text = await _generate_john_alert_text("whale_big", {
                            "symbol": symbol,
                            "side": w.get("side"),
                            "usd": usd,
                        })
                        new_alerts.append({
                            "id": f"whale:{symbol}:{int(datetime.now().timestamp())}",
                            "type": "whale_big",
                            "symbol": symbol,
                            "severity": "high",
                            "text": text,
                            "data": {"usd": usd, "side": w.get("side")},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            # 2. Teknik analiz alarmları (15m bazlı, daha hassas)
            sig = detect_signals(symbol, "15m")
            if sig.get("error"):
                continue

            rsi_val = sig.get("rsi", {}).get("value")
            macd = sig.get("macd", {})
            vol = sig.get("volume", {})
            patterns = sig.get("candle_patterns", [])
            price = sig.get("price", 0)

            # RSI extreme
            if rsi_val is not None:
                if rsi_val < ALERT_RULES["rsi_oversold"]["threshold"]:
                    if _can_alert(f"rsi_low:{symbol}", ALERT_RULES["rsi_oversold"]["cooldown"]):
                        text = await _generate_john_alert_text("rsi_oversold", {"symbol": symbol, "rsi": rsi_val})
                        new_alerts.append({
                            "id": f"rsi:{symbol}:{int(datetime.now().timestamp())}",
                            "type": "rsi_oversold",
                            "symbol": symbol, "severity": "medium",
                            "text": text,
                            "data": {"rsi": rsi_val, "price": price},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                elif rsi_val > ALERT_RULES["rsi_overbought"]["threshold"]:
                    if _can_alert(f"rsi_high:{symbol}", ALERT_RULES["rsi_overbought"]["cooldown"]):
                        text = await _generate_john_alert_text("rsi_overbought", {"symbol": symbol, "rsi": rsi_val})
                        new_alerts.append({
                            "id": f"rsi:{symbol}:{int(datetime.now().timestamp())}",
                            "type": "rsi_overbought",
                            "symbol": symbol, "severity": "medium",
                            "text": text,
                            "data": {"rsi": rsi_val, "price": price},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            # Hacim spike
            ratio = vol.get("ratio", 1)
            if ratio >= ALERT_RULES["volume_spike"]["threshold"]:
                if _can_alert(f"vol:{symbol}", ALERT_RULES["volume_spike"]["cooldown"]):
                    text = await _generate_john_alert_text("volume_spike", {"symbol": symbol, "ratio": ratio})
                    new_alerts.append({
                        "id": f"vol:{symbol}:{int(datetime.now().timestamp())}",
                        "type": "volume_spike",
                        "symbol": symbol, "severity": "medium",
                        "text": text,
                        "data": {"ratio": ratio, "price": price},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

            # Güçlü formasyon (Boğa Yutan, Ayı Yutan, Marubozu)
            for p in patterns:
                if p.get("strength") == "strong":
                    pname = p.get("name", "")
                    pdir = p.get("direction", "")
                    if _can_alert(f"pat:{symbol}:{pname}", ALERT_RULES["pattern_strong"]["cooldown"]):
                        text = await _generate_john_alert_text("pattern_strong", {
                            "symbol": symbol, "pattern": pname, "direction": pdir,
                        })
                        new_alerts.append({
                            "id": f"pat:{symbol}:{int(datetime.now().timestamp())}",
                            "type": "pattern_strong",
                            "symbol": symbol,
                            "severity": "high" if pdir != "neutral" else "low",
                            "text": text,
                            "data": {"pattern": pname, "direction": pdir, "price": price},
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            # Fiyat breakout — son 1h içinde %2+ hareket
            klines_1h = list(kline_cache.get(symbol, {}).get("1h", []))
            if len(klines_1h) >= 2:
                prev_close = float(klines_1h[-2]["c"])
                if prev_close > 0:
                    change = (price - prev_close) / prev_close * 100
                    if abs(change) >= ALERT_RULES["price_breakout"]["threshold"]:
                        direction = "up" if change > 0 else "down"
                        if _can_alert(f"break:{symbol}:{direction}", ALERT_RULES["price_breakout"]["cooldown"]):
                            text = await _generate_john_alert_text("price_breakout", {
                                "symbol": symbol, "change": change,
                            })
                            new_alerts.append({
                                "id": f"break:{symbol}:{int(datetime.now().timestamp())}",
                                "type": "price_breakout",
                                "symbol": symbol,
                                "severity": "medium",
                                "text": text,
                                "data": {"change": change, "price": price, "direction": direction},
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })

        except Exception as e:
            print(f"[JOHN SCAN] {symbol}: {e}")

    # Yeni alarmları history'ye ekle ve SSE clients'a yolla
    for alert in new_alerts:
        _alert_history.append(alert)
        for queue in list(_sse_clients):
            try:
                await queue.put(alert)
            except Exception:
                pass

    if new_alerts:
        print(f"[JOHN] {len(new_alerts)} yeni alarm")


async def john_scanner_loop():
    """Background task — her 2dk tarama yapar."""
    print("[JOHN] Scanner başladı (her 2dk)")
    # İlk taramadan önce 30sn bekle (servis hazırlansın)
    await asyncio.sleep(30)
    while True:
        try:
            await _scan_for_alerts()
        except Exception as e:
            print(f"[JOHN SCAN] Hata: {e}")
        await asyncio.sleep(120)  # 2 dakika


# ─────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────

# Bu fonksiyonlar app.py'deki app objesine register edilecek
# app.py'de en sonda: john_register(app)  diye çağırılır

def john_register(app):
    """Tüm John endpoint'lerini app'e bağla."""
    from fastapi import HTTPException, Query, Request
    from fastapi.responses import StreamingResponse, JSONResponse
    from pydantic import BaseModel
    import httpx

    # Mevcut config'leri import et
    from __main__ import (
        DEEPINFRA_API_KEY, DEEPINFRA_BASE_URL, FINANS_LLM_MODEL,
        SUPPORTED_COINS, kline_cache, price_cache, whale_history,
        detect_signals, INTERVALS,
    )

    # ── /john/intro/{symbol} — Sayfa açılışı yorumu ─────────
    @app.get("/john/intro/{symbol}")
    async def john_intro(symbol: str, interval: str = Query("1h", enum=INTERVALS)):
        """Coin sayfası açılışında John karşılama yorumu yapar."""
        symbol = symbol.upper()
        if symbol not in SUPPORTED_COINS:
            raise HTTPException(404)

        sig = detect_signals(symbol, interval)
        if "error" in sig:
            return {"text": "Selam, ben John. Veri henüz yüklenmedi, bir kahve içip dönelim ☕"}

        price = sig.get("price", 0)
        trend = sig.get("trend", "nötr")
        rsi = sig.get("rsi", {}).get("value")
        score = sig.get("signal", {}).get("score", 0)
        whales = sig.get("whales_recent", [])[-3:]

        # John'a özel bağlam
        context = f"""
Coin: {symbol.replace('USDT','')}
Fiyat: ${price:,.4f}
Trend: {trend}
RSI: {rsi}
Sinyal skoru: {score:+d}
Son whale: {len(whales)} işlem
"""

        # LLM çağrısı
        if not DEEPINFRA_API_KEY:
            return {"text": _john_intro_fallback(symbol, sig)}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"{DEEPINFRA_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}"},
                    json={
                        "model": FINANS_LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": JOHN_SYSTEM},
                            {"role": "user", "content": (
                                f"Yeni kullanıcı sayfaya girdi, {symbol.replace('USDT','')} "
                                f"verilerini açıyor. Onu kısa karşıla, mevcut durumu "
                                f"2-3 cümleyle özetle. Veri:\n{context}"
                            )},
                        ],
                        "max_tokens": 200,
                        "temperature": 0.6,
                    }
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                    return {"text": text, "symbol": symbol, "price": price}
        except Exception as e:
            print(f"[JOHN INTRO] {e}")

        return {"text": _john_intro_fallback(symbol, sig)}

    # ── /john/alerts — Son uyarıları getir (polling) ─────────
    @app.get("/john/alerts")
    async def john_alerts(limit: int = Query(20, ge=1, le=50)):
        """Son alarmlar — kullanıcı sayfaya girdiğinde geçmişi görsün."""
        items = list(_alert_history)[-limit:]
        items.reverse()  # En yenisi başta
        return {
            "alerts": items,
            "count": len(items),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── /john/alerts/stream — SSE canlı yayın ────────────────
    @app.get("/john/alerts/stream")
    async def john_alerts_stream():
        """Server-Sent Events ile canlı alarm akışı."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        _sse_clients.append(queue)

        async def event_gen():
            try:
                # İlk bağlantıda son 5 alarmı yolla
                recent = list(_alert_history)[-5:]
                for alert in recent:
                    yield f"data: {json.dumps(alert, ensure_ascii=False, default=str)}\n\n"

                # Sonra canlı dinle
                while True:
                    try:
                        alert = await asyncio.wait_for(queue.get(), timeout=25.0)
                        yield f"data: {json.dumps(alert, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        # Keepalive ping
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

    # ── /john/ask — Kullanıcı sorusu, John cevap verir ───────
    class JohnAskRequest(BaseModel):
        question: str
        symbol: Optional[str] = None
        interval: Optional[str] = "1h"

    @app.post("/john/ask")
    async def john_ask(req: JohnAskRequest):
        """Kullanıcı serbest soru sorar, John cevaplar."""
        if not DEEPINFRA_API_KEY:
            return {"text": "Şu an konuşamıyorum, biraz sonra tekrar dene 🤝"}

        context_data = ""
        if req.symbol:
            sym = req.symbol.upper()
            if sym in SUPPORTED_COINS:
                sig = detect_signals(sym, req.interval or "1h")
                if "error" not in sig:
                    rsi = sig.get("rsi", {})
                    macd = sig.get("macd", {})
                    context_data = (
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
                    f"{DEEPINFRA_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}"},
                    json={
                        "model": FINANS_LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": JOHN_SYSTEM},
                            {"role": "user", "content": req.question + context_data},
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

    # ── /john/dismiss — Uyarıyı kapat ────────────────────────
    class DismissRequest(BaseModel):
        alert_id: str

    @app.post("/john/dismiss")
    async def john_dismiss(req: DismissRequest):
        """Kullanıcı bir uyarıyı kapatır — frontend tracking için."""
        return {"success": True, "id": req.alert_id}

    print("[JOHN] Endpoints registered: /john/intro, /john/alerts, /john/ask")


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


def john_startup_task():
    """app.py startup event'inde çağrılır."""
    asyncio.create_task(john_scanner_loop())