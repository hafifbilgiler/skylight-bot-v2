"""
═══════════════════════════════════════════════════════════════
SKYLIGHT — TASK BUILDER  v1.0
═══════════════════════════════════════════════════════════════

Model kod cevabı üretmeden önce bu modül çalışır.
Intent + workspace + prompt'tan bir task objesi oluşturur.
Bu obje hem system prompt'a inject edilir hem Redis'e kaydedilir.

Task tipleri:
  generate  → Yeni kod yaz
  debug     → Hata bul ve düzelt
  modify    → Mevcut kodu değiştir
  explain   → Kodu açıkla
  review    → Kodu incele
  test      → Test yaz
  docs      → Dokümantasyon yaz

Response stilleri:
  full_file   → Tüm dosyayı yaz
  patch       → Sadece değişen kısmı yaz
  explain     → Açıklama (kod yok veya minimal)
  step_by_step → Adım adım
  root_cause  → Root cause analizi + fix
═══════════════════════════════════════════════════════════════
"""

import re
from typing import Optional, List, Dict


# ─────────────────────────────────────────────────────────────
# TASK TYPE DETECTION
# ─────────────────────────────────────────────────────────────

_GENERATE_SIGNALS = (
    "yaz", "oluştur", "yarat", "create", "generate", "write",
    "make", "build", "implement", "kodla", "yap", "ekle",
    "fonksiyon yaz", "class yaz", "script yaz", "api yaz",
    "endpoint yaz", "servis yaz", "modül yaz",
)

_DEBUG_SIGNALS = (
    "hata", "error", "bug", "çalışmıyor", "crash", "exception",
    "traceback", "failed", "fix", "düzelt", "neden çalışmıyor",
    "sorun", "problem", "500", "404", "null", "undefined",
    "crashloopback", "oomkilled", "imagepull",
)

_MODIFY_SIGNALS = (
    "güncelle", "değiştir", "refactor", "iyileştir", "update",
    "modify", "change", "ekle", "kaldır", "sil", "remove",
    "yeniden yaz", "rewrite", "optimize", "temizle",
)

_EXPLAIN_SIGNALS = (
    "açıkla", "anlat", "explain", "nasıl çalışıyor", "ne yapıyor",
    "ne demek", "nedir", "anlamıyorum", "what does", "how does",
)

_REVIEW_SIGNALS = (
    "incele", "gözden geçir", "review", "kontrol et",
    "sorun var mı", "hata var mı", "check", "bakabilir misin",
)

_TEST_SIGNALS = (
    "test yaz", "unit test", "test ekle", "write tests",
    "test coverage", "pytest", "jest", "test senaryosu",
)

_DOCS_SIGNALS = (
    "dokümantasyon", "documentation", "docstring", "yorum ekle",
    "comment", "readme", "jsdoc", "açıklama ekle",
)


def _detect_task_type(prompt: str, intent: str) -> str:
    """Prompt ve intent'ten task tipini belirle."""
    p = prompt.lower()

    # Intent'ten direkt map
    _INTENT_TO_TASK = {
        "debug_request":    "debug",
        "code_generate":    "generate",
        "code_modify":      "modify",
        "code_explain":     "explain",
        "code_review":      "review",
        "code_test":        "test",
        "code_docs":        "docs",
        "feature_request":  "modify",
        "performance":      "debug",
        "security":         "review",
    }
    if intent in _INTENT_TO_TASK:
        return _INTENT_TO_TASK[intent]

    # Prompt signal'larından detect et
    if any(s in p for s in _DEBUG_SIGNALS):    return "debug"
    if any(s in p for s in _TEST_SIGNALS):     return "test"
    if any(s in p for s in _DOCS_SIGNALS):     return "docs"
    if any(s in p for s in _REVIEW_SIGNALS):   return "review"
    if any(s in p for s in _EXPLAIN_SIGNALS):  return "explain"
    if any(s in p for s in _MODIFY_SIGNALS):   return "modify"
    if any(s in p for s in _GENERATE_SIGNALS): return "generate"

    return "generate"  # varsayılan


def _detect_response_style(
    task_type: str,
    prompt: str,
    workspace: Dict,
) -> str:
    """
    Kullanıcının ne beklediğini belirle.
    user_prefs + task_type + prompt sinyallerine bakılır.
    """
    p     = prompt.lower()
    prefs = workspace.get("user_prefs", {})

    # Kullanıcı "sadece açıkla" dediyse
    if any(s in p for s in ("sadece açıkla", "açıkla yeter", "explain only", "just explain")):
        return "explain"

    # Kullanıcı "adım adım" dediyse
    if any(s in p for s in ("adım adım", "step by step", "aşama aşama")):
        return "step_by_step"

    # Debug → root cause + fix
    if task_type == "debug":
        return "root_cause"

    # Explain → sadece açıklama
    if task_type == "explain":
        return "explain"

    # Review → patch önerileri
    if task_type in ("review", "docs", "test"):
        return "patch"

    # Modify + user "full code" istemiyorsa → patch
    if task_type == "modify":
        if prefs.get("full_code", True):
            return "full_file"
        return "patch"

    # Generate → full file
    if task_type == "generate":
        return "full_file"

    return "full_file"


def _detect_target(prompt: str, workspace: Dict, history: List[Dict]) -> str:
    """
    Hangi dosya/fonksiyon/komponent hedefleniyor?
    Önce prompt'tan, sonra workspace'ten, sonra history'den çıkar.
    """
    # Backtick içi
    m = re.search(r'`([^`]+)`', prompt)
    if m:
        return m.group(1)

    # Dosya adı
    file_m = re.search(
        r'([a-zA-Z0-9_\-]+\.[a-zA-Z]{1,6})',
        prompt,
    )
    if file_m:
        return file_m.group(1)

    # Fonksiyon/class adı
    code_m = re.search(
        r'(?:def|class|function|func)\s+(\w+)|(\w+)\s+(?:fonksiyonu|function|metodu|method|class)',
        prompt, re.IGNORECASE,
    )
    if code_m:
        return (code_m.group(1) or code_m.group(2) or "").strip()

    # Workspace'ten active_file
    if workspace.get("active_file"):
        return workspace["active_file"]

    # History'deki son kod bloğundan dosya adı
    for msg in reversed(history[-4:]):
        content = msg.get("content", "")
        hist_m = re.search(r'#\s*(?:File:|Dosya:)?\s*([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z]{1,6})', content)
        if hist_m:
            return hist_m.group(1)

    return ""


def _detect_user_goal(prompt: str) -> str:
    """Prompt'tan kullanıcının hedefini çıkar (max 100 char)."""
    # Soru işaretini temizle
    goal = prompt.strip().rstrip("?").strip()
    # İlk anlamlı cümle
    for sep in [".", "\n", ","]:
        if sep in goal:
            goal = goal.split(sep)[0].strip()
            break
    return goal[:100]


def _detect_missing_context(
    task_type: str,
    prompt: str,
    workspace: Dict,
    has_code_in_history: bool,
) -> List[str]:
    """
    Eksik bilgileri tespit et.
    Bunlar system prompt'a "eksik bilgi" olarak eklenir —
    LLM bunları varsaymak yerine sormalı.
    """
    missing = []
    p = prompt.lower()

    if task_type in ("debug", "modify") and not has_code_in_history:
        if "kod" not in p and "```" not in prompt:
            missing.append("hedef kod paylaşılmamış")

    if task_type == "generate":
        if not workspace.get("language") and "python" not in p and "js" not in p:
            if len(prompt.split()) < 5:
                missing.append("dil/teknoloji belirtilmemiş")

    if task_type == "debug" and "hata" in p and "mesaj" not in p and "traceback" not in p:
        if "```" not in prompt and len(prompt.split()) < 8:
            missing.append("hata mesajı paylaşılmamış")

    return missing


# ─────────────────────────────────────────────────────────────
# ANA FONKSIYON
# ─────────────────────────────────────────────────────────────

def build_task(
    prompt: str,
    intent: str,
    workspace: Dict,
    history: List[Dict],
) -> Dict:
    """
    Kod isteğinden önce task objesi oluştur.
    Bu obje:
      1. System prompt'a inject edilir (LLM ne yapacağını bilir)
      2. Redis'e kaydedilir (follow-up'larda bağlam korunur)

    Args:
        prompt    : Kullanıcının mesajı
        intent    : intent_classifier'dan gelen intent string
        workspace : Redis'ten gelen workspace memory
        history   : Son mesaj geçmişi

    Returns:
        task dict
    """
    has_code = any("```" in m.get("content", "") for m in history[-6:])

    task_type      = _detect_task_type(prompt, intent)
    response_style = _detect_response_style(task_type, prompt, workspace)
    target         = _detect_target(prompt, workspace, history)
    user_goal      = _detect_user_goal(prompt)
    missing        = _detect_missing_context(task_type, prompt, workspace, has_code)

    # Confidence: eksik bilgi varsa düşür
    if missing:
        confidence = "low"
    elif target:
        confidence = "high"
    else:
        confidence = "medium"

    task = {
        "task_type":      task_type,
        "target":         target,
        "user_goal":      user_goal,
        "response_style": response_style,
        "missing_context": missing,
        "confidence":     confidence,
        "language":       workspace.get("language", ""),
        "framework":      workspace.get("framework", ""),
        "user_prefs":     workspace.get("user_prefs", {}),
    }

    return task


def task_to_prompt_hint(task: Dict) -> str:
    """
    Task objesini system prompt'a eklenecek metin bloğuna çevir.
    LLM bu bloğu görünce ne yapacağını kesin bilir.
    """
    lines = ["[GÖREV]"]

    _TYPE_TR = {
        "generate":   "Yeni kod yaz",
        "debug":      "Hata analizi + fix",
        "modify":     "Mevcut kodu değiştir",
        "explain":    "Kodu açıkla",
        "review":     "Kodu incele",
        "test":       "Test yaz",
        "docs":       "Dokümantasyon yaz",
    }
    _STYLE_TR = {
        "full_file":    "Tam dosyayı yaz",
        "patch":        "Sadece değişen kısmı göster",
        "explain":      "Açıklama yap (kod yok veya minimal)",
        "step_by_step": "Adım adım anlat",
        "root_cause":   "Root cause + fix",
    }

    lines.append(f"Tip: {_TYPE_TR.get(task['task_type'], task['task_type'])}")

    if task.get("target"):
        lines.append(f"Hedef: {task['target']}")

    if task.get("user_goal"):
        lines.append(f"Kullanıcı hedefi: {task['user_goal']}")

    lines.append(f"Yanıt stili: {_STYLE_TR.get(task['response_style'], task['response_style'])}")

    if task.get("language"):
        lang_fw = task["language"]
        if task.get("framework"):
            lang_fw += f" / {task['framework']}"
        lines.append(f"Stack: {lang_fw}")

    # User prefs
    prefs = task.get("user_prefs", {})
    pref_notes = []
    if prefs.get("full_code"):    pref_notes.append("tam kod ister")
    if prefs.get("no_break"):     pref_notes.append("mevcut yapıyı bozma")
    if prefs.get("no_explain"):   pref_notes.append("açıklama istemez")
    if prefs.get("step_by_step"): pref_notes.append("adım adım ister")
    if pref_notes:
        lines.append(f"Tercihler: {', '.join(pref_notes)}")

    # Eksik bilgi varsa LLM'e söyle
    if task.get("missing_context"):
        missing_str = ", ".join(task["missing_context"])
        lines.append(f"⚠️  Eksik bilgi: {missing_str} — gerekirse kullanıcıdan iste")

    lines.append("[/GÖREV]")
    return "\n".join(lines)