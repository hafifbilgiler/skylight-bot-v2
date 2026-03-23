"""
═══════════════════════════════════════════════════════════════
SKYLIGHT CHAT SERVICE — v3.1 (LiveDataRouter entegrasyonu)
═══════════════════════════════════════════════════════════════
v3.0 → v3.1 değişiklikleri:

  ✅ needs_real_time_data() KALDIRILDI — artık yok
  ✅ call_smart_tools() KALDIRILDI — artık yok
  ✅ REAL_TIME_KEYWORDS KALDIRILDI — artık yok
  ✅ Tüm canlı veri kararları smart_tools /classify'a taşındı
  ✅ Tek fonksiyon: call_live_data(query) → /live endpoint
  ✅ format_live_context() → LLM'e hazır format /live'dan gelir

  Kural: Chat servisi asla keyword listesi tutmaz.
         "Bu sorgu için canlı veri lazım mı?" → smart_tools /classify
         "Canlı veriyi getir" → smart_tools /live

  Tüm diğer özellikler aynen korundu:
  ✅ Memory sistemi
  ✅ Thinking display
  ✅ Web synthesis
  ✅ Auto summaries
  ✅ Code context + compression
  ✅ Multi-mode support
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
import re
from datetime import datetime

from intent_classifier import build_reasoning_hint, get_intent_thinking_steps, Intent
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

DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY",  "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
SMART_TOOLS_URL    = os.getenv("SMART_TOOLS_URL",    "http://skylight-smart-tools:8081")

DB_HOST     = os.getenv("DB_HOST",     "postgres")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME",     "skylight_db")
DB_USER     = os.getenv("DB_USER",     "skylight_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SUMMARY_INTERVAL = int(os.getenv("SUMMARY_INTERVAL", "15"))

MODE_CONFIGS = {
    "assistant": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  int(os.getenv("DEEPINFRA_ASSISTANT_MAX_TOKENS", "4096")),
        "temperature": float(os.getenv("DEEPINFRA_ASSISTANT_TEMPERATURE", "0.7")),
        "top_p":       0.9,
        "system_prompt": ASSISTANT_SYSTEM_PROMPT,
    },
    "code": {
        "model":       os.getenv("DEEPINFRA_CODE_MODEL", "Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo"),
        "max_tokens":  int(os.getenv("DEEPINFRA_CODE_MAX_TOKENS", "16000")),
        "temperature": float(os.getenv("DEEPINFRA_CODE_TEMPERATURE", "0.2")),
        "top_p":       0.85,
        "system_prompt":           CODE_SYSTEM_PROMPT,
        "compression_threshold":   12,
        "large_file_threshold":    2000,
    },
    "it_expert": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  3500,
        "temperature": 0.5,
        "top_p":       0.88,
        "system_prompt": IT_EXPERT_SYSTEM_PROMPT,
    },
    "student": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  2500,
        "temperature": 0.8,
        "top_p":       0.9,
        "system_prompt": STUDENT_SYSTEM_PROMPT,
    },
    "social": {
        "model":       os.getenv("DEEPINFRA_ASSISTANT_MODEL", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
        "max_tokens":  2500,
        "temperature": 0.9,
        "top_p":       0.92,
        "system_prompt": SOCIAL_SYSTEM_PROMPT,
    },
}

app = FastAPI(title="Skylight Chat Service", version="3.1.0")
db_pool: Optional[asyncpg.Pool] = None

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            min_size=2, max_size=10, command_timeout=60,
        )
        print("[DB] Connection pool created")
    except Exception as e:
        print(f"[DB ERROR] {e}")
        db_pool = None

@app.on_event("shutdown")
async def shutdown_db():
    global db_pool
    if db_pool:
        await db_pool.close()

# ═══════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role:    str
    content: str

class ChatRequest(BaseModel):
    prompt:          str
    mode:            str
    user_id:         int
    conversation_id: Optional[str]        = None
    history:         Optional[List[Dict]] = None
    rag_context:     Optional[str]        = None
    context:         Optional[str]        = None
    session_summary: Optional[str]        = None

class ThinkingStep(BaseModel):
    emoji:   str
    message: str

# ═══════════════════════════════════════════════════════════════
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LIVE DATA — Hızlı local detection + Smart Tools veri çekme
#
# KURAL: Detection LOCAL (0ms, network yok)
#        Veri çekme → smart_tools /unified (sadece lazımsa)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ═══════════════════════════════════════════════════════════════

# ── Anlık API sinyalleri — smart tools /unified'a gider ────────
_CURRENCY_KW  = ("dolar","euro","eur","usd","gbp","sterlin","pound","jpy","yen","chf",
                 "kur","döviz","doviz","exchange rate","kaç tl","kac tl","tl kaç",
                 "dolar kaç","euro kaç","kur nedir")
_WEATHER_KW   = ("hava durumu","havadurumu","hava nasıl","havalar nasıl",
                 "hava kaç derece","sıcaklık","sicaklik","yağmur yağıyor",
                 "kar yağıyor","weather","forecast","bugün hava","yarın hava","derece")
_CRYPTO_KW    = ("bitcoin","btc","ethereum","eth","dogecoin","doge","solana",
                 "kripto","crypto","coin fiyat","bitcoin kaç","ethereum kaç")
_TIME_KW      = ("saat kaç","saat kac","saati kaç","şimdi saat","şu an saat",
                 "bugün ne günü","what time","günün saati")
_NEWS_KW      = ("son haberler","güncel haberler","bugünün haberleri","son dakika",
                 "breaking news","haberleri göster","haberleri getir","haber oku",
                 "gündem ne","today's news","latest news")
_PRICE_KW     = ("kaç lira","fiyatı ne kadar","fiyatı kaç","altın fiyatı",
                 "gram altın","petrol fiyatı","borsa","bist","hisse fiyatı")

# ── Borsa keywords ──────────────────────────────────────────
_BORSA_KW     = (
    "borsa","bist","hisse","teknik analiz","mum analiz","mum grafiği",
    "rsi","macd","bollinger","destek direnç","al sinyali","sat sinyali",
    "hisse analiz","thyao","garan","akbnk","eregl","kchol",
    "btc analiz","eth analiz","kripto analiz",
)
BORSA_URL = os.getenv("BORSA_URL", "http://skylight-borsa:8086")

# ── Canlı veri gerekmez ─────────────────────────────────────────
_STATIC_KW    = ("nasıl kullanılır","syntax","örnek ver","açıkla","anlat",
                 "ne demek","tanımı nedir","nasıl yapılır","tutorial")
_TECH_MODES   = {"code","it_expert"}


def _detect_live_type(query: str, mode: str) -> Optional[str]:
    """
    LOCAL detection — 0ms, network yok.
    Döner: "currency" | "weather" | "crypto" | "time" | "news" | "price" | None
    """
    if mode in _TECH_MODES:
        return None
    q = query.lower()
    if any(k in q for k in _STATIC_KW) and len(q.split()) <= 6:
        return None
    if any(k in q for k in _CURRENCY_KW): return "currency"
    if any(k in q for k in _WEATHER_KW):  return "weather"
    if any(k in q for k in _CRYPTO_KW):   return "crypto"
    if any(k in q for k in _TIME_KW):     return "time"
    if any(k in q for k in _NEWS_KW):     return "news"
    if any(k in q for k in _BORSA_KW):    return "borsa"
    if any(k in q for k in _PRICE_KW):    return "price_search"
    return None


async def get_live_data(query: str, mode: str = "assistant") -> Optional[str]:
    """
    1. Local detection (0ms) — canlı veri lazım mı?
    2. Lazımsa smart_tools /unified'a git — gerçek veriyi getir
    3. format_for_llm() ile LLM'e hazır formata dönüştür

    Ağ çağrısı sadece canlı veri gerektiğinde yapılır.
    """
    live_type = _detect_live_type(query, mode)
    if not live_type:
        return None

    # Borsa — borsa servisine git
    if live_type == "borsa":
        try:
            # Sembolü query'den çıkar
            import re as _re
            # Bilinen BIST sembolleri
            known = ["THYAO","GARAN","AKBNK","EREGL","KCHOL","SAHOL","PETKM","TUPRS",
                     "BIMAS","ASELS","FROTO","TOASO","SISE","TTKOM","ARCLK",
                     "BTC","ETH","BNB","SOL","XRP","DOGE"]
            sym = None
            q_upper = query.upper()
            for k in known:
                if k in q_upper:
                    sym = k
                    break
            # Regex ile 2-6 harf sembol bul
            if not sym:
                m = _re.search(r'\b([A-ZÇĞİÖŞÜ]{2,6})\b', q_upper)
                if m:
                    sym = m.group(1)

            if sym:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"{BORSA_URL}/analyze/{sym}")
                    if resp.status_code == 200:
                        d = resp.json()
                        summary = d.get("ai_summary", "")
                        print(f"[BORSA] ✅ {sym} analizi alındı")
                        return f"[Borsa Analizi]\n{summary}\n[/Borsa Analizi]"
        except Exception as e:
            print(f"[BORSA] Hata: {e}")
        return None

    if not SMART_TOOLS_URL:
        return None

    print(f"[LIVE DATA] '{query[:40]}' → {live_type}")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{SMART_TOOLS_URL}/unified",
                json={"query": query},
            )
            if resp.status_code != 200:
                print(f"[LIVE DATA] HTTP {resp.status_code}")
                return None

            data      = resp.json()
            if not data.get("success"):
                return None

            tool_used = data.get("tool_used", live_type)
            tool_data = data.get("data", {})

            # Format — LLM'e beslenecek temiz metin
            parts = [f"[Canlı Veri — {tool_used.upper()}]"]

            if tool_used == "weather":
                parts.append(
                    f"📍 {tool_data.get('city')}, {tool_data.get('country')}\n"
                    f"🌡️ {tool_data.get('temperature')}°C (hissedilen {tool_data.get('feels_like')}°C)\n"
                    f"☁️ {tool_data.get('description')}\n"
                    f"💧 Nem: %{tool_data.get('humidity')} | "
                    f"💨 Rüzgar: {tool_data.get('wind_speed')} km/h"
                )
            elif tool_used == "currency":
                parts.append(f"💱 {tool_data.get('formatted')}")
            elif tool_used == "crypto":
                parts.append(
                    f"₿ {tool_data.get('formatted')}\n"
                    f"📈 24s değişim: {tool_data.get('change_24h'):+.2f}%"
                )
            elif tool_used == "time":
                parts.append(f"🕐 {tool_data.get('formatted_tr')}")
            elif tool_used == "news":
                articles = tool_data.get("articles", [])[:5]
                if articles:
                    parts.append("📰 Son Haberler:")
                    for i, a in enumerate(articles, 1):
                        parts.append(f"  {i}. {a.get('title','')}")
            elif tool_used in ("web_search","price_search"):
                for r in tool_data.get("results", [])[:3]:
                    parts.append(f"• {r.get('title','')}: {r.get('content','')[:200]}")

            formatted = "\n".join(parts)
            print(f"[LIVE DATA] ✅ {tool_used} — {len(formatted)} chars")
            return formatted

    except httpx.TimeoutException:
        print(f"[LIVE DATA] Timeout")
    except Exception as e:
        print(f"[LIVE DATA] Error: {e}")
    return None


# ═══════════════════════════════════════════════════════════════
# MEMORY & CONTEXT
# ═══════════════════════════════════════════════════════════════

async def load_user_memory(user_id: int) -> Optional[str]:
    if not db_pool:
        return None
    try:
        async with db_pool.acquire() as conn:
            memory = await conn.fetchval("SELECT get_user_memory_for_prompt($1)", user_id)
            if memory:
                return memory
            await conn.execute(
                "INSERT INTO user_memory (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                user_id)
            return "[USER MEMORY]\nNo memory data yet\n[/USER MEMORY]"
    except Exception as e:
        print(f"[MEMORY ERROR] {e}")
        return None


async def load_conversation_summary(conversation_id: str) -> Optional[str]:
    if not db_pool or not conversation_id:
        return None
    try:
        async with db_pool.acquire() as conn:
            summary = await conn.fetchval(
                "SELECT get_conversation_summary($1::uuid)", conversation_id)
            return summary or "[CONVERSATION SUMMARY]\nNew conversation\n[/CONVERSATION SUMMARY]"
    except Exception as e:
        print(f"[SUMMARY ERROR] {e}")
        return None


async def should_create_summary(conversation_id: str) -> bool:
    if not db_pool or not conversation_id:
        return False
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT COUNT(*) as total_messages,
                       COALESCE(MAX(cs.messages_end), 0) as last_summary_end
                FROM messages m
                LEFT JOIN conversation_summaries cs ON m.conversation_id = cs.conversation_id
                WHERE m.conversation_id = $1::uuid
            """, conversation_id)
            if result:
                diff = result['total_messages'] - result['last_summary_end']
                return diff >= SUMMARY_INTERVAL
    except Exception as e:
        print(f"[SUMMARY CHECK ERROR] {e}")
    return False


async def create_conversation_summary(user_id: int, conversation_id: str, config: Dict):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            messages = await conn.fetch("""
                SELECT content, role, created_at, id FROM messages
                WHERE conversation_id = $1::uuid
                AND id > COALESCE(
                    (SELECT max(end_message_id::text)::uuid FROM conversation_summaries
                     WHERE conversation_id = $1::uuid),
                    '00000000-0000-0000-0000-000000000000'::uuid)
                ORDER BY created_at ASC
            """, conversation_id)
            if not messages or len(messages) < 5:
                return

            conv_text = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
            prompt    = f"""Analyze this conversation and create a structured summary:

{conv_text}

Provide a JSON response:
{{
    "topic": "Main topic",
    "subtopics": ["sub1","sub2"],
    "progress": "What was accomplished",
    "decisions_made": ["decision1"],
    "next_steps": ["step1"],
    "learned_facts": {{"preferences": "...", "technical": "..."}}
}}
Respond ONLY with valid JSON."""

            summary_response = ""
            async for chunk in stream_deepinfra_completion(
                messages=[
                    {"role": "system", "content": "Conversation analyst. JSON only."},
                    {"role": "user",   "content": prompt}
                ],
                model=config["model"], max_tokens=800,
                temperature=0.3, top_p=0.85,
            ):
                summary_response += chunk

            try:
                data = json.loads(summary_response.strip())
            except json.JSONDecodeError:
                if "```json" in summary_response:
                    s = summary_response.find("```json") + 7
                    e = summary_response.find("```", s)
                    data = json.loads(summary_response[s:e].strip())
                else:
                    return

            await conn.execute("""
                INSERT INTO conversation_summaries
                (user_id, conversation_id, messages_start, messages_end,
                 topic, subtopics, summary_text, progress,
                 decisions_made, next_steps, learned_facts,
                 start_message_id, end_message_id, messages_summarized)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb,
                        $12::uuid, $13::uuid, $14)
            """,
                user_id, conversation_id, 1, len(messages),
                data.get('topic','Conversation'), data.get('subtopics',[]),
                data.get('progress',''),          data.get('progress',''),
                data.get('decisions_made',[]),     data.get('next_steps',[]),
                json.dumps(data.get('learned_facts',{})),
                messages[0]['id'], messages[-1]['id'], len(messages),
            )
            print(f"[SUMMARY] Created: {data.get('topic')}")

            if data.get('learned_facts'):
                await update_user_memory_from_summary(user_id, data['learned_facts'])
    except Exception as e:
        print(f"[SUMMARY CREATION ERROR] {e}")
        import traceback; traceback.print_exc()


async def update_user_memory_from_summary(user_id: int, learned_facts: Dict):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            if 'technical' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory SET technical_preferences = technical_preferences || $1::jsonb,
                    updated_at=NOW() WHERE user_id=$2
                """, json.dumps({'learned': learned_facts['technical']}), user_id)
            if 'preferences' in learned_facts:
                await conn.execute("""
                    UPDATE user_memory SET communication_style = communication_style || $1::jsonb,
                    updated_at=NOW() WHERE user_id=$2
                """, json.dumps({'noted': learned_facts['preferences']}), user_id)
    except Exception as e:
        print(f"[MEMORY UPDATE ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
# CODE MODE
# ═══════════════════════════════════════════════════════════════

async def load_code_context(conversation_id: str) -> Optional[Dict]:
    if not db_pool or not conversation_id:
        return None
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT last_code, last_language, last_file_name,
                       tech_stack, compressed_history, messages_since_compression
                FROM code_context WHERE conversation_id=$1::uuid
            """, conversation_id)
            if result:
                return dict(result)
            await conn.execute("""
                INSERT INTO code_context (conversation_id, user_id)
                SELECT $1::uuid, user_id FROM conversations WHERE id=$1::uuid
                ON CONFLICT DO NOTHING
            """, conversation_id)
    except Exception as e:
        print(f"[CODE CONTEXT ERROR] {e}")
    return None


def extract_code_from_message(message: str) -> Optional[Dict]:
    match = re.search(r'```(\w+)\n(.*?)```', message, re.DOTALL)
    if not match:
        return None
    language = match.group(1).lower()
    code     = match.group(2).strip()
    file_name = None
    if language == 'python':
        m = re.search(r'#\s*(?:File:|Dosya:)?\s*([a-zA-Z0-9_\-\.\/]+\.py)', code, re.IGNORECASE)
        if m: file_name = m.group(1)
    elif language in ['javascript','typescript','js','ts']:
        m = re.search(r'//\s*(?:File:|Dosya:)?\s*([a-zA-Z0-9_\-\.\/]+\.(?:js|ts))', code, re.IGNORECASE)
        if m: file_name = m.group(1)
    return {"code": code, "language": language, "file_name": file_name,
            "line_count": len(code.split('\n'))}


async def track_shared_code(conversation_id: str, code: str, language: str, file_name: Optional[str]):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE code_context SET last_code=$2, last_language=$3,
                last_file_name=COALESCE($4, last_file_name),
                messages_since_compression=messages_since_compression+1, updated_at=NOW()
                WHERE conversation_id=$1::uuid
            """, conversation_id, code, language, file_name)
    except Exception as e:
        print(f"[CODE TRACKING ERROR] {e}")


async def should_compress_code_context(conversation_id: str, threshold: int = 12) -> bool:
    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT messages_since_compression FROM code_context
                WHERE conversation_id=$1::uuid
            """, conversation_id)
            return bool(count and count >= threshold)
    except:
        return False


async def compress_code_context(conversation_id: str, messages: List[Dict], config: Dict):
    if len(messages) < 5:
        return
    conv_text = "\n\n".join([
        f"{m['role'].upper()}: {m['content'][:500]}"
        for m in messages[:-5]
    ])
    prompt = f"""Analyze this code conversation and create a BRIEF summary (max 150 words):
{conv_text}
Extract ONLY: Files discussed, Issues fixed, Technical decisions, Current project context.
Format as compact bullet points. Use Turkish if conversation is Turkish."""
    try:
        compressed = ""
        async for chunk in stream_deepinfra_completion(
            messages=[{"role":"system","content":"You summarize code conversations concisely."},
                      {"role":"user","content":prompt}],
            model=config["model"], max_tokens=400, temperature=0.3, top_p=0.85,
        ):
            compressed += chunk
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE code_context SET compressed_history=$2,
                compression_metadata=$3::jsonb,
                messages_since_compression=0, last_compression_at=NOW()
                WHERE conversation_id=$1::uuid
            """, conversation_id, compressed,
                json.dumps({"compressed_at": datetime.now().isoformat()}))
        print(f"[CODE COMPRESSION] {len(conv_text)} → {len(compressed)} chars")
    except Exception as e:
        print(f"[CODE COMPRESSION ERROR] {e}")


async def build_code_messages(
    user_id: int,
    conversation_id: str,
    user_prompt: str,
    history: List[Dict],
    config: Dict,
    **kwargs,
) -> List[Dict]:
    messages     = []
    code_context = await load_code_context(conversation_id) if conversation_id else None

    system_content = config["system_prompt"]

    # ── REASONING LAYER — Code mode intent ───────────────────────
    reasoning_hint = build_reasoning_hint(user_prompt, history or [], "code")
    system_content = reasoning_hint + "\n\n" + system_content
    print(f"[INTENT/CODE] {reasoning_hint.split(chr(10))[0]}")
    # ─────────────────────────────────────────────────────────────

    # Memory
    user_memory = await load_user_memory(user_id)
    system_content = system_content.replace(
        "{user_memory}", user_memory or "[USER MEMORY]\nNo memory yet\n[/USER MEMORY]")

    # Code context
    if code_context:
        parts = ["[CODE CONTEXT]"]
        last_code = code_context.get('last_code')
        if last_code and len(last_code) < 5000:
            parts.append(f"\nLast shared code ({code_context.get('last_language')}):")
            parts.append(f"```{code_context.get('last_language')}\n{last_code[:3000]}\n```\n")
        tech_stack = code_context.get('tech_stack')
        if tech_stack:
            parts.append(f"Project stack: {', '.join(tech_stack)}")
        compressed = code_context.get('compressed_history')
        if compressed:
            parts.append(f"\nPrevious conversation summary:\n{compressed}\n")
        parts.append("[/CODE CONTEXT]")
        system_content += "\n\n" + "\n".join(parts)

    # ── Canlı veri (code modda da çalışır) ──────────────────────
    live_context = await get_live_data(user_prompt, mode="code")
    if live_context:
        system_content += f"\n\n{live_context}"

    # RAG + web context
    if kwargs.get('rag_context'):
        system_content += f"\n\n[RAG Context]\n{kwargs['rag_context']}\n[/RAG Context]"
    if kwargs.get('context'):
        system_content += f"\n\n[Web Search]\n{kwargs['context']}\n[/Web Search]"

    messages.append({"role": "system", "content": system_content})

    # Compression check
    if conversation_id and await should_compress_code_context(
        conversation_id, config.get("compression_threshold", 12)
    ):
        asyncio.create_task(compress_code_context(conversation_id, history, config))

    # History
    recent = history[-8:] if (code_context and code_context.get('compressed_history') and len(history) > 10) else history[-20:]
    for msg in recent:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_prompt})

    # Track code in background
    code_data = extract_code_from_message(user_prompt)
    if code_data and conversation_id:
        asyncio.create_task(track_shared_code(
            conversation_id, code_data['code'],
            code_data['language'], code_data.get('file_name')))

    return messages


# ═══════════════════════════════════════════════════════════════
# THINKING DISPLAY
# ═══════════════════════════════════════════════════════════════

def should_show_thinking(prompt: str, mode: str, history: list = None) -> bool:
    """Intent-aware thinking display."""
    # Debug/hata her zaman
    debug_indicators = ['debug','hata','fix','düzelt','error','crash',
                        'çalışmıyor','exception','traceback','crashloopback']
    p = prompt.lower()
    if any(i in p for i in debug_indicators):
        return True
    # Code modunda teknik işlemler
    if mode == "code" and any(i in p for i in ['dosya','file','refactor','optimize']):
        return True
    # IT expert modunda sorunlar
    if mode == "it_expert" and any(i in p for i in ['error','hata','sorun','problem']):
        return True
    # Kısa follow-up sorularda thinking gösterme
    if history and len(prompt.split()) <= 5:
        return False
    return False


async def generate_thinking_steps(
    prompt: str,
    mode: str,
    history: List[Dict] = None,
) -> List[ThinkingStep]:
    """Intent-aware thinking steps."""
    raw_steps = get_intent_thinking_steps(prompt, history or [], mode)
    return [ThinkingStep(emoji=e, message=m) for e, m in raw_steps]


# ═══════════════════════════════════════════════════════════════
# WEB SEARCH SYNTHESIS
# ═══════════════════════════════════════════════════════════════

async def synthesize_web_results(raw: str, query: str, config: Dict) -> str:
    prompt = f"""Web search results for: "{query}"

{raw}

Synthesize into a clear answer:
1. Combine related info from multiple sources
2. Include dates for time-sensitive info
3. Brief attribution: "Kaynak: X" (no full URLs)
4. Resolve conflicts — prefer most recent
5. User-friendly language, add context

Same language as query (Turkish/English). Concise but comprehensive."""
    try:
        out = ""
        async for chunk in stream_deepinfra_completion(
            messages=[{"role":"system","content":"Research synthesizer. Clear, accurate summaries."},
                      {"role":"user","content":prompt}],
            model=config["model"], max_tokens=1500, temperature=0.5, top_p=0.85,
        ):
            out += chunk
        print(f"[WEB SYNTHESIS] {len(raw)} → {len(out)} chars")
        return out
    except Exception as e:
        print(f"[WEB SYNTHESIS ERROR] {e}")
        return raw


# ═══════════════════════════════════════════════════════════════
# DEEPINFRA CLIENT
# ═══════════════════════════════════════════════════════════════

async def stream_deepinfra_completion(
    messages:    List[Dict],
    model:       str,
    max_tokens:  int,
    temperature: float,
    top_p:       float,
) -> AsyncGenerator[str, None]:
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
    }
    payload = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
        "top_p": top_p, "stream": True,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{DEEPINFRA_BASE_URL}/chat/completions",
                                 headers=headers, json=payload) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data    = json.loads(data_str)
                        content = data.get("choices",[{}])[0].get("delta",{}).get("content","")
                        if content:
                            yield content
                    except:
                        continue


# ═══════════════════════════════════════════════════════════════
# MESSAGE BUILDER — Ana sistem prompt oluşturma
# ═══════════════════════════════════════════════════════════════

async def build_messages(
    mode:            str,
    user_id:         int,
    conversation_id: Optional[str],
    user_prompt:     str,
    history:         List[Dict],
    rag_context:     Optional[str] = None,
    context:         Optional[str] = None,
    session_summary: Optional[str] = None,
    config:          Dict           = None,
) -> List[Dict]:
    """
    LLM'e gönderilecek mesaj dizisini oluşturur.

    Öncelik sırası:
    1. USER MEMORY       — en yüksek öncelik
    2. CONVERSATION SUMMARY
    3. LIVE DATA         — smart_tools /live (kur, hava, kripto, haber, araştırma)
    4. RAG CONTEXT       — döküman/bilgi tabanı
    5. WEB SEARCH        — gateway'den gelen context
    6. SESSION SUMMARY   — legacy destek
    """

    # Code mode özel builder'a gider
    if mode == "code":
        return await build_code_messages(
            user_id=user_id, conversation_id=conversation_id,
            user_prompt=user_prompt, history=history, config=config,
            rag_context=rag_context, context=context, session_summary=session_summary,
        )

    messages       = []
    system_content = config["system_prompt"]

    # ── REASONING LAYER — Intent classification (LOCAL, 0ms) ─────
    # Claude'un iç akıl yürütmesini simüle eder.
    # Kullanıcının niyetini tespit eder, LLM'e ne yapması gerektiğini söyler.
    reasoning_hint = build_reasoning_hint(user_prompt, history or [], mode)
    system_content = reasoning_hint + "\n\n" + system_content
    print(f"[INTENT] {reasoning_hint.split(chr(10))[0]}")
    # ─────────────────────────────────────────────────────────────

    # ── GÜNCEL TARİH — smart_tools NTP (worldtimeapi) ────────────
    try:
        async with httpx.AsyncClient(timeout=4.0) as _c:
            _r = await _c.post(
                f"{SMART_TOOLS_URL}/unified",
                json={"query": "bugün tarih ne", "tool_type": "time"}
            )
            if _r.status_code == 200:
                _d = _r.json().get("data", {})
                _date_only = _d.get("date_only") or _d.get("formatted_tr", "").split(",")[0].strip()
                if _date_only:
                    system_content = (
                        f"[Sistem Bilgisi]\nBugünün tarihi: {_date_only}\n[/Sistem Bilgisi]\n\n"
                        + system_content
                    )
    except Exception:
        # Fallback: UTC+3 sistem saati
        from datetime import timezone, timedelta
        _now = datetime.now(timezone(timedelta(hours=3)))
        _months = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
                   "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
        _days = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
        _date_only = f"{_now.day} {_months[_now.month-1]} {_now.year}, {_days[_now.weekday()]}"
        system_content = (
            f"[Sistem Bilgisi]\nBugünün tarihi: {_date_only}\n[/Sistem Bilgisi]\n\n"
            + system_content
        )
    # ─────────────────────────────────────────────────────────────

    # 1. USER MEMORY
    user_memory = await load_user_memory(user_id)
    system_content = system_content.replace(
        "{user_memory}", user_memory or "[USER MEMORY]\nNo memory yet\n[/USER MEMORY]")

    # 2. CONVERSATION SUMMARY
    conv_summary = await load_conversation_summary(conversation_id)
    if conv_summary:
        system_content += f"\n\n{conv_summary}"

    # 3. LIVE DATA — smart_tools /classify + /live
    #    Artık keyword listesi yok. Tek çağrı, tek karar.
    live_context = await get_live_data(user_prompt, mode=mode)
    if live_context:
        system_content += f"\n\n{live_context}"
        print(f"[CHAT] Live data injected: {len(live_context)} chars")

    # 4. RAG CONTEXT
    if rag_context:
        system_content += f"\n\n[RAG Context]\n{rag_context}\n[/RAG Context]"

    # 5. WEB SEARCH (gateway'den geliyor — deep search context)
    if context:
        print(f"[CHAT] Web context received ({len(context)} chars), synthesizing...")
        synthesized = await synthesize_web_results(context, user_prompt, config)
        system_content += f"\n\n[Web Search Results]\n{synthesized}\n[/Web Search Results]"

    # 6. SESSION SUMMARY (legacy)
    if session_summary:
        system_content += f"\n\n[SESSION SUMMARY]\n{session_summary}\n[/SESSION SUMMARY]"

    messages.append({"role": "system", "content": system_content})

    # History (son 15 çift = 30 mesaj)
    for msg in (history or [])[-15:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_prompt})

    return messages


# ═══════════════════════════════════════════════════════════════
# CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Ana chat endpoint.

    LIVE DATA AKIŞI:
    1. build_messages() → get_live_data(prompt, mode) çağırır
    2. get_live_data() → smart_tools /classify → karar
    3. Canlı veri lazımsa → smart_tools /live → formatted context
    4. Context system prompt'a eklenir
    5. LLM cevaplar — gerçek verilerle

    ÖRNEK:
    "güncel euro kaç tl"
      → get_live_data() → /classify → live_utility/currency
      → /live → "1 EUR = 43.52 TRY"
      → system: "[Canlı Veri — CURRENCY]\n💱 1 EUR = 43.52 TRY"
      → LLM: "Euro şu an 43.52 TL" ✅
    """

    if request.mode not in MODE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")

    config        = MODE_CONFIGS[request.mode]
    show_thinking = should_show_thinking(request.prompt, request.mode, request.history or [])

    messages = await build_messages(
        mode=request.mode, user_id=request.user_id,
        conversation_id=request.conversation_id,
        user_prompt=request.prompt,
        history=request.history or [],
        rag_context=request.rag_context,
        context=request.context,
        session_summary=request.session_summary,
        config=config,
    )

    async def response_generator():
        try:
            buffer = ""

            # Thinking display
            if show_thinking:
                steps = await generate_thinking_steps(request.prompt, request.mode, request.history or [])
                for step in steps:
                    yield f"{step.emoji} {step.message}\n"
                yield "\n"

            # Streaming response
            async for chunk in stream_deepinfra_completion(
                messages=messages,
                model=config["model"], max_tokens=config["max_tokens"],
                temperature=config["temperature"], top_p=config["top_p"],
            ):
                buffer += chunk
                if any(c in buffer for c in [' ','.','!','?','\n',',']) or len(buffer) > 10:
                    yield buffer
                    buffer = ""
            if buffer:
                yield buffer

            # Periodic summary (background)
            if request.conversation_id:
                asyncio.create_task(check_and_create_summary_async(
                    request.user_id, request.conversation_id, config))

        except Exception as e:
            yield f"\n\n⚠️ Bir hata oluştu: {str(e)}"

    return StreamingResponse(response_generator(), media_type="text/plain; charset=utf-8")


async def check_and_create_summary_async(user_id: int, conversation_id: str, config: Dict):
    try:
        if await should_create_summary(conversation_id):
            await create_conversation_summary(user_id, conversation_id, config)
    except Exception as e:
        print(f"[SUMMARY BACKGROUND ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status":  "healthy",
        "service": "chat",
        "version": "3.1.0",
        "modes":   list(MODE_CONFIGS.keys()),
        "features": {
            "memory_system":    db_pool is not None,
            "thinking_display": True,
            "web_synthesis":    True,
            "auto_summaries":   True,
            "live_data_router": bool(SMART_TOOLS_URL),
            "code_context":     True,
        },
    }


@app.get("/")
async def root():
    return {
        "service": "Skylight Chat Service",
        "version": "3.1.0",
        "live_data": f"LiveDataRouter → {SMART_TOOLS_URL}/classify + /live",
        "modes":     list(MODE_CONFIGS.keys()),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)