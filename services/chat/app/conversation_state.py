"""
═══════════════════════════════════════════════════════════════
SKYLIGHT — CONVERSATION STATE & WORKSPACE MEMORY  v1.0
Redis-backed, TTL'li, konuşma başına izole.
═══════════════════════════════════════════════════════════════

Redis DB ayrımı (mevcut servislere dokunulmaz):
  DB 0 → Image Analysis
  DB 1 → SearXNG cache
  DB 2 → Smart Tools cache
  DB 3 → Conversation State   ← BU DOSYA
  DB 4 → Workspace Memory     ← BU DOSYA

Key şemaları:
  conv:{conversation_id}        TTL 24 saat
  workspace:{conversation_id}   TTL 7 gün
  task:{conversation_id}        TTL 2 saat
  prefs:{user_id}               TTL 30 gün
═══════════════════════════════════════════════════════════════
"""

import json
import os
from typing import Optional, List, Dict, Any
from datetime import datetime

import redis.asyncio as aioredis

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

REDIS_BASE_URL = os.getenv("REDIS_URL", "redis://skylight-redis:6379")

# TTL (saniye)
TTL_CONV      = 60 * 60 * 24       # 24 saat
TTL_WORKSPACE = 60 * 60 * 24 * 7   # 7 gün
TTL_TASK      = 60 * 60 * 2        # 2 saat
TTL_PREFS     = 60 * 60 * 24 * 30  # 30 gün

DB_CONV      = 3
DB_WORKSPACE = 4

# ─────────────────────────────────────────────────────────────
# CONNECTION — tek wrapper, geçişte sadece burası değişir
# ─────────────────────────────────────────────────────────────

_pools: Dict[int, aioredis.Redis] = {}


async def _get_redis(db: int) -> aioredis.Redis:
    """
    DB bazlı connection pool döndürür.
    İleride Sentinel'e geçilirse sadece bu fonksiyon değişir.
    """
    if db not in _pools:
        _pools[db] = await aioredis.from_url(
            f"{REDIS_BASE_URL}/{db}",
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _pools[db]


async def close_redis():
    """Shutdown'da bağlantıları kapat."""
    for pool in _pools.values():
        await pool.aclose()
    _pools.clear()


# ═══════════════════════════════════════════════════════════════
# CONVERSATION STATE
# ═══════════════════════════════════════════════════════════════

async def get_conv_state(conversation_id: str) -> Dict:
    """
    Konuşma state'ini Redis'ten al.
    Yoksa boş state döndür — hata atmaz.

    State alanları:
      conversation_type : "general" | "code_session" | "mixed"
      active_topic      : "namaz vakti hesaplama"
      topic_stack       : Son 5 konu ["ima işleme", "namaz vakti"]
      last_user_goal    : "Android'den konum alıp hesap yapma"
      last_intent       : "code_generate" | "general_chat" | ...
      message_count     : Toplam mesaj sayısı
      is_code_heavy     : Son 5 mesajın çoğu kod mu?
    """
    try:
        r = await _get_redis(DB_CONV)
        raw = await r.get(f"conv:{conversation_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        print(f"[CONV STATE] get hata: {e}")

    return {
        "conversation_type": "general",
        "active_topic":      "",
        "topic_stack":       [],
        "last_user_goal":    "",
        "last_intent":       "",
        "message_count":     0,
        "is_code_heavy":     False,
    }


async def update_conv_state(conversation_id: str, updates: Dict) -> None:
    """
    State'i güncelle — sadece gönderilen alanları değiştir.
    TTL'yi yenile (her mesajda 24 saat uzar).
    """
    try:
        r     = await _get_redis(DB_CONV)
        state = await get_conv_state(conversation_id)
        state.update(updates)

        # topic_stack max 5 tutar
        if "active_topic" in updates and updates["active_topic"]:
            stack = state.get("topic_stack", [])
            topic = updates["active_topic"]
            if not stack or stack[-1] != topic:
                stack.append(topic)
                state["topic_stack"] = stack[-5:]

        await r.setex(
            f"conv:{conversation_id}",
            TTL_CONV,
            json.dumps(state, ensure_ascii=False),
        )
    except Exception as e:
        print(f"[CONV STATE] update hata: {e}")


async def increment_message_count(conversation_id: str, intent: str) -> None:
    """
    Her mesajda çağrılır. message_count artar, intent kaydedilir.
    Code intent'leri is_code_heavy'yi günceller.
    """
    CODE_INTENTS = {"code_generate", "code_debug", "code_modify", "code_explain"}
    try:
        state = await get_conv_state(conversation_id)
        state["message_count"] = state.get("message_count", 0) + 1
        state["last_intent"]   = intent

        # code_heavy: son intent kod ise ve 3'ten fazla mesaj varsa
        if intent in CODE_INTENTS and state["message_count"] >= 3:
            state["is_code_heavy"] = True
            if state["conversation_type"] == "general":
                state["conversation_type"] = "mixed"
        elif state["is_code_heavy"] and intent not in CODE_INTENTS:
            state["conversation_type"] = "mixed"

        await update_conv_state(conversation_id, state)
    except Exception as e:
        print(f"[CONV STATE] increment hata: {e}")


# ═══════════════════════════════════════════════════════════════
# WORKSPACE MEMORY — Kod odaklı konuşmalar için
# ═══════════════════════════════════════════════════════════════

async def get_workspace(conversation_id: str) -> Dict:
    """
    Kod workspace'ini al.

    Alanlar:
      active_file     : "app.py"
      language        : "python"
      framework       : "FastAPI"
      last_error      : "ImportError: no module named..."
      open_tasks      : ["auth ekle", "test yaz"]
      user_prefs      : {full_code: True, no_explain: False, no_break: True}
      versions        : {"v1": "...", "v2": "...", "current": "..."}
      last_modified   : "login fonksiyonu"
    """
    try:
        r   = await _get_redis(DB_WORKSPACE)
        raw = await r.get(f"workspace:{conversation_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        print(f"[WORKSPACE] get hata: {e}")

    return {
        "active_file":   "",
        "language":      "",
        "framework":     "",
        "last_error":    "",
        "open_tasks":    [],
        "user_prefs":    {
            "full_code":   True,   # Tam kod ister
            "no_explain":  False,  # Açıklama istemez mi
            "no_break":    True,   # Mevcut yapıyı bozma
            "step_by_step": False, # Adım adım ister mi
        },
        "versions":      {},      # {"v1": kod, "v2": kod, "current": kod}
        "last_modified": "",
    }


async def update_workspace(conversation_id: str, updates: Dict) -> None:
    """Workspace'i güncelle — sadece gönderilen alanları değiştir."""
    try:
        r         = await _get_redis(DB_WORKSPACE)
        workspace = await get_workspace(conversation_id)

        # user_prefs merge (override değil)
        if "user_prefs" in updates:
            workspace["user_prefs"].update(updates.pop("user_prefs"))

        workspace.update(updates)

        await r.setex(
            f"workspace:{conversation_id}",
            TTL_WORKSPACE,
            json.dumps(workspace, ensure_ascii=False),
        )
    except Exception as e:
        print(f"[WORKSPACE] update hata: {e}")


async def save_version(conversation_id: str, code: str, label: str = "") -> str:
    """
    Kod versiyonu kaydet.
    "önceki hale dön" dediğinde buradan alınır.

    Returns: version key ("v1", "v2", ...)
    """
    try:
        workspace = await get_workspace(conversation_id)
        versions  = workspace.get("versions", {})

        # Yeni versiyon numarası
        existing_nums = [
            int(k[1:]) for k in versions
            if k.startswith("v") and k[1:].isdigit()
        ]
        next_num = (max(existing_nums) + 1) if existing_nums else 1
        ver_key  = f"v{next_num}"

        # Max 5 versiyon tut
        if len(versions) >= 5:
            oldest = f"v{min(existing_nums)}"
            del versions[oldest]

        versions[ver_key]     = {"code": code[:5000], "label": label,
                                 "saved_at": datetime.now().isoformat()}
        versions["current"]   = ver_key

        await update_workspace(conversation_id, {"versions": versions})
        print(f"[WORKSPACE] Versiyon kaydedildi: {ver_key} ({len(code)} chars)")
        return ver_key

    except Exception as e:
        print(f"[WORKSPACE] save_version hata: {e}")
        return ""


async def get_version(conversation_id: str, version: str = "current") -> Optional[str]:
    """
    Versiyon kodunu getir.
    version: "current", "v1", "v2", "previous"
    """
    try:
        workspace = await get_workspace(conversation_id)
        versions  = workspace.get("versions", {})

        if version == "previous":
            current_key = versions.get("current", "")
            if current_key and current_key.startswith("v"):
                prev_num = int(current_key[1:]) - 1
                version  = f"v{prev_num}" if prev_num >= 1 else current_key
            else:
                version = "current"

        if version == "current":
            current_key = versions.get("current", "")
            ver_data    = versions.get(current_key, {})
        else:
            ver_data = versions.get(version, {})

        return ver_data.get("code") if ver_data else None

    except Exception as e:
        print(f"[WORKSPACE] get_version hata: {e}")
        return None


async def add_task(conversation_id: str, task: str) -> None:
    """Açık görev ekle — "auth ekle", "test yaz" gibi."""
    try:
        workspace = await get_workspace(conversation_id)
        tasks = workspace.get("open_tasks", [])
        if task not in tasks:
            tasks.append(task)
            tasks = tasks[-10:]  # max 10 görev
        await update_workspace(conversation_id, {"open_tasks": tasks})
    except Exception as e:
        print(f"[WORKSPACE] add_task hata: {e}")


async def complete_task(conversation_id: str, task_keyword: str) -> None:
    """Görevi tamamla — keyword içereni bul ve sil."""
    try:
        workspace = await get_workspace(conversation_id)
        tasks = [t for t in workspace.get("open_tasks", [])
                 if task_keyword.lower() not in t.lower()]
        await update_workspace(conversation_id, {"open_tasks": tasks})
    except Exception as e:
        print(f"[WORKSPACE] complete_task hata: {e}")


# ═══════════════════════════════════════════════════════════════
# TASK OBJECT — Her kod isteğinde anlık oluşturulur
# ═══════════════════════════════════════════════════════════════

async def set_task(conversation_id: str, task: Dict) -> None:
    """
    Aktif task'ı kaydet.

    Task alanları:
      task_type      : "generate" | "debug" | "modify" | "explain" | "review"
      target         : "login fonksiyonu"
      user_goal      : "JWT ekle"
      response_style : "full_file" | "patch" | "explain" | "step_by_step"
      confidence     : "high" | "medium" | "low"
    """
    try:
        r = await _get_redis(DB_CONV)
        task["created_at"] = datetime.now().isoformat()
        await r.setex(
            f"task:{conversation_id}",
            TTL_TASK,
            json.dumps(task, ensure_ascii=False),
        )
    except Exception as e:
        print(f"[TASK] set hata: {e}")


async def get_task(conversation_id: str) -> Optional[Dict]:
    """Aktif task'ı getir."""
    try:
        r   = await _get_redis(DB_CONV)
        raw = await r.get(f"task:{conversation_id}")
        return json.loads(raw) if raw else None
    except Exception as e:
        print(f"[TASK] get hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# USER PREFS — Kalıcı tercihler (tüm konuşmalar)
# ═══════════════════════════════════════════════════════════════

async def get_user_prefs(user_id: int) -> Dict:
    """
    Kullanıcının kalıcı tercihlerini al.
    Tüm konuşmalarda geçerli — user_memory'den farklı,
    sadece davranış tercihleri burada.
    """
    try:
        r   = await _get_redis(DB_WORKSPACE)
        raw = await r.get(f"prefs:{user_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        print(f"[USER PREFS] get hata: {e}")

    return {
        "full_code":    True,   # Tam dosya ister
        "no_explain":   False,  # Açıklama istemez mi
        "no_break":     True,   # Mevcut yapıyı bozma
        "step_by_step": False,  # Adım adım ister mi
        "lang_pref":    "tr",   # Dil tercihi
    }


async def update_user_prefs(user_id: int, updates: Dict) -> None:
    """Kullanıcı tercihini güncelle."""
    try:
        r     = await _get_redis(DB_WORKSPACE)
        prefs = await get_user_prefs(user_id)
        prefs.update(updates)
        await r.setex(
            f"prefs:{user_id}",
            TTL_PREFS,
            json.dumps(prefs, ensure_ascii=False),
        )
    except Exception as e:
        print(f"[USER PREFS] update hata: {e}")


# ═══════════════════════════════════════════════════════════════
# CONTEXT BUILDER — app.py'e entegrasyon için
# ═══════════════════════════════════════════════════════════════

async def build_state_context(
    conversation_id: str,
    user_id: int,
    prompt: str,
    mode: str,
) -> str:
    """
    app.py'de system prompt'a eklenir.
    State + workspace bilgilerini LLM'e hazır formata çevirir.
    """
    parts = []

    # Conversation state
    state = await get_conv_state(conversation_id)
    if state.get("active_topic"):
        parts.append(f"[Aktif Konu] {state['active_topic']}")
    if state.get("last_user_goal"):
        parts.append(f"[Son Hedef] {state['last_user_goal']}")
    if state.get("topic_stack") and len(state["topic_stack"]) > 1:
        parts.append(f"[Konu Geçmişi] {' → '.join(state['topic_stack'][-3:])}")

    # Workspace (sadece code_session'da)
    if mode in ("code", "it_expert") or state.get("is_code_heavy"):
        ws = await get_workspace(conversation_id)
        if ws.get("active_file"):
            parts.append(f"[Aktif Dosya] {ws['active_file']} ({ws.get('language','')})")
        if ws.get("framework"):
            parts.append(f"[Framework] {ws['framework']}")
        if ws.get("last_error"):
            parts.append(f"[Son Hata] {ws['last_error']}")
        if ws.get("open_tasks"):
            parts.append(f"[Açık Görevler] {', '.join(ws['open_tasks'][:3])}")
        if ws.get("versions"):
            ver_count = len([k for k in ws["versions"] if k.startswith("v")])
            current   = ws["versions"].get("current", "")
            if ver_count > 0:
                parts.append(f"[Kod Versiyonları] {ver_count} versiyon, şu an: {current}")

        # User prefs
        prefs = await get_user_prefs(user_id)
        pref_hints = []
        if prefs.get("full_code"):      pref_hints.append("tam kod ister")
        if prefs.get("no_break"):       pref_hints.append("mevcut yapıyı bozma")
        if prefs.get("no_explain"):     pref_hints.append("açıklama istemez")
        if prefs.get("step_by_step"):   pref_hints.append("adım adım ister")
        if pref_hints:
            parts.append(f"[Kullanıcı Tercihleri] {', '.join(pref_hints)}")

    # Aktif task
    task = await get_task(conversation_id)
    if task:
        parts.append(
            f"[Aktif Görev] {task.get('task_type','?')}: "
            f"{task.get('target','?')} — {task.get('user_goal','?')}"
        )

    if not parts:
        return ""

    return "[CONVERSATION STATE]\n" + "\n".join(parts) + "\n[/CONVERSATION STATE]"


async def extract_and_update_state(
    conversation_id: str,
    user_id: int,
    prompt: str,
    response: str,
    intent: str,
    mode: str,
) -> None:
    """
    Her cevap sonrası state'i güncelle.
    Background task olarak çalışır — response'u bloklamaz.
    """
    try:
        updates: Dict[str, Any] = {}
        ws_updates: Dict[str, Any] = {}

        # ── Topic detection — basit ama etkili ─────────────────
        # Prompt'tan konu çıkar (ilk 60 karakter yeterli)
        topic_candidate = prompt.strip()[:80].split("?")[0].split(".")[0].strip()
        if len(topic_candidate) > 10:
            updates["active_topic"] = topic_candidate
            updates["last_user_goal"] = prompt.strip()[:150]

        # ── Intent güncelle ────────────────────────────────────
        updates["last_intent"] = intent
        await increment_message_count(conversation_id, intent)

        # ── Workspace güncelle (kod modunda) ───────────────────
        if mode in ("code", "it_expert"):
            # Dosya adı tespiti
            import re
            file_m = re.search(
                r'(?:dosya|file|#\s*File:?|//\s*File:?)\s*[:\-]?\s*'
                r'([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z]{1,6})',
                prompt + " " + response,
                re.IGNORECASE,
            )
            if file_m:
                ws_updates["active_file"] = file_m.group(1)

            # Dil tespiti — kod bloğundan
            lang_m = re.search(r'```(\w+)', response)
            if lang_m:
                lang = lang_m.group(1).lower()
                if lang not in ("json", "yaml", "text", "plaintext", "bash", "sh"):
                    ws_updates["language"] = lang

            # Framework tespiti
            fw_map = {
                "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
                "express": "Express", "react": "React", "next": "Next.js",
                "vue": "Vue", "nuxt": "Nuxt", "flutter": "Flutter",
                "spring": "Spring Boot", "rails": "Rails",
            }
            combined = (prompt + " " + response).lower()
            for kw, fw in fw_map.items():
                if kw in combined:
                    ws_updates["framework"] = fw
                    break

            # Hata tespiti
            err_m = re.search(
                r'(Error|Exception|Traceback)[:\s]+([^\n]{0,120})',
                prompt,
                re.IGNORECASE,
            )
            if err_m:
                ws_updates["last_error"] = err_m.group(0)[:120]

            # Kod versiyonu kaydet — response'ta büyük kod bloğu varsa
            code_blocks = re.findall(r'```\w*\n(.*?)```', response, re.DOTALL)
            largest_block = max(code_blocks, key=len) if code_blocks else ""
            if len(largest_block) > 200:
                label = topic_candidate[:40] if topic_candidate else ""
                await save_version(conversation_id, largest_block, label)

        # ── Kullanıcı tercihi tespiti ──────────────────────────
        pref_map = {
            "tam kodu": {"full_code": True},
            "tamamını yaz": {"full_code": True},
            "full code": {"full_code": True},
            "açıklama yapma": {"no_explain": True},
            "sadece kod": {"no_explain": True},
            "mevcut yapıyı bozma": {"no_break": True},
            "adım adım": {"step_by_step": True},
        }
        p_lower = prompt.lower()
        for kw, pref in pref_map.items():
            if kw in p_lower:
                await update_user_prefs(user_id, pref)
                break

        # ── Kaydet ─────────────────────────────────────────────
        if updates:
            await update_conv_state(conversation_id, updates)
        if ws_updates:
            await update_workspace(conversation_id, ws_updates)

    except Exception as e:
        print(f"[STATE EXTRACT] hata: {e}")