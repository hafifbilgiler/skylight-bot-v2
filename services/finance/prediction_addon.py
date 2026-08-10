"""
═══════════════════════════════════════════════════════════════
CANLI TAHMİN v2 — Ağırlıklı Benzerlik Motoru (KNN)
═══════════════════════════════════════════════════════════════
v1: kaba kutu eşleştirme. v2: her mumun 5 özellikli parmak izi
(RSI, momentum, trend mesafesi, volatilite, hacim oranı) ile
geçmişteki EN BENZER 40 durumu bulur (ağırlıklı mesafe),
sonuç dağılımını, beklenen hareket aralığını, güven seviyesini
ve kullanıcıya disiplin yorumunu üretir.

Dürüstlük ilkesi: garanti yok, geçmiş dağılım var. Sistemin işi
kazandırmak değil — kötü (duygusal) kararları azaltmak.

Endpoint: GET /predict/{symbol}?interval=1h
app.py register aynı: register_prediction(app, _sys.modules[__name__])
═══════════════════════════════════════════════════════════════
"""
import math
import time as _time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import Query, HTTPException

_app_module = None
_pred_cache: Dict[str, Dict] = {}
_PRED_TTL = 60

_THRESHOLDS = {"15m": 0.003, "1h": 0.005, "4h": 0.010, "1d": 0.020}
_HORIZON = 4
_K = 40  # en benzer kaç geçmiş durum

# Özellik ağırlıkları (mesafe hesabında)
_W = {"rsi": 0.30, "mom": 0.25, "gap": 0.20, "vol": 0.15, "vr": 0.10}


# ─────────────────────────── Seriler ───────────────────────────

def _rsi_at(closes: List[float], i: int, period: int = 14) -> Optional[float]:
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


def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out: List[Optional[float]] = [None] * (period - 1)
    ema = sum(values[:period]) / period
    out.append(ema)
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _features(closes, volumes, rsi_vals, e9, e21, i) -> Optional[Dict[str, float]]:
    """Mum i'nin normalize edilmiş 5 özellikli parmak izi (hepsi ~0..1)."""
    if i < 25 or rsi_vals[i] is None or e9[i] is None or e21[i] is None:
        return None
    rsi = rsi_vals[i] / 100.0
    mom = _clamp((closes[i] - closes[i - 5]) / closes[i - 5], -0.05, 0.05) / 0.10 + 0.5
    gap = _clamp((e9[i] - e21[i]) / e21[i], -0.03, 0.03) / 0.06 + 0.5
    # Volatilite: son 14 mum getiri std sapması
    rets = [(closes[j] / closes[j - 1] - 1) for j in range(i - 13, i + 1)]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    vol = _clamp(vol, 0, 0.03) / 0.03
    # Hacim oranı: son mum / 20 mum ortalaması
    vr = 0.5
    if volumes and volumes[i] is not None:
        window = [v for v in volumes[i - 19:i + 1] if v]
        if window and sum(window) > 0:
            avg = sum(window) / len(window)
            if avg > 0:
                vr = _clamp(volumes[i] / avg, 0, 3) / 3.0
    return {"rsi": rsi, "mom": mom, "gap": gap, "vol": vol, "vr": vr}


def _dist(a: Dict, b: Dict) -> float:
    return math.sqrt(sum(_W[k] * (a[k] - b[k]) ** 2 for k in _W))


def _wpercentile(pairs: List[tuple], q: float) -> float:
    """Ağırlıklı yüzdelik: pairs = [(değer, ağırlık)], q 0..1."""
    if not pairs:
        return 0.0
    s = sorted(pairs, key=lambda x: x[0])
    total = sum(w for _, w in s)
    if total <= 0:
        return s[len(s) // 2][0]
    acc = 0.0
    for v, w in s:
        acc += w
        if acc / total >= q:
            return v
    return s[-1][0]


# ─────────────────── Kendi teknik mini-skoru ───────────────────
# detect_signals hata verirse bile panel çalışsın diye bağımsız hesap

def _own_tech_score(rsi, mom_raw, gap_raw) -> float:
    s = 0.0
    if gap_raw > 0: s += 1.5
    else: s -= 1.5
    if mom_raw > 0.01: s += 1.0
    elif mom_raw < -0.01: s -= 1.0
    if rsi < 30: s += 1.5      # aşırı satım → toparlanma potansiyeli
    elif rsi > 70: s -= 1.5    # aşırı alım → düzeltme riski
    return _clamp(s, -5, 5)


# ─────────────────── Kullanıcı yorumu (disiplin) ───────────────────

def _interpretation(score, rsi, mom_raw, whale_norm, confidence) -> str:
    parts = []
    if score >= 58:
        parts.append("Geçmiş benzerlikler yükseliş lehine.")
        parts.append("Disiplin: tek seferde büyük pozisyon yerine kademeli hareket, önceden belirlenmiş stop seviyesi.")
    elif score <= 42:
        parts.append("Geçmiş benzerlikler düşüş riskine işaret ediyor.")
        parts.append("Disiplin: acele 'dipten alma' denemesi yerine netleşme beklemek geçmişte daha az hata yaptırdı.")
    else:
        parts.append("Kararsız bölge — net bir yön avantajı yok.")
        parts.append("Profesyoneller bu bölgede işlem sıklığını azaltır; işlem yapmamak da bir pozisyondur.")
    # Duygu freni (FOMO / panik)
    if rsi > 72 and mom_raw > 0.02:
        parts.append("⚠️ FOMO uyarısı: fiyat kısa sürede hızlı yükseldi. Tepe kovalamak en sık para kaybettiren davranıştır.")
    elif rsi < 28 and mom_raw < -0.02:
        parts.append("⚠️ Panik uyarısı: sert düşüş sonrası duyguyla satış, geçmişte çoğu zaman en kötü zamanlama oldu.")
    if whale_norm >= 65:
        parts.append("Büyük oyuncular net alıcı tarafta.")
    elif whale_norm <= 35:
        parts.append("Büyük oyuncular net satıcı tarafta.")
    if confidence == "düşük":
        parts.append("Örnek sayısı az — bu tabloya tek başına güvenme.")
    return " ".join(parts)


# ─────────────────────────── Register ───────────────────────────

def register_prediction(app, app_module):
    global _app_module
    _app_module = app_module

    @app.get("/predict/{symbol}")
    async def predict(symbol: str, interval: str = Query("1h")):
        symbol = symbol.upper()
        supported = getattr(app_module, "SUPPORTED_COINS", [])
        if symbol not in supported:
            raise HTTPException(404)

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
        volumes = []
        for k in klines:
            try:
                volumes.append(float(k.get("v", 0)) or None)
            except (TypeError, ValueError):
                volumes.append(None)

        n = len(closes)
        thr = _THRESHOLDS.get(interval, 0.005)

        rsi_vals: List[Optional[float]] = [None] * n
        for i in range(15, n):
            rsi_vals[i] = _rsi_at(closes, i)
        e9 = _ema_series(closes, 9)
        e21 = _ema_series(closes, 21)

        cur = _features(closes, volumes, rsi_vals, e9, e21, n - 1)
        if cur is None:
            return {"error": "Durum hesaplanamadı", "symbol": symbol}

        # ── KNN: en benzer geçmiş durumlar ──
        candidates = []
        for i in range(30, n - _HORIZON):
            f = _features(closes, volumes, rsi_vals, e9, e21, i)
            if f is None:
                continue
            d = _dist(cur, f)
            fwd = closes[i + _HORIZON] / closes[i] - 1
            candidates.append((d, fwd))
        candidates.sort(key=lambda x: x[0])
        nearest = candidates[:_K]

        if not nearest:
            probs = {"up": 33, "flat": 34, "down": 33}
            exp_low = exp_high = 0.0
            sample_count = 0
        else:
            # Benzerlik ağırlığı: yakın olan daha çok sayılır
            weighted = [(fwd, 1.0 / (0.02 + d)) for d, fwd in nearest]
            wsum = sum(w for _, w in weighted)
            up_w = sum(w for r, w in weighted if r > thr)
            dn_w = sum(w for r, w in weighted if r < -thr)
            fl_w = wsum - up_w - dn_w
            probs = {
                "up": round(up_w / wsum * 100),
                "flat": round(fl_w / wsum * 100),
                "down": round(dn_w / wsum * 100),
            }
            # yuvarlama farkını düzelt
            diff = 100 - sum(probs.values())
            probs["flat"] += diff
            exp_low = _wpercentile(weighted, 0.25) * 100
            exp_high = _wpercentile(weighted, 0.75) * 100
            sample_count = len(nearest)

        # ── Bileşenler ──
        rsi_now = rsi_vals[n - 1] or 50
        mom_raw = (closes[-1] - closes[-6]) / closes[-6]
        gap_raw = (e9[-1] - e21[-1]) / e21[-1] if e9[-1] and e21[-1] else 0

        # Teknik: önce mevcut motor, hata verirse kendi hesabımız (panel asla boş kalmaz)
        tech_score = None
        try:
            sig = app_module.detect_signals(symbol, interval)
            if isinstance(sig, dict) and "error" not in sig:
                tech_score = sig.get("signal", {}).get("score")
        except Exception:
            pass
        if tech_score is None:
            tech_score = _own_tech_score(rsi_now, mom_raw, gap_raw)
        tech_norm = _clamp((tech_score + 5) * 10, 0, 100)

        hist_norm = _clamp(50 + (probs["up"] - probs["down"]) / 2, 0, 100)

        whale_history = getattr(app_module, "whale_history", {})
        whales = list(whale_history.get(symbol, []))[-20:]
        buy_usd = sum(w.get("usd", 0) for w in whales if w.get("side") == "BUY")
        sell_usd = sum(w.get("usd", 0) for w in whales if w.get("side") == "SELL")
        tot = buy_usd + sell_usd
        whale_norm = 50 if tot == 0 else _clamp(50 + (buy_usd - sell_usd) / tot * 50, 0, 100)

        composite = round(0.4 * hist_norm + 0.4 * tech_norm + 0.2 * whale_norm)
        if composite >= 58:
            direction, dlabel = "yükseliş", "Yükseliş eğilimi"
        elif composite <= 42:
            direction, dlabel = "düşüş", "Düşüş eğilimi"
        else:
            direction, dlabel = "yatay", "Yatay / kararsız"

        # ── Güven seviyesi ──
        max_prob = max(probs.values())
        if sample_count >= 30 and max_prob >= 55:
            confidence = "yüksek"
        elif sample_count >= 20 and max_prob >= 45:
            confidence = "orta"
        else:
            confidence = "düşük"

        horizon_map = {"15m": "~1 saat", "1h": "~4 saat", "4h": "~16 saat", "1d": "~4 gün"}
        horizon = horizon_map.get(interval, f"{_HORIZON} mum")

        result = {
            "symbol": symbol,
            "interval": interval,
            "horizon": horizon,
            "probabilities": probs,
            "sample_count": sample_count,
            "matched_on": "5 özellikli benzerlik (RSI, momentum, trend, volatilite, hacim)",
            "expected_range": {"low": round(exp_low, 2), "high": round(exp_high, 2)},
            "confidence": confidence,
            "composite": {"score": composite, "direction": direction, "label": dlabel},
            "components": {
                "technical": round(tech_norm),
                "historical": round(hist_norm),
                "whale": round(whale_norm),
            },
            "interpretation": _interpretation(composite, rsi_now, mom_raw, whale_norm, confidence),
            "price": closes[-1],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "disclaimer": ("Geçmiş verilere dayalı istatistiksel dağılımdır. "
                           "Gelecek garantisi değildir, yatırım tavsiyesi değildir."),
        }
        _pred_cache[ck] = {"data": result, "ts": _time.time()}
        return result

    print("[PREDICTION] ✅ v2 register edildi: /predict/{symbol} (KNN benzerlik motoru)")