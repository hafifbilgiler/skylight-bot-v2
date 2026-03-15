"""
═══════════════════════════════════════════════════════════════
SKYLIGHT CHAT SERVICE - WITH SMART TOOLS INTEGRATION
═══════════════════════════════════════════════════════════════
Handles all chat modes with mode-specific logic
- Assistant Mode
- Code Mode
- IT Expert Mode
- Student Mode
- Social Mode

FEATURES:
- Real-time data detection (weather, time, currency, news)
- Smart Tools integration for current information
- Enhanced conversation context (15 message pairs)
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

# DeepInfra Configuration
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")

# Smart Tools Configuration
SMART_TOOLS_URL = os.getenv("SMART_TOOLS_URL", "http://skylight-smart-tools:8081")

# Model configurations per mode
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

app = FastAPI(title="Skylight Chat Service", version="2.0.0")

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    prompt: str
    mode: str
    user_id: int
    conversation_id: Optional[str] = None
    history: Optional[List[Dict]] = None
    rag_context: Optional[str] = None
    context: Optional[str] = None
    session_summary: Optional[str] = None

# ═══════════════════════════════════════════════════════════════
# SMART TOOLS INTEGRATION
# ═══════════════════════════════════════════════════════════════

REAL_TIME_KEYWORDS = {
    # Zaman
    "şuan", "şimdi", "an", "bugün", "güncel", "canlı", "son dakika",
    "now", "current", "live", "today", "latest", "this moment",
    
    # Hava
    "hava", "havadurumu", "havalar", "weather", "sıcaklık", "sicaklik",
    "derece", "yağmur", "yagmur", "kar", "rüzgar", "ruzgar",
    
    # Finans
    "dolar", "euro", "kur", "döviz", "doviz", "sterlin", "pound",
    "bitcoin", "ethereum", "kripto", "crypto", "btc", "eth",
    
    # Haberler
    "haber", "news", "gündem", "gundem", "gelişme", "gelisme",
    
    # Saat
    "saat", "time", "kaç", "kac", "kaçta", "kacta",
    
    # Fiyat
    "fiyat", "price", "ne kadar", "kac lira", "kaç lira",
}

def needs_real_time_data(query: str) -> bool:
    """Detect if query needs real-time data from Smart Tools"""
    query_lower = query.lower()
    
    # Check for real-time keywords
    for keyword in REAL_TIME_KEYWORDS:
        if keyword in query_lower:
            return True
    
    return False

async def call_smart_tools(query: str) -> Optional[str]:
    """
    Call Smart Tools service for real-time data
    Returns formatted context string or None
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{SMART_TOOLS_URL}/unified",
                json={"query": query}
            )
            
            if response.status_code != 200:
                print(f"[SMART TOOLS] Error: {response.status_code}")
                return None
            
            data = response.json()
            
            if not data.get("success"):
                print(f"[SMART TOOLS] Failed: {data.get('error')}")
                return None
            
            # Format the tool response into context
            tool_used = data.get("tool_used", "unknown")
            tool_data = data.get("data", {})
            
            context_parts = [f"[Real-time Data - {tool_used.upper()}]"]
            
            if tool_used == "weather":
                context_parts.append(
                    f"Location: {tool_data.get('city')}, {tool_data.get('country')}\n"
                    f"Temperature: {tool_data.get('temperature')}°C (feels like {tool_data.get('feels_like')}°C)\n"
                    f"Conditions: {tool_data.get('description')}\n"
                    f"Humidity: {tool_data.get('humidity')}%\n"
                    f"Wind: {tool_data.get('wind_speed')} km/h"
                )
            
            elif tool_used == "time":
                context_parts.append(
                    f"Current Time: {tool_data.get('short', tool_data.get('formatted_tr'))}"
                )
            
            elif tool_used == "currency":
                context_parts.append(
                    f"Exchange Rate: {tool_data.get('formatted')}"
                )
            
            elif tool_used == "crypto":
                context_parts.append(
                    f"Cryptocurrency: {tool_data.get('formatted')}\n"
                    f"24h Change: {tool_data.get('change_24h')}%"
                )
            
            elif tool_used == "news":
                articles = tool_data.get('articles', [])[:3]
                if articles:
                    context_parts.append("Latest News:")
                    for i, article in enumerate(articles, 1):
                        context_parts.append(f"{i}. {article.get('title')}")
            
            elif tool_used == "web_search":
                results = tool_data.get('results', [])[:3]
                if results:
                    context_parts.append("Search Results:")
                    for i, result in enumerate(results, 1):
                        context_parts.append(
                            f"{i}. {result.get('title')}\n   {result.get('content', '')[:150]}"
                        )
            
            formatted_context = "\n".join(context_parts)
            print(f"[SMART TOOLS] Success: {tool_used} - {len(formatted_context)} chars")
            
            return formatted_context
            
    except Exception as e:
        print(f"[SMART TOOLS ERROR] {e}")
        return None

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

async def build_messages(
    mode: str,
    user_prompt: str,
    history: List[Dict],
    rag_context: Optional[str] = None,
    context: Optional[str] = None,
    session_summary: Optional[str] = None,
) -> List[Dict]:
    """
    Build message array for DeepInfra API
    Now with Smart Tools integration!
    """
    
    config = MODE_CONFIGS[mode]
    messages = []
    
    # System prompt
    system_content = config["system_prompt"]
    
    # ✅ SMART TOOLS: Check if real-time data needed
    smart_tools_context = None
    if needs_real_time_data(user_prompt):
        print(f"[CHAT] Real-time data detected, calling Smart Tools...")
        smart_tools_context = await call_smart_tools(user_prompt)
    
    # Add all contexts to system prompt
    if smart_tools_context:
        system_content += f"\n\n[Real-Time Data]\n{smart_tools_context}\n[/Real-Time Data]"
    
    if rag_context:
        system_content += f"\n\n[Context Data]\n[RAG Context - Documentation]\n{rag_context}\n[/Context Data]"
    
    if context:
        system_content += f"\n\n[Context Data]\n[Web Search Results]\n{context}\n[/Context Data]"
    
    if session_summary:
        system_content += f"\n\n[SESSION SUMMARY]\n{session_summary}\n[/SESSION SUMMARY]"
    
    messages.append({"role": "system", "content": system_content})
    
    # Add history (last 15 message pairs = 30 total messages)
    if history:
        for msg in history[-15:]:
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
    Now with Smart Tools integration for real-time data!
    """
    
    # Validate mode
    if request.mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")
    
    # Get mode configuration
    config = MODE_CONFIGS[request.mode]
    
    # Build messages (with Smart Tools integration)
    messages = await build_messages(
        mode=request.mode,
        user_prompt=request.prompt,
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
        "features": {
            "smart_tools": bool(SMART_TOOLS_URL),
            "real_time_data": True,
        }
    }

@app.get("/")
async def root():
    return {
        "service": "Skylight Chat Service",
        "version": "2.0.0",
        "modes": list(MODE_CONFIGS.keys()),
        "features": {
            "conversation_context": "15 message pairs (30 total)",
            "smart_tools_integration": True,
            "real_time_data": ["weather", "time", "currency", "crypto", "news"],
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)