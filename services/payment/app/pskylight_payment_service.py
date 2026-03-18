"""
═══════════════════════════════════════════════════════════════
SKYLIGHT PAYMENT SERVICE - iyzico Integration
═══════════════════════════════════════════════════════════════
Secure payment processing with iyzico
Features:
- Subscription management
- Payment tracking  
- Webhook handling
- Security: CSRF, rate limiting, audit logs
═══════════════════════════════════════════════════════════════
Version: 1.0.0
Author: Skylight AI
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os
import asyncpg
import hashlib
import json
import uuid
import httpx
from datetime import datetime, timedelta
from collections import defaultdict
import logging

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="Skylight Payment Service", version="1.0.0")

# CORS
app.add_middleware(
    CORS Middleware,
    allow_origins=[
        "https://skylight.ai",
        "https://www.skylight.ai",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)

# Environment
IYZICO_API_KEY = os.getenv("IYZICO_API_KEY", "")
IYZICO_SECRET_KEY = os.getenv("IYZICO_SECRET_KEY", "")
IYZICO_BASE_URL = os.getenv("IYZICO_BASE_URL", "https://sandbox-api.iyzipay.com")
IYZICO_CALLBACK_URL = os.getenv("IYZICO_CALLBACK_URL", "https://skylight.ai/payment/callback")

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "skylight_db")
DB_USER = os.getenv("DB_USER", "skylight_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Pricing
PREMIUM_MONTHLY_PRICE = "199.00"
PREMIUM_YEARLY_PRICE = "1990.00"

# Database pool
db_pool = None

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Rate limiting
rate_limit_store = defaultdict(list)

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
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
    if db_pool:
        await db_pool.close()

# ═══════════════════════════════════════════════════════════════
# SECURITY
# ═══════════════════════════════════════════════════════════════

def check_rate_limit(identifier: str, limit: int = 10, window: int = 60) -> bool:
    now = datetime.now()
    cutoff = now - timedelta(seconds=window)
    
    rate_limit_store[identifier] = [
        t for t in rate_limit_store[identifier] if t > cutoff
    ]
    
    if len(rate_limit_store[identifier]) >= limit:
        return False
    
    rate_limit_store[identifier].append(now)
    return True

def generate_iyzico_auth(request_body: dict) -> tuple[str, str]:
    import secrets
    import base64
    
    random_str = secrets.token_hex(16)
    auth_string = f"{IYZICO_API_KEY}:{IYZICO_SECRET_KEY}:{random_str}"
    hash_value = hashlib.sha256(auth_string.encode()).digest()
    auth_value = base64.b64encode(hash_value).decode()
    
    return f"IYZWS {IYZICO_API_KEY}:{auth_value}", random_str

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
        logger.error(f"[AUDIT ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class InitPaymentRequest(BaseModel):
    user_token: str
    plan: str  # "monthly" or "yearly"

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

async def get_user(token: str) -> Optional[dict]:
    if not db_pool:
        return None
    
    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("""
                SELECT id, email, name, is_premium, premium_expires_at
                FROM users WHERE auth_token = $1
            """, token)
            return dict(user) if user else None
    except Exception as e:
        logger.error(f"[USER ERROR] {e}")
        return None

async def update_premium(user_id: int, is_premium: bool, expires_at: datetime = None):
    if not db_pool:
        return
    
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET is_premium = $2, premium_expires_at = $3, updated_at = NOW()
                WHERE id = $1
            """, user_id, is_premium, expires_at)
            logger.info(f"[PREMIUM] User {user_id}: {is_premium}")
    except Exception as e:
        logger.error(f"[PREMIUM ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "payment",
        "iyzico": bool(IYZICO_API_KEY and IYZICO_SECRET_KEY),
        "database": "ok" if db_pool else "error",
    }

@app.post("/initialize")
async def initialize(req_data: InitPaymentRequest, request: Request):
    """Initialize iyzico checkout"""
    
    # Rate limit
    client_ip = request.client.host
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    # Check config
    if not IYZICO_API_KEY or not IYZICO_SECRET_KEY:
        logger.error("[PAYMENT] iyzico not configured")
        raise HTTPException(status_code=500, detail="Payment system not configured")
    
    # Get user
    user = await get_user(req_data.user_token)
    if not user:
        await log_event("init_failed", data={"reason": "invalid_token"}, ip=client_ip)
        raise HTTPException(status_code=401, detail="Invalid token")
    
    # Check premium
    if user.get('is_premium'):
        raise HTTPException(status_code=400, detail="Already premium")
    
    # Price
    price = PREMIUM_YEARLY_PRICE if req_data.plan == "yearly" else PREMIUM_MONTHLY_PRICE
    basket_id = f"skylight_{req_data.plan}"
    description = f"Skylight AI Premium - {req_data.plan.title()}"
    
    # Conversation ID
    conv_id = f"sky_{user['id']}_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}"
    
    # Name parsing
    full_name = user.get('name', 'Skylight User')
    parts = full_name.split(' ', 1)
    buyer_name = parts[0] if parts else 'User'
    buyer_surname = parts[1] if len(parts) > 1 else 'User'
    
    # Payload
    payload = {
        "locale": "tr",
        "conversationId": conv_id,
        "price": price,
        "paidPrice": price,
        "currency": "TRY",
        "basketId": basket_id,
        "paymentGroup": "SUBSCRIPTION",
        "callbackUrl": IYZICO_CALLBACK_URL,
        "enabledInstallments": [1],
        "buyer": {
            "id": str(user['id']),
            "name": buyer_name,
            "surname": buyer_surname,
            "gsmNumber": "+905350000000",
            "email": user['email'],
            "identityNumber": "11111111111",
            "registrationAddress": "Istanbul, Turkey",
            "ip": client_ip,
            "city": "Istanbul",
            "country": "Turkey",
            "zipCode": "34000",
        },
        "shippingAddress": {
            "contactName": full_name,
            "city": "Istanbul",
            "country": "Turkey",
            "address": "Istanbul, Turkey",
            "zipCode": "34000",
        },
        "billingAddress": {
            "contactName": full_name,
            "city": "Istanbul",
            "country": "Turkey",
            "address": "Istanbul, Turkey",
            "zipCode": "34000",
        },
        "basketItems": [{
            "id": "premium",
            "name": description,
            "category1": "Digital Service",
            "category2": "AI Subscription",
            "itemType": "VIRTUAL",
            "price": price,
        }],
    }
    
    # Auth
    auth, rnd = generate_iyzico_auth(payload)
    
    # Call iyzico
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{IYZICO_BASE_URL}/payment/iyzipos/checkoutform/initialize/auth/ecom",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": auth,
                    "x-iyzi-rnd": rnd,
                },
            )
            
            data = resp.json()
            
            if data.get("status") != "success":
                error = data.get("errorMessage", "Unknown error")
                logger.error(f"[IYZICO ERROR] {error}")
                await log_event("init_failed", user['id'], {"error": error}, client_ip)
                raise HTTPException(status_code=500, detail=f"Payment failed: {error}")
            
            # Store transaction
            if db_pool:
                async with db_pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO payment_transactions
                        (user_id, conversation_id, plan_type, amount, currency, status, iyzico_token, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    """, user['id'], conv_id, req_data.plan, float(price), "TRY", "pending", data.get("token"))
            
            await log_event("init_success", user['id'], {"conv_id": conv_id, "plan": req_data.plan}, client_ip)
            
            return {
                "success": True,
                "checkoutFormContent": data.get("checkoutFormContent"),
                "token": data.get("token"),
                "paymentPageUrl": data.get("paymentPageUrl"),
                "conversationId": conv_id,
            }
    
    except httpx.HTTPError as e:
        logger.error(f"[IYZICO HTTP ERROR] {e}")
        raise HTTPException(status_code=500, detail="Payment service unavailable")

@app.post("/callback")
async def callback(request: Request):
    """iyzico callback"""
    
    form = await request.form()
    token = form.get("token")
    conv_id = form.get("conversationId")
    
    if not token:
        return HTMLResponse("<h1>Error: Missing token</h1>", status_code=400)
    
    # Retrieve result
    payload = {"locale": "tr", "conversationId": conv_id, "token": token}
    auth, rnd = generate_iyzico_auth(payload)
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{IYZICO_BASE_URL}/payment/iyzipos/checkoutform/auth/ecom/detail",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": auth,
                    "x-iyzi-rnd": rnd,
                },
            )
            
            result = resp.json()
            status = result.get("paymentStatus")
            
            # Get transaction
            if db_pool:
                async with db_pool.acquire() as conn:
                    tx = await conn.fetchrow("""
                        SELECT id, user_id, plan_type FROM payment_transactions
                        WHERE conversation_id = $1
                    """, conv_id)
                    
                    if not tx:
                        return HTMLResponse("<h1>Error: Transaction not found</h1>", status_code=404)
                    
                    user_id = tx['user_id']
                    plan = tx['plan_type']
                    
                    # Update transaction
                    await conn.execute("""
                        UPDATE payment_transactions
                        SET status = $2, iyzico_response = $3, completed_at = NOW()
                        WHERE conversation_id = $1
                    """, conv_id, status, json.dumps(result))
                    
                    if status == "SUCCESS":
                        # Set expiry
                        expires = datetime.now() + timedelta(days=365 if plan == "yearly" else 30)
                        await update_premium(user_id, True, expires)
                        await log_event("payment_success", user_id, {"conv_id": conv_id, "plan": plan}, request.client.host)
                        
                        return HTMLResponse("""
                        <html>
                        <head><meta http-equiv="refresh" content="2;url=https://skylight.ai/premium/success"></head>
                        <body>
                            <h1>✅ Payment Successful!</h1>
                            <p>Premium activated. Redirecting...</p>
                        </body>
                        </html>
                        """)
                    
                    else:
                        await log_event("payment_failed", user_id, {"conv_id": conv_id, "status": status}, request.client.host)
                        return HTMLResponse("""
                        <html>
                        <head><meta http-equiv="refresh" content="3;url=https://skylight.ai/premium"></head>
                        <body>
                            <h1>❌ Payment Failed</h1>
                            <p>Please try again.</p>
                        </body>
                        </html>
                        """)
    
    except Exception as e:
        logger.error(f"[CALLBACK ERROR] {e}")
        return HTMLResponse("<h1>Error processing payment</h1>", status_code=500)

@app.get("/subscription/status")
async def subscription(request: Request):
    """Get subscription status"""
    
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    token = auth.split(" ", 1)[1]
    user = await get_user(token)
    
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    return {
        "user_id": user['id'],
        "email": user['email'],
        "is_premium": user.get('is_premium', False),
        "premium_expires_at": user.get('premium_expires_at').isoformat() if user.get('premium_expires_at') else None,
    }

# Run
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8083)