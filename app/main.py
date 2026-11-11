"""ClipViral — FastAPI backend.

No YouTube API key: yt-dlp reads the pasted link directly.
Endpoints:
  GET  /                        health
  POST /api/video-info          {url} -> title/uploader/duration
  POST /api/upload              video file (max 500 MB) -> {file_id}
  POST /api/clips               {url|source_file, start, end, model} -> job
                                (download + transcribe + clip candidates)
  POST /api/render              {job_id|url|source_file, start, end, ...} -> job
  GET  /api/jobs/{id}           job status/result
  GET  /api/download/{file}    rendered mp4 — NO approval needed: any user
                                with a plan downloads right after render
                                (clip approval was removed 2026-11-10)
  POST /api/auth/register       {name, email, password} -> user + token
  POST /api/auth/login          {email, password} -> token
  GET  /api/auth/me            (token) -> current user
  GET  /api/me/stats           clips este mes / en proceso / total
  POST /api/withdrawals         request a withdrawal (amount, method, details)
  POST /api/admin/impersonate/{user_id}  admin -> JWT for that user
  GET/POST /api/admin/settings  platform defaults (brand, frame)

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
  subtitle_position / show_subtitles / quality: see RenderRequest.
"""
import json
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, billing, clips_db, jobs, settings
from .auth import User, get_current_user, get_optional_user
from .billing import (PaymentMethod, PaymentRequest, Plan, UserSubscription,
                      approve_request, get_active_subscription,
                      has_active_subscription, method_out, plan_out,
                      reject_request, request_out, subscription_out)
from .clips import build_candidates, build_candidates_llm
from .clips_db import Clip, clip_out, create_clip
from .config import (BASE_DIR, DEFAULT_BRAND_TEXT, DEFAULT_FRAME_COLOR,
                     DEFAULT_FRAME_WIDTH, MAX_UPLOAD_BYTES, MAX_WORKERS,
                     OUTPUTS_DIR, RECEIPTS_DIR, SOURCES_DIR, TRANSCRIPTS_DIR,
                     UPLOADS_DIR)
from .db import SessionLocal, db_backend, get_db, init_db
from .render import render_clip
from .settings import (WD_APPROVED, WD_PENDING, WD_REJECTED,
                       WithdrawalRequest, get_setting, set_setting,
                       withdrawal_out)
from .transcribe import transcribe_file, transcription_engine
from .video import download_section, probe, video_info


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # crea la tabla users si no existe (Postgres o SQLite)
    yield


app = FastAPI(title="ClipViral", version="1.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


@app.exception_handler(Exception)
async def _json_500_handler(_request, _exc):
    """Any unhandled error still returns JSON (never an empty/plain body),
    so the frontend's response.json() never throws
    "Unexpected end of JSON input" at the user."""
    return JSONResponse(
        status_code=500,
        content={"detail": "Error interno del servidor, inténtalo de nuevo"},
    )


# ---------------------------------------------------------------- models
class VideoInfoRequest(BaseModel):
    url: str


class ClipsRequest(BaseModel):
    # Either a YouTube url …
    url: str | None = None
    # … or an uploaded file (POST /api/upload returns its file_id).
    source_file: str | None = None
    # Absolute seconds in the original video to analyse (default: first 2 min).
    start: float = 0.0
    end: float = 120.0
    model: str = "base"  # tiny | base | small
    # Clip prefs (defaults come from the user's saved config).
    max_clips: int = 5
    min_len: float = 20.0
    max_len: float = 45.0


class RenderRequest(BaseModel):
    # Either a clips job (reuses its downloaded source + transcript) …
    job_id: str | None = None
    # … or a raw url + range (downloads that section first) …
    url: str | None = None
    # … or an uploaded file + range (cuts that section first).
    source_file: str | None = None
    start: float = Field(..., description="Absolute start (s) in original video")
    end: float = Field(..., description="Absolute end (s) in original video")
    model: str = "base"
    quality: str = "1080p"  # 720p | 1080p
    # --- Configuración: marco ---
    frame_enabled: bool = True
    frame_color: str = DEFAULT_FRAME_COLOR
    frame_width: int = DEFAULT_FRAME_WIDTH
    brand_text: str = DEFAULT_BRAND_TEXT
    title_text: str = ""
    # --- Clip identity for the library: the selected candidate's hook
    # title + virality score (LLM) or score (heuristic). The frontend
    # sends these from the chosen candidate; video_title is the source
    # video's title from /api/video-info.
    clip_title: str = ""
    clip_score: float | None = None
    video_title: str = ""
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
    max_clips: int = 5
    min_clip_secs: float = 20.0
    max_clip_secs: float = 45.0
    quality: str = "1080p"
    subtitles_enabled: bool = True
    template: str = "Gancho grande"
    created_at: str | None = None


class UpdateMeRequest(BaseModel):
    name: str | None = None
    brand_text: str | None = None
    frame_enabled: bool | None = None
    max_clips: int | None = None
    min_clip_secs: float | None = None
    max_clip_secs: float | None = None
    quality: str | None = None
    subtitles_enabled: bool | None = None
    template: str | None = None


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


def _cut_upload(upload: Path, start: float, end: float, job_id: str) -> Path:
    """Cut [start, end] out of an uploaded video into data/sources/.

    Stream copy first (fast); if the container won't cut cleanly, fall
    back to a re-encode. Returns the cut file.
    """
    out = SOURCES_DIR / f"{job_id}.mp4"
    dur = max(1.0, end - start)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(upload),
           "-t", str(dur), "-c", "copy", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(upload),
               "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast",
               "-c:a", "aac", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(f"No se pudo cortar el archivo subido: {proc.stderr[-500:]}")
    return out


def _upload_path(file_id: str) -> Path:
    """Resolve an upload file_id, guarding against path traversal."""
    name = Path(file_id or "").name
    if not name or name != (file_id or ""):
        raise HTTPException(400, "Archivo subido no válido")
    path = UPLOADS_DIR / name
    if not path.exists():
        raise HTTPException(404, "Archivo subido no encontrado")
    return path


def _run_clips_job(job_id: str, url: str | None, start: float, end: float,
                   model: str, source_file: str | None = None,
                   max_clips: int = 5, min_len: float = 20.0,
                   max_len: float = 45.0):
    try:
        if source_file:
            jobs.update(job_id, status="running", progress=10,
                        message="Leyendo tu archivo…")
            source = _cut_upload(_upload_path(source_file), start, end, job_id)
        else:
            jobs.update(job_id, status="running", progress=10,
                        message="Descargando el video…")
            source = download_section(url, start, end, job_id)
        jobs.update(job_id, progress=40, message="Transcribiendo el audio…")
        transcript = transcribe_file(source, job_id, model=model)
        jobs.update(job_id, progress=80,
                    message="La IA está eligiendo los mejores momentos…")
        # Real LLM first (Groq llama, free tier) when a key is set; ANY
        # LLM failure returns None and we fall back to the heuristic, so
        # a bad LLM day can never fail a clips job. No key = heuristic.
        candidates = build_candidates_llm(transcript["segments"],
                                          max_clips=max_clips,
                                          min_len=min_len, max_len=max_len)
        selector = "llm"
        if candidates is None:
            selector = "heuristic"
            candidates = build_candidates(transcript["segments"],
                                          min_len=min_len, max_len=max_len)
        candidates = candidates[:max(1, max_clips)]
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
                        "selector": selector,
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
    if req.source_file:
        jobs.update(job_id, status="running", progress=10,
                    message="Leyendo tu archivo…")
        source = _cut_upload(_upload_path(req.source_file), req.start, req.end, job_id)
        jobs.update(job_id, progress=40, message="Transcribiendo el audio…")
        transcript = transcribe_file(source, job_id, model=req.model)
        return source, req.start, transcript
    if req.url:
        jobs.update(job_id, status="running", progress=10,
                    message="Descargando el video…")
        source = download_section(req.url, req.start, req.end, job_id)
        jobs.update(job_id, progress=40, message="Transcribiendo el audio…")
        transcript = transcribe_file(source, job_id, model=req.model)
        return source, req.start, transcript
    raise HTTPException(400, "Pasa job_id, url o un archivo subido")


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
                    message="Cortando tu clip…")
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
            quality=req.quality,
        )
        info = probe(out_path)
        jobs.update(job_id, status="done", progress=100, message="Clip listo.",
                    result={"file": out_name, "download_url": f"/api/download/{out_name}",
                            "duration": duration, "output": info,
                            "frame_enabled": req.frame_enabled,
                            "brand_text": req.brand_text})
        # Durable per-user record for the panel. The clip is downloadable
        # IMMEDIATELY — no admin approval for clips (reversed 2026-11-10).
        # Never breaks the render.
        try:
            parent = jobs.get(job_id) or {}
            video_url = req.url or ""
            # Library card title: the chosen candidate's hook title (LLM),
            # else the source video's title, else the on-video title text.
            title = req.clip_title or req.video_title or req.title_text or ""
            uploader = ""
            if req.job_id:
                src = jobs.get(req.job_id) or {}
                video_url = (src.get("payload") or {}).get("url", video_url)
            db = SessionLocal()
            try:
                # job_id guardado = el trabajo de clips del que salió este
                # clip (su transcripción y su video fuente viven ahí, y el
                # botón Editar los necesita). Sin trabajo padre (URL o
                # archivo directo), el propio trabajo de render.
                create_clip(db, user_id=parent.get("user_id"),
                            video_url=video_url, title=title,
                            uploader=uploader, start=req.start, end=req.end,
                            job_id=(req.job_id or job_id), file=out_name,
                            score=req.clip_score,
                            section_start=section_start)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — panel record is best-effort
            pass
    except Exception as e:  # noqa: BLE001
        jobs.update(job_id, status="error", error=str(e), message="Falló el render.")


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {"ok": True, "service": "ClipViral", "version": "1.4.0",
            "warp": os.environ.get("WARP_STATUS", "not_configured"),
            "proxy": bool(os.environ.get("YTDLP_PROXY")),
            "transcription": transcription_engine(),
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
    if req.max_clips is not None:
        user.max_clips = max(1, min(15, int(req.max_clips)))
    if req.min_clip_secs is not None:
        user.min_clip_secs = max(5.0, float(req.min_clip_secs))
    if req.max_clip_secs is not None:
        user.max_clip_secs = max(10.0, min(180.0, float(req.max_clip_secs)))
    if req.quality is not None and req.quality in ("720p", "1080p"):
        user.quality = req.quality
    if req.subtitles_enabled is not None:
        user.subtitles_enabled = bool(req.subtitles_enabled)
    if req.template is not None and req.template.strip():
        user.template = req.template.strip()[:80]
    db.add(user)
    db.commit()
    db.refresh(user)
    return auth.user_out(user)


# ---------------------------------------------------------------- panel (usuario)
@app.get("/api/me/stats")
def api_my_stats(db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    from .config import JOBS_DIR

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    q = db.query(Clip).filter(Clip.user_id == user.id)
    total = q.count()
    this_month = q.filter(Clip.created_at >= month_start).count()
    # Videos en proceso: jobs en cola o corriendo de este usuario (v1: JSON).
    in_process = 0
    try:
        for path in JOBS_DIR.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — skip corrupt job files
                continue
            if job.get("user_id") == user.id and job.get("status") in ("queued", "running"):
                in_process += 1
    except Exception:  # noqa: BLE001 — stats are best-effort
        in_process = 0
    return {
        "clips_this_month": this_month,
        "videos_in_process": in_process,
        "clips_total": total,
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


# NOTA 2026-11-10: la aprobación de clips por el admin fue ELIMINADA por
# decisión de Juan — los clips se descargan apenas se renderizan. Los
# endpoints approve/reject de clips ya no existen; solo la aprobación de
# PAGOS (suscripciones) sigue siendo manual.


# ---------------------------------------------------------------- subida de video
UPLOAD_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...),
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    _require_plan(db, user)
    ext = (Path(file.filename or "").suffix or "").lower()
    if ext not in UPLOAD_EXTS:
        raise HTTPException(
            400, "El archivo debe ser un video (mp4, mov, webm, mkv o avi)")
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS_DIR / filename
    total = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        400, "El video no puede pasar de 500 MB")
                out.write(chunk)
    except HTTPException:
        if dest.exists():
            dest.unlink()
        raise
    return {"file_id": filename, "filename": file.filename or filename,
            "size": total}


# ---------------------------------------------------------------- impersonar (admin entra a la cuenta de un usuario)
@app.post("/api/admin/impersonate/{user_id}")
def api_admin_impersonate(user_id: int, db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(404, "Usuario no encontrado")
    if target.is_admin:
        raise HTTPException(400, "No se puede impersonar a otro admin")
    return {"access_token": auth.create_access_token(target.id),
            "user": auth.user_out(target)}


# ---------------------------------------------------------------- retiros
class DecideRequest(BaseModel):
    admin_note: str = ""


class WithdrawalRequestIn(BaseModel):
    amount_dop: int
    method: str = ""
    details: str = ""


@app.post("/api/withdrawals")
def api_create_withdrawal(req: WithdrawalRequestIn,
                          db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    if req.amount_dop <= 0:
        raise HTTPException(400, "El monto debe ser mayor que 0")
    if not req.method.strip():
        raise HTTPException(400, "Indica el método de retiro")
    w = WithdrawalRequest(user_id=user.id, amount_dop=int(req.amount_dop),
                          method=req.method.strip()[:120],
                          details=req.details.strip()[:500])
    db.add(w)
    db.commit()
    db.refresh(w)
    return withdrawal_out(w, user=user)


@app.get("/api/withdrawals/mine")
def api_my_withdrawals(db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    rows = (db.query(WithdrawalRequest)
            .filter(WithdrawalRequest.user_id == user.id)
            .order_by(WithdrawalRequest.created_at.desc()).all())
    return [withdrawal_out(w, user=user) for w in rows]


@app.get("/api/admin/withdrawals")
def api_admin_withdrawals(db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    rows = (db.query(WithdrawalRequest)
            .order_by(WithdrawalRequest.created_at.desc()).all())
    out = []
    for w in rows:
        out.append(withdrawal_out(
            w, db.query(User).filter(User.id == w.user_id).first()))
    return out


def _decide_withdrawal(db: Session, w_id: int, status: str,
                       admin_note: str) -> dict:
    w = db.query(WithdrawalRequest).filter(WithdrawalRequest.id == w_id).first()
    if w is None:
        raise HTTPException(404, "Solicitud de retiro no encontrada")
    if w.status != WD_PENDING:
        raise HTTPException(400, f"La solicitud ya está {w.status}")
    w.status = status
    w.admin_note = admin_note or ""
    w.decided_at = datetime.now(timezone.utc)
    db.add(w)
    db.commit()
    db.refresh(w)
    return withdrawal_out(
        w, db.query(User).filter(User.id == w.user_id).first())


@app.post("/api/admin/withdrawals/{w_id}/approve")
def api_admin_approve_withdrawal(w_id: int, body: DecideRequest | None = None,
                                 db: Session = Depends(get_db),
                                 _admin: User = Depends(require_admin)):
    return _decide_withdrawal(db, w_id, WD_APPROVED,
                              body.admin_note if body else "")


@app.post("/api/admin/withdrawals/{w_id}/reject")
def api_admin_reject_withdrawal(w_id: int, body: DecideRequest | None = None,
                                db: Session = Depends(get_db),
                                _admin: User = Depends(require_admin)):
    return _decide_withdrawal(db, w_id, WD_REJECTED,
                              body.admin_note if body else "")


# ---------------------------------------------------------------- configuración de la plataforma (admin)
class SettingsRequest(BaseModel):
    settings: dict


@app.get("/api/admin/settings")
def api_admin_get_settings(db: Session = Depends(get_db),
                           _admin: User = Depends(require_admin)):
    from .settings import DEFAULT_SETTINGS

    return {k: get_setting(db, k, v) for k, v in DEFAULT_SETTINGS.items()}


@app.post("/api/admin/settings")
def api_admin_save_settings(req: SettingsRequest,
                            db: Session = Depends(get_db),
                            _admin: User = Depends(require_admin)):
    from .settings import DEFAULT_SETTINGS

    out = {}
    for k, v in (req.settings or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        set_setting(db, k, str(v)[:500])
        out[k] = str(v)[:500]
    return out


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
    if not req.url and not req.source_file:
        raise HTTPException(400, "Pega un link de YouTube o sube un video")
    if req.url and not req.source_file and req.end <= req.start:
        raise HTTPException(400, "end debe ser mayor que start")
    if req.source_file and req.end <= req.start:
        raise HTTPException(400, "end debe ser mayor que start")
    job = jobs.create("clips", req.model_dump())
    if user is not None:
        jobs.update(job["id"], user_id=user.id)
    background.add_task(_run_clips_job, job["id"], req.url, req.start, req.end,
                        req.model, req.source_file, req.max_clips, req.min_len,
                        req.max_len)
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


@app.get("/api/jobs/{job_id}/transcript")
def api_job_transcript(job_id: str):
    """Transcript of a clips job + the section's absolute start, so the
    panel can reopen a stored clip in the subtitle editor (Edit button).
    The transcript JSON lives in data/transcripts/{job_id}.json; the
    section start comes from the job record on disk (jobs survive
    restarts — they are JSON files, not memory). 404 if the transcript
    is gone. Same public-in-v1 posture as /api/download.
    """
    safe_id = Path(job_id).name
    path = TRANSCRIPTS_DIR / f"{safe_id}.json"
    if not path.exists():
        raise HTTPException(404, "Transcripción no encontrada")
    job = jobs.get(safe_id) or {}
    section_start = (job.get("result") or {}).get("section_start")
    if section_start is None:
        # Renders from a raw URL/upload have no section_start in the
        # result; the requested start is in the job payload.
        section_start = (job.get("payload") or {}).get("start")
    return {"job_id": safe_id,
            "section_start": section_start,
            "transcript": json.loads(path.read_text(encoding="utf-8"))}


@app.get("/api/jobs/{job_id}/source")
def api_job_source(job_id: str):
    """Serve the downloaded section file of a clips job, so the browser can
    show real video thumbnails per candidate and play the selected clip in
    the live preview. FileResponse supports Range requests, which is what
    <video> needs to seek (and for the #t= fragment to pick a frame).
    Same public-in-v1 posture as /api/download."""
    safe_id = Path(job_id).name
    path = None
    job = jobs.get(safe_id)
    if job and (job.get("result") or {}).get("source_file"):
        path = SOURCES_DIR / Path(job["result"]["source_file"]).name
    if path is None or not path.exists():
        # The in-memory job record may be gone (restart wipes jobs), but
        # the section file on disk survives — resolve it by job id.
        files = sorted(SOURCES_DIR.glob(f"{safe_id}.*"))
        path = files[0] if files else None
    if path is None or not path.exists():
        raise HTTPException(404, "Video no encontrado")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


# Minimal frontend (same origin, no extra server).
_frontend = BASE_DIR / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
