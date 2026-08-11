"""
═══════════════════════════════════════════════════════════════
PATLAMA RADARI — Gerilim Skoru Motoru
═══════════════════════════════════════════════════════════════
Ani hareketleri ÖNCEDEN söylemez (kimse söyleyemez) — ama patlama
öncesi "gerilim" işaretlerini yakalar ve erken uyarı verir.

5 sinyal → 0-100 gerilim skoru:
  1. Bollinger sıkışması (volatilite daralması → yay geriliyor)
  2. Hacim uyanışı (sessizlik sonrası hacim artışı)
  3. Balina birikimi (üst üste büyük işlemler)
  4. Sınıra dayanma (güçlü destek/dirence yakınlık)
  5. RSI uçta (aşırı alım/satım + sıkışma birleşimi)

DÜRÜSTLÜK: Skor YÖN söylemez. "Hareket yaklaşıyor" der, "yukarı/aşağı"
demez — çünkü sıkışan yay iki yöne de boşalabilir.

Mimari:
  - Sürekli tarayıcı: her 15 sn tüm coinleri hesaplar, RAM'de tutar
  - GET /tension          → tüm coinler (hazır cache'ten, anında)
  - GET /tension/{symbol} → tek coin detay
  - WS  /ws/tension       → skor değişince canlı iter

app.py'ye ekle (prediction'ın yanına):
  from tension_addon import register_tension
  register_tension(app, _sys.modules[__name__])

Dockerfile'a: COPY tension_addon.py .
═══════════════════════════════════════════════════════════════
"""
import asyncio
import json
import time as _time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

_app_module = None

# Sürekli taranan gerilim cache'i: {symbol: {score, signals, ...}}
_tension_cache: Dict[str, Dict] = {}
_tension_ws_clients: Set[WebSocket] = set()
_SCAN_INTERVAL = 15       # saniye
_INTERVAL = "1h"          # gerilim hangi zaman diliminde hesaplanır


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ─────────────── Gerilim sinyalleri ───────────────

def _bollinger_squeeze(closes: List[float]) -> tuple:
    """Bollinger genişliği son 50 muma göre ne kadar dar? Dar = sıkışma = gerilim."""
    if len(closes) < 70:
        return 0.0, None
    def bb_width(window):
        mid = sum(window) / len(window)
        std = (sum((x - mid) ** 2 for x in window) / len(window)) ** 0.5
        return (4 * std / mid * 100) if mid else 0
    cur_width = bb_width(closes[-20:])
    # Son 50 mumdaki genişlik dağılımı
    widths = []
    for i in range(len(closes) - 50, len(closes)):
        if i >= 20:
            widths.append(bb_width(closes[i - 20:i]))
    if not widths:
        return 0.0, None
    widths_sorted = sorted(widths)
    # cur_width en dar %25'te mi? (percentile)
    rank = sum(1 for w in widths_sorted if w < cur_width) / len(widths_sorted)
    # rank düşük = şu an tarihî olarak dar = yüksek gerilim
    score = _clamp((0.35 - rank) / 0.35, 0, 1) if rank < 0.35 else 0.0
    detail = f"Bollinger daralması (%{round(rank*100)} sıkışıklık)" if score > 0.3 else None
    return score, detail


def _volume_awakening(volumes: List[float]) -> tuple:
    """Uzun sessizlik sonrası hacim artışı → birileri hareketleniyor."""
    vols = [v for v in volumes if v]
    if len(vols) < 25:
        return 0.0, None
    recent = vols[-3:]
    baseline = vols[-23:-3]
    if not baseline:
        return 0.0, None
    avg_recent = sum(recent) / len(recent)
    avg_base = sum(baseline) / len(baseline)
    if avg_base <= 0:
        return 0.0, None
    ratio = avg_recent / avg_base
    # ratio > 1.5 → uyanış başlıyor; 3x+ → güçlü
    score = _clamp((ratio - 1.3) / 1.7, 0, 1)
    detail = f"Hacim uyanışı ({ratio:.1f}x)" if score > 0.3 else None
    return score, detail


def _whale_accumulation(app_module, symbol: str) -> tuple:
    """Son 30 dk'da üst üste büyük işlem → akıllı para birikiyor."""
    whale_history = getattr(app_module, "whale_history", {})
    whales = list(whale_history.get(symbol, []))
    if not whales:
        return 0.0, None
    now = _time.time()
    recent = 0
    for w in whales[-30:]:
        try:
            ts = datetime.fromisoformat(str(w.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            if now - ts < 1800:  # 30 dk
                recent += 1
        except Exception:
            continue
    score = _clamp(recent / 6.0, 0, 1)  # 6+ işlem = tam gerilim
    detail = f"Balina birikimi ({recent} büyük işlem/30dk)" if recent >= 2 else None
    return score, detail


def _boundary_proximity(closes: List[float]) -> tuple:
    """Fiyat son 50 mumun tepe/dibine yakın mı? Sınıra dayanma = kırılım riski."""
    if len(closes) < 50:
        return 0.0, None
    window = closes[-50:]
    hi, lo = max(window), min(window)
    cur = closes[-1]
    if hi == lo:
        return 0.0, None
    pos = (cur - lo) / (hi - lo)  # 0=dip, 1=tepe
    # Tepeye (>0.92) veya dibe (<0.08) yakınsa gerilim
    if pos >= 0.92:
        score = _clamp((pos - 0.92) / 0.08, 0, 1)
        return score, "Direnç sınırına dayandı"
    if pos <= 0.08:
        score = _clamp((0.08 - pos) / 0.08, 0, 1)
        return score, "Destek sınırına dayandı"
    return 0.0, None


def _rsi_extreme(closes: List[float]) -> tuple:
    """RSI aşırı bölgede mi? Uçlar dönüş/kırılım gerilimidir."""
    calc_rsi = getattr(_app_module, "calc_rsi", None)
    rsi = calc_rsi(closes) if calc_rsi else None
    if rsi is None:
        return 0.0, None
    if rsi >= 70:
        score = _clamp((rsi - 70) / 20, 0, 1)
        return score, f"RSI aşırı alım ({rsi:.0f})"
    if rsi <= 30:
        score = _clamp((30 - rsi) / 20, 0, 1)
        return score, f"RSI aşırı satım ({rsi:.0f})"
    return 0.0, None


# Ağırlıklar — toplam gerilim skoruna katkı
_WEIGHTS = {"squeeze": 0.30, "volume": 0.25, "whale": 0.20, "boundary": 0.15, "rsi": 0.10}


def compute_tension(app_module, symbol: str) -> Dict:
    """Bir coin için gerilim skorunu hesapla."""
    kline_cache = getattr(app_module, "kline_cache", {})
    klines = list(kline_cache.get(symbol, {}).get(_INTERVAL, []))
    if len(klines) < 70:
        return {"symbol": symbol, "score": 0, "level": "veri yok", "signals": [], "price": 0}

    closes = [float(k["c"]) for k in klines]
    volumes = []
    for k in klines:
        try:
            volumes.append(float(k.get("v", 0)))
        except (TypeError, ValueError):
            volumes.append(0)

    s_sq, d_sq = _bollinger_squeeze(closes)
    s_vo, d_vo = _volume_awakening(volumes)
    s_wh, d_wh = _whale_accumulation(app_module, symbol)
    s_bo, d_bo = _boundary_proximity(closes)
    s_rs, d_rs = _rsi_extreme(closes)

    raw = (s_sq * _WEIGHTS["squeeze"] + s_vo * _WEIGHTS["volume"] +
           s_wh * _WEIGHTS["whale"] + s_bo * _WEIGHTS["boundary"] +
           s_rs * _WEIGHTS["rsi"])
    score = round(raw * 100)

    signals = [d for d in (d_sq, d_vo, d_wh, d_bo, d_rs) if d]
    active_count = len(signals)

    # Seviye: birden fazla sinyal aktifse gerilim daha anlamlı
    if score >= 65 and active_count >= 2:
        level = "yüksek"
    elif score >= 40:
        level = "orta"
    elif score >= 20:
        level = "hafif"
    else:
        level = "sakin"

    return {
        "symbol": symbol,
        "coin": symbol.replace("USDT", ""),
        "score": score,
        "level": level,
        "signals": signals,
        "active_count": active_count,
        "price": closes[-1],
        "note": "Gerilim yüksek — sert hareket yaklaşabilir (yön belirsiz)" if level == "yüksek"
                else "Piyasa nispeten sakin" if level == "sakin"
                else "Orta seviye hareketlilik",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────── Sürekli tarayıcı ───────────────

async def _scanner_loop(app_module):
    """Her 15 sn tüm coinleri tara, değişenleri WS'e it."""
    supported = getattr(app_module, "SUPPORTED_COINS", [])
    # İlk taramadan önce verinin oturmasını bekle
    await asyncio.sleep(10)
    while True:
        try:
            changed = []
            for symbol in supported:
                t = compute_tension(app_module, symbol)
                old = _tension_cache.get(symbol, {})
                _tension_cache[symbol] = t
                # Skor 5+ puan değiştiyse veya seviye değiştiyse "değişti" say
                if abs(t["score"] - old.get("score", -99)) >= 5 or t["level"] != old.get("level"):
                    changed.append(t)
            if changed and _tension_ws_clients:
                await _broadcast_tension(changed)
        except Exception as e:
            print(f"[TENSION] scanner hata: {e}")
        await asyncio.sleep(_SCAN_INTERVAL)


async def _broadcast_tension(items: List[Dict]):
    """Bağlı tüm WS istemcilerine güncelleme it."""
    dead = set()
    payload = json.dumps({"type": "tension_update", "items": items}, default=str)
    for ws in list(_tension_ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _tension_ws_clients.discard(ws)


def _sorted_snapshot() -> List[Dict]:
    """Tüm coinler, gerilime göre azalan sıralı."""
    items = list(_tension_cache.values())
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return items


# ─────────────── Register ───────────────

def register_tension(app, app_module):
    global _app_module
    _app_module = app_module

    @app.get("/tension")
    async def tension_all():
        return {
            "items": _sorted_snapshot(),
            "count": len(_tension_cache),
            "scan_interval": _SCAN_INTERVAL,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/tension/{symbol}")
    async def tension_one(symbol: str):
        symbol = symbol.upper()
        if symbol in _tension_cache:
            return _tension_cache[symbol]
        return compute_tension(app_module, symbol)

    @app.websocket("/ws/tension")
    async def tension_ws(ws: WebSocket):
        await ws.accept()
        # Token doğrula (mevcut sistemle aynı)
        token = ws.query_params.get("token", "")
        verify = getattr(app_module, "verify_token", None)
        if verify and not verify(token):
            await ws.send_text(json.dumps({"error": "Yetkisiz"}))
            await ws.close()
            return
        _tension_ws_clients.add(ws)
        try:
            # Bağlanır bağlanmaz mevcut snapshot'ı gönder
            await ws.send_text(json.dumps({
                "type": "tension_snapshot",
                "items": _sorted_snapshot(),
            }, default=str))
            # Ping döngüsü (bağlantı canlı kalsın)
            async def send_ping():
                while True:
                    await asyncio.sleep(20)
                    try:
                        await ws.send_text(json.dumps({"type": "ping"}))
                    except Exception:
                        break
            asyncio.create_task(send_ping())
            while True:
                await ws.receive_text()  # istemciden mesaj beklemiyoruz, bağlantıyı tutuyoruz
        except WebSocketDisconnect:
            pass
        except Exception as e:
            print(f"[TENSION WS] {e}")
        finally:
            _tension_ws_clients.discard(ws)

    @app.on_event("startup")
    async def _tension_startup():
        asyncio.create_task(_scanner_loop(app_module))
        print("[TENSION] ✅ Sürekli tarayıcı başladı (her 15 sn)")

    print("[TENSION] ✅ Patlama Radarı register edildi: /tension, /ws/tension")