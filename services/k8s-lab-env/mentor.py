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