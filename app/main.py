"""MarketingCaribe Clipper — FastAPI backend.

No YouTube API key: yt-dlp reads the pasted link directly.
Endpoints:
  GET  /                        health
  POST /api/video-info          {url} -> title/uploader/duration
  POST /api/clips               {url, start, end, model} -> job (download +
                                transcribe + clip candidates)
  POST /api/render              {job_id|url, start, end, frame_*, brand_text,
                                title_text, subtitle_*, edited_words} -> job
  GET  /api/jobs/{id}           job status/result
  GET  /api/download/{file}    rendered mp4
  POST /api/auth/register       {name, email, password} -> user + token
  POST /api/auth/login          {email, password} -> token
  GET  /api/auth/me            (token) -> current user

Usuarios: PostgreSQL en Render (DATABASE_URL) o SQLite local
(data/clipper.db) si no hay DATABASE_URL. Claves con bcrypt, sesiones JWT.
v1: video-info/clips/render/jobs/download siguen públicos; si el cliente
manda un token válido, el user_id queda registrado en el job.

Render options (Configuración):
  frame_enabled: bool = True   Con marco / Sin marco.
                               True  = yellow frame + brand name on top.
                               False = NO frame and NO name.
  brand_text: str = "@marketingcaribe"   "Nombre que saldrá en el marco",
                               editable. Empty = frame kept, name omitted.
  frame_color / frame_width / title_text / subtitle_style / subtitle_size /
  subtitle_position / show_subtitles: see RenderRequest.
"""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, billing, clips_db, jobs
from .auth import User, get_current_user, get_optional_user
from .billing import (PaymentMethod, PaymentRequest, Plan, UserSubscription,
                      approve_request, get_active_subscription,
                      has_active_subscription, method_out, plan_out,
                      reject_request, request_out, subscription_out)
from .clips import build_candidates
from .clips_db import (STATUS_APROBADO, STATUS_PENDIENTE, STATUS_RECHAZADO,
                       Clip, clip_out, create_clip)
from .config import (BASE_DIR, DEFAULT_BRAND_TEXT, DEFAULT_FRAME_COLOR,
                     DEFAULT_FRAME_WIDTH, MAX_WORKERS, OUTPUTS_DIR,
                     RECEIPTS_DIR, SOURCES_DIR, TRANSCRIPTS_DIR)
from .db import SessionLocal, db_backend, get_db, init_db
from .render import render_clip
from .transcribe import transcribe_file
from .video import download_section, probe, video_info


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # crea la tabla users si no existe (Postgres o SQLite)
    yield


app = FastAPI(title="MarketingCaribe Clipper", version="1.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


# ---------------------------------------------------------------- models
class VideoInfoRequest(BaseModel):
    url: str


class ClipsRequest(BaseModel):
    url: str
    # Absolute seconds in the original video to analyse (default: first 2 min).
    start: float = 0.0
    end: float = 120.0
    model: str = "base"  # tiny | base | small


class RenderRequest(BaseModel):
    # Either a clips job (reuses its downloaded source + transcript) …
    job_id: str | None = None
    # … or a raw url + range (downloads that section first).
    url: str | None = None
    start: float = Field(..., description="Absolute start (s) in original video")
    end: float = Field(..., description="Absolute end (s) in original video")
    model: str = "base"
    # --- Configuración: marco ---
    frame_enabled: bool = True
    frame_color: str = DEFAULT_FRAME_COLOR
    frame_width: int = DEFAULT_FRAME_WIDTH
    brand_text: str = DEFAULT_BRAND_TEXT
    title_text: str = ""
    # --- Configuración: subtítulos ---
    show_subtitles: bool = True
    subtitle_style: str = "viral"  # viral | plain | yellow
    subtitle_size: int = 76
    subtitle_position: str = "bottom"  # bottom | middle | top
    # Edited subtitle words (relative to clip start): [{w, s, e}] — the
    # frontend sends these when Juan corrects the auto-transcription.
    edited_words: list[dict] | None = None


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    plan: str
    is_admin: bool
    brand_text: str = DEFAULT_BRAND_TEXT
    frame_enabled: bool = True
    created_at: str | None = None


class UpdateMeRequest(BaseModel):
    name: str | None = None
    brand_text: str | None = None
    frame_enabled: bool | None = None


# ---------------------------------------------------------------- helpers
def _clip_words(transcript: dict, clip_start_rel: float,
                clip_end_rel: float) -> list[dict]:
    words = []
    for s in transcript.get("segments", []):
        for w in s.get("words", []):
            if clip_start_rel <= w["s"] < clip_end_rel:
                words.append({"w": w["w"], "s": round(w["s"] - clip_start_rel, 2),
                              "e": round(w["e"] - clip_start_rel, 2)})
    return [w for w in words if w["w"].strip()]


def _run_clips_job(job_id: str, url: str, start: float, end: float, model: str):
    try:
        jobs.update(job_id, status="running", progress=10,
                    message="Descargando el rango del video…")
        source = download_section(url, start, end, job_id)
        jobs.update(job_id, progress=40, message="Transcribiendo (Whisper local)…")
        transcript = transcribe_file(source, job_id, model=model)
        jobs.update(job_id, progress=80, message="Buscando momentos para clips…")
        candidates = build_candidates(transcript["segments"])
        # Candidates are relative to the section; expose absolute times too.
        for c in candidates:
            c["abs_start"] = round(c["start"] + start, 2)
            c["abs_end"] = round(c["end"] + start, 2)
        info = probe(source)
        jobs.update(job_id, status="done", progress=100,
                    message="Listo.",
                    result={
                        "source_file": source.name,
                        "section_start": start, "section_end": end,
                        "source": info,
                        "transcript_file": Path(transcript["path"]).name,
                        "transcript": transcript,
                        "candidates": candidates,
                    })
    except Exception as e:  # noqa: BLE001 — surfaced to the client as job error
        jobs.update(job_id, status="error", error=str(e), message="Falló.")


def _resolve_source_for_render(req: RenderRequest, job_id: str):
    """Return (source_path, section_start, transcript_dict)."""
    if req.job_id:
        parent = jobs.get(req.job_id)
        if not parent or parent.get("status") != "done" or not parent.get("result"):
            raise HTTPException(400, "job_id no válido o aún no terminado")
        res = parent["result"]
        source = SOURCES_DIR / res["source_file"]
        section_start = float(res["section_start"])
        transcript = json.loads(
            (TRANSCRIPTS_DIR / res["transcript_file"]).read_text(encoding="utf-8"))
        return source, section_start, transcript
    if req.url:
        jobs.update(job_id, status="running", progress=10,
                    message="Descargando el rango del video…")
        source = download_section(req.url, req.start, req.end, job_id)
        jobs.update(job_id, progress=40, message="Transcribiendo (Whisper local)…")
        transcript = transcribe_file(source, job_id, model=req.model)
        return source, req.start, transcript
    raise HTTPException(400, "Pasa job_id o url")


def _run_render_job(job_id: str, req: RenderRequest):
    try:
        source, section_start, transcript = _resolve_source_for_render(req, job_id)
        # Clip window relative to the downloaded section.
        rel_start = req.start - section_start
        rel_end = req.end - section_start
        if rel_start < -1 or rel_end <= rel_start:
            raise ValueError(
                f"El clip {req.start}-{req.end}s no está dentro del rango "
                f"descargado (inicio de sección {section_start}s).")
        rel_start = max(rel_start, 0.0)
        duration = rel_end - rel_start
        if duration > 180:
            raise ValueError("Clip demasiado largo (máx. 180 s en v1).")
        words = req.edited_words
        if words is None and req.show_subtitles:
            words = _clip_words(transcript, rel_start, rel_end)
        jobs.update(job_id, progress=70,
                    message="Renderizando 9:16 con marco y subtítulos…")
        out_name = f"clip_{job_id}.mp4"
        out_path = OUTPUTS_DIR / out_name
        render_clip(
            source, rel_start, duration, out_path, words,
            frame_enabled=req.frame_enabled,
            frame_color=req.frame_color, frame_width=req.frame_width,
            brand_text=req.brand_text, title_text=req.title_text,
            subtitle_style=req.subtitle_style, subtitle_size=req.subtitle_size,
            subtitle_position=req.subtitle_position,
            show_subtitles=req.show_subtitles,
        )
        info = probe(out_path)
        jobs.update(job_id, status="done", progress=100, message="Clip listo.",
                    result={"file": out_name, "download_url": f"/api/download/{out_name}",
                            "duration": duration, "output": info,
                            "frame_enabled": req.frame_enabled,
                            "brand_text": req.brand_text})
        # Durable per-user record for the panel (status starts "pendiente",
        # only an admin can approve/reject it). Never breaks the render.
        try:
            parent = jobs.get(job_id) or {}
            video_url = req.url or ""
            title = req.title_text or ""
            uploader = ""
            if req.job_id:
                src = jobs.get(req.job_id) or {}
                video_url = (src.get("payload") or {}).get("url", video_url)
            db = SessionLocal()
            try:
                create_clip(db, user_id=parent.get("user_id"),
                            video_url=video_url, title=title,
                            uploader=uploader, start=req.start, end=req.end,
                            job_id=job_id, file=out_name)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — panel record is best-effort
            pass
    except Exception as e:  # noqa: BLE001
        jobs.update(job_id, status="error", error=str(e), message="Falló el render.")


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {"ok": True, "service": "MarketingCaribe Clipper", "version": "1.4.0",
            "warp": os.environ.get("WARP_STATUS", "not_configured"),
            "proxy": bool(os.environ.get("YTDLP_PROXY")),
            "db": db_backend(), "auth": True}


# ---------------------------------------------------------------- auth
@app.post("/api/auth/register", response_model=TokenResponse)
def api_register(req: RegisterRequest, db: Session = Depends(get_db)):
    try:
        user = auth.register_user(db, req.name, req.email, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"access_token": auth.create_access_token(user.id),
            "user": auth.user_out(user)}


@app.post("/api/auth/login", response_model=TokenResponse)
def api_login(req: LoginRequest, db: Session = Depends(get_db)):
    user = auth.authenticate_user(db, req.email, req.password)
    if user is None:
        raise HTTPException(401, "Correo o contraseña incorrectos")
    return {"access_token": auth.create_access_token(user.id),
            "user": auth.user_out(user)}


@app.get("/api/auth/me", response_model=UserResponse)
def api_me(user: User = Depends(get_current_user)):
    return auth.user_out(user)


@app.patch("/api/auth/me", response_model=UserResponse)
def api_update_me(req: UpdateMeRequest, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    if req.name is not None:
        if not req.name.strip():
            raise HTTPException(400, "El nombre no puede estar vacío")
        user.name = req.name.strip()
    if req.brand_text is not None:
        user.brand_text = req.brand_text.strip() or DEFAULT_BRAND_TEXT
    if req.frame_enabled is not None:
        user.frame_enabled = bool(req.frame_enabled)
    db.add(user)
    db.commit()
    db.refresh(user)
    return auth.user_out(user)


# ---------------------------------------------------------------- panel (usuario)
@app.get("/api/me/stats")
def api_my_stats(db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    q = db.query(Clip).filter(Clip.user_id == user.id)
    return {
        "clips": q.count(),
        "pendientes": q.filter(Clip.status == STATUS_PENDIENTE).count(),
        "aprobados": q.filter(Clip.status == STATUS_APROBADO).count(),
        "rechazados": q.filter(Clip.status == STATUS_RECHAZADO).count(),
    }


@app.get("/api/me/clips")
def api_my_clips(db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    clips = (db.query(Clip).filter(Clip.user_id == user.id)
             .order_by(Clip.created_at.desc()).all())
    return [clip_out(c) for c in clips]


# ---------------------------------------------------------------- panel admin
def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Solo el administrador puede ver esto")
    return user


@app.get("/api/admin/users")
def api_admin_users(db: Session = Depends(get_db),
                    _admin: User = Depends(require_admin)):
    users = db.query(User).order_by(User.created_at.asc()).all()
    return [auth.user_out(u) for u in users]


@app.get("/api/admin/clips")
def api_admin_clips(db: Session = Depends(get_db),
                    _admin: User = Depends(require_admin)):
    clips = db.query(Clip).order_by(Clip.created_at.desc()).all()
    out = []
    for c in clips:
        row = clip_out(c)
        owner = db.query(User).filter(User.id == c.user_id).first()
        row["user_name"] = owner.name if owner else "Anónimo"
        row["user_email"] = owner.email if owner else ""
        out.append(row)
    return out


def _admin_set_clip(db: Session, clip_id: int, status: str) -> dict:
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if clip is None:
        raise HTTPException(404, "Clip no encontrado")
    if status not in (STATUS_APROBADO, STATUS_RECHAZADO):
        raise HTTPException(400, "Estado no válido")
    clip.status = status
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip_out(clip)


@app.post("/api/admin/clips/{clip_id}/approve")
def api_admin_approve(clip_id: int, db: Session = Depends(get_db),
                      _admin: User = Depends(require_admin)):
    return _admin_set_clip(db, clip_id, STATUS_APROBADO)


@app.post("/api/admin/clips/{clip_id}/reject")
def api_admin_reject(clip_id: int, db: Session = Depends(get_db),
                     _admin: User = Depends(require_admin)):
    return _admin_set_clip(db, clip_id, STATUS_RECHAZADO)


# ---------------------------------------------------------------- planes y pagos
class PlanRequest(BaseModel):
    name: str
    price_dop: int
    duration_days: int = 30
    features: list[str] = []
    is_popular: bool = False
    is_active: bool = True
    sort_order: int = 0


class MethodRequest(BaseModel):
    name: str
    bank: str = ""
    account_number: str = ""
    account_holder: str = ""
    account_type: str = ""
    instructions: str = ""
    is_active: bool = True


class DecideRequest(BaseModel):
    admin_note: str = ""


@app.get("/api/plans")
def api_plans(db: Session = Depends(get_db)):
    plans = (db.query(Plan).filter(Plan.is_active.is_(True))
             .order_by(Plan.sort_order.asc(), Plan.id.asc()).all())
    return [plan_out(p) for p in plans]


@app.post("/api/admin/plans")
def api_admin_create_plan(req: PlanRequest, db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    plan = Plan(**req.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan_out(plan)


@app.put("/api/admin/plans/{plan_id}")
def api_admin_update_plan(plan_id: int, req: PlanRequest,
                          db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if plan is None:
        raise HTTPException(404, "Plan no encontrado")
    for k, v in req.model_dump().items():
        setattr(plan, k, v)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan_out(plan)


@app.delete("/api/admin/plans/{plan_id}")
def api_admin_delete_plan(plan_id: int, db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if plan is None:
        raise HTTPException(404, "Plan no encontrado")
    db.delete(plan)
    db.commit()
    return {"ok": True, "deleted": plan_id}


@app.get("/api/payment-methods")
def api_payment_methods(db: Session = Depends(get_db),
                        _user: User = Depends(get_current_user)):
    methods = (db.query(PaymentMethod)
               .filter(PaymentMethod.is_active.is_(True))
               .order_by(PaymentMethod.id.asc()).all())
    return [method_out(m) for m in methods]


@app.post("/api/admin/payment-methods")
def api_admin_create_method(req: MethodRequest, db: Session = Depends(get_db),
                            _admin: User = Depends(require_admin)):
    method = PaymentMethod(**req.model_dump())
    db.add(method)
    db.commit()
    db.refresh(method)
    return method_out(method)


@app.put("/api/admin/payment-methods/{method_id}")
def api_admin_update_method(method_id: int, req: MethodRequest,
                            db: Session = Depends(get_db),
                            _admin: User = Depends(require_admin)):
    method = db.query(PaymentMethod).filter(PaymentMethod.id == method_id).first()
    if method is None:
        raise HTTPException(404, "Método no encontrado")
    for k, v in req.model_dump().items():
        setattr(method, k, v)
    db.add(method)
    db.commit()
    db.refresh(method)
    return method_out(method)


@app.delete("/api/admin/payment-methods/{method_id}")
def api_admin_delete_method(method_id: int, db: Session = Depends(get_db),
                            _admin: User = Depends(require_admin)):
    method = db.query(PaymentMethod).filter(PaymentMethod.id == method_id).first()
    if method is None:
        raise HTTPException(404, "Método no encontrado")
    db.delete(method)
    db.commit()
    return {"ok": True, "deleted": method_id}


MAX_RECEIPT_BYTES = 5 * 1024 * 1024  # 5 MB
RECEIPT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif",
                 "application/pdf"}


@app.post("/api/subscriptions")
async def api_create_subscription(
        plan_id: int = Form(...), payment_method_id: int = Form(...),
        receipt: UploadFile = File(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user)):
    plan = db.query(Plan).filter(Plan.id == plan_id,
                                 Plan.is_active.is_(True)).first()
    if plan is None:
        raise HTTPException(400, "Plan no válido o inactivo")
    method = db.query(PaymentMethod).filter(
        PaymentMethod.id == payment_method_id,
        PaymentMethod.is_active.is_(True)).first()
    if method is None:
        raise HTTPException(400, "Método de pago no válido o inactivo")
    if receipt.content_type not in RECEIPT_TYPES:
        raise HTTPException(400, "El comprobante debe ser imagen o PDF")
    data = await receipt.read()
    if len(data) > MAX_RECEIPT_BYTES:
        raise HTTPException(400, "El comprobante no puede pasar de 5 MB")
    if not data:
        raise HTTPException(400, "El comprobante está vacío")
    ext = (Path(receipt.filename or "comprobante").suffix or "").lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"):
        ext = ".pdf" if receipt.content_type == "application/pdf" else ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    (RECEIPTS_DIR / filename).write_bytes(data)
    req = PaymentRequest(user_id=user.id, plan_id=plan.id,
                         payment_method_id=method.id, receipt_file=filename)
    db.add(req)
    db.commit()
    db.refresh(req)
    return request_out(req, plan=plan, method=method, user=user)


@app.get("/api/subscriptions/mine")
def api_my_subscriptions(db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    reqs = (db.query(PaymentRequest).filter(PaymentRequest.user_id == user.id)
            .order_by(PaymentRequest.created_at.desc()).all())
    out = []
    for r in reqs:
        plan = db.query(Plan).filter(Plan.id == r.plan_id).first()
        method = db.query(PaymentMethod).filter(
            PaymentMethod.id == r.payment_method_id).first()
        out.append(request_out(r, plan=plan, method=method, user=user))
    active = get_active_subscription(db, user.id)
    return {"requests": out,
            "active_subscription": (subscription_out(
                active, db.query(Plan).filter(Plan.id == active.plan_id).first())
                if active else None)}


@app.get("/api/admin/subscriptions")
def api_admin_subscriptions(db: Session = Depends(get_db),
                            _admin: User = Depends(require_admin)):
    reqs = (db.query(PaymentRequest)
            .order_by(PaymentRequest.created_at.desc()).all())
    out = []
    for r in reqs:
        out.append(request_out(
            r, plan=db.query(Plan).filter(Plan.id == r.plan_id).first(),
            method=db.query(PaymentMethod).filter(
                PaymentMethod.id == r.payment_method_id).first(),
            user=db.query(User).filter(User.id == r.user_id).first()))
    return out


@app.post("/api/admin/subscriptions/{req_id}/approve")
def api_admin_approve_sub(req_id: int, body: DecideRequest | None = None,
                          db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    req = db.query(PaymentRequest).filter(PaymentRequest.id == req_id).first()
    if req is None:
        raise HTTPException(404, "Solicitud no encontrada")
    if req.status != billing.REQ_PENDING:
        raise HTTPException(400, f"La solicitud ya está {req.status}")
    try:
        sub = approve_request(db, req, (body.admin_note if body else ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"request": _request_full(db, req),
            "subscription": subscription_out(
                sub, db.query(Plan).filter(Plan.id == sub.plan_id).first())}


@app.post("/api/admin/subscriptions/{req_id}/reject")
def api_admin_reject_sub(req_id: int, body: DecideRequest | None = None,
                         db: Session = Depends(get_db),
                         _admin: User = Depends(require_admin)):
    req = db.query(PaymentRequest).filter(PaymentRequest.id == req_id).first()
    if req is None:
        raise HTTPException(404, "Solicitud no encontrada")
    if req.status != billing.REQ_PENDING:
        raise HTTPException(400, f"La solicitud ya está {req.status}")
    reject_request(db, req, (body.admin_note if body else ""))
    return _request_full(db, req)


def _request_full(db: Session, req: PaymentRequest) -> dict:
    return request_out(
        req, plan=db.query(Plan).filter(Plan.id == req.plan_id).first(),
        method=db.query(PaymentMethod).filter(
            PaymentMethod.id == req.payment_method_id).first(),
        user=db.query(User).filter(User.id == req.user_id).first())


@app.get("/api/admin/receipts/{filename}")
def api_admin_receipt(filename: str,
                      _admin: User = Depends(require_admin)):
    path = RECEIPTS_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404, "Comprobante no encontrado")
    return FileResponse(path)


@app.post("/api/video-info")
def api_video_info(req: VideoInfoRequest):
    try:
        return video_info(req.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"No se pudo leer el video: {e}") from e


def _require_plan(db: Session, user: User | None) -> None:
    """Bloqueo sin plan: solo aplica al usuario logueado. Sin token, en
    v1 los endpoints siguen públicos para no romper el flujo actual."""
    if user is not None and not has_active_subscription(db, user.id):
        raise HTTPException(
            403, "Debes contratar un plan para usar el clipper")


@app.post("/api/clips")
def api_clips(req: ClipsRequest, background: BackgroundTasks,
              user: User | None = Depends(get_optional_user),
              db: Session = Depends(get_db)):
    _require_plan(db, user)
    if req.end <= req.start:
        raise HTTPException(400, "end debe ser mayor que start")
    job = jobs.create("clips", req.model_dump())
    if user is not None:
        jobs.update(job["id"], user_id=user.id)
    background.add_task(_run_clips_job, job["id"], req.url, req.start, req.end, req.model)
    return {"job_id": job["id"], "status": job["status"]}


@app.post("/api/render")
def api_render(req: RenderRequest, background: BackgroundTasks,
               user: User | None = Depends(get_optional_user),
               db: Session = Depends(get_db)):
    _require_plan(db, user)
    if req.end <= req.start:
        raise HTTPException(400, "end debe ser mayor que start")
    job = jobs.create("render", req.model_dump())
    if user is not None:
        jobs.update(job["id"], user_id=user.id)
    background.add_task(_run_render_job, job["id"], req)
    return {"job_id": job["id"], "status": job["status"]}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job no encontrado")
    return job


@app.get("/api/download/{filename}")
def api_download(filename: str):
    path = OUTPUTS_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404, "Archivo no encontrado")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


# Minimal frontend (same origin, no extra server).
_frontend = BASE_DIR / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
