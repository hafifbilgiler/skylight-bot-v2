import os
import json
import time
import base64
import hashlib
import hmac
import secrets
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Body
from fastapi.responses import JSONResponse


app = FastAPI(
    title="ONE-BUNE Payment Service",
    version="1.0.0",
)


IYZICO_API_KEY = os.getenv("IYZICO_API_KEY", "").strip()
IYZICO_SECRET_KEY = os.getenv("IYZICO_SECRET_KEY", "").strip()
IYZICO_BASE_URL = os.getenv("IYZICO_BASE_URL", "https://sandbox-api.iyzipay.com").strip()

PAYMENT_CALLBACK_URL = os.getenv(
    "PAYMENT_CALLBACK_URL",
    "https://one-bune.com/payment/iyzico-callback.php",
).strip()

APP_PUBLIC_URL = os.getenv(
    "APP_PUBLIC_URL",
    "https://one-bune.com",
).strip()

IYZICO_MONTHLY_PLAN_CODE = os.getenv("IYZICO_MONTHLY_PLAN_CODE", "").strip()
IYZICO_YEARLY_PLAN_CODE = os.getenv("IYZICO_YEARLY_PLAN_CODE", "").strip()

REQUEST_TIMEOUT = float(os.getenv("PAYMENT_HTTP_TIMEOUT", "45"))


def ensure_config() -> None:
    missing = []
    if not IYZICO_API_KEY:
        missing.append("IYZICO_API_KEY")
    if not IYZICO_SECRET_KEY:
        missing.append("IYZICO_SECRET_KEY")
    if not IYZICO_MONTHLY_PLAN_CODE:
        missing.append("IYZICO_MONTHLY_PLAN_CODE")
    if not IYZICO_YEARLY_PLAN_CODE:
        missing.append("IYZICO_YEARLY_PLAN_CODE")

    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Payment service config missing: {', '.join(missing)}",
        )


def build_auth(uri: str, body: str = "") -> tuple[str, str]:
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


async def iyzico_post(uri: str, payload: dict) -> dict:
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


async def iyzico_get(uri: str) -> dict:
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


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "service": "payment-service",
        "iyzico_base_url": IYZICO_BASE_URL,
        "has_api_key": bool(IYZICO_API_KEY),
        "has_secret_key": bool(IYZICO_SECRET_KEY),
        "has_monthly_plan": bool(IYZICO_MONTHLY_PLAN_CODE),
        "has_yearly_plan": bool(IYZICO_YEARLY_PLAN_CODE),
    }


@app.post("/payment/checkout")
async def payment_checkout(
    body: dict = Body(...),
    authorization: Optional[str] = Header(None),
):
    ensure_config()

    if not authorization:
        raise HTTPException(status_code=401, detail="Login required")

    billing_period = (body.get("billing_period") or "monthly").strip().lower()
    if billing_period not in {"monthly", "yearly"}:
        raise HTTPException(status_code=400, detail="Invalid billing_period")

    pricing_plan_reference_code = (
        IYZICO_MONTHLY_PLAN_CODE
        if billing_period == "monthly"
        else IYZICO_YEARLY_PLAN_CODE
    )

    # Şimdilik gateway/user bilgisi gelmese bile servis çalışsın diye fallback bırakıldı.
    customer_email = body.get("email") or "customer@one-bune.com"
    customer_name = body.get("name") or "ONE"
    customer_surname = body.get("surname") or "BUNE"
    customer_gsm = body.get("gsmNumber") or "+905000000000"
    customer_identity = body.get("identityNumber") or "11111111111"

    conversation_id = f"checkout-{int(time.time())}"

    payload = {
        "locale": "tr",
        "conversationId": conversation_id,
        "callbackUrl": PAYMENT_CALLBACK_URL,
        "pricingPlanReferenceCode": pricing_plan_reference_code,
        "subscriptionInitialStatus": "ACTIVE",
        "customer": {
            "name": customer_name,
            "surname": customer_surname,
            "email": customer_email,
            "gsmNumber": customer_gsm,
            "identityNumber": customer_identity,
        },
    }

    data = await iyzico_post("/v2/subscription/checkoutform/initialize", payload)

    if data.get("status") != "success":
        raise HTTPException(status_code=400, detail=data)

    return {
        "status": "success",
        "checkoutFormContent": data.get("checkoutFormContent"),
        "token": data.get("token"),
        "conversationId": conversation_id,
    }


@app.get("/payment/callback")
async def payment_callback(token: str):
    ensure_config()

    if not token:
        raise HTTPException(status_code=400, detail="Missing token")

    data = await iyzico_get(f"/v2/subscription/checkoutform/{token}")

    if data.get("status") != "success":
        raise HTTPException(status_code=400, detail=data)

    return {
        "status": "success",
        "payment_status": data,
        "redirect": f"{APP_PUBLIC_URL}/?premium=1",
    }