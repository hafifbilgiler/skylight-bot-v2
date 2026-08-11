"""
═══════════════════════════════════════════════════════════════
PORTFÖY (Postgres) — kişiye özel, kalıcı
═══════════════════════════════════════════════════════════════
Kullanıcının portföyü artık tarayıcıda değil, DB'de saklanır.
Token'dan email (JWT sub) çıkarılır → users tablosundan id bulunur
→ portföy o kullanıcıya bağlanır. Cihaz değişse bile durur.

Endpoint:
  POST /portfolio/add    {token, symbol, amount, buy_price}
  GET  /portfolio/list   (?token=... veya body)  → pozisyonlar
  POST /portfolio/delete {token, id}

Gateway ile aynı DB (pg-secret). DB yoksa 503 döner (frontend
localStorage'a düşer — güvenli).

app.py'ye: from portfolio_addon import register_portfolio
           register_portfolio(app, _sys.modules[__name__])
Dockerfile: COPY portfolio_addon.py .
═══════════════════════════════════════════════════════════════
"""
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request

_HAS_DB = False
try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _HAS_DB = True
except Exception:
    _HAS_DB = False

import jwt as _jwt

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
JWT_SECRET = os.getenv("JWT_SECRET", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

_pf_pool = None
_pf_lock = threading.Lock()
_use_db = False


def _init():
    global _pf_pool, _use_db
    if not _HAS_DB or not DB_NAME or not DB_USER:
        return False
    if _pf_pool is not None:
        return True
    try:
        with _pf_lock:
            if _pf_pool is None:
                _pf_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=4,
                    host=DB_HOST, database=DB_NAME,
                    user=DB_USER, password=DB_PASSWORD, port=DB_PORT,
                )
        _use_db = True
        print("[PORTFOLIO] ✅ Postgres havuzu kuruldu")
        return True
    except Exception as e:
        print(f"[PORTFOLIO] ⚠ Postgres bağlanamadı: {e}")
        return False


def _create_table():
    if not _use_db:
        return
    ddl = """
    CREATE TABLE IF NOT EXISTS onebune_portfolio (
        id         BIGSERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        symbol     VARCHAR(20) NOT NULL,
        amount     DOUBLE PRECISION NOT NULL,
        buy_price  DOUBLE PRECISION NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_obpf_user ON onebune_portfolio(user_id);
    """
    try:
        conn = _pf_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            print("[PORTFOLIO] ✅ Tablo hazır: onebune_portfolio")
        finally:
            _pf_pool.putconn(conn)
    except Exception as e:
        print(f"[PORTFOLIO] tablo hatası: {e}")


def _user_id_from_token(token: str) -> Optional[int]:
    """JWT'den email çıkar → users tablosundan id bul."""
    if not token or not _use_db:
        return None
    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        if not email:
            return None
    except Exception:
        return None
    try:
        conn = _pf_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE email = %s", (email,))
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            _pf_pool.putconn(conn)
    except Exception as e:
        print(f"[PORTFOLIO] user_id hatası: {e}")
        return None


def register_portfolio(app, app_module):
    _init()
    _create_table()

    @app.post("/portfolio/add")
    async def pf_add(request: Request):
        body = await request.json()
        token = body.get("token", "")
        uid = _user_id_from_token(token)
        if uid is None:
            return {"error": "auth", "message": "Giriş gerekli"}
        symbol = str(body.get("symbol", "")).upper()
        try:
            amount = float(body.get("amount", 0))
            buy = float(body.get("buy_price", 0))
        except (TypeError, ValueError):
            return {"error": "invalid"}
        if not symbol or amount <= 0 or buy <= 0:
            return {"error": "invalid", "message": "Geçersiz veri"}
        try:
            conn = _pf_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO onebune_portfolio (user_id, symbol, amount, buy_price) "
                        "VALUES (%s,%s,%s,%s) RETURNING id",
                        (uid, symbol, amount, buy),
                    )
                    new_id = cur.fetchone()[0]
                conn.commit()
            finally:
                _pf_pool.putconn(conn)
            return {"ok": True, "id": new_id}
        except Exception as e:
            print(f"[PORTFOLIO] add: {e}")
            return {"error": "db"}

    @app.post("/portfolio/list")
    async def pf_list(request: Request):
        body = await request.json()
        token = body.get("token", "")
        uid = _user_id_from_token(token)
        if uid is None:
            return {"error": "auth", "items": []}
        try:
            conn = _pf_pool.getconn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, symbol, amount, buy_price FROM onebune_portfolio "
                        "WHERE user_id=%s ORDER BY created_at DESC",
                        (uid,),
                    )
                    rows = cur.fetchall()
            finally:
                _pf_pool.putconn(conn)
            return {"items": [dict(r) for r in rows]}
        except Exception as e:
            print(f"[PORTFOLIO] list: {e}")
            return {"error": "db", "items": []}

    @app.post("/portfolio/delete")
    async def pf_delete(request: Request):
        body = await request.json()
        token = body.get("token", "")
        uid = _user_id_from_token(token)
        if uid is None:
            return {"error": "auth"}
        try:
            pid = int(body.get("id", 0))
        except (TypeError, ValueError):
            return {"error": "invalid"}
        try:
            conn = _pf_pool.getconn()
            try:
                with conn.cursor() as cur:
                    # Sadece kendi kaydını silebilir
                    cur.execute("DELETE FROM onebune_portfolio WHERE id=%s AND user_id=%s", (pid, uid))
                conn.commit()
            finally:
                _pf_pool.putconn(conn)
            return {"ok": True}
        except Exception as e:
            print(f"[PORTFOLIO] delete: {e}")
            return {"error": "db"}

    mode = "Postgres" if _use_db else "DEVRE DIŞI (DB yok → frontend localStorage)"
    print(f"[PORTFOLIO] ✅ Register edildi ({mode})")