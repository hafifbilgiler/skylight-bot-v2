"""
═══════════════════════════════════════════════════════════════
CANLI TAHMİN — Geçmiş Desen İsabet Motoru (Finans Eklentisi)
═══════════════════════════════════════════════════════════════
Sahte AI değil: mevcut sinyal durumunun (RSI + trend + momentum)
geçmiş 300 mumda kaç kez görüldüğünü ve sonrasında ne olduğunu
sayar. Dürüst, kanıta dayalı olasılık verir.

Yeni endpoint:
  GET /predict/{symbol}?interval=1h
    → {probabilities:{up,flat,down}, sample_count, composite:{score,direction}, ...}

app.py'ye ekle (whale_radar'ın yanına):
  from prediction_addon import register_prediction
  register_prediction(app, _sys.modules[__name__])

Dockerfile'a ekle:
  COPY prediction_addon.py .
═══════════════════════════════════════════════════════════════
"""
import time as _time
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import Query, HTTPException

_app_module = None
_pred_cache: Dict[str, Dict] = {}
_PRED_TTL = 60  # 60 sn cache — canlı ama hafif

# Yön eşiği: interval'e göre "yükseldi/düştü" saymak için minimum hareket
_THRESHOLDS = {"15m": 0.003, "1h": 0.005, "4h": 0.010, "1d": 0.020}
_HORIZON = 4  # kaç mum sonrasına bakılıyor


def _rsi_at(closes: List[float], i: int, period: int = 14):
    if i < period:
        return None
    gains = losses = 0.0
    for j in range(i - period + 1, i + 1):
        d = closes[j] - closes[j - 1]
        if d > 0: gains += d
        else: losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - 100 / (1 + rs)


def _ema_series(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    # başa None dolgusu ekle ki index hizalansın
    pad = [None] * (period - 1)
    return pad + out


def _fingerprint(closes, rsi_vals, e9, e21, i):
    """Mum i'deki durum parmak izi: (rsi_bucket, ema_rel, mom_bucket)."""
    rsi = rsi_vals[i]
    if rsi is None or e9[i] is None or e21[i] is None or i < 5:
        return None
    if rsi < 30: rb = 0
    elif rsi < 45: rb = 1
    elif rsi < 55: rb = 2
    elif rsi < 70: rb = 3
    else: rb = 4
    er = 1 if e9[i] > e21[i] else 0
    mom = (closes[i] - closes[i - 5]) / closes[i - 5]
    if mom < -0.02: mb = 0
    elif mom < -0.005: mb = 1
    elif mom <= 0.005: mb = 2
    elif mom <= 0.02: mb = 3
    else: mb = 4
    return (rb, er, mb)


def _outcome(closes, i, horizon, thr):
    r = closes[i + horizon] / closes[i] - 1
    if r > thr: return "up"
    if r < -thr: return "down"
    return "flat"


def register_prediction(app, app_module):
    global _app_module
    _app_module = app_module

    @app.get("/predict/{symbol}")
    async def predict(symbol: str, interval: str = Query("1h")):
        symbol = symbol.upper()
        supported = getattr(app_module, "SUPPORTED_COINS", [])
        if symbol not in supported:
            raise HTTPException(404)

        # Cache
        ck = f"{symbol}:{interval}"
        cached = _pred_cache.get(ck)
        if cached and (_time.time() - cached["ts"]) < _PRED_TTL:
            return {**cached["data"], "_cached": True}

        kline_cache = getattr(app_module, "kline_cache", {})
        if symbol not in kline_cache or interval not in kline_cache.get(symbol, {}):
            await app_module.fetch_historical(symbol, interval, 300)

        klines = list(kline_cache.get(symbol, {}).get(interval, []))
        if len(klines) < 60:
            return {"error": "Yetersiz veri", "symbol": symbol}

        closes = [float(k["c"]) for k in klines]
        n = len(closes)
        thr = _THRESHOLDS.get(interval, 0.005)

        # Seriler (tek geçiş, self-contained)
        rsi_vals = [None] * n
        for i in range(15, n):
            rsi_vals[i] = _rsi_at(closes, i)
        e9 = _ema_series(closes, 9)
        e21 = _ema_series(closes, 21)
        # uzunluk hizala
        while len(e9) < n: e9.append(e9[-1] if e9 else None)
        while len(e21) < n: e21.append(e21[-1] if e21 else None)

        # Şu anki parmak izi (son mum)
        cur = _fingerprint(closes, rsi_vals, e9, e21, n - 1)
        if cur is None:
            return {"error": "Durum hesaplanamadı", "symbol": symbol}

        # Geçmişte benzer durumları tara
        def scan(match_fn):
            counts = {"up": 0, "flat": 0, "down": 0}
            total = 0
            for i in range(30, n - _HORIZON):
                fp = _fingerprint(closes, rsi_vals, e9, e21, i)
                if fp is None or not match_fn(fp):
                    continue
                counts[_outcome(closes, i, _HORIZON, thr)] += 1
                total += 1
            return counts, total

        # Önce 3 özellik, azsa gevşet
        counts, total = scan(lambda fp: fp == cur)
        matched_on = "RSI + trend + momentum"
        if total < 12:
            counts, total = scan(lambda fp: fp[0] == cur[0] and fp[1] == cur[1])
            matched_on = "RSI + trend"
        if total < 12:
            counts, total = scan(lambda fp: fp[0] == cur[0])
            matched_on = "RSI bölgesi"

        if total == 0:
            probs = {"up": 33, "flat": 34, "down": 33}
        else:
            probs = {k: round(v / total * 100) for k, v in counts.items()}

        # ── Bileşenler ──
        # 1) Teknik skor (mevcut motor)
        sig = app_module.detect_signals(symbol, interval)
        tech_score = sig.get("signal", {}).get("score", 0) if "error" not in sig else 0
        tech_norm = max(0, min(100, (tech_score + 5) * 10))

        # 2) Geçmiş isabet → 0-100
        hist_norm = max(0, min(100, 50 + (probs["up"] - probs["down"]) / 2))

        # 3) Balina akışı → 0-100
        whale_history = getattr(app_module, "whale_history", {})
        whales = list(whale_history.get(symbol, []))[-20:]
        buy_usd = sum(w.get("usd", 0) for w in whales if w.get("side") == "BUY")
        sell_usd = sum(w.get("usd", 0) for w in whales if w.get("side") == "SELL")
        tot_usd = buy_usd + sell_usd
        whale_norm = 50 if tot_usd == 0 else max(0, min(100, 50 + (buy_usd - sell_usd) / tot_usd * 50))

        composite = round(0.4 * hist_norm + 0.4 * tech_norm + 0.2 * whale_norm)
        if composite >= 58:
            direction, dlabel = "yükseliş", "Yükseliş eğilimi"
        elif composite <= 42:
            direction, dlabel = "düşüş", "Düşüş eğilimi"
        else:
            direction, dlabel = "yatay", "Yatay / kararsız"

        horizon_map = {"15m": "~1 saat", "1h": "~4 saat", "4h": "~16 saat", "1d": "~4 gün"}

        result = {
            "symbol": symbol,
            "interval": interval,
            "horizon": horizon_map.get(interval, f"{_HORIZON} mum"),
            "probabilities": probs,
            "sample_count": total,
            "matched_on": matched_on,
            "composite": {"score": composite, "direction": direction, "label": dlabel},
            "components": {
                "technical": round(tech_norm),
                "historical": round(hist_norm),
                "whale": round(whale_norm),
            },
            "price": closes[-1],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "disclaimer": ("Geçmiş verilere dayalı istatistiksel dağılımdır. "
                           "Gelecek garantisi değildir, yatırım tavsiyesi değildir."),
        }
        _pred_cache[ck] = {"data": result, "ts": _time.time()}
        return result

    print("[PREDICTION] ✅ Endpoint register edildi: /predict/{symbol}")