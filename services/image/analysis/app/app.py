"""
═══════════════════════════════════════════════════════════════
SKYLIGHT IMAGE ANALYSIS SERVICE v2.0 - PRODUCTION
═══════════════════════════════════════════════════════════════
✨ NEW FEATURES:
- Claude AI-level context awareness
- Conversation continuity (remember previous analyses)
- Mode-specific analysis (assistant vs code)
- Professional prompts for different scenarios
- Redis conversation cache
- Multi-turn image discussions
═══════════════════════════════════════════════════════════════
"""

import os
import logging
import json
import base64
from typing import Optional, List, Dict
from datetime import datetime

import requests
import redis
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Import production prompts
from prompts.production import (
    VISION_ASSISTANT_SYSTEM_PROMPT,
    VISION_CODE_SYSTEM_PROMPT,
    VISION_FOLLOWUP_SYSTEM_PROMPT,
)

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# VISION PROVIDER — gemini (default) | deepinfra (fallback)
# ═══════════════════════════════════════════════════════════════
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "gemini").strip().lower()

# Gemini (Vertex AI) Configuration
GEMINI_PROJECT     = os.getenv("GEMINI_PROJECT", "gen-lang-client-0907571701")
GEMINI_LOCATION    = os.getenv("GEMINI_LOCATION", "us-central1")
GEMINI_MODEL       = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash-lite")
GEMINI_SA_KEY_PATH = os.getenv("GEMINI_SA_KEY_PATH", "/etc/vertex-sa/key.json")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")  # AI Studio fallback
GEMINI_MAX_TOKENS  = int(os.getenv("GEMINI_VISION_MAX_TOKENS", "4096"))
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_VISION_TEMPERATURE", "0.4"))

# DeepInfra Configuration (fallback)
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
DEEPINFRA_VISION_MODEL = os.getenv("DEEPINFRA_VISION_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct")
DEEPINFRA_VISION_MAX_TOKENS = int(os.getenv("DEEPINFRA_VISION_MAX_TOKENS", "4096"))
DEEPINFRA_VISION_TEMPERATURE = float(os.getenv("DEEPINFRA_VISION_TEMPERATURE", "0.4"))

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://skylight-redis:6379/0")
REDIS_TTL = int(os.getenv("REDIS_TTL", "86400"))  # 24 hours

# Feature Flags
ENABLE_REDIS_CACHE = True

# Initialize Redis
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    logger.info("✓ Redis connected")
except Exception as e:
    logger.warning(f"⚠️ Redis unavailable: {e}")
    ENABLE_REDIS_CACHE = False
    redis_client = None

if VISION_PROVIDER == "gemini":
    _has_sa = os.path.exists(GEMINI_SA_KEY_PATH)
    if not (_has_sa or GEMINI_API_KEY):
        logger.error("⚠️ GEMINI provider seçildi ama Vertex SA key veya GEMINI_API_KEY yok!")
    else:
        logger.info(f"✓ Vision provider: GEMINI ({GEMINI_MODEL}, {'Vertex SA' if _has_sa else 'AI Studio'})")
elif VISION_PROVIDER == "deepinfra":
    if not DEEPINFRA_API_KEY:
        logger.error("⚠️ DEEPINFRA provider seçildi ama DEEPINFRA_API_KEY yok!")
    else:
        logger.info(f"✓ Vision provider: DEEPINFRA ({DEEPINFRA_VISION_MODEL})")
else:
    logger.error(f"⚠️ Bilinmeyen VISION_PROVIDER: {VISION_PROVIDER}")

# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Skylight Image Analysis Service",
    version="2.0.0",
    description="Production-grade context-aware image analysis"
)

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class HistoryMessage(BaseModel):
    role: str
    content: str
    metadata: Optional[Dict] = None

class AnalyzeRequest(BaseModel):
    image_data: str  # base64
    image_type: str = "image/png"
    prompt: str
    user_id: str
    conversation_id: Optional[str] = None
    mode: str = "assistant"  # "assistant" or "code"
    history: Optional[List[HistoryMessage]] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    preserve_analysis: bool = True  # Store in Redis for follow-ups

# ═══════════════════════════════════════════════════════════════
# CONTEXT MANAGER
# ═══════════════════════════════════════════════════════════════

class VisionContextManager:
    """Manage vision analysis context across conversation"""
    
    @staticmethod
    def extract_previous_vision_analysis(history: List[HistoryMessage]) -> Optional[str]:
        """Get most recent vision analysis from this conversation"""
        if not history:
            return None
        
        for msg in reversed(history):
            if msg.role == "assistant" and msg.metadata:
                if msg.metadata.get("type") == "vision_analysis":
                    return msg.content[:1500]  # Limit context size
        
        return None
    
    @staticmethod
    def detect_followup_question(prompt: str, has_history: bool) -> bool:
        """Detect if this is a follow-up question about the same image"""
        followup_indicators = [
            "bu", "bunu", "bunun", "bu görselde", "bu resimde",
            "şu", "şunu", "this", "that", "the image", "the picture",
            "daha fazla", "detay", "açıkla", "explain more", "tell me more",
            "ne demek", "nedir", "what is", "what does", "how"
        ]
        
        prompt_lower = prompt.lower()
        return has_history and any(ind in prompt_lower for ind in followup_indicators)
    
    @staticmethod
    def get_cached_analysis(conversation_id: str) -> Optional[str]:
        """Retrieve cached vision analysis from Redis"""
        if not ENABLE_REDIS_CACHE or not redis_client:
            return None
        try:
            key = f"vision:analysis:{conversation_id}"
            cached = redis_client.get(key)
            if cached:
                logger.info(f"[REDIS] Retrieved cached analysis for {conversation_id}")
                return cached
        except Exception as e:
            logger.warning(f"[REDIS] Retrieval failed: {e}")
        return None
    
    @staticmethod
    def cache_analysis(conversation_id: str, analysis: str):
        """Cache vision analysis in Redis"""
        if not ENABLE_REDIS_CACHE or not redis_client:
            return
        try:
            key = f"vision:analysis:{conversation_id}"
            redis_client.setex(key, REDIS_TTL, analysis)
            logger.info(f"[REDIS] Cached analysis for {conversation_id}")
        except Exception as e:
            logger.warning(f"[REDIS] Cache failed: {e}")


# ═══════════════════════════════════════════════════════════════
# PROMPT BUILDER
# ═══════════════════════════════════════════════════════════════

def build_vision_messages(
    image_data: str,
    image_type: str,
    prompt: str,
    mode: str,
    history: Optional[List[HistoryMessage]] = None,
    conversation_id: Optional[str] = None
) -> List[Dict]:
    """
    Build messages for vision API with context awareness
    """
    messages = []
    
    # Detect if this is a follow-up
    is_followup = VisionContextManager.detect_followup_question(
        prompt,
        has_history=bool(history)
    )
    
    # Get previous analysis (from history or cache)
    previous_analysis = None
    if is_followup:
        previous_analysis = VisionContextManager.extract_previous_vision_analysis(history or [])
        if not previous_analysis and conversation_id:
            previous_analysis = VisionContextManager.get_cached_analysis(conversation_id)
    
    # Select system prompt
    if is_followup and previous_analysis:
        system_prompt = VISION_FOLLOWUP_SYSTEM_PROMPT
        system_prompt += f"\n\nPREVIOUS ANALYSIS:\n{previous_analysis}\n"
        logger.info("[CONTEXT] Follow-up question detected, using previous analysis")
    elif mode == "code":
        system_prompt = VISION_CODE_SYSTEM_PROMPT
    else:
        system_prompt = VISION_ASSISTANT_SYSTEM_PROMPT
    
    messages.append({
        "role": "system",
        "content": system_prompt
    })
    
    # Add conversation history (if not follow-up - avoid duplication)
    if history and not is_followup:
        for msg in history[-5:]:  # Last 5 messages
            if msg.role in ["user", "assistant"]:
                messages.append({
                    "role": msg.role,
                    "content": msg.content
                })
    
    # Current user message with image
    user_content = []
    
    # Add image only if NOT a follow-up (reusing same image)
    if not is_followup:
        user_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{image_type};base64,{image_data}"
            }
        })
    
    user_content.append({
        "type": "text",
        "text": prompt
    })
    
    messages.append({
        "role": "user",
        "content": user_content
    })
    
    return messages


# ═══════════════════════════════════════════════════════════════
# VISION API STREAMING
# ═══════════════════════════════════════════════════════════════

def _stream_gemini_vision(
    messages: List[Dict],
    max_tokens: int,
    temperature: float,
):
    """
    Vertex AI Gemini Flash Lite ile multimodal vision stream.
    OpenAI formatındaki messages'ı Gemini formatına çevirir.
    """
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        logger.error("[GEMINI] google-genai paketi yok!")
        yield "⚠️ Gemini SDK eksik (google-genai)"
        return

    # Vertex AI client
    try:
        if os.path.exists(GEMINI_SA_KEY_PATH) and GEMINI_PROJECT:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GEMINI_SA_KEY_PATH
            client = genai.Client(
                vertexai=True,
                project=GEMINI_PROJECT,
                location=GEMINI_LOCATION,
            )
            logger.info(f"[GEMINI] Vertex AI ({GEMINI_PROJECT}/{GEMINI_LOCATION})")
        elif GEMINI_API_KEY:
            client = genai.Client(api_key=GEMINI_API_KEY)
            logger.info("[GEMINI] AI Studio (API key)")
        else:
            yield "⚠️ Gemini için Vertex SA key veya GEMINI_API_KEY gerekli."
            return
    except Exception as e:
        logger.error(f"[GEMINI] Client init hatası: {e}")
        yield f"⚠️ Gemini istemcisi başlatılamadı: {e}"
        return

    # OpenAI messages → Gemini contents
    system_text = ""
    contents = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            # System tek string olarak biriktir
            if isinstance(content, str):
                system_text += (("\n\n" if system_text else "") + content)
            continue
        # role: user / assistant → user / model
        gem_role = "user" if role == "user" else "model"
        parts = []
        if isinstance(content, str):
            parts.append(gtypes.Part.from_text(text=content))
        elif isinstance(content, list):
            for item in content:
                itype = item.get("type")
                if itype == "text":
                    parts.append(gtypes.Part.from_text(text=item.get("text", "")))
                elif itype == "image_url":
                    url = item.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        # data:image/png;base64,XXX
                        try:
                            header, b64 = url.split(",", 1)
                            mime = header.split(";")[0].replace("data:", "") or "image/jpeg"
                            img_bytes = base64.b64decode(b64)
                            parts.append(gtypes.Part.from_bytes(data=img_bytes, mime_type=mime))
                        except Exception as e:
                            logger.error(f"[GEMINI] Image parse hatası: {e}")
        if parts:
            contents.append(gtypes.Content(role=gem_role, parts=parts))

    # Streaming çağrı
    try:
        config = gtypes.GenerateContentConfig(
            system_instruction=system_text or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        logger.info(f"[GEMINI] ▶ Vision stream | model={GEMINI_MODEL} | parts={sum(len(c.parts) for c in contents)}")
        stream = client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )
        for chunk in stream:
            text = getattr(chunk, "text", None)
            if text:
                yield text
    except Exception as e:
        logger.error(f"[GEMINI] Stream hatası: {e}")
        yield f"⚠️ Gemini vision stream hatası: {e}"


def _stream_deepinfra_vision(
    messages: List[Dict],
    max_tokens: int,
    temperature: float,
):
    """DeepInfra Qwen-VL ile vision stream (fallback)."""
    headers = {
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPINFRA_VISION_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True
    }
    logger.info(f"[VISION-DI] Stream | model={DEEPINFRA_VISION_MODEL}")
    response = requests.post(
        f"{DEEPINFRA_BASE_URL}/chat/completions",
        headers=headers, json=payload, stream=True, timeout=120
    )
    if response.status_code != 200:
        yield f"⚠️ Vision API error (HTTP {response.status_code})"
        return
    for line in response.iter_lines():
        if not line: continue
        line_str = line.decode('utf-8')
        if not line_str.strip() or "[DONE]" in line_str:
            continue
        if line_str.startswith("data: "):
            line_str = line_str[6:]
        try:
            chunk_data = json.loads(line_str)
            if "choices" in chunk_data and chunk_data["choices"]:
                content = chunk_data["choices"][0].get("delta", {}).get("content", "")
                if content:
                    yield content
        except json.JSONDecodeError:
            continue


def stream_vision_analysis(
    messages: List[Dict],
    max_tokens: int,
    temperature: float,
    conversation_id: Optional[str] = None,
    preserve_analysis: bool = True
):
    """
    Stream vision analysis — provider'a göre yönlendirir.
    VISION_PROVIDER=gemini (default) veya deepinfra
    """
    full_response = []
    try:
        if VISION_PROVIDER == "gemini":
            stream = _stream_gemini_vision(messages, max_tokens, temperature)
        else:
            stream = _stream_deepinfra_vision(messages, max_tokens, temperature)

        for chunk in stream:
            if chunk:
                full_response.append(chunk)
                yield chunk

        # Cache the full response
        if preserve_analysis and conversation_id and full_response:
            complete_analysis = "".join(full_response)
            VisionContextManager.cache_analysis(conversation_id, complete_analysis)

    except requests.exceptions.Timeout:
        logger.error("[VISION] Timeout")
        yield "⚠️ Vision analysis timeout"
    except Exception as e:
        logger.error(f"[VISION] Exception: {e}")
        yield f"⚠️ Vision analysis error: {str(e)}"


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """Health check"""
    active_model = GEMINI_MODEL if VISION_PROVIDER == "gemini" else DEEPINFRA_VISION_MODEL
    return {
        "status": "healthy",
        "service": "skylight-image-analysis",
        "version": "3.0.0",
        "provider": VISION_PROVIDER,
        "model": active_model,
        "features": {
            "context_aware": True,
            "follow_up_questions": True,
            "conversation_memory": ENABLE_REDIS_CACHE,
            "mode_specific_analysis": True,
            "multi_provider": True,
        }
    }


@app.post("/analyze")
async def analyze_endpoint(request: AnalyzeRequest):
    """
    Analyze image with Claude AI-level context awareness
    
    Features:
    - Mode-specific analysis (assistant vs code)
    - Follow-up question support
    - Conversation memory via Redis
    - Context continuity
    """
    try:
        logger.info(f"[ANALYZE] User {request.user_id}, Mode: {request.mode}")
        
        # Build context-aware messages
        messages = build_vision_messages(
            image_data=request.image_data,
            image_type=request.image_type,
            prompt=request.prompt,
            mode=request.mode,
            history=request.history,
            conversation_id=request.conversation_id
        )
        
        # Get parameters
        if VISION_PROVIDER == "gemini":
            max_tokens = request.max_tokens or GEMINI_MAX_TOKENS
            temperature = request.temperature or GEMINI_TEMPERATURE
        else:
            max_tokens = request.max_tokens or DEEPINFRA_VISION_MAX_TOKENS
            temperature = request.temperature or DEEPINFRA_VISION_TEMPERATURE
        
        # Stream response
        return StreamingResponse(
            stream_vision_analysis(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                conversation_id=request.conversation_id,
                preserve_analysis=request.preserve_analysis
            ),
            media_type="text/plain; charset=utf-8"
        )
    
    except Exception as e:
        logger.error(f"[ANALYZE] Error: {e}")
        return StreamingResponse(
            iter([f"⚠️ Internal error: {str(e)}"]),
            media_type="text/plain; charset=utf-8"
        )


# ═══════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    active_model = GEMINI_MODEL if VISION_PROVIDER == "gemini" else DEEPINFRA_VISION_MODEL
    active_mt    = GEMINI_MAX_TOKENS if VISION_PROVIDER == "gemini" else DEEPINFRA_VISION_MAX_TOKENS
    active_tmp   = GEMINI_TEMPERATURE if VISION_PROVIDER == "gemini" else DEEPINFRA_VISION_TEMPERATURE
    logger.info("=" * 60)
    logger.info("🔍 SKYLIGHT IMAGE ANALYSIS SERVICE v3.0")
    logger.info(f"   Provider: {VISION_PROVIDER.upper()}")
    logger.info(f"   Model: {active_model}")
    logger.info(f"   Max Tokens: {active_mt}")
    logger.info(f"   Temperature: {active_tmp}")
    logger.info(f"   Redis Cache: {'✓' if ENABLE_REDIS_CACHE else '✗'}")
    logger.info("   Features:")
    logger.info("     ✓ Hybrid: Gemini Flash Lite (default) + DeepInfra fallback")
    logger.info("     ✓ Context-Aware Analysis")
    logger.info("     ✓ Follow-Up Questions")
    logger.info("     ✓ Conversation Memory")
    logger.info("     ✓ Mode-Specific Prompts")
    logger.info("=" * 60)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)