"""Caché inteligente de descargas y transcripciones (v205).

Problema: cada análisis volvía a descargar el mismo audio y a
transcribirlo con Whisper, aunque otro trabajo ya hubiera pedido el
mismo video y el mismo rango. En un video de 1 h eso son ~4.5 min de
descarga + ~1.5 min de transcripción, repetidos por cada trabajo.

Solución: guardar cada descarga y cada transcripción bajo una clave
(video_id de YouTube + rango pedido + tipo), y reutilizarlas cuando
otro trabajo pida el mismo contenido.

- La clave es (video_id, start, end, kind): kind = "audio" (solo
  audio para el análisis), "v360" (lowres), "v720" (render 720p,
  cliente android) o "v1080" (render/vista previa 1080p).
- Un archivo solo se reutiliza si está COMPLETO: existe, tiene
  tamaño > 0 y su duración de audio cubre >= 80% del rango pedido
  (el mismo umbral que download_section usa para aceptar una
  descarga). Un .part o un archivo truncado nunca se reutiliza.
- La transcripción solo se reutiliza cuando el audio TAMBIÉN se
  reutiliza (mismo archivo => mismo inicio real => los tiempos
  cuadran). Si el audio es nuevo, se transcribe de nuevo.
- Los lives (is_live) NUNCA se cachean: su contenido cambia con el
  tiempo y una sección por timestamp no es fiable.
- Limpieza automática: los archivos de caché con más de N días
  (ajuste cache_max_age_days, por defecto 30) se borran. La limpieza
  corre como mucho una vez cada 24 h (marcador en disco), para no
  hacerlo en cada trabajo.

No toca el diseño ni la interfaz, y no cambia el comportamiento de
los renders: download_section devuelve el mismo tipo de archivo,
solo que a veces viene de la caché en vez de bajarse de nuevo.
"""
import hashlib
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import CACHE_DIR, DATA_DIR, TRANSCRIPTS_DIR

logger = logging.getLogger(__name__)

# Duración mínima aceptable de un archivo cacheado, como fracción del
# rango pedido (igual que download_section: 0.8).
MIN_COVERAGE = 0.8
# La limpieza corre como mucho una vez cada 24 h.
CLEANUP_INTERVAL_SECONDS = 24 * 3600
_CLEANUP_MARKER = CACHE_DIR / ".last_cleanup"

for _d in (CACHE_DIR / "audio", CACHE_DIR / "video",
           CACHE_DIR / "transcripts"):
    _d.mkdir(parents=True, exist_ok=True)


def video_id_from_url(url: str | None) -> str | None:
    """ID del video de YouTube en la URL, o un hash estable de la URL
    cuando no es YouTube (Twitch, enlace directo). None si no hay URL."""
    if not url:
        return None
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)
    # No es YouTube (Twitch, etc.): hash estable de la URL.
    return "u" + hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def _tag(video_id: str, start: float, end: float, kind: str) -> str:
    """Nombre base del archivo de caché (precisión 0.1 s, como la
    vista previa: {padre}_clip_{start10}_{end10})."""
    return f"{video_id}_{int(start * 10)}_{int(end * 10)}_{kind}"


def cache_path(video_id: str, start: float, end: float, kind: str,
               ext: str = "") -> Path:
    """Ruta del archivo de caché para (video, rango, tipo)."""
    sub = "audio" if kind == "audio" else (
        "transcripts" if kind == "transcript" else "video")
    name = _tag(video_id, start, end, kind)
    return CACHE_DIR / sub / (name + ext if ext else name)


def _media_duration(path: Path) -> tuple[float, float]:
    """(duración video, duración audio) por ffprobe. (0, 0) si no se
    puede leer. Implementación local (no importa video.py) para no
    crear una importación circular: video.py importa este módulo.
    Igual que _stream_durations en video.py: lee la duración del
    stream, luego el tag DURATION, luego la duración del formato —
    los webm/mkv no siempre traen la duración en el stream (un .webm
    de audio válido daba 0.0 y la caché lo rechazaba)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,duration",
             "-show_entries", "stream_tags=DURATION",
             "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
        fmt_dur = float(data.get("format", {}).get("duration") or 0)
        v_dur = a_dur = 0.0
        for s in data.get("streams", []):
            d = float(s.get("duration") or 0)
            if not d:
                # Tag DURATION ("00:10:00.000000") o duración del formato.
                tag = (s.get("tags") or {}).get("DURATION") or ""
                if tag:
                    try:
                        h, m, sec = tag.split(":")
                        d = int(h) * 3600 + int(m) * 60 + float(sec)
                    except ValueError:
                        d = 0.0
                if not d:
                    d = fmt_dur
            if s.get("codec_type") == "video":
                v_dur = max(v_dur, d)
            elif s.get("codec_type") == "audio":
                a_dur = max(a_dur, d)
        return v_dur, a_dur
    except Exception:
        return 0.0, 0.0


def _is_complete_media(path: Path, start: float, end: float,
                       need_video: bool) -> bool:
    """True si el archivo existe, tiene tamaño y su audio cubre el
    rango pedido (y tiene video decodificable si se pide video)."""
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        if path.suffix == ".part":
            return False
        v_dur, a_dur = _media_duration(path)
        if a_dur < (end - start) * MIN_COVERAGE:
            return False
        if need_video and v_dur < (end - start) * MIN_COVERAGE:
            return False
        return True
    except OSError:
        return False


def _find_cached(video_id: str, start: float, end: float, kind: str,
                 need_video: bool) -> Path | None:
    """Busca un archivo cacheado válido para (video, rango, tipo).
    Devuelve la ruta o None. Ignora .part y archivos truncados."""
    sub = "audio" if kind == "audio" else "video"
    prefix = _tag(video_id, start, end, kind)
    for p in sorted((CACHE_DIR / sub).glob(prefix + ".*")):
        if p.suffix == ".part":
            continue
        if _is_complete_media(p, start, end, need_video):
            return p
        # Archivo truncado o ilegible: borrarlo para no volver a
        # encontrarlo en el siguiente trabajo.
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def cache_get(video_id: str, start: float, end: float, kind: str,
              need_video: bool = False) -> Path | None:
    """Archivo cacheado válido para (video, rango, tipo), o None."""
    if not video_id:
        return None
    return _find_cached(video_id, start, end, kind, need_video)


def cache_put(src: Path, video_id: str, start: float, end: float,
              kind: str) -> Path | None:
    """Guarda una copia de `src` en la caché. Devuelve la ruta de
    caché o None si no se pudo guardar. Nunca rompe el trabajo si
    falla (la caché es un extra, no un requisito)."""
    if not video_id or not src.is_file():
        return None
    try:
        dest = cache_path(video_id, start, end, kind, ext=src.suffix)
        # Borrar versiones viejas del mismo (video, rango, tipo) con
        # otra extensión, para no acumular .webm + .mp4 del mismo trozo.
        prefix = _tag(video_id, start, end, kind)
        sub = "audio" if kind == "audio" else "video"
        for old in (CACHE_DIR / sub).glob(prefix + ".*"):
            if old != dest:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass
        shutil.copyfile(src, dest)
        return dest
    except OSError as e:
        logger.warning("cache_put falló (%s)", e)
        return None


def transcript_cache_path(video_id: str, start: float, end: float) -> Path:
    return cache_path(video_id, start, end, "transcript", ext=".json")


def transcript_get(video_id: str, start: float, end: float) -> dict | None:
    """Transcripción cacheada válida para (video, rango), o None.
    Válida = JSON legible, con segmentos y suficientes palabras
    (el mismo control de calidad que transcribe.py aplica a Groq:
    al menos 8 palabras y al menos 1 palabra por cada 3 s)."""
    if not video_id:
        return None
    p = transcript_cache_path(video_id, start, end)
    try:
        if not p.is_file() or p.stat().st_size <= 0:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    segments = data.get("segments") or []
    if not segments:
        return None
    total_words = sum(len(s.get("words", [])) for s in segments)
    span = max((float(s.get("end", 0) or 0) for s in segments), default=0.0)
    if total_words < max(8.0, span / 3.0):
        return None
    return data


def transcript_put(transcript: dict, video_id: str, start: float,
                   end: float) -> Path | None:
    """Guarda una transcripción en la caché. Devuelve la ruta o None."""
    if not video_id or not (transcript.get("segments") or []):
        return None
    try:
        p = transcript_cache_path(video_id, start, end)
        p.write_text(json.dumps(transcript, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p
    except OSError as e:
        logger.warning("transcript_put falló (%s)", e)
        return None


def cleanup_cache(max_age_days: int = 30) -> dict:
    """Borra archivos de caché (audio, video, transcripciones) con más
    de max_age_days. Devuelve {borrados, bytes_liberados}. También
    limpia transcripciones viejas por trabajo en TRANSCRIPTS_DIR que
    ya no corresponden a ningún trabajo activo (archivos .json/.wav
    con más de max_age_days). Nunca borra nada de sources/ ni de
    outputs/ (esos son clips del usuario)."""
    now = time.time()
    cutoff = now - max_age_days * 86400
    deleted = 0
    freed = 0
    for base in (CACHE_DIR, TRANSCRIPTS_DIR):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.name == ".last_cleanup":
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    freed += p.stat().st_size
                    p.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                continue
    try:
        _CLEANUP_MARKER.write_text(str(int(now)), encoding="utf-8")
    except OSError:
        pass
    if deleted:
        logger.info("cleanup_cache: %d archivos borrados, %.1f MB liberados",
                    deleted, freed / 1048576)
    return {"deleted": deleted, "freed_bytes": freed}


def maybe_cleanup(max_age_days: int = 30) -> None:
    """Limpieza automática, como mucho una vez cada 24 h. Se llama en
    cada trabajo; si el marcador es reciente, no hace nada. Además de
    la caché, borra los clips con más de 7 días (v217, Juan): el
    sistema solo guarda los videos que el usuario realmente usará."""
    try:
        if _CLEANUP_MARKER.is_file():
            last = int(_CLEANUP_MARKER.read_text(encoding="utf-8").strip() or "0")
            if time.time() - last < CLEANUP_INTERVAL_SECONDS:
                return
    except (OSError, ValueError):
        pass
    try:
        cleanup_cache(max_age_days)
    except Exception as e:  # noqa: BLE001 — la limpieza nunca debe romper un trabajo
        logger.warning("maybe_cleanup falló (%s)", e)
    try:
        # Clips viejos (v217): importación perezosa para no crear un
        # ciclo entre módulos (clips_db importa auth).
        from .clips_db import CLIP_RETENTION_DAYS, cleanup_old_clips
        from .db import SessionLocal
        _db = SessionLocal()
        try:
            cleanup_old_clips(_db, CLIP_RETENTION_DAYS)
        finally:
            _db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup_old_clips falló (%s)", e)
