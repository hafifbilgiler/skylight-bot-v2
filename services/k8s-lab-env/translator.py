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

        # ── PVC node'u: PersistentVolumeClaim + otomatik uploader pod ──
        if t == "pvc":
            objects.append(_pvc_obj(base, ns, node))
            # Her PVC için bir dosya yöneticisi (filebrowser) pod'u — kullanıcı buradan dosya yükler
            objects.extend(_uploader_objs(base, ns))
            continue

        # ── Service node'u: bir K8s Service nesnesi üretir, kendi pod'u yok ──
        # Bağlantı yönü iki türlü olabilir: service→app VEYA app→service
        if t == "service":
            # Bu service'e bağlı VEYA bu service'in bağlandığı app'i bul
            app = None
            # service → app (service'ten app'e ok)
            for dep in deps_of.get(nid, []):
                if dep and dep["type"] == "app":
                    app = dep; break
            # app → service (app'ten service'e ok)
            if not app:
                for e in edges:
                    if e["to"] == nid:
                        src = nodes.get(e["from"])
                        if src and src["type"] == "app":
                            app = src; break
            if not app:
                continue  # hiçbir app'e bağlı değilse service atla (öğretici: boş service işe yaramaz)
            objects.append(_service_obj(base, ns, app, node))
            continue

        # ── Diğerleri: SADECE Deployment (otomatik service YOK) ──
        # Öğretici amaç: app tek başına dışarıdan erişilemez.
        # Kullanıcı Service ekleyip bağlayınca erişim öğrenir.
        env = dict(COMPONENT_ENV.get(t, {}))

        # App ise: bağlı olduğu servislerin bağlantı env'lerini ekle
        if t == "app":
            for dep in deps_of.get(nid, []):
                if not dep:
                    continue
                dep_type = dep["type"]
                if dep_type in CONNECTION_ENV:
                    dep_host = _safe_name(dep["name"])
                    env.update(CONNECTION_ENV[dep_type](dep_host))

        # Bu pod'a bağlı PVC var mı? (pod→pvc VEYA pvc→pod) → mount et
        mounted_pvc, mount_path = None, None
        for dep in deps_of.get(nid, []):
            if dep and dep["type"] == "pvc":
                mounted_pvc = _safe_name(dep["name"])
                mount_path = node.get("mountPath") or "/data"
        if not mounted_pvc:
            for e in edges:
                if e["to"] == nid:
                    src = nodes.get(e["from"])
                    if src and src["type"] == "pvc":
                        mounted_pvc = _safe_name(src["name"])
                        mount_path = node.get("mountPath") or "/data"

        # Kullanıcının eklediği env'ler (config panelinden)
        for kv in node.get("extraEnv", []):
            if kv.get("key"):
                env[kv["key"]] = kv.get("value", "")

        objects.append(_deployment_obj(base, ns, img, port, env, t, node, mounted_pvc, mount_path))
        # NOT: otomatik internal service KALDIRILDI — kullanıcı kendi Service'ini eklesin (öğrenme)

    return objects


def _deployment_obj(name, ns, image, port, env, ntype, node, mounted_pvc=None, mount_path=None):
    """Bir Deployment — güvenli defaultlarla (non-root, token yok).
    mounted_pvc verilirse o PVC mount_path'e bağlanır."""
    # Güvenlik: privilege escalation kapalı.
    # App'ler: drop ALL (user değiştirmez, en sıkı).
    # Servisler (redis/postgres/vb): SETUID/SETGID gerekir (root→servis user geçişi),
    #   yoksa "failed switching to redis: operation not permitted" hatası.
    if ntype == "app":
        caps = {"drop": ["ALL"]}
    elif ntype == "nginx":
        # Nginx 80 portunu (1024 altı) bağlamak için NET_BIND_SERVICE gerekir
        caps = {"drop": ["ALL"], "add": ["SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "NET_BIND_SERVICE"]}
    else:
        # Servisler kendi user'ına geçebilmek için setuid/setgid tutar, gerisi düşer
        caps = {"drop": ["ALL"], "add": ["SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE"]}
    sec = {
        "allowPrivilegeEscalation": False,
        "capabilities": caps,
    }
    container = {
        "name": name,
        "image": image,
        "ports": [{"containerPort": port}],
        "env": [{"name": k, "value": str(v)} for k, v in env.items()],
        "securityContext": sec,
    }
    # App türü ise: basit bir "çalışıyorum" komutu (kullanıcı kodu yok, demo)
    if ntype == "app":
        paths = node.get("paths", ["/"])
        container["command"] = ["sh", "-c",
            f"echo 'App {name} :{port} hazir - pathler: {' '.join(paths)}'; "
            f"while true; do sleep 3600; done"]
        # App'ler non-root çalışsın (bilinen user)
        sec["runAsNonRoot"] = True
        sec["runAsUser"] = 1000
    # Redis/Postgres/RabbitMQ/Nginx: kendi imaj user'ına geçer (setuid/setgid ile)

    pod_spec = {
        "automountServiceAccountToken": False,  # POD TOKEN'SIZ (sızma engeli)
        "containers": [container],
    }
    # PVC mount: kullanıcının kalıcı diskini istediği dizine bağla
    if mounted_pvc:
        container["volumeMounts"] = [{"name": "data-vol", "mountPath": mount_path or "/data"}]
        pod_spec["volumes"] = [{"name": "data-vol",
                                "persistentVolumeClaim": {"claimName": mounted_pvc}}]

    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {"onebune.lab/node": name, "app": name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": pod_spec,
            },
        },
    }


def _pvc_obj(name, ns, node):
    """Kullanıcının kalıcı diski (PersistentVolumeClaim). Max 1GB."""
    from config import MAX_PVC_SIZE_GB
    # Kullanıcı boyut isteyebilir ama tavanı aşamaz
    try:
        want = float(str(node.get("sizeGb", 1)).replace("Gi", "").replace("GB", ""))
    except Exception:
        want = 1
    size = min(max(want, 0.1), MAX_PVC_SIZE_GB)   # 0.1GB - 1GB arası
    # Gi cinsinden (tam sayı değilse Mi'ye çevir)
    if size >= 1 and size == int(size):
        storage = f"{int(size)}Gi"
    else:
        storage = f"{int(size * 1024)}Mi"
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {"onebune.lab/pvc": name}},
        "spec": {
            "accessModes": ["ReadWriteMany"],   # NFS RWX destekler — çoklu pod paylaşır
            "resources": {"requests": {"storage": storage}},
        },
    }


def _uploader_objs(pvc_name, ns):
    """Bir PVC için hafif bir yardımcı pod (alpine).
    Dosya işlemleri exec ile yapılır (upload/list/delete).
    PVC RWX olduğu için diğer pod'lar da aynı anda aynı diske erişir."""
    up_name = f"{pvc_name}-files"
    container = {
        "name": "filehelper",
        "image": "alpine:3.20",
        "command": ["sh", "-c", "while true; do sleep 3600; done"],  # dosya işlemleri exec ile
        "volumeMounts": [{"name": "data", "mountPath": "/srv"}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE"]},
        },
    }
    deploy = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": up_name, "namespace": ns,
                     "labels": {"app": up_name, "onebune.lab/uploader": pvc_name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": up_name}},
            "template": {
                "metadata": {"labels": {"app": up_name}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [container],
                    "volumes": [{"name": "data",
                                 "persistentVolumeClaim": {"claimName": pvc_name}}],
                },
            },
        },
    }
    return [deploy]   # service'e gerek yok, exec ile erişiyoruz


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