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


def _force_del_deploy(k, name, ns):
    """Deployment'ı force sil — pod'lar hemen gitsin (Terminating'de takılmasın)."""
    opts = k["client"].V1DeleteOptions(grace_period_seconds=0, propagation_policy="Background")
    try:
        k["apps"].delete_namespaced_deployment(name, ns, body=opts)
    except Exception:
        pass
    # Deployment'ın pod'larını da force sil (garanti)
    try:
        pods = k["core"].list_namespaced_pod(ns, label_selector=f"app={name}").items
        for p in pods:
            try:
                k["core"].delete_namespaced_pod(p.metadata.name, ns,
                    body=k["client"].V1DeleteOptions(grace_period_seconds=0, propagation_policy="Background"))
            except Exception:
                pass
    except Exception:
        pass


def _force_del_pvc(k, name, ns):
    """PVC'yi force sil."""
    opts = k["client"].V1DeleteOptions(grace_period_seconds=0, propagation_policy="Background")
    try:
        k["core"].delete_namespaced_persistent_volume_claim(name, ns, body=opts)
    except Exception:
        pass


def _deploy_changed(k, name, ns, desired):
    """Mevcut deployment ile istenen arasında anlamlı fark var mı?
    env, imaj, mount değişmişse True (güncelle/rollout gerekir)."""
    try:
        cur = k["apps"].read_namespaced_deployment(name, ns)
    except Exception:
        return True  # okunamıyorsa güncelle
    try:
        cur_c = cur.spec.template.spec.containers[0]
        des_c = desired["spec"]["template"]["spec"]["containers"][0]
        # İmaj değişti mi
        if cur_c.image != des_c.get("image"):
            return True
        # Env değişti mi (isim=değer kümesi)
        cur_env = {e.name: (e.value or "") for e in (cur_c.env or [])}
        des_env = {e["name"]: str(e.get("value", "")) for e in des_c.get("env", [])}
        if cur_env != des_env:
            return True
        # Volume mount değişti mi (PVC bağlama)
        cur_mounts = {(m.name, m.mount_path) for m in (cur_c.volume_mounts or [])}
        des_mounts = {(m["name"], m["mountPath"]) for m in des_c.get("volumeMounts", [])}
        if cur_mounts != des_mounts:
            return True
    except Exception:
        return True  # emin değilsek güncelle
    return False


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
    desired_secrets = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Secret"}
    desired_cms     = {o["metadata"]["name"]: o for o in objects if o["kind"] == "ConfigMap"}

    # Mevcut durum: cluster'da şu an ne var
    current_deploys = {d.metadata.name for d in k["apps"].list_namespaced_deployment(ns).items}
    current_svcs = {s.metadata.name for s in k["core"].list_namespaced_service(ns).items
                    if s.metadata.labels and (s.metadata.labels.get("app") or s.metadata.labels.get("onebune.lab/svc"))}
    current_pvcs = {p.metadata.name for p in k["core"].list_namespaced_persistent_volume_claim(ns).items}

    applied, kept, removed = [], [], []

    # ── SECRET senkron (envFrom için — deployment'tan ÖNCE olmalı) ──
    for name, obj in desired_secrets.items():
        try:
            k["core"].read_namespaced_secret(name, ns)
            k["core"].replace_namespaced_secret(name, ns, obj)   # güncelle
        except Exception:
            k["core"].create_namespaced_secret(ns, obj)
        applied.append(f"Secret/{name}")

    # ── CONFIGMAP senkron (lab-canvas hariç — o ayrı yönetiliyor) ──
    for name, obj in desired_cms.items():
        try:
            k["core"].read_namespaced_config_map(name, ns)
            k["core"].replace_namespaced_config_map(name, ns, obj)
        except Exception:
            k["core"].create_namespaced_config_map(ns, obj)
        applied.append(f"ConfigMap/{name}")

    # ── PVC senkron: sadece OLUŞTURMA (önce PVC, pod'lar ona bağlanacak) ──
    for name, obj in desired_pvcs.items():
        if name in current_pvcs:
            kept.append(f"PVC/{name}")     # zaten var — veri korunur
        else:
            k["core"].create_namespaced_persistent_volume_claim(ns, obj)
            applied.append(f"PVC/{name}")
    # NOT: PVC SİLME en sona alındı (mount eden pod'lar önce silinmeli)

    # ── DEPLOYMENT senkron ──
    for name, obj in desired_deploys.items():
        if name in current_deploys:
            # Zaten var — değişmiş mi kontrol et (env, imaj, mount)
            if _deploy_changed(k, name, ns, obj):
                # Değişmiş → güncelle (pod yeni ayarlarla yeniden başlar = rollout)
                k["apps"].patch_namespaced_deployment(name, ns, obj)
                applied.append(f"Deployment/{name} (güncellendi)")
            else:
                kept.append(f"Deployment/{name}")   # değişmemiş — dokunma (kesintisiz)
        else:
            k["apps"].create_namespaced_deployment(ns, obj)
            applied.append(f"Deployment/{name}")
    # Canvas'ta olmayan deployment'ları sil (uploader -files hariç, onları PVC senkronu yönetir)
    for name in current_deploys - set(desired_deploys):
        if name.endswith("-files"):
            continue  # uploader pod'u — PVC silinince ayrıca silinir
        _force_del_deploy(k, name, ns)
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
        if name.endswith("-files"):
            continue  # uploader service — PVC silinince ayrıca silinir
        k["core"].delete_namespaced_service(name, ns)
        removed.append(f"Service/{name}")

    # ── PVC SİLME (EN SON — mount eden pod'lar yukarıda silindi) ──
    # Canvas'ta olmayan PVC → kullanıcı ekrandan sildi → cluster'dan da git (force)
    for name in current_pvcs - set(desired_pvcs):
        # PVC'ye bağlı uploader pod'unu FORCE sil (Terminating'de takılmasın)
        _force_del_deploy(k, f"{name}-files", ns)
        _force_del_pvc(k, name, ns)
        removed.append(f"PVC/{name}")

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
    """Namespace'teki pod'ların durumu + workspace kaynak kullanımı."""
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
    # Cluster'daki PVC'ler (frontend CLI'dan silineni ekrandan da kaldırsın)
    pvcs = [p.metadata.name for p in k["core"].list_namespaced_persistent_volume_claim(ns).items]

    # Workspace kaynak kullanımı (ResourceQuota'dan) — kullanıcı ne kadar kaldığını görsün
    quota = None
    try:
        rqs = k["core"].list_namespaced_resource_quota(ns).items
        if rqs:
            st = rqs[0].status or {}
            used = dict(st.used or {})
            hard = dict(st.hard or {})
            def _g(d, *keys):
                for kk in keys:
                    if kk in d:
                        return d[kk]
                return None
            quota = {
                "podsUsed": _g(used, "pods", "count/pods"),
                "podsHard": _g(hard, "pods", "count/pods"),
                "cpuUsed": _g(used, "requests.cpu", "cpu"),
                "cpuHard": _g(hard, "requests.cpu", "cpu"),
                "memUsed": _g(used, "requests.memory", "memory"),
                "memHard": _g(hard, "requests.memory", "memory"),
            }
    except Exception:
        pass

    return {"namespace": ns, "pods": pods, "pvcs": pvcs, "count": len(pods), "quota": quota}


# ═══════════ DESTROY ═══════════
def destroy_workspace(user_id: str) -> dict:
    """Kullanıcı workspace'ini komple sil (kullanıcı isterse)."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    k["core"].delete_namespace(ns)
    return {"namespace": ns, "destroyed": True}


def get_pod_logs(user_id: str, pod_name: str, tail: int = 30) -> str:
    """Bir pod'un son log satırlarını döndür.
    pod_name tam pod adı VEYA deployment/bileşen adı olabilir (app label ile eşleştirir).
    Pod hazır değilse/çökmüşse durumu açıklayan net mesaj döner."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    import re as _re
    safe = _re.sub(r"[^a-z0-9-]", "", (pod_name or "").lower())

    # 1) app label ile pod bul (deployment adı = app label)
    pod = None
    try:
        pods = k["core"].list_namespaced_pod(ns, label_selector=f"app={safe}").items
        if pods:
            pods.sort(key=lambda p: p.metadata.creation_timestamp or 0, reverse=True)
            pod = pods[0]
    except Exception:
        pass

    # 2) Label ile bulunamadıysa: isim öneki ile ara (pod adı safe ile başlıyor mu)
    if pod is None:
        try:
            allpods = k["core"].list_namespaced_pod(ns).items
            matches = [p for p in allpods if (p.metadata.name or "").startswith(safe)]
            if matches:
                matches.sort(key=lambda p: p.metadata.creation_timestamp or 0, reverse=True)
                pod = matches[0]
        except Exception:
            pass

    if pod is None:
        return f"'{safe}' için pod bulunamadı. Bileşen çalışıyor mu? (⚡ Çalıştır'a bastın mı?)"

    real = pod.metadata.name
    phase = (pod.status.phase if pod.status else "") or "?"

    # Pod'un container adı (log için gerekebilir)
    cname = None
    try:
        if pod.spec and pod.spec.containers:
            cname = pod.spec.containers[0].name
    except Exception:
        pass

    # 3) Pod henüz başlamadıysa (Pending/ContainerCreating) → net mesaj
    if phase in ("Pending",):
        # Neden pending? container durumlarına bak
        reason = ""
        try:
            cs = (pod.status.container_statuses or [])
            for c in cs:
                w = c.state.waiting if c.state else None
                if w and w.reason:
                    reason = f" ({w.reason})"
                    break
        except Exception:
            pass
        return f"Pod '{real}' henüz başlıyor{reason}. Birkaç saniye sonra tekrar dene (↻)."

    # 4) Logu çek — önce normal, container adıyla
    def _clean(raw):
        """Log çıktısını okunabilir metne çevir: bytes→str, b'...' repr, ANSI temizle."""
        if raw is None:
            return ""
        # bytes ise decode et
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        raw = str(raw)
        # Bazen string olarak b'...' repr gelir → gerçek içeriğe çevir
        if raw.startswith("b'") and raw.endswith("'"):
            inner = raw[2:-1]
            # \n \t \x1b gibi kaçışları gerçek karaktere çevir
            try:
                inner = inner.encode().decode("unicode_escape")
                raw = inner
            except Exception:
                raw = inner
        elif raw.startswith('b"') and raw.endswith('"'):
            inner = raw[2:-1]
            try:
                raw = inner.encode().decode("unicode_escape")
            except Exception:
                raw = inner
        # ANSI kaçış dizilerini temizle (\x1b[...m renk kodları)
        raw = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
        raw = raw.replace("\\x1b", "").replace("\r", "")
        return raw

    def _read(previous=False):
        kwargs = {"tail_lines": tail}
        if cname:
            kwargs["container"] = cname
        if previous:
            kwargs["previous"] = True
        raw = k["core"].read_namespaced_pod_log(real, ns, **kwargs)
        return _clean(raw)

    try:
        out = _read(previous=False)
        if out and out.strip():
            return out
        # Log boşsa ve pod çökmüşse önceki logu dene
        try:
            prev = _read(previous=True)
            if prev and prev.strip():
                return "(önceki çöken container logu:)\n" + prev
        except Exception:
            pass
        return f"Pod '{real}' çalışıyor ({phase}) ama henüz log üretmedi."
    except Exception as e:
        # Çöken pod: önceki logu dene
        try:
            prev = _read(previous=True)
            if prev and prev.strip():
                return "(önceki çöken container logu:)\n" + prev
        except Exception:
            pass
        return f"(log alınamadı: {e})"


def rollout_restart(user_id: str, name: str) -> dict:
    """Bir deployment'ı rollout restart et — kubectl rollout restart gibi.
    Pod template'ine restart zaman damgası ekler, K8s pod'u yeniden oluşturur."""
    k = _load_k8s()
    ns = ns_name(user_id)
    _guard_ns(ns)
    import re as _re
    safe = _re.sub(r"[^a-z0-9-]", "", (name or "").lower())
    if not safe:
        return {"ok": False, "message": "Geçersiz bileşen adı"}
    # restart annotation'ı ekle → yeni pod template hash → rollout tetiklenir
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    patch = {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": now
    }}}}}
    try:
        k["apps"].patch_namespaced_deployment(safe, ns, patch)
        return {"ok": True, "name": safe, "restartedAt": now}
    except Exception as e:
        return {"ok": False, "message": f"Rollout hatası: {e}"}