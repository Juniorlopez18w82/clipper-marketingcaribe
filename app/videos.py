"""Videos IA con fal.ai (el proveedor que eligió Juan, el más barato).

Docs leídas (2026-11-11, no adivinadas):
- Cola: https://fal.ai/docs/model-apis/model-endpoints/queue
  POST https://queue.fal.run/{endpoint} (Authorization: Key <FAL_API_KEY>)
  -> 202 {request_id, response_url, status_url, cancel_url}
  GET status_url -> {status: IN_QUEUE|IN_PROGRESS|COMPLETED}
  GET response_url -> {"video": {"url": "...mp4"}} (o error).
  Las URLs del submit se usan tal cual (reconstruirlas da 405).
- Wan 3.0: https://fal.ai/models/alibaba/wan-3.0/text-to-video/api
  input {prompt, resolution 480p/720p/1080p, aspect_ratio, duration int,
  audio bool}; precio $0.05/$0.10/$0.20 por segundo (480p/720p/1080p);
  2-30 s por clip, audio nativo.
- Veo 3.1 Lite: https://fal.ai/models/fal-ai/veo3.1/lite/api
  input {prompt, aspect_ratio 16:9/9:16, duration "4s"|"6s"|"8s",
  resolution 720p/1080p, generate_audio bool}; $0.05/s.
- Kling 3.0 Standard:
  https://fal.ai/models/fal-ai/kling-video/v3/standard/text-to-video/api
  input {prompt, duration "3".."15" (string), generate_audio bool,
  aspect_ratio 16:9/9:16/1:1}; $0.084/s.
- Seedance 1.0 Pro Fast:
  https://fal.ai/models/fal-ai/bytedance/seedance/v1/pro/fast/text-to-video/api
  input {prompt, aspect_ratio, resolution 480p/720p/1080p,
  duration "2".."12" (string)}; por tokens: 1080p 5 s ≈ $0.245
  (≈ $0.049/s); a 480p ≈ $0.024/s. SIN audio.
- Hailuo H3 Max: https://fal.ai/models/minimax/h3-max/text-to-video/api
  input {prompt, duration float, resolution 480P/768P/1080P,
  aspect_ratio, prompt_expansion_mode}; audio nativo (siempre).
  Precio: $0.05/s a 480P (estándar; con el descuento promocional
  vigente hasta el 2026-11-15 es menos). OJO: el H3 a secas (2K)
  cuesta $0.26/s en fal.ai — el de $0.05/s es el H3 Max a 480P,
  que es el incluido aquí.
- Grok Imagine 1.5 Lite:
  https://fal.ai/models/xai/grok-imagine-video/v1.5/lite/text-to-video/api
  input {prompt, duration int, resolution 480p/720p/1080p,
  aspect_ratio}; audio nativo (siempre). $0.02/s a 480p.

Cada generación es UN clip corto (el más largo es Wan 3.0, ~30 s);
ningún modelo genera 1 minuto de una vez: un video de 1 minuto son
varios clips cortos unidos.

Precios: $/s a la resolución que se envía (la más barata de cada
modelo). Se verifican en vivo contra la página llms.txt de cada
endpoint (caché 1 h); si fal.ai no responde, la lista estática se
devuelve marcada approximate=true ("precios aproximados").
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String

from . import fal
from .config import VIDEOS_DIR
from .db import Base

logger = logging.getLogger(__name__)

# Modelos curados para la UI. "id" es el endpoint de fal.ai.
# resolution = la resolución que se envía (la más barata del modelo).
# audio_param = campo del input que enciende/apaga el audio, o None
# cuando el modelo siempre genera con audio (o nunca, audio=False).
VIDEO_MODELS = [
    {"id": "alibaba/wan-3.0/text-to-video", "name": "Wan 3.0",
     "price_per_second": 0.10, "resolution": "720p",
     "durations": list(range(2, 31)),
     "aspect_ratios": ["16:9", "4:3", "1:1", "3:4", "9:16"],
     "audio": True, "audio_param": "audio"},
    {"id": "fal-ai/veo3.1/lite", "name": "Veo 3.1 Lite",
     "price_per_second": 0.05, "resolution": "720p",
     "durations": [4, 6, 8], "aspect_ratios": ["16:9", "9:16"],
     "audio": True, "audio_param": "generate_audio"},
    {"id": "fal-ai/kling-video/v3/standard/text-to-video",
     "name": "Kling 3.0 Standard",
     "price_per_second": 0.084, "resolution": None,
     "durations": list(range(3, 16)),
     "aspect_ratios": ["16:9", "9:16", "1:1"],
     "audio": True, "audio_param": "generate_audio"},
    {"id": "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video",
     "name": "Seedance 1.0 Pro Fast",
     "price_per_second": 0.024, "resolution": "480p",
     "durations": list(range(2, 13)),
     "aspect_ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
     "audio": False, "audio_param": None},
    {"id": "minimax/h3-max/text-to-video", "name": "Hailuo H3 Max",
     "price_per_second": 0.05, "resolution": "480P",
     "durations": list(range(5, 16)),
     "aspect_ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
     "audio": True, "audio_param": None},
    {"id": "xai/grok-imagine-video/v1.5/lite/text-to-video",
     "name": "Grok Imagine 1.5 Lite",
     "price_per_second": 0.02, "resolution": "480p",
     "durations": list(range(1, 16)),
     "aspect_ratios": ["16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16"],
     "audio": True, "audio_param": None},
]

_models_cache = {"at": 0.0, "data": None}
_MODELS_TTL = 3600  # 1 h


def fetch_video_models() -> list:
    """Lista curada de modelos de video. Verifica en vivo que cada
    endpoint exista en fal.ai (su llms.txt público); si fal.ai no
    responde, devuelve la lista estática con approximate=true."""
    now = time.time()
    if _models_cache["data"] is not None and now - _models_cache["at"] < _MODELS_TTL:
        return _models_cache["data"]
    out = []
    try:
        for m in VIDEO_MODELS:
            ok = fal.endpoint_exists(m["id"])
            out.append({**m, "approximate": not ok})
    except Exception as e:  # fal.ai no responde: lista estática
        logger.warning("fal.ai no responde (%s); lista estática", e)
        out = [{**m, "approximate": True} for m in VIDEO_MODELS]
    _models_cache.update({"at": now, "data": out})
    return out


def get_model(models: list, model_id: str) -> dict | None:
    for m in models:
        if m["id"] == model_id:
            return m
    return None


def build_fal_payload(model: dict, prompt: str, duration: int,
                      aspect_ratio: str, audio: bool) -> dict:
    """Input propio de cada endpoint de fal.ai (los campos y tipos
    difieren por modelo: Veo y Kling y Seedance quieren la duración
    como string; Veo con sufijo 's'; H3 Max con resolución en
    mayúsculas '480P')."""
    mid = model["id"]
    base = {"prompt": prompt, "aspect_ratio": aspect_ratio}
    if mid == "alibaba/wan-3.0/text-to-video":
        base.update({"resolution": model["resolution"],
                     "duration": int(duration)})
        if model["audio_param"]:
            base[model["audio_param"]] = bool(audio)
    elif mid == "fal-ai/veo3.1/lite":
        base.update({"duration": f"{duration}s",
                     "resolution": model["resolution"]})
        if model["audio_param"]:
            base[model["audio_param"]] = bool(audio)
    elif mid == "fal-ai/kling-video/v3/standard/text-to-video":
        base.update({"duration": str(duration)})
        if model["audio_param"]:
            base[model["audio_param"]] = bool(audio)
    elif mid == "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video":
        base.update({"resolution": model["resolution"],
                     "duration": str(duration)})
    elif mid == "minimax/h3-max/text-to-video":
        base.update({"duration": duration,
                     "resolution": model["resolution"],
                     "prompt_expansion_mode": "disabled"})
    elif mid == "xai/grok-imagine-video/v1.5/lite/text-to-video":
        base.update({"duration": int(duration),
                     "resolution": model["resolution"]})
    else:  # modelo nuevo sin rama propia: lo mínimo común
        base.update({"duration": int(duration)})
    return base


def submit_video(model: dict, prompt: str, duration: int,
                 aspect_ratio: str, audio: bool) -> dict:
    """Envía el trabajo a la cola de fal.ai. Devuelve request_id +
    status_url/response_url. Sin FAL_API_KEY: FalError con el mensaje
    claro ('Configura tu clave de fal.ai en el servidor')."""
    payload = build_fal_payload(model, prompt, duration, aspect_ratio, audio)
    return fal.submit(model["id"], payload)


class AIVideoJob(Base):
    __tablename__ = "ai_video_jobs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    prompt = Column(String(1000), nullable=False)
    model = Column(String(128), nullable=False)  # endpoint de fal.ai
    duration = Column(Integer, nullable=False)
    aspect_ratio = Column(String(16), nullable=False, default="9:16")
    audio = Column(Boolean, nullable=False, default=True)
    status = Column(String(32), nullable=False, default="processing")
    fal_request_id = Column(String(128), nullable=True)
    # Las URLs que devolvió el submit: se usan tal cual para el poll
    # (reconstruirlas con la ruta del endpoint devuelve 405).
    fal_status_url = Column(String(512), nullable=True)
    fal_response_url = Column(String(512), nullable=True)
    file = Column(String(255), nullable=True)  # mp4 en data/videos/{user_id}/
    estimated_cost_usd = Column(Float, nullable=True)
    error = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


def create_job(db, user_id: int, prompt: str, model_id: str, duration: int,
               aspect_ratio: str, audio: bool, sub: dict,
               estimated: float | None) -> AIVideoJob:
    job = AIVideoJob(user_id=user_id, prompt=prompt, model=model_id,
                     duration=duration, aspect_ratio=aspect_ratio,
                     audio=audio, status="processing",
                     fal_request_id=sub["request_id"],
                     fal_status_url=sub.get("status_url"),
                     fal_response_url=sub.get("response_url"),
                     estimated_cost_usd=estimated)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def job_out(job: AIVideoJob) -> dict:
    return {
        "id": job.id,
        "prompt": job.prompt,
        "model": job.model,
        "duration": job.duration,
        "aspect_ratio": job.aspect_ratio,
        "audio": job.audio,
        "status": job.status,
        "file_url": f"/api/videos/{job.id}/file" if job.file else None,
        "estimated_cost_usd": job.estimated_cost_usd,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def refresh_job(db, job: AIVideoJob) -> AIVideoJob:
    """Consulta fal.ai por el estado del trabajo; si se completó,
    descarga el MP4 al servidor y lo marca listo. Los estados
    terminales no se vuelven a consultar. En fallo de poll/descarga
    se conserva el último estado conocido para que el frontend
    reintente."""
    if job.status in ("completed", "failed", "cancelled", "expired"):
        return job
    if not job.fal_status_url:
        job.status = "failed"
        job.error = "Sin URL de seguimiento en fal.ai"
        db.commit()
        return job
    try:
        st = fal.get_status(job.fal_status_url)
    except fal.FalError as e:
        logger.warning("poll fal.ai video %s: %s", job.id, e)
        return job
    status = (st.get("status") or "").upper()
    if st.get("error"):
        job.status = "failed"
        job.error = str(st["error"])[:500]
        db.commit()
        return job
    if status in ("IN_QUEUE", "IN_PROGRESS"):
        job.status = "processing"
        db.commit()
        return job
    if status != "COMPLETED":
        return job  # estado desconocido: se reintenta en el próximo poll
    # COMPLETED: el resultado trae el video (o el error del trabajo).
    if not job.fal_response_url:
        job.status = "failed"
        job.error = "fal.ai no devolvió URL de resultado"
        db.commit()
        return job
    try:
        result = fal.get_result(job.fal_response_url)
    except fal.FalError as e:
        logger.warning("result fal.ai video %s: %s", job.id, e)
        return job
    if result.get("error"):
        job.status = "failed"
        job.error = str(result["error"])[:500]
        db.commit()
        return job
    video = result.get("video") or {}
    url = video.get("url")
    if not url:
        job.status = "failed"
        job.error = "fal.ai no devolvió la URL del video"
        db.commit()
        return job
    dest_dir = VIDEOS_DIR / str(job.user_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"video_{job.id}.mp4"
    try:
        fal.download_file(url, dest)
    except Exception as e:
        logger.warning("descarga video %s: %s", job.id, e)
        return job
    job.file = dest.name
    job.status = "completed"
    job.error = None
    db.commit()
    return job


def user_can_generate_videos(db, user) -> bool:
    """Videos IA: SOLO admin (2026-11-11, Juan: "Próximamente" para
    los usuarios; solo el admin entra directo). Antes eran Pro y Full."""
    return bool(user.is_admin)
