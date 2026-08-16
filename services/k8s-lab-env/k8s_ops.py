"""
k8s_ops.py — Kubernetes ile konuşan katman.
Workspace açma, deploy, uyut/uyandır, durum, terminal.
Güvenlik: sadece lab- önekli ns'lere dokunur (çift koruma).
"""
import datetime
from config import (NS_PREFIX, WORKSPACE_TTL_DAYS, MAX_PODS_PER_USER,
                    IN_CLUSTER, WORKSPACE_TEMPLATE)
from translator import build_manifests

# K8s client — lazy import (test ortamında yoksa patlamasın)
_k8s = None
def _load_k8s():
    global _k8s
    if _k8s is not None:
        return _k8s
    from kubernetes import client, config, utils
    if IN_CLUSTER:
        config.load_incluster_config()
    else:
        config.load_kube_config()
    _k8s = {
        "core": client.CoreV1Api(),
        "apps": client.AppsV1Api(),
        "client": client,
        "utils": utils,
        "api_client": client.ApiClient(),
    }
    return _k8s


def _guard_ns(ns: str):
    """SADECE lab- önekli ns'lere izin — yanlışlıkla prod'a dokunmayı engeller."""
    if not ns.startswith(NS_PREFIX):
        raise ValueError(f"Güvenlik: '{ns}' lab namespace'i değil, işlem reddedildi.")


def ns_name(user_id: str) -> str:
    return f"{NS_PREFIX}{user_id}"


# ═══════════ WORKSPACE ═══════════
def ensure_workspace(user_id: str) -> dict:
    """Kullanıcının workspace'i yoksa şablondan oluştur. Varsa bilgisini döndür."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)

    # Var mı?
    try:
        existing = k["core"].read_namespace(ns)
        exp = existing.metadata.annotations.get("onebune.lab/expires", "")
        return {"exists": True, "namespace": ns, "expires": exp, "created": False}
    except k["client"].ApiException as e:
        if e.status != 404:
            raise

    # Yoksa: şablonu doldur + uygula
    now = datetime.datetime.utcnow()
    expires = now + datetime.timedelta(days=WORKSPACE_TTL_DAYS)
    tpl = open(WORKSPACE_TEMPLATE, encoding="utf-8").read()
    filled = (tpl
              .replace("{USER_ID}", user_id)
              .replace("{CREATED_TS}", now.isoformat() + "Z")
              .replace("{EXPIRES_TS}", expires.isoformat() + "Z"))

    # Çok belgeli YAML'ı uygula
    import yaml as _yaml
    docs = [d for d in _yaml.safe_load_all(filled) if d]
    k["utils"].create_from_dict(k["api_client"], {"apiVersion": "v1", "kind": "List", "items": docs})

    return {"exists": True, "namespace": ns, "expires": expires.isoformat() + "Z", "created": True}


# ═══════════ DEPLOY (Start) ═══════════
# ═══════════ CANVAS STATE (ConfigMap'te sakla) ═══════════
CANVAS_CM = "lab-canvas"   # kullanıcının çizimini tutan ConfigMap

def save_canvas(user_id: str, graph: dict):
    """Canvas çizimini ns'deki ConfigMap'e kaydet (refresh'te geri gelsin)."""
    import json
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    body = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": CANVAS_CM, "namespace": ns},
        "data": {"canvas": json.dumps(graph, ensure_ascii=False)},
    }
    try:
        k["core"].create_namespaced_config_map(ns, body)
    except k["client"].ApiException as e:
        if e.status == 409:
            k["core"].patch_namespaced_config_map(CANVAS_CM, ns, body)
        else:
            raise


def load_canvas(user_id: str) -> dict:
    """Kayıtlı canvas'ı oku. Yoksa boş döner."""
    import json
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    try:
        cm = k["core"].read_namespaced_config_map(CANVAS_CM, ns)
        return json.loads(cm.data.get("canvas", "{}"))
    except k["client"].ApiException as e:
        if e.status == 404:
            return {"nodes": [], "edges": []}
        raise


def deploy_graph(user_id: str, graph: dict) -> dict:
    """Canvas'ı namespace'e AKILLI SENKRON uygula:
    - Yeni olanı ekle, silineni kaldır, zaten var olanı DOKUNMA (yeniden başlatma)."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)

    objects = build_manifests(graph, user_id)

    # Canvas çizimini kaydet (refresh'te geri gelsin)
    try:
        save_canvas(user_id, graph)
    except Exception:
        pass

    # İstenen durum: canvas'tan gelen nesne adları
    desired_deploys = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Deployment"}
    desired_svcs    = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Service"}
    desired_pvcs    = {o["metadata"]["name"]: o for o in objects if o["kind"] == "PersistentVolumeClaim"}

    # Mevcut durum: cluster'da şu an ne var
    current_deploys = {d.metadata.name for d in k["apps"].list_namespaced_deployment(ns).items}
    current_svcs = {s.metadata.name for s in k["core"].list_namespaced_service(ns).items
                    if s.metadata.labels and (s.metadata.labels.get("app") or s.metadata.labels.get("onebune.lab/svc"))}
    current_pvcs = {p.metadata.name for p in k["core"].list_namespaced_persistent_volume_claim(ns).items}

    applied, kept, removed = [], [], []

    # ── PVC senkron (önce PVC — pod'lar ona bağlanacak) ──
    # PVC OLUŞTURULUR ama SİLİNMEZ (veri kaybını önlemek için — kullanıcı özel silmeli)
    for name, obj in desired_pvcs.items():
        if name in current_pvcs:
            kept.append(f"PVC/{name}")     # zaten var — veri korunur
        else:
            k["core"].create_namespaced_persistent_volume_claim(ns, obj)
            applied.append(f"PVC/{name}")

    # ── DEPLOYMENT senkron ──
    for name, obj in desired_deploys.items():
        if name in current_deploys:
            kept.append(f"Deployment/{name}")   # zaten var — DOKUNMA (pod kesintisiz)
        else:
            k["apps"].create_namespaced_deployment(ns, obj)
            applied.append(f"Deployment/{name}")
    # Canvas'ta olmayan deployment'ları sil
    for name in current_deploys - set(desired_deploys):
        k["apps"].delete_namespaced_deployment(name, ns)
        removed.append(f"Deployment/{name}")

    # ── SERVICE senkron ──
    for name, obj in desired_svcs.items():
        if name in current_svcs:
            # Service değişmiş olabilir (port vb.) → patch, ama varlığı korunur
            try:
                k["core"].patch_namespaced_service(name, ns, obj)
                kept.append(f"Service/{name}")
            except k["client"].ApiException:
                pass
        else:
            k["core"].create_namespaced_service(ns, obj)
            applied.append(f"Service/{name}")
    for name in current_svcs - set(desired_svcs):
        k["core"].delete_namespaced_service(name, ns)
        removed.append(f"Service/{name}")

    return {
        "namespace": ns,
        "applied": applied,      # yeni eklenenler
        "kept": kept,            # dokunulmayanlar (kesintisiz)
        "removed": removed,      # silinenler
        "count": len(applied) + len(kept),
    }


def _clean_workloads(ns: str):
    """Namespace'teki tüm lab deployment/service'lerini sil (yeni canvas için)."""
    k = _load_k8s()
    _guard_ns(ns)
    for d in k["apps"].list_namespaced_deployment(ns).items:
        k["apps"].delete_namespaced_deployment(d.metadata.name, ns)
    for s in k["core"].list_namespaced_service(ns).items:
        # kube dahili service'leri koru
        if s.metadata.labels and (s.metadata.labels.get("app") or s.metadata.labels.get("onebune.lab/svc")):
            k["core"].delete_namespaced_service(s.metadata.name, ns)


# ═══════════ SLEEP / WAKE ═══════════
def sleep_workspace(user_id: str) -> dict:
    """Tüm deployment'ları scale 0 — pod'lar durur, canvas kalır."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    count = 0
    for d in k["apps"].list_namespaced_deployment(ns).items:
        k["apps"].patch_namespaced_deployment_scale(
            d.metadata.name, ns, {"spec": {"replicas": 0}})
        count += 1
    return {"namespace": ns, "slept": count}


def wake_workspace(user_id: str) -> dict:
    """Tüm deployment'ları scale 1 — pod'lar tekrar kalkar."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    count = 0
    for d in k["apps"].list_namespaced_deployment(ns).items:
        k["apps"].patch_namespaced_deployment_scale(
            d.metadata.name, ns, {"spec": {"replicas": 1}})
        count += 1
    return {"namespace": ns, "woke": count}


# ═══════════ STATUS ═══════════
def get_status(user_id: str) -> dict:
    """Namespace'teki pod'ların durumu — frontend yeşil/sarı nokta için."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    pods = []
    for p in k["core"].list_namespaced_pod(ns).items:
        phase = p.status.phase
        ready = False
        if p.status.container_statuses:
            ready = all(c.ready for c in p.status.container_statuses)
        pods.append({
            "name": p.metadata.name,
            "app": (p.metadata.labels or {}).get("app", ""),
            "phase": phase,                 # Running / Pending / ...
            "ready": ready,
            "status": "running" if (phase == "Running" and ready)
                      else "starting" if phase in ("Pending", "Running")
                      else "error",
        })
    return {"namespace": ns, "pods": pods, "count": len(pods)}


# ═══════════ DESTROY ═══════════
def destroy_workspace(user_id: str) -> dict:
    """Kullanıcı workspace'ini komple sil (kullanıcı isterse)."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    k["core"].delete_namespace(ns)
    return {"namespace": ns, "destroyed": True}