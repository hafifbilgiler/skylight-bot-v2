# namaz_routes.py
"""
ONE-BUNE NAMAZ VAKITLERI APP - AUTH ROUTES

Namaz Vakitleri Android uygulamasina ozel email+OTP giris akisi.

ONEMLI: Bu dosya mevcut chatbot'un "users" / "otp_codes" tablolarina
HIC DOKUNMAZ. Kendi izole tablolarini kullanir:
  - namaz_app_users
  - namaz_app_otp_codes
  - namaz_app_subscriptions

JWT token'lar "scope": "namaz_app" alani tasir - bu sayede:
  - Namaz app'inden alinan token chatbot endpoint'lerinde calismaz
  - Chatbot'tan alinan token namaz app endpoint'lerinde calismaz
Iki sistem birbirine asla sizamaz.

main.py (gateway) icine eklenecek tek sey (dosyanin sonuna,
if __name__ == "__main__": satirindan ONCE):

    from namaz_routes import router as namaz_router
    app.include_router(namaz_router)

Bu dosya kendi DB pool'unu acar (ayni DB_HOST/DB_NAME env degiskenlerini
kullanir), boylece main.py'nin ic yapisina bagimli olmadan calisir.
"""

import os
import random
import datetime
import smtplib
import threading
from typing import Optional

import jwt
import psycopg2
import psycopg2.pool
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel, EmailStr

router = APIRouter()

# =====================================================
# CONFIG - main.py ile AYNI ortam degiskenlerinden okunur
# =====================================================

JWT_SECRET = os.getenv("JWT_SECRET", "31aad766798d891f4c587d7f3bc925cd7e1e14989c421ae3c38eb80c1d4ede05")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
TOKEN_EXPIRE_DAYS = int(os.getenv("TOKEN_EXPIRE_DAYS", "7"))

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.hostinger.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "noreply@one-bune.com")

ABUSE_CONTROL_URL = os.getenv("ABUSE_CONTROL_URL", "http://skylight-bot-abuse-control:8010")

# =====================================================
# SABIT TEST HESABI
# =====================================================
TEST_EMAIL = "testers@duavenamaz.com"
TEST_CODE = "123456"

# =====================================================
# KENDI DB POOL'U - main.py'deki pool'dan ayri bir nesne
# ama AYNI veritabanina baglanir
# =====================================================

_namaz_db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_namaz_pool_lock = threading.Lock()


def _get_namaz_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _namaz_db_pool
    if _namaz_db_pool is None or _namaz_db_pool.closed:
        with _namaz_pool_lock:
            if _namaz_db_pool is None or _namaz_db_pool.closed:
                _namaz_db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=5,
                    host=DB_HOST, database=DB_NAME,
                    user=DB_USER, password=DB_PASSWORD, port=DB_PORT,
                )
                print("[NAMAZ DB POOL] Initialized (1-5 connections)")
    return _namaz_db_pool


def _namaz_smtp_send(msg):
    """Port 465 -> SSL, 587 -> STARTTLS."""
    if SMTP_PORT == 465:
        import ssl
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ctx, timeout=15) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


def _namaz_get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _namaz_abuse_post(path: str, payload: dict):
    """Abuse control servisine bildirim. Servis yoksa/hata verirse sessizce gecer."""
    if not ABUSE_CONTROL_URL:
        return
    try:
        import httpx
        r = httpx.post(f"{ABUSE_CONTROL_URL}{path}", json=payload, timeout=5)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", "Abuse control blocked request")
            except Exception:
                detail = "Abuse control blocked request"
            raise HTTPException(status_code=r.status_code, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMAZ ABUSE CONTROL ERROR] {path} -> {e}")
        return


# =====================================================
# PYDANTIC MODELS
# =====================================================

class NamazOTPRequest(BaseModel):
    email: EmailStr
    mode: str
    name: Optional[str] = None


class NamazOTPVerify(BaseModel):
    email: EmailStr
    code: str
    mode: str
    name: Optional[str] = None


# =====================================================
# AUTH HELPER - namaz app token dogrulama
# =====================================================

def get_namaz_user_from_token(authorization: Optional[str] = None) -> Optional[int]:
    """
    Namaz app JWT token'indan user_id cikarir.
    scope alani "namaz_app" degilse token'i reddeder - boylece chatbot'tan
    alinan bir token buraya asla gecerli olmaz, ve tersi de gecerli degildir.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    try:
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        scope = payload.get("scope")
        if not email or scope != "namaz_app":
            raise HTTPException(status_code=401, detail="Invalid token")
        pool = _get_namaz_pool()
        conn = pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM namaz_app_users WHERE email = %s", (email,))
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=401, detail="User not found")
            return result[0]
        finally:
            pool.putconn(conn)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# =====================================================
# /namaz/request_code
# =====================================================

@router.post("/namaz/request_code")
async def namaz_request_code_endpoint(req: NamazOTPRequest, request: Request):
    try:
        ip_address = _namaz_get_client_ip(request)
        _namaz_abuse_post("/otp/request/check", {"email": req.email, "ip_address": ip_address})

        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM namaz_app_users WHERE email = %s", (req.email,))
            existing_user = cur.fetchone()

            if req.mode == "login" and not existing_user:
                # Test hesabi icin otomatik kullanici olustur
                if req.email.lower() == TEST_EMAIL:
                    cur.execute("INSERT INTO namaz_app_users (email, name, is_premium) VALUES (%s, %s, true) RETURNING id", (req.email, "Tester"))
                    new_user = cur.fetchone()
                    cur.execute("""
                        INSERT INTO namaz_app_subscriptions (user_id, plan_id, status, billing_period, current_period_end)
                        VALUES (%s, 'lifetime', 'active', 'lifetime', '2030-01-01')
                    """, (new_user[0],))
                    conn.commit()
                    existing_user = new_user
                else:
                    raise HTTPException(status_code=404, detail="Kullanici bulunamadi.")
            if req.mode == "register" and existing_user:
                raise HTTPException(status_code=400, detail="E-posta kullanimda.")

            # --- TEST HESABI: email gonderme, kodu sabit yap ---
            if req.email.lower() == TEST_EMAIL:
                cur.execute("""
                    INSERT INTO namaz_app_otp_codes (email, code, expire_at, created_at)
                    VALUES (%s, %s, '2030-01-01', NOW())
                    ON CONFLICT (email) DO UPDATE
                    SET code = EXCLUDED.code, expire_at = EXCLUDED.expire_at, created_at = NOW()
                """, (req.email, TEST_CODE))
                conn.commit()
                if req.mode == "register" and not existing_user:
                    cur.execute("INSERT INTO namaz_app_users (email, name, is_premium) VALUES (%s, %s, true) RETURNING id", (req.email, "Tester"))
                    new_user = cur.fetchone()
                    cur.execute("""
                        INSERT INTO namaz_app_subscriptions (user_id, plan_id, status, billing_period, current_period_end)
                        VALUES (%s, 'lifetime', 'active', 'lifetime', '2030-01-01')
                    """, (new_user[0],))
                    conn.commit()
                return {"status": "success", "message": "Kod gonderildi."}
            # --- TEST HESABI SONU ---

            generated_otp = str(random.randint(100000, 999999))
            cur.execute("""
                INSERT INTO namaz_app_otp_codes (email, code, expire_at, created_at)
                VALUES (%s, %s, NOW() + INTERVAL '5 minutes', NOW())
                ON CONFLICT (email) DO UPDATE
                SET code = EXCLUDED.code, expire_at = EXCLUDED.expire_at, created_at = NOW()
            """, (req.email, generated_otp))
            conn.commit()

            msg = MIMEMultipart()
            msg["From"] = SMTP_FROM
            msg["To"] = req.email
            msg["Subject"] = "OneBune Namaz Vakitleri Dogrulama Kodu"
            msg.attach(MIMEText(
                f"Merhaba,\n\nOneBune Namaz Vakitleri dogrulama kodun: {generated_otp}\n\n"
                f"Bu kod 5 dakika boyunca gecerlidir.\n\nOneBune",
                "plain", "utf-8"
            ))
            _namaz_smtp_send(msg)

            _namaz_abuse_post("/otp/request/mark-sent", {"email": req.email, "ip_address": ip_address})
            return {"status": "success", "message": "Kod gonderildi."}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMAZ REQUEST_CODE ERROR] {e}")
        raise HTTPException(status_code=500, detail="Kod gonderme hatasi.")


# =====================================================
# /namaz/verify_code
# =====================================================

@router.post("/namaz/verify_code")
async def namaz_verify_otp(body: NamazOTPVerify, request: Request):
    email = body.email.lower().strip()
    code = body.code.strip()
    mode = body.mode.strip()
    name = (body.name or "").strip() if mode == "register" else None
    ip_address = _namaz_get_client_ip(request)

    # Test hesabi kontrolu
    is_test = email == TEST_EMAIL

    try:
        _namaz_abuse_post("/otp/verify/check", {"email": email, "ip_address": ip_address})

        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute(
                "SELECT code, expire_at FROM namaz_app_otp_codes WHERE email = %s LIMIT 1",
                (email,)
            )
            row = cur.fetchone()
            if not row:
                _namaz_abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip_address})
                raise HTTPException(status_code=400, detail="Kod bulunamadi.")

            stored_code, expire_at = row

            # Test hesabi icin sure kontrolu ATLA
            if not is_test:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if expire_at is None or now_utc > expire_at:
                    _namaz_abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip_address})
                    raise HTTPException(status_code=400, detail="Kod suresi dolmus.")

            if code != stored_code:
                _namaz_abuse_post("/otp/verify/mark-failed", {"email": email, "ip_address": ip_address})
                raise HTTPException(status_code=400, detail="Hatali kod.")

            _namaz_abuse_post("/otp/verify/clear", {"email": email, "ip_address": ip_address})

            if mode == "register":
                cur.execute("SELECT id FROM namaz_app_users WHERE email = %s", (email,))
                if cur.fetchone():
                    # Test hesabi: zaten varsa login'e cevir
                    if is_test:
                        mode = "login"
                    else:
                        raise HTTPException(status_code=400, detail="Bu email zaten kayitli.")

            if mode == "register":
                cur.execute(
                    "INSERT INTO namaz_app_users (email, name) VALUES (%s, %s) RETURNING id, name",
                    (email, name or email.split("@")[0])
                )
                user_row = cur.fetchone()
            elif mode == "login":
                cur.execute("SELECT id, name FROM namaz_app_users WHERE email = %s", (email,))
                user_row = cur.fetchone()
                if not user_row:
                    raise HTTPException(status_code=400, detail="Kullanici bulunamadi.")
            else:
                raise HTTPException(status_code=400, detail="Gecersiz mode.")

            # Test hesabi icin OTP'yi SILME (tekrar kullanilabilir)
            if not is_test:
                cur.execute("DELETE FROM namaz_app_otp_codes WHERE email = %s", (email,))
            cur.execute("UPDATE namaz_app_users SET last_login = NOW() WHERE id = %s", (user_row[0],))
            conn.commit()

            token = jwt.encode(
                {
                    "sub": email,
                    "scope": "namaz_app",
                    "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE_DAYS),
                },
                JWT_SECRET,
                algorithm=JWT_ALGORITHM,
            )
            return {
                "status": "success",
                "token": token,
                "user": {"id": user_row[0], "name": str(user_row[1]), "email": email},
            }
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMAZ VERIFY OTP ERROR] {e}")
        raise HTTPException(status_code=500, detail="Dogrulama hatasi.")


# =====================================================
# /namaz/subscription/status
# =====================================================

@router.get("/namaz/subscription/status")
async def namaz_subscription_status(authorization: str = Header(None)):
    user_id = get_namaz_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")

    try:
        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                SELECT plan_id, status, billing_period, current_period_end
                FROM namaz_app_subscriptions
                WHERE user_id = %s AND status IN ('active', 'trialing')
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,))
            row = cur.fetchone()

            if not row:
                return {
                    "plan_id": "free",
                    "plan_name": "Ucretsiz Uyelik",
                    "is_premium": False,
                    "status": "active",
                    "billing_period": "free",
                    "current_period_end": None,
                }

            plan_id, status, billing_period, period_end = row
            is_premium = plan_id != "free" and status in ("active", "trialing")
            return {
                "plan_id": plan_id,
                "plan_name": "Premium" if is_premium else "Ucretsiz Uyelik",
                "is_premium": is_premium,
                "status": status,
                "billing_period": billing_period,
                "current_period_end": period_end.isoformat() if period_end else None,
            }
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMAZ SUBSCRIPTION STATUS ERROR] {e}")
        raise HTTPException(status_code=500, detail="Abonelik durumu alinamadi.")


# =====================================================
# /namaz/check_user
# =====================================================

@router.get("/namaz/check_user")
async def namaz_check_user(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="No authorization header.")
    try:
        user_id = get_namaz_user_from_token(authorization)
        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("SELECT email, name FROM namaz_app_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found.")
            email, name = row
            return {"status": "success", "user": {"id": user_id, "email": email, "name": name}}
        finally:
            pool.putconn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Check user error: {str(e)}")

# =====================================================
# ZİKİR ENDPOINT'LERİ
# =====================================================

ZIKIRLER = [
    "Sübhanallah", "Elhamdülillah", "Allahuekber",
    "La ilahe illallah", "Estağfirullah", "Salavat",
    "La havle vela kuvvete illa billah",
]

class ZikirIncrementRequest(BaseModel):
    zikir_name: str
    count: int = 1
    target: int = 33

class ZikirSetTargetRequest(BaseModel):
    zikir_name: str
    target: int

@router.get("/namaz/zikir/list")
async def namaz_zikir_list():
    return {"zikirler": ZIKIRLER}

@router.post("/namaz/zikir/increment")
async def namaz_zikir_increment(req: ZikirIncrementRequest, authorization: str = Header(None)):
    user_id = get_namaz_user_from_token(authorization)
    if req.zikir_name not in ZIKIRLER:
        raise HTTPException(status_code=400, detail="Gecersiz zikir adi")
    if req.count < 1 or req.count > 1000:
        raise HTTPException(status_code=400, detail="Gecersiz sayi")
    try:
        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO namaz_app_zikir_logs (user_id, zikir_name, count, target, log_date)
                VALUES (%s, %s, %s, %s, CURRENT_DATE)
                ON CONFLICT (user_id, zikir_name, log_date)
                DO UPDATE SET
                    count = namaz_app_zikir_logs.count + EXCLUDED.count,
                    target = EXCLUDED.target,
                    updated_at = NOW()
                RETURNING count, target
            """, (user_id, req.zikir_name, req.count, req.target))
            row = cur.fetchone(); conn.commit()
            return {"status": "success", "count": row[0], "target": row[1]}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/namaz/zikir/reset")
async def namaz_zikir_reset(req: ZikirSetTargetRequest, authorization: str = Header(None)):
    user_id = get_namaz_user_from_token(authorization)
    if req.zikir_name not in ZIKIRLER:
        raise HTTPException(status_code=400, detail="Gecersiz zikir adi")
    try:
        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE namaz_app_zikir_logs
                SET count = 0, target = %s, updated_at = NOW()
                WHERE user_id = %s AND zikir_name = %s AND log_date = CURRENT_DATE
            """, (req.target, user_id, req.zikir_name))
            conn.commit()
            return {"status": "success"}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/namaz/zikir/today")
async def namaz_zikir_today(authorization: str = Header(None)):
    user_id = get_namaz_user_from_token(authorization)
    try:
        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                SELECT zikir_name, count, target
                FROM namaz_app_zikir_logs
                WHERE user_id = %s AND log_date = CURRENT_DATE
            """, (user_id,))
            rows = cur.fetchall()
            data = {r[0]: {"count": r[1], "target": r[2]} for r in rows}
            result = []
            for z in ZIKIRLER:
                entry = data.get(z, {"count": 0, "target": 33})
                result.append({
                    "zikir_name": z,
                    "count": entry["count"],
                    "target": entry["target"],
                    "done": entry["count"] >= entry["target"],
                })
            return {"date": "today", "zikirler": result}
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/namaz/zikir/history")
async def namaz_zikir_history(days: int = 7, authorization: str = Header(None)):
    user_id = get_namaz_user_from_token(authorization)
    if days < 1 or days > 30:
        days = 7
    try:
        pool = _get_namaz_pool(); conn = pool.getconn(); cur = conn.cursor()
        try:
            cur.execute("""
                SELECT log_date, SUM(count) as total, COUNT(DISTINCT zikir_name) as types
                FROM namaz_app_zikir_logs
                WHERE user_id = %s AND log_date >= CURRENT_DATE - INTERVAL '%s days'
                GROUP BY log_date ORDER BY log_date DESC
            """, (user_id, days))
            rows = cur.fetchall()
            return {
                "days": days,
                "history": [
                    {"date": str(r[0]), "total": int(r[1]), "types": int(r[2])}
                    for r in rows
                ]
            }
        finally:
            pool.putconn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))