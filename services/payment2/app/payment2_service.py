"""
ONE-BUNE Payment2 Service — Lemon Squeezy
==========================================
- Webhook'lardan ödeme/abonelik eventlerini alır
- Premium aktivasyonu yapar (mevcut iyzico ile aynı DB şeması)
- Embedded checkout için checkout URL oluşturur

Endpoints:
  GET  /payment2/health              → health check
  POST /payment2/checkout            → kullanıcıya özel checkout URL döner
  POST /payment2/webhook             → Lemon Squeezy webhook (signature doğrulamalı)
  GET  /payment2/subscription/status → abonelik durumu
  POST /payment2/subscription/cancel → aboneliği iptal et (Lemon API)
"""
import os
import json
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict, Any

import httpx
import jwt as pyjwt
import asyncpg
import aiosmtplib
from fastapi import FastAPI, Request, HTTPException, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("payment2")

# ═════════════════ ENV ═════════════════
LEMON_API_KEY        = os.getenv("LEMON_API_KEY", "")
LEMON_WEBHOOK_SECRET = os.getenv("LEMON_WEBHOOK_SECRET", "")
LEMON_STORE_ID       = os.getenv("LEMON_STORE_ID", "")
LEMON_VARIANT_ID     = os.getenv("LEMON_VARIANT_ID", "")
LEMON_CHECKOUT_URL   = os.getenv("LEMON_CHECKOUT_URL", "")  # https://one-bune.lemonsqueezy.com/checkout/buy/...
LEMON_API_BASE       = "https://api.lemonsqueezy.com/v1"

APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "https://one-bune.com")
JWT_SECRET     = os.getenv("JWT_SECRET", "")

# DB
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASS = os.getenv("DB_PASSWORD", "")

# SMTP
SMTP_SERVER = os.getenv("SMTP_SERVER", "")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
SMTP_FROM   = os.getenv("SMTP_FROM", SMTP_USER)

# ═════════════════ APP ═════════════════
app = FastAPI(title="ONE-BUNE Payment2 (Lemon)", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db_pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def startup():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASS,
            min_size=1, max_size=5, command_timeout=30,
        )
        logger.info("[PAY2] DB pool hazır")
    except Exception as e:
        logger.error(f"[PAY2] DB pool hatası: {e}")


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


# ═════════════════ HELPERS ═════════════════

def _verify_jwt(authorization: str) -> Dict[str, Any]:
    """Bearer token'dan payload çıkar, geçersizse 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization gerekli")
    token = authorization.split(" ", 1)[1]
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token geçersiz: {e}")


def _verify_lemon_signature(raw_body: bytes, signature: str) -> bool:
    """Lemon webhook X-Signature doğrula."""
    if not LEMON_WEBHOOK_SECRET or not signature:
        logger.warning(f"[PAY2 SIG] secret_empty={not LEMON_WEBHOOK_SECRET} sig_empty={not signature}")
        return False
    computed = hmac.new(
        LEMON_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    ok = hmac.compare_digest(computed, signature)
    logger.info(f"[PAY2 SIG] secret_first6={LEMON_WEBHOOK_SECRET[:6]} body_len={len(raw_body)} "
                f"received={signature[:16]} computed={computed[:16]} match={ok}")
    return ok


async def _send_email(to: str, subject: str, html: str, plain: str):
    """SMTP ile email gönder (async)."""
    if not SMTP_SERVER or not SMTP_USER:
        logger.warning("[PAY2 EMAIL] SMTP yapılandırılmamış")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = f"ONE-BUNE <{SMTP_FROM}>"
        msg["To"]      = to
        msg["Subject"] = subject
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=SMTP_SERVER, port=SMTP_PORT,
            username=SMTP_USER, password=SMTP_PASS,
            start_tls=True,
        )
        logger.info(f"[PAY2 EMAIL] → {to} / {subject}")
    except Exception as e:
        logger.error(f"[PAY2 EMAIL ERROR] {e}")


def _welcome_email(first_name: str) -> tuple[str, str]:
    name = (first_name or "Premium Üye").strip()
    html = f"""<!DOCTYPE html><html><body style="font-family:Inter,Arial,sans-serif;background:#f4f4f5;margin:0;padding:40px 20px;">
<table width="100%" cellspacing="0" cellpadding="0">
<tr><td align="center">
<table width="560" style="background:#fff;border-radius:16px;padding:32px;">
<tr><td style="text-align:center;">
<h1 style="color:#0a0a0c;margin:0 0 12px 0;">🎉 Premium Üyeliğin Aktif!</h1>
<p style="color:#52525b;font-size:15px;line-height:1.6;margin:0 0 20px 0;">Merhaba <strong>{name}</strong>,</p>
<p style="color:#52525b;font-size:15px;line-height:1.6;margin:0 0 16px 0;">ONE-BUNE Premium üyeliğin başarıyla aktif edildi. Artık tüm premium özelliklere sınırsız erişimin var.</p>
<a href="{APP_PUBLIC_URL}" style="display:inline-block;background:linear-gradient(135deg,#4facfe,#00f2fe);color:#001428;text-decoration:none;padding:14px 32px;border-radius:10px;font-weight:700;margin-top:20px;">Hemen Kullanmaya Başla</a>
</td></tr></table>
</td></tr></table>
</body></html>"""
    plain = f"Merhaba {name},\n\nONE-BUNE Premium üyeliğin aktif edildi.\n\n{APP_PUBLIC_URL}"
    return html, plain


def _cancel_email(first_name: str, end_date: str) -> tuple[str, str]:
    name = (first_name or "Üye").strip()
    html = f"""<!DOCTYPE html><html><body style="font-family:Inter,Arial,sans-serif;background:#f4f4f5;margin:0;padding:40px 20px;">
<table width="100%" cellspacing="0" cellpadding="0"><tr><td align="center">
<table width="560" style="background:#fff;border-radius:16px;padding:32px;">
<tr><td>
<h2 style="color:#0a0a0c;">ONE-BUNE Premium İptal</h2>
<p style="color:#52525b;font-size:15px;line-height:1.6;">Merhaba <strong>{name}</strong>, aboneliğin iptal talebin alındı.</p>
<p style="color:#52525b;font-size:14px;">Üyeliğin <strong>{end_date}</strong> tarihine kadar aktif kalacak. Sonrasında ücretsiz pakete geri dönersin.</p>
</td></tr></table></td></tr></table></body></html>"""
    plain = f"Merhaba {name},\n\nAboneliğin iptal edildi. {end_date} tarihine kadar Premium kullanmaya devam edebilirsin."
    return html, plain


# ═════════════════ DB OPERATIONS ═════════════════

async def _activate_premium(
    user_id: int,
    lemon_subscription_id: str,
    period_end: datetime,
    customer_email: Optional[str] = None,
):
    """
    Kullanıcıyı premium yap + subscriptions kaydı oluştur/güncelle.
    Mevcut iyzico ile aynı şema — lemon ID'sini iyzico_subscription_ref kolonuna
    yazıyoruz (kolon adı tarihsel, içerik provider-agnostic).
    """
    if not db_pool:
        logger.error("[PAY2] DB pool yok!")
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET is_premium = TRUE, subscription_active = TRUE
            WHERE id = $1
        """, user_id)

        # UPSERT subscriptions — Lemon ID'sini iyzico_subscription_ref'e yazıyoruz
        # (mevcut kolon, provider-agnostic kullanım)
        await conn.execute("""
            INSERT INTO user_subscriptions
                (user_id, plan_id, status, billing_period,
                 current_period_start, current_period_end,
                 iyzico_subscription_ref, metadata,
                 created_at, updated_at)
            VALUES ($1, 'premium', 'active', 'monthly',
                    NOW(), $2, $3, '{"provider":"lemonsqueezy"}'::jsonb, NOW(), NOW())
            ON CONFLICT (user_id) WHERE status IN ('active','trialing')
            DO UPDATE SET
                plan_id                 = 'premium',
                status                  = 'active',
                billing_period          = 'monthly',
                current_period_start    = NOW(),
                current_period_end      = $2,
                iyzico_subscription_ref = $3,
                metadata                = '{"provider":"lemonsqueezy"}'::jsonb,
                updated_at              = NOW()
        """, user_id, period_end, lemon_subscription_id)

        # Email — kullanıcıyı bul
        row = await conn.fetchrow("SELECT email, name FROM users WHERE id=$1", user_id)
        if row and row["email"]:
            html, plain = _welcome_email(row.get("name") or "")
            asyncio.create_task(
                _send_email(row["email"], "✅ ONE-BUNE Premium üyeliğin aktif", html, plain)
            )

    logger.info(f"[PAY2] Premium aktifleştirildi: user_id={user_id} sub={lemon_subscription_id}")


async def _cancel_subscription_db(user_id: int, end_date: datetime):
    """Aboneliği iptal et — end_date'e kadar aktif kalacak."""
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE user_subscriptions
            SET status = 'cancelled', current_period_end = $2, updated_at = NOW()
            WHERE user_id = $1 AND status IN ('active','trialing')
        """, user_id, end_date)

        row = await conn.fetchrow("SELECT email, name FROM users WHERE id=$1", user_id)
        if row and row["email"]:
            end_str = end_date.strftime("%d.%m.%Y")
            html, plain = _cancel_email(row.get("name") or "", end_str)
            asyncio.create_task(
                _send_email(row["email"], "ONE-BUNE Premium abonelik iptali", html, plain)
            )

    logger.info(f"[PAY2] Subscription iptal: user_id={user_id} end={end_date}")


async def _expire_subscription(user_id: int):
    """Süresi dolan/iptal edilen aboneliği kapat."""
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET is_premium = FALSE, subscription_active = FALSE
            WHERE id = $1
        """, user_id)
        await conn.execute("""
            UPDATE user_subscriptions SET status = 'expired', updated_at = NOW()
            WHERE user_id = $1 AND status IN ('active','cancelled')
        """, user_id)
    logger.info(f"[PAY2] Subscription expired: user_id={user_id}")


async def _user_id_from_lemon_data(custom: dict, customer_email: str) -> Optional[int]:
    """Lemon checkout 'custom_data' içinde user_id varsa al, yoksa email'den bul."""
    if custom and "user_id" in custom:
        try:
            return int(custom["user_id"])
        except Exception:
            pass
    if customer_email and db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM users WHERE LOWER(email)=LOWER($1)", customer_email)
            if row:
                return row["id"]
    return None


# ═════════════════ ENDPOINTS ═════════════════

@app.get("/payment2/health")
async def health():
    return {"status": "ok", "service": "payment2", "provider": "lemonsqueezy"}


@app.post("/payment2/checkout")
async def checkout_create(
    authorization: str = Header(None),
    body: dict = Body(default={}),
):
    """
    Kullanıcıya özel checkout URL döner.
    custom_data: {"user_id": "...", "email": "..."}  — webhook'ta okunacak
    """
    payload = _verify_jwt(authorization)
    user_id = payload.get("user_id") or payload.get("sub")
    email   = payload.get("email", "")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id bulunamadı")

    if not LEMON_CHECKOUT_URL:
        raise HTTPException(status_code=500, detail="LEMON_CHECKOUT_URL yapılandırılmamış")

    # Basit yol: hazır checkout URL'i + query param ile custom_data
    # Lemon embedded checkout için: ?checkout[custom][user_id]=X&checkout[email]=X
    from urllib.parse import quote
    url = (
        f"{LEMON_CHECKOUT_URL}"
        f"?embed=1"
        f"&checkout[email]={quote(email)}"
        f"&checkout[custom][user_id]={user_id}"
        f"&checkout[custom][email]={quote(email)}"
    )

    return {"success": True, "url": url, "user_id": user_id}


@app.post("/payment2/webhook")
async def lemon_webhook(request: Request, x_signature: str = Header(default="", alias="X-Signature")):
    """Lemon Squeezy webhook handler."""
    raw_body = await request.body()

    # Imza doğrula
    if not _verify_lemon_signature(raw_body, x_signature):
        logger.warning(f"[PAY2 WEBHOOK] İmza eşleşmedi: sig={x_signature[:16]}...")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error(f"[PAY2 WEBHOOK] JSON parse: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    meta  = payload.get("meta", {})
    event = meta.get("event_name", "")
    data  = payload.get("data", {})
    attr  = data.get("attributes", {})
    cdata = meta.get("custom_data", {}) or {}

    customer_email = (attr.get("user_email") or attr.get("customer_email") or "").lower()
    user_id = await _user_id_from_lemon_data(cdata, customer_email)

    logger.info(f"[PAY2 WEBHOOK] event={event} user_id={user_id} email={customer_email}")

    if not user_id:
        # Lemon bekliyor: 200 OK; kullanıcı yoksa logla geç
        logger.warning(f"[PAY2 WEBHOOK] Kullanıcı bulunamadı: email={customer_email}")
        return {"success": True, "note": "user_not_found"}

    try:
        if event == "subscription_created":
            sub_id = data.get("id", "")
            renews_at = attr.get("renews_at")  # ISO 8601
            period_end = _parse_iso(renews_at) or datetime.now(timezone.utc).replace(day=1)
            await _activate_premium(user_id, sub_id, period_end, customer_email)

        elif event == "subscription_payment_success":
            sub_id = (data.get("attributes") or {}).get("subscription_id", "") or data.get("id", "")
            renews_at = attr.get("renews_at")
            period_end = _parse_iso(renews_at) or datetime.now(timezone.utc)
            await _activate_premium(user_id, str(sub_id), period_end, customer_email)

        elif event == "subscription_payment_failed":
            logger.warning(f"[PAY2 WEBHOOK] Ödeme başarısız: user_id={user_id}")
            # Şimdilik hesabı düşürmüyoruz — Lemon kendisi retry yapacak.

        elif event == "subscription_cancelled":
            ends_at = attr.get("ends_at") or attr.get("renews_at")
            end_dt  = _parse_iso(ends_at) or datetime.now(timezone.utc)
            await _cancel_subscription_db(user_id, end_dt)

        elif event == "subscription_expired":
            await _expire_subscription(user_id)

        elif event == "order_created":
            # Tek seferlik satış (subscription değil) — şimdilik no-op
            logger.info(f"[PAY2 WEBHOOK] order_created user_id={user_id} (no-op)")

        else:
            logger.info(f"[PAY2 WEBHOOK] İşlenmemiş event: {event}")
    except Exception as e:
        logger.error(f"[PAY2 WEBHOOK] İşleme hatası: {e}")
        # 500 dönmeyelim, Lemon retry yapar — 200 ile geç
        return {"success": False, "error": str(e)}

    return {"success": True, "event": event, "user_id": user_id}


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # "2026-06-13T10:00:00.000000Z" gibi
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


@app.get("/payment2/subscription/status")
async def subscription_status(authorization: str = Header(None)):
    """Kullanıcının abonelik durumunu döner."""
    payload = _verify_jwt(authorization)
    user_id = payload.get("user_id") or payload.get("sub")

    if not db_pool:
        raise HTTPException(status_code=503, detail="DB yok")

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
        """, int(user_id))

    if not row:
        return {"success": True, "is_premium": False, "subscription": None}

    # Provider'ı metadata'dan al
    meta = row["metadata"] or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    provider = meta.get("provider", "iyzico")

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
            "payment_provider":     provider,
        } if row["plan_id"] else None,
    }


@app.post("/payment2/subscription/cancel")
async def subscription_cancel(authorization: str = Header(None)):
    """
    Lemon Squeezy API ile aboneliği iptal et.
    Lemon webhook'u ile DB güncellenecek (subscription_cancelled event).
    """
    payload = _verify_jwt(authorization)
    user_id = payload.get("user_id") or payload.get("sub")

    if not db_pool:
        raise HTTPException(status_code=503, detail="DB yok")

    # Lemon subscription_id — iyzico_subscription_ref kolonunda saklı
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT iyzico_subscription_ref, metadata FROM user_subscriptions
            WHERE user_id = $1 AND status IN ('active','trialing')
            ORDER BY created_at DESC LIMIT 1
        """, int(user_id))

    if not row or not row["iyzico_subscription_ref"]:
        raise HTTPException(status_code=404, detail="Aktif abonelik bulunamadı")

    # Provider check — sadece lemonsqueezy ise iptal et
    meta = row["metadata"] or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if meta.get("provider") != "lemonsqueezy":
        raise HTTPException(status_code=400, detail="Bu abonelik Lemon Squeezy değil — payment/iyzico endpoint'i kullan")

    sub_id = row["iyzico_subscription_ref"]

    if not LEMON_API_KEY:
        raise HTTPException(status_code=500, detail="LEMON_API_KEY yok")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.delete(
                f"{LEMON_API_BASE}/subscriptions/{sub_id}",
                headers={
                    "Authorization": f"Bearer {LEMON_API_KEY}",
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                }
            )
            if r.status_code >= 400:
                logger.error(f"[PAY2 CANCEL] Lemon API {r.status_code}: {r.text[:200]}")
                raise HTTPException(status_code=502, detail=f"Lemon API hata: {r.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PAY2 CANCEL] {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {"success": True, "message": "İptal talebi alındı. Webhook ile süreç tamamlanacak."}