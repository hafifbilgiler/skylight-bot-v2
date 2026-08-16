"""
ONE-BUNE DevOps Lab — Orkestratör
config.py: güvenlik sabitleri, whitelist imajlar, kurallar
"""
import os

# ── Kullanıcı kuralları (Serkan'ın belirlediği) ──
MAX_PODS_PER_USER = 8          # bir kullanıcı en fazla 8 pod
WORKSPACE_TTL_DAYS = 5         # workspace 5 gün yaşar
NS_PREFIX = "lab-"            # tüm workspace ns'leri bu önekle (çift koruma)
SYSTEM_NS = "onebune-lab-system"

# ── Whitelist imajlar — SADECE bunlar deploy edilebilir ──
# Rastgele imaj (kripto madenci vb.) ENGELLİ. Kullanıcı imaj adı gönderemez;
# sadece 'type' gönderir, imajı biz belirleriz.
COMPONENT_IMAGES = {
    "redis":    "redis:7-alpine",
    "postgres": "postgres:16-alpine",
    "rabbitmq": "rabbitmq:3-management-alpine",
    "nginx":    "nginx:alpine",
    # app türleri — sabit taban imajlar (kullanıcı kodu değil, hazır runtime)
    "app:flask":   "python:3.12-slim",
    "app:fastapi": "python:3.12-slim",
    "app:node":    "node:20-alpine",
    "app:django":  "python:3.12-slim",
    "app:spring":  "eclipse-temurin:21-jre",
    "app:custom":  "nginx:alpine",   # 'custom' şimdilik demo placeholder (güvenli)
}

# ── Bileşenlerin varsayılan portları ──
COMPONENT_PORTS = {
    "redis": 6379, "postgres": 5432, "rabbitmq": 5672, "nginx": 80,
}

# ── Bileşen env gereksinimleri (bağlantı için) ──
# App bir servise bağlanınca bu env'ler enjekte edilir
CONNECTION_ENV = {
    "redis":    lambda host: {"REDIS_URL": f"redis://{host}:6379", "REDIS_HOST": host},
    "postgres": lambda host: {"DATABASE_URL": f"postgresql://postgres:labpass@{host}:5432/app",
                              "POSTGRES_HOST": host, "PGHOST": host},
    "rabbitmq": lambda host: {"RABBITMQ_URL": f"amqp://guest:guest@{host}:5672/",
                              "RABBITMQ_HOST": host},
}

# ── Servislerin kendi başlangıç env'leri (şifre vb.) ──
COMPONENT_ENV = {
    "postgres": {"POSTGRES_PASSWORD": "labpass", "POSTGRES_DB": "app", "POSTGRES_USER": "postgres"},
    "redis": {},
    "rabbitmq": {"RABBITMQ_DEFAULT_USER": "guest", "RABBITMQ_DEFAULT_PASS": "guest"},
    "nginx": {},
}

# ── K8s bağlantısı ──
# Orkestratör cluster İÇİNDE çalışırsa in-cluster config, dışarıdaysa kubeconfig
IN_CLUSTER = os.getenv("IN_CLUSTER", "true").lower() == "true"

# ── Template yolu ──
WORKSPACE_TEMPLATE = os.getenv("WORKSPACE_TEMPLATE", "workspace-template.yaml")


def resolve_image(node: dict) -> str:
    """Node'un güvenli imajını döndür. Whitelist dışıysa None (reddet)."""
    t = node.get("type")
    if t == "app":
        kind = node.get("kind", "flask")
        return COMPONENT_IMAGES.get(f"app:{kind}")
    return COMPONENT_IMAGES.get(t)


def validate_graph(graph: dict) -> tuple:
    """Canvas grafiğini güvenlik açısından doğrula.
    Dönüş: (ok: bool, hata_mesajı: str veya None)"""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    if not nodes:
        return False, "Boş mimari — en az bir bileşen ekleyin."

    # 8 pod kuralı (service node'u pod değil, sayılmaz)
    pod_nodes = [n for n in nodes if n.get("type") != "service"]
    if len(pod_nodes) > MAX_PODS_PER_USER:
        return False, f"En fazla {MAX_PODS_PER_USER} pod kurabilirsiniz (şu an {len(pod_nodes)})."

    # Her node whitelist'te mi? (service node'u pod değil, imaj gerekmez)
    for n in nodes:
        if n.get("type") == "service":
            continue  # service bir K8s Service nesnesi, pod'u yok
        img = resolve_image(n)
        if not img:
            return False, f"Bilinmeyen bileşen türü: {n.get('type')} — güvenlik için reddedildi."

    # Node id'leri geçerli mi (edge referansları)
    ids = {n["id"] for n in nodes}
    for e in edges:
        if e.get("from") not in ids or e.get("to") not in ids:
            return False, "Geçersiz bağlantı referansı."

    return True, None