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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import jobs
from .clips import build_candidates
from .config import (BASE_DIR, DEFAULT_BRAND_TEXT, DEFAULT_FRAME_COLOR,
                     DEFAULT_FRAME_WIDTH, MAX_WORKERS, OUTPUTS_DIR,
                     SOURCES_DIR, TRANSCRIPTS_DIR)
from .render import render_clip
from .transcribe import transcribe_file
from .video import download_section, probe, video_info

app = FastAPI(title="MarketingCaribe Clipper", version="1.0.0")
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
    except Exception as e:  # noqa: BLE001
        jobs.update(job_id, status="error", error=str(e), message="Falló el render.")


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {"ok": True, "service": "MarketingCaribe Clipper", "version": "1.0.0"}


@app.post("/api/video-info")
def api_video_info(req: VideoInfoRequest):
    try:
        return video_info(req.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"No se pudo leer el video: {e}") from e


@app.post("/api/clips")
def api_clips(req: ClipsRequest, background: BackgroundTasks):
    if req.end <= req.start:
        raise HTTPException(400, "end debe ser mayor que start")
    job = jobs.create("clips", req.model_dump())
    background.add_task(_run_clips_job, job["id"], req.url, req.start, req.end, req.model)
    return {"job_id": job["id"], "status": job["status"]}


@app.post("/api/render")
def api_render(req: RenderRequest, background: BackgroundTasks):
    if req.end <= req.start:
        raise HTTPException(400, "end debe ser mayor que start")
    job = jobs.create("render", req.model_dump())
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
