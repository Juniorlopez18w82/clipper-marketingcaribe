"""Local transcription with faster-whisper (no cloud, no API key),
or Groq Whisper (free tier) when GROQ_API_KEY is set.

Proven 2026-11-10 (clipper-prueba/transcribe2.py + transcribe_base.py):
- Video used: BULIN 47 / Alofoke, 54:13, NO YouTube captions available.
- faster-whisper 1.2.1 + av 19.x breaks decode_audio() (``metadata_errors``
  kwarg). Fix, proven in the test: load the WAV with the stdlib ``wave``
  module and pass a numpy float32 array to model.transcribe().
- Model "base", CPU, int8, language="es", word_timestamps=True.

Groq (added 2026-11-10, Juan's choice): local faster-whisper is too slow
on Render's free server (shared CPU, 512 MB), so when GROQ_API_KEY is
set, transcription goes to Groq's free tier instead — same Whisper,
model whisper-large-v3-turbo, ~160x realtime, no card. Free-tier limits:
25 MB max file per request, 2,000 requests/day, 8 h audio/day. Any Groq
failure falls back to local faster-whisper automatically — a Groq outage
must never fail a clips job.

Honest limitation (tell Juan, do not hide it): Whisper is imperfect
on fast Dominican slang + overlapping voices — some words come out
garbled. Subtitles are therefore EDITABLE before export (see render.py:
``edited_words``), and a larger model / cloud Whisper improves accuracy.
"""
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

from .config import TRANSCRIPTS_DIR, WHISPER_COMPUTE, WHISPER_DEVICE, WHISPER_MODEL

logger = logging.getLogger(__name__)

_model = None

# --- Groq Whisper (free tier) -------------------------------------------
GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
# Free tier: 25 MB max file per request. Larger files go local instead.
GROQ_MAX_FILE_BYTES = 25 * 1024 * 1024
GROQ_TIMEOUT = 120  # seconds per request


def transcription_engine() -> str:
    """Which engine transcribe_file() will try first: "groq" or "local"."""
    return "groq" if os.environ.get("GROQ_API_KEY") else "local"


def _groq_eligible(wav_path: Path) -> bool:
    """True when the file fits Groq's free-tier 25 MB per-request limit."""
    return wav_path.stat().st_size <= GROQ_MAX_FILE_BYTES


def _map_groq_response(payload: dict) -> list[dict]:
    """Map Groq verbose_json into the same segment shape local Whisper
    returns: {start, end, text, words: [{w, s, e}]}. Callers (clips.py,
    render.py) see no difference.

    Groq puts word timestamps in the TOP-LEVEL "words" array, NOT
    inside each segment (found 2026-11-10: every segment came back
    with no "words" key, so clip selection saw 0 words and returned
    0 clips even though the transcript had 18 segments). Words are
    assigned to the segment whose [start, end] contains the word's
    start; a word outside every segment goes to the nearest one.
    If a segment carries its own embedded words (other providers),
    those are used instead."""
    top_words = [
        {"w": w.get("word", ""),
         "s": round(float(w.get("start", 0) or 0), 2),
         "e": round(float(w.get("end", 0) or 0), 2)}
        for w in (payload.get("words") or [])
    ]
    out_segments = []
    for s in payload.get("segments", []) or []:
        start = round(float(s.get("start", 0) or 0), 2)
        end = round(float(s.get("end", 0) or 0), 2)
        embedded = [
            {"w": w.get("word", ""),
             "s": round(float(w.get("start", 0) or 0), 2),
             "e": round(float(w.get("end", 0) or 0), 2)}
            for w in (s.get("words") or [])
        ]
        seg_words = embedded or [w for w in top_words if start <= w["s"] < end]
        out_segments.append({
            "start": start,
            "end": end,
            "text": (s.get("text") or "").strip(),
            "words": seg_words,
        })
    # Words that fell outside every segment (rounding/gaps): attach
    # each to the nearest segment so no word is lost.
    if out_segments and top_words:
        covered = {id(w) for s in out_segments for w in s["words"]}
        for w in top_words:
            if id(w) in covered:
                continue
            nearest = min(out_segments,
                          key=lambda s: min(abs(w["s"] - s["start"]),
                                            abs(w["s"] - s["end"])))
            nearest["words"].append(w)
        for s in out_segments:
            s["words"].sort(key=lambda w: w["s"])
    return out_segments


def _transcribe_groq(wav_path: Path) -> list[dict] | None:
    """Try Groq Whisper. Returns segments on success, None when Groq
    was not attempted or failed — the caller then falls back to local
    faster-whisper. An EMPTY result is treated as a failure too: a
    real 60 s clip of speech must not transcribe to zero segments
    (seen 2026-11-10: Groq answered 200 with no segments for a WAV
    that curl transcribed into 18 segments), so returning None lets
    the local engine retry instead of the job ending with 0 clips.
    A genuinely silent video then transcribes empty locally as well,
    and the clips job fails with a clear message (see main.py)."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    if not _groq_eligible(wav_path):
        size_mb = wav_path.stat().st_size / (1024 * 1024)
        logger.warning(
            "Groq skipped: wav %.1f MB exceeds the 25 MB free-tier limit; "
            "using local faster-whisper", size_mb)
        return None

    boundary = uuid.uuid4().hex
    fields = [
        ("model", GROQ_MODEL),
        ("language", "es"),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("timestamp_granularities[]", "segment"),
    ]
    body = bytearray()
    for name, value in fields:
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode()
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="file"; '
             f'filename="{wav_path.name}"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode()
    body += wav_path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        GROQ_API_URL, data=bytes(body), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 # Groq (Cloudflare) 403s Python-urllib's default UA;
                 # a normal UA is required (verified 2026-11-10).
                 "User-Agent": "ClipViral/1.4.0"})
    try:
        with urllib.request.urlopen(req, timeout=GROQ_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning("Groq transcription failed (HTTP %s); "
                       "falling back to local faster-whisper", e.code)
        return None
    except Exception as e:  # noqa: BLE001 — timeout / DNS / network, any
        logger.warning("Groq transcription failed (%s); "
                       "falling back to local faster-whisper", e)
        return None
    try:
        segments = _map_groq_response(payload)
    except Exception as e:  # noqa: BLE001 — unexpected response shape
        logger.warning("Groq response could not be parsed (%s); "
                       "falling back to local faster-whisper", e)
        return None
    if not segments:
        logger.warning("Groq transcription returned no segments; "
                       "falling back to local faster-whisper")
        return None
    return segments



def _get_model(model_name: str | None = None):
    global _model
    name = model_name or WHISPER_MODEL
    # Cache per process; a different model name forces a reload.
    if _model is not None and getattr(_model, "_clipper_name", None) == name:
        return _model
    from faster_whisper import WhisperModel

    _model = WhisperModel(name, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    _model._clipper_name = name  # type: ignore[attr-defined]
    return _model


def extract_wav(source: Path, wav_path: Path) -> Path:
    """16 kHz mono WAV for Whisper (ffmpeg)."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source),
         "-vn", "-ac", "1", "-ar", "16000", str(wav_path)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg wav extract failed: {proc.stderr[-1000:]}")
    return wav_path


def load_wav_array(wav_path: Path) -> np.ndarray:
    """The av-bug bypass: stdlib wave -> numpy, never a file path to Whisper."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise ValueError("expected 16kHz mono WAV (use extract_wav first)")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_file(source: Path, job_id: str, model: str | None = None) -> dict:
    """Transcribe a downloaded section. Returns {segments, words, path}.

    Times are RELATIVE to the start of ``source`` (the downloaded section).
    Callers add the section's absolute start to get original-video times.

    Engine: Groq Whisper first when GROQ_API_KEY is set (fast, free
    tier); any Groq failure falls back to local faster-whisper, so the
    return shape is identical either way. ``model`` only applies to the
    local engine; Groq always uses whisper-large-v3-turbo.
    """
    wav_path = TRANSCRIPTS_DIR / f"{job_id}.wav"
    extract_wav(source, wav_path)
    groq_segments = _transcribe_groq(wav_path)
    if groq_segments is not None:
        out_segments = groq_segments
        used_model = GROQ_MODEL
    else:
        audio = load_wav_array(wav_path)
        whisper = _get_model(model)
        segments, _info = whisper.transcribe(audio, language="es", word_timestamps=True)
        out_segments = []
        for s in segments:
            out_segments.append({
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "text": s.text.strip(),
                "words": [
                    {"w": w.word, "s": round(w.start, 2), "e": round(w.end, 2)}
                    for w in (s.words or [])
                ],
            })
        used_model = model or WHISPER_MODEL
    result = {"job_id": job_id, "model": used_model,
              "language": "es", "segments": out_segments}
    out_path = TRANSCRIPTS_DIR / f"{job_id}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**result, "path": str(out_path)}
