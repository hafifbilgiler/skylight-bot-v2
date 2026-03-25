"""
═══════════════════════════════════════════════════════════════
ONE-BUNE PAYMENT SERVICE - iyzico Subscription
═══════════════════════════════════════════════════════════════
v2.2 değişiklikleri:
  ✅ Callback idempotency — duplicate activation önlendi
  ✅ DB yoksa kullanıcıya bilgi verilir, sessiz fail yok
  ✅ paidPrice hardcoded 64 kaldırıldı — iyzico'dan alınıyor
  ✅ İptal guard — iyzico fail ederse DB dokunulmaz
  ✅ Email async (aiosmtplib) — event loop bloklanmıyor
═══════════════════════════════════════════════════════════════
"""

import os, json, time, base64, hashlib, hmac, secrets, logging, asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import httpx, asyncpg, aiosmtplib
from fastapi import FastAPI, HTTPException, Header, Body, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="ONE-BUNE Payment Service", version="2.2.0")

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

SMTP_SERVER = os.getenv("SMTP_SERVER", "").strip()
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "").strip()
SMTP_PASS   = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM   = os.getenv("SMTP_FROM", os.getenv("FROM_EMAIL", SMTP_USER)).strip()

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


def build_auth(uri: str, body_str: str) -> tuple[str, str]:
    """
    iyzico IYZWSv2 resmi format (docs.iyzico.com):
    1. payload  = randomKey + uri_path + body
    2. sig      = hex(HMAC-SHA256(secretKey, payload))
    3. authStr  = "apiKey:X&randomKey:X&signature:X"
    4. header   = "IYZWSv2 " + base64(authStr)
    """
    rnd     = secrets.token_hex(8)
    payload = rnd + uri + body_str
    sig     = hmac.new(
        IYZICO_SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    auth_str = f"apiKey:{IYZICO_API_KEY}&randomKey:{rnd}&signature:{sig}"
    encoded  = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
    return f"IYZWSv2 {encoded}", rnd


async def iyzico_post(uri: str, payload: Dict) -> Dict:
    body      = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    auth, rnd = build_auth(uri, body)
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
    if r.status_code == 422:
        logger.error(f"[IYZICO 422] {json.dumps(data, ensure_ascii=False)}")
        raise HTTPException(502, f"iyzico format hatası: {data}")
    if r.status_code >= 400:
        raise HTTPException(502, data)
    return data


async def iyzico_get(uri: str) -> Dict:
    auth, rnd = build_auth(uri, "")
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


# ─────────────────────────────────────────────────────────────
# EMAIL — ASYNC (event loop bloklanmaz)
# ─────────────────────────────────────────────────────────────

async def _send_email_async(to: str, subject: str, html: str, plain: str):
    """Async SMTP ile email gönder — aiosmtplib kullanır."""
    if not SMTP_SERVER or not SMTP_USER:
        logger.warning("[EMAIL] SMTP yapılandırılmamış, email gönderilmedi.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]       = f"ONE-BUNE <{SMTP_FROM}>"
        msg["To"]         = to
        msg["Subject"]    = subject
        msg["X-Priority"] = "3"
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=SMTP_SERVER,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASS,
            start_tls=True,
        )
        logger.info(f"[EMAIL] Gönderildi: {to} / {subject}")
    except Exception as e:
        logger.error(f"[EMAIL ERROR] to={to} subject={subject} error={e}")


def _build_welcome_email(first_name: str) -> tuple[str, str]:
    """Premium hoş geldin email içeriği döner: (html, plain)"""
    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ONE-BUNE Premium</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:40px 20px;">
  <tr>
    <td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">

        <!-- HEADER -->
        <tr>
          <td style="background:#0a0a0c;border-radius:16px 16px 0 0;padding:32px;text-align:center;">
            <div style="display:inline-block;width:56px;height:56px;border-radius:50%;
                        background:linear-gradient(135deg,#bc4efd,#00f2fe);
                        line-height:56px;font-size:24px;margin-bottom:16px;">&#9733;</div>
            <h1 style="color:#ffffff;font-size:22px;font-weight:700;margin:0 0 6px;">
              Premium Uyeliginiz Aktif
            </h1>
            <p style="color:#8e8ea0;font-size:14px;margin:0;">
              ONE-BUNE AI &middot; SKYMERGE TECHNOLOGY
            </p>
          </td>
        </tr>

        <!-- BODY -->
        <tr>
          <td style="background:#111114;padding:32px;">
            <p style="color:#eeeef0;font-size:15px;line-height:1.6;margin:0 0 24px;">
              Merhaba <strong>{first_name}</strong>,
            </p>
            <p style="color:#8e8ea0;font-size:14px;line-height:1.7;margin:0 0 28px;">
              ONE-BUNE Premium uyeliginiz basariyla aktif edildi.
              Artik tum ozelliklere sinırsiz erisebilirsiniz.
            </p>

            <!-- FEATURES -->
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="background:#18181d;border-radius:12px;
                          border:1px solid rgba(255,255,255,0.06);
                          padding:20px;margin-bottom:28px;">
              <tr><td>
                <p style="color:#00f2fe;font-size:11px;font-weight:700;
                           text-transform:uppercase;letter-spacing:1.5px;
                           margin:0 0 16px;">Erisim Kazandiklariniz</p>
                <table width="100%" cellpadding="0" cellspacing="0">
                  {"".join(f'''<tr>
                    <td style="padding:6px 0;">
                      <table cellpadding="0" cellspacing="0"><tr>
                        <td style="color:#00f2fe;font-size:13px;width:24px;">&rsaquo;</td>
                        <td style="color:#eeeef0;font-size:13px;line-height:1.5;">{text}</td>
                      </tr></table>
                    </td>
                  </tr>''' for text in [
                    "Sinırsiz mesaj hakki",
                    "7 AI modu — Asistan, Kod, IT Uzmani, Ogrenci, Sosyal",
                    "Dosya yukleme ve analiz",
                    "AI gorsel olusturma",
                    "Web arama ve derin arastirma",
                    "Oncelikli destek",
                  ])}
                </table>
              </td></tr>
            </table>

            <!-- CTA -->
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center">
                  <a href="https://one-bune.com"
                     style="display:inline-block;padding:14px 40px;
                            background:linear-gradient(135deg,#bc4efd,#00f2fe);
                            color:#0a0a0c;font-size:14px;font-weight:700;
                            border-radius:10px;text-decoration:none;
                            letter-spacing:0.3px;">
                    Kullanmaya Basla
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- FOOTER -->
        <tr>
          <td style="background:#0a0a0c;border-radius:0 0 16px 16px;
                     padding:20px 32px;text-align:center;
                     border-top:1px solid rgba(255,255,255,0.05);">
            <p style="color:#55556a;font-size:11px;margin:0 0 6px;line-height:1.6;">
              Bu e-posta <strong style="color:#8e8ea0;">one-bune.com</strong> uzerinden
              gerceklestirilen Premium aboneliginiz nedeniyle gonderilmistir.
            </p>
            <p style="color:#55556a;font-size:11px;margin:0;">
              Aboneliginizi istediginiz zaman hesap ayarlarinizdan iptal edebilirsiniz.
            </p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""

    plain = f"""Merhaba {first_name},

ONE-BUNE Premium uyeliginiz basariyla aktif edildi.

Artik erisebilecekleriniz:
- Sinırsiz mesaj hakki
- 7 AI modu
- Dosya yukleme ve analiz
- AI gorsel olusturma
- Web arama ve derin arastirma
- Oncelikli destek

Kullanmaya baslamak icin: https://one-bune.com

ONE-BUNE AI / SKYMERGE TECHNOLOGY
"""
    return html, plain


def _build_cancel_email(first_name: str, period_end, immediate: bool) -> tuple[str, str]:
    """İptal email içeriği döner: (html, plain)"""
    period_str = period_end.strftime("%d.%m.%Y") if period_end else ""

    if immediate:
        detail = "Premium aboneliginiz aninda iptal edildi."
        sub    = "Premium ozelliklerine erisim sona erdi."
    else:
        detail = f"Premium aboneliginiz <strong>{period_str}</strong> tarihine kadar aktif kalacak, bu tarihten itibaren ucretsiz plana gececeksiniz."
        sub    = f"Erisim {period_str} tarihine kadar devam eder."

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:Inter,-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:40px 20px;">
  <tr><td align="center">
    <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">

      <tr>
        <td style="background:#0a0a0c;border-radius:16px 16px 0 0;padding:32px;text-align:center;">
          <div style="font-size:32px;margin-bottom:12px;">&#128274;</div>
          <h1 style="color:#ffffff;font-size:20px;font-weight:700;margin:0 0 6px;">
            Abonelik Iptali
          </h1>
          <p style="color:#8e8ea0;font-size:13px;margin:0;">ONE-BUNE AI &middot; SKYMERGE TECHNOLOGY</p>
        </td>
      </tr>

      <tr>
        <td style="background:#111114;padding:32px;">
          <p style="color:#eeeef0;font-size:15px;margin:0 0 20px;">
            Merhaba <strong>{first_name}</strong>,
          </p>
          <p style="color:#8e8ea0;font-size:14px;line-height:1.7;margin:0 0 24px;">
            {detail}
          </p>

          <div style="background:#18181d;border-radius:10px;padding:16px 20px;
                      border:1px solid rgba(255,180,0,0.2);margin-bottom:24px;">
            <p style="color:#ffb400;font-size:13px;margin:0;">
              &#9432; {sub}
            </p>
          </div>

          <p style="color:#8e8ea0;font-size:13px;line-height:1.7;margin:0 0 24px;">
            Tekrar abone olmak isterseniz one-bune.com uzerinden
            Premium'a geri donebilirsiniz.
          </p>

          <table width="100%" cellpadding="0" cellspacing="0">
            <tr><td align="center">
              <a href="https://one-bune.com"
                 style="display:inline-block;padding:12px 32px;
                        background:#18181d;border:1px solid rgba(255,255,255,0.1);
                        color:#eeeef0;font-size:13px;font-weight:600;
                        border-radius:10px;text-decoration:none;">
                one-bune.com
              </a>
            </td></tr>
          </table>
        </td>
      </tr>

      <tr>
        <td style="background:#0a0a0c;border-radius:0 0 16px 16px;padding:20px 32px;
                   text-align:center;border-top:1px solid rgba(255,255,255,0.05);">
          <p style="color:#55556a;font-size:11px;margin:0;line-height:1.6;">
            Bu e-posta abonelik iptaliniz nedeniyle gonderilmistir.<br>
            ONE-BUNE AI / SKYMERGE TECHNOLOGY
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    plain = f"""Merhaba {first_name},

{detail.replace('<strong>','').replace('</strong>','')}

{sub}

Tekrar abone olmak icin: https://one-bune.com

ONE-BUNE AI / SKYMERGE TECHNOLOGY
"""
    return html, plain


async def activate_subscription(user_id: int, subscription_ref: str, plan_code: str):
    """Kullanıcıyı premium yap — DB şeması ile uyumlu"""
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE users
                SET is_premium = TRUE, subscription_active = TRUE
                WHERE id = $1
            """, user_id)

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

            row = await conn.fetchrow(
                "SELECT email, name FROM users WHERE id = $1", user_id
            )

        logger.info(f"[SUBSCRIPTION] User {user_id} aktifleştirildi")

        # Async email — event loop bloklanmaz
        if row and SMTP_SERVER and SMTP_USER:
            first_name = (row["name"] or "Kullanici").split()[0]
            html, plain = _build_welcome_email(first_name)
            asyncio.create_task(
                _send_email_async(row["email"], "Premium üyeliğiniz aktif edildi", html, plain)
            )

    except Exception as e:
        logger.error(f"[SUBSCRIPTION ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

from pydantic import BaseModel, Field, field_validator
import re as _re

class CheckoutBody(BaseModel):
    token:          Optional[str] = None
    gsmNumber:      Optional[str] = Field(None, max_length=15)
    identityNumber: Optional[str] = Field(None, max_length=11)
    address:        Optional[str] = Field(None, max_length=500)
    city:           Optional[str] = Field(None, max_length=100)
    zipCode:        Optional[str] = Field(None, max_length=10)
    email:          Optional[str] = Field(None, max_length=255)
    name:           Optional[str] = Field(None, max_length=100)
    surname:        Optional[str] = Field(None, max_length=100)

    @field_validator("gsmNumber")
    @classmethod
    def validate_phone(cls, v):
        if v:
            digits = _re.sub(r'[\s\-\+]', '', v)
            if not digits.isdigit() or len(digits) < 10:
                raise ValueError("Geçersiz telefon numarası")
        return v

    @field_validator("identityNumber")
    @classmethod
    def validate_identity(cls, v):
        if v and (not v.isdigit() or len(v) != 11):
            raise ValueError("TC Kimlik No 11 haneli rakam olmalı")
        return v


class CancelBody(BaseModel):
    token:     Optional[str] = None
    immediate: bool = False
    admin:     bool = False
    user_id:   Optional[int] = None


@app.get("/payment/health")
async def health():
    return {
        "status":           "healthy",
        "service":          "payment-service",
        "version":          "2.2.0",
        "iyzico_base_url":  IYZICO_BASE_URL,
        "has_api_key":      bool(IYZICO_API_KEY),
        "has_plan_code":    bool(IYZICO_MONTHLY_PLAN_CODE),
        "database":         "ok" if db_pool else "error",
    }


@app.post("/payment/checkout")
async def payment_checkout(
    request: Request,
    body: CheckoutBody = Body(...),
    authorization: Optional[str] = Header(None),
):
    """iyzico checkout formu başlat"""
    ensure_config()

    token = (body.token
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

    email    = body.email    or user.get("email") or "user@one-bune.com"
    fullname = user.get("name", "One Bune User")
    parts    = fullname.split(" ", 1)
    name     = body.name    or parts[0]
    surname  = body.surname or (parts[1] if len(parts) > 1 else "User")

    phone_input    = (body.gsmNumber      or "").strip()
    city_input     = (body.city           or "").strip()
    address_input  = (body.address        or "").strip()
    zip_input      = (body.zipCode        or "").strip()
    identity_input = (body.identityNumber or "").strip()

    gsm = None
    if phone_input:
        digits = phone_input.replace("+","").replace(" ","").replace("-","")
        if digits.startswith("90") and len(digits) == 12:
            gsm = "+" + digits
        elif digits.startswith("0") and len(digits) == 11:
            gsm = "+9" + digits
        elif len(digits) == 10:
            gsm = "+90" + digits
        else:
            gsm = "+" + digits

    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT phone, city, address, zip_code, identity_no
                FROM users WHERE id = $1
            """, user["id"])
            if row:
                gsm            = gsm            or row["phone"]       or "+905300000000"
                city_input     = city_input     or row["city"]        or "Istanbul"
                address_input  = address_input  or row["address"]     or "Türkiye"
                zip_input      = zip_input      or row["zip_code"]    or "34000"
                identity_input = identity_input or row["identity_no"] or "11111111111"

            await conn.execute("""
                UPDATE users SET
                    phone       = COALESCE(NULLIF($1,''), phone),
                    city        = COALESCE(NULLIF($2,''), city),
                    address     = COALESCE(NULLIF($3,''), address),
                    zip_code    = COALESCE(NULLIF($4,''), zip_code),
                    identity_no = COALESCE(NULLIF($5,''), identity_no)
                WHERE id = $6
            """, gsm, city_input, address_input, zip_input, identity_input, user["id"])
    else:
        gsm            = gsm            or "+905300000000"
        city_input     = city_input     or "Istanbul"
        address_input  = address_input  or "Türkiye"
        zip_input      = zip_input      or "34000"
        identity_input = identity_input or "11111111111"

    billing = {
        "contactName": f"{name} {surname}",
        "address":     address_input,
        "zipCode":     zip_input,
        "city":        city_input,
        "country":     "Türkiye",
    }

    conv_id = f"onebune-{user['id']}-{int(time.time())}"

    payload = {
        "locale":                   "tr",
        "conversationId":           conv_id,
        "callbackUrl":              PAYMENT_CALLBACK_URL,
        "pricingPlanReferenceCode": IYZICO_MONTHLY_PLAN_CODE,
        "subscriptionInitialStatus":"ACTIVE",
        "customer": {
            "name":            name,
            "surname":         surname,
            "email":           email,
            "gsmNumber":       gsm,
            "identityNumber":  identity_input,
            "billingAddress":  billing,
            "shippingAddress": billing,
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
    """
    iyzico ödeme sonucu callback — v2.2
    ✅ Idempotent: aynı token ikinci kez gelirse tekrar işlenmez
    ✅ DB yoksa kullanıcıya bilgi verilir, sessiz fail olmaz
    ✅ paidPrice iyzico'dan alınır, hardcoded değer yok
    """
    ensure_config()

    form  = await request.form()
    token = form.get("token")

    if not token:
        return HTMLResponse("<h1>Hata: Token eksik</h1>", status_code=400)

    try:
        # ── 1. iyzico'dan sonucu al ──────────────────────────────
        data = await iyzico_get(f"/v2/subscription/checkoutform/{token}")

        if data.get("status") != "success":
            logger.error(f"[CALLBACK] iyzico ödeme başarısız: {data}")
            await log_event("payment_failed", data={"token": token, "iyzico_response": data},
                            ip=request.client.host)
            return HTMLResponse(f"""
                <html>
                <head><meta http-equiv="refresh" content="3;url={APP_PUBLIC_URL}?payment=failed"></head>
                <body style="font-family:sans-serif;text-align:center;padding:40px;">
                <h1>❌ Ödeme Başarısız</h1>
                <p>Lütfen tekrar deneyin.</p>
                </body></html>
            """)

        # ── 2. DB kontrol ────────────────────────────────────────
        if not db_pool:
            logger.critical(
                f"[CALLBACK] KRITIK: DB POOL YOK — "
                f"token={token} subscriptionRef={data.get('subscriptionReferenceCode')} "
                f"paymentId={data.get('paymentId')}"
            )
            # Ödeme iyzico'da başarılı ama DB'ye yazılamıyor
            # Production'da buraya Slack/PagerDuty alert ekle
            return HTMLResponse("""
                <html>
                <body style="font-family:sans-serif;text-align:center;padding:40px;">
                <h1>⚠️ Geçici Sistem Hatası</h1>
                <p>Ödemeniz alındı ancak aktivasyon gecikmeli olabilir.<br>
                Lütfen destek ile iletişime geçin: destek@one-bune.com</p>
                </body></html>
            """, status_code=503)

        async with db_pool.acquire() as conn:
            # ── 3. Checkout kaydını bul ──────────────────────────
            checkout = await conn.fetchrow("""
                SELECT user_id, conversation_id, pricing_plan_code, status
                FROM subscription_checkouts WHERE iyzico_token = $1
            """, token)

            if not checkout:
                logger.error(f"[CALLBACK] Checkout bulunamadı: token={token}")
                return HTMLResponse(
                    "<h1>Hata: Ödeme kaydı bulunamadı. Destek ile iletişime geçin.</h1>",
                    status_code=404
                )

            # ── 4. IDEMPOTENCY — zaten tamamlandıysa tekrar işleme ──
            if checkout["status"] == "completed":
                logger.warning(f"[CALLBACK] Duplicate callback yoksayıldı: token={token}")
                return HTMLResponse(f"""
                    <html>
                    <head><meta http-equiv="refresh" content="2;url={APP_PUBLIC_URL}?premium=1"></head>
                    <body style="font-family:sans-serif;text-align:center;padding:40px;">
                    <h1>✅ Premium Zaten Aktif</h1>
                    <p>Yönlendiriliyorsunuz...</p>
                    </body></html>
                """)

            user_id   = checkout["user_id"]
            plan_code = checkout["pricing_plan_code"]

            # ── 5. Checkout'u tamamlandı yap ────────────────────
            await conn.execute("""
                UPDATE subscription_checkouts
                SET status='completed', iyzico_response=$2, completed_at=NOW()
                WHERE iyzico_token=$1
            """, token, json.dumps(data))

            # ── 6. Aboneliği aktifleştir ─────────────────────────
            # iyzico subscription checkout'ta referenceCode data.data içinde gelir
            inner = data.get("data") or {}
            sub_ref = (
                data.get("subscriptionReferenceCode")
                or inner.get("referenceCode")
                or inner.get("subscriptionReferenceCode")
                or ""
            )
            await activate_subscription(user_id, sub_ref, plan_code)

            # ── 7. Ödeme geçmişine kaydet — paidPrice iyzico'dan ─
            # iyzico subscription checkout'ta paidPrice üst seviyede olmayabilir
            inner = data.get("data") or {}
            paid_price = (
                data.get("paidPrice")
                or inner.get("paidPrice")
                or inner.get("price")
            )
            if paid_price is None:
                logger.warning(f"[CALLBACK] paidPrice bulunamadı, 0.0 kaydediliyor")

            await conn.execute("""
                INSERT INTO payment_history
                    (user_id, plan_id, amount, currency, status,
                     iyzico_payment_id, iyzico_conversation_id,
                     iyzico_raw_result, paid_at, created_at)
                VALUES ($1, 'premium', $2, 'TRY', 'completed',
                        $3, $4, $5, NOW(), NOW())
            """, user_id,
                 float(paid_price) if paid_price is not None else 0.0,
                 data.get("paymentId", ""),
                 data.get("conversationId", ""),
                 json.dumps(data))

            await log_event("payment_success", user_id, {"token": token}, request.client.host)

        return HTMLResponse(f"""
            <html>
            <head><meta http-equiv="refresh" content="2;url={APP_PUBLIC_URL}?premium=1"></head>
            <body style="font-family:sans-serif;text-align:center;padding:40px;">
            <h1>✅ Ödeme Başarılı!</h1>
            <p>Premium üyeliğiniz aktif edildi. Yönlendiriliyorsunuz...</p>
            </body></html>
        """)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CALLBACK ERROR] {e}", exc_info=True)
        return HTMLResponse("""
            <html>
            <body style="font-family:sans-serif;text-align:center;padding:40px;">
            <h1>⚠️ İşlem Hatası</h1>
            <p>Ödemeniz alınmış olabilir. Lütfen destek ile iletişime geçin:<br>
            <strong>destek@one-bune.com</strong></p>
            </body></html>
        """, status_code=500)


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


@app.post("/payment/subscription/cancel")
async def subscription_cancel(
    request: Request,
    body: CancelBody = Body(...),
    authorization: Optional[str] = Header(None),
):
    admin_cancel   = body.admin
    direct_user_id = body.user_id

    if admin_cancel and direct_user_id:
        target_user_id = direct_user_id
    else:
        token = (body.token
                 or (authorization.split(" ")[1] if authorization and " " in authorization else None))
        if not token:
            raise HTTPException(401, "Login required")
        user = await get_user(token)
        if not user:
            raise HTTPException(401, "Geçersiz token")
        target_user_id = user["id"]

    immediate = body.immediate

    try:
        if not db_pool:
            raise HTTPException(500, "DB bağlantısı yok")

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT iyzico_subscription_ref, current_period_end
                FROM user_subscriptions
                WHERE user_id = $1 AND status IN ('active','trialing')
                LIMIT 1
            """, target_user_id)

            if not row:
                raise HTTPException(404, "Aktif abonelik bulunamadı")

            sub_ref    = row["iyzico_subscription_ref"]
            period_end = row["current_period_end"]

            # ── iyzico iptal — fail ederse DB güncellenmez ───────
            iyzico_ok    = False
            iyzico_error = None

            if sub_ref and IYZICO_API_KEY:
                try:
                    result = await iyzico_post(
                        f"/v2/subscription/cancel/{sub_ref}",
                        {"locale": "tr", "conversationId": f"cancel-{target_user_id}-{int(time.time())}"}
                    )
                    iyzico_ok = result.get("status") == "success"
                    if not iyzico_ok:
                        iyzico_error = result.get("errorMessage", "Bilinmeyen hata")
                    logger.info(f"[CANCEL] iyzico: {result}")
                except Exception as e:
                    iyzico_error = str(e)
                    logger.error(f"[CANCEL] iyzico hatası: {e}")

                # iyzico başarısız → DB'ye dokunma, kullanıcıya hata ver
                if not iyzico_ok:
                    raise HTTPException(
                        502,
                        f"Abonelik iyzico tarafında iptal edilemedi: {iyzico_error}. "
                        f"Lütfen destek ile iletişime geçin."
                    )

            # ── iyzico OK veya sub_ref yoksa DB güncelle ─────────
            if immediate:
                await conn.execute("""
                    UPDATE user_subscriptions
                    SET status='cancelled', cancelled_at=NOW(),
                        cancel_at_period_end=FALSE, updated_at=NOW()
                    WHERE user_id=$1 AND status IN ('active','trialing')
                """, target_user_id)
                await conn.execute("""
                    UPDATE users SET is_premium=FALSE, subscription_active=FALSE
                    WHERE id=$1
                """, target_user_id)
                message = "Abonelik iptal edildi."
            else:
                await conn.execute("""
                    UPDATE user_subscriptions
                    SET cancel_at_period_end=TRUE, updated_at=NOW()
                    WHERE user_id=$1 AND status IN ('active','trialing')
                """, target_user_id)
                period_str = period_end.strftime("%d.%m.%Y") if period_end else ""
                message = f"Abonelik {period_str} tarihinde sona erecek."

            await log_event("subscription_cancel", target_user_id,
                           {"immediate": immediate, "iyzico_ok": iyzico_ok},
                           request.client.host)

            user_row = await conn.fetchrow(
                "SELECT email, name FROM users WHERE id = $1", target_user_id
            )

        # Async email — event loop bloklanmaz
        if user_row and SMTP_SERVER and SMTP_USER:
            first_name = (user_row["name"] or "Kullanici").split()[0]
            html, plain = _build_cancel_email(first_name, period_end, immediate)
            asyncio.create_task(
                _send_email_async(user_row["email"], "ONE-BUNE Premium abonelik iptali", html, plain)
            )

        return {"success": True, "message": message, "immediate": immediate}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CANCEL ERROR] {e}")
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)