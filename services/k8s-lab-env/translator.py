"""
translator.py — Canvas JSON'unu K8s nesnelerine çevirir.
App → Deployment, Service → Service nesnesi, bağlantılar → env.
Kullanıcı YAML görmez; bu tamamen arka planda çalışır.
"""
from config import (COMPONENT_IMAGES, COMPONENT_PORTS, COMPONENT_ENV,
                    CONNECTION_ENV, resolve_image)


def _safe_name(name: str) -> str:
    """K8s isim kuralı: küçük harf, tire, rakam."""
    import re
    s = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    return s or "node"


def _port_of(node: dict) -> int:
    t = node["type"]
    if t == "app":
        return int(node.get("port", 8080))
    if t == "service":
        return int(node.get("targetPort", 80))
    return COMPONENT_PORTS.get(t, 80)


def build_manifests(graph: dict, user_id: str) -> list:
    """Canvas grafiğinden K8s nesne listesi üretir (dict olarak).
    Her node → Deployment (+ headless olmayan basit Service ile erişim)."""
    ns = f"lab-{user_id}"
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = graph.get("edges", [])
    objects = []

    # Her node için: hangi node'lara bağlı (env enjeksiyonu için)
    deps_of = {}   # app_id -> [bağlı olduğu servis node'ları]
    for e in edges:
        frm, to = e["from"], e["to"]
        deps_of.setdefault(frm, []).append(nodes.get(to))

    for nid, node in nodes.items():
        t = node["type"]
        base = _safe_name(node["name"])
        img = resolve_image(node)
        port = _port_of(node)

        # ── Service node'u: bir K8s Service nesnesi üretir, kendi pod'u yok ──
        if t == "service":
            # Service hangi app'e bağlı? (edge: service -> app)
            targets = deps_of.get(nid, [])
            app = next((x for x in targets if x and x["type"] == "app"), None)
            if not app:
                continue  # bağlı app yoksa service atla
            objects.append(_service_obj(base, ns, app, node))
            continue

        # ── Diğerleri: Deployment + (erişim için) Service ──
        env = dict(COMPONENT_ENV.get(t, {}))

        # App ise: bağlı olduğu servislerin bağlantı env'lerini ekle
        if t == "app":
            for dep in deps_of.get(nid, []):
                if not dep:
                    continue
                dep_type = dep["type"]
                if dep_type in CONNECTION_ENV:
                    dep_host = _safe_name(dep["name"])   # service adı = pod adı
                    env.update(CONNECTION_ENV[dep_type](dep_host))

        objects.append(_deployment_obj(base, ns, img, port, env, t, node))
        # Her bileşen için dahili erişim Service'i (app'lerin DB'ye ulaşması için)
        objects.append(_internal_service_obj(base, ns, port))

    return objects


def _deployment_obj(name, ns, image, port, env, ntype, node):
    """Bir Deployment — güvenli defaultlarla (non-root, token yok)."""
    container = {
        "name": name,
        "image": image,
        "ports": [{"containerPort": port}],
        "env": [{"name": k, "value": str(v)} for k, v in env.items()],
        "securityContext": {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    # App türü ise: basit bir "çalışıyorum" komutu (kullanıcı kodu yok, demo)
    if ntype == "app":
        paths = node.get("paths", ["/"])
        container["command"] = ["sh", "-c",
            f"echo 'App {name} :{port} hazır — pathler: {' '.join(paths)}'; "
            f"while true; do sleep 3600; done"]
        # non-root için app'lerde runAsUser
        container["securityContext"]["runAsUser"] = 1000

    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {"onebune.lab/node": name, "app": name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "automountServiceAccountToken": False,  # POD TOKEN'SIZ (sızma engeli)
                    "containers": [container],
                },
            },
        },
    }


def _internal_service_obj(name, ns, port):
    """Dahili ClusterIP Service — app'lerin bileşene adıyla ulaşması için."""
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": {"app": name}},
        "spec": {
            "selector": {"app": name},
            "ports": [{"port": port, "targetPort": port}],
            "type": "ClusterIP",
        },
    }


def _service_obj(name, ns, app_node, svc_node):
    """Kullanıcının çizdiği Service node'u → gerçek Service nesnesi."""
    svc_type = svc_node.get("svcType", "ClusterIP")
    port = int(svc_node.get("port", 80))
    target = int(svc_node.get("targetPort", app_node.get("port", 8080)))
    spec = {
        "selector": {"app": _safe_name(app_node["name"])},
        "ports": [{"port": port, "targetPort": target}],
        "type": svc_type,
    }
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": {"onebune.lab/svc": name}},
        "spec": spec,
    }