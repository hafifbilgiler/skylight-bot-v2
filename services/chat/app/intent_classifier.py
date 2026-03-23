"""
═══════════════════════════════════════════════════════════════
SKYLIGHT — INTENT CLASSIFIER + REASONING LAYER
═══════════════════════════════════════════════════════════════

Claude'un içsel akıl yürütme katmanını simüle eder.

LLM çağrısından ÖNCE çalışır:
1. Kullanıcının niyetini tespit eder (LOCAL — 0ms, network yok)
2. LLM'e ne tür yanıt üretmesi gerektiğini söyleyen
   bir REASONING HINT üretir
3. Bu hint system prompt'a enjekte edilir

Neden önemli:
- LLM, "ne soruluyor?" değil "nasıl cevap vereyim?"den
  başlayarak üretim yapar
- Follow-up sorularda tüm konuyu baştan anlatmaz
- Debug isteklerinde root cause analizine odaklanır
- Öğretim isteklerinde adım adım ilerler
═══════════════════════════════════════════════════════════════
"""

import re
from typing import Optional, List, Dict, Tuple


# ─────────────────────────────────────────────────────────────
# INTENT TYPES
# ─────────────────────────────────────────────────────────────

class Intent:
    # Takip soruları
    FOLLOW_UP_SPECIFIC   = "follow_up_specific"   # "bu fonksiyon nasıl?" → sadece o
    FOLLOW_UP_SIMPLIFY   = "follow_up_simplify"   # "daha basit anlat"
    FOLLOW_UP_CONTINUE   = "follow_up_continue"   # "devam et", "bitir"
    FOLLOW_UP_SUMMARY    = "follow_up_summary"    # "özetle", "kısaca"
    FOLLOW_UP_EXPAND     = "follow_up_expand"     # "daha detaylı", "aç"
    FOLLOW_UP_WHY        = "follow_up_why"        # "neden?", "niye böyle?"

    # Teknik istekler
    DEBUG_REQUEST        = "debug_request"        # hata analizi, neden çalışmıyor
    CODE_GENERATE        = "code_generate"        # yeni kod yaz
    CODE_EXPLAIN         = "code_explain"         # kodu açıkla
    CODE_MODIFY          = "code_modify"          # kodu düzelt/ekle/refactor
    CODE_SECTION         = "code_section"         # kodun belirli bir kısmı

    # Öğrenme
    CONCEPT_LEARN        = "concept_learn"        # kavram nedir, nasıl çalışır
    HOW_TO               = "how_to"               # nasıl yapılır

    # Genel
    PROBLEM_SOLVE        = "problem_solve"        # bir problemi çöz
    NEW_TOPIC            = "new_topic"            # tamamen yeni konu
    CHAT                 = "chat"                 # sohbet, basit soru


# ─────────────────────────────────────────────────────────────
# SINYAL SÖZCÜKLERİ — LOCAL DETECTION (0ms)
# ─────────────────────────────────────────────────────────────

_FOLLOW_UP_SPECIFIC_SIGNALS = (
    # Türkçe
    "o kısım", "bu kısım", "o fonksiyon", "bu fonksiyon",
    "o satır", "bu satır", "o bölüm", "bu bölüm",
    "o class", "bu class", "o metod", "bu metod",
    "o bloğu", "bu bloğu", "orayı", "burayı",
    "o kodu", "bu kodu", "o kısmı", "bu kısmı",
    "o şeyi", "bunu aç", "onu aç", "orayı aç",
    # İngilizce
    "that function", "this function", "that part", "this part",
    "that line", "this line", "that section", "this section",
    "that method", "this method", "that class", "this class",
)

_FOLLOW_UP_SIMPLIFY_SIGNALS = (
    "daha basit", "basitçe", "daha kolay", "anlamadım",
    "anlayamadım", "kafam karıştı", "karmaşık", "daha açık",
    "daha net", "simpler", "simplify", "easier", "don't understand",
    "confused", "more clearly",
)

_FOLLOW_UP_CONTINUE_SIGNALS = (
    "devam et", "devam", "continue", "bitir", "tamamla",
    "complete", "finish", "geri kalan", "rest of", "kaldığın yerden",
    "yarıda kaldı", "kesmiştin", "durdun",
)

_FOLLOW_UP_SUMMARY_SIGNALS = (
    "özetle", "özet ver", "kısaca", "kısa", "sadece özet",
    "genel olarak", "kısalt", "summarize", "summary", "briefly",
    "in short", "tldr", "tl;dr",
)

_FOLLOW_UP_EXPAND_SIGNALS = (
    "daha detaylı", "detay ver", "daha fazla", "genişlet",
    "açar mısın", "daha fazla anlat", "elaborate", "more detail",
    "expand on", "tell me more", "more about",
)

_FOLLOW_UP_WHY_SIGNALS = (
    "neden böyle", "neden bu şekilde", "neden kullandın",
    "neden seçtin", "neden tercih", "neden yaptın",
    "neden bunu", "neden şunu", "neden onu",
    "why did you", "why is this", "what's the reason",
    "what's the purpose", "why use", "why not",
)

# Kısa "neden?" soruları — sadece 1-2 kelimeyse
_FOLLOW_UP_WHY_SHORT = (
    "neden", "niye", "niçin", "why",
)

_DEBUG_SIGNALS = (
    "hata", "error", "bug", "çalışmıyor", "çöktü", "crash",
    "exception", "traceback", "failed", "başarısız",
    "neden çalışmıyor", "neden hata", "sorun var", "problem var",
    "düzeltir misin", "fix", "debug", "neden çalışmıyor",
    "stacktrace", "hata alıyorum", "hata veriyor",
    "500", "404", "null pointer", "index error", "key error",
    "crashloopback", "oomkilled", "pending", "imagepullbackoff",
)

_CODE_EXPLAIN_SIGNALS = (
    "nasıl çalışıyor", "ne yapıyor", "ne işe yarıyor", "açıkla",
    "anlat", "explain", "how does", "what does", "what is",
    "what's this", "bu ne", "bu ne yapıyor", "bu nedir",
    "ne anlama geliyor", "anlamı ne",
)

_CODE_MODIFY_SIGNALS = (
    "düzelt", "güncelle", "değiştir", "refactor", "iyileştir",
    "optimize", "ekle", "kaldır", "sil", "update", "modify",
    "change", "remove", "add feature", "özellik ekle",
    "yeniden yaz", "rewrite",
)

_CODE_GENERATE_SIGNALS = (
    "yaz", "oluştur", "yarat", "create", "generate", "write",
    "make a", "build a", "implement", "yap", "kodla",
    "fonksiyon yaz", "class yaz", "script yaz",
)

_CONCEPT_LEARN_SIGNALS = (
    "nedir", "ne demek", "nasıl", "öğrenmek istiyorum",
    "öğret", "teach me", "what is", "how to learn",
    "what are", "basics of", "temellerini", "giriş",
    "introduction", "kavramı", "tanım", "definition",
)


# ─────────────────────────────────────────────────────────────
# BAĞLAM ANALİZİ
# ─────────────────────────────────────────────────────────────

def _has_recent_code(history: List[Dict], lookback: int = 4) -> bool:
    """Son N mesajda kod bloğu var mı?"""
    for msg in history[-lookback:]:
        if "```" in msg.get("content", ""):
            return True
    return False


def _has_recent_technical_answer(history: List[Dict], lookback: int = 2) -> bool:
    """Son N asistan mesajı teknik/uzun cevap içeriyor mu?"""
    for msg in reversed(history[-lookback * 2:]):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if len(content) > 300 or "```" in content:
                return True
    return False


def _extract_target(prompt: str, history: List[Dict]) -> Optional[str]:
    """
    Kullanıcının bahsettiği spesifik hedefi çıkar.
    Örn: "monitor_pods fonksiyonu nasıl çalışıyor?" → "monitor_pods fonksiyonu"
    """
    p = prompt.lower()

    # Backtick içindeki ifade
    backtick = re.search(r'`([^`]+)`', prompt)
    if backtick:
        return backtick.group(1)

    # "X fonksiyonu/satırı/kısmı" pattern
    patterns = [
        r'(\w+)\s+fonksiyonu',
        r'(\w+)\s+function',
        r'(\w+)\s+class',
        r'(\w+)\s+metodu',
        r'(\w+)\s+method',
        r'(\w+)\s+kısmı',
        r'(\w+)\s+bölümü',
        r'(\w+)\s+satırı',
        r'def\s+(\w+)',
        r'class\s+(\w+)',
    ]
    for pat in patterns:
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            return m.group(0)

    # Son asistan mesajından başlık/konu çıkar
    for msg in reversed(history[-4:]):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            # İlk code block başlığı
            code_m = re.search(r'```\w*\s*\n?#\s*(.+)', content)
            if code_m:
                return code_m.group(1).strip()[:50]
            break

    return None


def _is_very_short(prompt: str) -> bool:
    """3 kelimeden kısa mı?"""
    return len(prompt.strip().split()) <= 3


def _is_new_topic(prompt: str, history: List[Dict]) -> bool:
    """
    Bu prompt önceki konuşmayla ilgisiz yeni bir konu mu?
    Basit heuristik: çok uzunsa ve önceki mesajlarla kelime örtüşmesi azsa.
    """
    if not history:
        return True

    p_words = set(prompt.lower().split())
    prev_words = set()
    for msg in history[-3:]:
        prev_words.update(msg.get("content", "").lower().split())

    # Stop words çıkar
    stopwords = {"ne", "bu", "şu", "o", "bir", "var", "yok", "ve", "de", "da",
                 "the", "a", "an", "is", "it", "what", "how", "why", "can", "could"}
    p_words -= stopwords
    prev_words -= stopwords

    if not p_words or not prev_words:
        return True

    overlap = len(p_words & prev_words) / len(p_words)
    return overlap < 0.1 and len(prompt.split()) > 8


# ─────────────────────────────────────────────────────────────
# ANA SINIFLANDIRICI
# ─────────────────────────────────────────────────────────────

def classify_intent(
    prompt: str,
    history: List[Dict],
    mode: str = "assistant",
) -> Dict:
    """
    Kullanıcının niyetini LOCAL olarak sınıflandırır.
    Network çağrısı yok — 0ms.

    Döner:
    {
        "intent": Intent.xxx,
        "target": "monitor_pods fonksiyonu" | None,
        "has_prior_context": bool,
        "response_strategy": "...",  # LLM'e verilecek yönerge
        "confidence": "high" | "medium" | "low",
    }
    """
    p = prompt.lower().strip()

    # ── 0. GREETING GUARD — Her zaman önce kontrol et ─────────
    # Selamlama/basit sohbet başlangıcı → asla thinking gösterme, asla devam etme
    _GREETINGS = (
        "selam", "merhaba", "hey", "hi ", "hello", "naber", "nasılsın",
        "günaydın", "iyi günler", "iyi akşamlar", "iyi geceler",
        "selamlar", "heyy", "seleam", "slm", "mrb",
        "how are you", "what's up", "sup",
    )
    if any(p == g or p.startswith(g) for g in _GREETINGS):
        # Selamlama → sohbet, thinking yok
        return {
            "intent": Intent.CHAT,
            "target": None,
            "has_prior_context": False,
            "response_strategy": (
                "Kullanıcı sohbet başlatıyor. "
                "Samimi ve kısa karşılık ver. "
                "Önceki konuşmayı referans verme. "
                "Proaktif yardım öner ama kısa tut."
            ),
            "confidence": "high",
        }
    # ─────────────────────────────────────────────────────────────

    has_code_context   = _has_recent_code(history)
    has_tech_context   = _has_recent_technical_answer(history)
    has_history        = len(history) >= 1
    is_short           = _is_very_short(prompt)

    # ── 1. DEVAM ET / TAMAMLA ──────────────────────────────────
    if any(sig in p for sig in _FOLLOW_UP_CONTINUE_SIGNALS):
        return {
            "intent": Intent.FOLLOW_UP_CONTINUE,
            "target": None,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı devam etmeni istiyor. "
                "Kaldığın EXACT yerden devam et — hiçbir şeyi tekrar açıklama, "
                "özetleme, giriş yapma. Direkt kaldığın noktadan sürdür."
            ),
            "confidence": "high",
        }

    # ── 2. ÖZET ────────────────────────────────────────────────
    if any(sig in p for sig in _FOLLOW_UP_SUMMARY_SIGNALS):
        return {
            "intent": Intent.FOLLOW_UP_SUMMARY,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı sadece özet istiyor. "
                "Kısa, maddeler halinde ana noktaları ver. "
                "Yeniden tam açıklama yapma, kod yazma. "
                "3-5 madde idealdir."
            ),
            "confidence": "high",
        }

    # ── 3. SPESİFİK KISIM SORUSU ──────────────────────────────
    # "o/bu X nasıl çalışıyor?" kombinasyonu da specific sayılır
    _has_referential = any(ref in p for ref in ("o ", "bu ", "şu ", "that ", "this "))
    _has_explain_q   = any(sig in p for sig in _CODE_EXPLAIN_SIGNALS)
    _is_specific_combo = _has_referential and _has_explain_q and has_history

    if (any(sig in p for sig in _FOLLOW_UP_SPECIFIC_SIGNALS) or _is_specific_combo) and has_history:
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.FOLLOW_UP_SPECIFIC,
            "target": target,
            "has_prior_context": True,
            "response_strategy": (
                f"Kullanıcı önceki cevabın SADECE '{target or 'belirtilen'}' "
                f"kısmını soruyor. "
                "YALNIZCA o kısmı açıkla — cerrahi odak. "
                "Önceki cevabın geri kalanını tekrar etme. "
                "Tüm mimariyi baştan anlatma. "
                "Kısa referansla başla, sonra sadece o noktayı detaylandır."
            ),
            "confidence": "high",
        }

    # ── 4. NEDEN? ──────────────────────────────────────────────
    _is_why = (
        any(sig in p for sig in _FOLLOW_UP_WHY_SIGNALS) or
        (any(sig in p for sig in _FOLLOW_UP_WHY_SHORT) and is_short)
    )
    if _is_why and has_history:
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.FOLLOW_UP_WHY,
            "target": target,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı bir kararın/yaklaşımın NEDEN'ini soruyor. "
                "Sadece o kararın mantığını açıkla — 2-4 cümle yeterli. "
                "Tüm çözümü yeniden sunma."
            ),
            "confidence": "high",
        }

    # ── 5. DAHA BASİT ANLAT ───────────────────────────────────
    if any(sig in p for sig in _FOLLOW_UP_SIMPLIFY_SIGNALS):
        return {
            "intent": Intent.FOLLOW_UP_SIMPLIFY,
            "target": None,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı önceki açıklamayı anlamadı. "
                "FARKLI bir yaklaşımla, daha basit dille açıkla. "
                "Aynı cümleleri tekrar etme — gerçek bir analoji veya "
                "somut örnek kullan. Teknik jargonu azalt."
            ),
            "confidence": "high",
        }

    # ── 6. DAHA DETAYLI ───────────────────────────────────────
    if any(sig in p for sig in _FOLLOW_UP_EXPAND_SIGNALS) and has_history:
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.FOLLOW_UP_EXPAND,
            "target": target,
            "has_prior_context": True,
            "response_strategy": (
                f"Kullanıcı '{target or 'konu'}' hakkında daha fazla detay istiyor. "
                "Önceki cevabın üzerine inşa et — baştan yazmadan genişlet. "
                "Örnekler, edge case'ler veya ilgili alt konular ekleyebilirsin."
            ),
            "confidence": "medium",
        }

    # ── 7. DEBUG / HATA ANALIZI ───────────────────────────────
    if any(sig in p for sig in _DEBUG_SIGNALS):
        return {
            "intent": Intent.DEBUG_REQUEST,
            "target": None,
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı bir hata/sorun bildiriyor. "
                "Şu akışı uygula: "
                "1) 🔍 Problemi 1-2 cümleyle özetle "
                "2) 💡 Root cause — neden oluştuğunu açıkla "
                "3) 🔧 Somut çözüm adımları "
                "4) ✅ Nasıl doğrulanacağını söyle. "
                "Kullanıcı sadece hatayı paylaştıysa çözüme atla — "
                "uzun açıklama yapma."
            ),
            "confidence": "high",
        }

    # ── 8. KOD AÇIKLAMA ───────────────────────────────────────
    if any(sig in p for sig in _CODE_EXPLAIN_SIGNALS) and (has_code_context or mode == "code"):
        target = _extract_target(prompt, history)
        is_specific = target is not None
        return {
            "intent": Intent.CODE_EXPLAIN if not is_specific else Intent.CODE_SECTION,
            "target": target,
            "has_prior_context": has_code_context,
            "response_strategy": (
                f"Kullanıcı {'`' + target + '`' if target else 'kodu'} anlamak istiyor. "
                + (f"SADECE {target} kısmını açıkla, tüm dosyayı değil. " if is_specific else "")
                + "Satır satır veya mantık akışıyla açıkla. "
                "Kodu yeniden yazma — sadece açıkla. "
                "Gerçek dünya analogisi veya somut örnek ekle."
            ),
            "confidence": "high" if is_specific else "medium",
        }

    # ── 9. KOD DEĞİŞTİRME ─────────────────────────────────────
    if any(sig in p for sig in _CODE_MODIFY_SIGNALS) and (has_code_context or mode == "code"):
        return {
            "intent": Intent.CODE_MODIFY,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı mevcut kodu değiştirmek istiyor. "
                "[CODE CONTEXT]'teki veya son paylaşılan kodu baz al. "
                "Sadece değişen kısımları açıkla, tüm dosyayı yeniden yaz "
                "(eğer birden fazla bölüm değişiyorsa tam dosyayı ver). "
                "Değişiklikleri madde madde listele."
            ),
            "confidence": "high",
        }

    # ── 10. YENİ KOD YAZMA ────────────────────────────────────
    if any(sig in p for sig in _CODE_GENERATE_SIGNALS) and mode in ("code", "it_expert"):
        return {
            "intent": Intent.CODE_GENERATE,
            "target": None,
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı yeni kod yazmanı istiyor. "
                "Tam, eksiksiz, production-ready kod yaz. "
                "ASLA truncate etme — 500+ satır gerekiyorsa yaz. "
                "Kısa açıklama + tam kod + bağımlılıklar."
            ),
            "confidence": "high",
        }

    # ── 11. KAVRAM ÖĞRENME ────────────────────────────────────
    if any(sig in p for sig in _CONCEPT_LEARN_SIGNALS):
        return {
            "intent": Intent.CONCEPT_LEARN,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı bir kavram/teknolojiyi öğrenmek istiyor. "
                "Önce basit versiyonu ver (1-2 cümle). "
                "Sonra gerçek dünya analogisi ile açıkla. "
                "Ardından somut örnek göster. "
                "Tüm detayları bir seferde dökme — katmanlı öğret."
            ),
            "confidence": "medium",
        }

    # ── 12. YENİ KONU ─────────────────────────────────────────
    if _is_new_topic(prompt, history):
        return {
            "intent": Intent.NEW_TOPIC,
            "target": None,
            "has_prior_context": False,
            "response_strategy": (
                "Bu konuşma bağlamıyla ilgisiz yeni bir konu. "
                "Fresh start — önceki konuyu referans verme. "
                "Soruyu doğrudan ve tam olarak cevapla."
            ),
            "confidence": "low",
        }

    # ── 13. VARSAYILAN: GENEL SOHBET ──────────────────────────
    return {
        "intent": Intent.CHAT,
        "target": None,
        "has_prior_context": has_history,
        "response_strategy": (
            "Kullanıcının sorusunu doğrudan cevapla. "
            "Konuşma geçmişini dikkate al. "
            "Sohbet akışını koru."
        ),
        "confidence": "low",
    }


# ─────────────────────────────────────────────────────────────
# REASONING HINT OLUŞTUR
# Bu sistem prompt'una enjekte edilir — LLM ilk bunu görür
# ─────────────────────────────────────────────────────────────

def build_reasoning_hint(
    prompt: str,
    history: List[Dict],
    mode: str = "assistant",
) -> str:
    """
    Intent'i classify et ve LLM için reasoning hint üret.
    Bu string sistem prompt'unun EN BAŞINA eklenir.
    """
    result = classify_intent(prompt, history, mode)
    intent  = result["intent"]
    target  = result["target"]
    strategy = result["response_strategy"]

    # Emoji map
    intent_labels = {
        Intent.FOLLOW_UP_SPECIFIC: "📍 TAKİP — SPESİFİK KISIM",
        Intent.FOLLOW_UP_SIMPLIFY: "🔄 TAKİP — BASİTLEŞTİR",
        Intent.FOLLOW_UP_CONTINUE: "▶️  TAKİP — DEVAM ET",
        Intent.FOLLOW_UP_SUMMARY:  "📋 TAKİP — ÖZET",
        Intent.FOLLOW_UP_EXPAND:   "🔍 TAKİP — DETAYLANDIR",
        Intent.FOLLOW_UP_WHY:      "❓ TAKİP — NEDEN",
        Intent.DEBUG_REQUEST:      "🐛 DEBUG — HATA ANALİZİ",
        Intent.CODE_GENERATE:      "⚙️  KOD — YENİ KOD",
        Intent.CODE_EXPLAIN:       "📖 KOD — AÇIKLA",
        Intent.CODE_MODIFY:        "✏️  KOD — DEĞİŞTİR",
        Intent.CODE_SECTION:       "🎯 KOD — SPESİFİK BÖLÜM",
        Intent.CONCEPT_LEARN:      "🎓 ÖĞRENME",
        Intent.HOW_TO:             "🛠️  NASIL YAPILIR",
        Intent.PROBLEM_SOLVE:      "🔧 PROBLEM ÇÖZME",
        Intent.NEW_TOPIC:          "🆕 YENİ KONU",
        Intent.CHAT:               "💬 SOHBET",
    }

    label = intent_labels.get(intent, "💬 SOHBET")

    hint = f"""[REASONING LAYER — {label}]
Tespit edilen niyet: {intent}
{f'Hedef: {target}' if target else ''}
Strateji: {strategy}
[/REASONING LAYER]"""

    return hint


# ─────────────────────────────────────────────────────────────
# THINKING STEPS — intent'e göre
# ─────────────────────────────────────────────────────────────

def get_intent_thinking_steps(
    prompt: str,
    history: List[Dict],
    mode: str,
) -> List[tuple]:
    """
    Intent'e göre uygun thinking steps döner.
    Format: List of (emoji, message) tuples.
    Türkçe/İngilizce otomatik tespit.
    """
    result  = classify_intent(prompt, history, mode)
    intent  = result["intent"]
    is_tr   = any(c in prompt for c in "çğışöüÇĞİŞÖÜ")

    steps_map = {
        Intent.DEBUG_REQUEST: [
            ("🔍", "Hata analiz ediliyor..." if is_tr else "Analyzing the error..."),
            ("💡", "Root cause tespit ediliyor..." if is_tr else "Finding root cause..."),
            ("🔧", "Çözüm hazırlanıyor..." if is_tr else "Preparing solution..."),
        ],
        Intent.FOLLOW_UP_SPECIFIC: [
            ("🎯", "İlgili kısım bulunuyor..." if is_tr else "Locating the specific part..."),
        ],
        Intent.FOLLOW_UP_CONTINUE: [
            ("▶️", "Kaldığım yerden devam ediyorum..." if is_tr else "Continuing from where I left off..."),
        ],
        Intent.CODE_GENERATE: [
            ("🔍", "Gereksinimler analiz ediliyor..." if is_tr else "Analyzing requirements..."),
            ("🔧", "Kod yazılıyor..." if is_tr else "Writing code..."),
        ],
        Intent.CODE_MODIFY: [
            ("🔍", "Mevcut kod inceleniyor..." if is_tr else "Reviewing existing code..."),
            ("✏️", "Değişiklikler uygulanıyor..." if is_tr else "Applying changes..."),
        ],
        Intent.CONCEPT_LEARN: [
            ("🎓", "Kavram hazırlanıyor..." if is_tr else "Preparing explanation..."),
        ],
    }

    return steps_map.get(intent, [
        ("🔍", "Analiz ediliyor..." if is_tr else "Analyzing..."),
    ])