"""Imágenes IA con fal.ai (el proveedor que eligió Juan, también para
imágenes; Gemini queda fuera).

Docs leídas (2026-11-11, no adivinadas):
- Cola: https://fal.ai/docs/model-apis/model-endpoints/queue
  POST https://queue.fal.run/{endpoint} (Authorization: Key <FAL_API_KEY>)
  -> 202 {request_id, response_url, status_url, cancel_url}
  GET status_url -> {status: IN_QUEUE|IN_PROGRESS|COMPLETED}
  GET response_url -> {"images": [{"url": "..."}]} (o error).
  Las URLs del submit se usan tal cual (reconstruirlas da 405).
- FLUX.1 [schnell]: https://fal.ai/models/fal-ai/flux/schnell/api
  input {prompt, image_size: square_hd|square|portrait_4_3|
  portrait_16_9|landscape_4_3|landscape_16_9, num_images,
  output_format jpeg|png}; $0.003 por megapíxel (≈ $0.003/imagen
  a los tamaños que se envían). Rápido y el más barato.
- Seedream 4.0:
  https://fal.ai/models/fal-ai/bytedance/seedream/v4/text-to-image/api
  input {prompt, image_size (mismo enum + auto/auto_2K/auto_4K),
  num_images, enhance_prompt_mode}; $0.03 por imagen. Alta calidad.
- Nano Banana (Gemini 2.5 Flash Image):
  https://fal.ai/models/fal-ai/nano-banana/api
  input {prompt, num_images, aspect_ratio: 21:9|16:9|3:2|4:3|5:4|1:1|
  4:5|3:4|2:3|9:16, output_format jpeg|png|webp}; $0.0398 por
  imagen (≈ $0.04). Bueno para portadas con texto.

Aspectos de la UI (Vertical 9:16 / Cuadrado 1:1 / Horizontal 16:9)
se mapean al campo propio de cada modelo: image_size en FLUX y
Seedream (portrait_16_9 / square_hd / landscape_16_9) y
aspect_ratio en Nano Banana ("9:16" / "1:1" / "16:9").

Precios aproximados (verificados en las páginas llms.txt de cada
modelo el 2026-11-11); la lista se verifica en vivo contra fal.ai
(caché 1 h) y, si fal.ai no responde, se marca approximate=true.
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from . import fal
from .config import IMAGES_DIR
from .db import Base

logger = logging.getLogger(__name__)

ASPECTS = ("9:16", "1:1", "16:9")

# Modelos curados para la UI. "id" es el endpoint de fal.ai.
# aspect_field: cómo recibe el modelo el aspecto ("image_size" o
# "aspect_ratio"); aspect_map: aspecto de la UI -> valor del modelo.
IMAGE_MODELS = [
    {"id": "fal-ai/flux/schnell", "name": "FLUX Schnell",
     "price_per_image": 0.003, "aspect_field": "image_size",
     "aspect_map": {"9:16": "portrait_16_9", "1:1": "square_hd",
                    "16:9": "landscape_16_9"}},
    {"id": "fal-ai/bytedance/seedream/v4/text-to-image",
     "name": "Seedream 4.0",
     "price_per_image": 0.03, "aspect_field": "image_size",
     "aspect_map": {"9:16": "portrait_16_9", "1:1": "square_hd",
                    "16:9": "landscape_16_9"}},
    {"id": "fal-ai/nano-banana", "name": "Nano Banana",
     "price_per_image": 0.04, "aspect_field": "aspect_ratio",
     "aspect_map": {"9:16": "9:16", "1:1": "1:1", "16:9": "16:9"}},
]

_models_cache = {"at": 0.0, "data": None}
_MODELS_TTL = 3600  # 1 h


def fetch_image_models() -> list:
    """Lista curada de modelos de imagen. Verifica en vivo que cada
    endpoint exista en fal.ai (su llms.txt público); si fal.ai no
    responde, devuelve la lista estática con approximate=true."""
    now = time.time()
    if _models_cache["data"] is not None and now - _models_cache["at"] < _MODELS_TTL:
        return _models_cache["data"]
    out = []
    try:
        for m in IMAGE_MODELS:
            ok = fal.endpoint_exists(m["id"])
            out.append({**m, "approximate": not ok})
    except Exception as e:  # fal.ai no responde: lista estática
        logger.warning("fal.ai no responde (%s); lista estática", e)
        out = [{**m, "approximate": True} for m in IMAGE_MODELS]
    _models_cache.update({"at": now, "data": out})
    return out


def get_model(models: list, model_id: str) -> dict | None:
    for m in models:
        if m["id"] == model_id:
            return m
    return None


def build_fal_payload(model: dict, prompt: str, aspect: str) -> dict:
    """Input propio de cada endpoint: FLUX y Seedream reciben el
    aspecto como image_size; Nano Banana como aspect_ratio."""
    payload = {"prompt": prompt, "num_images": 1}
    value = model["aspect_map"].get(aspect, model["aspect_map"]["9:16"])
    payload[model["aspect_field"]] = value
    return payload


def submit_image(model: dict, prompt: str, aspect: str) -> dict:
    """Envía el trabajo a la cola de fal.ai. Devuelve request_id +
    status_url/response_url. Sin FAL_API_KEY: FalError con el mensaje
    claro ('Configura tu clave de fal.ai en el servidor')."""
    payload = build_fal_payload(model, prompt, aspect)
    return fal.submit(model["id"], payload)


class GeneratedImage(Base):
    __tablename__ = "generated_images"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    prompt = Column(String(1000), nullable=False)
    model = Column(String(128), nullable=False, default="fal-ai/flux/schnell")
    aspect = Column(String(16), nullable=False, default="9:16")
    status = Column(String(32), nullable=False, default="processing")
    fal_request_id = Column(String(128), nullable=True)
    # Las URLs que devolvió el submit: se usan tal cual para el poll
    # (reconstruirlas con la ruta del endpoint devuelve 405).
    fal_status_url = Column(String(512), nullable=True)
    fal_response_url = Column(String(512), nullable=True)
    filename = Column(String(255), nullable=True)  # en data/images/{user_id}/
    estimated_cost_usd = Column(Float, nullable=True)
    error = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


def create_image(db, user_id: int, prompt: str, model_id: str, aspect: str,
                 sub: dict, estimated: float | None) -> GeneratedImage:
    rec = GeneratedImage(user_id=user_id, prompt=prompt, model=model_id,
                         aspect=aspect, status="processing",
                         fal_request_id=sub["request_id"],
                         fal_status_url=sub.get("status_url"),
                         fal_response_url=sub.get("response_url"),
                         estimated_cost_usd=estimated)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def image_out(rec: GeneratedImage) -> dict:
    return {
        "id": rec.id,
        "prompt": rec.prompt,
        "model": rec.model,
        "aspect": rec.aspect,
        "status": rec.status,
        "file_url": f"/api/images/{rec.id}/file" if rec.filename else None,
        "estimated_cost_usd": rec.estimated_cost_usd,
        "error": rec.error,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


def refresh_image(db, rec: GeneratedImage) -> GeneratedImage:
    """Consulta fal.ai por el estado del trabajo; si se completó,
    descarga la imagen al servidor y la marca lista. Los estados
    terminales no se vuelven a consultar. En fallo de poll/descarga
    se conserva el último estado conocido para que el frontend
    reintente."""
    if rec.status in ("completed", "failed"):
        return rec
    if not rec.fal_status_url:
        rec.status = "failed"
        rec.error = "Sin URL de seguimiento en fal.ai"
        db.commit()
        return rec
    try:
        st = fal.get_status(rec.fal_status_url)
    except fal.FalError as e:
        logger.warning("poll fal.ai imagen %s: %s", rec.id, e)
        return rec
    status = (st.get("status") or "").upper()
    if st.get("error"):
        rec.status = "failed"
        rec.error = str(st["error"])[:500]
        db.commit()
        return rec
    if status in ("IN_QUEUE", "IN_PROGRESS"):
        rec.status = "processing"
        db.commit()
        return rec
    if status != "COMPLETED":
        return rec  # estado desconocido: se reintenta en el próximo poll
    # COMPLETED: el resultado trae la imagen (o el error del trabajo).
    if not rec.fal_response_url:
        rec.status = "failed"
        rec.error = "fal.ai no devolvió URL de resultado"
        db.commit()
        return rec
    try:
        result = fal.get_result(rec.fal_response_url)
    except fal.FalError as e:
        logger.warning("result fal.ai imagen %s: %s", rec.id, e)
        return rec
    if result.get("error"):
        rec.status = "failed"
        rec.error = str(result["error"])[:500]
        db.commit()
        return rec
    images = result.get("images") or []
    url = (images[0] or {}).get("url") if images else None
    if not url:
        rec.status = "failed"
        rec.error = "fal.ai no devolvió la URL de la imagen"
        db.commit()
        return rec
    dest_dir = IMAGES_DIR / str(rec.user_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Extensión según la URL (las CDN de fal.ai devuelven jpeg/png).
    ext = ".png"
    low = url.lower().split("?")[0]
    if low.endswith((".jpg", ".jpeg")):
        ext = ".jpg"
    dest = dest_dir / f"image_{rec.id}{ext}"
    try:
        fal.download_file(url, dest)
    except Exception as e:
        logger.warning("descarga imagen %s: %s", rec.id, e)
        return rec
    rec.filename = dest.name
    rec.status = "completed"
    rec.error = None
    db.commit()
    return rec


def user_can_generate_images(db, user) -> bool:
    """Imágenes IA: solo el plan Full (o admin). Pro y Básico dicen
    'Sin imágenes dibujadas por IA' en sus features, así que el
    chequeo es por el nombre exacto del feature."""
    if user.is_admin:
        return True
    from .billing import get_active_subscription
    sub = get_active_subscription(db, user.id)
    if not sub:
        return False
    plan = sub.plan
    return plan.name == "Full" or "Imágenes dibujadas por IA" in (plan.features or [])
