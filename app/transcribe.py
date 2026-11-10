"""Local transcription with faster-whisper (no cloud, no API key).

Proven 2026-11-10 (clipper-prueba/transcribe2.py + transcribe_base.py):
- Video used: BULIN 47 / Alofoke, 54:13, NO YouTube captions available.
- faster-whisper 1.2.1 + av 19.x breaks decode_audio() (``metadata_errors``
  kwarg). Fix, proven in the test: load the WAV with the stdlib ``wave``
  module and pass a numpy float32 array to model.transcribe().
- Model "base", CPU, int8, language="es", word_timestamps=True.

Honest limitation (tell Juan, do not hide it): local Whisper is imperfect
on fast Dominican slang + overlapping voices — some words come out
garbled. Subtitles are therefore EDITABLE before export (see render.py:
``edited_words``), and a larger model / cloud Whisper improves accuracy.
"""
import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from .config import TRANSCRIPTS_DIR, WHISPER_COMPUTE, WHISPER_DEVICE, WHISPER_MODEL

_model = None


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
    """
    wav_path = TRANSCRIPTS_DIR / f"{job_id}.wav"
    extract_wav(source, wav_path)
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
    result = {"job_id": job_id, "model": model or WHISPER_MODEL,
              "language": "es", "segments": out_segments}
    out_path = TRANSCRIPTS_DIR / f"{job_id}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**result, "path": str(out_path)}
