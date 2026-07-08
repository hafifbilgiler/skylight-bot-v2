"""
═══════════════════════════════════════════════════════════════
BALİNA RADARI — Tüm Coinlerin Büyük İşlemleri (Finans Eklentisi)
═══════════════════════════════════════════════════════════════
Mevcut app.py'ye eklenir (JOHN/metals gibi register pattern).
app.py'nin ÇEKİRDEĞİNE DOKUNMAZ — sadece hafızadaki whale_history'yi okur.

Yeni endpoint:
  GET /whales/radar?minutes=60&limit=50
      → tüm coinlerin son N dakikadaki balinalarını
        tek listede, zamana göre sıralı döner + özet istatistik

app.py'nin sonuna ekle (metals'ın yanına):
  from whale_radar_addon import register_whale_radar
  register_whale_radar(app, _sys.modules[__name__])

Dockerfile'a ekle:
  COPY whale_radar_addon.py .
═══════════════════════════════════════════════════════════════
"""
import time as _time
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import Query

_app_module = None


def _parse_ts(ts) -> float:
    """ISO timestamp string → unix saniye."""
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def register_whale_radar(app, app_module):
    """Balina radarı endpoint'ini app'e bağla."""
    global _app_module
    _app_module = app_module

    @app.get("/whales/radar")
    async def whale_radar(
        minutes: int = Query(60, ge=1, le=1440),
        limit: int = Query(50, ge=1, le=200),
    ):
        """
        Tüm coinlerin son N dakikadaki balinalarını tek listede döner.
        Frontend bunu periyodik çeker (canlıya yakın radar).
        """
        whale_history = getattr(app_module, "whale_history", {})
        supported = getattr(app_module, "SUPPORTED_COINS", [])

        now = _time.time()
        cutoff = now - (minutes * 60)

        all_whales: List[Dict] = []

        # Tüm coinlerin balinalarını topla
        for symbol in supported:
            whales = list(whale_history.get(symbol, []))
            for w in whales:
                ts = _parse_ts(w.get("timestamp", 0))
                if ts >= cutoff:
                    all_whales.append({
                        "symbol": symbol,
                        "coin": symbol.replace("USDT", ""),
                        "side": w.get("side", "BUY"),
                        "usd": w.get("usd", 0),
                        "price": w.get("price", 0),
                        "qty": w.get("qty", 0),
                        "emoji": w.get("emoji", "🐋"),
                        "timestamp": w.get("timestamp", ""),
                        "_ts": ts,
                    })

        # Zamana göre sırala (en yeni önce)
        all_whales.sort(key=lambda x: x["_ts"], reverse=True)

        # Özet istatistik (son N dakika)
        buy_usd = sum(w["usd"] for w in all_whales if w["side"] == "BUY")
        sell_usd = sum(w["usd"] for w in all_whales if w["side"] == "SELL")
        buy_count = sum(1 for w in all_whales if w["side"] == "BUY")
        sell_count = sum(1 for w in all_whales if w["side"] == "SELL")

        # En aktif coin
        coin_activity: Dict[str, float] = {}
        for w in all_whales:
            coin_activity[w["coin"]] = coin_activity.get(w["coin"], 0) + w["usd"]
        top_coin = max(coin_activity, key=coin_activity.get) if coin_activity else None

        # _ts alanını temizle (frontend'e gitmesin)
        result_whales = all_whales[:limit]
        for w in result_whales:
            w.pop("_ts", None)

        return {
            "whales": result_whales,
            "count": len(result_whales),
            "total_in_window": len(all_whales),
            "window_minutes": minutes,
            "threshold_usd": getattr(app_module, "WHALE_USD_THRESH", 500000),
            "summary": {
                "buy_usd": round(buy_usd),
                "sell_usd": round(sell_usd),
                "buy_count": buy_count,
                "sell_count": sell_count,
                "net_usd": round(buy_usd - sell_usd),
                "sentiment": "alım baskın" if buy_usd > sell_usd * 1.2
                             else "satış baskın" if sell_usd > buy_usd * 1.2
                             else "dengeli",
                "top_coin": top_coin,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    print("[WHALE RADAR] ✅ Endpoint register edildi: /whales/radar")