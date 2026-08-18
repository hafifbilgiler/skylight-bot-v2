"""
ONE-BUNE DevOps Lab — Orkestratör
config.py: güvenlik sabitleri, whitelist imajlar, kurallar
"""
import os

# ── Kullanıcı kuralları (Serkan'ın belirlediği) ──
MAX_PODS_PER_USER = 8          # bir kullanıcı en fazla 8 pod
MAX_PVC_PER_USER = 1           # bir kullanıcı en fazla 1 PVC
MAX_PVC_SIZE_GB = 1            # PVC tavanı 1GB

# ── AI Mentor (DeepInfra) ──
DEEPINFRA_API_KEY = os.environ.get("DEEPINFRA_API_KEY", "")
DEEPINFRA_MODEL = os.environ.get("DEEPINFRA_MODEL", "meta-llama/Meta-Llama-3.1-70B-Instruct")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
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

# ── GÜVENLİ IMAGE WHITELIST'İ ──
# Kullanıcı serbest image YAZAMAZ — sadece bu onaylı listeden seçer.
# Her seçenek resmi/güvenilir registry'den, sabit tag'li (latest yok — reproducibility).
IMAGE_WHITELIST = {
    "redis": [
        "redis:7-alpine", "redis:7.2-alpine", "redis:6-alpine",
    ],
    "postgres": [
        "postgres:16-alpine", "postgres:15-alpine", "postgres:14-alpine",
    ],
    "rabbitmq": [
        "rabbitmq:3-management-alpine", "rabbitmq:3.13-management-alpine",
    ],
    "nginx": [
        "nginx:alpine", "nginx:1.27-alpine", "nginx:1.26-alpine",
    ],
    "mysql": [
        "mysql:8.4", "mysql:8.0",
    ],
    "mongodb": [
        "mongo:7", "mongo:6",
    ],
    # App runtime'ları (kullanıcı kodu değil, çalışma ortamı)
    "app": [
        "python:3.12-slim", "python:3.11-slim",
        "node:20-alpine", "node:22-alpine",
        "golang:1.23-alpine", "eclipse-temurin:21-jre",
        "nginx:alpine",
    ],
}

def image_options(node_type: str) -> list:
    """Bir bileşen için seçilebilir güvenli imaj listesi."""
    return IMAGE_WHITELIST.get(node_type, [])

def is_image_allowed(node_type: str, image: str) -> bool:
    """Verilen imaj bu bileşen için whitelist'te mi?"""
    return image in IMAGE_WHITELIST.get(node_type, [])

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
    "mysql":    lambda host: {"DATABASE_URL": f"mysql://root:labpass@{host}:3306/app",
                              "MYSQL_HOST": host},
    "mongodb":  lambda host: {"MONGO_URL": f"mongodb://{host}:27017/app",
                              "MONGO_HOST": host},
}

# ── Servislerin kendi başlangıç env'leri (şifre vb.) ──
COMPONENT_ENV = {
    "postgres": {"POSTGRES_PASSWORD": "labpass", "POSTGRES_DB": "app", "POSTGRES_USER": "postgres"},
    "redis": {},
    "rabbitmq": {"RABBITMQ_DEFAULT_USER": "guest", "RABBITMQ_DEFAULT_PASS": "guest"},
    "nginx": {},
    "mysql": {"MYSQL_ROOT_PASSWORD": "labpass", "MYSQL_DATABASE": "app"},
    "mongodb": {},
}

# ── K8s bağlantısı ──
# Orkestratör cluster İÇİNDE çalışırsa in-cluster config, dışarıdaysa kubeconfig
IN_CLUSTER = os.getenv("IN_CLUSTER", "true").lower() == "true"

# ── Template yolu ──
WORKSPACE_TEMPLATE = os.getenv("WORKSPACE_TEMPLATE", "workspace-template.yaml")


def resolve_image(node: dict) -> str:
    """Node'un güvenli imajını döndür.
    Kullanıcı 'image' alanında bir imaj seçtiyse VE whitelist'teyse onu kullan.
    Aksi halde varsayılan sabit imaj. Whitelist dışı imaj ASLA kullanılmaz."""
    t = node.get("type")
    chosen = node.get("image", "")
    # Kullanıcının seçtiği imaj whitelist'te mi? (App için de diğerleri için de)
    wl_key = "app" if t == "app" else t
    if chosen and is_image_allowed(wl_key, chosen):
        return chosen
    # Varsayılana düş
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

    # 8 pod kuralı (service, pvc, secret, configmap pod değil, sayılmaz)
    NON_POD = ("service", "pvc", "secret", "configmap")
    pod_nodes = [n for n in nodes if n.get("type") not in NON_POD]
    if len(pod_nodes) > MAX_PODS_PER_USER:
        return False, f"En fazla {MAX_PODS_PER_USER} pod kurabilirsiniz (şu an {len(pod_nodes)})."

    # PVC kuralı: en fazla 1 PVC
    pvc_nodes = [n for n in nodes if n.get("type") == "pvc"]
    if len(pvc_nodes) > MAX_PVC_PER_USER:
        return False, f"En fazla {MAX_PVC_PER_USER} kalıcı disk (PVC) oluşturabilirsiniz."

    # Her node whitelist'te mi? (pod olmayan node'lar imaj gerektirmez)
    for n in nodes:
        if n.get("type") in NON_POD:
            continue  # service/pvc/secret/configmap pod değil
        img = resolve_image(n)
        if not img:
            return False, f"Bilinmeyen bileşen türü: {n.get('type')} — güvenlik için reddedildi."

    # Node id'leri geçerli mi (edge referansları)
    ids = {n["id"] for n in nodes}
    for e in edges:
        if e.get("from") not in ids or e.get("to") not in ids:
            return False, "Geçersiz bağlantı referansı."

    return True, None