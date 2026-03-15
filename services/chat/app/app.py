"""
═══════════════════════════════════════════════════════════════
SKYLIGHT CHAT SERVICE - FIXED VERSION
═══════════════════════════════════════════════════════════════
Handles all chat modes with mode-specific logic
- Assistant Mode
- Code Mode
- IT Expert Mode
- Student Mode
- Social Mode

CHANGES:
- ChatRequest.query → ChatRequest.prompt (compatibility with Gateway)
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
import httpx
import json
import os

# Load production prompts
from prompts_production import (
    ASSISTANT_SYSTEM_PROMPT,
    CODE_SYSTEM_PROMPT,
    IT_EXPERT_SYSTEM_PROMPT,
    STUDENT_SYSTEM_PROMPT,
    SOCIAL_SYSTEM_PROMPT,
    CODE_VISION_SYSTEM_PROMPT,
)

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# DeepInfra Configuration (from environment - matching original)
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")

# Model configurations per mode (using env vars from original deployment)
MODE_CONFIGS = {
    "assistant": {
        "model": os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens": int(os.getenv("DEEPINFRA_ASSISTANT_MAX_TOKENS", "4096")),
        "temperature": float(os.getenv("DEEPINFRA_ASSISTANT_TEMPERATURE", "0.7")),
        "top_p": 0.9,
        "system_prompt": ASSISTANT_SYSTEM_PROMPT,
    },
    "code": {
        "model": os.getenv("DEEPINFRA_CODE_MODEL", "Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo"),
        "max_tokens": int(os.getenv("DEEPINFRA_CODE_MAX_TOKENS", "4096")),
        "temperature": float(os.getenv("DEEPINFRA_CODE_TEMPERATURE", "0.3")),
        "top_p": 0.85,
        "system_prompt": CODE_SYSTEM_PROMPT,
    },
    "it_expert": {
        "model": os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens": 3500,
        "temperature": 0.5,
        "top_p": 0.88,
        "system_prompt": IT_EXPERT_SYSTEM_PROMPT,
    },
    "student": {
        "model": os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens": 2500,
        "temperature": 0.8,
        "top_p": 0.9,
        "system_prompt": STUDENT_SYSTEM_PROMPT,
    },
    "social": {
        "model": os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens": 2500,
        "temperature": 0.9,
        "top_p": 0.92,
        "system_prompt": SOCIAL_SYSTEM_PROMPT,
    },
}

app = FastAPI(title="Skylight Chat Service", version="1.0.0")

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    prompt: str  # ✅ FIXED: Changed from "query" to "prompt"
    mode: str
    user_id: int
    conversation_id: Optional[str] = None
    history: Optional[List[Dict]] = None
    rag_context: Optional[str] = None
    context: Optional[str] = None
    session_summary: Optional[str] = None

# ═══════════════════════════════════════════════════════════════
# DEEPINFRA CLIENT
# ═══════════════════════════════════════════════════════════════

async def stream_deepinfra_completion(
    messages: List[Dict],
    model: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
):
    """Stream completion from DeepInfra"""
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
    }
    
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
    }
    
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{DEEPINFRA_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except:
                        continue

# ═══════════════════════════════════════════════════════════════
# MESSAGE BUILDERS
# ═══════════════════════════════════════════════════════════════

def build_messages(
    mode: str,
    user_prompt: str,  # Internal parameter name (can be anything)
    history: List[Dict],
    rag_context: Optional[str] = None,
    context: Optional[str] = None,
    session_summary: Optional[str] = None,
) -> List[Dict]:
    """
    Build message array for DeepInfra API
    """
    
    config = MODE_CONFIGS[mode]
    messages = []
    
    # System prompt
    system_content = config["system_prompt"]
    
    # Add context injections to system prompt
    if rag_context:
        system_content += f"\n\n[Context Data]\n[RAG Context - Documentation]\n{rag_context}\n[/Context Data]"
    
    if context:
        system_content += f"\n\n[Context Data]\n[Web Search Results]\n{context}\n[/Context Data]"
    
    if session_summary:
        system_content += f"\n\n[SESSION SUMMARY]\n{session_summary}\n[/SESSION SUMMARY]"
    
    messages.append({"role": "system", "content": system_content})
    
    # Add history (last 10 messages)
    if history:
        for msg in history[-10:]:
            messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })
    
    # Current query
    messages.append({"role": "user", "content": user_prompt})
    
    return messages

# ═══════════════════════════════════════════════════════════════
# CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Main chat endpoint - handles all modes
    """
    
    # Validate mode
    if request.mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")
    
    # Get mode configuration
    config = MODE_CONFIGS[request.mode]
    
    # Build messages
    messages = build_messages(
        mode=request.mode,
        user_prompt=request.prompt,  # ✅ FIXED: Changed from request.query to request.prompt
        history=request.history or [],
        rag_context=request.rag_context,
        context=request.context,
        session_summary=request.session_summary,
    )
    
    # Stream response
    async def response_generator():
        try:
            async for chunk in stream_deepinfra_completion(
                messages=messages,
                model=config["model"],
                max_tokens=config["max_tokens"],
                temperature=config["temperature"],
                top_p=config["top_p"],
            ):
                yield chunk
        except Exception as e:
            error_msg = f"\n\n⚠️ Bir hata oluştu: {str(e)}"
            yield error_msg
    
    return StreamingResponse(
        response_generator(),
        media_type="text/plain; charset=utf-8",
    )

# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "chat",
        "modes": list(MODE_CONFIGS.keys()),
    }

@app.get("/")
async def root():
    return {
        "service": "Skylight Chat Service",
        "version": "1.0.0",
        "modes": list(MODE_CONFIGS.keys()),
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)