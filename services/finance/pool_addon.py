"""
═══════════════════════════════════════════════════════════════
ÖĞRENEN TAHMİN HAVUZU (Postgres)
═══════════════════════════════════════════════════════════════
Her saat 12 coin için "anlık görüntü" kaydeder:
  durum (rsi, trend, momentum, volatilite, hacim, haber, balina)
  + o an verilen tahmin
4 saat sonra "gerçekte ne oldu" yazılır (tuttu/tutmadı).
Havuz büyüdükçe tahmin GERÇEK geçmişe bakar:
  "tam bu koşullarda geçmişte N kez ne oldu?"

Gateway ile AYNI desen: psycopg2 ThreadedConnectionPool.
DB env yoksa → otomatik /tmp/onebune_pool.json'a düşer (çökmez).

YAZMA: saatte 1, 12 satır toplu → günde 288 satır (DB'yi yormaz).
OKUMA: arka planda periyodik, RAM'e alınır (tahmin anında DB'ye gitmez).

app.py'ye: from pool_addon import register_pool
           register_pool(app, _sys.modules[__name__])
Dockerfile: COPY pool_addon.py .
requirements: psycopg2-binary==2.9.9
YAML env: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD (gateway ile aynı pg-secret)
═══════════════════════════════════════════════════════════════
"""
import asyncio
import json
import math
import os
import threading
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

# ── DB bağlantısı (gateway ile aynı desen) ──
_HAS_DB = False
try:
    import psycopg2
    import psycopg2.pool
    import psycopg2.extras
    _HAS_DB = True
except Exception:
    _HAS_DB = False

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

_POOL_FILE = "/tmp/onebune_pool.json"   # DB yoksa fallback
_db_pool = None
_pool_lock = threading.Lock()
_use_db = False

_INTERVAL = "1h"
_HORIZON = 4                # kaç mum sonrasına bakılır (4 saat)
_SNAPSHOT_EVERY = 3600      # saatte 1 kayıt
_RESULT_CHECK_EVERY = 900   # 15 dk'da bir sonuçları doldur


def _init_pool() -> bool:
    """DB havuzunu kur. Başarılıysa True."""
    global _db_pool, _use_db
    if not _HAS_DB or not DB_NAME or not DB_USER:
        return False
    if _db_pool is not None:
        return True
    try:
        with _pool_lock:
            if _db_pool is None:
                _db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=5,
                    host=DB_HOST, database=DB_NAME,
                    user=DB_USER, password=DB_PASSWORD, port=DB_PORT,
                )
        _use_db = True
        print("[POOL] ✅ Postgres bağlantı havuzu kuruldu (1-5)")
        return True
    except Exception as e:
        print(f"[POOL] ⚠ Postgres bağlanamadı, /tmp fallback: {e}")
        return False


def _create_table():
    if not _use_db:
        return
    ddl = """
    CREATE TABLE IF NOT EXISTS onebune_market_snapshots (
        id           BIGSERIAL PRIMARY KEY,
        symbol       VARCHAR(20) NOT NULL,
        snap_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
        price        DOUBLE PRECISION,
        rsi          DOUBLE PRECISION,
        momentum     DOUBLE PRECISION,
        trend_gap    DOUBLE PRECISION,
        volatility   DOUBLE PRECISION,
        volume_ratio DOUBLE PRECISION,
        news_score   DOUBLE PRECISION,
        whale_score  DOUBLE PRECISION,
        pred_dir     VARCHAR(12),
        pred_score   INTEGER,
        result_price DOUBLE PRECISION,
        result_dir   VARCHAR(12),
        correct      BOOLEAN,
        evaluated    BOOLEAN DEFAULT FALSE
    );
    CREATE INDEX IF NOT EXISTS idx_obms_symbol_ts ON onebune_market_snapshots(symbol, snap_ts);
    CREATE INDEX IF NOT EXISTS idx_obms_eval ON onebune_market_snapshots(evaluated);
    """
    try:
        conn = _db_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            print("[POOL] ✅ Tablo hazır: onebune_market_snapshots")
        finally:
            _db_pool.putconn(conn)
    except Exception as e:
        print(f"[POOL] tablo hatası: {e}")


# ─────────────── Özellik çıkarımı ───────────────

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _extract_features(app_module, symbol: str) -> Optional[Dict]:
    """Bir coin için o anki durumu (özellikler + tahmin) çıkar."""
    kline_cache = getattr(app_module, "kline_cache", {})
    klines = list(kline_cache.get(symbol, {}).get(_INTERVAL, []))
    if len(klines) < 30:
        return None
    closes = [float(k["c"]) for k in klines]
    volumes = []
    for k in klines:
        try:
            volumes.append(float(k.get("v", 0)))
        except (TypeError, ValueError):
            volumes.append(0)

    calc_rsi = getattr(app_module, "calc_rsi", None)
    rsi = calc_rsi(closes) if calc_rsi else None

    momentum = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] else 0
    # trend: ema9-ema21 farkı
    calc_ema = getattr(app_module, "calc_ema", None)
    trend_gap = 0
    if calc_ema:
        e9 = calc_ema(closes, 9)
        e21 = calc_ema(closes, 21)
        if e9 and e21 and e9[-1] and e21[-1]:
            trend_gap = (e9[-1] - e21[-1]) / e21[-1]
    # volatilite
    rets = [(closes[i] / closes[i-1] - 1) for i in range(len(closes)-14, len(closes)) if i > 0]
    vol = (sum((r - sum(rets)/len(rets))**2 for r in rets)/len(rets))**0.5 if rets else 0
    # hacim oranı
    vratio = 1.0
    vv = [v for v in volumes[-20:] if v]
    if vv and sum(vv) > 0:
        avg = sum(vv) / len(vv)
        if avg > 0 and volumes[-1]:
            vratio = volumes[-1] / avg

    # Haber skoru (0-100)
    news_cache = getattr(app_module, "_news_analysis_cache", {})
    coin_short = symbol.replace("USDT", "")
    nv = []
    for a in list(news_cache.values())[-100:]:
        if not isinstance(a, dict):
            continue
        coins = [str(c).upper() for c in (a.get("affected_coins") or [])]
        if coins and coin_short not in coins and "BTC" not in coins:
            continue
        imp = (a.get("impact") or "nötr").lower()
        stw = {"zayıf": 0.5, "orta": 1.0, "güçlü": 1.5}.get((a.get("strength") or "orta").lower(), 1.0)
        nv.append(stw if "yük" in imp else -stw if "düş" in imp else 0.0)
    news_score = _clamp(50 + (sum(nv)/len(nv))*30, 0, 100) if nv else 50.0

    # Balina skoru
    wh = getattr(app_module, "whale_history", {})
    whales = list(wh.get(symbol, []))[-20:]
    buy = sum(w.get("usd", 0) for w in whales if w.get("side") == "BUY")
    sell = sum(w.get("usd", 0) for w in whales if w.get("side") == "SELL")
    tot = buy + sell
    whale_score = _clamp(50 + (buy-sell)/tot*50, 0, 100) if tot else 50.0

    # O anki tahmin (prediction_addon varsa cache'inden, yoksa basit)
    pred_dir, pred_score = "yatay", 50
    try:
        import prediction_addon as pa
        c = pa._pred_cache.get(f"{symbol}:{_INTERVAL}")
        if c:
            comp = c["data"].get("composite", {})
            pred_dir = comp.get("direction", "yatay")
            pred_score = comp.get("score", 50)
    except Exception:
        pass

    return {
        "symbol": symbol, "price": closes[-1], "rsi": rsi,
        "momentum": momentum, "trend_gap": trend_gap, "volatility": vol,
        "volume_ratio": vratio, "news_score": news_score, "whale_score": whale_score,
        "pred_dir": pred_dir, "pred_score": pred_score,
    }


# ─────────────── Kayıt (DB veya dosya) ───────────────

def _save_snapshots(rows: List[Dict]):
    if _use_db:
        try:
            conn = _db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO onebune_market_snapshots
                           (symbol, price, rsi, momentum, trend_gap, volatility,
                            volume_ratio, news_score, whale_score, pred_dir, pred_score)
                           VALUES %s""",
                        [(r["symbol"], r["price"], r["rsi"], r["momentum"], r["trend_gap"],
                          r["volatility"], r["volume_ratio"], r["news_score"], r["whale_score"],
                          r["pred_dir"], r["pred_score"]) for r in rows],
                    )
                conn.commit()
            finally:
                _db_pool.putconn(conn)
        except Exception as e:
            print(f"[POOL] kayıt hatası: {e}")
    else:
        # /tmp fallback
        try:
            data = []
            if os.path.exists(_POOL_FILE):
                with open(_POOL_FILE) as f:
                    data = json.load(f)
            now = datetime.now(timezone.utc).isoformat()
            for r in rows:
                data.append({**r, "snap_ts": now, "evaluated": False})
            data = data[-5000:]
            with open(_POOL_FILE, "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"[POOL] dosya kayıt hatası: {e}")


def _evaluate_results(app_module):
    """4 saat dolan kayıtların sonucunu yaz (gerçekleşen fiyat/yön)."""
    kline_cache = getattr(app_module, "kline_cache", {})
    thr = 0.005  # 1h için yön eşiği

    def actual_dir(old_price, new_price):
        r = new_price / old_price - 1
        return "yükseliş" if r > thr else "düşüş" if r < -thr else "yatay"

    if _use_db:
        try:
            conn = _db_pool.getconn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=_HORIZON)
                    cur.execute(
                        """SELECT id, symbol, price, pred_dir FROM onebune_market_snapshots
                           WHERE evaluated = FALSE AND snap_ts <= %s LIMIT 500""",
                        (cutoff,),
                    )
                    rows = cur.fetchall()
                    for row in rows:
                        cur_price = (getattr(app_module, "price_cache", {}) or {}).get(row["symbol"], 0)
                        if not cur_price:
                            continue
                        ad = actual_dir(row["price"], cur_price)
                        correct = (ad == row["pred_dir"])
                        cur.execute(
                            """UPDATE onebune_market_snapshots
                               SET result_price=%s, result_dir=%s, correct=%s, evaluated=TRUE
                               WHERE id=%s""",
                            (cur_price, ad, correct, row["id"]),
                        )
                conn.commit()
            finally:
                _db_pool.putconn(conn)
        except Exception as e:
            print(f"[POOL] değerlendirme hatası: {e}")


# ─────────────── Öğrenme sorgusu ───────────────

def query_similar(app_module, symbol: str, features: Dict) -> Optional[Dict]:
    """Havuzda benzer geçmiş durumları bul, sonuç dağılımını döndür."""
    if not _use_db or not features:
        return None
    try:
        conn = _db_pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Aynı coin + değerlendirilmiş + benzer RSI/momentum aralığı
                cur.execute(
                    """SELECT result_dir, COUNT(*) as n FROM onebune_market_snapshots
                       WHERE symbol=%s AND evaluated=TRUE
                         AND rsi BETWEEN %s AND %s
                         AND momentum BETWEEN %s AND %s
                       GROUP BY result_dir""",
                    (symbol,
                     (features["rsi"] or 50) - 8, (features["rsi"] or 50) + 8,
                     features["momentum"] - 0.01, features["momentum"] + 0.01),
                )
                rows = cur.fetchall()
        finally:
            _db_pool.putconn(conn)
        total = sum(r["n"] for r in rows)
        if total < 5:
            return None
        dist = {"yükseliş": 0, "yatay": 0, "düşüş": 0}
        for r in rows:
            if r["result_dir"] in dist:
                dist[r["result_dir"]] = r["n"]
        return {
            "sample_count": total,
            "up": round(dist["yükseliş"] / total * 100),
            "flat": round(dist["yatay"] / total * 100),
            "down": round(dist["düşüş"] / total * 100),
            "source": "gerçek geçmiş havuzu",
        }
    except Exception as e:
        print(f"[POOL] sorgu hatası: {e}")
        return None


def scorecard(app_module) -> Dict:
    """Şeffaf isabet karnesi — coin bazında + genel + yön bazında.
    KİMSEDE OLMAYAN: gerçek başarı oranını dürüstçe gösterir."""
    if not _use_db:
        return {"enabled": False, "note": "Karne için veritabanı gerekli"}
    try:
        conn = _db_pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Genel
                cur.execute("SELECT COUNT(*) FILTER (WHERE evaluated) as ev, "
                            "COUNT(*) FILTER (WHERE correct) as ok FROM onebune_market_snapshots")
                g = cur.fetchone()
                overall_pct = round(g["ok"] / g["ev"] * 100) if g["ev"] else None
                # Coin bazında
                cur.execute("""SELECT symbol,
                               COUNT(*) FILTER (WHERE evaluated) as ev,
                               COUNT(*) FILTER (WHERE correct) as ok
                               FROM onebune_market_snapshots
                               WHERE evaluated = TRUE
                               GROUP BY symbol HAVING COUNT(*) FILTER (WHERE evaluated) >= 3
                               ORDER BY (COUNT(*) FILTER (WHERE correct))::float /
                                        NULLIF(COUNT(*) FILTER (WHERE evaluated),0) DESC""")
                per_coin = []
                for row in cur.fetchall():
                    if row["ev"]:
                        per_coin.append({
                            "symbol": row["symbol"],
                            "coin": row["symbol"].replace("USDT", ""),
                            "evaluated": row["ev"],
                            "correct": row["ok"],
                            "pct": round(row["ok"] / row["ev"] * 100),
                        })
                # Yön bazında (yükseliş tahminleri mi düşüş mü daha iyi?)
                cur.execute("""SELECT pred_dir,
                               COUNT(*) FILTER (WHERE evaluated) as ev,
                               COUNT(*) FILTER (WHERE correct) as ok
                               FROM onebune_market_snapshots
                               WHERE evaluated = TRUE GROUP BY pred_dir""")
                by_dir = {}
                for row in cur.fetchall():
                    if row["ev"]:
                        by_dir[row["pred_dir"]] = {
                            "evaluated": row["ev"], "correct": row["ok"],
                            "pct": round(row["ok"] / row["ev"] * 100),
                        }
        finally:
            _db_pool.putconn(conn)
        return {
            "enabled": True,
            "overall": {"evaluated": g["ev"], "correct": g["ok"], "pct": overall_pct},
            "per_coin": per_coin,
            "by_direction": by_dir,
            "note": ("Bu karne gerçek geçmiş tahminlerimizin sonucudur. "
                     "Hiçbir şey saklamıyoruz — tutmayanlar da burada."),
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def pool_stats(app_module) -> Dict:
    """Havuz istatistiği (kaç kayıt, isabet oranı)."""
    if not _use_db:
        return {"enabled": False, "storage": "geçici dosya"}
    try:
        conn = _db_pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT COUNT(*) as total, "
                            "COUNT(*) FILTER (WHERE evaluated) as evaluated, "
                            "COUNT(*) FILTER (WHERE correct) as correct "
                            "FROM onebune_market_snapshots")
                r = cur.fetchone()
        finally:
            _db_pool.putconn(conn)
        pct = round(r["correct"] / r["evaluated"] * 100) if r["evaluated"] else None
        return {"enabled": True, "storage": "postgres",
                "total": r["total"], "evaluated": r["evaluated"],
                "accuracy_pct": pct}
    except Exception as e:
        return {"enabled": True, "error": str(e)}


# ─────────────── Döngüler ───────────────

async def _snapshot_loop(app_module):
    await asyncio.sleep(30)   # açılışta verinin oturmasını bekle
    while True:
        try:
            supported = getattr(app_module, "SUPPORTED_COINS", [])
            rows = []
            for sym in supported:
                f = _extract_features(app_module, sym)
                if f and f["rsi"] is not None:
                    rows.append(f)
            if rows:
                _save_snapshots(rows)
                print(f"[POOL] {len(rows)} anlık görüntü kaydedildi")
        except Exception as e:
            print(f"[POOL] snapshot döngü: {e}")
        await asyncio.sleep(_SNAPSHOT_EVERY)


async def _result_loop(app_module):
    await asyncio.sleep(120)
    while True:
        try:
            _evaluate_results(app_module)
        except Exception as e:
            print(f"[POOL] result döngü: {e}")
        await asyncio.sleep(_RESULT_CHECK_EVERY)


# ─────────────── Register ───────────────

def register_pool(app, app_module):
    from fastapi import Query as _Q

    ok = _init_pool()
    if ok:
        _create_table()

    @app.get("/pool/stats")
    async def pool_stats_ep():
        return pool_stats(app_module)

    @app.get("/pool/scorecard")
    async def scorecard_ep():
        return scorecard(app_module)

    @app.on_event("startup")
    async def _pool_startup():
        asyncio.create_task(_snapshot_loop(app_module))
        asyncio.create_task(_result_loop(app_module))
        mode = "Postgres" if _use_db else "/tmp dosya"
        print(f"[POOL] ✅ Öğrenen havuz başladı ({mode}) — saatlik kayıt + 15dk sonuç")

    print("[POOL] ✅ Tahmin havuzu register edildi")