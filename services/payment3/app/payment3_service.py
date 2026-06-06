"""
ONE-BUNE Payment3 Service — Shopier
═══════════════════════════════════════════════════════════════════════════
- POST /payment3/checkout              → Shopier ürün linki + tracking ID
- POST /payment3/webhook               → Shopier order.created event
- GET  /payment3/subscription/status   → Premium durumu
- POST /payment3/subscription/cancel   → İptal (manuel — Shopier abonelik yok)
- GET  /payment3/health                → K8s liveness

Mevcut DB şeması kullanılır:
  user_subscriptions.iyzico_subscription_ref  → Shopier order ID
  user_subscriptions.metadata                  → {"provider":"shopier", ...}

Yeni kullanıcı durumu:
  Webhook'tan gelen email DB'de yoksa otomatik user oluşturulur (password=NULL,
  OTP sistemine uyumlu). Welcome email atılır.
"""

import os
import sys
import json
import hmac
import hashlib
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import asyncpg
import httpx
import jwt as pyjwt
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("payment3")

# ── ENV ────────────────────────────────────────────────────────────────────
SHOPIER_PAT             = os.getenv("SHOPIER_PAT", "").strip()
SHOPIER_WEBHOOK_TOKEN   = os.getenv("SHOPIER_WEBHOOK_TOKEN", "").strip()
SHOPIER_PRODUCT_URL     = os.getenv("SHOPIER_PRODUCT_URL", "").strip()
SHOPIER_PRODUCT_ID      = os.getenv("SHOPIER_PRODUCT_ID", "").strip()  # opsiyonel filtre
SHOPIER_API_BASE        = "https://api.shopier.com/v1"

APP_PUBLIC_URL          = os.getenv("APP_PUBLIC_URL", "https://one-bune.com").strip()
JWT_SECRET              = os.getenv("JWT_SECRET", "").strip()

DB_HOST     = os.getenv("DB_HOST", "postgres")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME", "")
DB_USER     = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SMTP_SERVER = os.getenv("SMTP_SERVER", "").strip()
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "").strip()
SMTP_PASS   = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM   = os.getenv("SMTP_FROM", SMTP_USER).strip()

# Premium plan id (subscription_plans tablosunda 'premium' var)
PREMIUM_PLAN_ID = "premium"

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(title="ONE-BUNE Payment3 (Shopier)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db_pool: Optional[asyncpg.Pool] = None


# ═══════════════════════════════════════════════════════════════════════════
# DB
# ═══════════════════════════════════════════════════════════════════════════
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
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        logger.info("[PAY3] DB pool oluşturuldu")
    except Exception as e:
        logger.error(f"[PAY3] DB pool hatası: {e}")


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


# ═══════════════════════════════════════════════════════════════════════════
# JWT helpers
# ═══════════════════════════════════════════════════════════════════════════
def _verify_jwt(authorization: str) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization gerekli")
    token = authorization.split(" ", 1)[1]
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token geçersiz: {e}")


async def _user_id_from_jwt(payload: dict) -> tuple[int, str]:
    """JWT'de sub=email → users tablosundan id sorgula."""
    email = payload.get("email") or payload.get("sub") or ""
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="JWT'de email yok")
    if not db_pool:
        raise HTTPException(status_code=503, detail="DB yok")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE LOWER(email)=LOWER($1)", email)
    if not row:
        raise HTTPException(status_code=404, detail=f"Kullanıcı bulunamadı: {email}")
    return row["id"], email


# ═══════════════════════════════════════════════════════════════════════════
# Email
# ═══════════════════════════════════════════════════════════════════════════
async def _send_email(to: str, subject: str, html: str, plain: str):
    """Mailjet/SMTP üzerinden e-posta — gateway ile aynı stil."""
    if not SMTP_SERVER or not SMTP_USER:
        logger.warning("[PAY3 EMAIL] SMTP yapılandırılmamış")
        return

    def _send_sync():
        msg = MIMEMultipart("alternative")
        msg["From"]    = f"ONE-BUNE <{SMTP_FROM}>"
        msg["To"]      = to
        msg["Subject"] = subject
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))
        if SMTP_PORT == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ctx, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)

    try:
        await asyncio.to_thread(_send_sync)
        logger.info(f"[PAY3 EMAIL] → {to} / {subject}")
    except Exception as e:
        logger.error(f"[PAY3 EMAIL ERROR] {e}")


def _welcome_email_existing(name: str) -> tuple[str, str]:
    n = (name or "").strip() or "ONE-BUNE kullanıcısı"
    html = f"""
<!doctype html>
<html><body style="font-family:Arial,sans-serif;background:#0a0a0f;color:#fff;padding:30px;">
  <div style="max-width:600px;margin:auto;background:#15151f;border-radius:14px;padding:30px;">
    <h2 style="color:#00f2fe;">🎉 Premium aktif!</h2>
    <p>Merhaba <strong>{n}</strong>,</p>
    <p>Shopier üzerinden yapılan ödemen onaylandı. Hesabın <strong>1 ay</strong> boyunca Premium olarak aktif edildi.</p>
    <p>✅ Sınırsız mesaj<br>✅ Tüm AI modları<br>✅ Dosya & görsel desteği</p>
    <p style="margin-top:20px;">
      <a href="{APP_PUBLIC_URL}" style="background:linear-gradient(135deg,#bc4efd,#00d4e6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:bold;">ONE-BUNE'a Git</a>
    </p>
    <p style="font-size:11px;color:#888;margin-top:30px;">Sorularınız için: info@one-bune.com</p>
  </div>
</body></html>
"""
    plain = f"Merhaba {n}, Premium aboneliğin aktif! {APP_PUBLIC_URL}"
    return html, plain


def _welcome_email_new(name: str, email: str) -> tuple[str, str]:
    """Shopier'dan direkt gelen yeni kullanıcı için."""
    n = (name or "").strip() or email
    html = f"""
<!doctype html>
<html><body style="font-family:Arial,sans-serif;background:#0a0a0f;color:#fff;padding:30px;">
  <div style="max-width:600px;margin:auto;background:#15151f;border-radius:14px;padding:30px;">
    <h2 style="color:#00f2fe;">🎉 ONE-BUNE'a hoş geldin!</h2>
    <p>Merhaba <strong>{n}</strong>,</p>
    <p>Shopier üzerinden yapılan ödemen onaylandı. <strong>Premium hesabın oluşturuldu</strong> ve <strong>1 ay</strong> boyunca aktif.</p>
    <p>Giriş yapmak için:</p>
    <ol>
      <li><a href="{APP_PUBLIC_URL}" style="color:#00f2fe;">{APP_PUBLIC_URL}</a> adresine git</li>
      <li>Email kutuna <strong>{email}</strong> yaz</li>
      <li>"Kod gönder" butonuna bas → email'ine OTP gelecek</li>
      <li>OTP ile giriş yap, Premium otomatik aktif</li>
    </ol>
    <p style="margin-top:20px;">
      <a href="{APP_PUBLIC_URL}" style="background:linear-gradient(135deg,#bc4efd,#00d4e6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:bold;">Hemen Giriş Yap</a>
    </p>
    <p style="font-size:11px;color:#888;margin-top:30px;">Sorularınız için: info@one-bune.com</p>
  </div>
</body></html>
"""
    plain = (
        f"Merhaba {n}, Premium hesabın oluşturuldu!\n\n"
        f"Giriş için: {APP_PUBLIC_URL} adresinde {email} ile OTP kodu alarak giriş yap."
    )
    return html, plain


# ═══════════════════════════════════════════════════════════════════════════
# Premium aktivasyon (DB)
# ═══════════════════════════════════════════════════════════════════════════
async def _activate_premium(
    user_id: int,
    shopier_order_id: str,
    period_end: datetime,
    customer_email: Optional[str] = None,
    customer_name: Optional[str] = None,
    is_new_user: bool = False,
):
    """Mevcut user_subscriptions şemasına Shopier order ID'si yaz."""
    if not db_pool:
        logger.error("[PAY3] DB pool yok!")
        return

    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET is_premium = TRUE, subscription_active = TRUE
            WHERE id = $1
        """, user_id)

        metadata = json.dumps({"provider": "shopier", "order_id": shopier_order_id})

        # UPSERT — aktif/trialing varsa update et, yoksa insert
        await conn.execute("""
            INSERT INTO user_subscriptions
                (user_id, plan_id, status, billing_period,
                 current_period_start, current_period_end,
                 iyzico_subscription_ref, metadata,
                 created_at, updated_at)
            VALUES ($1, 'premium', 'active', 'monthly',
                    NOW(), $2, $3, $4::jsonb, NOW(), NOW())
            ON CONFLICT (user_id) WHERE status IN ('active','trialing')
            DO UPDATE SET
                plan_id                 = 'premium',
                status                  = 'active',
                billing_period          = 'monthly',
                current_period_start    = NOW(),
                current_period_end      = $2,
                iyzico_subscription_ref = $3,
                metadata                = $4::jsonb,
                updated_at              = NOW()
        """, user_id, period_end, shopier_order_id, metadata)

        # Email
        row = await conn.fetchrow("SELECT email, name FROM users WHERE id=$1", user_id)
        if row and row["email"]:
            name = row.get("name") or customer_name or ""
            if is_new_user:
                html, plain = _welcome_email_new(name, row["email"])
                subject = "🎉 ONE-BUNE Premium hesabın oluşturuldu"
            else:
                html, plain = _welcome_email_existing(name)
                subject = "✅ ONE-BUNE Premium üyeliğin aktif"
            asyncio.create_task(_send_email(row["email"], subject, html, plain))

    logger.info(f"[PAY3] Premium aktif: user_id={user_id} order={shopier_order_id} new_user={is_new_user}")


async def _ensure_user(email: str, first_name: str = "", last_name: str = "", phone: str = "") -> tuple[int, bool]:
    """
    Email ile user_id bul; yoksa oluştur (OTP sistemine uyumlu — password=NULL).
    Döner: (user_id, is_new_user)
    """
    if not db_pool:
        raise HTTPException(status_code=503, detail="DB yok")

    email_l = email.strip().lower()
    full_name = f"{first_name} {last_name}".strip() or email_l.split("@")[0]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE LOWER(email)=LOWER($1)", email_l)
        if row:
            return row["id"], False

        # Yeni user oluştur
        row = await conn.fetchrow("""
            INSERT INTO users (email, name, phone, created_at, last_login, last_active)
            VALUES ($1, $2, NULLIF($3, ''), NOW(), NOW(), NOW())
            RETURNING id
        """, email_l, full_name, phone)
        new_id = row["id"]
        logger.info(f"[PAY3] Yeni user oluşturuldu: id={new_id} email={email_l}")
        return new_id, True


# ═══════════════════════════════════════════════════════════════════════════
# Shopier signature verification
# ═══════════════════════════════════════════════════════════════════════════
def _verify_shopier_signature(raw_body: bytes, signature_header: str, timestamp_header: str) -> bool:
    """
    Shopier-Signature: HMAC-SHA256(timestamp + "." + body, webhook_token)
    Token webhook oluşturulurken Shopier'dan alındı.
    """
    if not SHOPIER_WEBHOOK_TOKEN:
        logger.error("[PAY3 SIG] SHOPIER_WEBHOOK_TOKEN yok")
        return False
    if not signature_header or not timestamp_header:
        return False

    # Yaygın HMAC formatı: signed = timestamp + "." + raw_body
    signed_payload = f"{timestamp_header}.".encode() + raw_body
    computed = hmac.new(
        SHOPIER_WEBHOOK_TOKEN.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    # Bazı formatlarda sadece body imzalanabilir — alternatif de denenecek
    computed_body_only = hmac.new(
        SHOPIER_WEBHOOK_TOKEN.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    match_a = hmac.compare_digest(computed, signature_header)
    match_b = hmac.compare_digest(computed_body_only, signature_header)

    logger.info(
        f"[PAY3 SIG] body_len={len(raw_body)} ts={timestamp_header} "
        f"received={signature_header[:16]} "
        f"computed_ts_body={computed[:16]} match_a={match_a} match_b={match_b}"
    )

    return match_a or match_b


# ═══════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/payment3/health")
async def health():
    return {
        "status": "ok",
        "service": "payment3-shopier",
        "db": db_pool is not None,
        "shopier_configured": bool(SHOPIER_PRODUCT_URL),
    }


@app.post("/payment3/checkout")
async def checkout_create(authorization: str = Header(None)):
    """
    Frontend'den çağrılır → Shopier ürün linkini döner.
    Kullanıcı oraya gider, ödeme yapar; webhook ile premium aktif olur.
    """
    payload = _verify_jwt(authorization)
    user_id, email = await _user_id_from_jwt(payload)

    if not SHOPIER_PRODUCT_URL:
        raise HTTPException(status_code=500, detail="SHOPIER_PRODUCT_URL yapılandırılmamış")

    # Kullanıcının email'ini önceden doldurmak için URL parametresi ekleyebiliriz
    # Shopier "?email=" parametresi kabul ediyor (mağaza sayfasında autofill)
    from urllib.parse import quote
    url = f"{SHOPIER_PRODUCT_URL}?email={quote(email)}"

    return {"success": True, "url": url, "user_id": user_id, "email": email}


@app.post("/payment3/webhook")
async def webhook_handler(request: Request):
    """Shopier webhook handler — order.created event'ini işler."""
    raw_body = await request.body()
    headers = request.headers

    event       = headers.get("shopier-event") or headers.get("Shopier-Event", "")
    signature   = headers.get("shopier-signature") or headers.get("Shopier-Signature", "")
    timestamp   = headers.get("shopier-timestamp") or headers.get("Shopier-Timestamp", "")
    webhook_id  = headers.get("shopier-webhook-id") or headers.get("Shopier-Webhook-Id", "")

    # İmza doğrula
    if not _verify_shopier_signature(raw_body, signature, timestamp):
        logger.error(f"[PAY3 WEBHOOK] İmza geçersiz! webhook_id={webhook_id}")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Body parse
    try:
        order = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"[PAY3 WEBHOOK] event={event} webhook_id={webhook_id} order_id={order.get('id')}")

    # Sadece order.created işle (paid status'lu)
    if event != "order.created":
        return {"success": True, "skipped": True, "event": event}

    # Sadece paid order'ları işle
    payment_status = order.get("paymentStatus", "")
    if payment_status != "paid":
        logger.info(f"[PAY3] Skipped (paymentStatus={payment_status})")
        return {"success": True, "skipped": True, "reason": "not_paid"}

    # Ürün filtresi (opsiyonel — sadece premium ürün için işle)
    if SHOPIER_PRODUCT_ID:
        line_items = order.get("lineItems") or []
        product_ids = [str(li.get("productId", "")) for li in line_items]
        if SHOPIER_PRODUCT_ID not in product_ids:
            logger.info(f"[PAY3] Skipped (product mismatch: got {product_ids}, expected {SHOPIER_PRODUCT_ID})")
            return {"success": True, "skipped": True, "reason": "product_mismatch"}

    # Alıcı bilgileri (önce shippingInfo, yoksa billingInfo)
    shipping = order.get("shippingInfo") or {}
    billing  = order.get("billingInfo")  or {}
    info     = shipping if shipping.get("email") else billing

    email     = (info.get("email") or "").strip().lower()
    first_nm  = (info.get("firstName") or "").strip()
    last_nm   = (info.get("lastName") or "").strip()
    phone     = (info.get("phone") or "").strip()

    if not email or "@" not in email:
        logger.error(f"[PAY3 WEBHOOK] Email yok order={order.get('id')}")
        return {"success": False, "error": "no_email"}

    # User bul/oluştur
    user_id, is_new = await _ensure_user(email, first_nm, last_nm, phone)

    # Period_end — Shopier abonelik yok, +30 gün manuel
    period_end = datetime.now(timezone.utc) + timedelta(days=30)
    order_id = str(order.get("id", ""))

    await _activate_premium(
        user_id=user_id,
        shopier_order_id=order_id,
        period_end=period_end,
        customer_email=email,
        customer_name=f"{first_nm} {last_nm}".strip(),
        is_new_user=is_new,
    )

    return {"success": True, "user_id": user_id, "is_new_user": is_new, "order_id": order_id}


@app.get("/payment3/subscription/status")
async def subscription_status(authorization: str = Header(None)):
    """Kullanıcının abonelik durumu."""
    payload = _verify_jwt(authorization)
    user_id, _ = await _user_id_from_jwt(payload)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT s.plan_id, s.status, s.billing_period,
                   s.current_period_start, s.current_period_end,
                   s.iyzico_subscription_ref, s.metadata,
                   u.is_premium, u.subscription_active
            FROM users u
            LEFT JOIN user_subscriptions s ON s.user_id = u.id
                AND s.status IN ('active','cancelled','trialing')
            WHERE u.id = $1
            ORDER BY s.created_at DESC NULLS LAST
            LIMIT 1
        """, user_id)

    if not row:
        return {"success": True, "is_premium": False, "subscription": None}

    meta = row["metadata"] or {}
    if isinstance(meta, str):
        try: meta = json.loads(meta)
        except: meta = {}

    return {
        "success":    True,
        "is_premium": bool(row["is_premium"]),
        "active":     bool(row["subscription_active"]),
        "subscription": {
            "plan_id":              row["plan_id"],
            "status":               row["status"],
            "billing_period":       row["billing_period"],
            "current_period_start": row["current_period_start"].isoformat() if row["current_period_start"] else None,
            "current_period_end":   row["current_period_end"].isoformat() if row["current_period_end"] else None,
            "payment_provider":     meta.get("provider", "shopier"),
        } if row["plan_id"] else None,
    }


@app.post("/payment3/subscription/cancel")
async def subscription_cancel(authorization: str = Header(None)):
    """
    Shopier'da abonelik mantığı yok — sadece DB'de cancelled işaretle,
    period_end'e kadar premium aktif kalır.
    """
    payload = _verify_jwt(authorization)
    user_id, _ = await _user_id_from_jwt(payload)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE user_subscriptions
            SET status = 'cancelled', updated_at = NOW()
            WHERE user_id = $1 AND status IN ('active','trialing')
        """, user_id)

    logger.info(f"[PAY3 CANCEL] user_id={user_id} (Shopier: manuel iptal)")
    return {
        "success": True,
        "message": "Aboneliğiniz iptal edildi. Dönem sonuna kadar Premium aktif kalacak. Yenilemek için Shopier'dan tekrar satın alabilirsiniz."
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8007)