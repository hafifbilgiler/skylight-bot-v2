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
import premium as _premium

app = FastAPI(title="ONE-BUNE DevOps Lab Orchestrator")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Kullanıcı kimliği: JWT'den user_id çöz (gateway ile aynı mantık) ──
def resolve_user(token: Optional[str]) -> str:
    """JWT token'dan güvenli user_id üret + PREMIUM zorunlu.
    DevOps Lab premium bir üründür — premium olmayan 403 alır."""
    if not token:
        raise HTTPException(401, "Giriş gerekli")
    try:
        import jwt as pyjwt
        secret = os.getenv("JWT_SECRET", "")
        if secret:
            payload = pyjwt.decode(token, secret, algorithms=["HS256"])
        else:
            payload = pyjwt.decode(token, options={"verify_signature": False})
        sub = payload.get("sub", "")
    except Exception:
        raise HTTPException(401, "Geçersiz oturum")
    # PREMIUM KONTROLÜ — lab'ın tüm işlemleri premium ister
    if not _premium.is_premium_email(sub):
        raise HTTPException(403, "DevOps Lab premium bir özelliktir — abonelik gerekli.")
    # email → güvenli kısa id (ns adında kullanılabilir)
    import hashlib, re
    safe = re.sub(r"[^a-z0-9]", "", sub.split("@")[0].lower())[:12]
    h = hashlib.sha1(sub.encode()).hexdigest()[:6]
    return f"{safe or 'u'}{h}"


# JWT'den ham email (premium kontrolü için — DB'de email ile eşleşir)
def resolve_email(token: Optional[str]) -> str:
    if not token:
        raise HTTPException(401, "Giriş gerekli")
    try:
        import jwt as pyjwt
        secret = os.getenv("JWT_SECRET", "")
        if secret:
            payload = pyjwt.decode(token, secret, algorithms=["HS256"])
        else:
            payload = pyjwt.decode(token, options={"verify_signature": False})
        return payload.get("sub", "")
    except Exception:
        raise HTTPException(401, "Geçersiz oturum")


# Premium zorunlu — değilse 403. Lab premium bir üründür.
def require_premium(token: Optional[str]):
    """Kullanıcı premium değilse 403 döndür."""
    email = resolve_email(token)
    if not _premium.is_premium_email(email):
        raise HTTPException(403, "DevOps Lab premium bir özelliktir — abonelik gerekli.")
    return email


@app.get("/lab/plan")
def lab_plan(token: Optional[str] = Header(None, alias="X-Token")):
    """Kullanıcının premium durumu — frontend kilit ekranı kararı için.
    Bu endpoint premium GEREKTİRMEZ (herkes kendi planını sorabilir)."""
    if not token:
        return {"is_premium": False, "logged_in": False}
    email = resolve_email(token)
    return {"is_premium": _premium.is_premium_email(email), "logged_in": bool(email)}


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


class RestartReq(BaseModel):
    name: str   # deployment adı (pod/bileşen adı)

@app.post("/lab/restart")
def restart(req: RestartReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Bir deployment'ı rollout restart et — pod yeniden başlar (yeni ayarlarla)."""
    uid = resolve_user(token)
    return k8s_ops.rollout_restart(uid, req.name)


# ═══════════ AI MENTOR (DeepInfra) ═══════════
import mentor as _mentor

class MentorReq(BaseModel):
    graph: dict = {}
    instruction: str = ""

@app.post("/lab/mentor_analyze")
def mentor_analyze(req: MentorReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Workspace'i analiz et — pod durumları + loglar + bağlantılar → LLM yorumu."""
    uid = resolve_user(token)
    status = k8s_ops.get_status(uid)
    # Çöken/sorunlu pod'ların loglarını topla (analiz için)
    logs = {}
    try:
        for p in status.get("pods", []):
            if p.get("phase") != "Running" or not p.get("ready"):
                name = p.get("name")
                try:
                    logs[name] = k8s_ops.get_pod_logs(uid, name, tail=30)
                except Exception:
                    pass
    except Exception:
        pass
    return _mentor.analyze(req.graph, status, logs)

@app.post("/lab/mentor_build")
def mentor_build(req: MentorReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Sohbet: mesaj SORU ise cevap, KURMA ise canvas önerisi. Deploy etmez."""
    uid = resolve_user(token)
    if not req.instruction.strip():
        raise HTTPException(400, "Bir şey yaz.")
    status = None
    try:
        status = k8s_ops.get_status(uid)
    except Exception:
        pass
    return _mentor.chat(req.instruction, req.graph, status)


from fastapi.responses import StreamingResponse

@app.post("/lab/mentor_stream")
def mentor_stream(req: MentorReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Streaming sohbet — cevap parça parça akar. Soru=stream, kurma=[[BUILD]]{json}."""
    uid = resolve_user(token)
    status = None
    try:
        status = k8s_ops.get_status(uid)
    except Exception:
        pass
    def gen():
        for chunk in _mentor.chat_stream(req.instruction, req.graph, status):
            yield chunk
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@app.post("/lab/mentor_analyze_stream")
def mentor_analyze_stream(req: MentorReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Streaming analiz — yorum parça parça akar."""
    uid = resolve_user(token)
    status = k8s_ops.get_status(uid)
    logs = {}
    try:
        for p in status.get("pods", []):
            if p.get("phase") != "Running" or not p.get("ready"):
                try:
                    logs[p.get("name")] = k8s_ops.get_pod_logs(uid, p.get("name"), tail=30)
                except Exception:
                    pass
    except Exception:
        pass
    def gen():
        for chunk in _mentor.analyze_stream(req.graph, status, logs):
            yield chunk
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# ═══════════ DOSYA İŞLEMLERİ (PVC'ye upload/list) — exec ile ═══════════
from fastapi import Request
from pydantic import BaseModel as _BM
import base64 as _b64

class UploadReq(_BM):
    pvc: str
    path: str          # hedef dizin, örn. /usr/share/nginx/html
    filename: str
    content_b64: str = ""   # dosya içeriği base64 (silme isteğinde boş)

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

def _exec_in(ns, pod, command, stdin_data=None):
    """Pod içinde komut çalıştır, çıktıyı döndür.
    stdin_data verilirse komuta stdin olarak akıtılır (büyük binary için)."""
    k = k8s_ops._load_k8s()
    from kubernetes.stream import stream
    if stdin_data is None:
        return stream(k["core"].connect_get_namespaced_pod_exec, pod, ns,
                      command=command, stderr=True, stdin=False, stdout=True, tty=False)
    # stdin akışı: önce TÜM veriyi yaz, sonra stdin kapanana kadar çıktı oku
    resp = stream(k["core"].connect_get_namespaced_pod_exec, pod, ns,
                  command=command, stderr=True, stdin=True, stdout=True, tty=False,
                  _preload_content=False)
    out = ""
    CHUNK = 16384
    idx = 0
    # 1) Tüm veriyi gönder
    while idx < len(stdin_data):
        resp.update(timeout=1)
        if resp.peek_stdout():
            out += resp.read_stdout()
        if resp.peek_stderr():
            out += resp.read_stderr()
        chunk = stdin_data[idx:idx+CHUNK]
        resp.write_stdin(chunk)
        idx += CHUNK
    # 2) stdin'i EOF ile kapat (base64 -d işlemeyi bitirsin)
    try:
        resp.write_stdin("\x04")  # Ctrl-D benzeri; ardından kanalı kapat
    except Exception:
        pass
    # 3) Komut bitene kadar çıktıyı topla (max ~15sn)
    import time as _t
    deadline = _t.time() + 15
    while resp.is_open() and _t.time() < deadline:
        resp.update(timeout=1)
        if resp.peek_stdout():
            out += resp.read_stdout()
        if resp.peek_stderr():
            out += resp.read_stderr()
        if not resp.peek_stdout() and not resp.peek_stderr():
            break
    resp.close()
    return out

@app.post("/lab/file_list")
def file_list(req: ListReq, token: Optional[str] = Header(None, alias="X-Token")):
    """PVC içindeki dosyaları listele. Disk /data altında mount'lu (standart)."""
    uid = resolve_user(token)
    ns, pod = _uploader_pod(uid, req.pvc)
    # Güvenlik: path içinde .. yok, /data köküne sabitle
    rel = req.path.strip("/").replace("..", "")
    safe_path = "/data/" + rel if rel else "/data"
    out = _exec_in(ns, pod, ["sh", "-c", f"ls -la '{safe_path}' 2>&1 || echo BOS"])
    return {"path": req.path, "listing": out}

@app.post("/lab/file_upload")
def file_upload(req: UploadReq, token: Optional[str] = Header(None, alias="X-Token")):
    """Dosyayı PVC'nin belirtilen dizinine yaz (uploader pod üzerinden).
    base64 stdin'den akıtılır — büyük binary (PNG vb.) için 'Argument list too long' olmaz."""
    uid = resolve_user(token)
    ns, pod = _uploader_pod(uid, req.pvc)
    rel = req.path.strip("/").replace("..", "")
    target_dir = "/data/" + rel if rel else "/data"
    # Dosya adını güvenli yap: boşluk, parantez, özel karakterler → alt çizgi
    import re as _re
    fname = _re.sub(r"[^A-Za-z0-9._-]", "_", req.filename.replace("..", ""))
    if not fname:
        fname = "dosya"
    try:
        # Dizini oluştur ve herkese açık yap (hangi user'lı pod bağlanırsa okusun/yazsın)
        _exec_in(ns, pod, ["sh", "-c", f"mkdir -p '{target_dir}' && chmod -R 777 '{target_dir}'"])
        # base64 içeriğini stdin'den ver → pod içinde çöz → dosyaya yaz
        target = f"{target_dir}/{fname}"
        cmd = ["sh", "-c", f"base64 -d > '{target}'"]
        _exec_in(ns, pod, cmd, stdin_data=req.content_b64)
        # Dosyayı herkese okuma/yazma yap (non-root pod'lar da erişsin)
        _exec_in(ns, pod, ["sh", "-c", f"chmod 666 '{target}'"])
        # Yazıldı mı doğrula
        check = _exec_in(ns, pod, ["sh", "-c", f"ls -la '{target}' 2>&1"])
        if "No such" in check or "cannot" in check.lower():
            raise HTTPException(500, f"Dosya yazılamadı: {check}")
        return {"ok": True, "path": target, "detail": check}
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
    target = "/data/" + (rel + "/" if rel else "") + fname
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