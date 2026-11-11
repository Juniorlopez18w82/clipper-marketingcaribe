"""Cliente mínimo para la cola de fal.ai, compartido por Videos IA e
Imágenes IA. Solo stdlib urllib, sin dependencias nuevas.

Flujo (docs: https://fal.ai/docs/model-apis/model-endpoints/queue y
cada página de modelo en https://fal.ai/models/<endpoint>/api):
  POST https://queue.fal.run/{endpoint}
    header Authorization: Key <FAL_API_KEY>
    body = el input propio del modelo (prompt, duration, ...)
    -> 202 {request_id, response_url, status_url, cancel_url,
            status: "IN_QUEUE"}
  GET status_url  -> {status: IN_QUEUE | IN_PROGRESS | COMPLETED, ...}
  GET response_url -> resultado del modelo, p. ej.
    {"video": {"url": "https://...mp4"}} o {"images": [{"url": "..."}]}

Las URLs que devuelve el submit se usan TAL CUAL: reconstruirlas con
la ruta completa del endpoint devuelve 405 (lo dice la propia doc).
La clave se lee SOLO del entorno (FAL_API_KEY); nunca se escribe en
archivos, logs ni respuestas.
"""

import json
import os
import urllib.error
import urllib.request

QUEUE_BASE = "https://queue.fal.run"
MODELS_BASE = "https://fal.ai/models"
FAL_KEY_ERROR = "Configura tu clave de fal.ai en el servidor"
USER_AGENT = "ClipViral/1.5.0"


class FalError(ValueError):
    """Error de fal.ai (o de su clave): el mensaje llega al usuario."""


def get_fal_key() -> str:
    key = os.environ.get("FAL_API_KEY")
    if not key:
        raise FalError(FAL_KEY_ERROR)
    return key


def _request_json(url: str, key: str, data=None, method: str = "GET",
                  timeout: int = 60) -> dict:
    headers = {"Authorization": f"Key {key}", "User-Agent": USER_AGENT}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
            msg = (payload.get("detail") or payload.get("error")
                   or payload.get("message") or str(e))
        except Exception:
            msg = str(e)
        raise FalError(str(msg)) from e


def submit(endpoint: str, payload: dict) -> dict:
    """Envía un trabajo a la cola. Devuelve request_id + las URLs de
    seguimiento tal cual las da fal.ai."""
    data = _request_json(f"{QUEUE_BASE}/{endpoint}", get_fal_key(),
                         data=payload, method="POST")
    rid = data.get("request_id")
    if not rid:
        raise FalError("fal.ai no devolvió request_id")
    return {"request_id": rid,
            "status_url": data.get("status_url"),
            "response_url": data.get("response_url")}


def get_status(status_url: str) -> dict:
    """Estado de un trabajo: IN_QUEUE | IN_PROGRESS | COMPLETED.
    Si el trabajo falló, fal devuelve un campo "error" aquí o en el
    resultado."""
    return _request_json(status_url, get_fal_key())


def get_result(response_url: str) -> dict:
    """Resultado de un trabajo completado (video o images con URL)."""
    return _request_json(response_url, get_fal_key())


def download_file(url: str, dest_path, timeout: int = 300) -> None:
    """Descarga un archivo de la CDN de fal.ai (URL pública, sin clave)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, \
            open(dest_path, "wb") as f:
        f.write(resp.read())


def endpoint_exists(endpoint: str, timeout: int = 10) -> bool:
    """¿Existe este endpoint en fal.ai? Comprueba su página llms.txt
    pública (sin clave). Sirve para marcar la lista curada como
    verificada en vivo o como aproximada si fal.ai no responde."""
    url = f"{MODELS_BASE}/{endpoint}/llms.txt"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False
