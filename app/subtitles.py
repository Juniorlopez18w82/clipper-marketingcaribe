"""Subtítulos de YouTube antes de Whisper (v205).

Problema: cada análisis transcribía el audio con Groq Whisper (o con
faster-whisper en local), aunque muchos videos de YouTube ya traen
subtítulos (manuales del creador o automáticos de YouTube). Bajar esos
subtítulos tarda segundos y evita minutos de transcripción.

Orden de preferencia:
  1. Subtítulos MANUALES (los sube el creador, más fiables).
  2. Subtítulos AUTOMÁTICOS (YouTube los genera, buenos en español).
  3. Si no hay ninguno utilizable, Whisper (Groq y luego local), como antes.

Formato de salida: el mismo que transcribe_file (segmentos con
start/end/text y palabras con {w, s, e}), para no romper el resto del
pipeline (selección de clips, subtítulos virales, render).
  - json3 (formato nativo de YouTube) trae tiempos POR PALABRA: se
    usan tal cual.
  - VTT solo trae tiempos por CUE (frase): las palabras se reparten
    uniformemente dentro de la frase. Es una aproximación, pero los
    subtítulos virales (3 palabras a la vez) quedan bien sincronizados
    a nivel de frase.
  - Las palabras se agrupan en segmentos de ~8-10 s como los de Whisper,
    para que la selección de clips vea la misma forma.

Los tiempos vienen en tiempo ABSOLUTO del video; se convierten a
relativos restando section_start_real (el inicio real del archivo de
audio, medido por _actual_section_start). Por eso el llamador debe
medir section_start_real ANTES de pedir los subtítulos.

No se usa en lives (is_live): los subtítulos en vivo no son fiables
para una ventana por timestamp.
"""
import json
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .video import _proxy, _raw_info

logger = logging.getLogger(__name__)

# Idiomas preferidos, en orden. Después de estos, cualquier otro.
PREFERRED_LANGS = ["es", "en"]


def _pick_subtitle(info: dict) -> tuple[str, bool] | None:
    """Elige el mejor subtítulo disponible: (idioma, es_automatico).
    Por idioma primero (es, luego en), y dentro de cada idioma manual
    antes que automático: para un video en español, un auto en español
    vale más que un manual en inglés (otro idioma). Después, cualquier
    manual y luego cualquier automático. None si no hay ninguno.
    v208: "live_chat" NO es un subtítulo (es el replay del chat del
    directo) — se excluye siempre, porque elegirlo gastaba ~15 s
    bajando un json de 1.5 MB que no tiene ningún segmento usable."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items()
              if k != "live_chat"}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items()
            if k != "live_chat"}
    for lang in PREFERRED_LANGS:
        if lang in manual:
            return lang, False
        if lang in auto:
            return lang, True
    for lang in sorted(manual):
        return lang, False
    for lang in sorted(auto):
        return lang, True
    return None


def _download_subtitle(url: str, lang: str, is_auto: bool,
                       fmt: str) -> Path | None:
    """Baja el subtítulo de un idioma en un formato (json3 o vtt).
    Devuelve la ruta del archivo o None si no se pudo bajar."""
    tmpdir = Path(tempfile.mkdtemp(prefix="clipzio_subs_"))
    out_tmpl = str(tmpdir / "sub")
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-warnings",
        "--skip-download",
        "--write-auto-subs" if is_auto else "--write-subs",
        "--sub-lang", lang,
        "--sub-format", fmt,
        "-o", out_tmpl,
        url,
    ]
    # Los subtítulos de YouTube no necesitan cliente de reproductor
    # (son metadatos), pero el proxy sí: sin él, el VPS no llega.
    proxy = _proxy()
    if proxy:
        cmd[3:3] = ["--proxy", proxy]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    files = sorted(tmpdir.glob("sub.*"))
    return files[0] if files else None


def _parse_json3(path: Path) -> list[dict]:
    """json3 de YouTube -> segmentos con palabras por palabra.
    Cada evento tiene tStartMs/dDurationMs y segs con tOffsetMs."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    segments = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        start = (ev.get("tStartMs") or 0) / 1000.0
        dur = (ev.get("dDurationMs") or 0) / 1000.0
        words = []
        text_parts = []
        for seg in ev.get("segs", []):
            w = (seg.get("utf8") or "").strip()
            if not w:
                continue
            # "\n" en json3 marca salto de línea, no es una palabra.
            if w == "\n":
                continue
            ws = start + (seg.get("tOffsetMs") or 0) / 1000.0
            words.append({"w": w, "s": round(ws, 2), "e": round(ws + 0.25, 2)})
            text_parts.append(w)
        if not words:
            continue
        text = " ".join(text_parts).strip()
        segments.append({
            "start": round(start, 2),
            "end": round(start + max(dur, 0.1), 2),
            "text": text,
            "words": words,
        })
    return segments


def _parse_vtt(path: Path) -> list[dict]:
    """VTT -> segmentos por cue, con palabras repartidas de forma
    uniforme dentro de la cue (aproximación: VTT no trae tiempos por
    palabra)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Quitar cabecera WEBVTT y etiquetas <c>, <b>, etc.
    text = re.sub(r"<[^>]+>", "", text)
    cue_re = re.compile(
        r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*"
        r"(\d+):(\d+):(\d+)[.,](\d+)\s*\n(.*?)(?=\n\n|\Z)",
        re.DOTALL)
    segments = []
    for m in cue_re.finditer(text):
        sh, sm, ss, sms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        eh, em, es, ems = int(m.group(5)), int(m.group(6)), int(m.group(7)), int(m.group(8))
        start = sh * 3600 + sm * 60 + ss + sms / 1000.0
        end = eh * 3600 + em * 60 + es + ems / 1000.0
        cue_text = " ".join(m.group(9).strip().split())
        if not cue_text or end <= start:
            continue
        words = cue_text.split()
        n = len(words)
        span = end - start
        w_list = []
        for i, w in enumerate(words):
            ws = start + span * i / n
            we = start + span * (i + 1) / n
            w_list.append({"w": w, "s": round(ws, 2), "e": round(we, 2)})
        segments.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "text": cue_text,
            "words": w_list,
        })
    return segments


def _group_segments(segments: list[dict], max_len: float = 10.0) -> list[dict]:
    """Agrupa segmentos cortos (cues de 1-2 s) en segmentos de hasta
    ~10 s, como los de Whisper, para que la selección de clips y el
    render vean la misma forma. No cambia los tiempos de las palabras."""
    out = []
    cur = None
    for s in segments:
        if cur is None:
            cur = {"start": s["start"], "end": s["end"],
                   "text": s["text"], "words": list(s["words"])}
            continue
        if s["start"] - cur["start"] <= max_len:
            cur["end"] = s["end"]
            cur["text"] = (cur["text"] + " " + s["text"]).strip()
            cur["words"].extend(s["words"])
        else:
            out.append(cur)
            cur = {"start": s["start"], "end": s["end"],
                   "text": s["text"], "words": list(s["words"])}
    if cur is not None:
        out.append(cur)
    return out


def fetch_subtitles(url: str, start: float, end: float,
                    section_start_real: float) -> dict | None:
    """Subtítulos de YouTube para [start, end], en el formato interno
    de transcripción (segmentos con palabras). Los tiempos salen
    RELATIVOS al archivo de audio (restando section_start_real), igual
    que los de Whisper. Devuelve None si no hay subtítulos utilizables
    (el llamador usa Whisper entonces)."""
    info = _raw_info(url)
    if info is None:
        return None
    picked = _pick_subtitle(info)
    if picked is None:
        return None
    lang, is_auto = picked
    segments: list[dict] = []
    # json3 primero (tiempos por palabra); VTT si no hay json3.
    for fmt in ("json3", "vtt"):
        path = _download_subtitle(url, lang, is_auto, fmt)
        if path is None:
            continue
        try:
            segments = (_parse_json3(path) if fmt == "json3"
                        else _parse_vtt(path))
        finally:
            try:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
            except OSError:
                pass
        if segments:
            break
    if not segments:
        return None
    # Filtrar al rango pedido y pasar a tiempos relativos al archivo.
    rel = []
    for s in segments:
        if s["end"] <= start or s["start"] >= end:
            continue
        words = [w for w in s["words"]
                 if start <= w["s"] < end]
        if not words:
            continue
        rel.append({
            "start": round(s["start"] - section_start_real, 2),
            "end": round(s["end"] - section_start_real, 2),
            "text": s["text"],
            "words": [{"w": w["w"],
                       "s": round(w["s"] - section_start_real, 2),
                       "e": round(w["e"] - section_start_real, 2)}
                      for w in words],
        })
    if not rel:
        return None
    # Calidad mínima: igual que el control de transcribe.py (al menos
    # 8 palabras y 1 palabra por cada 3 s). Un subtítulo vacío o con
    # solo "[Música]" no sirve.
    total_words = sum(len(s["words"]) for s in rel)
    span = max((float(s.get("end", 0) or 0) for s in rel), default=0.0)
    if total_words < max(8.0, span / 3.0):
        return None
    grouped = _group_segments(rel)
    logger.info("subtitles: %s (%s) %d palabras en %.0f s",
                lang, "auto" if is_auto else "manual",
                total_words, span)
    return {
        "job_id": "",
        "model": "youtube-auto" if is_auto else "youtube-manual",
        "language": lang,
        "segments": grouped,
    }
