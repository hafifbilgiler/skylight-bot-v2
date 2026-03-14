"""
═══════════════════════════════════════════════════════════════
SKYLIGHT IMAGE GENERATION SERVICE v3.0 - PRODUCTION
═══════════════════════════════════════════════════════════════
✨ NEW FEATURES:
- Claude AI-level context awareness
- Multi-modal conversation memory (text + vision + images)
- Professional prompt engineering
- Redis conversation cache
- Context synthesis across modalities
- Iterative editing support
- Vision → Image → Refine cycles
═══════════════════════════════════════════════════════════════
"""

import os
import base64
import logging
import hashlib
import time
import json
from typing import Optional, List, Dict, Tuple
from datetime import datetime

import psycopg2
import requests
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Import production prompts
from prompts.production import (
    VISION_TO_IMAGE_SYSTEM_PROMPT,
    TEXT_TO_IMAGE_SYSTEM_PROMPT,
    ITERATIVE_EDIT_SYSTEM_PROMPT,
)

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# DeepInfra Configuration
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
DEEPINFRA_IMAGE_MODEL = os.getenv("DEEPINFRA_IMAGE_MODEL", "black-forest-labs/FLUX-2-max")
DEEPINFRA_IMAGE_SIZE = os.getenv("DEEPINFRA_IMAGE_SIZE", "1024x1024")
DEEPINFRA_PROMPT_MODEL = os.getenv("DEEPINFRA_PROMPT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8")

# PostgreSQL Configuration
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://skylight-redis:6379/0")
REDIS_TTL = int(os.getenv("REDIS_TTL", "86400"))  # 24 hours

# Feature Flags
ENABLE_DB_STORAGE = bool(DB_NAME and DB_USER and DB_PASSWORD)
ENABLE_REDIS_CACHE = True

# Initialize Redis
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=False)
    redis_client.ping()
    logger.info("✓ Redis connected")
except Exception as e:
    logger.warning(f"⚠️ Redis unavailable: {e}")
    ENABLE_REDIS_CACHE = False
    redis_client = None

if not DEEPINFRA_API_KEY:
    logger.error("⚠️ DEEPINFRA_API_KEY is not set!")

logger.info(f"✓ DB Storage: {'Enabled' if ENABLE_DB_STORAGE else 'Disabled'}")
logger.info(f"✓ Redis Cache: {'Enabled' if ENABLE_REDIS_CACHE else 'Disabled'}")

# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Skylight Image Generation Service",
    version="3.0.0",
    description="Production-grade context-aware image generation"
)

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class ConversationMessage(BaseModel):
    role: str
    content: str
    metadata: Optional[Dict] = None

class GenerateRequest(BaseModel):
    prompt: str
    user_id: str
    conversation_id: Optional[str] = None
    conversation_history: Optional[List[ConversationMessage]] = None
    mode: str = "assistant"  # "assistant" or "code"
    style: Optional[str] = None
    size: str = "1024x1024"
    save_to_db: bool = True
    context_override: Optional[str] = None  # Manual context injection

class GenerateResponse(BaseModel):
    success: bool
    image_b64: Optional[str] = None
    image_url: Optional[str] = None
    enhanced_prompt: Optional[str] = None
    context_summary: Optional[Dict] = None
    saved_to_db: bool = False
    error: Optional[str] = None

# ═══════════════════════════════════════════════════════════════
# CONTEXT MANAGER - Claude AI Style
# ═══════════════════════════════════════════════════════════════

class ContextManager:
    """
    Manages multi-modal conversation context
    Similar to Claude AI's context awareness
    """
    
    @staticmethod
    def extract_vision_context(history: List[ConversationMessage]) -> Optional[Dict]:
        """Extract most recent vision analysis from conversation"""
        if not history:
            return None
        
        for msg in reversed(history):
            if msg.role == "assistant" and msg.metadata:
                if msg.metadata.get("type") == "vision_analysis":
                    return {
                        "content": msg.content[:2000],  # Limit context
                        "timestamp": msg.metadata.get("timestamp"),
                        "confidence": "high"
                    }
                # Legacy format support
                if "[VISION_ANALYSIS]" in msg.content:
                    import re
                    match = re.search(r'\[VISION_ANALYSIS\](.*?)\[/VISION_ANALYSIS\]',
                                    msg.content, re.DOTALL)
                    if match:
                        return {
                            "content": match.group(1).strip()[:2000],
                            "timestamp": None,
                            "confidence": "medium"
                        }
        return None
    
    @staticmethod
    def extract_previous_images(history: List[ConversationMessage]) -> List[Dict]:
        """Extract previously generated images in this conversation"""
        images = []
        for msg in history:
            if msg.role == "assistant" and msg.metadata:
                if msg.metadata.get("type") == "generated_image":
                    images.append({
                        "prompt": msg.metadata.get("prompt", ""),
                        "timestamp": msg.metadata.get("timestamp"),
                    })
        return images[-3:]  # Last 3 images
    
    @staticmethod
    def extract_text_context(history: List[ConversationMessage], max_messages: int = 5) -> str:
        """Extract relevant text context from recent conversation"""
        relevant_messages = []
        
        for msg in history[-(max_messages * 2):]:
            if msg.role == "user":
                # Skip image generation requests
                if not any(kw in msg.content.lower() for kw in 
                          ["görsel", "resim", "image", "picture", "çiz", "draw"]):
                    relevant_messages.append(msg.content)
        
        return " | ".join(relevant_messages[-max_messages:]) if relevant_messages else ""
    
    @staticmethod
    def detect_request_type(prompt: str, history: List[ConversationMessage]) -> str:
        """
        Detect what type of image generation request this is
        
        Returns: "vision_to_image" | "iterative_edit" | "text_to_image" | "vague_with_context"
        """
        prompt_lower = prompt.lower()
        
        # Check for vision context
        vision_ctx = ContextManager.extract_vision_context(history)
        if vision_ctx:
            # Vague prompts with vision = vision_to_image
            vague_vision_triggers = [
                "buna görsel", "bunun görsel", "bundan görsel", "görseli yap",
                "bunu görsele", "şunun görsel", "bu analiz", "bunun üzerinden"
            ]
            if any(trigger in prompt_lower for trigger in vague_vision_triggers):
                return "vision_to_image"
        
        # Check for iterative editing
        prev_images = ContextManager.extract_previous_images(history)
        if prev_images:
            edit_triggers = [
                "düzelt", "değiştir", "iyileştir", "revize", "fix", "improve",
                "daha", "less", "more", "ekle", "çıkar", "add", "remove",
                "rengini", "boyutunu", "stilini", "change", "modify"
            ]
            if any(trigger in prompt_lower for trigger in edit_triggers):
                return "iterative_edit"
        
        # Check for vague prompt with text context
        vague_triggers = [
            "görsel yap", "resim yap", "görsel oluştur", "çiz",
            "bunun hakkında", "bunla ilgili", "şununla ilgili"
        ]
        if any(trigger in prompt_lower for trigger in vague_triggers):
            text_ctx = ContextManager.extract_text_context(history)
            if text_ctx:
                return "vague_with_context"
        
        # Default: text to image
        return "text_to_image"
    
    @staticmethod
    def build_context_summary(
        request_type: str,
        vision_ctx: Optional[Dict],
        text_ctx: str,
        prev_images: List[Dict]
    ) -> Dict:
        """Build comprehensive context summary"""
        return {
            "request_type": request_type,
            "has_vision_context": bool(vision_ctx),
            "has_text_context": bool(text_ctx),
            "previous_images_count": len(prev_images),
            "vision_confidence": vision_ctx.get("confidence") if vision_ctx else None,
            "context_sources": [
                source for source, present in {
                    "vision_analysis": bool(vision_ctx),
                    "text_conversation": bool(text_ctx),
                    "previous_images": bool(prev_images),
                }.items() if present
            ]
        }

# ═══════════════════════════════════════════════════════════════
# PROMPT ENGINEERING - Production Grade
# ═══════════════════════════════════════════════════════════════

def enhance_prompt_with_llm(
    user_prompt: str,
    request_type: str,
    vision_ctx: Optional[Dict],
    text_ctx: str,
    prev_images: List[Dict],
    mode: str = "assistant"
) -> str:
    """
    Use LLM to enhance prompt with context awareness
    Claude AI-level prompt engineering
    """
    try:
        # Select appropriate system prompt
        if request_type == "vision_to_image":
            system_prompt = VISION_TO_IMAGE_SYSTEM_PROMPT
            context_injection = f"\n\nVISION ANALYSIS:\n{vision_ctx['content']}\n"
        
        elif request_type == "iterative_edit":
            system_prompt = ITERATIVE_EDIT_SYSTEM_PROMPT
            prev_prompts = [img["prompt"] for img in prev_images if img.get("prompt")]
            context_injection = f"\n\nPREVIOUS IMAGES:\n" + "\n".join(
                f"- {p}" for p in prev_prompts
            ) + "\n"
        
        elif request_type == "vague_with_context":
            system_prompt = TEXT_TO_IMAGE_SYSTEM_PROMPT
            context_injection = f"\n\nCONVERSATION CONTEXT:\n{text_ctx}\n"
        
        else:
            system_prompt = TEXT_TO_IMAGE_SYSTEM_PROMPT
            context_injection = ""
        
        # Add mode-specific guidance
        if mode == "code":
            system_prompt += "\n\nMODE: Code/Technical. Focus on: UI mockups, diagrams, technical illustrations, clean design, professional look."
        
        headers = {
            "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": DEEPINFRA_PROMPT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt + context_injection},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.4,
            "max_tokens": 300
        }
        
        logger.info(f"[PROMPT ENHANCEMENT] Type: {request_type}, Mode: {mode}")
        
        response = requests.post(
            f"{DEEPINFRA_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=20
        )
        
        if response.status_code == 200:
            data = response.json()
            enhanced = data["choices"][0]["message"]["content"].strip()
            
            logger.info(f"[PROMPT] Original: {user_prompt[:60]}...")
            logger.info(f"[PROMPT] Enhanced: {enhanced[:80]}...")
            
            return enhanced
        else:
            logger.warning(f"[PROMPT] Enhancement failed (HTTP {response.status_code}), using original")
            return user_prompt
    
    except Exception as e:
        logger.error(f"[PROMPT] Error: {e}")
        return user_prompt


# ═══════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ═══════════════════════════════════════════════════════════════

def get_db_conn():
    """Get database connection"""
    if not ENABLE_DB_STORAGE:
        return None
    try:
        return psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            connect_timeout=5
        )
    except Exception as e:
        logger.error(f"[DB] Connection failed: {e}")
        return None


def save_generated_image(
    user_id: str,
    conversation_id: Optional[str],
    prompt_original: str,
    prompt_enhanced: str,
    image_url: str,
    image_b64: str,
    context_summary: Dict,
    runtime_ms: int = 0,
) -> Tuple[bool, Optional[str]]:
    """Save generated image with context metadata"""
    if not ENABLE_DB_STORAGE:
        return False, None
    
    conn = None
    cur = None
    try:
        image_hash = hashlib.sha256(image_b64.encode()).hexdigest()
        
        conn = get_db_conn()
        if not conn:
            return False, None
        
        cur = conn.cursor()
        
        # Deduplication
        cur.execute("""
            SELECT id FROM generated_images
            WHERE image_hash = %s AND is_deleted = FALSE
            LIMIT 1
        """, (image_hash,))
        existing = cur.fetchone()
        
        if existing:
            return True, str(existing[0])
        
        # Insert
        cur.execute("""
            INSERT INTO generated_images
                (user_id, conversation_id, prompt_turkish, prompt_english,
                 image_url, image_b64, image_hash, image_size_bytes,
                 model_used, generation_cost, generation_time_ms, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            int(user_id) if user_id.isdigit() else 0,
            conversation_id,
            prompt_original,
            prompt_enhanced,
            image_url,
            image_b64,
            image_hash,
            len(image_b64),
            DEEPINFRA_IMAGE_MODEL,
            0.07,
            runtime_ms,
            json.dumps(context_summary)  # Store context metadata
        ))
        
        image_id = str(cur.fetchone()[0])
        conn.commit()
        
        logger.info(f"[DB] ✓ Saved image {image_id}")
        return True, image_id
    
    except Exception as e:
        logger.error(f"[DB] Save error: {e}")
        if conn:
            conn.rollback()
        return False, None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ═══════════════════════════════════════════════════════════════
# IMAGE GENERATION
# ═══════════════════════════════════════════════════════════════

def download_image_as_base64(image_url: str, retries: int = 3) -> Optional[str]:
    """Download image with retry logic"""
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(image_url, timeout=30)
            if response.status_code == 200:
                b64_string = base64.b64encode(response.content).decode('utf-8')
                logger.info(f"[DOWNLOAD] ✓ {len(b64_string)} chars")
                return b64_string
            if attempt < retries:
                time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"[DOWNLOAD] Attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None


def generate_image_flux(prompt: str) -> Tuple[Optional[str], Optional[str], Optional[str], int]:
    """Generate image using FLUX-2"""
    try:
        headers = {
            "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
            "Content-Type": "application/json"
        }
        
        width, height = map(int, DEEPINFRA_IMAGE_SIZE.split("x"))
        payload = {"prompt": prompt, "width": width, "height": height}
        
        logger.info(f"[FLUX-2] Generating: {prompt[:100]}...")
        
        response = requests.post(
            f"https://api.deepinfra.com/v1/inference/{DEEPINFRA_IMAGE_MODEL}",
            headers=headers,
            json=payload,
            timeout=90
        )
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("status") != "ok":
                return None, None, f"⚠️ {data.get('error', 'Unknown error')}", 0
            
            image_url = data.get("image_url")
            if not image_url:
                return None, None, "⚠️ No image URL", 0
            
            runtime_ms = data.get("inference_status", {}).get("runtime_ms", 0)
            b64_image = download_image_as_base64(image_url, retries=3)
            
            if b64_image:
                logger.info(f"[FLUX-2] ✓ Generated ({runtime_ms}ms)")
                return b64_image, image_url, None, runtime_ms
            else:
                return None, image_url, "⚠️ Download failed", runtime_ms
        else:
            return None, None, f"⚠️ HTTP {response.status_code}", 0
    
    except requests.exceptions.Timeout:
        return None, None, "⚠️ Timeout (90s)", 0
    except Exception as e:
        logger.error(f"[FLUX-2] Exception: {e}")
        return None, None, f"⚠️ {str(e)}", 0


# ═══════════════════════════════════════════════════════════════
# REDIS CONVERSATION CACHE
# ═══════════════════════════════════════════════════════════════

def cache_generation_context(conversation_id: str, context_data: Dict):
    """Cache generation context in Redis"""
    if not ENABLE_REDIS_CACHE or not redis_client:
        return
    try:
        key = f"imggen:ctx:{conversation_id}"
        redis_client.setex(key, REDIS_TTL, json.dumps(context_data))
        logger.info(f"[REDIS] Cached context for {conversation_id}")
    except Exception as e:
        logger.warning(f"[REDIS] Cache failed: {e}")


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """Health check"""
    return {
        "status": "healthy",
        "service": "skylight-image-generation",
        "version": "3.0.0",
        "model": DEEPINFRA_IMAGE_MODEL,
        "prompt_model": DEEPINFRA_PROMPT_MODEL,
        "features": {
            "context_aware": True,
            "vision_to_image": True,
            "iterative_editing": True,
            "redis_cache": ENABLE_REDIS_CACHE,
            "db_storage": ENABLE_DB_STORAGE,
        }
    }


@app.post("/generate", response_model=GenerateResponse)
async def generate_endpoint(request: GenerateRequest):
    """
    Generate image with Claude AI-level context awareness
    
    Supports:
    - Vision → Image generation
    - Text → Image with conversation context
    - Iterative editing
    - Multi-modal context synthesis
    """
    try:
        logger.info(f"[GENERATE] User {request.user_id}, Mode: {request.mode}")
        logger.info(f"[GENERATE] Prompt: {request.prompt[:60]}...")
        
        # Extract context
        vision_ctx = ContextManager.extract_vision_context(request.conversation_history or [])
        text_ctx = ContextManager.extract_text_context(request.conversation_history or [])
        prev_images = ContextManager.extract_previous_images(request.conversation_history or [])
        
        # Detect request type
        request_type = ContextManager.detect_request_type(request.prompt, request.conversation_history or [])
        
        # Build context summary
        context_summary = ContextManager.build_context_summary(
            request_type, vision_ctx, text_ctx, prev_images
        )
        
        logger.info(f"[CONTEXT] Type: {request_type}")
        logger.info(f"[CONTEXT] Sources: {context_summary['context_sources']}")
        
        # Enhance prompt with LLM
        enhanced_prompt = enhance_prompt_with_llm(
            user_prompt=request.prompt,
            request_type=request_type,
            vision_ctx=vision_ctx,
            text_ctx=text_ctx,
            prev_images=prev_images,
            mode=request.mode
        )
        
        # Generate image
        b64_image, image_url, error, runtime_ms = generate_image_flux(enhanced_prompt)
        
        if error:
            return GenerateResponse(
                success=False,
                error=error,
                context_summary=context_summary
            )
        
        # Save to DB
        saved = False
        if b64_image and request.save_to_db:
            success, image_id = save_generated_image(
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                prompt_original=request.prompt,
                prompt_enhanced=enhanced_prompt,
                image_url=image_url or "",
                image_b64=b64_image,
                context_summary=context_summary,
                runtime_ms=runtime_ms
            )
            saved = success
        
        # Cache context
        if request.conversation_id:
            cache_generation_context(request.conversation_id, {
                "last_prompt": enhanced_prompt,
                "request_type": request_type,
                "timestamp": datetime.utcnow().isoformat()
            })
        
        logger.info(f"[GENERATE] ✓ Success (saved: {saved})")
        
        return GenerateResponse(
            success=True,
            image_b64=b64_image,
            image_url=image_url,
            enhanced_prompt=enhanced_prompt,
            context_summary=context_summary,
            saved_to_db=saved
        )
    
    except Exception as e:
        logger.error(f"[GENERATE] Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return GenerateResponse(
            success=False,
            error=f"⚠️ Internal error: {str(e)}"
        )


# ═══════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("🎨 SKYLIGHT IMAGE GENERATION SERVICE v3.0")
    logger.info(f"   Image Model: {DEEPINFRA_IMAGE_MODEL}")
    logger.info(f"   Prompt Model: {DEEPINFRA_PROMPT_MODEL}")
    logger.info(f"   Size: {DEEPINFRA_IMAGE_SIZE}")
    logger.info(f"   DB Storage: {'✓' if ENABLE_DB_STORAGE else '✗'}")
    logger.info(f"   Redis Cache: {'✓' if ENABLE_REDIS_CACHE else '✗'}")
    logger.info("   Features:")
    logger.info("     ✓ Context-Aware Generation")
    logger.info("     ✓ Vision → Image")
    logger.info("     ✓ Iterative Editing")
    logger.info("     ✓ Multi-Modal Context Synthesis")
    logger.info("=" * 60)