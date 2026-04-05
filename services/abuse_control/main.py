import os
import datetime
import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Skylight Bot Abuse Control")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
)

print(f"[ABUSE CONTROL] REDIS_URL={REDIS_URL}")

try:
    redis_client.ping()
    print("[ABUSE CONTROL] Redis connection OK")
except Exception as e:
    print(f"[ABUSE CONTROL] Redis connection FAILED: {e}")


class OTPRequestCheck(BaseModel):
    email: str
    ip_address: str


class OTPVerifyCheck(BaseModel):
    email: str
    ip_address: str


class OTPVerifyFail(BaseModel):
    email: str
    ip_address: str


class ChatCheck(BaseModel):
    user_id: str
    ip_address: str


def now_utc() -> datetime.datetime:
    return datetime.datetime.utcnow()


@app.get("/health")
def health():
    try:
        redis_client.ping()
        return {"status": "ok", "redis": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"redis error: {e}")


# =====================================================
# OTP REQUEST LIMITS
# =====================================================
@app.post("/otp/request/check")
def otp_request_check(body: OTPRequestCheck):
    email = body.email.strip().lower()
    ip_address = body.ip_address.strip()

    # Aynı email için 60 saniye cooldown
    cooldown_key = f"otp:cooldown:email:{email}"
    if redis_client.exists(cooldown_key):
        raise HTTPException(
            status_code=429,
            detail="Bu e-posta için çok sık kod istendi. Lütfen 60 saniye bekleyin."
        )

    # Aynı IP için saatlik limit
    hour_key = f"otp:req:ip:{ip_address}:{now_utc().strftime('%Y-%m-%d-%H')}"
    ip_count = redis_client.incr(hour_key)
    if ip_count == 1:
        redis_client.expire(hour_key, 3600)

    if ip_count > 10:
        raise HTTPException(
            status_code=429,
            detail="Bu IP adresinden çok fazla kod istendi. Lütfen daha sonra tekrar deneyin."
        )

    # Aynı email için günlük limit
    day_key = f"otp:req:email:{email}:{now_utc().strftime('%Y-%m-%d')}"
    email_day_count = redis_client.incr(day_key)
    if email_day_count == 1:
        redis_client.expire(day_key, 86400)

    if email_day_count > 20:
        raise HTTPException(
            status_code=429,
            detail="Bu e-posta için günlük kod limiti aşıldı."
        )

    return {
        "status": "ok",
        "ip_hour_count": ip_count,
        "email_day_count": email_day_count
    }


@app.post("/otp/request/mark-sent")
def otp_request_mark_sent(body: OTPRequestCheck):
    email = body.email.strip().lower()
    cooldown_key = f"otp:cooldown:email:{email}"
    redis_client.setex(cooldown_key, 60, "1")
    return {"status": "ok"}


# =====================================================
# OTP VERIFY LIMITS
# =====================================================
@app.post("/otp/verify/check")
def otp_verify_check(body: OTPVerifyCheck):
    email = body.email.strip().lower()
    ip_address = body.ip_address.strip()

    block_key = f"otp:verify:block:{email}:{ip_address}"
    if redis_client.exists(block_key):
        raise HTTPException(
            status_code=429,
            detail="Çok fazla hatalı doğrulama denemesi. Lütfen 15 dakika sonra tekrar deneyin."
        )

    return {"status": "ok"}


@app.post("/otp/verify/mark-failed")
def otp_verify_mark_failed(body: OTPVerifyFail):
    email = body.email.strip().lower()
    ip_address = body.ip_address.strip()

    fail_key = f"otp:verify:fail:{email}:{ip_address}"
    fail_count = redis_client.incr(fail_key)

    if fail_count == 1:
        redis_client.expire(fail_key, 900)  # 15 dk

    if fail_count >= 5:
        block_key = f"otp:verify:block:{email}:{ip_address}"
        redis_client.setex(block_key, 900, "1")  # 15 dk blok

    return {"status": "ok", "fail_count": fail_count}


@app.post("/otp/verify/clear")
def otp_verify_clear(body: OTPVerifyCheck):
    email = body.email.strip().lower()
    ip_address = body.ip_address.strip()

    fail_key = f"otp:verify:fail:{email}:{ip_address}"
    block_key = f"otp:verify:block:{email}:{ip_address}"

    redis_client.delete(fail_key, block_key)
    return {"status": "ok"}


# =====================================================
# CHAT RATE LIMITS
# =====================================================
@app.post("/chat/check")
def chat_check(body: ChatCheck):
    user_id = str(body.user_id).strip() or "guest"
    ip_address = body.ip_address.strip()

    # User bazlı: 10 saniyede 10 istek
    user_key = f"chat:user:{user_id}"
    user_count = redis_client.incr(user_key)
    if user_count == 1:
        redis_client.expire(user_key, 10)

    if user_count > 10:
        raise HTTPException(
            status_code=429,
            detail="Çok hızlı mesaj gönderiyorsunuz. Lütfen birkaç saniye bekleyin."
        )

    # IP bazlı: 10 saniyede 20 istek
    ip_key = f"chat:ip:{ip_address}"
    ip_count = redis_client.incr(ip_key)
    if ip_count == 1:
        redis_client.expire(ip_key, 10)

    if ip_count > 20:
        raise HTTPException(
            status_code=429,
            detail="Bu IP adresinden çok fazla istek gönderildi."
        )

    return {
        "status": "ok",
        "user_requests": user_count,
        "ip_requests": ip_count
    }