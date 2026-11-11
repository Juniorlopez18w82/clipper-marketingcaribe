"""Render a 9:16 full-bleed clip with FFmpeg — the style Juan approved.

Approved 2026-11-10 (clip_bulin_9x16_full.mp4, 27 s, original 0:50-1:17):
- 1080x1920, center crop 405x720 -> 1080x1920 (source fills the screen,
  no black bars — general form: crop=ih*9/16:ih, centred, then scale)
- Yellow #FADE4C frame, 14 px, around the WHOLE frame
- @marketingcaribe on top, yellow, on a dark box
- Viral ASS subtitles: white bold, 3 words at a time, yellow karaoke
  highlight, over the video at the bottom

NEW REQUIREMENT (Juan 2026-11-10, Configuración):
- ``frame_enabled`` (default True) — Con marco / Sin marco toggle.
  True  -> draw the yellow frame AND the brand name on top.
  False -> draw NEITHER the frame NOR the name (clean video + subtitles).
- ``brand_text`` (default "@marketingcaribe") — editable field
  "Nombre que saldrá en el marco". Empty string = frame kept, name omitted.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

from .camera import analyze_camera, probe_size
from .config import DEFAULT_BRAND_TEXT, DEFAULT_FRAME_COLOR, DEFAULT_FRAME_WIDTH, FONT_BOLD

logger = logging.getLogger(__name__)


def hex_to_ffmpeg(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad hex color: {hex_color}")
    return f"0x{h.upper()}"


def hex_to_ass_bgr(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}&"


def _fmt(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(words: list[dict], size: int = 76, position: str = "bottom",
              style: str = "viral", highlight_color: str = "#FADE4C",
              width: int = 1080, height: int = 1920) -> str:
    """ASS subtitles from word timestamps (times relative to clip start).

    Groups of 3 words, split on pauses > 0.8 s (proven in make_clip.py).
    ``style``: viral = white text + yellow karaoke word;
               plain = white text, no karaoke; yellow = all-yellow text.
    ``position``: bottom (MarginV 320) / middle (MarginV 850) / top.
    """
    words = [w for w in words if str(w.get("w", "")).strip()]
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > 0.8:
            groups.append(cur)
            cur = []
        cur.append(w)
        if len(cur) >= 3:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    if style == "yellow":
        primary = hex_to_ass_bgr(highlight_color)
        secondary = primary
    else:
        primary = "&H00FFFFFF&"
        secondary = hex_to_ass_bgr(highlight_color)

    alignment = 8 if position == "top" else 2
    # Márgenes y tamaño de fuente escalan con la resolución de salida.
    scale = width / 1080.0
    margin_v = int(round(850 if position == "middle" else 320) * (height / 1920.0))
    size = max(20, int(round(size * scale)))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Viral,DejaVu Sans,{size},{primary},{secondary},&H00000000,"
        f"&H00000000,-1,0,0,0,100,100,0,0,1,4,0,{alignment},80,80,{margin_v},1",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i, g in enumerate(groups):
        start, end = g[0]["s"], g[-1]["e"] + 0.15
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0]["s"] - 0.05)
        if style == "viral":
            parts = []
            for w in g:
                dur = int(max(5, (w["e"] - w["s"]) * 100))
                parts.append(f"{{\\k{dur}}}{w['w'].strip().upper()}")
            text = " ".join(parts)
        else:
            text = " ".join(w["w"].strip().upper() for w in g)
        lines.append(f"Dialogue: 0,{_fmt(start)},{_fmt(end)},Viral,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def _dual_crop(w: int, h: int) -> tuple[int, int, int, int, int]:
    """Crop for one half of a two-person shot (dual camera).

    Each half (w/2 wide) must fill a 1080x960 panel (aspect 9/8) with no
    distortion and no black bars, so the half is cropped to the panel
    aspect and centered inside its half. Returns (cw, ch, y, x_left,
    x_right) in source pixels, all even (yuv420-safe).
    """
    half = w / 2
    panel_aspect = 9 / 8
    if half / h >= panel_aspect:
        cw, ch, y = h * panel_aspect, float(h), 0.0
    else:
        cw, ch, y = half, half / panel_aspect, (h - half / panel_aspect) / 2
    cw = min(int(round(cw / 2) * 2), int(half // 2 * 2))
    ch = int(round(ch / 2) * 2)
    y = int(round(y / 2) * 2)
    xl = int((half - cw) // 2 * 2)
    xr = int(half) + xl
    return cw, ch, y, xl, xr


def _path_x_at(path: list, t: float) -> float:
    """Crop-x of the follow path nearest to time t (path is smoothed)."""
    best = path[0][1]
    best_d = abs(path[0][0] - t)
    for pt, x in path:
        d = abs(pt - t)
        if d < best_d:
            best, best_d = x, d
    return best


def _overlay_chain(tmpdir: Path, *, out_w: int, out_h: int, scale: float,
                   frame_hex: str, frame_enabled: bool, frame_width: int,
                   brand_text: str, title_text: str, show_subtitles: bool,
                   words: list[dict] | None, subtitle_style: str,
                   subtitle_size: int, subtitle_position: str,
                   frame_color: str) -> str:
    """Filter chain from [vid] to [out]: frame + brand + title + ASS.

    Identical for every camera mode — the overlays always apply on top
    of the already-framed 9:16 video.
    """
    chain = "[vid]"
    # --- Frame + brand name: ONLY when frame_enabled is True. ---
    if frame_enabled:
        chain += (f"drawbox=x=0:y=0:w={out_w}:h={out_h}:color={frame_hex}:"
                  f"t={frame_width},")
        if brand_text and brand_text.strip():
            brand_file = tmpdir / "brand.txt"
            brand_file.write_text(brand_text.strip(), encoding="utf-8")
            chain += (
                "drawtext=fontfile=" + FONT_BOLD +
                f":textfile={brand_file}:fontsize={int(round(68 * scale))}:fontcolor={frame_hex}:"
                f"x=(w-text_w)/2:y={int(round(90 * scale))}:box=1:boxcolor=black@0.55:boxborderw=20,"
            )
        if title_text and title_text.strip():
            title_file = tmpdir / "title.txt"
            title_file.write_text(title_text.strip(), encoding="utf-8")
            chain += (
                "drawtext=fontfile=" + FONT_BOLD +
                f":textfile={title_file}:fontsize={int(round(30 * scale))}:fontcolor=white:"
                f"x=(w-text_w)/2:y={int(round(180 * scale))}:box=1:boxcolor=black@0.55:boxborderw=14,"
            )
    if show_subtitles and words:
        ass_file = tmpdir / "clip.ass"
        ass_file.write_text(
            build_ass(words, size=subtitle_size, position=subtitle_position,
                      style=subtitle_style, highlight_color=frame_color,
                      width=out_w, height=out_h),
            encoding="utf-8",
        )
        chain += f"ass={ass_file}[out]"
    else:
        chain = chain.rstrip(",") + "[out]"
    return chain


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg {what} failed: {proc.stderr[-2000:]}")


def _render_follow(source: Path, start_in_source: float, duration: float,
                   output: Path, cam_path: list, *, out_w: int, out_h: int,
                   tmpdir: Path, overlay_kwargs: dict) -> Path:
    """Follow modes: 2 s chunks, each cropped at the smoothed crop-x of
    its midpoint, then concatenated and overlaid in one final pass.

    Each chunk carries its own audio cut at the same timestamps, so
    audio stays in sync; the final pass re-encodes the concatenated
    timeline (no -c copy), which absorbs any timestamp gaps.
    """
    chunk_dur = 2.0
    chunks: list[Path] = []
    t = 0.0
    i = 0
    while t < duration:
        cd = min(chunk_dur, duration - t)
        x = int(round(_path_x_at(cam_path, t + cd / 2)))
        chunk = tmpdir / f"chunk_{i:03d}.mp4"
        _run([
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(start_in_source + t), "-i", str(source), "-t", str(cd),
            "-vf", f"crop=ih*9/16:ih:{x}:0,scale={out_w}:{out_h},setpts=PTS-STARTPTS",
            "-af", "asetpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "128k", str(chunk),
        ], f"follow chunk {i}")
        chunks.append(chunk)
        t += cd
        i += 1
    concat_file = tmpdir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{c}'\n" for c in chunks), encoding="utf-8")
    base = "[0:v]setpts=PTS-STARTPTS[vid]"
    chain = _overlay_chain(tmpdir, out_w=out_w, out_h=out_h, **overlay_kwargs)
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-filter_complex", base + ";" + chain,
        "-map", "[out]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(output),
    ], "follow final pass")
    return output


def render_clip(source: Path, start_in_source: float, duration: float,
                output: Path, words: list[dict] | None = None, *,
                frame_enabled: bool = True,
                frame_color: str = DEFAULT_FRAME_COLOR,
                frame_width: int = DEFAULT_FRAME_WIDTH,
                brand_text: str = DEFAULT_BRAND_TEXT,
                title_text: str = "",
                subtitle_style: str = "viral",
                subtitle_size: int = 76,
                subtitle_position: str = "bottom",
                show_subtitles: bool = True,
                quality: str = "1080p",
                camera_mode: str = "center",
                camera_person: str = "center") -> Path:
    """Render one clip. ``start_in_source`` is seconds into ``source`` file.

    ``quality``: "1080p" (1080x1920) or "720p" (720x1280) — marco, nombre
    y subtítulos escalan con la resolución.

    ``camera_mode``: "center" (default, the approved center crop),
    "dual" (left half on top, right half below, for two-person
    interviews), "follow_speaker" (9:16 crop follows whoever is
    speaking, face detection + mouth-movement score) or "follow_person"
    (follows the face in the requested zone: ``camera_person`` left /
    center / right). Follow modes fall back to the center crop when no
    face is found, so tracking can never fail a render.
    """
    if duration <= 0:
        raise ValueError("duration must be > 0")
    mode = (camera_mode or "center").strip().lower()
    if mode not in ("center", "dual", "follow_speaker", "follow_person"):
        mode = "center"
    person = (camera_person or "center").strip().lower()
    if person not in ("left", "center", "right"):
        person = "center"
    out_w, out_h = (720, 1280) if str(quality).strip() == "720p" else (1080, 1920)
    scale = out_w / 1080.0
    frame_hex = hex_to_ffmpeg(frame_color)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        overlay_kwargs = dict(
            scale=scale, frame_hex=frame_hex, frame_enabled=frame_enabled,
            frame_width=frame_width, brand_text=brand_text,
            title_text=title_text, show_subtitles=show_subtitles,
            words=words, subtitle_style=subtitle_style,
            subtitle_size=subtitle_size, subtitle_position=subtitle_position,
            frame_color=frame_color,
        )
        if mode in ("follow_speaker", "follow_person"):
            cam = analyze_camera(source, start_in_source, duration, mode, person)
            if not cam["fallback"]:
                logger.info("camera %s: %s/%s samples with faces, "
                            "crop-x %.0f-%.0f, %s switches",
                            mode, cam["stats"]["samples_with_faces"],
                            cam["stats"]["samples"],
                            cam["stats"]["x_min"], cam["stats"]["x_max"],
                            cam["stats"]["switches"])
                return _render_follow(source, start_in_source, duration,
                                      output, cam["path"],
                                      out_w=out_w, out_h=out_h,
                                      tmpdir=tmpdir,
                                      overlay_kwargs=overlay_kwargs)
            logger.warning("camera %s: no faces found, using center crop",
                           mode)
            mode = "center"
        if mode == "dual":
            vw, vh = probe_size(source)
            cw, ch, cy, xl, xr = _dual_crop(vw, vh)
            base = (
                f"[0:v]split=2[l][r];"
                f"[l]crop={cw}:{ch}:{xl}:{cy},scale={out_w}:{out_h // 2}[top];"
                f"[r]crop={cw}:{ch}:{xr}:{cy},scale={out_w}:{out_h // 2}[bot];"
                f"[top][bot]vstack=inputs=2,setpts=PTS-STARTPTS[vid]"
            )
        else:  # center — exactly the approved behavior
            base = (
                f"[0:v]crop=ih*9/16:ih:(iw-ow)/2:0,scale={out_w}:{out_h},"
                "setpts=PTS-STARTPTS[vid]"
            )
        chain = _overlay_chain(tmpdir, out_w=out_w, out_h=out_h,
                               **overlay_kwargs)
        _run([
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(start_in_source), "-i", str(source), "-t", str(duration),
            "-filter_complex", base + ";" + chain,
            "-map", "[out]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(output),
        ], "render")
    return output
