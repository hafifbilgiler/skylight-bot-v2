"""Premium abonelik kontrolü — gateway ile aynı PostgreSQL'e bakar.
DevOps Lab premium bir üründür: sadece is_premium kullanıcılar erişebilir.
"""
import os
import threading

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

_pool = None
_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                import psycopg2.pool
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=5,
                    host=DB_HOST, database=DB_NAME,
                    user=DB_USER, password=DB_PASSWORD, port=DB_PORT,
                )
    return _pool


def _user_id_from_email(cur, email):
    """Email → users.id (gateway ile aynı)."""
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    return row[0] if row else None


def is_premium_email(email) -> bool:
    """Email'e göre premium mi? (JWT'de sub=email olduğu için)."""
    if not email:
        return False
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        try:
            uid = _user_id_from_email(cur, email)
            if not uid:
                return False
            cur.execute("""
                SELECT us.plan_id, us.current_period_end
                FROM user_subscriptions us
                JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = %s AND us.status IN ('active', 'trialing')
                ORDER BY sp.sort_order DESC LIMIT 1
            """, (uid,))
            row = cur.fetchone()
            if not row:
                return False
            plan_id, period_end = row
            if period_end is not None:
                import datetime
                if period_end < datetime.datetime.now(datetime.timezone.utc):
                    return False
            return plan_id != "free"
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[PREMIUM CHECK ERROR] {e}")
        return False


def is_premium(user_id) -> bool:
    """Kullanıcının aktif premium (ücretsiz olmayan) aboneliği var mı?
    Gateway'deki get_user_subscription ile aynı sorgu mantığı."""
    if not user_id:
        return False
    try:
        pool = _get_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT us.plan_id, us.current_period_end
                FROM user_subscriptions us
                JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = %s AND us.status IN ('active', 'trialing')
                ORDER BY sp.sort_order DESC LIMIT 1
            """, (str(user_id),))
            row = cur.fetchone()
            if not row:
                return False
            plan_id, period_end = row
            if period_end is not None:
                import datetime
                if period_end < datetime.datetime.now(datetime.timezone.utc):
                    return False
            return plan_id != "free"
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"[PREMIUM CHECK ERROR] {e}")
        return False


def get_plan_info(user_id) -> dict:
    """Frontend için plan bilgisi (kilit ekranı kararı)."""
    prem = is_premium(user_id)
    return {"is_premium": prem, "plan": "premium" if prem else "free"}