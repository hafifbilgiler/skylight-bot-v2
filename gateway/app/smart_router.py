"""
═══════════════════════════════════════════════════════════════
SKYLIGHT — SMART ROUTER v4.0
Chain-of-Thought reasoning — Claude gibi adım adım düşünme
═══════════════════════════════════════════════════════════════

Akış:
  1. DİL & BAĞLAM   → Kullanıcı kim, ne diyor, geçmiş ne?
  2. NİYET          → Gerçekte ne istiyor?
  3. ACİLİYET       → Canlı veri gerekli mi?
  4. KAPASİTE       → Hangi model/araç?
  5. BELİRSİZLİK    → Soru sormam gerekiyor mu?
  6. KARAR          → Final yönlendirme

Her adım öncekine dayanır — if/else değil, gerçek akıl yürütme.
═══════════════════════════════════════════════════════════════
"""

import json
import os
import re
from typing import Optional, List, Dict

import httpx

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

DEEPINFRA_API_KEY  = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
ROUTER_MODEL       = os.getenv("DEEPINFRA_ROUTER_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct")
ROUTER_TIMEOUT     = float(os.getenv("ROUTER_TIMEOUT", "5.0"))


# ─────────────────────────────────────────────────────────────
# CHAIN-OF-THOUGHT ROUTER PROMPT
# ─────────────────────────────────────────────────────────────

_COT_SYSTEM = """Sen bir AI asistanın ön-işlemcisisin. Kullanıcının mesajını derinlemesine anlayıp doğru servise yönlendiriyorsun.

ÖNEMLİ: Sen Claude'un "tool use" sistemine benzer çalışıyorsun. Her mesajı şu soruları sorarak değerlendir:
- Bu mesajı yanıtlamak için GERÇEK ZAMANLI veri gerekiyor mu? (kur, hava, haberler)
- Kullanıcı KOD mu istiyor, AÇIKLAMA mı?
- Duygusal destek mi gerekiyor?
- Bağlam ne söylüyor?

ADIM 1 — DİL & KULLANICI PROFİLİ:
  • Kullanıcı hangi dili kullanıyor?
  • Teknik seviyesi ne? (teknik terimler → expert, basit dil → beginner)
  • Tonu ne? (resmi/samimi/sinirli/üzgün)

ADIM 2 — BAĞLAM ANALİZİ:
  • Önceki mesajlar var mı?
  • Bu mesaj bir konuşmanın devamı mı, yoksa yeni bir konu mu?
  • Kısa/belirsiz mesajsa önceki bağlamdan ne anlaşılıyor?

ADIM 3 — GERÇEK NİYET:
  • Kullanıcı GERÇEKTE ne istiyor? (yüzeysel kelimeler değil, asıl amaç)
  • "nasıl yapılır" → açıklama istiyor, KOD değil
  • "yaz/implement et" → direkt kod istiyor
  • "çalışmıyor/hata" → debug istiyor
  • "dolar kaç" → anlık fiyat istiyor (araştırma değil)
  • "bitcoin yatırımı mantıklı mı" → görüş istiyor (fiyat değil)
  • "üzgünüm/yoruldum" → duygusal destek istiyor

ADIM 4 — ACİLİYET & VERİ TÜRÜ:
  • Anlık/güncel veri gerekiyor mu? (kur, hava, haberler) → live_data
  • Araştırma/analiz gerekiyor mu? (son gelişmeler, konu araştırması) → deep_search
  • Statik bilgi yeterli mi? → chat/code
  • Görsel üretim/analiz mi? → image

ADIM 5 — BELİRSİZLİK KONTROLÜ:
  • Mesaj yeterince açık mı?
  • Hangi stack/dil kullanılacak belirsiz mi?
  • Kapsam belirsiz mi? (küçük script mi, tam proje mi?)
  • Belirsizse → sormam gereken soru nedir?

ADIM 6 — KARAR:
  Düşünceni tamamladıktan sonra SADECE şu JSON'u döndür:

{
  "thinking": "<adım adım kısa düşünce özeti, 1-2 cümle>",
  "route": "chat|code|live_data|deep_search|image_gen|image_analyze|emotional",
  "intent": "<intent>",
  "needs_realtime": true|false,
  "tool": "currency|weather|crypto|news|time|web|borsa|none",
  "model": "qwen3|llama4|auto",
  "confidence": "high|medium|low",
  "language": "tr|en|other",
  "user_level": "beginner|intermediate|expert|unknown",
  "ambiguous": true|false,
  "clarification_needed": "<soru metni veya null>"
}

INTENT LİSTESİ:
general_chat | greeting | opinion_request | comparison
how_to | concept_explain | step_by_step | example_request
code_generate | code_debug | code_modify | code_review | code_explain | code_optimize | architecture_design
live_currency | live_weather | live_crypto | live_news | live_time | live_sports | live_borsa
web_search | fact_check | current_events | product_research
image_generate | image_edit | image_analyze
emotional_support | motivation | personal_advice
user_command | user_preference

MODEL SEÇİMİ:
• qwen3  → karmaşık kod, büyük proje, refactor, architecture
• llama4 → genel sohbet, açıklama, araştırma, duygusal destek
• auto   → belirsiz durumlar

ASLA JSON dışında bir şey yazma. thinking alanına düşünceni yaz."""


def _build_cot_prompt(prompt: str, history: List[Dict]) -> str:
    """Chain-of-thought için zengin bağlam."""
    parts = []

    # Bağlam
    if history:
        parts.append("=== SON KONUŞMA BAĞLAMI ===")
        for msg in history[-6:]:  # Son 6 mesaj — daha derin bağlam
            role    = "KULLANICI" if msg.get("role") == "user" else "ASİSTAN"
            content = (msg.get("content") or "")[:200]
            parts.append(f"{role}: {content}")
        parts.append("")

    parts.append(f"=== ANALİZ EDİLECEK MESAJ ===")
    parts.append(prompt[:500])

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# ADIM ADIM REASONING ENGINE
# ─────────────────────────────────────────────────────────────

async def llm_route(prompt: str, history: List[Dict]) -> Optional[Dict]:
    """
    LLM'e chain-of-thought yaptır → JSON karar al.
    """
    if not DEEPINFRA_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT) as client:
            resp = await client.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      ROUTER_MODEL,
                    "messages":   [
                        {"role": "system", "content": _COT_SYSTEM},
                        {"role": "user",   "content": _build_cot_prompt(prompt, history)},
                    ],
                    "max_tokens":  250,   # thinking + JSON için biraz daha fazla
                    "temperature": 0.0,   # Deterministik
                    "stream":      False,
                },
            )

        if resp.status_code != 200:
            print(f"[ROUTER] HTTP {resp.status_code}")
            return None

        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        if not raw:
            return None

        # JSON çıkar — thinking alanı da JSON içinde
        clean = raw.strip()
        if "```" in clean:
            m     = re.search(r"\{.*\}", clean, re.DOTALL)
            clean = m.group(0) if m else clean

        # Sadece JSON kısmını al
        json_match = re.search(r"\{.*\}", clean, re.DOTALL)
        if json_match:
            clean = json_match.group(0)

        result = json.loads(clean)
        result["_source"] = "llm_cot"

        thinking = result.get("thinking", "")
        print(f"[ROUTER] 🧠 {thinking[:80]}")
        print(f"[ROUTER] ✅ {result.get('intent')} → {result.get('route')} "
              f"| conf={result.get('confidence')} "
              f"| model={result.get('model')} "
              f"| lvl={result.get('user_level')}")

        return result

    except json.JSONDecodeError as e:
        print(f"[ROUTER] JSON parse hatası: {e} | raw={raw[:100]}")
        return None
    except Exception as e:
        print(f"[ROUTER] LLM hatası: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# AKILLI KEYWORD FALLBACK
# LLM başarısız olursa devreye girer — o zaman da mantıklı davranır
# ─────────────────────────────────────────────────────────────

def _detect_level(prompt: str) -> str:
    tech = ["api","endpoint","async","await","kubernetes","docker","postgresql",
            "redis","nginx","fastapi","jwt","oauth","microservice","kafka","grpc"]
    n = sum(1 for t in tech if t in prompt.lower())
    return "expert" if n >= 3 else "intermediate" if n >= 1 else "unknown"


def _is_tr(prompt: str) -> bool:
    return any(c in prompt for c in "çğışöüÇĞİŞÖÜ")


def keyword_fallback(prompt: str, history: List[Dict]) -> Dict:
    """
    Adım adım keyword analizi — LLM olmasa da mantıklı karar.
    if/else değil — her adım önceki bilgiye dayanır.
    """
    p     = prompt.lower().strip()
    lang  = "tr" if _is_tr(prompt) else "en"
    level = _detect_level(prompt)

    # ── ADIM 1: Bağlam kontrolü ───────────────────────────────
    # Kısa mesajsa önceki konuşmadan anlam çıkar
    prev_had_code  = False
    prev_had_image = False
    if history and len(p) <= 25:
        for msg in reversed(history[-4:]):
            content = msg.get("content", "")
            if "```" in content:
                prev_had_code = True
                break
            if "[IMAGE_B64]" in content or "görsel" in content.lower():
                prev_had_image = True
                break

        if prev_had_code:
            if any(w in p for w in ["çalışmıyor","hata","olmadı","yine","patladı"]):
                return _mk("code","code_debug","qwen3",conf="high",lang=lang,level=level,
                           thinking="Önceki konuşmada kod var, kullanıcı hata bildiriyor → debug")
            if any(w in p for w in ["değiştir","düzelt","ekle","kaldır","refactor"]):
                return _mk("code","code_modify","qwen3",conf="high",lang=lang,level=level,
                           thinking="Önceki kod bağlamında değişiklik isteği → modify")
            if any(w in p for w in ["devam","bitir","tamam","evet","sonraki"]):
                return _mk("code","code_generate","qwen3",conf="medium",lang=lang,level=level,
                           thinking="Kod yazma devam ediyor → continue")

        if prev_had_image:
            if any(w in p for w in ["beğenmedim","değiştir","farklı","başka","olmadı"]):
                return _mk("image_gen","image_edit","auto",conf="high",lang=lang,level=level,
                           thinking="Görsel bağlamı var, beğenmeme → image_edit")

    # ── ADIM 2: Duygusal içerik ───────────────────────────────
    emotional_kw = ["üzgünüm","mutsuzum","yoruldum","sıkıldım","bunaldım",
                    "depresyon","yalnız","ağlıyorum","stres","kaygı","endişe"]
    if any(w in p for w in emotional_kw):
        return _mk("emotional","emotional_support","llama4",conf="high",lang=lang,level=level,
                   thinking="Duygusal ifadeler tespit edildi → empati modu")

    # ── ADIM 3: Canlı veri — önce fiyat sorgusu mu? ──────────
    price_question = any(w in p for w in ["kaç","fiyat","ne kadar","değer","kur"])

    currency_kw = ["dolar","euro","sterlin","yen","frank","döviz","usd","eur","gbp","chf"]
    if price_question and any(w in p for w in currency_kw):
        return _mk("live_data","live_currency","auto","currency",True,"high",lang,level,
                   thinking="Döviz kuru sorusu, anlık fiyat gerekiyor → live_currency")

    crypto_kw = ["bitcoin","btc","ethereum","eth","solana","sol","bnb","xrp","kripto"]
    if price_question and any(w in p for w in crypto_kw):
        return _mk("live_data","live_crypto","auto","crypto",True,"high",lang,level,
                   thinking="Kripto fiyat sorusu → live_crypto")

    # NOT: "bitcoin yatırımı" fiyat sorusu değil → chat'e düşecek

    weather_kw = ["hava durumu","hava nasıl","sıcaklık","yağmur","kar yağacak","derece"]
    if any(w in p for w in weather_kw):
        return _mk("live_data","live_weather","auto","weather",True,"high",lang,level,
                   thinking="Hava durumu sorusu → live_weather")

    borsa_kw = ["borsa","bist","thyao","garan","akbnk","eregl","hisse senedi"]
    if any(w in p for w in borsa_kw):
        return _mk("live_data","live_borsa","auto","borsa",True,"high",lang,level,
                   thinking="Borsa/hisse sorgusu → live_borsa")

    news_kw = ["son haberler","güncel haber","son dakika","haber oku","manşet"]
    if any(w in p for w in news_kw):
        return _mk("live_data","live_news","auto","news",True,"high",lang,level,
                   thinking="Haber talebi → live_news")

    # ── ADIM 4: Kod niyeti ────────────────────────────────────
    debug_kw = ["çalışmıyor","hata alıyorum","error","traceback","exception",
                "crash","çöktü","patladı","olmadı","başarısız"]
    if any(w in p for w in debug_kw):
        return _mk("code","code_debug","qwen3",conf="high",lang=lang,level=level,
                   thinking="Hata/debug sinyali → code_debug")

    # "nasıl" → önce açıklama isteği mi kod isteği mi?
    howto_kw = ["nasıl yapılır","nasıl yapabilirim","nasıl yaparım","nasıl kurulur","nasıl çalışır"]
    if any(w in p for w in howto_kw):
        # Eğer aynı cümlede "yaz/kodla" da varsa → kod isteği
        if any(w in p for w in ["yaz","kodla","implement"]):
            return _mk("code","code_generate","qwen3",conf="high",lang=lang,level=level,
                       thinking="Nasıl sorusu + yaz/kodla → code_generate")
        return _mk("chat","how_to","llama4",conf="high",lang=lang,level=level,
                   thinking="Nasıl sorusu → açıklama istiyor, kod değil → how_to")

    # Açık kod yazma isteği
    code_action = ["yaz","implement","kodla","oluştur","geliştir"]
    code_object = ["fonksiyon","class","api","endpoint","script","kod","servis","bot","uygulama"]
    if any(w in p for w in code_action) and any(w in p for w in code_object):
        # Büyük proje mi küçük snippet mi?
        big_project = any(w in p for w in ["uygulama","platform","sistem","proje","full"])
        model = "qwen3"
        return _mk("code","code_generate",model,conf="high",lang=lang,level=level,
                   thinking=f"Kod yazma isteği {'(büyük proje)' if big_project else '(snippet)'} → code_generate")

    # Mimari/tasarım
    arch_kw = ["mimari","architecture","tasarla","nasıl tasarlamalı","yapı kur","system design"]
    if any(w in p for w in arch_kw):
        return _mk("code","architecture_design","qwen3",conf="high",lang=lang,level=level,
                   thinking="Mimari/tasarım sorusu → architecture_design")

    # ── ADIM 5: Görsel ───────────────────────────────────────
    img_gen_kw = ["görsel oluştur","resim çiz","görsel yap","illüstrasyon","fotoğraf üret","image"]
    if any(w in p for w in img_gen_kw):
        return _mk("image_gen","image_generate","auto",conf="high",lang=lang,level=level,
                   thinking="Görsel üretim isteği → image_generate")

    # ── ADIM 6: Araştırma ────────────────────────────────────
    research_kw = ["araştır","bul","son gelişmeler","güncel durum","ne oldu","analiz et",
                   "incele","neler oluyor","gündem","son açıklama"]
    if any(w in p for w in research_kw):
        return _mk("deep_search","web_search","llama4","web",True,"medium",lang,level,
                   thinking="Araştırma/güncel bilgi talebi → deep_search")

    # ── ADIM 7: Default ──────────────────────────────────────
    return _mk("chat","general_chat","llama4",conf="low",lang=lang,level=level,
               thinking="Hiçbir spesifik sinyal yok → genel sohbet")


def _mk(route, intent, model="auto", tool="none", needs_rt=False,
        conf="medium", lang="tr", level="unknown",
        ambiguous=False, thinking="") -> Dict:
    return {
        "route":                route,
        "intent":               intent,
        "needs_realtime":       needs_rt,
        "tool":                 tool,
        "model":                model,
        "confidence":           conf,
        "language":             lang,
        "user_level":           level,
        "ambiguous":            ambiguous,
        "clarification_needed": None,
        "thinking":             thinking,
        "_source":              "keyword_cot_fallback",
    }


# ─────────────────────────────────────────────────────────────
# ANA FONKSİYON
# ─────────────────────────────────────────────────────────────

async def route_message(prompt: str, history: List[Dict] = None) -> Dict:
    """
    Mesajı adım adım analiz et ve yönlendir.

    Akış:
      1. LLM chain-of-thought (Llama-3.1-8B)
      2. Başarısızsa → akıllı keyword fallback (o da adım adım düşünür)

    Dönen dict:
      route     → chat / code / live_data / deep_search / image_gen / emotional
      intent    → code_debug / how_to / live_currency / vs.
      model     → qwen3 / llama4 / auto
      thinking  → router'ın düşünce özeti (debug için)
      ambiguous → True ise kullanıcıya soru sor
      user_level → beginner / intermediate / expert
    """
    history = history or []

    result = await llm_route(prompt, history)

    if result is None:
        result = keyword_fallback(prompt, history)
        print(f"[ROUTER] 🔄 Fallback → {result['intent']} | {result.get('thinking','')[:60]}")

    return result


# ─────────────────────────────────────────────────────────────
# GATEWAY ENTEGRASYON
# ─────────────────────────────────────────────────────────────

def router_to_gateway_mode(decision: Dict) -> str:
    """Router kararını gateway mode'una çevir."""
    route  = decision.get("route",  "chat")
    intent = decision.get("intent", "general_chat")
    model  = decision.get("model",  "auto")
    level  = decision.get("user_level", "unknown")

    # Kod → Qwen3-Coder 480B
    if route == "code" or model == "qwen3":
        return "code"

    # Expert + teknik konu → IT Expert modu
    if level == "expert" and route == "chat" and intent in (
        "architecture_design","how_to","concept_explain","step_by_step"
    ):
        return "it_expert"

    # Canlı veri ve araştırma → assistant (live data inject ile)
    if route in ("live_data", "deep_search"):
        return "assistant"

    # Görsel
    if route == "image_gen":
        return "image_gen"

    # Duygusal destek → empati modunda assistant
    if route == "emotional":
        return "assistant"

    return "assistant"


def should_ask_clarification(decision: Dict, prompt: str) -> Optional[str]:
    """Belirsiz mesajlarda ne sorulacak?"""
    if not decision.get("ambiguous"):
        return None

    clarification = decision.get("clarification_needed")
    if clarification:
        return clarification

    intent = decision.get("intent", "")
    if "code_generate" in intent:
        return "Hangi programlama dili veya framework kullanmamı istersin?"
    if "how_to" in intent:
        return "Hangi konuda yardımcı olayım, biraz daha açar mısın?"
    if "web_search" in intent:
        return "Hangi konuyu araştırayım?"
    if "image_generate" in intent:
        return "Nasıl bir görsel istiyorsun? Biraz tarif eder misin?"

    return None