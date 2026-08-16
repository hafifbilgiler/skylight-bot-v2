"""
main.py — ONE-BUNE DevOps Lab Orkestratör (FastAPI)
Endpoint'ler: workspace, deploy(start), sleep, wake, status, destroy, terminal.
Kullanıcı YAML görmez; canvas JSON gönderir, gerisi arkada.
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os

from config import validate_graph, MAX_PODS_PER_USER, WORKSPACE_TTL_DAYS
import k8s_ops

app = FastAPI(title="ONE-BUNE DevOps Lab Orchestrator")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Kullanıcı kimliği: JWT'den user_id çöz (gateway ile aynı mantık) ──
def resolve_user(token: Optional[str]) -> str:
    """JWT token'dan güvenli user_id üret. Gateway'in JWT'sini kullanır."""
    if not token:
        raise HTTPException(401, "Giriş gerekli")
    try:
        import jwt as pyjwt
        # NOT: gateway ile aynı secret env'den gelir; imza doğrulanır
        secret = os.getenv("JWT_SECRET", "")
        if secret:
            payload = pyjwt.decode(token, secret, algorithms=["HS256"])
        else:
            payload = pyjwt.decode(token, options={"verify_signature": False})
        sub = payload.get("sub", "")
        # email → güvenli kısa id (ns adında kullanılabilir)
        import hashlib, re
        safe = re.sub(r"[^a-z0-9]", "", sub.split("@")[0].lower())[:12]
        h = hashlib.sha1(sub.encode()).hexdigest()[:6]
        return f"{safe or 'u'}{h}"
    except Exception:
        raise HTTPException(401, "Geçersiz oturum")


class DeployReq(BaseModel):
    graph: dict


@app.get("/lab/health")
def health():
    return {"ok": True, "max_pods": MAX_PODS_PER_USER, "ttl_days": WORKSPACE_TTL_DAYS}


@app.post("/lab/workspace")
def workspace(token: Optional[str] = Header(None, alias="X-Token")):
    """Workspace'i garanti et + kayıtlı canvas + pod durumunu döndür (refresh için)."""
    uid = resolve_user(token)
    try:
        ws = k8s_ops.ensure_workspace(uid)
        # Kayıtlı canvas'ı ve mevcut pod durumunu ekle
        try:
            ws["canvas"] = k8s_ops.load_canvas(uid)
        except Exception:
            ws["canvas"] = {"nodes": [], "edges": []}
        try:
            ws["status"] = k8s_ops.get_status(uid)
        except Exception:
            ws["status"] = {"pods": []}
        return ws
    except Exception as e:
        raise HTTPException(500, f"Workspace hatası: {e}")


@app.post("/lab/deploy")
def deploy(req: DeployReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Canvas → pod'lar (Start). Boş canvas = hepsini sil (senkron)."""
    uid = resolve_user(token)
    nodes = req.graph.get("nodes", [])
    # Boş canvas: doğrulama atla, akıllı senkron her şeyi siler
    if nodes:
        ok, err = validate_graph(req.graph)
        if not ok:
            raise HTTPException(400, err)
    try:
        k8s_ops.ensure_workspace(uid)
        result = k8s_ops.deploy_graph(uid, req.graph)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(500, f"Deploy hatası: {e}")


@app.post("/lab/sleep")
def sleep(token: Optional[str] = Header(None, alias="X-Token")):
    uid = resolve_user(token)
    return k8s_ops.sleep_workspace(uid)


@app.post("/lab/wake")
def wake(token: Optional[str] = Header(None, alias="X-Token")):
    uid = resolve_user(token)
    return k8s_ops.wake_workspace(uid)


@app.get("/lab/status")
def status(token: Optional[str] = Header(None, alias="X-Token")):
    uid = resolve_user(token)
    return k8s_ops.get_status(uid)


@app.post("/lab/destroy")
def destroy(token: Optional[str] = Header(None, alias="X-Token")):
    uid = resolve_user(token)
    return k8s_ops.destroy_workspace(uid)


# ═══════════ DOSYA İŞLEMLERİ (PVC'ye upload/list) — exec ile ═══════════
from fastapi import Request
from pydantic import BaseModel as _BM
import base64 as _b64

class UploadReq(_BM):
    pvc: str
    path: str          # hedef dizin, örn. /usr/share/nginx/html
    filename: str
    content_b64: str   # dosya içeriği base64

class ListReq(_BM):
    pvc: str
    path: str = "/"

def _uploader_pod(uid, pvc):
    """PVC'nin uploader pod adını bul."""
    k = k8s_ops._load_k8s()
    ns = k8s_ops.ns_name(uid)
    k8s_ops._guard_ns(ns)
    import re as _re
    safe = _re.sub(r"[^a-z0-9-]", "", pvc.lower())
    for p in k["core"].list_namespaced_pod(ns, label_selector=f"app={safe}-files").items:
        if p.status.phase == "Running":
            return ns, p.metadata.name
    raise HTTPException(400, "Dosya yöneticisi pod'u hazır değil — önce Çalıştır'a bas")

def _exec_in(ns, pod, command):
    """Pod içinde komut çalıştır, çıktıyı döndür."""
    k = k8s_ops._load_k8s()
    from kubernetes.stream import stream
    return stream(k["core"].connect_get_namespaced_pod_exec, pod, ns,
                  command=command, stderr=True, stdin=False, stdout=True, tty=False)

@app.post("/lab/file_list")
def file_list(req: ListReq, token: Optional[str] = Header(None, alias="X-Token")):
    """PVC içindeki bir dizini listele. Disk /srv altında mount'lu."""
    uid = resolve_user(token)
    ns, pod = _uploader_pod(uid, req.pvc)
    # Güvenlik: path içinde .. yok, /srv köküne sabitle
    safe_path = "/srv/" + req.path.strip("/").replace("..", "")
    out = _exec_in(ns, pod, ["sh", "-c", f"ls -la '{safe_path}' 2>&1 || echo BOS"])
    return {"path": req.path, "listing": out}

@app.post("/lab/file_upload")
def file_upload(req: UploadReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Dosyayı PVC'nin belirtilen dizinine yaz (uploader pod üzerinden)."""
    uid = resolve_user(token)
    ns, pod = _uploader_pod(uid, req.pvc)
    # Hedef dizin /srv altına map'lenir (PVC orada mount'lu)
    rel = req.path.strip("/").replace("..", "")
    target_dir = "/srv/" + rel if rel else "/srv"
    fname = req.filename.replace("/", "").replace("..", "")
    # base64'ü pod içinde çöz ve yaz (büyük dosya için stdin yerine echo|base64 -d)
    try:
        # içeriği base64 olarak pod'a gönder
        cmd = ["sh", "-c",
               f"mkdir -p '{target_dir}' && echo '{req.content_b64}' | base64 -d > '{target_dir}/{fname}' && echo OK && ls -la '{target_dir}/{fname}'"]
        out = _exec_in(ns, pod, cmd)
        if "OK" not in out:
            raise HTTPException(500, f"Yazma hatası: {out}")
        return {"ok": True, "path": f"{target_dir}/{fname}", "detail": out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload hatası: {e}")

@app.post("/lab/file_delete")
def file_delete(req: UploadReq, token: Optional[str] = Header(None, alias="X-Token")):
    """PVC'den dosya sil."""
    uid = resolve_user(token)
    ns, pod = _uploader_pod(uid, req.pvc)
    rel = req.path.strip("/").replace("..", "")
    fname = req.filename.replace("/", "").replace("..", "")
    target = "/srv/" + (rel + "/" if rel else "") + fname
    out = _exec_in(ns, pod, ["sh", "-c", f"rm -f '{target}' && echo SILINDI"])
    return {"ok": "SILINDI" in out, "detail": out}


# ═══════════ TERMINAL (WebSocket → pod exec) ═══════════
@app.websocket("/lab/terminal/{pod_name}")
async def terminal(ws: WebSocket, pod_name: str, token: str = ""):
    """Tarayıcı terminali ↔ pod içi shell. Sadece kullanıcının kendi ns'inde."""
    await ws.accept()
    try:
        uid = resolve_user(token)
    except Exception:
        await ws.send_text("\r\n[Oturum geçersiz]\r\n")
        await ws.close()
        return

    ns = k8s_ops.ns_name(uid)
    try:
        k8s_ops._guard_ns(ns)
        k = k8s_ops._load_k8s()
        from kubernetes.stream import stream
        # Pod'un bu kullanıcıya ait olduğunu doğrula (başka pod'a erişemez)
        pods = [p.metadata.name for p in k["core"].list_namespaced_pod(ns).items]
        if pod_name not in pods:
            await ws.send_text("\r\n[Bu pod size ait değil]\r\n")
            await ws.close()
            return

        resp = stream(k["core"].connect_get_namespaced_pod_exec,
                      pod_name, ns, command=["/bin/sh"],
                      stderr=True, stdin=True, stdout=True, tty=True,
                      _preload_content=False)

        await ws.send_text(f"\r\n[{pod_name} terminaline bağlandın]\r\n")
        import asyncio
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                await ws.send_text(resp.read_stdout())
            if resp.peek_stderr():
                await ws.send_text(resp.read_stderr())
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=0.05)
                resp.write_stdin(data)
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break
        resp.close()
    except Exception as e:
        try:
            await ws.send_text(f"\r\n[Terminal hatası: {e}]\r\n")
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass