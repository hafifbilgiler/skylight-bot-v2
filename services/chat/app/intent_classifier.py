"""
═══════════════════════════════════════════════════════════════
SKYLIGHT — INTENT CLASSIFIER + REASONING LAYER  v2.0
═══════════════════════════════════════════════════════════════

v2.0 yenilikleri:
  ✅ 13 → 26 intent (2x genişleme)
  ✅ Comparison, Review, Architecture, Performance, Security,
     Opinion, Documentation, Test, Feature, Step-by-Step,
     Example-Based, Emotional intent'leri eklendi
  ✅ Intent → thinking steps bağlantısı güçlendirildi
  ✅ Confidence scoring iyileştirildi
  ✅ Multi-signal detection (tek sinyal yerine kombinasyon)
  ✅ Greeting guard korundu
═══════════════════════════════════════════════════════════════
"""

import re
from typing import Optional, List, Dict, Tuple


# ─────────────────────────────────────────────────────────────
# INTENT TYPES
# ─────────────────────────────────────────────────────────────

class Intent:
    # ── Takip soruları ────────────────────────────────────────
    FOLLOW_UP_SPECIFIC   = "follow_up_specific"   # "bu fonksiyon nasıl?"
    FOLLOW_UP_SIMPLIFY   = "follow_up_simplify"   # "daha basit anlat"
    FOLLOW_UP_CONTINUE   = "follow_up_continue"   # "devam et", "bitir"
    FOLLOW_UP_SUMMARY    = "follow_up_summary"    # "özetle"
    FOLLOW_UP_EXPAND     = "follow_up_expand"     # "daha detaylı"
    FOLLOW_UP_WHY        = "follow_up_why"        # "neden?"
    FOLLOW_UP_EXAMPLE    = "follow_up_example"    # "örnekle göster"
    FOLLOW_UP_COMPARE    = "follow_up_compare"    # "X mi Y mi daha iyi?"

    # ── Teknik istekler ───────────────────────────────────────
    DEBUG_REQUEST        = "debug_request"        # hata analizi
    CODE_GENERATE        = "code_generate"        # yeni kod yaz
    CODE_EXPLAIN         = "code_explain"         # kodu açıkla
    CODE_MODIFY          = "code_modify"          # düzelt/ekle/refactor
    CODE_SECTION         = "code_section"         # spesifik bölüm
    CODE_REVIEW          = "code_review"          # "bu koda bak, sorun var mı?"
    CODE_TEST            = "code_test"            # test yaz
    CODE_DOCS            = "code_docs"            # dokümantasyon yaz

    # ── Mimari & Tasarım ──────────────────────────────────────
    ARCHITECTURE         = "architecture"         # "nasıl tasarlamalıyım?"
    FEATURE_REQUEST      = "feature_request"      # "şu özelliği ekle"
    PERFORMANCE          = "performance"          # "yavaş, optimize et"
    SECURITY             = "security"             # "güvenli mi? açık var mı?"

    # ── Öğrenme ───────────────────────────────────────────────
    CONCEPT_LEARN        = "concept_learn"        # "nedir, nasıl çalışır"
    HOW_TO               = "how_to"               # "nasıl yapılır"
    STEP_BY_STEP         = "step_by_step"         # "adım adım anlat"

    # ── Karar & Görüş ─────────────────────────────────────────
    OPINION_REQUEST      = "opinion_request"      # "ne düşünüyorsun / tavsiye et"
    COMPARISON           = "comparison"           # "X vs Y" — genel karşılaştırma

    # ── Kullanıcı Komutları ───────────────────────────────────
    USER_COMMAND         = "user_command"         # "bana sormadan kod yazma", "türkçe konuş"
    USER_PREFERENCE      = "user_preference"      # "kısa cevap ver", "emoji kullanma"

    # ── Duygusal / Sosyal ─────────────────────────────────────
    EMOTIONAL            = "emotional"            # "seni gördüğüme sevindim", teşekkür, övgü
    FRUSTRATION          = "frustration"          # "saçmalıyorsun", "yanlış yaptın"

    # ── Genel ─────────────────────────────────────────────────
    PROBLEM_SOLVE        = "problem_solve"        # genel problem
    NEW_TOPIC            = "new_topic"            # yeni konu
    CHAT                 = "chat"                 # sohbet


# ─────────────────────────────────────────────────────────────
# SİNYAL SÖZLÜĞÜ
# ─────────────────────────────────────────────────────────────

# Takip sinyalleri
_FOLLOW_UP_SPECIFIC_SIGNALS = (
    "o kısım", "bu kısım", "o fonksiyon", "bu fonksiyon",
    "o satır", "bu satır", "o bölüm", "bu bölüm",
    "o class", "bu class", "o metod", "bu metod",
    "o bloğu", "bu bloğu", "orayı", "burayı",
    "o kodu", "bu kodu", "o kısmı", "bu kısmı",
    "o şeyi", "bunu aç", "onu aç", "orayı aç",
    "that function", "this function", "that part", "this part",
    "that line", "this line", "that section", "this section",
    "that method", "this method", "that class", "this class",
)

_FOLLOW_UP_CONTINUE_SIGNALS = (
    "devam et", "devam", "continue", "bitir", "tamamla",
    "complete", "finish", "geri kalan", "rest of",
    "kaldığın yerden", "yarıda kaldı", "kesmiştin", "durdun",
    "kaldığı yerden", "kaldığımız yerden",
)

_FOLLOW_UP_SUMMARY_SIGNALS = (
    "özetle", "özet ver", "kısaca", "kısa", "sadece özet",
    "genel olarak", "kısalt", "summarize", "summary",
    "briefly", "in short", "tldr", "tl;dr",
)

_FOLLOW_UP_SIMPLIFY_SIGNALS = (
    "daha basit", "basitçe", "daha kolay", "anlamadım",
    "anlayamadım", "kafam karıştı", "daha açık", "daha net",
    "simpler", "simplify", "easier", "don't understand",
    "confused", "more clearly", "sadeleştir",
)

_FOLLOW_UP_EXPAND_SIGNALS = (
    "daha detaylı", "detay ver", "daha fazla", "genişlet",
    "açar mısın", "daha fazla anlat", "elaborate",
    "more detail", "expand on", "tell me more", "more about",
)

_FOLLOW_UP_WHY_SIGNALS = (
    "neden böyle", "neden bu şekilde", "neden kullandın",
    "neden seçtin", "neden tercih", "neden yaptın",
    "neden bunu", "neden şunu", "neden onu",
    "why did you", "why is this", "what's the reason",
    "what's the purpose", "why use", "why not",
)

_FOLLOW_UP_WHY_SHORT = ("neden", "niye", "niçin", "why",)

_FOLLOW_UP_EXAMPLE_SIGNALS = (
    "örnekle göster", "örnek ver", "örneğini göster",
    "örnek kod", "show me an example", "give me an example",
    "example please", "can you show", "nasıl görünür",
    "örnek üzerinden", "örnekle anlat",
)

_FOLLOW_UP_COMPARE_SIGNALS = (
    "hangisi daha iyi", "hangisini kullanmalı", "farkı ne",
    "farkları neler", "mi daha iyi", "mu daha iyi",
    "mı daha iyi", "mü daha iyi",
    "which is better", "what's the difference",
    "vs", "versus", "compare", "karşılaştır",
)

# Teknik sinyaller
_DEBUG_SIGNALS = (
    "hata", "error", "bug", "çalışmıyor", "çöktü", "crash",
    "exception", "traceback", "failed", "başarısız",
    "neden çalışmıyor", "neden hata", "sorun var", "problem var",
    "fix", "debug", "hata alıyorum", "hata veriyor",
    "500", "404", "null pointer", "index error", "key error",
    "crashloopback", "oomkilled", "imagepullbackoff",
)

_CODE_EXPLAIN_SIGNALS = (
    "nasıl çalışıyor", "ne yapıyor", "ne işe yarıyor",
    "açıkla", "anlat", "explain", "how does", "what does",
    "what is", "bu ne", "bu ne yapıyor", "bu nedir",
    "ne anlama geliyor", "anlamı ne",
)

_CODE_MODIFY_SIGNALS = (
    "düzelt", "güncelle", "değiştir", "refactor", "iyileştir",
    "ekle", "kaldır", "sil", "update", "modify",
    "change", "remove", "add feature", "özellik ekle",
    "yeniden yaz", "rewrite",
)

_CODE_GENERATE_SIGNALS = (
    "yaz", "oluştur", "yarat", "create", "generate",
    "write", "make a", "build a", "implement", "yap", "kodla",
    "fonksiyon yaz", "class yaz", "script yaz",
)

_CODE_REVIEW_SIGNALS = (
    "incele", "gözden geçir", "review", "kontrol et",
    "sorun var mı", "hata var mı", "ne düşünüyorsun bu koda",
    "bu kod nasıl", "code review", "eksik var mı",
    "iyileştirebilir misin", "bakabilir misin",
    "check this", "look at this", "what do you think",
)

_CODE_TEST_SIGNALS = (
    "test yaz", "unit test", "test ekle", "test oluştur",
    "write tests", "add tests", "test coverage",
    "pytest", "jest", "test cases", "test senaryosu",
)

_CODE_DOCS_SIGNALS = (
    "dokümantasyon", "documentation", "docstring", "yorum ekle",
    "comment", "readme", "api docs", "swagger", "dökümante et",
    "açıklama ekle", "jsdoc",
)

_ARCHITECTURE_SIGNALS = (
    "nasıl tasarlayabilirim", "nasıl tasarlamalıyım", "nasıl tasarlasam",
    "mimari", "architecture",
    "nasıl yapılandırmalıyım", "design pattern", "pattern",
    "nasıl organize", "proje yapısı", "folder structure",
    "microservice", "monolith", "sistem tasarımı",
    "nasıl kurmalıyım", "best practice",
)

_FEATURE_REQUEST_SIGNALS = (
    "özellik ekle", "feature ekle", "yeni özellik",
    "add feature", "new feature", "implement feature",
    "bunu ekler misin", "şunu ekler misin",
    "entegrasyon ekle", "modül ekle",
)

_PERFORMANCE_SIGNALS = (
    "yavaş", "slow", "optimize", "hızlandır", "performans",
    "performance", "latency", "gecikme", "memory leak",
    "bellek", "cpu yüksek", "timeout", "ağır çalışıyor",
    "daha hızlı", "faster", "bottleneck",
)

_SECURITY_SIGNALS = (
    "güvenli mi", "güvenlik açığı", "vulnerability",
    "sql injection", "xss", "csrf", "güvenlik", "security",
    "exploit", "penetration", "açık var mı", "saldırıya açık",
    "şifreleme", "encryption", "token güvenli mi",
    "is this secure", "security issue",
)

_CONCEPT_LEARN_SIGNALS = (
    "nedir", "ne demek", "öğrenmek istiyorum",
    "öğret", "teach me", "what is", "how to learn",
    "what are", "basics of", "temellerini", "giriş",
    "introduction", "kavramı", "tanım", "definition",
)

_HOW_TO_SIGNALS = (
    "nasıl yapılır", "nasıl yapabilirim", "how to",
    "how do i", "how can i",
)

_STEP_BY_STEP_SIGNALS = (
    "adım adım", "step by step", "aşama aşama",
    "sırasıyla", "önce ne yapmalıyım", "hangi adımlar",
    "walkthrough", "tutorial", "rehber", "guide",
)

_OPINION_SIGNALS = (
    "ne düşünüyorsun", "tavsiye et", "önerirsin",
    "hangisini seçmeli", "senin görüşün", "bence",
    "what do you think", "recommend", "suggest",
    "your opinion", "which should i", "what would you",
    "ne önerirsin", "tercih eder misin",
)

_COMPARISON_SIGNALS = (
    " vs ", " versus ", "karşılaştır", "compare",
    "farkı nedir", "fark ne", "difference between",
    "hangisi", "which one", "better than", "daha iyi",
    "mı daha", "mi daha", "mu daha", "mü daha",
)

_GREETINGS = (
    "selam", "merhaba", "hey", "hi", "hello", "naber",
    "nasılsın", "günaydın", "iyi günler", "iyi akşamlar",
    "iyi geceler", "selamlar", "heyy", "seleam", "slm", "mrb",
    "how are you", "what's up", "sup", "yo ",
)

# Kullanıcı komutları — bunlar hafızaya kaydedilmeli
_USER_COMMAND_SIGNALS = (
    # Kod davranışı
    "sormadan kod yazma", "kod yazma sormadan", "önce sor",
    "izin almadan yazma", "sormadan yapma", "sormadan başlama",
    "don't write code", "ask before", "ask me first",
    "without asking", "don't do anything without",
    # Dil
    "türkçe konuş", "türkçe cevap ver", "türkçe yaz",
    "ingilizce konuş", "ingilizce cevap", "speak turkish",
    "speak english", "respond in turkish", "respond in english",
    # Format
    "kısa cevap ver", "uzun yazma", "madde madde yaz",
    "emoji kullanma", "emoji kullan", "kod bloğu kullan",
    "keep it short", "be concise", "no emoji",
    # Kişilik
    "daha resmi ol", "daha samimi ol", "sen modunda konuş",
    "siz modunda konuş", "formal", "informal",
    # Genel yasaklar
    "bunu yapma", "öyle yapma", "şöyle yapma",
    "sadece şunu yap", "sadece bunu yap",
)

_USER_PREFERENCE_SIGNALS = (
    "her zaman", "bundan sonra", "hep böyle", "artık hep",
    "from now on", "always", "never", "in the future",
    "tercihim", "istiyorum ki", "prefer",
)

# Duygusal sinyaller
_EMOTIONAL_POSITIVE_SIGNALS = (
    "sevindim", "mutlu oldum", "harika", "muhteşem", "mükemmel",
    "teşekkür", "sağ ol", "eyvallah", "süper", "çok iyi",
    "güzel", "bravo", "aferin", "tebrikler", "şahane",
    "thank you", "thanks", "awesome", "great job", "well done",
    "love it", "perfect", "excellent",
)

_EMOTIONAL_NEGATIVE_SIGNALS = (
    "saçmalıyorsun", "yanlış", "berbat", "rezalet", "olmadı",
    "beğenmedim", "beklediğim bu değil", "hayır hayır",
    "you're wrong", "that's not right", "terrible",
    "sinir bozucu", "bıktım", "artık olmaz",
)

_FRUSTRATION_SIGNALS = (
    "anlamıyorsun", "dinlemiyorsun", "söyledim ama", "kaç kere",
    "yine aynı hatayı", "hâlâ aynı", "hala aynı",
    "you don't understand", "i told you", "again", "still wrong",
    "not listening",
)


# ─────────────────────────────────────────────────────────────
# YARDIMCI FONKSİYONLAR
# ─────────────────────────────────────────────────────────────

def _has_recent_code(history: List[Dict], lookback: int = 4) -> bool:
    for msg in history[-lookback:]:
        if "```" in msg.get("content", ""):
            return True
    return False


def _has_recent_technical_answer(history: List[Dict], lookback: int = 2) -> bool:
    for msg in reversed(history[-lookback * 2:]):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if len(content) > 300 or "```" in content:
                return True
    return False


def _extract_target(prompt: str, history: List[Dict]) -> Optional[str]:
    backtick = re.search(r'`([^`]+)`', prompt)
    if backtick:
        return backtick.group(1)

    patterns = [
        r'(\w+)\s+fonksiyonu', r'(\w+)\s+function',
        r'(\w+)\s+class',      r'(\w+)\s+metodu',
        r'(\w+)\s+method',     r'(\w+)\s+kısmı',
        r'(\w+)\s+bölümü',     r'(\w+)\s+satırı',
        r'def\s+(\w+)',        r'class\s+(\w+)',
    ]
    for pat in patterns:
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            return m.group(0)

    for msg in reversed(history[-4:]):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            code_m = re.search(r'```\w*\s*\n?#\s*(.+)', content)
            if code_m:
                return code_m.group(1).strip()[:50]
            break

    return None


def _extract_last_topic(history: List[Dict]) -> str:
    """
    Son asistan cevabından konuyu çıkar.
    Kısa follow-up'larda reasoning hint'e inject edilir.
    "kendim nasıl yaparım hesabı" → "namaz vakti hesaplama" bağlamı korunur.
    """
    for msg in reversed(history[-6:]):
        if msg.get("role") == "assistant":
            content = msg.get("content", "").strip()
            # İlk anlamlı satırı al
            for line in content.split("\n"):
                line = line.strip()
                if len(line) > 20 and not line.startswith("#"):
                    return line[:120]
    return ""


def _is_short_followup(prompt: str, history: List[Dict]) -> bool:
    """
    Kısa mesaj (≤7 kelime) + geçmişte asistan cevabı varsa → follow-up say.

    PROBLEM: "kendim nasıl yaparım hesabı" (5 kelime) → CHAT intent düşüyor
             Kelime overlap düşük → _is_new_topic True dönüyor
    FIX:     Kısa mesaj + geçmiş varsa → her zaman önceki konunun devamı.

    İstisnalar (follow-up SAYILMAZ):
    - Selamlama
    - Açıkça yeni konu başlatan kelimeler
    """
    words = prompt.strip().split()
    if len(words) > 7:
        return False
    if not history:
        return False

    p = prompt.lower().strip()

    # Selamlama ise follow-up değil
    _GREETINGS_SET = {
        "selam", "merhaba", "hey", "hi", "hello", "naber",
        "nasılsın", "günaydın", "iyi günler", "slm", "mrb"
    }
    if any(p == g or p.startswith(g) for g in _GREETINGS_SET):
        return False

    # Açıkça yeni konu sinyali
    _NEW_TOPIC_SIGNALS = (
        "başka bir şey", "farklı bir konu", "yeni soru",
        "bir de şunu", "şimdi şunu", "another thing", "new question",
    )
    if any(sig in p for sig in _NEW_TOPIC_SIGNALS):
        return False

    # Son mesajlar arasında asistan cevabı var mı?
    return any(m.get("role") == "assistant" for m in history[-4:])


def _is_new_topic(prompt: str, history: List[Dict]) -> bool:
    if not history:
        return True

    # Kısa mesaj asla yeni konu sayılmaz — _is_short_followup halleder
    if len(prompt.split()) <= 7:
        return False

    p_words = set(prompt.lower().split())
    prev_words = set()
    for msg in history[-3:]:
        prev_words.update(msg.get("content", "").lower().split())
    stopwords = {
        "ne", "bu", "şu", "o", "bir", "var", "yok", "ve", "de", "da",
        "the", "a", "an", "is", "it", "what", "how", "why", "can",
        "could", "mi", "mı", "mu", "mü", "için", "ile",
    }
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
    Kullanıcının niyetini LOCAL olarak sınıflandırır. 0ms, network yok.

    Döner: { intent, target, has_prior_context, response_strategy, confidence }
    """
    p = prompt.lower().strip()
    has_code_context = _has_recent_code(history)
    has_history      = len(history) >= 1

    # ══════════════════════════════════════════════════════════
    # 0. GREETING GUARD — her şeyden önce
    # ══════════════════════════════════════════════════════════
    if any(p == g or p.startswith(g) for g in _GREETINGS):
        return {
            "intent": Intent.CHAT,
            "target": None,
            "has_prior_context": False,
            "response_strategy": (
                "Kullanıcı sohbet başlatıyor. Samimi ve kısa karşılık ver. "
                "Önceki konuşmayı referans verme. Proaktif yardım öner ama kısa tut."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 0b. KULLANICI KOMUTU — hafızaya kaydedilmeli
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _USER_COMMAND_SIGNALS):
        # Komutu çıkar
        command_text = prompt.strip()
        is_persistent = any(sig in p for sig in _USER_PREFERENCE_SIGNALS)
        return {
            "intent": Intent.USER_COMMAND,
            "target": command_text,
            "has_prior_context": has_history,
            "is_persistent": is_persistent,  # "bundan sonra hep böyle" → DB'ye yaz
            "response_strategy": (
                f"Kullanıcı bir komut/kural belirledi: '{command_text}'. "
                "BU KURALI HEMEN UYGULA ve kısa onay ver. "
                "Örn: 'Anladım, bundan sonra sormadan kod yazmayacağım.' "
                "Açıklama yapma, özür dileme, sadece onayla ve uygula. "
                "Bu kural bu konuşmanın geri kalanında geçerli."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 0c. DUYGUSAL / SOSYAL MESAJ
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _EMOTIONAL_POSITIVE_SIGNALS):
        return {
            "intent": Intent.EMOTIONAL,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı duygusal/sosyal bir mesaj gönderiyor. "
                "Kısa, samimi ve sıcak karşılık ver. "
                "KESİNLİKLE KOD YAZMA — bunun için gerek yok. "
                "1-2 cümle yeterli. "
                "Kullanıcı ne yapmak istediğini sormak istiyorsa kısaca sor."
            ),
            "confidence": "high",
        }

    if any(sig in p for sig in _FRUSTRATION_SIGNALS):
        return {
            "intent": Intent.FRUSTRATION,
            "target": None,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı hayal kırıklığı veya sinirlilik yaşıyor. "
                "Savunmaya geçme. Özür dile, ne anlamadığını sor. "
                "Kısa, sakin ve empatik ol. "
                "Sorunu net anlamadan bir daha deneme."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 1. DEVAM ET
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _FOLLOW_UP_CONTINUE_SIGNALS):
        return {
            "intent": Intent.FOLLOW_UP_CONTINUE,
            "target": None,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı devam etmeni istiyor. "
                "Kaldığın EXACT yerden devam et. "
                "Hiçbir şeyi tekrar açıklama, özetleme, giriş yapma."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 2. ÖZET
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _FOLLOW_UP_SUMMARY_SIGNALS):
        return {
            "intent": Intent.FOLLOW_UP_SUMMARY,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı sadece özet istiyor. "
                "3-5 madde, kısa. Yeniden açıklama yapma, kod yazma."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 3. SPESİFİK KISIM (referans + açıklama sinyali birlikte)
    # ══════════════════════════════════════════════════════════
    _has_referential   = any(ref in p for ref in ("o ", "bu ", "şu ", "that ", "this "))
    _has_explain_q     = any(sig in p for sig in _CODE_EXPLAIN_SIGNALS)
    _is_specific_combo = _has_referential and _has_explain_q and has_history

    if (any(sig in p for sig in _FOLLOW_UP_SPECIFIC_SIGNALS) or _is_specific_combo) and has_history:
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.FOLLOW_UP_SPECIFIC,
            "target": target,
            "has_prior_context": True,
            "response_strategy": (
                f"Kullanıcı SADECE '{target or 'belirtilen'}' kısmını soruyor. "
                "YALNIZCA o kısmı açıkla — cerrahi odak. "
                "Önceki cevabın geri kalanını tekrar etme."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 4. NEDEN?
    # ══════════════════════════════════════════════════════════
    _is_why = (
        any(sig in p for sig in _FOLLOW_UP_WHY_SIGNALS) or
        (any(sig in p for sig in _FOLLOW_UP_WHY_SHORT) and len(p.split()) <= 3)
    )
    if _is_why and has_history:
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.FOLLOW_UP_WHY,
            "target": target,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı bir kararın/yaklaşımın NEDEN'ini soruyor. "
                "Sadece o kararın mantığını açıkla — 2-4 cümle. "
                "Tüm çözümü yeniden sunma."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 5. ÖRNEK İSTE
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _FOLLOW_UP_EXAMPLE_SIGNALS):
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.FOLLOW_UP_EXAMPLE,
            "target": target,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı somut bir örnek istiyor. "
                "Önce kısa açıklama, sonra çalışan kod örneği ver. "
                "Örneği gerçekçi ve minimal tut — toy example değil, "
                "gerçek dünyaya yakın."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 6. KARŞILAŞTIRMA (follow-up context'te)
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _FOLLOW_UP_COMPARE_SIGNALS) and has_history:
        return {
            "intent": Intent.FOLLOW_UP_COMPARE,
            "target": None,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı iki şeyi karşılaştırmak istiyor. "
                "Tablo veya madde madde karşılaştır. "
                "Sonunda 'hangisi ne zaman kullanılır' öner."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 7. BASİTLEŞTİR
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _FOLLOW_UP_SIMPLIFY_SIGNALS):
        return {
            "intent": Intent.FOLLOW_UP_SIMPLIFY,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı önceki açıklamayı anlamadı. "
                "FARKLI bir yaklaşımla, daha basit dille açıkla. "
                "Aynı cümleleri tekrar etme — gerçek bir analoji kullan."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 8. DETAYLANDIR
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _FOLLOW_UP_EXPAND_SIGNALS) and has_history:
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.FOLLOW_UP_EXPAND,
            "target": target,
            "has_prior_context": True,
            "response_strategy": (
                f"Kullanıcı '{target or 'konu'}' hakkında daha fazla detay istiyor. "
                "Önceki cevabın üzerine inşa et — baştan yazmadan genişlet."
            ),
            "confidence": "medium",
        }

    # ══════════════════════════════════════════════════════════
    # 9. CODE REVIEW — debug'dan önce (daha spesifik)
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _CODE_REVIEW_SIGNALS) and (has_code_context or "```" in prompt):
        return {
            "intent": Intent.CODE_REVIEW,
            "target": None,
            "has_prior_context": True,
            "response_strategy": (
                "Kullanıcı kod review istiyor. "
                "Şu sırayla incele: "
                "1) Mantık hataları ve bug'lar "
                "2) Güvenlik sorunları "
                "3) Performans sorunları "
                "4) Kod kalitesi (okunabilirlik, naming, DRY) "
                "5) İyileştirme önerileri. "
                "Her madde için somut düzeltme kodu ver."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 9b. GÜVENLİK
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _SECURITY_SIGNALS):
        return {
            "intent": Intent.SECURITY,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı güvenlik sorunu soruyor. "
                "1) Mevcut güvenlik açıklarını listele "
                "2) Her açık için risk seviyesi belirt (Yüksek/Orta/Düşük) "
                "3) Somut düzeltme kodunu ver. "
                "Güvenlik konusunda net ve direkt ol."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 10. PERFORMANS
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _PERFORMANCE_SIGNALS):
        return {
            "intent": Intent.PERFORMANCE,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı performans sorunu yaşıyor veya optimizasyon istiyor. "
                "1) Bottleneck'i tespit et "
                "2) Ölçülebilir iyileştirme öner "
                "3) Önce/sonra karşılaştırmalı göster. "
                "Premature optimization yapma — gerçek darboğaza odaklan."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 11. DEBUG
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _DEBUG_SIGNALS):
        return {
            "intent": Intent.DEBUG_REQUEST,
            "target": None,
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı hata/sorun bildiriyor. "
                "1) 🔍 Problemi 1-2 cümleyle özetle "
                "2) 💡 Root cause — neden oluştuğunu açıkla "
                "3) 🔧 Somut çözüm adımları "
                "4) ✅ Nasıl test edileceğini söyle."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 12. TEST YAZ
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _CODE_TEST_SIGNALS):
        return {
            "intent": Intent.CODE_TEST,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı test yazmanı istiyor. "
                "[CODE CONTEXT]'teki kodu baz al. "
                "Unit test + edge case + happy path yaz. "
                "Mock/patch gerekiyorsa ekle. "
                "Test framework kullanıcının stack'ine uygun seç (pytest/jest vb.)."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 14. DOKÜMANTASYON
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _CODE_DOCS_SIGNALS):
        return {
            "intent": Intent.CODE_DOCS,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı dokümantasyon istiyor. "
                "Mevcut kodu baz al, her fonksiyon/class için "
                "docstring/JSDoc yaz. "
                "Parametre tipleri, return değerleri, örnek kullanım ekle."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 15. MİMARİ & TASARIM
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _ARCHITECTURE_SIGNALS):
        return {
            "intent": Intent.ARCHITECTURE,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı mimari/tasarım sorusu soruyor. "
                "1) Önce seçenekleri sun (2-3 yaklaşım) "
                "2) Trade-off'ları açıkla (avantaj/dezavantaj) "
                "3) Kullanıcının durumuna göre tavsiye ver "
                "4) Somut başlangıç noktası göster. "
                "Tek doğru cevap yok — bağlamı analiz et."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 16. ÖZELLIK EKLEME
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _FEATURE_REQUEST_SIGNALS):
        return {
            "intent": Intent.FEATURE_REQUEST,
            "target": None,
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı mevcut sisteme özellik eklemek istiyor. "
                "[CODE CONTEXT]'i incele, mevcut yapıyla uyumlu olarak ekle. "
                "Sadece değişen/eklenen kısımları göster — tüm dosyayı yeniden yazma. "
                "(Eğer bütüncül değişiklik gerekiyorsa tam dosyayı ver.)"
            ),
            "confidence": "medium",
        }

    # ══════════════════════════════════════════════════════════
    # 17. KOD AÇIKLAMA (spesifik değil, genel)
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _CODE_EXPLAIN_SIGNALS) and (has_code_context or mode == "code"):
        target = _extract_target(prompt, history)
        return {
            "intent": Intent.CODE_EXPLAIN,
            "target": target,
            "has_prior_context": has_code_context,
            "response_strategy": (
                f"Kullanıcı {'`' + target + '`' if target else 'kodu'} anlamak istiyor. "
                "Satır satır veya mantık akışıyla açıkla. "
                "Kodu yeniden yazma — sadece açıkla. "
                "Gerçek dünya analogisi veya somut örnek ekle."
            ),
            "confidence": "medium",
        }

    # ══════════════════════════════════════════════════════════
    # 18. KOD DEĞİŞTİRME
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _CODE_MODIFY_SIGNALS) and (has_code_context or mode == "code"):
        return {
            "intent": Intent.CODE_MODIFY,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı mevcut kodu değiştirmek istiyor. "
                "[CODE CONTEXT]'teki kodu baz al. "
                "Değişiklikleri madde madde listele."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 19. YENİ KOD YAZMA
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _CODE_GENERATE_SIGNALS) and mode in ("code", "it_expert", "assistant"):
        return {
            "intent": Intent.CODE_GENERATE,
            "target": None,
            "has_prior_context": has_code_context,
            "response_strategy": (
                "Kullanıcı yeni kod yazmanı istiyor. "
                "Tam, eksiksiz, production-ready kod yaz. "
                "ASLA truncate etme — 500+ satır gerekiyorsa yaz."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 20. ADIM ADIM
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _STEP_BY_STEP_SIGNALS):
        return {
            "intent": Intent.STEP_BY_STEP,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı adım adım açıklama istiyor. "
                "Her adımı numaralı ver. "
                "Her adımda 'neden' kısmını da açıkla. "
                "Hepsini bir seferde dökme — adım adım ilerle."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 21. GENEL KARŞILAŞTIRMA
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _COMPARISON_SIGNALS):
        return {
            "intent": Intent.COMPARISON,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı iki şeyi karşılaştırmak istiyor. "
                "Tablo veya madde madde karşılaştır. "
                "Tarafsız ol — her ikisinin güçlü/zayıf yanlarını ver. "
                "Sonunda use case'e göre tavsiye et."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 22. GÖRÜş / TAVSİYE
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _OPINION_SIGNALS):
        return {
            "intent": Intent.OPINION_REQUEST,
            "target": None,
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı görüş veya tavsiye istiyor. "
                "Önce seçenekleri sun, sonra net bir tavsiye ver. "
                "Kararsız kalma — bağlamı analiz edip net yön göster. "
                "'Bu durumda X'i tavsiye ederim çünkü...' formatı kullan."
            ),
            "confidence": "medium",
        }

    # ══════════════════════════════════════════════════════════
    # 23. KAVRAM ÖĞRENME
    # ══════════════════════════════════════════════════════════
    if any(sig in p for sig in _CONCEPT_LEARN_SIGNALS):
        return {
            "intent": Intent.CONCEPT_LEARN,
            "target": _extract_target(prompt, history),
            "has_prior_context": has_history,
            "response_strategy": (
                "Kullanıcı bir kavramı öğrenmek istiyor. "
                "Önce basit versiyonu ver (1-2 cümle). "
                "Sonra gerçek dünya analogisi ile açıkla. "
                "Ardından somut örnek göster. "
                "Tüm detayları bir seferde dökme — katmanlı öğret."
            ),
            "confidence": "medium",
        }

    # ══════════════════════════════════════════════════════════
    # 23b. KISA MESAJ + GEÇMİŞ → FOLLOW-UP
    # ══════════════════════════════════════════════════════════
    # "kendim nasıl yaparım hesabı", "peki nasıl", "kod göster"
    # gibi kısa mesajlar önceki konunun devamıdır.
    # Kelime overlap düşük olsa bile bağlamı koru.
    # ══════════════════════════════════════════════════════════
    if _is_short_followup(prompt, history):
        last_topic = _extract_last_topic(history)
        return {
            "intent": Intent.FOLLOW_UP_SPECIFIC,
            "target": last_topic[:60] if last_topic else None,
            "has_prior_context": True,
            "response_strategy": (
                f"Kısa takip sorusu. Önceki konu: '{last_topic[:80] if last_topic else 'önceki konu'}'. "
                "Kullanıcı o konunun devamını soruyor — önceki bağlamı kullan. "
                "Soruyu önceki konuyla ilişkilendirerek cevapla. "
                "Yeni bir konuymuş gibi davranma."
            ),
            "confidence": "high",
        }

    # ══════════════════════════════════════════════════════════
    # 24. YENİ KONU
    # ══════════════════════════════════════════════════════════
    if _is_new_topic(prompt, history):
        return {
            "intent": Intent.NEW_TOPIC,
            "target": None,
            "has_prior_context": False,
            "response_strategy": (
                "Yeni bir konu. Fresh start — önceki konuyu referans verme. "
                "Soruyu doğrudan ve tam olarak cevapla."
            ),
            "confidence": "low",
        }

    # ══════════════════════════════════════════════════════════
    # 25. VARSAYILAN: SOHBET
    # ══════════════════════════════════════════════════════════
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
# REASONING HINT — sistem prompt'a enjekte edilir
# ─────────────────────────────────────────────────────────────

def build_reasoning_hint(
    prompt: str,
    history: List[Dict],
    mode: str = "assistant",
) -> str:
    result   = classify_intent(prompt, history, mode)
    intent   = result["intent"]
    target   = result["target"]
    strategy = result["response_strategy"]

    _LABELS = {
        Intent.FOLLOW_UP_SPECIFIC:  "📍 TAKİP — SPESİFİK KISIM",
        Intent.FOLLOW_UP_SIMPLIFY:  "🔄 TAKİP — BASİTLEŞTİR",
        Intent.FOLLOW_UP_CONTINUE:  "▶️  TAKİP — DEVAM ET",
        Intent.FOLLOW_UP_SUMMARY:   "📋 TAKİP — ÖZET",
        Intent.FOLLOW_UP_EXPAND:    "🔍 TAKİP — DETAYLANDIR",
        Intent.FOLLOW_UP_WHY:       "❓ TAKİP — NEDEN",
        Intent.FOLLOW_UP_EXAMPLE:   "💡 TAKİP — ÖRNEK İSTE",
        Intent.FOLLOW_UP_COMPARE:   "⚖️  TAKİP — KARŞILAŞTIR",
        Intent.DEBUG_REQUEST:       "🐛 DEBUG — HATA ANALİZİ",
        Intent.CODE_GENERATE:       "⚙️  KOD — YENİ KOD",
        Intent.CODE_EXPLAIN:        "📖 KOD — AÇIKLA",
        Intent.CODE_MODIFY:         "✏️  KOD — DEĞİŞTİR",
        Intent.CODE_SECTION:        "🎯 KOD — SPESİFİK BÖLÜM",
        Intent.CODE_REVIEW:         "🔬 KOD — REVIEW",
        Intent.CODE_TEST:           "🧪 KOD — TEST YAZ",
        Intent.CODE_DOCS:           "📝 KOD — DÖKÜMANTASYON",
        Intent.ARCHITECTURE:        "🏗️  MİMARİ & TASARIM",
        Intent.FEATURE_REQUEST:     "✨ ÖZELLİK EKLE",
        Intent.PERFORMANCE:         "⚡ PERFORMANS",
        Intent.SECURITY:            "🔒 GÜVENLİK",
        Intent.CONCEPT_LEARN:       "🎓 ÖĞRENME",
        Intent.STEP_BY_STEP:        "📶 ADIM ADIM",
        Intent.COMPARISON:          "⚖️  KARŞILAŞTIRMA",
        Intent.OPINION_REQUEST:     "💭 GÖRÜş / TAVSİYE",
        Intent.USER_COMMAND:        "⚡ KULLANICI KOMUTU",
        Intent.USER_PREFERENCE:     "⚙️  KULLANICI TERCİHİ",
        Intent.EMOTIONAL:           "💛 DUYGUSAL",
        Intent.FRUSTRATION:         "😤 HAYAL KIRIKLIGI",
        Intent.NEW_TOPIC:           "🆕 YENİ KONU",
        Intent.CHAT:                "💬 SOHBET",
    }

    label = _LABELS.get(intent, "💬 SOHBET")

    return (
        f"[REASONING LAYER — {label}]\n"
        f"Niyet: {intent}\n"
        + (f"Hedef: {target}\n" if target else "")
        + f"Strateji: {strategy}\n"
        f"[/REASONING LAYER]"
    )


# ─────────────────────────────────────────────────────────────
# THINKING STEPS — intent bazlı
# ─────────────────────────────────────────────────────────────

def get_intent_thinking_steps(
    prompt: str,
    history: List[Dict],
    mode: str,
) -> List[tuple]:
    """
    Intent'e göre uygun thinking steps döner.
    Sadece gerçekten faydalı olduğu durumlarda adım göster.
    """
    result = classify_intent(prompt, history, mode)
    intent = result["intent"]
    is_tr  = any(c in prompt for c in "çğışöüÇĞİŞÖÜ")

    _STEPS = {
        Intent.DEBUG_REQUEST: [
            ("🔍", "Hata analiz ediliyor..."        if is_tr else "Analyzing the error..."),
            ("💡", "Root cause tespit ediliyor..."  if is_tr else "Finding root cause..."),
            ("🔧", "Çözüm hazırlanıyor..."          if is_tr else "Preparing fix..."),
        ],
        Intent.CODE_REVIEW: [
            ("🔬", "Kod inceleniyor..."              if is_tr else "Reviewing code..."),
            ("🔍", "Sorunlar tespit ediliyor..."     if is_tr else "Finding issues..."),
            ("✅", "Öneriler hazırlanıyor..."        if is_tr else "Preparing suggestions..."),
        ],
        Intent.SECURITY: [
            ("🔒", "Güvenlik taranıyor..."           if is_tr else "Scanning security..."),
            ("⚠️", "Açıklar tespit ediliyor..."     if is_tr else "Finding vulnerabilities..."),
            ("🔧", "Düzeltmeler hazırlanıyor..."     if is_tr else "Preparing fixes..."),
        ],
        Intent.PERFORMANCE: [
            ("⚡", "Bottleneck analizi..."           if is_tr else "Analyzing bottleneck..."),
            ("🔧", "Optimizasyon hazırlanıyor..."    if is_tr else "Preparing optimization..."),
        ],
        Intent.ARCHITECTURE: [
            ("🏗️", "Mimari seçenekler değerlendiriliyor..." if is_tr else "Evaluating options..."),
            ("⚖️", "Trade-off'lar analiz ediliyor..." if is_tr else "Analyzing trade-offs..."),
        ],
        Intent.CODE_GENERATE: [
            ("🔍", "Gereksinimler analiz ediliyor..." if is_tr else "Analyzing requirements..."),
            ("🔧", "Kod yazılıyor..."                if is_tr else "Writing code..."),
        ],
        Intent.FOLLOW_UP_SPECIFIC: [
            ("🎯", "İlgili kısım bulunuyor..."       if is_tr else "Locating the part..."),
        ],
        Intent.FOLLOW_UP_CONTINUE: [
            ("▶️", "Kaldığım yerden devam..."        if is_tr else "Continuing from where I left..."),
        ],
    }

    # Bu intent'lerde thinking step gösterme
    _NO_STEPS = {
        Intent.CHAT, Intent.FOLLOW_UP_SUMMARY, Intent.FOLLOW_UP_WHY,
        Intent.FOLLOW_UP_SIMPLIFY, Intent.FOLLOW_UP_EXPAND,
        Intent.FOLLOW_UP_EXAMPLE, Intent.CONCEPT_LEARN,
        Intent.NEW_TOPIC, Intent.OPINION_REQUEST,
        Intent.EMOTIONAL, Intent.FRUSTRATION,
        Intent.USER_COMMAND, Intent.USER_PREFERENCE,
    }

    if intent in _NO_STEPS:
        return []

    return _STEPS.get(intent, [])