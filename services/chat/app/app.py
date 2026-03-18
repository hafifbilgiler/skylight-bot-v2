"""
═══════════════════════════════════════════════════════════════
SKYLIGHT CHAT SERVICE - FULL VERSION WITH MEMORY & CONTEXT
═══════════════════════════════════════════════════════════════
FEATURES:
- ✅ Memory System (user preferences, learning)
- ✅ Thinking Display (Claude-style progress)
- ✅ Web Search Synthesis (LLM-powered)
- ✅ Auto Summaries (periodic, every 15-20 messages)
- ✅ Context Management (smart loading)
- ✅ Smart Tools Integration (real-time data)
- ✅ Enhanced conversation context
═══════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, AsyncGenerator
import httpx
import json
import os
import asyncpg
import asyncio
from datetime import datetime

# Load ENHANCED production prompts
from system_prompts import (
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

# Database Configuration
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "skylight_db")
DB_USER = os.getenv("DB_USER", "skylight_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Summary Configuration
SUMMARY_INTERVAL = int(os.getenv("SUMMARY_INTERVAL", "15"))  # Messages before creating summary

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

app = FastAPI(title="Skylight Chat Service", version="3.0.0")

# Database connection pool (global)
db_pool: Optional[asyncpg.Pool] = None

# ═══════════════════════════════════════════════════════════════
# DATABASE CONNECTION
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_db():
    """Initialize database connection pool on startup"""
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
            command_timeout=60,
        )
        print("[DB] Connection pool created successfully")
    except Exception as e:
        print(f"[DB ERROR] Failed to create pool: {e}")
        db_pool = None

@app.on_event("shutdown")
async def shutdown_db():
    """Close database pool on shutdown"""
    global db_pool
    if db_pool:
        await db_pool.close()
        print("[DB] Connection pool closed")

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

class ThinkingStep(BaseModel):
    emoji: str
    message: str
    type: str = "thinking"

# ═══════════════════════════════════════════════════════════════
# MEMORY & CONTEXT FUNCTIONS
# ═══════════════════════════════════════════════════════════════

async def load_user_memory(user_id: int) -> Optional[str]:
    """Load user memory from database for prompt injection"""
    if not db_pool:
        return None
    
    try:
        async with db_pool.acquire() as conn:
            memory = await conn.fetchval(
                "SELECT get_user_memory_for_prompt($1)",
                user_id
            )
            
            if memory:
                print(f"[MEMORY] Loaded for user {user_id}: {len(memory)} chars")
                return memory
            else:
                # Create default memory if not exists
                await conn.execute("""
                    INSERT INTO user_memory (user_id)
                    VALUES ($1)
                    ON CONFLICT (user_id) DO NOTHING
                """, user_id)
                return "[USER MEMORY]\nNo memory data yet\n[/USER MEMORY]"
                
    except Exception as e:
        print(f"[MEMORY ERROR] {e}")
        return None

async def load_conversation_summary(conversation_id: str) -> Optional[str]:
    """Load most recent conversation summary"""
    if not db_pool or not conversation_id:
        return None
    
    try:
        async with db_pool.acquire() as conn:
            summary = await conn.fetchval(
                "SELECT get_conversation_summary($1::uuid)",
                conversation_id
            )
            
            if summary:
                print(f"[SUMMARY] Loaded for conversation {conversation_id}: {len(summary)} chars")
                return summary
            else:
                return "[CONVERSATION SUMMARY]\nNew conversation\n[/CONVERSATION SUMMARY]"
                
    except Exception as e:
        print(f"[SUMMARY ERROR] {e}")
        return None

async def should_create_summary(conversation_id: str) -> bool:
    """Check if we should create a periodic summary"""
    if not db_pool or not conversation_id:
        return False
    
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_messages,
                    COALESCE(MAX(cs.messages_end), 0) as last_summary_end
                FROM messages m
                LEFT JOIN conversation_summaries cs 
                    ON m.conversation_id = cs.conversation_id
                WHERE m.conversation_id = $1::uuid
            """, conversation_id)
            
            if result:
                messages_since_summary = result['total_messages'] - result['last_summary_end']
                should_create = messages_since_summary >= SUMMARY_INTERVAL
                
                if should_create:
                    print(f"[SUMMARY] {messages_since_summary} new messages, creating summary...")
                
                return should_create
            
            return False
            
    except Exception as e:
        print(f"[SUMMARY CHECK ERROR] {e}")
        return False

async def create_conversation_summary(
    user_id: int,
    conversation_id: str,
    config: Dict
):
    """Create a conversation summary using LLM"""
    if not db_pool:
        return
    
    try:
        async with db_pool.acquire() as conn:
            # Get messages since last summary
            messages = await conn.fetch("""
                SELECT content, role, created_at, id
                FROM messages
                WHERE conversation_id = $1::uuid
                AND id > COALESCE(
                    (SELECT max(end_message_id) FROM conversation_summaries 
                     WHERE conversation_id = $1::uuid),
                    '00000000-0000-0000-0000-000000000000'::uuid
                )
                ORDER BY created_at ASC
            """, conversation_id)
            
            if not messages or len(messages) < 5:
                return  # Not enough messages to summarize
            
            # Build conversation text
            conversation_text = "\n".join([
                f"{msg['role']}: {msg['content']}"
                for msg in messages
            ])
            
            # Create summary prompt
            summary_prompt = f"""Analyze this conversation and create a structured summary:

{conversation_text}

Provide a JSON response with:
{{
    "topic": "Main topic discussed",
    "subtopics": ["subtopic1", "subtopic2"],
    "progress": "What was accomplished",
    "decisions_made": ["decision1", "decision2"],
    "next_steps": ["step1", "step2"],
    "learned_facts": {{
        "preferences": "User preferences discovered",
        "technical": "Technical details mentioned"
    }}
}}

Respond ONLY with valid JSON, no markdown, no explanation."""

            # Call LLM for summary
            summary_messages = [
                {"role": "system", "content": "You are a conversation analyst. Extract structured summaries in JSON format."},
                {"role": "user", "content": summary_prompt}
            ]
            
            summary_response = ""
            async for chunk in stream_deepinfra_completion(
                messages=summary_messages,
                model=config["model"],
                max_tokens=1000,
                temperature=0.3,
                top_p=0.85,
            ):
                summary_response += chunk
            
            # Parse JSON response
            try:
                summary_data = json.loads(summary_response.strip())
            except json.JSONDecodeError:
                # Try to extract JSON from markdown
                if "```json" in summary_response:
                    json_start = summary_response.find("```json") + 7
                    json_end = summary_response.find("```", json_start)
                    summary_response = summary_response[json_start:json_end].strip()
                    summary_data = json.loads(summary_response)
                else:
                    print(f"[SUMMARY ERROR] Invalid JSON: {summary_response[:200]}")
                    return
            
            # Store summary in database
            await conn.execute("""
                INSERT INTO conversation_summaries (
                    user_id, conversation_id, messages_start, messages_end,
                    topic, subtopics, summary_text, progress,
                    decisions_made, next_steps, learned_facts,
                    start_message_id, end_message_id, messages_summarized
                ) VALUES (
                    $1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::uuid, $13::uuid, $14
                )
            """,
                user_id,
                conversation_id,
                1 if not messages else 1,  # Start index
                len(messages),  # End index
                summary_data.get('topic', 'Conversation'),
                summary_data.get('subtopics', []),
                summary_data.get('progress', ''),
                summary_data.get('progress', ''),
                summary_data.get('decisions_made', []),
                summary_data.get('next_steps', []),
                json.dumps(summary_data.get('learned_facts', {})),
                messages[0]['id'] if messages else None,
                messages[-1]['id'] if messages else None,
                len(messages)
            )
            
            print(f"[SUMMARY] Created for conversation {conversation_id}: {summary_data.get('topic')}")
            
            # Update user memory with learned facts
            if summary_data.get('learned_facts'):
                await update_user_memory_from_summary(user_id, summary_data['learned_facts'])
                
    except Exception as e:
        print(f"[SUMMARY CREATION ERROR] {e}")
        import traceback
        traceback.print_exc()

async def update_user_memory_from_summary(user_id: int, learned_facts: Dict):
    """Update user memory with facts learned from conversation"""
    if not db_pool:
        return
    
    try:
        async with db_pool.acquire() as conn:
            # Update technical preferences if mentioned
            if 'technical' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory
                    SET technical_preferences = technical_preferences || $1::jsonb,
                        updated_at = NOW()
                    WHERE user_id = $2
                """, json.dumps({'learned': learned_facts['technical']}), user_id)
            
            # Update communication style if mentioned
            if 'preferences' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory
                    SET communication_style = communication_style || $1::jsonb,
                        updated_at = NOW()
                    WHERE user_id = $2
                """, json.dumps({'noted': learned_facts['preferences']}), user_id)
                
            print(f"[MEMORY] Updated from summary for user {user_id}")
            
    except Exception as e:
        print(f"[MEMORY UPDATE ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# THINKING DISPLAY FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def should_show_thinking(prompt: str, mode: str) -> bool:
    """Determine if thinking steps should be shown"""
    
    # Complex task indicators
    complex_indicators = [
        'debug', 'hata', 'fix', 'düzelt', 'refactor', 'iyileştir',
        'optimize', 'deploy', 'kurulum', 'install', 'analyze', 'analiz'
    ]
    
    # File operation indicators
    file_indicators = [
        'dosya', 'file', 'kod', 'code', 'oku', 'read', 'yaz', 'write'
    ]
    
    prompt_lower = prompt.lower()
    
    # Show for complex tasks
    if any(indicator in prompt_lower for indicator in complex_indicators):
        return True
    
    # Show for file operations in code mode
    if mode == "code" and any(indicator in prompt_lower for indicator in file_indicators):
        return True
    
    # Show for IT mode troubleshooting
    if mode == "it_expert" and any(word in prompt_lower for word in ['error', 'hata', 'sorun', 'problem']):
        return True
    
    return False

async def generate_thinking_steps(prompt: str, mode: str) -> List[ThinkingStep]:
    """Generate appropriate thinking steps based on task"""
    steps = []
    prompt_lower = prompt.lower()
    
    # Step 1: Understanding
    steps.append(ThinkingStep(
        emoji="🔍",
        message="Problemi anlıyorum..." if "tr" in prompt_lower or any(c in prompt for c in "çğıöşü") else "Understanding the problem..."
    ))
    
    # Step 2: Context (if complex)
    if any(word in prompt_lower for word in ['debug', 'fix', 'error', 'hata', 'sorun']):
        steps.append(ThinkingStep(
            emoji="📊",
            message="Bağlamı kontrol ediyorum..." if "tr" in prompt_lower or any(c in prompt for c in "çğıöşü") else "Checking context..."
        ))
    
    # Step 3: Analysis (for debugging)
    if any(word in prompt_lower for word in ['debug', 'hata', 'bug']):
        steps.append(ThinkingStep(
            emoji="💡",
            message="Root cause analizi..." if "tr" in prompt_lower or any(c in prompt for c in "çğıöşü") else "Root cause analysis..."
        ))
    
    # Step 4: Action
    steps.append(ThinkingStep(
        emoji="🔧",
        message="Çözüm hazırlanıyor..." if "tr" in prompt_lower or any(c in prompt for c in "çğıöşü") else "Preparing solution..."
    ))
    
    return steps

# ═══════════════════════════════════════════════════════════════
# WEB SEARCH SYNTHESIS
# ═══════════════════════════════════════════════════════════════

async def synthesize_web_results(
    raw_results: str,
    query: str,
    config: Dict
) -> str:
    """
    Synthesize raw web search results into clear, structured answer using LLM
    """
    
    synthesis_prompt = f"""You have web search results for the query: "{query}"

Raw search results:
{raw_results}

Synthesize these into a clear, structured answer following these rules:
1. Combine related information from multiple sources
2. Include dates for time-sensitive information (e.g., "Kubernetes 1.30 (Nisan 2024)")
3. Brief source attribution (e.g., "Kaynak: Kubernetes docs") - NO full URLs
4. Resolve conflicts by preferring most recent and authoritative sources
5. Explain in user-friendly language
6. Add context and interpretation

Respond in the SAME language as the query (Turkish/English).
Be concise but comprehensive. Use markdown formatting sparingly."""

    synthesis_messages = [
        {"role": "system", "content": "You are a research synthesizer. Create clear, accurate summaries from web search results."},
        {"role": "user", "content": synthesis_prompt}
    ]
    
    try:
        synthesized = ""
        async for chunk in stream_deepinfra_completion(
            messages=synthesis_messages,
            model=config["model"],
            max_tokens=1500,
            temperature=0.5,
            top_p=0.85,
        ):
            synthesized += chunk
        
        print(f"[WEB SYNTHESIS] Original: {len(raw_results)} chars → Synthesized: {len(synthesized)} chars")
        return synthesized
        
    except Exception as e:
        print(f"[WEB SYNTHESIS ERROR] {e}")
        return raw_results  # Fallback to raw results

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
) -> AsyncGenerator[str, None]:
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
    user_id: int,
    conversation_id: Optional[str],
    user_prompt: str,
    history: List[Dict],
    rag_context: Optional[str] = None,
    context: Optional[str] = None,
    session_summary: Optional[str] = None,
    config: Dict = None,
) -> List[Dict]:
    """
    Build message array for DeepInfra API
    WITH FULL CONTEXT: Memory + Summary + Smart Tools + Web Synthesis
    """
    
    messages = []
    
    # Get base system prompt
    system_content = config["system_prompt"]
    
    # ✅ 1. LOAD USER MEMORY (Highest Priority)
    user_memory = await load_user_memory(user_id)
    if user_memory:
        # Inject memory into system prompt using {user_memory} placeholder
        system_content = system_content.replace("{user_memory}", user_memory)
    else:
        system_content = system_content.replace("{user_memory}", "[USER MEMORY]\nNo memory yet\n[/USER MEMORY]")
    
    # ✅ 2. LOAD CONVERSATION SUMMARY
    conv_summary = await load_conversation_summary(conversation_id)
    if conv_summary:
        system_content += f"\n\n{conv_summary}"
    
    # ✅ 3. SMART TOOLS: Check if real-time data needed
    smart_tools_context = None
    if needs_real_time_data(user_prompt):
        print(f"[CHAT] Real-time data detected, calling Smart Tools...")
        smart_tools_context = await call_smart_tools(user_prompt)
    
    if smart_tools_context:
        system_content += f"\n\n[Real-Time Data]\n{smart_tools_context}\n[/Real-Time Data]"
    
    # ✅ 4. RAG CONTEXT (Documentation)
    if rag_context:
        system_content += f"\n\n[Context Data]\n[RAG Context - Documentation]\n{rag_context}\n[/Context Data]"
    
    # ✅ 5. WEB SEARCH RESULTS (Will be synthesized if present)
    if context:
        # Synthesize web results instead of raw paste
        print(f"[CHAT] Web results detected, synthesizing...")
        synthesized_context = await synthesize_web_results(context, user_prompt, config)
        system_content += f"\n\n[Context Data]\n[Web Search Results - Synthesized]\n{synthesized_context}\n[/Context Data]"
    
    # ✅ 6. SESSION SUMMARY (Legacy support)
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
# CHAT ENDPOINT WITH THINKING DISPLAY
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Main chat endpoint - FULL VERSION
    ✅ Memory Loading
    ✅ Thinking Display
    ✅ Web Search Synthesis
    ✅ Auto Summaries
    ✅ Context Management
    """
    
    # Validate mode
    if request.mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")
    
    # Get mode configuration
    config = MODE_CONFIGS[request.mode]
    
    # Check if thinking should be shown
    show_thinking = should_show_thinking(request.prompt, request.mode)
    
    # Build messages (with FULL context)
    messages = await build_messages(
        mode=request.mode,
        user_id=request.user_id,
        conversation_id=request.conversation_id,
        user_prompt=request.prompt,
        history=request.history or [],
        rag_context=request.rag_context,
        context=request.context,
        session_summary=request.session_summary,
        config=config,
    )
    
    # Stream response with thinking display
    async def response_generator():
        try:
            # ✅ THINKING DISPLAY (if complex task)
            if show_thinking:
                thinking_steps = await generate_thinking_steps(request.prompt, request.mode)
                
                for step in thinking_steps:
                    # Send thinking step as JSON
                    yield f"data: {json.dumps(step.dict())}\n\n"
                    await asyncio.sleep(0.3)  # Small delay for visual effect
                
                # Send completion marker for thinking
                yield f"data: {json.dumps({'type': 'thinking_done'})}\n\n"
            
            # ✅ STREAM ACTUAL RESPONSE
            async for chunk in stream_deepinfra_completion(
                messages=messages,
                model=config["model"],
                max_tokens=config["max_tokens"],
                temperature=config["temperature"],
                top_p=config["top_p"],
            ):
                # Send content as SSE
                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
            
            # ✅ PERIODIC SUMMARY CHECK (after response)
            if request.conversation_id:
                asyncio.create_task(check_and_create_summary_async(
                    request.user_id,
                    request.conversation_id,
                    config
                ))
            
        except Exception as e:
            error_msg = f"⚠️ Bir hata oluştu: {str(e)}"
            yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"
    
    return StreamingResponse(
        response_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )

async def check_and_create_summary_async(
    user_id: int,
    conversation_id: str,
    config: Dict
):
    """Background task to check and create summary"""
    try:
        if await should_create_summary(conversation_id):
            await create_conversation_summary(user_id, conversation_id, config)
    except Exception as e:
        print(f"[SUMMARY BACKGROUND ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "chat",
        "version": "3.0.0",
        "modes": list(MODE_CONFIGS.keys()),
        "features": {
            "memory_system": db_pool is not None,
            "thinking_display": True,
            "web_synthesis": True,
            "auto_summaries": True,
            "smart_tools": bool(SMART_TOOLS_URL),
            "context_management": True,
        }
    }

@app.get("/")
async def root():
    return {
        "service": "Skylight Chat Service",
        "version": "3.0.0 - FULL VERSION",
        "modes": list(MODE_CONFIGS.keys()),
        "features": {
            "memory_system": "User preferences, learning, progressive familiarity",
            "thinking_display": "Claude-style progress steps",
            "web_synthesis": "LLM-powered search result synthesis",
            "auto_summaries": f"Periodic summaries every {SUMMARY_INTERVAL} messages",
            "context_management": "Priority-based context loading",
            "conversation_context": "15 message pairs (30 total)",
            "smart_tools": "Real-time data (weather, time, currency, crypto, news)",
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)