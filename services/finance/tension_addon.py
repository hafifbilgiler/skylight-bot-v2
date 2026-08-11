"""
═══════════════════════════════════════════════════════════════
PATLAMA RADARI v2 — Gerilim Skoru + Canlı Şok Tespiti
═══════════════════════════════════════════════════════════════
İKİ katman:

  A) GERİLİM (önceden uyarır): 5 sinyal → 0-100. Patlamadan ÖNCE
     sıkışma/hacim/balina işaretlerini yakalar. 15 sn'de bir taranır.
     "Gerilim yüksek, hareket yakın olabilir" (yön söylemez).

  B) ŞOK (anında yakalar): canlı fiyat penceresine bakar. Son ~60 sn'de
     fiyat sert kıpırdadıysa "ŞU AN oynuyor" der. 2 sn'de bir kontrol.
     Bu tahmin değil — hareket başladığı an bildirim.

Kullanıcı akışı: Gerilim "hazırlan" → Şok "işte başladı".

Endpoint:
  GET /tension          → tüm coinler (gerilim + şok durumu), sıralı
  GET /tension/{symbol} → tek coin
  WS  /ws/tension       → canlı yayın (hem gerilim hem şok anında iter)

app.py'ye: from tension_addon import register_tension
           register_tension(app, _sys.modules[__name__])
Dockerfile: COPY tension_addon.py .
═══════════════════════════════════════════════════════════════
"""
import asyncio
import json
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Set

from fastapi import WebSocket, WebSocketDisconnect

_app_module = None

_tension_cache: Dict[str, Dict] = {}
_tension_ws_clients: Set[WebSocket] = set()
_price_window: Dict[str, deque] = {}   # {symbol: deque[(ts, price)]} — şok tespiti için
_shock_state: Dict[str, Dict] = {}     # {symbol: {active, ...}}

_SCAN_INTERVAL = 15    # gerilim taraması (sn)
_SHOCK_INTERVAL = 2    # şok kontrolü (sn)
_PRICE_WINDOW_SEC = 90 # fiyat penceresi genişliği
_INTERVAL = "1h"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ═══════════════ A) GERİLİM SİNYALLERİ ═══════════════

def _bollinger_squeeze(closes: List[float]):
    if len(closes) < 70:
        return 0.0, None
    def bb_width(w):
        mid = sum(w) / len(w)
        std = (sum((x - mid) ** 2 for x in w) / len(w)) ** 0.5
        return (4 * std / mid * 100) if mid else 0
    cur = bb_width(closes[-20:])
    widths = [bb_width(closes[i - 20:i]) for i in range(len(closes) - 50, len(closes)) if i >= 20]
    if not widths:
        return 0.0, None
    rank = sum(1 for w in widths if w < cur) / len(widths)
    score = _clamp((0.35 - rank) / 0.35, 0, 1) if rank < 0.35 else 0.0
    return score, (f"Sıkışma (%{round((1-rank)*100)} dar)" if score > 0.3 else None)


def _volume_awakening(volumes: List[float]):
    vols = [v for v in volumes if v]
    if len(vols) < 25:
        return 0.0, None
    ar = sum(vols[-3:]) / 3
    ab = sum(vols[-23:-3]) / 20
    if ab <= 0:
        return 0.0, None
    ratio = ar / ab
    score = _clamp((ratio - 1.3) / 1.7, 0, 1)
    return score, (f"Hacim uyanışı ({ratio:.1f}x)" if score > 0.3 else None)


def _whale_accumulation(app_module, symbol):
    wh = getattr(app_module, "whale_history", {})
    whales = list(wh.get(symbol, []))
    if not whales:
        return 0.0, None
    now = _time.time()
    recent = 0
    for w in whales[-30:]:
        try:
            ts = datetime.fromisoformat(str(w.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            if now - ts < 1800:
                recent += 1
        except Exception:
            continue
    score = _clamp(recent / 6.0, 0, 1)
    return score, (f"Balina birikimi ({recent} işlem)" if recent >= 2 else None)


def _boundary_proximity(closes):
    if len(closes) < 50:
        return 0.0, None
    w = closes[-50:]
    hi, lo, cur = max(w), min(w), closes[-1]
    if hi == lo:
        return 0.0, None
    pos = (cur - lo) / (hi - lo)
    if pos >= 0.92:
        return _clamp((pos - 0.92) / 0.08, 0, 1), "Dirence dayandı"
    if pos <= 0.08:
        return _clamp((0.08 - pos) / 0.08, 0, 1), "Desteğe dayandı"
    return 0.0, None


def _rsi_extreme(closes):
    calc_rsi = getattr(_app_module, "calc_rsi", None)
    rsi = calc_rsi(closes) if calc_rsi else None
    if rsi is None:
        return 0.0, None
    if rsi >= 70:
        return _clamp((rsi - 70) / 20, 0, 1), f"RSI aşırı alım ({rsi:.0f})"
    if rsi <= 30:
        return _clamp((30 - rsi) / 20, 0, 1), f"RSI aşırı satım ({rsi:.0f})"
    return 0.0, None


_WEIGHTS = {"squeeze": 0.30, "volume": 0.25, "whale": 0.20, "boundary": 0.15, "rsi": 0.10}


def compute_tension(app_module, symbol: str) -> Dict:
    kline_cache = getattr(app_module, "kline_cache", {})
    klines = list(kline_cache.get(symbol, {}).get(_INTERVAL, []))
    shock = _shock_state.get(symbol, {})

    if len(klines) < 70:
        return {"symbol": symbol, "coin": symbol.replace("USDT", ""),
                "score": 0, "level": "veri yok", "signals": [], "active_count": 0,
                "price": (getattr(app_module, "price_cache", {}) or {}).get(symbol, 0),
                "shock": shock, "note": "Veri toplanıyor",
                "timestamp": datetime.now(timezone.utc).isoformat()}

    closes = [float(k["c"]) for k in klines]
    volumes = []
    for k in klines:
        try:
            volumes.append(float(k.get("v", 0)))
        except (TypeError, ValueError):
            volumes.append(0)

    # Mum formasyonu sinyali (güçlü dönüş/hareket formasyonları)
    pattern_sig = None
    try:
        detect_patterns = getattr(app_module, "detect_candle_patterns", None)
        if detect_patterns and len(klines) >= 3:
            pats = detect_patterns(klines)
            strong = [p for p in pats if p.get("strength") in ("strong", "medium")]
            if strong:
                pattern_sig = strong[0].get("emoji", "") + " " + strong[0].get("name", "")
    except Exception:
        pass

    parts = [
        (_bollinger_squeeze(closes), "squeeze"),
        (_volume_awakening(volumes), "volume"),
        (_whale_accumulation(app_module, symbol), "whale"),
        (_boundary_proximity(closes), "boundary"),
        (_rsi_extreme(closes), "rsi"),
    ]
    raw = sum(res[0] * _WEIGHTS[key] for (res, key) in parts)
    score = round(raw * 100)
    signals = [res[1] for (res, key) in parts if res[1]]
    if pattern_sig:
        signals.append(pattern_sig)
        score = min(100, score + 12)  # güçlü formasyon gerilimi artırır
    active = len(signals)

    if score >= 65 and active >= 2:
        level = "yuksek"
    elif score >= 40:
        level = "orta"
    elif score >= 20:
        level = "hafif"
    else:
        level = "sakin"

    price = closes[-1]
    live = (getattr(app_module, "price_cache", {}) or {}).get(symbol, 0)
    if live:
        price = live

    return {
        "symbol": symbol, "coin": symbol.replace("USDT", ""),
        "score": score, "level": level, "signals": signals, "active_count": active,
        "price": price, "shock": shock,
        "note": ("Gerilim yüksek — sert hareket yaklaşabilir (yön belirsiz)" if level == "yuksek"
                 else "Orta seviye hareketlilik" if level == "orta"
                 else "Hafif hareketlilik" if level == "hafif"
                 else "Piyasa sakin"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════ B) CANLI ŞOK TESPİTİ ═══════════════

def _update_price_window(app_module):
    """price_cache'ten anlık fiyatları pencereye ekle (2 sn'de bir çağrılır)."""
    price_cache = getattr(app_module, "price_cache", {}) or {}
    now = _time.time()
    for symbol, price in price_cache.items():
        if not price:
            continue
        if symbol not in _price_window:
            _price_window[symbol] = deque()
        dq = _price_window[symbol]
        dq.append((now, float(price)))
        # Pencereyi temizle (eski kayıtları at)
        while dq and now - dq[0][0] > _PRICE_WINDOW_SEC:
            dq.popleft()


def _detect_shock(app_module, symbol: str) -> Dict:
    """Son ~60 sn'de fiyat sert kıpırdadı mı?"""
    dq = _price_window.get(symbol)
    if not dq or len(dq) < 5:
        return {"active": False}
    now = _time.time()
    prices = [p for (t, p) in dq if now - t <= 60]
    if len(prices) < 5:
        return {"active": False}
    p_start = prices[0]
    p_now = prices[-1]
    p_hi = max(prices)
    p_lo = min(prices)
    if p_start <= 0:
        return {"active": False}
    # 60 sn'lik net değişim
    change = (p_now - p_start) / p_start * 100
    # 60 sn'lik salınım aralığı (volatilite şoku)
    swing = (p_hi - p_lo) / p_start * 100
    # Eşikler: coin başına ~%0.8 net veya %1.2 salınım = şok
    THR_CHANGE = 0.8
    THR_SWING = 1.2
    if abs(change) >= THR_CHANGE or swing >= THR_SWING:
        direction = "yükseliş" if change > 0.2 else "düşüş" if change < -0.2 else "dalgalı"
        intensity = _clamp(max(abs(change) / THR_CHANGE, swing / THR_SWING), 1, 3)
        return {
            "active": True,
            "direction": direction,
            "change_pct": round(change, 2),
            "swing_pct": round(swing, 2),
            "intensity": round(intensity, 1),
            "ts": now,
        }
    return {"active": False}


# ═══════════════ TARAYICI DÖNGÜLERİ ═══════════════

async def _tension_scanner(app_module):
    supported = getattr(app_module, "SUPPORTED_COINS", [])
    await asyncio.sleep(10)
    while True:
        try:
            changed = []
            for symbol in supported:
                t = compute_tension(app_module, symbol)
                old = _tension_cache.get(symbol, {})
                _tension_cache[symbol] = t
                if abs(t["score"] - old.get("score", -99)) >= 5 or t["level"] != old.get("level"):
                    changed.append(t)
            if changed and _tension_ws_clients:
                await _broadcast({"type": "tension_update", "items": changed})
        except Exception as e:
            print(f"[TENSION] scanner: {e}")
        await asyncio.sleep(_SCAN_INTERVAL)


async def _shock_scanner(app_module):
    """Her 2 sn: fiyat penceresini güncelle + şok kontrol et."""
    supported = getattr(app_module, "SUPPORTED_COINS", [])
    await asyncio.sleep(5)
    while True:
        try:
            _update_price_window(app_module)
            shock_changes = []
            for symbol in supported:
                new_shock = _detect_shock(app_module, symbol)
                old_shock = _shock_state.get(symbol, {})
                _shock_state[symbol] = new_shock
                # Şok yeni başladı veya bitti → bildir
                if new_shock.get("active") != old_shock.get("active"):
                    if symbol in _tension_cache:
                        _tension_cache[symbol]["shock"] = new_shock
                    shock_changes.append({**_tension_cache.get(symbol, {"symbol": symbol}), "shock": new_shock})
                elif new_shock.get("active"):
                    # Devam eden şokun değerini güncel tut
                    if symbol in _tension_cache:
                        _tension_cache[symbol]["shock"] = new_shock
            if shock_changes and _tension_ws_clients:
                await _broadcast({"type": "shock_update", "items": shock_changes})
        except Exception as e:
            print(f"[TENSION] shock: {e}")
        await asyncio.sleep(_SHOCK_INTERVAL)


async def _broadcast(msg: Dict):
    dead = set()
    payload = json.dumps(msg, default=str)
    for ws in list(_tension_ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _tension_ws_clients.discard(ws)


def _snapshot() -> List[Dict]:
    items = list(_tension_cache.values())
    # Sıralama: önce aktif şok, sonra gerilim skoru
    items.sort(key=lambda x: (x.get("shock", {}).get("active", False), x.get("score", 0)), reverse=True)
    return items


# ═══════════════ REGISTER ═══════════════

def register_tension(app, app_module):
    global _app_module
    _app_module = app_module

    @app.get("/tension")
    async def tension_all():
        return {"items": _snapshot(), "count": len(_tension_cache),
                "scan_interval": _SCAN_INTERVAL,
                "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/tension/{symbol}")
    async def tension_one(symbol: str):
        symbol = symbol.upper()
        return _tension_cache.get(symbol) or compute_tension(app_module, symbol)

    @app.websocket("/ws/tension")
    async def tension_ws(ws: WebSocket):
        await ws.accept()
        token = ws.query_params.get("token", "")
        verify = getattr(app_module, "verify_token", None)
        if verify and not verify(token):
            await ws.send_text(json.dumps({"error": "Yetkisiz"}))
            await ws.close()
            return
        _tension_ws_clients.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "tension_snapshot", "items": _snapshot()}, default=str))
            async def ping():
                while True:
                    await asyncio.sleep(20)
                    try:
                        await ws.send_text(json.dumps({"type": "ping"}))
                    except Exception:
                        break
            asyncio.create_task(ping())
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            print(f"[TENSION WS] {e}")
        finally:
            _tension_ws_clients.discard(ws)

    @app.on_event("startup")
    async def _tension_startup():
        asyncio.create_task(_tension_scanner(app_module))
        asyncio.create_task(_shock_scanner(app_module))
        print("[TENSION] ✅ Tarayıcılar başladı (gerilim 15sn + şok 2sn)")

    print("[TENSION] ✅ Patlama Radarı v2 register edildi")