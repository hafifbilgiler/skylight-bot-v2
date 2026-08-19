"""AI Mentor — workspace'i analiz eder ve 'pod yap' isteklerini canvas'a çevirir.
DeepInfra LLM kullanır. Sadece öneri üretir; deploy hep kullanıcının kontrolünde.
"""
import json
import httpx
from config import DEEPINFRA_API_KEY, DEEPINFRA_MODEL, DEEPINFRA_URL

# Kullanılabilir bileşenler (LLM'in bilmesi gereken paletin sınırları)
PALETTE_INFO = """Kullanılabilir bileşenler (sadece bunlar):
- app: Uygulama (Flask/Node vb.). Alanlar: name, kind (flask/node/python), port, image, paths (liste)
- service: K8s Service. Alanlar: name, svcType (ClusterIP/NodePort), port, targetPort
- redis: Redis önbellek. Alan: name
- postgres: PostgreSQL veritabanı. Alan: name
- rabbitmq: RabbitMQ kuyruk. Alan: name
- nginx: Nginx reverse proxy. Alan: name
- pvc: Kalıcı Disk. Alanlar: name, sizeGb (max 1)

Kurallar:
- En fazla 8 pod (service ve pvc sayılmaz)
- Bağlantılar (edges): app→redis/postgres/rabbitmq/service/pvc, service→app, nginx→service/app/pvc
- app bir DB'ye bağlanınca bağlantı env'leri otomatik gelir
"""


def _llm(messages, max_tokens=1200, temperature=0.3):
    """DeepInfra chat completion çağrısı."""
    if not DEEPINFRA_API_KEY:
        return {"error": "DeepInfra API anahtarı ayarlı değil (DEEPINFRA_API_KEY)."}
    try:
        r = httpx.post(
            DEEPINFRA_URL,
            headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": DEEPINFRA_MODEL, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        return {"text": data["choices"][0]["message"]["content"]}
    except Exception as e:
        return {"error": f"LLM hatası: {e}"}


def _format_status(status):
    """Pod durumlarını kullanıcı dostu bir listeye çevir (kubectl get pods gibi)."""
    pods = (status or {}).get("pods", [])
    if not pods:
        return "Şu an çalışan pod yok. Bir mimari kurup ⚡ Çalıştır'a bas."
    lines = ["Workspace'indeki pod durumları:\n"]
    for p in pods:
        name = p.get("name", "?")
        phase = p.get("phase", "?")
        ready = p.get("ready")
        # Duruma göre simge
        if phase == "Running" and ready:
            icon = "🟢"
        elif phase == "Running" and not ready:
            icon = "🟡"
        elif phase in ("Pending", "ContainerCreating"):
            icon = "🔵"
        else:
            icon = "🔴"
        state = phase + (" · hazır" if ready else " · hazır değil")
        lines.append(f"{icon} {name} — {state}")
    running = sum(1 for p in pods if p.get("phase") == "Running" and p.get("ready"))
    lines.append(f"\nToplam {len(pods)} pod, {running} tanesi sağlıklı çalışıyor.")
    return "\n".join(lines)


def _summarize_workspace(graph, status):
    """Canvas + cluster durumunu LLM'e verilecek özet metne çevir."""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    lines = ["MEVCUT MİMARİ:"]
    if not nodes:
        lines.append("  (canvas boş)")
    for n in nodes:
        t = n.get("type")
        name = n.get("name")
        extra = ""
        if t == "app":
            extra = f" (tür={n.get('kind')}, port={n.get('port')})"
        elif t == "service":
            extra = f" ({n.get('svcType')} {n.get('port')}→{n.get('targetPort')})"
        elif t == "pvc":
            extra = f" ({n.get('sizeGb',1)}GB)"
        env = n.get("extraEnv") or []
        if env:
            extra += " env=[" + ", ".join(e.get("key","") for e in env) + "]"
        if n.get("mountPath"):
            extra += f" mount={n.get('mountPath')}"
        lines.append(f"  - {t}: {name}{extra}")

    if edges:
        lines.append("BAĞLANTILAR:")
        id2name = {n["id"]: n.get("name") for n in nodes}
        for e in edges:
            a = id2name.get(e.get("from"), "?")
            b = id2name.get(e.get("to"), "?")
            lines.append(f"  - {a} → {b}")

    # Cluster durumu (pod'ların gerçek hali)
    pods = (status or {}).get("pods", [])
    if pods:
        lines.append("POD DURUMLARI (cluster):")
        for p in pods:
            lines.append(f"  - {p.get('name')}: {p.get('phase')} (ready={p.get('ready')})")
    return "\n".join(lines)


def analyze(graph, status, logs_by_pod=None):
    """Workspace'i analiz et — sorunları, önerileri döndür (LLM yorumu)."""
    summary = _summarize_workspace(graph, status)
    log_text = ""
    if logs_by_pod:
        log_text = "\n\nPOD LOGLARI (son satırlar):\n"
        for pod, log in logs_by_pod.items():
            snippet = (log or "")[-800:]
            log_text += f"--- {pod} ---\n{snippet}\n"

    system = (
        "Sen bir Kubernetes DevOps mentörüsün. Kullanıcı görsel bir K8s laboratuvarında "
        "bileşenler kuruyor. Türkçe, kısa ve öğretici konuş. Mimariyi incele, sorunları "
        "(çöken pod, eksik bağlantı, yanlış yapılandırma) tespit et, NEDEN olduğunu açıkla "
        "ve nasıl düzeltileceğini söyle. Doğru yapılmış şeyleri de kısaca takdir et. "
        "Markdown başlık kullanma, düz paragraf ve kısa maddeler kullan."
    )
    user = f"{summary}{log_text}\n\nBu mimariyi analiz et: sorunlar, nedenleri, öneriler."
    return _llm([{"role": "system", "content": system},
                 {"role": "user", "content": user}])


def build(instruction, graph):
    """'Bana X kur' isteğini canvas JSON'una çevir (node+edge önerisi).
    Deploy ETMEZ — sadece canvas önerisi döndürür."""
    current = _summarize_workspace(graph, None)
    system = (
        "Sen bir Kubernetes mimari asistanısın. Kullanıcının isteğine göre görsel "
        "canvas'a eklenecek bileşenleri JSON olarak üret. SADECE JSON döndür, başka hiçbir "
        "açıklama yazma.\n\n" + PALETTE_INFO +
        "\n\nÇıktı formatı (yalnızca bu JSON):\n"
        '{"nodes":[{"id":"a1","type":"app","name":"app-1","kind":"flask","port":5000,"paths":["/"]},'
        '{"id":"r1","type":"redis","name":"redis-1"}],'
        '"edges":[{"from":"a1","to":"r1"}],'
        '"note":"Kısa Türkçe açıklama: ne kurdum ve neden"}\n\n'
        "Kurallar: id'ler kısa ve benzersiz olsun. Mevcut bileşenlerle çakışma olmasın "
        "(yeni isimler ver). Sadece paletdeki türleri kullan. En fazla 8 pod."
    )
    user = f"MEVCUT DURUM:\n{current}\n\nİSTEK: {instruction}\n\nSadece JSON üret."
    res = _llm([{"role": "system", "content": system},
                {"role": "user", "content": user}], temperature=0.2)
    if "error" in res:
        return res
    # JSON'u ayıkla (LLM bazen ```json ... ``` ile sarar)
    text = res["text"].strip()
    text = text.replace("```json", "").replace("```", "").strip()
    # İlk { ile son } arasını al
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end+1]
    try:
        parsed = json.loads(text)
        return {"canvas": parsed}
    except Exception as e:
        return {"error": f"LLM geçerli JSON üretmedi: {e}", "raw": res["text"][:500]}


def _llm_stream(messages, max_tokens=1200, temperature=0.3):
    """DeepInfra'dan streaming — cevabı parça parça yield eder."""
    if not DEEPINFRA_API_KEY:
        yield "[HATA] DeepInfra API anahtarı ayarlı değil."
        return
    try:
        with httpx.stream(
            "POST", DEEPINFRA_URL,
            headers={"Authorization": f"Bearer {DEEPINFRA_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": DEEPINFRA_MODEL, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature, "stream": True},
            timeout=90,
        ) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                        delta = obj["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except Exception:
                        continue
    except Exception as e:
        yield f"[HATA] LLM akış hatası: {e}"


def chat_stream(message, graph, status=None):
    """Konuşkan, akıllı mentor. Niyeti VE hedefin net olup olmadığını değerlendirir.
    Belirsizse (birden çok pod var, ne silineceği belli değil) kullanıcıya SORAR.
    Net ise aksiyonu alır. İnsanla konuşur gibi doğal."""
    # Mevcut mimariyi çıkar (LLM bunu görüp akıllı karar versin)
    nodes = graph.get("nodes", [])
    pods = (status or {}).get("pods", [])
    pod_names = [p.get("name", "") for p in pods]
    node_list = [f"{n.get('type')}:{n.get('name')}" for n in nodes]
    running = [p.get("name") for p in pods if p.get("status") == "running"]

    # Bağlam metni — LLM'in "ne var" bilmesi için
    ctx = f"Canvas'taki bileşenler: {', '.join(node_list) if node_list else 'boş'}\n"
    ctx += f"Çalışan pod'lar: {', '.join(running) if running else 'yok'}\n"
    ctx += f"Toplam {len(nodes)} bileşen, {len(running)} çalışan pod."

    # 1) Niyet + hedef netliği belirle (JSON döndürür)
    intent_sys = (
        "Sen bir Kubernetes DevOps mentörüsün. Kullanıcının mesajını analiz et ve JSON döndür.\n\n"
        "MEVCUT DURUM:\n" + ctx + "\n\n"
        "Kullanıcının ne yapmak istediğini belirle. Şu niyetlerden biri:\n"
        "- CLEAR: her şeyi silmek (tüm mimariyi/canvası temizlemek)\n"
        "- DELETE_ONE: TEK bir bileşeni silmek (örn 'redis'i sil')\n"
        "- BUILD: yeni bileşen kurmak/eklemek\n"
        "- STATUS: pod/durum listelemek\n"
        "- LOGS: bir pod'un logunu görmek\n"
        "- ASK: soru sormak / bilgi istemek\n"
        "- CLARIFY: niyet belli ama HEDEF belirsiz (soru sorman lazım)\n\n"
        "ÖNEMLİ KURALLAR:\n"
        "1. Kullanıcı 'sil' derse ama NEYİ sileceği belli değilse VE birden çok bileşen varsa → CLARIFY\n"
        "2. Kullanıcı 'hepsini sil'/'temizle'/'her şeyi kaldır' derse → CLEAR\n"
        "3. Kullanıcı 'redis'i sil' gibi net bileşen söylerse → DELETE_ONE (target=redis)\n"
        "4. Kullanıcı 'pod' veya 'log' der ama hangi pod belli değilse VE birden çok pod varsa → CLARIFY\n"
        "5. Tek pod/bileşen varsa hedef otomatik nettir, CLARIFY'a gerek yok\n\n"
        "JSON formatı (SADECE bu, başka metin yok):\n"
        '{\"intent\": \"...\", \"target\": \"bileşen adı veya boş\", \"question\": \"kullanıcıya sorulacak soru (sadece CLARIFY ise)\"}\n\n'
        "Örnekler:\n"
        "'redis kur' → {\"intent\":\"BUILD\",\"target\":\"\",\"question\":\"\"}\n"
        "'sil' (3 bileşen var) → {\"intent\":\"CLARIFY\",\"target\":\"\",\"question\":\"Neyi silmek istersin? Şu an redis, postgres ve app var. Hepsini mi yoksa birini mi?\"}\n"
        "'redis'i sil' → {\"intent\":\"DELETE_ONE\",\"target\":\"redis\",\"question\":\"\"}\n"
        "'hepsini sil' → {\"intent\":\"CLEAR\",\"target\":\"\",\"question\":\"\"}\n"
        "'logları göster' (2 pod var) → {\"intent\":\"CLARIFY\",\"target\":\"\",\"question\":\"Hangi pod'un loglarını görmek istersin? redis mi app mi?\"}\n"
        "'redis logu' → {\"intent\":\"LOGS\",\"target\":\"redis\",\"question\":\"\"}\n"
    )
    intent_res = _llm([{"role": "system", "content": intent_sys},
                       {"role": "user", "content": message}], max_tokens=200, temperature=0)

    # JSON parse et
    parsed = {}
    if "error" not in intent_res:
        raw = (intent_res.get("text") or "").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            # JSON parse edilemezse eski usul kelime ara
            up = raw.upper()
            if "CLEAR" in up: parsed = {"intent": "CLEAR"}
            elif "DELETE_ONE" in up: parsed = {"intent": "DELETE_ONE"}
            elif "BUILD" in up: parsed = {"intent": "BUILD"}
            elif "STATUS" in up: parsed = {"intent": "STATUS"}
            elif "LOGS" in up: parsed = {"intent": "LOGS"}
            elif "CLARIFY" in up: parsed = {"intent": "CLARIFY", "question": "Ne yapmak istediğini biraz daha açar mısın?"}
            else: parsed = {"intent": "ASK"}

    intent = (parsed.get("intent") or "ASK").upper()
    target = (parsed.get("target") or "").strip()
    question = (parsed.get("question") or "").strip()

    # CLARIFY: hedef belirsiz, kullanıcıya sor (aksiyon ALMA)
    if "CLARIFY" in intent:
        yield question or "Tam olarak ne yapmak istediğini açar mısın?"
        return

    # CLEAR: her şeyi sil
    if "CLEAR" in intent:
        yield "[[CLEAR]]"
        return

    # DELETE_ONE: tek bileşen sil (hedef ile)
    if "DELETE_ONE" in intent:
        if target:
            yield "[[DELETE]]" + target
        else:
            yield "Hangi bileşeni silmek istediğini anlayamadım. Adını yazar mısın?"
        return

    if "STATUS" in intent:
        yield _format_status(status)
        return

    if "LOGS" in intent:
        # Hedef varsa onu, yoksa mesajın tamamını gönder (main.py çözer)
        yield "[[LOGS]]" + (target or message or "")
        return

    if "BUILD" in intent:
        b = build(message, graph)
        if "error" in b:
            yield "[HATA] " + b["error"]
        else:
            yield "[[BUILD]]" + json.dumps(b["canvas"], ensure_ascii=False)
        return

    # ASK: workspace bağlamıyla streaming cevap
    summary = _summarize_workspace(graph, status)
    system = (
        "Sen bir Kubernetes DevOps mentörüsün. Kullanıcı görsel bir K8s laboratuvarında "
        "bileşenler kurmuş. Onun KENDİ mimarisi hakkındaki sorularını yanıtla — genel değil, "
        "aşağıdaki gerçek duruma göre. Türkçe, kısa, samimi, öğretici ol — bir arkadaşınla "
        "konuşur gibi doğal. Markdown başlık kullanma. Bir şey mimaride yoksa 'şu an yok' de.\n\n" + PALETTE_INFO
    )
    user = f"KULLANICININ MEVCUT MİMARİSİ:\n{summary}\n\nSORU: {message}"
    for chunk in _llm_stream([{"role": "system", "content": system},
                              {"role": "user", "content": user}]):
        yield chunk


def analyze_stream(graph, status, logs_by_pod=None):
    """Analizi streaming yap — yorum parça parça aksın."""
    summary = _summarize_workspace(graph, status)
    log_text = ""
    if logs_by_pod:
        log_text = "\n\nPOD LOGLARI (son satırlar):\n"
        for pod, log in logs_by_pod.items():
            log_text += f"--- {pod} ---\n{(log or '')[-800:]}\n"
    system = (
        "Sen bir Kubernetes DevOps mentörüsün. Kullanıcı görsel bir K8s laboratuvarında "
        "bileşenler kuruyor. Türkçe, kısa ve öğretici konuş. Mimariyi incele, sorunları "
        "tespit et, NEDEN olduğunu açıkla, nasıl düzeltileceğini söyle. Doğru şeyleri takdir et. "
        "Markdown başlık kullanma."
    )
    user = f"{summary}{log_text}\n\nBu mimariyi analiz et: sorunlar, nedenleri, öneriler."
    for chunk in _llm_stream([{"role": "system", "content": system},
                              {"role": "user", "content": user}]):
        yield chunk


def chat(message, graph, status=None):
    """Kullanıcının mesajını workspace bağlamında ele al.
    LLM önce niyeti belirler: SORU mu (cevap ver) yoksa KURMA isteği mi (canvas üret).
    Dönenler:
      - {"mode":"answer","text":...}  → sohbet cevabı
      - {"mode":"build","canvas":...} → canvas önerisi (kurma)
    """
    summary = _summarize_workspace(graph, status)
    # 1) Niyet belirle: bu bir kurma isteği mi, yoksa soru mu?
    intent_sys = (
        "Kullanıcının mesajını sınıflandır. SADECE tek kelime döndür:\n"
        "BUILD = yeni bileşen kurmak/eklemek/silmek istiyorsa (örn: 'redis kur', 'app ekle', 'postgres bağla')\n"
        "ASK = soru soruyorsa veya bilgi istiyorsa (örn: 'redis neden çöktü', 'bu bağlantı doğru mu', 'nasıl çalışır')\n"
        "Sadece BUILD veya ASK yaz."
    )
    intent_res = _llm([{"role": "system", "content": intent_sys},
                       {"role": "user", "content": message}], max_tokens=10, temperature=0)
    if "error" in intent_res:
        return intent_res
    intent = intent_res["text"].strip().upper()

    # 2a) Kurma isteği → mevcut build() kullan
    if "BUILD" in intent:
        b = build(message, graph)
        if "error" in b:
            return b
        return {"mode": "build", "canvas": b["canvas"]}

    # 2b) Soru → workspace'i bilerek cevapla
    system = (
        "Sen bir Kubernetes DevOps mentörüsün. Kullanıcı görsel bir K8s laboratuvarında "
        "bileşenler kurmuş. Onun KENDİ mimarisi hakkındaki sorularını yanıtla — genel değil, "
        "aşağıdaki gerçek duruma göre. Türkçe, kısa, öğretici ol. Markdown başlık kullanma, "
        "düz paragraf ve kısa maddeler kullan. Bir şey mimaride yoksa 'şu an yok' de.\n\n"
        + PALETTE_INFO
    )
    user = f"KULLANICININ MEVCUT MİMARİSİ:\n{summary}\n\nSORU: {message}"
    res = _llm([{"role": "system", "content": system},
                {"role": "user", "content": user}])
    if "error" in res:
        return res
    return {"mode": "answer", "text": res["text"]}