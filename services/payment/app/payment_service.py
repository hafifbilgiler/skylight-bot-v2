"""
═══════════════════════════════════════════════════════════════
SKYLIGHT PAYMENT SERVICE - iyzico Subscription v2
═══════════════════════════════════════════════════════════════
Features:
- Subscription-based payment (monthly recurring)
- Database tracking
- Premium activation
- Audit logging
═══════════════════════════════════════════════════════════════
"""

import os
import json
import time
import base64
import hashlib
import hmac
import secrets
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import httpx
import asyncpg
from fastapi import FastAPI, HTTPException, Header, Body, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Skylight Payment Service",
    version="2.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://skylight.ai",
        "https://www.skylight.ai",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment
IYZICO_API_KEY = os.getenv("IYZICO_API_KEY", "").strip()
IYZICO_SECRET_KEY = os.getenv("IYZICO_SECRET_KEY", "").strip()
IYZICO_BASE_URL = os.getenv("IYZICO_BASE_URL", "https://sandbox-api.iyzipay.com").strip()

# Callback will be handled by this service, not PHP
PAYMENT_CALLBACK_URL = os.getenv(
    "PAYMENT_CALLBACK_URL",
    "https://skylight.ai/api/payment/callback",
).strip()

APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "https://skylight.ai").strip()

# Monthly plan code (from iyzico merchant panel)
IYZICO_MONTHLY_PLAN_CODE = os.getenv("IYZICO_MONTHLY_PLAN_CODE", "").strip()

REQUEST_TIMEOUT = float(os.getenv("PAYMENT_HTTP_TIMEOUT", "45"))

# Database
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "skylight_db")
DB_USER = os.getenv("DB_USER", "skylight_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Database pool
db_pool = None

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """Initialize database connection pool"""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            min_size=2,
            max_size=10,
        )
        logger.info("[PAYMENT] Database pool created")
    except Exception as e:
        logger.error(f"[PAYMENT DB ERROR] {e}")


@app.on_event("shutdown")
async def shutdown():
    """Close database connection pool"""
    if db_pool:
        await db_pool.close()


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def ensure_config() -> None:
    """Check required environment variables"""
    missing = []

    if not IYZICO_API_KEY:
        missing.append("IYZICO_API_KEY")
    if not IYZICO_SECRET_KEY:
        missing.append("IYZICO_SECRET_KEY")
    if not IYZICO_MONTHLY_PLAN_CODE:
        missing.append("IYZICO_MONTHLY_PLAN_CODE")

    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Payment service config missing: {', '.join(missing)}",
        )


def build_auth(uri: str, body: str = "") -> tuple[str, str]:
    """Build iyzico authentication header"""
    rnd = secrets.token_hex(16)
    payload = f"{rnd}{uri}{body}"

    signature = hmac.new(
        IYZICO_SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    raw = f"apiKey:{IYZICO_API_KEY}&randomKey:{rnd}&signature:{signature}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")

    return f"IYZWSv2 {encoded}", rnd


async def iyzico_post(uri: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Make POST request to iyzico API"""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    auth, rnd = build_auth(uri, body)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            f"{IYZICO_BASE_URL}{uri}",
            content=body.encode("utf-8"),
            headers={
                "Authorization": auth,
                "x-iyzi-rnd": rnd,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    try:
        data = response.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"Iyzico returned invalid JSON (HTTP {response.status_code})",
        )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=data)

    return data


async def iyzico_get(uri: str) -> Dict[str, Any]:
    """Make GET request to iyzico API"""
    auth, rnd = build_auth(uri, "")

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            f"{IYZICO_BASE_URL}{uri}",
            headers={
                "Authorization": auth,
                "x-iyzi-rnd": rnd,
                "Accept": "application/json",
            },
        )

    try:
        data = response.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"Iyzico returned invalid JSON (HTTP {response.status_code})",
        )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=data)

    return data


async def get_user(token: str) -> Optional[dict]:
    """Get user by auth token"""
    if not db_pool:
        return None
    
    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("""
                SELECT id, email, name, is_premium, subscription_active, auth_token
                FROM users WHERE auth_token = $1
            """, token)
            return dict(user) if user else None
    except Exception as e:
        logger.error(f"[USER ERROR] {e}")
        return None


async def log_event(event: str, user_id: int = None, data: dict = None, ip: str = None):
    """Log audit event"""
    if not db_pool:
        return
    
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO payment_audit_log (event_type, user_id, data, ip_address, created_at)
                VALUES ($1, $2, $3, $4, NOW())
            """, event, user_id, json.dumps(data) if data else None, ip)
    except Exception as e:
        logger.error(f"[AUDIT ERROR] {e}")


async def activate_subscription(user_id: int, subscription_ref: str, plan_code: str):
    """Activate user subscription"""
    if not db_pool:
        return
    
    try:
        async with db_pool.acquire() as conn:
            # Update user
            await conn.execute("""
                UPDATE users 
                SET is_premium = TRUE, 
                    subscription_active = TRUE,
                    updated_at = NOW()
                WHERE id = $1
            """, user_id)
            
            # Insert/Update subscription
            await conn.execute("""
                INSERT INTO user_subscriptions 
                (user_id, is_active, iyzico_subscription_reference_code, pricing_plan_code, 
                 started_at, status, created_at, updated_at)
                VALUES ($1, TRUE, $2, $3, NOW(), 'active', NOW(), NOW())
                ON CONFLICT (user_id) 
                DO UPDATE SET 
                    is_active = TRUE,
                    iyzico_subscription_reference_code = $2,
                    pricing_plan_code = $3,
                    started_at = NOW(),
                    status = 'active',
                    updated_at = NOW()
            """, user_id, subscription_ref, plan_code)
            
            logger.info(f"[SUBSCRIPTION] User {user_id}: activated")
    except Exception as e:
        logger.error(f"[SUBSCRIPTION ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health() -> Dict[str, Any]:
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "payment-service",
        "version": "2.0.0",
        "iyzico_base_url": IYZICO_BASE_URL,
        "has_api_key": bool(IYZICO_API_KEY),
        "has_secret_key": bool(IYZICO_SECRET_KEY),
        "has_monthly_plan": bool(IYZICO_MONTHLY_PLAN_CODE),
        "database": "ok" if db_pool else "error",
    }


@app.post("/payment/checkout")
async def payment_checkout(
    request: Request,
    body: dict = Body(...),
    authorization: Optional[str] = Header(None),
):
    """
    Initialize subscription checkout
    
    Request body:
    {
        "token": "user_auth_token",  // optional, can be in header
        "email": "user@example.com",
        "name": "John",
        "surname": "Doe"
    }
    """
    ensure_config()

    # Get auth token
    token = body.get("token") or (authorization.split(" ")[1] if authorization and " " in authorization else None)
    
    if not token:
        raise HTTPException(status_code=401, detail="Login required")

    # Get user
    user = await get_user(token)
    if not user:
        await log_event("checkout_failed", data={"reason": "invalid_token"}, ip=request.client.host)
        raise HTTPException(status_code=401, detail="Invalid token")

    # Check if already subscribed
    if user.get('subscription_active'):
        raise HTTPException(status_code=400, detail="Already subscribed")

    # Customer info (from request or user data)
    customer_email = body.get("email") or user.get("email") or "user@skylight.ai"
    
    # Parse name
    full_name = user.get("name", "Skylight User")
    parts = full_name.split(" ", 1)
    customer_name = body.get("name") or parts[0] if parts else "User"
    customer_surname = body.get("surname") or (parts[1] if len(parts) > 1 else "User")
    
    customer_gsm = body.get("gsmNumber") or "+905000000000"
    customer_identity = body.get("identityNumber") or "11111111111"

    # Conversation ID
    conversation_id = f"skylight-{user['id']}-{int(time.time())}"

    # iyzico payload
    payload = {
        "locale": "tr",
        "conversationId": conversation_id,
        "callbackUrl": PAYMENT_CALLBACK_URL,
        "pricingPlanReferenceCode": IYZICO_MONTHLY_PLAN_CODE,
        "subscriptionInitialStatus": "ACTIVE",
        "customer": {
            "name": customer_name,
            "surname": customer_surname,
            "email": customer_email,
            "gsmNumber": customer_gsm,
            "identityNumber": customer_identity,
        },
    }

    # Call iyzico
    try:
        data = await iyzico_post("/v2/subscription/checkoutform/initialize", payload)
        
        if data.get("status") != "success":
            logger.error(f"[IYZICO ERROR] {data}")
            await log_event("checkout_failed", user['id'], {"error": data}, request.client.host)
            raise HTTPException(status_code=400, detail=data)

        # Save checkout to database
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO subscription_checkouts
                    (user_id, conversation_id, iyzico_token, pricing_plan_code, 
                     status, customer_email, customer_name, customer_surname, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                """, user['id'], conversation_id, data.get("token"), 
                     IYZICO_MONTHLY_PLAN_CODE, "pending", 
                     customer_email, customer_name, customer_surname)

        await log_event("checkout_success", user['id'], {"conv_id": conversation_id}, request.client.host)

        return {
            "status": "success",
            "checkoutFormContent": data.get("checkoutFormContent"),
            "token": data.get("token"),
            "conversationId": conversation_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CHECKOUT ERROR] {e}")
        raise HTTPException(status_code=500, detail="Checkout failed")


@app.post("/payment/callback")
async def payment_callback(request: Request):
    """
    iyzico callback endpoint
    
    This endpoint is called by iyzico after payment completion
    """
    ensure_config()

    # Get token from form data
    form = await request.form()
    token = form.get("token")

    if not token:
        return HTMLResponse("<h1>Error: Missing token</h1>", status_code=400)

    try:
        # Get payment result from iyzico
        data = await iyzico_get(f"/v2/subscription/checkoutform/{token}")

        if data.get("status") != "success":
            logger.error(f"[CALLBACK ERROR] {data}")
            return HTMLResponse("""
                <html>
                <head><meta http-equiv="refresh" content="3;url={0}"></head>
                <body>
                    <h1>❌ Ödeme Başarısız</h1>
                    <p>Lütfen tekrar deneyin.</p>
                </body>
                </html>
            """.format(APP_PUBLIC_URL), status_code=200)

        # Get checkout from database
        if not db_pool:
            raise Exception("Database not available")

        async with db_pool.acquire() as conn:
            checkout = await conn.fetchrow("""
                SELECT user_id, conversation_id, pricing_plan_code
                FROM subscription_checkouts
                WHERE iyzico_token = $1
            """, token)

            if not checkout:
                return HTMLResponse("<h1>Error: Checkout not found</h1>", status_code=404)

            user_id = checkout['user_id']
            plan_code = checkout['pricing_plan_code']

            # Update checkout status
            await conn.execute("""
                UPDATE subscription_checkouts
                SET status = 'completed',
                    iyzico_response = $2,
                    completed_at = NOW()
                WHERE iyzico_token = $1
            """, token, json.dumps(data))

            # Get subscription reference
            subscription_ref = data.get("subscriptionReferenceCode", "")

            # Activate subscription
            await activate_subscription(user_id, subscription_ref, plan_code)

            # Log payment
            await conn.execute("""
                INSERT INTO subscription_payments
                (user_id, payment_id, amount, currency, payment_status, iyzico_response, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
            """, user_id, data.get("paymentId"), 
                 float(data.get("paidPrice", 0)), 
                 "TRY", "SUCCESS", json.dumps(data))

            await log_event("payment_success", user_id, {"token": token}, request.client.host)

        # Redirect to success page
        return HTMLResponse("""
            <html>
            <head><meta http-equiv="refresh" content="2;url={0}?premium=1"></head>
            <body>
                <h1>✅ Ödeme Başarılı!</h1>
                <p>Premium üyeliğiniz aktif edildi. Yönlendiriliyorsunuz...</p>
            </body>
            </html>
        """.format(APP_PUBLIC_URL), status_code=200)

    except Exception as e:
        logger.error(f"[CALLBACK ERROR] {e}")
        return HTMLResponse("<h1>Error processing payment</h1>", status_code=500)


@app.get("/subscription/status")
async def subscription_status(authorization: Optional[str] = Header(None)):
    """Get user subscription status"""
    
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = authorization.split(" ")[1] if " " in authorization else authorization
    user = await get_user(token)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    return {
        "user_id": user['id'],
        "email": user['email'],
        "is_premium": user.get('is_premium', False),
        "subscription_active": user.get('subscription_active', False),
    }


# Run
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)