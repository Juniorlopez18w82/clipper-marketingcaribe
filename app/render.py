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
import subprocess
import tempfile
from pathlib import Path

from .config import DEFAULT_BRAND_TEXT, DEFAULT_FRAME_COLOR, DEFAULT_FRAME_WIDTH, FONT_BOLD


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
              style: str = "viral", highlight_color: str = "#FADE4C") -> str:
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
    margin_v = 850 if position == "middle" else 320
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
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
                show_subtitles: bool = True) -> Path:
    """Render one clip. ``start_in_source`` is seconds into ``source`` file."""
    if duration <= 0:
        raise ValueError("duration must be > 0")
    frame_hex = hex_to_ffmpeg(frame_color)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        filters = (
            "[0:v]crop=ih*9/16:ih:(iw-ow)/2:0,scale=1080:1920,"
            "setpts=PTS-STARTPTS[vid]"
        )
        chain = "[vid]"
        # --- Frame + brand name: ONLY when frame_enabled is True. ---
        if frame_enabled:
            chain += (f"drawbox=x=0:y=0:w=1080:h=1920:color={frame_hex}:"
                      f"t={frame_width},")
            if brand_text and brand_text.strip():
                brand_file = tmpdir / "brand.txt"
                brand_file.write_text(brand_text.strip(), encoding="utf-8")
                chain += (
                    "drawtext=fontfile=" + FONT_BOLD +
                    f":textfile={brand_file}:fontsize=68:fontcolor={frame_hex}:"
                    "x=(w-text_w)/2:y=90:box=1:boxcolor=black@0.55:boxborderw=20,"
                )
            if title_text and title_text.strip():
                title_file = tmpdir / "title.txt"
                title_file.write_text(title_text.strip(), encoding="utf-8")
                chain += (
                    "drawtext=fontfile=" + FONT_BOLD +
                    f":textfile={title_file}:fontsize=30:fontcolor=white:"
                    "x=(w-text_w)/2:y=180:box=1:boxcolor=black@0.55:boxborderw=14,"
                )
        if show_subtitles and words:
            ass_file = tmpdir / "clip.ass"
            ass_file.write_text(
                build_ass(words, size=subtitle_size, position=subtitle_position,
                          style=subtitle_style, highlight_color=frame_color),
                encoding="utf-8",
            )
            chain += f"ass={ass_file}[out]"
        else:
            chain = chain.rstrip(",") + "[out]"
        fc = filters + ";" + chain

        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(start_in_source), "-i", str(source), "-t", str(duration),
            "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(output),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg render failed: {proc.stderr[-2000:]}")
    return output
