"""YouTube / live download via yt-dlp. No YouTube Data API, no API key.

Proven 2026-11-10 on https://www.youtube.com/watch?v=lvPIofmDIGk
(BULIN 47, Alofokeradioshow, 54:13): yt-dlp reads title/duration/uploader
straight from the pasted link, and --download-sections cuts a time range
without downloading the whole video. That video has NO automatic captions,
so transcription is always done locally (see transcribe.py).
"""
import json
import subprocess
from pathlib import Path

import yt_dlp

from .config import SOURCES_DIR


def video_info(url: str) -> dict:
    """Metadata only — no download."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),  # seconds, None for an unfinished live
        "is_live": bool(info.get("is_live")),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "description": (info.get("description") or "")[:500],
    }


def _fmt_time(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _stream_durations(path: Path) -> tuple[float, float]:
    """(video_duration, audio_duration) from stream tags; 0 if missing."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-show_entries", "stream_tags=DURATION", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return 0.0, 0.0
    v_dur = a_dur = 0.0
    for s in json.loads(proc.stdout).get("streams", []):
        dur = s.get("tags", {}).get("DURATION", "00:00:00")
        parts = [float(p) for p in dur.split(":")]
        secs = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else 0.0
        if s.get("codec_type") == "video":
            v_dur = secs
        elif s.get("codec_type") == "audio":
            a_dur = secs
    return v_dur, a_dur


def download_section(url: str, start: float, end: float, job_id: str) -> Path:
    """Download only [start, end] (absolute seconds in the original video).

    Returns the downloaded file. Format: best video <=720p + audio, merged
    by ffmpeg (yt-dlp does the merge; the section cut is keyframe-based,
    so the file can be a few seconds longer than requested — render.py
    trims exactly with -ss/-t).

    Robustness (found in testing 2026-11-10): yt-dlp --download-sections
    OCCASIONALLY truncates the audio stream (one run gave 13.5 s of audio
    in a 70 s file, the re-run gave the full 70 s). So we verify the audio
    duration after download and retry up to 3 times.
    """
    if end <= start:
        raise ValueError("end must be greater than start")
    if end - start > 900:
        raise ValueError("section too long (max 15 min per job in v1)")
    out_tmpl = str(SOURCES_DIR / f"{job_id}.%(ext)s")
    section = f"*{_fmt_time(start)}-{_fmt_time(end)}"
    last_err = ""
    for attempt in range(1, 4):
        for old in SOURCES_DIR.glob(f"{job_id}.*"):
            old.unlink()
        cmd = [
            "python3", "-m", "yt_dlp", "--no-warnings",
            "--download-sections", section,
            "-f", "bv[height<=720]+ba/b[height<=720]",
            "-o", out_tmpl,
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            last_err = proc.stderr[-2000:]
            continue
        files = sorted(SOURCES_DIR.glob(f"{job_id}.*"))
        if not files:
            last_err = "yt-dlp finished but produced no file"
            continue
        v_dur, a_dur = _stream_durations(files[0])
        # Audio must cover (nearly) the whole section; video is ~10 s long
        # because of keyframes, so compare audio against the requested length.
        if a_dur >= (end - start) * 0.8:
            return files[0]
        last_err = (f"attempt {attempt}: audio truncated "
                    f"({a_dur:.1f}s for a {end - start:.0f}s section), retrying")
    raise RuntimeError(f"yt-dlp failed after 3 attempts: {last_err}")


def probe(path: Path) -> dict:
    """ffprobe: width/height/duration/codec. Raises if the file is unreadable."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=width,height,codec_name,codec_type",
         "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[-1000:]}")
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
        "duration": float(data.get("format", {}).get("duration") or 0),
    }
