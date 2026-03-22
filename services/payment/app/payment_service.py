"""
═══════════════════════════════════════════════════════════════
ONE-BUNE PAYMENT SERVICE - iyzico Subscription
═══════════════════════════════════════════════════════════════
"""

import os, json, time, base64, hashlib, hmac, secrets, logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import httpx, asyncpg
from fastapi import FastAPI, HTTPException, Header, Body, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="ONE-BUNE Payment Service", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://one-bune.com", "https://www.one-bune.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IYZICO_API_KEY         = os.getenv("IYZICO_API_KEY", "").strip()
IYZICO_SECRET_KEY      = os.getenv("IYZICO_SECRET_KEY", "").strip()
IYZICO_BASE_URL        = os.getenv("IYZICO_BASE_URL", "https://api.iyzipay.com").strip()
IYZICO_MONTHLY_PLAN_CODE = os.getenv("IYZICO_MONTHLY_PLAN_CODE", "").strip()

PAYMENT_CALLBACK_URL   = os.getenv("PAYMENT_CALLBACK_URL",
    "https://api.one-bune.com:8000/payment/callback").strip()
APP_PUBLIC_URL         = os.getenv("APP_PUBLIC_URL", "https://one-bune.com").strip()
REQUEST_TIMEOUT        = float(os.getenv("PAYMENT_HTTP_TIMEOUT", "45"))

DB_HOST     = os.getenv("DB_HOST", "postgres")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME", "one_bune_db")
DB_USER     = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

db_pool = None

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            min_size=2, max_size=10,
        )
        logger.info("[PAYMENT] DB pool OK")
    except Exception as e:
        logger.error(f"[PAYMENT DB] {e}")

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def ensure_config():
    missing = [k for k, v in {
        "IYZICO_API_KEY": IYZICO_API_KEY,
        "IYZICO_SECRET_KEY": IYZICO_SECRET_KEY,
        "IYZICO_MONTHLY_PLAN_CODE": IYZICO_MONTHLY_PLAN_CODE,
    }.items() if not v]
    if missing:
        raise HTTPException(500, f"Config eksik: {', '.join(missing)}")


def build_auth(body_str: str) -> tuple[str, str]:
    """
    iyzico resmi SDK auth formatı:
    1. hash = HMAC-SHA256(secretKey, apiKey + rnd + secretKey + body)
    2. sig  = base64(hash)
    3. params = "apiKey:X&randomKey:X&signature:X"
    4. Authorization = "IYZWS " + base64(params)
    """
    rnd     = secrets.token_hex(8)
    raw     = IYZICO_API_KEY + rnd + IYZICO_SECRET_KEY + body_str
    sig     = base64.b64encode(
        hmac.new(
            IYZICO_SECRET_KEY.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    params  = f"apiKey:{IYZICO_API_KEY}&randomKey:{rnd}&signature:{sig}"
    encoded = base64.b64encode(params.encode("utf-8")).decode("utf-8")
    return f"IYZWS {encoded}", rnd


async def iyzico_post(uri: str, payload: Dict) -> Dict:
    body      = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    auth, rnd = build_auth(body)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(
            IYZICO_BASE_URL + uri,
            content=body.encode("utf-8"),
            headers={
                "Authorization": auth,
                "x-iyzi-rnd":    rnd,
                "x-iyzi-client-version": "iyzipay-python-2.1.0",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
        )
    logger.info(f"[IYZICO] {uri} → {r.status_code}")
    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, f"iyzico geçersiz JSON (HTTP {r.status_code})")
    if r.status_code == 401:
        raise HTTPException(502, f"iyzico auth hatası: {data}")
    if r.status_code >= 400:
        raise HTTPException(502, data)
    return data


async def iyzico_get(uri: str) -> Dict:
    auth, rnd = build_auth("")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.get(
            IYZICO_BASE_URL + uri,
            headers={"Authorization": auth, "x-iyzi-rnd": rnd, "Accept": "application/json"},
        )
    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, f"iyzico geçersiz JSON (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise HTTPException(502, data)
    return data


JWT_SECRET    = os.getenv("JWT_SECRET", "").strip()
JWT_ALGORITHM = "HS256"


async def get_user(token: str) -> Optional[dict]:
    """JWT token ile kullanıcıyı bul"""
    if not db_pool:
        return None
    try:
        import jwt as _jwt
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email   = payload.get("sub")
        if not email:
            return None
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, email, name, is_premium, subscription_active
                FROM users WHERE email = $1
            """, email)
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"[USER] {e}")
        return None


async def log_event(event: str, user_id: int = None, data: dict = None, ip: str = None):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO payment_audit_log (event_type, user_id, data, ip_address, created_at)
                VALUES ($1, $2, $3, $4, NOW())
            """, event, user_id, json.dumps(data) if data else None, ip)
    except Exception as e:
        logger.error(f"[AUDIT] {e}")


async def activate_subscription(user_id: int, subscription_ref: str, plan_code: str):
    """Kullanıcıyı premium yap — DB şeması ile uyumlu"""
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            # users tablosu güncelle
            await conn.execute("""
                UPDATE users
                SET is_premium = TRUE, subscription_active = TRUE
                WHERE id = $1
            """, user_id)

            # user_subscriptions — mevcut şemaya uygun kolonlar
            await conn.execute("""
                INSERT INTO user_subscriptions
                    (user_id, plan_id, status, billing_period,
                     current_period_start, current_period_end,
                     iyzico_subscription_ref, created_at, updated_at)
                VALUES ($1, 'premium', 'active', 'monthly',
                        NOW(), NOW() + INTERVAL '30 days',
                        $2, NOW(), NOW())
                ON CONFLICT (user_id) WHERE status IN ('active','trialing')
                DO UPDATE SET
                    plan_id               = 'premium',
                    status                = 'active',
                    iyzico_subscription_ref = $2,
                    current_period_start  = NOW(),
                    current_period_end    = NOW() + INTERVAL '30 days',
                    updated_at            = NOW()
            """, user_id, subscription_ref)

            logger.info(f"[SUBSCRIPTION] User {user_id} aktifleştirildi")
    except Exception as e:
        logger.error(f"[SUBSCRIPTION ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/payment/health")
async def health():
    return {
        "status":           "healthy",
        "service":          "payment-service",
        "version":          "2.1.0",
        "iyzico_base_url":  IYZICO_BASE_URL,
        "has_api_key":      bool(IYZICO_API_KEY),
        "has_plan_code":    bool(IYZICO_MONTHLY_PLAN_CODE),
        "database":         "ok" if db_pool else "error",
    }


@app.post("/payment/checkout")
async def payment_checkout(
    request: Request,
    body: dict = Body(...),
    authorization: Optional[str] = Header(None),
):
    """iyzico checkout formu başlat"""
    ensure_config()

    token = (body.get("token")
             or (authorization.split(" ")[1] if authorization and " " in authorization else None))
    if not token:
        raise HTTPException(401, "Login required")

    user = await get_user(token)
    if not user:
        await log_event("checkout_failed", data={"reason": "invalid_token"},
                        ip=request.client.host)
        raise HTTPException(401, "Geçersiz token")

    if user.get("subscription_active"):
        raise HTTPException(400, "Zaten abone")

    # Müşteri bilgileri
    email    = body.get("email")    or user.get("email") or "user@one-bune.com"
    fullname = user.get("name", "One Bune User")
    parts    = fullname.split(" ", 1)
    name     = body.get("name")    or parts[0]
    surname  = body.get("surname") or (parts[1] if len(parts) > 1 else "User")
    gsm      = body.get("gsmNumber")      or "+905000000000"
    identity = body.get("identityNumber") or "11111111111"

    conv_id  = f"onebune-{user['id']}-{int(time.time())}"

    payload = {
        "locale":                   "tr",
        "conversationId":           conv_id,
        "callbackUrl":              PAYMENT_CALLBACK_URL,
        "pricingPlanReferenceCode": IYZICO_MONTHLY_PLAN_CODE,
        "subscriptionInitialStatus":"ACTIVE",
        "customer": {
            "name":           name,
            "surname":        surname,
            "email":          email,
            "gsmNumber":      gsm,
            "identityNumber": identity,
        },
    }

    try:
        data = await iyzico_post("/v2/subscription/checkoutform/initialize", payload)

        if data.get("status") != "success":
            logger.error(f"[IYZICO] {data}")
            await log_event("checkout_failed", user["id"], {"error": data},
                            request.client.host)
            raise HTTPException(400, data.get("errorMessage", "Checkout başlatılamadı"))

        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO subscription_checkouts
                        (user_id, conversation_id, iyzico_token, pricing_plan_code,
                         status, customer_email, customer_name, customer_surname, created_at)
                    VALUES ($1,$2,$3,$4,'pending',$5,$6,$7,NOW())
                """, user["id"], conv_id, data.get("token"),
                     IYZICO_MONTHLY_PLAN_CODE, email, name, surname)

        await log_event("checkout_success", user["id"], {"conv_id": conv_id},
                        request.client.host)

        return {
            "status":             "success",
            "checkoutFormContent": data.get("checkoutFormContent"),
            "token":              data.get("token"),
            "conversationId":     conv_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CHECKOUT] {e}")
        raise HTTPException(500, "Checkout başlatılamadı")


@app.post("/payment/callback")
async def payment_callback(request: Request):
    """iyzico ödeme sonucu callback"""
    ensure_config()

    form  = await request.form()
    token = form.get("token")

    if not token:
        return HTMLResponse("<h1>Hata: Token eksik</h1>", status_code=400)

    try:
        data = await iyzico_get(f"/v2/subscription/checkoutform/{token}")

        if data.get("status") != "success":
            logger.error(f"[CALLBACK] {data}")
            return HTMLResponse(f"""
                <html><head><meta http-equiv="refresh" content="3;url={APP_PUBLIC_URL}"></head>
                <body><h1>❌ Ödeme Başarısız</h1><p>Lütfen tekrar deneyin.</p></body></html>
            """)

        if not db_pool:
            raise Exception("DB yok")

        async with db_pool.acquire() as conn:
            checkout = await conn.fetchrow("""
                SELECT user_id, conversation_id, pricing_plan_code
                FROM subscription_checkouts WHERE iyzico_token = $1
            """, token)

            if not checkout:
                return HTMLResponse("<h1>Hata: Checkout bulunamadı</h1>", status_code=404)

            user_id  = checkout["user_id"]
            plan_code = checkout["pricing_plan_code"]

            # Checkout'u tamamlandı yap
            await conn.execute("""
                UPDATE subscription_checkouts
                SET status='completed', iyzico_response=$2, completed_at=NOW()
                WHERE iyzico_token=$1
            """, token, json.dumps(data))

            # Aboneliği aktifleştir
            sub_ref = data.get("subscriptionReferenceCode", "")
            await activate_subscription(user_id, sub_ref, plan_code)

            # payment_history'e kaydet — mevcut DB şemasına uygun
            await conn.execute("""
                INSERT INTO payment_history
                    (user_id, plan_id, amount, currency, status,
                     iyzico_payment_id, iyzico_conversation_id,
                     iyzico_raw_result, paid_at, created_at)
                VALUES ($1, 'premium', $2, 'TRY', 'completed',
                        $3, $4, $5, NOW(), NOW())
            """, user_id,
                 float(data.get("paidPrice", 64)),
                 data.get("paymentId", ""),
                 data.get("conversationId", ""),
                 json.dumps(data))

            await log_event("payment_success", user_id, {"token": token},
                            request.client.host)

        return HTMLResponse(f"""
            <html><head><meta http-equiv="refresh" content="2;url={APP_PUBLIC_URL}?premium=1"></head>
            <body><h1>✅ Ödeme Başarılı!</h1>
            <p>Premium üyeliğiniz aktif edildi. Yönlendiriliyorsunuz...</p></body></html>
        """)

    except Exception as e:
        logger.error(f"[CALLBACK ERROR] {e}")
        return HTMLResponse("<h1>Ödeme işleme hatası</h1>", status_code=500)


@app.get("/payment/subscription/status")
async def subscription_status(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(401, "Yetkisiz")
    token = authorization.split(" ")[1] if " " in authorization else authorization
    user  = await get_user(token)
    if not user:
        raise HTTPException(401, "Geçersiz token")
    return {
        "user_id":             user["id"],
        "email":               user["email"],
        "is_premium":          user.get("is_premium", False),
        "subscription_active": user.get("subscription_active", False),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)