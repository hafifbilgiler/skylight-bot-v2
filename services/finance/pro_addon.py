"""
═══════════════════════════════════════════════════════════════
PRO ADDON — Profesyonellik Katmanı
═══════════════════════════════════════════════════════════════
1. HABER ÖN-ISITMA: her 30 dk popüler coinlerin haberlerini arka
   planda AI ile analiz eder → kullanıcı açınca ANINDA yüklenir.
2. BALİNA GERİ-DOLDURMA: pod başlarken Binance aggTrades geçmişinden
   büyük işlemleri çekip whale_history'yi doldurur → radar asla boş
   başlamaz.
3. BALİNA KALICILAŞTIRMA: her 5 dk /tmp'ye kaydeder, başlangıçta
   yükler → container restart'ında veri kaybolmaz.

app.py'ye ekle (prediction'ın yanına):
  from pro_addon import register_pro
  register_pro(app, _sys.modules[__name__])

Dockerfile'a: COPY pro_addon.py .
═══════════════════════════════════════════════════════════════
"""
import asyncio
import json
import os
import time as _time
from datetime import datetime, timezone

import httpx

_WHALE_FILE = "/tmp/onebune_whales.json"
_PREWARM_COINS = ["BTC", "ETH", "SOL", "XRP"]
_PREWARM_INTERVAL = 1800  # 30 dk
_SAVE_INTERVAL = 300      # 5 dk


def _thresh(app_module, symbol: str) -> float:
    fn = getattr(app_module, "whale_thresh_for", None)
    if fn:
        return fn(symbol)
    return getattr(app_module, "WHALE_USD_THRESH", 500000)


# ─────────────── Balina: kalıcılaştırma ───────────────

def _save_whales(app_module):
    try:
        wh = getattr(app_module, "whale_history", {})
        data = {sym: list(dq)[-100:] for sym, dq in wh.items()}
        with open(_WHALE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[PRO] whale kayıt hatası: {e}")


def _load_whales(app_module):
    try:
        if not os.path.exists(_WHALE_FILE):
            return 0
        with open(_WHALE_FILE) as f:
            data = json.load(f)
        wh = getattr(app_module, "whale_history", {})
        count = 0
        for sym, items in data.items():
            if sym in wh:
                for w in items:
                    wh[sym].append(w)
                    count += 1
        return count
    except Exception as e:
        print(f"[PRO] whale yükleme hatası: {e}")
        return 0


# ─────────────── Balina: Binance geçmişten doldur ───────────────

async def _backfill_whales(app_module):
    """Binance'ten son 6 saatin büyük işlemlerini çek, radar dolu başlasın."""
    supported = getattr(app_module, "SUPPORTED_COINS", [])
    wh = getattr(app_module, "whale_history", {})
    total = 0
    now_ms = int(_time.time() * 1000)
    six_hours_ms = 6 * 3600 * 1000
    async with httpx.AsyncClient(timeout=15.0) as client:
        for symbol in supported:
            try:
                thr = _thresh(app_module, symbol)
                existing_ts = {w.get("timestamp") for w in wh.get(symbol, [])}
                found = []
                # Son 6 saati 30 dakikalık dilimlerle tara (aggTrades zaman aralığı)
                start = now_ms - six_hours_ms
                while start < now_ms:
                    end = min(start + 30 * 60 * 1000, now_ms)
                    try:
                        r = await client.get(
                            "https://api.binance.com/api/v3/aggTrades",
                            params={"symbol": symbol, "startTime": start, "endTime": end, "limit": 1000},
                        )
                        if r.status_code == 200:
                            for t in r.json():
                                price = float(t.get("p", 0))
                                qty = float(t.get("q", 0))
                                usd = price * qty
                                if usd >= thr:
                                    ts = datetime.fromtimestamp(t.get("T", 0) / 1000, tz=timezone.utc).isoformat()
                                    if ts in existing_ts:
                                        continue
                                    existing_ts.add(ts)
                                    side = "SELL" if t.get("m", False) else "BUY"
                                    found.append({
                                        "type": "whale", "symbol": symbol, "side": side,
                                        "usd": round(usd, 0), "price": price, "qty": qty,
                                        "emoji": "🐋 WHALE ALIM" if side == "BUY" else "🐋 WHALE SATIŞ",
                                        "timestamp": ts,
                                    })
                    except Exception:
                        pass
                    start = end
                    await asyncio.sleep(0.12)  # rate limit nezaketi
                # Zaman sırasına diz, ekle
                found.sort(key=lambda x: x["timestamp"])
                for w in found:
                    wh[symbol].append(w)
                total += len(found)
            except Exception as e:
                print(f"[PRO] backfill {symbol}: {e}")
    print(f"[PRO] ✅ Balina backfill (6 saat): {total} geçmiş işlem yüklendi")


# ─────────────── Haber: arka plan ön-ısıtma ───────────────

async def _prewarm_news(app_module):
    """Popüler coinlerin haberlerini önceden analiz et → cache sıcak kalır."""
    while True:
        try:
            get_news = None
            # app.py'deki get_news endpoint fonksiyonunu bul
            for attr in ("get_news",):
                get_news = getattr(app_module, attr, None)
                if get_news:
                    break
            if get_news:
                for coin in _PREWARM_COINS:
                    try:
                        await get_news(symbol=coin, analyze=True)
                        await asyncio.sleep(2)
                    except Exception as e:
                        print(f"[PRO] prewarm {coin}: {e}")
                print(f"[PRO] ✅ Haber ön-ısıtma tamam ({len(_PREWARM_COINS)} coin)")
        except Exception as e:
            print(f"[PRO] prewarm döngü: {e}")
        await asyncio.sleep(_PREWARM_INTERVAL)


async def _periodic_save(app_module):
    while True:
        await asyncio.sleep(_SAVE_INTERVAL)
        _save_whales(app_module)


# ─────────────── Register ───────────────

def register_pro(app, app_module):
    @app.on_event("startup")
    async def _pro_startup():
        # 1. Kalıcı dosyadan yükle
        loaded = _load_whales(app_module)
        if loaded:
            print(f"[PRO] ✅ {loaded} balina kalıcı dosyadan yüklendi")
        # 2. Binance geçmişinden doldur (arka planda, açılışı bloklamasın)
        asyncio.create_task(_backfill_whales(app_module))
        # 3. Haber ön-ısıtma döngüsü (ilk tur 60 sn sonra — açılış yükünü bekle)
        async def _delayed_prewarm():
            await asyncio.sleep(60)
            await _prewarm_news(app_module)
        asyncio.create_task(_delayed_prewarm())
        # 4. Periyodik balina kaydı
        asyncio.create_task(_periodic_save(app_module))

    print("[PRO] ✅ Pro katman register edildi (backfill + prewarm + persist)")