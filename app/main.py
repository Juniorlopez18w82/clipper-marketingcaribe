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
import logging
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, billing, clips_db, jobs, lives, settings
from .auth import User, get_current_user, get_optional_user
from .billing import (PaymentMethod, PaymentRequest, Plan, REQ_APPROVED,
                      UserSubscription, approve_request, assign_plan,
                      get_active_subscription, has_active_subscription,
                      method_out, plan_out, reject_request, request_out,
                      subscription_out)
from .clips import build_candidates, build_candidates_llm
from .clips_db import Clip, clip_out, create_clip
from .config import (BASE_DIR, DEFAULT_BRAND_TEXT, DEFAULT_FRAME_COLOR,
                     DEFAULT_FRAME_WIDTH, IMAGES_DIR, MAX_UPLOAD_BYTES,
                     MAX_WORKERS, OUTPUTS_DIR, RECEIPTS_DIR, SOURCES_DIR,
                     TRANSCRIPTS_DIR, UPLOADS_DIR, VIDEOS_DIR)
from .db import SessionLocal, db_backend, get_db, init_db
from .lives import (EDGE_SAFETY_SECONDS, STATUS_ACTIVE, STATUS_ENDED,
                    STATUS_PAUSED, STATUS_STOPPED, LiveSession,
                    live_session_out)
from .images import (ASPECTS, GeneratedImage, create_image,
                     fetch_image_models, get_model as get_image_model,
                     image_out, refresh_image, submit_image,
                     user_can_generate_images)
from .videos import (AIVideoJob, build_fal_payload, create_job,
                     fetch_video_models, get_model, job_out, refresh_job,
                     submit_video, user_can_generate_videos)
from .render import DEFAULT_TEMPLATE, TEMPLATES, render_clip, resolve_template
from .settings import get_setting, set_setting
from .transcribe import transcribe_file, transcription_engine
from .video import download_section, live_status, probe, video_info

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # crea la tabla users si no existe (Postgres o SQLite)
    _start_lives_monitor()  # hilo del monitor de Lives (sesiones activas)
    yield


app = FastAPI(title="ClipViral", version="1.5.0", lifespan=lifespan)
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
    # Live session this render belongs to (Lives monitor only): the
    # clip record is then linked to the session so the Lives section
    # lists it. None = normal clip from Crear Clip.
    live_session_id: int | None = None
    # Re-render an EXISTING clip (Editar > Guardar título y portada):
    # instead of creating a new clip record, the render overwrites
    # that clip's mp4 and updates its title/cover_at (2026-11-11).
    update_clip_id: int | None = None
    # Cover offset (s, relative to the clip start) chosen in the editor.
    # Stored on the clip; only changes the thumbnail in Mis Clips.
    cover_at: float | None = None
    # --- Estilo de la portada (modal Configurar portada, 2026-11-11) ---
    # Color del texto, borde de la letra (color + grosor), fondo de las
    # letras (color + si lleva caja) y tamaño del título de enganche.
    title_color: str | None = None
    title_border_color: str | None = None
    title_border_w: int | None = None
    title_bg_color: str | None = None
    title_bg: bool = True
    title_size: int | None = None
    # Posición del título en la portada (0.0 = arriba/izquierda,
    # 0.5 = centrado, 1.0 = abajo/derecha). Antes era siempre centrado.
    title_pos_x: float | None = None
    title_pos_y: float | None = None
    # --- Configuración: cámara (paso 3) ---
    # center (recorte fijo en el centro, como siempre) | dual (dos
    # personas, izquierda arriba y derecha abajo) | follow_speaker
    # (sigue a quien habla) | follow_person (sigue a una persona).
    # Default follow_speaker (2026-11-11): en entrevistas/podcasts el
    # que habla suele estar a un lado (Masha a la derecha) y "center"
    # lo cortaba; follow_speaker lo centra y, si no hay cara, cae al
    # centro igual.
    camera_mode: str = "follow_speaker"
    # left | center | right — solo para follow_person.
    camera_person: str = "center"
    # Esquina de la facecam — solo para camera_mode="split" (pantalla
    # dividida): "top-left" | "top-right" | "bottom-left" | "bottom-right".
    facecam_corner: str = "top-left"
    # --- Configuración: subtítulos ---
    show_subtitles: bool = True
    subtitle_style: str = "viral"  # viral | plain | yellow
    subtitle_size: int = 76
    subtitle_position: str = "bottom"  # bottom | middle | top
    # Color de la palabra clave (fondo): elección del editor para este
    # render. None = el de la plantilla o el default de la plataforma.
    highlight_color: str | None = None
    # --- Configuración: plantilla ---
    # Nombre de plantilla ("Gancho grande" / "Limpio" / "Impacto").
    # None = el usuario tocó un control de la plantilla en el editor:
    # render personalizado con sus valores, sin preset. Un nombre
    # desconocido cae a la plantilla guardada del usuario → la
    # plantilla por defecto de la plataforma → "Gancho grande".
    template: str | None = None
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
    # Fin del plan (ISO) para la cuenta atrás del encabezado: ends_at de
    # la suscripción activa, o created_at + 30 días para el admin sin
    # suscripción; None = usuario sin plan (nunca se inventa una fecha).
    plan_expires: str | None = None


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
            # Solo AUDIO para el análisis (2026-11-11): transcribir solo
            # necesita el audio; bajar el video completo de un video largo
            # (866 s a 1080p) dejaba el trabajo atascado en "Descargando"
            # 5-10 min. El audio son ~14 MB y baja en segundos; el video
            # se baja después, solo para el trozo del clip, en el render
            # y en la vista previa (clip-video).
            jobs.update(job_id, status="running", progress=10,
                        message="Descargando el audio…")
            source = download_section(url, start, end, job_id, audio_only=True)
        jobs.update(job_id, progress=40, message="Transcribiendo el audio…")
        transcript = transcribe_file(source, job_id, model=model)
        if not transcript["segments"]:
            raise RuntimeError(
                "No se pudo transcribir el audio de este video. "
                "Prueba con otro rango u otro video.")
        jobs.update(job_id, progress=80,
                    message="La IA está eligiendo los mejores momentos…")
        # Real LLM first (Groq llama, free tier) when a key is set; ANY
        # LLM failure returns None and we fall back to the heuristic, so
        # a bad LLM day can never fail a clips job. No key = heuristic.
        candidates = build_candidates_llm(transcript["segments"],
                                          max_clips=max_clips,
                                          min_len=min_len, max_len=max_len,
                                          abs_start=start)
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
                        # Para el render y la vista previa: el análisis
                        # baja solo audio (rápido); el video del clip se
                        # baja después, solo para ese trozo, con esta URL.
                        "url": url,
                        "audio_only": (source_file is None),
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
        # El análisis baja solo AUDIO (rápido); el render necesita el
        # VIDEO, pero solo para el trozo del clip (30-60 s), así que se
        # baja ahora a 1080p. Si el padre ya tenía video, se reutiliza.
        if res.get("audio_only") and res.get("url"):
            jobs.update(job_id, status="running", progress=10,
                        message="Descargando el video del clip…")
            source = download_section(res["url"], req.start, req.end, job_id)
            section_start = req.start
        else:
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


def _plan_expires_for(db, user: User) -> str | None:
    """Fin del plan del usuario (ISO) para la cuenta atrás del encabezado
    (2026-11-10, pedido de Juan). Suscripción activa y vigente → su
    ends_at. El ADMIN nunca compra: sin suscripción, su plan corre 30
    días desde que se creó su cuenta (created_at + 30 días). Usuario
    normal sin suscripción → None: nunca se inventa una fecha."""
    sub = get_active_subscription(db, user.id)
    if sub is not None and sub.ends_at is not None:
        return sub.ends_at.isoformat()
    if getattr(user, "is_admin", False) and user.created_at is not None:
        return (user.created_at + timedelta(days=30)).isoformat()
    return None


def _valid_hex(color: str | None) -> str | None:
    """#RRGGBB válido, o None."""
    if not color or not isinstance(color, str):
        return None
    c = color.strip()
    if len(c) == 7 and c.startswith("#"):
        try:
            int(c[1:], 16)
            return c.upper()
        except ValueError:
            return None
    return None


def _resolve_render_look(db, user_id: int | None, req: RenderRequest) -> dict:
    """Resuelve plantilla + color de palabra clave para un render.

    - req.template es un nombre válido → esa plantilla (sus valores de
      marco/color/posición son autoritativos).
    - req.template es None → render personalizado: el usuario tocó un
      control en el editor; se usan los valores del request, sin preset.
    - req.template es un nombre desconocido → la plantilla guardada del
      usuario → la plantilla por defecto de la plataforma → "Gancho
      grande".
    - Color de palabra clave: elección del editor (req.highlight_color)
      → color de la plantilla → default de la plataforma
      (subtitle_highlight_color) → #FADE4C.
    """
    preset = None
    template_name: str | None = None
    tpl = (req.template or "").strip()
    if tpl in TEMPLATES:
        template_name, preset = resolve_template(tpl)
    elif tpl:
        # Nombre desconocido: la plantilla guardada del usuario → la
        # default de la plataforma → "Gancho grande".
        user_tpl = None
        if user_id is not None:
            u = db.query(User).filter(User.id == user_id).first()
            user_tpl = getattr(u, "template", None) if u else None
        if user_tpl:
            template_name, preset = resolve_template(user_tpl)
        else:
            default_tpl = get_setting(db, "default_template", DEFAULT_TEMPLATE)
            template_name, preset = resolve_template(default_tpl)
    if preset is not None:
        look = {"template": template_name,
                "frame_enabled": preset["frame_enabled"],
                "frame_color": preset["frame_color"],
                "frame_width": preset["frame_width"],
                "subtitle_position": preset["subtitle_position"],
                "brand_size": preset["brand_size"],
                "brand_y": preset["brand_y"]}
        hl = (_valid_hex(req.highlight_color) or preset["highlight_color"])
    else:
        look = {"template": None,
                "frame_enabled": req.frame_enabled,
                "frame_color": req.frame_color,
                "frame_width": req.frame_width,
                "subtitle_position": req.subtitle_position,
                "brand_size": 68, "brand_y": 90}
        hl = _valid_hex(req.highlight_color)
    if hl is None:
        hl = _valid_hex(get_setting(db, "subtitle_highlight_color", "#FADE4C"))
    look["highlight_color"] = hl or "#FADE4C"
    return look


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
        # Re-render de un clip existente: se sobrescribe SU mp4 (misma
        # URL de descarga) en vez de crear un archivo nuevo.
        update_clip = None
        if req.update_clip_id:
            db_u = SessionLocal()
            try:
                update_clip = db_u.query(Clip).filter(
                    Clip.id == req.update_clip_id).first()
            finally:
                db_u.close()
        out_name = (update_clip.file if update_clip and update_clip.file
                    else f"clip_{job_id}.mp4")
        out_path = OUTPUTS_DIR / out_name
        # Plantilla + color de palabra clave (2026-11-10): la plantilla
        # del request (o la guardada/default) manda sobre marco, color,
        # posición de subtítulos y marca; el color de palabra clave
        # sigue su propia cadena (editor → plantilla → plataforma).
        parent0 = jobs.get(job_id) or {}
        look = {"template": None, "frame_enabled": req.frame_enabled,
                "frame_color": req.frame_color, "frame_width": req.frame_width,
                "subtitle_position": req.subtitle_position,
                "highlight_color": req.highlight_color or "#FADE4C",
                "brand_size": 68, "brand_y": 90}
        try:
            db0 = SessionLocal()
            try:
                look = _resolve_render_look(db0, parent0.get("user_id"), req)
            finally:
                db0.close()
        except Exception:  # noqa: BLE001 — la resolución nunca falla el render
            logger.warning("template resolution failed, using request values",
                           exc_info=True)
        render_clip(
            source, rel_start, duration, out_path, words,
            frame_enabled=look["frame_enabled"],
            frame_color=look["frame_color"], frame_width=look["frame_width"],
            brand_text=req.brand_text, title_text=req.title_text,
            subtitle_style=req.subtitle_style, subtitle_size=req.subtitle_size,
            subtitle_position=look["subtitle_position"],
            show_subtitles=req.show_subtitles,
            highlight_color=look["highlight_color"],
            template=look["template"],
            brand_size=look["brand_size"], brand_y=look["brand_y"],
            quality=req.quality,
            camera_mode=req.camera_mode, camera_person=req.camera_person,
            facecam_corner=req.facecam_corner,
            # Portada / título de enganche: el título gancho se quema
            # grande sobre la portada al inicio del clip, sin marco ni
            # marca (2026-11-11). Estilo desde el modal Configurar portada.
            hook_title=req.clip_title or "",
            cover_at=req.cover_at,
            title_color=req.title_color,
            title_border_color=req.title_border_color,
            title_border_w=req.title_border_w,
            title_bg_color=req.title_bg_color,
            title_bg=req.title_bg,
            title_size=req.title_size,
            title_pos_x=req.title_pos_x,
            title_pos_y=req.title_pos_y,
        )
        info = probe(out_path)
        jobs.update(job_id, status="done", progress=100, message="Clip listo.",
                    result={"file": out_name, "download_url": f"/api/download/{out_name}",
                            "duration": duration, "output": info,
                            "frame_enabled": look["frame_enabled"],
                            "brand_text": req.brand_text,
                            "template": look["template"] or "Personalizado"})
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
                if req.update_clip_id:
                    # Re-render: actualiza el clip existente (mismo id y
                    # mismo archivo, ya sobrescrito) con el título y la
                    # portada nuevos, en vez de crear un clip duplicado.
                    existing = db.query(Clip).filter(
                        Clip.id == req.update_clip_id).first()
                    if existing is not None:
                        existing.title = title
                        if req.cover_at is not None:
                            dur = max(0.0, (existing.end or 0)
                                      - (existing.start or 0))
                            existing.cover_at = max(
                                0.0, min(float(req.cover_at), dur))
                        db.commit()
                else:
                    # job_id guardado = el trabajo de clips del que salió este
                    # clip (su transcripción y su video fuente viven ahí, y el
                    # botón Editar los necesita). Sin trabajo padre (URL o
                    # archivo directo), el propio trabajo de render.
                    create_clip(db, user_id=parent.get("user_id"),
                                video_url=video_url, title=title,
                                uploader=uploader, start=req.start, end=req.end,
                                job_id=(req.job_id or job_id), file=out_name,
                                score=req.clip_score,
                                section_start=section_start,
                                cover_at=req.cover_at,
                                live_session_id=req.live_session_id)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — panel record is best-effort
            pass
    except Exception as e:  # noqa: BLE001
        jobs.update(job_id, status="error", error=str(e), message="Falló el render.")


# ---------------------------------------------------------------- lives
# Lives monitor (2026-11-10, Juan): a background thread that, every
# 30 s, checks the active live sessions and processes one round for
# each session that is due (now - last_run_at >= interval). A round
# takes the most recent window (ending EDGE_SAFETY_SECONDS before the
# live edge), extracts clips with the same pipeline as /api/clips,
# renders them, and links them to the session. One round at a time,
# sequential — renders are CPU-heavy and the in-process job queue is
# max 2, so the monitor never runs two rounds in parallel. A failed
# round (bot check, live edge unknown) only records last_error and
# retries at the next interval — never a tight retry loop (bursts
# trigger YouTube's bot check).
_LIVES_MONITOR_STARTED = False


def _live_round(session_id: int) -> None:
    """Process one extraction round for a live session. Synchronous;
    runs in the monitor thread."""
    db = SessionLocal()
    try:
        session = db.get(LiveSession, session_id)
        if session is None or session.status != STATUS_ACTIVE:
            return
        st = live_status(session.url)
        if st is None:
            session.last_error = ("YouTube bloqueó la lectura del live, "
                                  "se reintenta en el próximo intervalo.")
            session.last_run_at = datetime.now(timezone.utc)
            db.commit()
            return
        if not st.get("is_live"):
            # The live ended: stop monitoring (clips stay). The user can
            # still clip the replay from Crear Clip.
            session.status = STATUS_ENDED
            session.last_error = ""
            session.last_run_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("live session %s ended (%s)", session.id, session.title)
            return
        edge = st.get("edge")
        if edge is None:
            session.last_error = ("No se pudo leer hasta dónde va el live, "
                                  "se reintenta en el próximo intervalo.")
            session.last_run_at = datetime.now(timezone.utc)
            db.commit()
            return
        interval_s = float(session.interval_minutes) * 60.0
        end = float(edge) - EDGE_SAFETY_SECONDS
        start = end - interval_s
        if session.last_end is not None:
            # Never re-process old content, and never a huge backlog
            # (e.g. after a pause): only the most recent interval.
            start = max(float(session.last_end), start)
        if end - start < 15:
            # Not enough new content aired yet; keep last_end so the
            # next round accumulates it.
            session.last_run_at = datetime.now(timezone.utc)
            db.commit()
            return
        # --- Same pipeline as /api/clips, but synchronous in this thread.
        clips_job = jobs.create("clips", {
            "url": session.url, "start": start, "end": end,
            "model": "base", "max_clips": session.clips_per_round,
            "min_len": session.min_len, "max_len": session.max_len})
        jobs.update(clips_job["id"], user_id=session.user_id)
        _run_clips_job(clips_job["id"], session.url, start, end, "base",
                       None, session.clips_per_round, session.min_len,
                       session.max_len)
        done = jobs.get(clips_job["id"]) or {}
        result = done.get("result") or {}
        candidates = result.get("candidates") or []
        # Si el usuario detuvo o pausó el live mientras se descargaba y
        # transcribía (la parte larga de la ronda), no renderizar nada:
        # un live detenido no vuelve a sacar clips. Se vuelve a leer de
        # la BD porque el objeto `session` es de antes de la ronda y no
        # ve el cambio que hizo el usuario desde el panel.
        fresh = db.get(LiveSession, session.id)
        if fresh is None or fresh.status != STATUS_ACTIVE:
            logger.info("live session %s no longer active, skipping renders",
                        session.id)
            session.last_run_at = datetime.now(timezone.utc)
            db.commit()
            return
        # The user's brand (for the frame), else the default.
        user = db.get(User, session.user_id) if session.user_id else None
        brand = (getattr(user, "brand_text", None) or DEFAULT_BRAND_TEXT)
        rendered = 0
        for c in candidates[:max(1, session.clips_per_round)]:
            # Re-check antes de cada render: si lo detuvo en medio de la
            # ronda, los clips que faltan no se sacan.
            fresh = db.get(LiveSession, session.id)
            if fresh is None or fresh.status != STATUS_ACTIVE:
                logger.info("live session %s stopped mid-round, "
                            "stopping renders", session.id)
                break
            try:
                rjob = jobs.create("render", {
                    "job_id": clips_job["id"],
                    "start": c["abs_start"], "end": c["abs_end"],
                    "clip_title": c.get("title") or "",
                    "clip_score": c.get("score"),
                    "video_title": st.get("title") or session.title,
                    "live_session_id": session.id})
                jobs.update(rjob["id"], user_id=session.user_id)
                req = RenderRequest(
                    job_id=clips_job["id"],
                    start=float(c["abs_start"]), end=float(c["abs_end"]),
                    clip_title=c.get("title") or "",
                    clip_score=c.get("score"),
                    video_title=st.get("title") or session.title,
                    brand_text=brand,
                    live_session_id=session.id)
                _run_render_job(rjob["id"], req)
                rdone = jobs.get(rjob["id"]) or {}
                if rdone.get("status") == "done":
                    rendered += 1
            except Exception:
                logger.warning("live render failed (session %s)",
                               session.id, exc_info=True)
                continue
        session.last_end = end
        session.last_run_at = datetime.now(timezone.utc)
        session.total_clips = int(session.total_clips or 0) + rendered
        session.last_error = ""
        db.commit()
        logger.info("live session %s round: %s-%s -> %d clips",
                    session.id, round(start, 1), round(end, 1), rendered)
    except Exception as e:
        logger.warning("live round failed (session %s): %s",
                       session_id, e, exc_info=True)
        try:
            db.rollback()
            session = db.get(LiveSession, session_id)
            if session is not None:
                session.last_error = str(e)[:400]
                session.last_run_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _lives_monitor_loop() -> None:
    """Daemon thread: every 30 s, process one round per due session."""
    import time as _time
    while True:
        try:
            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                sessions = (db.query(LiveSession)
                            .filter(LiveSession.status == STATUS_ACTIVE)
                            .order_by(LiveSession.id).all())
                due = []
                for s in sessions:
                    if s.last_run_at is None:
                        due.append(s.id)
                    else:
                        last = s.last_run_at
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        if (now - last).total_seconds() >= s.interval_minutes * 60:
                            due.append(s.id)
            finally:
                db.close()
            for sid in due:
                _live_round(sid)
        except Exception:
            logger.warning("lives monitor tick failed", exc_info=True)
        _time.sleep(30)


def _start_lives_monitor() -> None:
    global _LIVES_MONITOR_STARTED
    if _LIVES_MONITOR_STARTED:
        return
    _LIVES_MONITOR_STARTED = True
    import threading
    t = threading.Thread(target=_lives_monitor_loop, daemon=True,
                         name="lives-monitor")
    t.start()
    logger.info("lives monitor started")


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {"ok": True, "service": "ClipViral", "version": "1.5.0",
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
    out = auth.user_out(user)
    out["plan_expires"] = _plan_expires_for(db, user)
    return {"access_token": auth.create_access_token(user.id), "user": out}


@app.post("/api/auth/login", response_model=TokenResponse)
def api_login(req: LoginRequest, db: Session = Depends(get_db)):
    user = auth.authenticate_user(db, req.email, req.password)
    if user is None:
        raise HTTPException(401, "Correo o contraseña incorrectos")
    if getattr(user, "is_suspended", False) and not user.is_admin:
        raise HTTPException(403, "Tu cuenta está suspendida. "
                                 "Contacta al administrador.")
    out = auth.user_out(user)
    out["plan_expires"] = _plan_expires_for(db, user)
    return {"access_token": auth.create_access_token(user.id), "user": out}


@app.get("/api/auth/me", response_model=UserResponse)
def api_me(db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    out = auth.user_out(user)
    out["plan_expires"] = _plan_expires_for(db, user)
    return out


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
    # Videos generados: videos fuente distintos de los que salieron clips.
    videos_total = q.with_entities(Clip.video_url).distinct().count()
    # Minutos generados: duración total de los clips, en minutos.
    minutes_total = 0.0
    try:
        from sqlalchemy import func as _func
        minutes_total = float(
            db.query(_func.coalesce(_func.sum(Clip.end - Clip.start), 0))
            .filter(Clip.user_id == user.id).scalar() or 0) / 60.0
    except Exception:  # noqa: BLE001 — stats are best-effort
        minutes_total = 0.0
    # Lives activos (sección Lives).
    lives_active = (db.query(LiveSession)
                    .filter(LiveSession.user_id == user.id,
                            LiveSession.status == STATUS_ACTIVE).count())
    # Últimos 3 clips generados (para el Inicio).
    recent = (db.query(Clip).filter(Clip.user_id == user.id)
              .order_by(Clip.id.desc()).limit(3).all())
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
        "videos_total": videos_total,
        "minutes_total": round(minutes_total, 1),
        "lives_active": lives_active,
        "recent_clips": [clip_out(c) for c in recent],
    }


@app.get("/api/me/clips")
def api_my_clips(db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    clips = (db.query(Clip).filter(Clip.user_id == user.id)
             .order_by(Clip.created_at.desc()).all())
    return [clip_out(c) for c in clips]


class ClipPatchRequest(BaseModel):
    # Edit the hook title and/or the cover offset of an existing clip,
    # without re-rendering. cover_at is clamped to the clip's duration.
    title: str | None = None
    cover_at: float | None = None


@app.patch("/api/me/clips/{clip_id}")
def api_patch_my_clip(clip_id: int, req: ClipPatchRequest,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip or (clip.user_id != user.id and not user.is_admin):
        raise HTTPException(404, "Clip no encontrado")
    if req.title is not None:
        clip.title = req.title.strip()[:255]
    if req.cover_at is not None:
        dur = max(0.0, (clip.end or 0) - (clip.start or 0))
        clip.cover_at = max(0.0, min(float(req.cover_at), dur))
    db.commit()
    db.refresh(clip)
    return clip_out(clip)


@app.delete("/api/me/clips/{clip_id}")
def api_delete_my_clip(clip_id: int,
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Delete a generated clip: the DB row and its rendered mp4 in
    data/outputs/. Owner or admin only. The file removal is best
    effort — a missing file does not fail the delete."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip or (clip.user_id != user.id and not user.is_admin):
        raise HTTPException(404, "Clip no encontrado")
    if clip.file:
        try:
            (OUTPUTS_DIR / clip.file).unlink(missing_ok=True)
        except OSError:
            pass
    db.delete(clip)
    db.commit()
    return {"deleted": clip_id}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/change-password")
def api_change_password(req: ChangePasswordRequest,
                        db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Change your own password from Mi Cuenta. The current password
    must match; the new one needs at least 6 characters."""
    if not auth.verify_password(req.current_password, user.password_hash):
        raise HTTPException(400, "La contraseña actual no es correcta")
    if len(req.new_password) < 6:
        raise HTTPException(
            400, "La contraseña nueva debe tener al menos 6 caracteres")
    user.password_hash = auth.hash_password(req.new_password)
    db.commit()
    return {"changed": True}


# ---------------------------------------------------------------- panel admin
def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Solo el administrador puede ver esto")
    return user


@app.get("/api/admin/users")
def api_admin_users(db: Session = Depends(get_db),
                    _admin: User = Depends(require_admin)):
    users = db.query(User).order_by(User.created_at.asc()).all()
    out = []
    for u in users:
        row = auth.user_out(u)
        sub = get_active_subscription(db, u.id)
        row["plan_expires"] = sub.ends_at.isoformat() if sub else None
        row["plan_started"] = sub.starts_at.isoformat() if sub else None
        out.append(row)
    return out


class CreateUserRequest(BaseModel):
    name: str
    email: str
    password: str
    is_admin: bool = False


@app.post("/api/admin/users")
def api_admin_create_user(req: CreateUserRequest,
                          db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    """Admin creates a user directly (client or another admin) from
    the floating + button in Panel Admin > Usuarios. Uses the same
    validation as public register (name, valid email, password >= 6,
    email not already registered)."""
    try:
        user = auth.register_user(db, req.name, req.email, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if req.is_admin:
        user.is_admin = True
        db.commit()
        db.refresh(user)
    row = auth.user_out(user)
    row["plan_expires"] = None
    return row


class EditUserRequest(BaseModel):
    name: str | None = None
    email: str | None = None


@app.patch("/api/admin/users/{user_id}")
def api_admin_edit_user(user_id: int, req: EditUserRequest,
                        db: Session = Depends(get_db),
                        _admin: User = Depends(require_admin)):
    """Admin edits a user's name and/or email (Panel Admin > Usuarios
    > menú ⋮ > Editar). The email must stay unique and valid."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(404, "Usuario no encontrado")
    if req.name is not None:
        if not req.name.strip():
            raise HTTPException(400, "El nombre es obligatorio")
        user.name = req.name.strip()
    if req.email is not None:
        email = req.email.strip().lower()
        if "@" not in email or "." not in email.split("@")[-1]:
            raise HTTPException(400, "Correo no válido")
        clash = (db.query(User).filter(User.email == email,
                                       User.id != user_id).first())
        if clash:
            raise HTTPException(400, "Ese correo ya está registrado")
        user.email = email
    db.commit()
    db.refresh(user)
    row = auth.user_out(user)
    sub = get_active_subscription(db, user.id)
    row["plan_expires"] = sub.ends_at.isoformat() if sub else None
    row["plan_started"] = sub.starts_at.isoformat() if sub else None
    return row


@app.delete("/api/admin/users/{user_id}")
def api_admin_delete_user(user_id: int,
                          db: Session = Depends(get_db),
                          admin: User = Depends(require_admin)):
    """Admin deletes a user (menú ⋮ > Eliminar): removes their clips
    (+ mp4 files), live sessions, subscriptions and payment requests,
    then the user. Cannot delete yourself, and cannot delete the last
    remaining admin (to avoid locking the panel)."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(404, "Usuario no encontrado")
    if user.id == admin.id:
        raise HTTPException(400, "No puedes eliminar tu propia cuenta")
    if user.is_admin:
        others = (db.query(User).filter(User.is_admin == True,  # noqa: E712
                                        User.id != user.id).count())
        if others == 0:
            raise HTTPException(
                400, "No puedes eliminar al único administrador")
    # Clips (+ mp4 files).
    for clip in db.query(Clip).filter(Clip.user_id == user_id).all():
        if clip.file:
            try:
                (OUTPUTS_DIR / clip.file).unlink(missing_ok=True)
            except OSError:
                pass
        db.delete(clip)
    db.query(LiveSession).filter(LiveSession.user_id == user_id).delete()
    db.query(UserSubscription).filter(
        UserSubscription.user_id == user_id).delete()
    db.query(PaymentRequest).filter(
        PaymentRequest.user_id == user_id).delete()
    db.delete(user)
    db.commit()
    return {"deleted": user_id}


@app.post("/api/admin/users/{user_id}/suspend")
def api_admin_suspend_user(user_id: int,
                           db: Session = Depends(get_db),
                           admin: User = Depends(require_admin)):
    """Suspend a user (menú ⋮ > Suspender cuenta): they cannot log in
    or use the panel until reactivated. Cannot suspend yourself."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(404, "Usuario no encontrado")
    if user.id == admin.id:
        raise HTTPException(400, "No puedes suspender tu propia cuenta")
    user.is_suspended = True
    db.commit()
    db.refresh(user)
    return auth.user_out(user)


@app.post("/api/admin/users/{user_id}/unsuspend")
def api_admin_unsuspend_user(user_id: int,
                             db: Session = Depends(get_db),
                             _admin: User = Depends(require_admin)):
    """Reactivate a suspended user (menú ⋮ > Reactivar cuenta)."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(404, "Usuario no encontrado")
    user.is_suspended = False
    db.commit()
    db.refresh(user)
    return auth.user_out(user)


@app.get("/api/admin/earnings")
def api_admin_earnings(db: Session = Depends(get_db),
                       _admin: User = Depends(require_admin)):
    """Ganancias (Panel Admin > Ganancias): totals from APPROVED
    payment requests (a sale = an approved receipt, priced at the
    plan's RD$ price), upcoming renewals (active subscriptions ending
    within 7 days), active/suspended user counts, users by plan and
    recent sales."""
    from sqlalchemy import func as _func

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0,
                              microsecond=0)
    approved = (db.query(PaymentRequest)
                .filter(PaymentRequest.status == REQ_APPROVED))
    total_sales = 0
    total_count = 0
    month_sales = 0
    recent = []
    for req in approved.order_by(PaymentRequest.decided_at.desc()).all():
        plan = db.query(Plan).filter(Plan.id == req.plan_id).first()
        amount = int(plan.price_dop) if plan else 0
        total_sales += amount
        total_count += 1
        if req.decided_at and req.decided_at >= month_start:
            month_sales += amount
        if len(recent) < 5:
            u = db.query(User).filter(User.id == req.user_id).first()
            recent.append({
                "user_name": u.name if u else "",
                "user_email": u.email if u else "",
                "plan_name": plan.name if plan else "",
                "amount_dop": amount,
                "decided_at": (req.decided_at.isoformat()
                               if req.decided_at else None),
            })
    # Upcoming renewals: active subscriptions ending within 7 days.
    soon = now + timedelta(days=7)
    upcoming = []
    for sub in (db.query(UserSubscription)
                .filter(UserSubscription.status == "active",
                        UserSubscription.ends_at > now,
                        UserSubscription.ends_at <= soon)
                .order_by(UserSubscription.ends_at.asc()).all()):
        u = db.query(User).filter(User.id == sub.user_id).first()
        plan = db.query(Plan).filter(Plan.id == sub.plan_id).first()
        upcoming.append({
            "user_name": u.name if u else "",
            "user_email": u.email if u else "",
            "plan_name": plan.name if plan else "",
            "amount_dop": int(plan.price_dop) if plan else 0,
            "ends_at": sub.ends_at.isoformat(),
        })
    # Active users: non-admin, not suspended, with a live subscription.
    active_users = 0
    users_by_plan: dict[str, int] = {}
    for sub in (db.query(UserSubscription)
                .filter(UserSubscription.status == "active",
                        UserSubscription.ends_at > now).all()):
        u = db.query(User).filter(User.id == sub.user_id).first()
        if u is None or u.is_admin or getattr(u, "is_suspended", False):
            continue
        active_users += 1
        plan = db.query(Plan).filter(Plan.id == sub.plan_id).first()
        key = plan.name if plan else "Sin plan"
        users_by_plan[key] = users_by_plan.get(key, 0) + 1
    suspended = (db.query(User)
                 .filter(User.is_suspended == True)  # noqa: E712
                 .order_by(User.name.asc()).all())
    total_users = db.query(User).filter(User.is_admin == False).count()  # noqa: E712
    return {
        "total_sales_dop": total_sales,
        "total_sales_count": total_count,
        "month_sales_dop": month_sales,
        "upcoming_renewals": upcoming,
        "active_users": active_users,
        "suspended_count": len(suspended),
        "suspended_users": [{
            "id": u.id, "name": u.name, "email": u.email,
            "plan": u.plan} for u in suspended],
        "users_by_plan": users_by_plan,
        "total_users": total_users,
        "recent_sales": recent,
    }


class AssignPlanRequest(BaseModel):
    plan_id: int


@app.post("/api/admin/users/{user_id}/assign-plan")
def api_admin_assign_plan(user_id: int, req: AssignPlanRequest,
                          db: Session = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    """El admin asigna una membresía manualmente a un usuario, sin
    solicitud de pago ni comprobante (2026-11-10, pedido de Juan)."""
    try:
        sub = assign_plan(db, user_id, req.plan_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    plan = db.query(Plan).filter(Plan.id == sub.plan_id).first()
    return {"ok": True, "subscription": subscription_out(sub, plan=plan),
            "user": auth.user_out(
                db.query(User).filter(User.id == user_id).first())}


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


# DecideRequest: nota del admin al aprobar/rechazar solicitudes de pago.
# (Antes también servía a los retiros; los retiros se eliminaron el
# 2026-11-10 por decisión de Juan: el usuario no hará ningún retiro.)
class DecideRequest(BaseModel):
    admin_note: str = ""


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


# ---------------------------------------------------------------- lives API
class LiveCreateRequest(BaseModel):
    url: str
    interval_minutes: int = 10
    clips_per_round: int = 3
    min_len: float = 20.0
    max_len: float = 45.0


def _require_live_plan(db: Session, user: User | None) -> None:
    """Lives are a Full/Pro feature (Básico: 'Sin clips de Lives').
    Admin bypasses plan gating entirely."""
    if user is None:
        return
    if getattr(user, "is_admin", False):
        return
    _require_plan(db, user)
    sub = get_active_subscription(db, user.id)
    if sub is None:
        raise HTTPException(403, "Debes contratar un plan para usar el clipper")
    plan = db.get(Plan, sub.plan_id) if sub.plan_id else None
    name = (plan.name if plan else "") or ""
    # "Básico" (con o sin acento) no tiene clips de Lives.
    norm = (name.strip().lower().replace("á", "a").replace("é", "e")
            .replace("í", "i").replace("ó", "o").replace("ú", "u"))
    if norm == "basico":
        raise HTTPException(
            403, "Los clips de Lives están en los planes Pro y Full")


def _live_owner(db: Session, user: User | None, session_id: int) -> LiveSession:
    session = db.get(LiveSession, session_id)
    if session is None:
        raise HTTPException(404, "Live no encontrado")
    if user is not None and not getattr(user, "is_admin", False):
        if session.user_id != user.id:
            raise HTTPException(403, "No tienes acceso a este live")
    return session


@app.post("/api/lives")
def api_lives_create(req: LiveCreateRequest,
                     user: User | None = Depends(get_optional_user),
                     db: Session = Depends(get_db)):
    _require_live_plan(db, user)
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "Pega el link del live")
    interval = max(2, min(60, int(req.interval_minutes or 10)))
    per_round = max(1, min(5, int(req.clips_per_round or 3)))
    min_len = max(5.0, min(120.0, float(req.min_len or 20.0)))
    max_len = max(min_len, min(180.0, float(req.max_len or 45.0)))
    st = live_status(url)
    if st is None:
        raise HTTPException(
            502, "No se pudo leer el live, inténtalo de nuevo")
    if not st.get("is_live"):
        raise HTTPException(400, "Ese link no es un live en curso")
    session = LiveSession(
        user_id=(user.id if user is not None else None), url=url,
        video_id=st.get("video_id") or "", title=st.get("title") or "",
        uploader=st.get("uploader") or "",
        thumbnail=st.get("thumbnail") or "", interval_minutes=interval,
        clips_per_round=per_round, min_len=min_len, max_len=max_len,
        status=STATUS_ACTIVE)
    db.add(session)
    db.commit()
    db.refresh(session)
    return live_session_out(session, 0)


@app.get("/api/lives")
def api_lives_list(user: User | None = Depends(get_optional_user),
                   db: Session = Depends(get_db)):
    _require_plan(db, user)
    q = db.query(LiveSession)
    if user is not None and not getattr(user, "is_admin", False):
        q = q.filter(LiveSession.user_id == user.id)
    sessions = q.order_by(LiveSession.id.desc()).all()
    out = []
    for s in sessions:
        n = db.query(Clip).filter(Clip.live_session_id == s.id).count()
        out.append(live_session_out(s, n))
    return out


@app.get("/api/lives/{session_id}")
def api_lives_get(session_id: int,
                  user: User | None = Depends(get_optional_user),
                  db: Session = Depends(get_db)):
    _require_plan(db, user)
    session = _live_owner(db, user, session_id)
    clips = (db.query(Clip).filter(Clip.live_session_id == session.id)
             .order_by(Clip.id.desc()).all())
    d = live_session_out(session, len(clips))
    d["clips"] = [clip_out(c) for c in clips]
    return d


@app.get("/api/lives/{session_id}/clips")
def api_lives_clips(session_id: int,
                    user: User | None = Depends(get_optional_user),
                    db: Session = Depends(get_db)):
    _require_plan(db, user)
    session = _live_owner(db, user, session_id)
    clips = (db.query(Clip).filter(Clip.live_session_id == session.id)
             .order_by(Clip.id.desc()).all())
    return [clip_out(c) for c in clips]


def _live_set_status(session_id: int, status: str,
                     user: User | None, db: Session) -> dict:
    _require_plan(db, user)
    session = _live_owner(db, user, session_id)
    if status == STATUS_ACTIVE and session.status in (STATUS_PAUSED,
                                                      STATUS_STOPPED):
        # On resume, restart from the live edge (don't process the
        # backlog that aired while paused/stopped) and run the first
        # round right away: last_run_at=None makes the session due on
        # the next monitor tick instead of waiting for the interval.
        session.last_end = None
        session.last_run_at = None
    session.status = status
    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)
    n = db.query(Clip).filter(Clip.live_session_id == session.id).count()
    return live_session_out(session, n)


@app.post("/api/lives/{session_id}/pause")
def api_lives_pause(session_id: int,
                    user: User | None = Depends(get_optional_user),
                    db: Session = Depends(get_db)):
    return _live_set_status(session_id, STATUS_PAUSED, user, db)


@app.post("/api/lives/{session_id}/resume")
def api_lives_resume(session_id: int,
                     user: User | None = Depends(get_optional_user),
                     db: Session = Depends(get_db)):
    return _live_set_status(session_id, STATUS_ACTIVE, user, db)


@app.post("/api/lives/{session_id}/stop")
def api_lives_stop(session_id: int,
                   user: User | None = Depends(get_optional_user),
                   db: Session = Depends(get_db)):
    return _live_set_status(session_id, STATUS_STOPPED, user, db)


@app.delete("/api/lives/{session_id}")
def api_lives_delete(session_id: int,
                     user: User | None = Depends(get_optional_user),
                     db: Session = Depends(get_db)):
    _require_plan(db, user)
    session = _live_owner(db, user, session_id)
    db.delete(session)
    db.commit()
    return {"ok": True}


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


# ---------------------------------------------------------------- imágenes con IA (fal.ai)
class ImageGenerateRequest(BaseModel):
    prompt: str
    model: str = "fal-ai/flux/schnell"
    aspect: str = "9:16"  # 9:16 | 1:1 | 16:9
    style: str = ""  # Realista | Dibujo | 3D (se añade al prompt)


@app.get("/api/images/models")
def api_image_models():
    """Modelos de imagen curados con $/imagen. Se verifican en vivo
    contra fal.ai (caché 1 h); si fal.ai no responde, la lista
    estática se marca approximate=true."""
    return fetch_image_models()


@app.post("/api/images/generate")
def api_generate_image(req: ImageGenerateRequest,
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Genera una imagen con fal.ai (Full o admin). La clave se lee
    del entorno en el servidor; si no está configurada, error claro.
    La generación es asíncrona (cola de fal.ai): el frontend consulta
    GET /api/images/{id} hasta que esté lista."""
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Escribe qué imagen quieres crear")
    if len(prompt) > 1000:
        raise HTTPException(400, "El prompt es demasiado largo (máx. 1000 caracteres)")
    if not user_can_generate_images(db, user):
        raise HTTPException(
            403, "Las imágenes con IA están en el plan Full")
    style = (req.style or "").strip()
    if style:
        prompt = f"{prompt}, {style}"
    models = fetch_image_models()
    model = get_image_model(models, req.model)
    if model is None:
        raise HTTPException(400, "Modelo de imagen no disponible")
    aspect = req.aspect if req.aspect in ASPECTS else "9:16"
    try:
        sub = submit_image(model, prompt, aspect)
    except ValueError as e:
        # Sin FAL_API_KEY en el servidor, o error de fal.ai.
        raise HTTPException(500, str(e)) from e
    rec = create_image(db, user.id, prompt, req.model, aspect, sub,
                       model["price_per_image"])
    return image_out(rec)


@app.get("/api/images/{image_id}")
def api_get_image(image_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Estado de una imagen: consulta fal.ai y, si se completó,
    descarga la imagen al servidor. El frontend lo llama cada ~5 s."""
    img = db.query(GeneratedImage).filter(
        GeneratedImage.id == image_id).first()
    if img is None:
        raise HTTPException(404, "Imagen no encontrada")
    if img.user_id != user.id and not user.is_admin:
        raise HTTPException(403, "Esta imagen no es tuya")
    try:
        img = refresh_image(db, img)
    except ValueError as e:
        raise HTTPException(500, str(e)) from e
    return image_out(img)


@app.get("/api/images")
def api_my_images(db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Mis imágenes generadas, las nuevas primero."""
    imgs = (db.query(GeneratedImage).filter(GeneratedImage.user_id == user.id)
            .order_by(GeneratedImage.created_at.desc()).all())
    return [image_out(i) for i in imgs]


@app.get("/api/images/{image_id}/file")
def api_image_file(image_id: int):
    """Sirve el archivo de una imagen generada. Misma postura pública
    que /api/download en v1 (el id es la única llave)."""
    # La ruta real sale del registro en la DB, nunca del parámetro.
    db = SessionLocal()
    try:
        img = db.query(GeneratedImage).filter(
            GeneratedImage.id == image_id).first()
    finally:
        db.close()
    if img is None or not img.filename:
        raise HTTPException(404, "Imagen no encontrada")
    path = IMAGES_DIR / str(img.user_id) / Path(img.filename).name
    if not path.exists():
        raise HTTPException(404, "Archivo no encontrado")
    media = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(path, media_type=media, filename=path.name)


# ---------------------------------------------------------------- videos IA (fal.ai)
class VideoGenerateRequest(BaseModel):
    prompt: str
    model: str
    duration: int = 8
    aspect_ratio: str = "9:16"
    audio: bool = True


@app.get("/api/videos/models")
def api_video_models():
    """Modelos de video curados con $/s, duraciones, aspectos y audio.
    Se verifican en vivo contra fal.ai (caché 1 h); si fal.ai no
    responde, la lista estática se marca approximate=true."""
    return fetch_video_models()


@app.post("/api/videos/generate")
def api_generate_video(req: VideoGenerateRequest,
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Crea un video con fal.ai (Full/Pro o admin). Cada generación
    es UN clip corto (el más largo es Wan 3.0, ~30 s), no un video
    largo: un video de 1 minuto son varios clips cortos unidos."""
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Escribe qué video quieres crear")
    if len(prompt) > 1000:
        raise HTTPException(400, "El prompt es demasiado largo (máx. 1000 caracteres)")
    if not user_can_generate_videos(db, user):
        raise HTTPException(
            403, "Los videos con IA están en los planes Pro y Full")
    models = fetch_video_models()
    model = get_model(models, req.model)
    if model is None:
        raise HTTPException(400, "Modelo de video no disponible")
    if req.duration not in model["durations"]:
        raise HTTPException(
            400, f"Este modelo solo genera {model['durations']} segundos")
    if req.aspect_ratio not in model["aspect_ratios"]:
        raise HTTPException(
            400, f"Este modelo solo soporta {model['aspect_ratios']}")
    # El audio solo se apaga cuando el modelo tiene un parámetro para
    # ello (Veo Lite, Kling, Wan); H3 Max y Grok Lite siempre llevan
    # audio, Seedance nunca.
    audio = bool(req.audio) and bool(model["audio_param"])
    estimated = round(model["price_per_second"] * req.duration, 2)
    try:
        sub = submit_video(model, prompt, req.duration,
                           req.aspect_ratio, audio)
    except ValueError as e:
        # Sin FAL_API_KEY en el servidor, o error de fal.ai.
        raise HTTPException(500, str(e)) from e
    job = create_job(db, user.id, prompt, req.model, req.duration,
                     req.aspect_ratio, audio, sub, estimated)
    out = job_out(job)
    out["polling_status"] = "processing"
    return out


@app.get("/api/videos/{video_id}")
def api_get_video(video_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Estado de un video: consulta fal.ai y, si se completó,
    descarga el MP4 al servidor. El frontend lo llama cada ~30 s."""
    job = db.query(AIVideoJob).filter(AIVideoJob.id == video_id).first()
    if job is None:
        raise HTTPException(404, "Video no encontrado")
    if job.user_id != user.id and not user.is_admin:
        raise HTTPException(403, "Este video no es tuyo")
    try:
        job = refresh_job(db, job)
    except ValueError as e:
        raise HTTPException(500, str(e)) from e
    return job_out(job)


@app.get("/api/videos")
def api_my_videos(db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Mis videos generados, los nuevos primero."""
    jobs = (db.query(AIVideoJob).filter(AIVideoJob.user_id == user.id)
            .order_by(AIVideoJob.created_at.desc()).all())
    return [job_out(j) for j in jobs]


@app.get("/api/videos/{video_id}/file")
def api_video_file(video_id: int):
    """Sirve el MP4 de un video generado. Misma postura pública que
    /api/download en v1 (el id es la única llave)."""
    db = SessionLocal()
    try:
        job = db.query(AIVideoJob).filter(AIVideoJob.id == video_id).first()
    finally:
        db.close()
    if job is None or not job.file:
        raise HTTPException(404, "Video no encontrado")
    path = VIDEOS_DIR / str(job.user_id) / Path(job.file).name
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


@app.get("/api/jobs/{job_id}/clip-video")
def api_job_clip_video(job_id: str, start: float, end: float):
    """Video (1080p) de UN trozo [start, end] de un clips job, para la vista
    previa y las miniaturas. El análisis baja solo audio (rápido), así que
    el video del clip se baja aquí, bajo demanda, y se guarda en caché por
    job+rango: la segunda vez que se pide el mismo trozo se sirve el
    archivo sin volver a descargar. Same public-in-v1 posture as /source."""
    if end <= start or (end - start) > 180:
        raise HTTPException(400, "Rango de clip no válido (máx. 180 s)")
    safe_id = Path(job_id).name
    job = jobs.get(safe_id)
    res = (job or {}).get("result") or {}
    url = res.get("url")
    if not url:
        raise HTTPException(400, "Este trabajo no tiene URL de video")
    # Caché en disco: {job_id}_clip_{start}_{end}.mp4 (o .webm según formato).
    tag = f"{safe_id}_clip_{int(start * 10)}_{int(end * 10)}"
    for f in sorted(SOURCES_DIR.glob(f"{tag}.*")):
        return FileResponse(f, media_type="video/mp4", filename=f.name)
    # Descarga solo este trozo a 1080p (rápido: 30-60 s de video).
    tmp_job = f"{tag}"
    path = download_section(url, start, end, tmp_job)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/camera-track")
def api_job_camera_track(job_id: str, start: float, end: float,
                         mode: str = "follow_speaker",
                         person: str = "center"):
    """Recorrido de cámara (crop-x cada 2 s) para la vista previa.

    La vista previa del panel recorta el video a 9:16 con CSS
    (object-fit: cover, centrado) y NO refleja el modo de cámara: con
    "Seguir al que habla" la persona que habla está a un lado y la vista
    previa la muestra cortada, aunque el clip renderizado sí la encuadra
    bien (comprobado 2026-11-11 con Masha: el render con follow_speaker
    la centra completa). Este endpoint devuelve el recorrido que usa el
    render (app/camera.py analyze_camera) para que el frontend mueva el
    encuadre de la vista previa con object-position mientras suena el
    clip. El frontend convierte crop-x a % con x / (width - crop_w).
    Solo tiene sentido para follow_speaker / follow_person (los modos
    que desplazan el recorte en horizontal); para el resto devuelve el
    recorrido centrado (fallback).
    """
    if end <= start or (end - start) > 180:
        raise HTTPException(400, "Rango de clip no válido (máx. 180 s)")
    mode = (mode or "follow_speaker").strip().lower()
    person = (person or "center").strip().lower()
    safe_id = Path(job_id).name
    job = jobs.get(safe_id)
    res = (job or {}).get("result") or {}
    url = res.get("url")
    if not url:
        raise HTTPException(400, "Este trabajo no tiene URL de video")
    tag = f"{safe_id}_clip_{int(start * 10)}_{int(end * 10)}"
    # Caché del recorrido (por trabajo + rango + modo + persona).
    track_file = SOURCES_DIR / f"{tag}_track_{mode}_{person}.json"
    if track_file.exists():
        try:
            return json.loads(track_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Video del trozo (misma caché que /clip-video).
    video = None
    for f in sorted(SOURCES_DIR.glob(f"{tag}.*")):
        video = f
        break
    if video is None:
        video = download_section(url, start, end, tag)
    from .camera import analyze_camera, probe_size
    duration = end - start
    cam = analyze_camera(video, 0, duration, mode, person)
    width, height = probe_size(video)
    crop_w = height * 9 / 16
    out = {
        "path": cam["path"],
        "width": width,
        "crop_w": crop_w,
        "fallback": cam["fallback"],
        "duration": duration,
    }
    try:
        track_file.write_text(json.dumps(out), encoding="utf-8")
    except Exception:
        pass
    return out


# Minimal frontend (same origin, no extra server).
_frontend = BASE_DIR / "frontend"
# URLs limpias por sección del panel (2026-11-10, Juan): cada pestaña
# tiene su propia URL y el frontend usa history.pushState, así que una
# recarga o un enlace directo a /mis-clips debe servir el panel; sin
# estas rutas StaticFiles devolvería 404. Solo las secciones conocidas:
# cualquier otra ruta sigue cayendo al StaticFiles de abajo.
_PANEL_ROUTES = ("/inicio", "/mis-clips", "/crear-clip", "/lives",
                 "/imagenes-ia", "/videos-ia", "/planes", "/mi-cuenta",
                 "/admin")


def _panel_index():
    return FileResponse(_frontend / "index.html")


for _route in _PANEL_ROUTES:
    app.add_api_route(_route, _panel_index, methods=["GET"],
                      name=f"panel{_route.replace('/', '_')}")
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
