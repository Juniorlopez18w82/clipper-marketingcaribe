"""YouTube / live download via yt-dlp. No YouTube Data API, no API key.

Proven 2026-11-10 on https://www.youtube.com/watch?v=lvPIofmDIGk
(BULIN 47, Alofokeradioshow, 54:13): yt-dlp reads title/duration/uploader
straight from the pasted link, and --download-sections cuts a time range
without downloading the whole video. That video has NO automatic captions,
so transcription is always done locally (see transcribe.py).

YouTube bot check (2026-11-10, Render): datacenter IPs get
"Sign in to confirm you're not a bot" on the default web player client.
Fix: try player clients in order — android, tv_embedded, web — because
YouTube checks each client differently; android/tv usually pass where web
is blocked. Verified locally 2026-11-10: default + android + tv_embedded
all read the Bulin video, ios was blocked. If ALL clients are blocked,
the only remaining fix is a YouTube cookies file (see _cookies_file):
put cookies.txt in data/ or set YTDLP_COOKIES_FILE, exported from a
browser logged into YouTube.

PROXY TYPE (root cause found 2026-11-10, job 3503f24a083a): yt-dlp cuts
the section with ffmpeg, and ffmpeg fetches the remote googlevideo URL
ITSELF. ffmpeg CANNOT use a SOCKS5 proxy — yt-dlp warns "does not support
SOCKS proxies" and hands ffmpeg HTTP_PROXY=socks5://..., which ffmpeg
ignores (proven locally: with http_proxy=socks5:// ffmpeg never even
connects to the proxy port; with http_proxy=http:// it sends HTTP CONNECT).
On Render that made every android section download fail with
"ffmpeg exited with code 8" while video_info (yt-dlp itself, which DOES
speak SOCKS5) worked fine. So YTDLP_PROXY must be an HTTP proxy:
entrypoint.sh adds wireproxy's [http] listener (127.0.0.1:1081) and sets
YTDLP_PROXY=http://127.0.0.1:1081. Never point it at socks5:// again.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yt_dlp

from .config import BASE_DIR, SOURCES_DIR

# Order matters: android first — it bypasses the web bot check most often.
PLAYER_CLIENTS = ["android", "tv_embedded", "web"]


def _cookies_file() -> str | None:
    """Optional YouTube cookies file, only if the user placed one there."""
    env = os.environ.get("YTDLP_COOKIES_FILE")
    if env and Path(env).exists():
        return env
    local = BASE_DIR / "data" / "cookies.txt"
    return str(local) if local.exists() else None


def _proxy() -> str | None:
    """Proxy for yt-dlp, set by entrypoint.sh when WARP is up.

    MUST be an HTTP proxy (http://...), never socks5:// — see the module
    docstring: ffmpeg, which yt-dlp uses to cut sections, cannot use SOCKS5.
    """
    return os.environ.get("YTDLP_PROXY") or None


def _ydl_opts(client: str) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": [client]}},
    }
    proxy = _proxy()
    if proxy:
        opts["proxy"] = proxy
    cookies = _cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _is_bot_block(err: str) -> bool:
    return "not a bot" in err or "Sign in to confirm" in err


def video_info(url: str) -> dict:
    """Metadata only — no download. Tries player clients in order."""
    last_err = ""
    for client in PLAYER_CLIENTS:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(client)) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:  # noqa: BLE001 — try the next player client
            last_err = str(e)
            continue
    else:
        raise RuntimeError(last_err or "YouTube bloqueó la lectura del video")
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


def _raw_info(url: str) -> dict | None:
    """Raw yt-dlp info dict (with formats), trying player clients in
    order. Returns None when every client is bot-blocked (the caller
    retries later — never hammer in a tight loop, bursts trigger the
    bot check)."""
    for client in PLAYER_CLIENTS:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(client)) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception:
            continue
    return None


def _fetch_text(url: str) -> str:
    """Fetch a URL as text through the HTTP proxy (for HLS manifests)."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    proxy = _proxy()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=30).read().decode("utf-8", "replace")
    return urllib.request.urlopen(req, timeout=30).read().decode(
        "utf-8", "replace")


def live_status(url: str) -> dict | None:
    """Live status + current live edge (seconds from stream start).

    For a live in progress returns {"is_live": True, "title": ...,
    "uploader": ..., "video_id": ..., "duration": None, "edge": <s>}.
    The edge comes from the HLS manifest: MEDIA-SEQUENCE (segment index
    from stream start) + the window's segment durations. YouTube's HLS
    DVR window only holds ~1 h, so the edge is (sequence + segments) *
    avg duration — validated 2026-11-10 on the Mansión VIP live (edge
    ≈ 358665 s ≈ 99.6 h, matching "DÍA 3"). Segments are ~5.0004 s, so
    the estimate is good to ~30 s; callers must apply a safety margin
    (never request content past the true edge).
    For a finished live / VOD returns {"is_live": False, "duration": ...,
    "edge": None}. Returns None when the info fetch is bot-blocked.
    """
    info = _raw_info(url)
    if info is None:
        return None
    base = {
        "is_live": bool(info.get("is_live")),
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or "",
        "video_id": info.get("id") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or "",
        "edge": None,
    }
    if not base["is_live"]:
        return base
    try:
        fmts = [f for f in (info.get("formats") or [])
                if "m3u8" in (f.get("url") or "")]
        if not fmts:
            return base
        fmts.sort(key=lambda f: f.get("tbr") or 9999)
        # Prefer 640x360 (decent DVR window); fall back to the cheapest.
        fmt = next((x for x in fmts
                    if x.get("resolution") == "640x360"), fmts[0])
        text = _fetch_text(fmt["url"])
        # Master playlist -> follow the cheapest media playlist.
        if "#EXT-X-STREAM-INF" in text:
            lines = text.splitlines()
            best = None
            for i, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF"):
                    m = re.search(r"BANDWIDTH=(\d+)", ln)
                    uri = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if uri and not uri.startswith("#"):
                        bw = int(m.group(1)) if m else 10 ** 9
                        if best is None or bw < best[0]:
                            best = (bw, uri)
            if not best:
                return base
            text = _fetch_text(best[1])
        seq_m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
        durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)]
        if not seq_m or not durs:
            return base
        avg = sum(durs) / len(durs)
        base["edge"] = round((int(seq_m.group(1)) + len(durs)) * avg, 1)
    except Exception:
        # Manifest fetch/parse failure: live, but edge unknown.
        base["edge"] = None
    return base


def _fmt_time(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _stream_durations(path: Path) -> tuple[float, float]:
    """(video_duration, audio_duration) in seconds; 0 if missing.

    BUG found 2026-11-10: the old code read only the stream DURATION tag,
    which webm/mkv files carry but MP4 files do NOT — the android client
    returns format 18 (MP4, 640x360), so every MP4 download was reported
    as 0 s of audio and retried forever. Now read the stream `duration`
    field first, fall back to the DURATION tag, then to format duration.
    """
    proc = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=codec_type,duration",
         "-show_entries", "stream_tags=DURATION",
         "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return 0.0, 0.0
    data = json.loads(proc.stdout)
    fmt_dur = float(data.get("format", {}).get("duration") or 0)
    v_dur = a_dur = 0.0
    for s in data.get("streams", []):
        secs = 0.0
        if s.get("duration"):
            try:
                secs = float(s["duration"])
            except (TypeError, ValueError):
                secs = 0.0
        if not secs:
            tag = (s.get("tags") or {}).get("DURATION", "")
            parts = [float(p) for p in tag.split(":")] if tag else []
            secs = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else 0.0
        if not secs:
            secs = fmt_dur
        if s.get("codec_type") == "video":
            v_dur = secs
        elif s.get("codec_type") == "audio":
            a_dur = secs
    return v_dur, a_dur


# Format selectors tried in order per client. The android client only
# offers the combined format 18 (640x360 MP4), so the bv+ba selector
# resolves to it via its /b fallback — but if a client ever reports
# "Requested format is not available", fall back to 18/best explicitly.
FORMAT_SELECTORS = [
    "bv[height<=720]+ba/b[height<=720]",
    "18/best[height<=720]/best",
]


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

    Bot check: tries each player client in order (android, tv_embedded,
    web), because YouTube blocks the web client on datacenter IPs.
    """
    if end <= start:
        raise ValueError("end must be greater than start")
    if end - start > 900:
        raise ValueError("section too long (max 15 min per job in v1)")
    out_tmpl = str(SOURCES_DIR / f"{job_id}.%(ext)s")
    section = f"*{_fmt_time(start)}-{_fmt_time(end)}"
    errors: list[str] = []
    attempt = 0
    for client in PLAYER_CLIENTS:
        for fmt in FORMAT_SELECTORS:
            attempt += 1
            for old in SOURCES_DIR.glob(f"{job_id}.*"):
                old.unlink()
            cmd = [
                sys.executable, "-m", "yt_dlp", "--no-warnings",
                "--extractor-args", f"youtube:player_client={client}",
                "--download-sections", section,
                "-f", fmt,
                "-o", out_tmpl,
                url,
            ]
            cookies = _cookies_file()
            if cookies:
                cmd[3:3] = ["--cookies", cookies]
            proxy = _proxy()
            if proxy:
                cmd[3:3] = ["--proxy", proxy]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                err = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "yt-dlp failed"
                errors.append(f"{client}/{fmt}: {err}")
                # Bot block: no point retrying this client, try the next one.
                if _is_bot_block(proc.stderr):
                    break
                continue
            files = sorted(SOURCES_DIR.glob(f"{job_id}.*"))
            if not files:
                errors.append(f"{client}/{fmt}: yt-dlp finished but produced no file")
                continue
            v_dur, a_dur = _stream_durations(files[0])
            # Audio must cover (nearly) the whole section; video is ~10 s long
            # because of keyframes, so compare audio against the requested length.
            if a_dur >= (end - start) * 0.8:
                return files[0]
            errors.append(f"{client}/{fmt}: audio {a_dur:.1f}s for a "
                          f"{end - start:.0f}s section (truncated)")
    if _proxy() and _proxy().startswith("socks"):
        errors.append("hint: YTDLP_PROXY is SOCKS5 — ffmpeg cannot use it; "
                      "use the wireproxy HTTP proxy instead (see entrypoint.sh)")
    raise RuntimeError(f"yt-dlp failed after {attempt} attempts: "
                       + " | ".join(errors))


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
