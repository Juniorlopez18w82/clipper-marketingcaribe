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
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from .camera import analyze_camera, probe_size
from .config import DEFAULT_BRAND_TEXT, DEFAULT_FRAME_COLOR, DEFAULT_FRAME_WIDTH, FONT_BOLD

logger = logging.getLogger(__name__)


# --- Plantillas (2026-11-10, Juan: elegir plantilla en Configuración) ---
# Una plantilla es un preset de look: marco (on/off + color + grosor),
# marca (tamaño y altura), posición de subtítulos y color de palabra
# clave. "Gancho grande" es el look aprobado desde el primer día.
TEMPLATES: dict[str, dict] = {
    "Gancho grande": {
        "frame_enabled": True, "frame_color": "#FADE4C", "frame_width": 14,
        "brand_size": 68, "brand_y": 90,
        "subtitle_position": "bottom", "highlight_color": "#FADE4C",
    },
    "Limpio": {
        "frame_enabled": False, "frame_color": "#FADE4C", "frame_width": 14,
        "brand_size": 68, "brand_y": 90,
        "subtitle_position": "bottom", "highlight_color": "#FADE4C",
    },
    "Impacto": {
        "frame_enabled": True, "frame_color": "#E10600", "frame_width": 18,
        "brand_size": 84, "brand_y": 80,
        "subtitle_position": "bottom", "highlight_color": "#E10600",
    },
}
DEFAULT_TEMPLATE = "Gancho grande"


def resolve_template(name: str | None) -> tuple[str, dict]:
    """Nombre canónico + preset de una plantilla. Un nombre desconocido
    (o vacío) cae a "Gancho grande" — nunca falla un render."""
    key = (name or "").strip()
    if key in TEMPLATES:
        return key, TEMPLATES[key]
    return DEFAULT_TEMPLATE, TEMPLATES[DEFAULT_TEMPLATE]


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


def _group_words(words: list[dict]) -> list[list[dict]]:
    """Agrupa palabras en grupos de 3, cortando en pausas > 0.8 s
    (probado en make_clip.py). Lo usan build_ass y la medición de
    cajas de palabra clave."""
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
    return groups


def _ass_header(size: int, position: str, highlight_color: str,
                width: int, height: int,
                extra_styles: list[str] | None = None) -> list[str]:
    """Cabecera ASS + estilo Viral (texto blanco en mayúsculas, borde
    negro, palabra que suena en el color de resaltado). ``extra_styles``
    añade líneas Style (p. ej. ViralBox para las cajas de palabra)."""
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
    ]
    lines.extend(extra_styles or [])
    return lines


def build_ass(words: list[dict], size: int = 76, position: str = "bottom",
              style: str = "viral", highlight_color: str = "#FADE4C",
              width: int = 1080, height: int = 1920,
              extra_styles: list[str] | None = None,
              extra_events: list[str] | None = None) -> str:
    """ASS subtitles from word timestamps (times relative to clip start).

    Groups of 3 words, split on pauses > 0.8 s (proven in make_clip.py).
    ``style``: viral = white text + highlight box on the sounding word;
               plain = white text, no karaoke; yellow = all-yellow text.
    ``position``: bottom (MarginV 320) / middle (MarginV 850) / top.
    Every word is uppercased, white, with a black border (Outline 4).
    The sounding word gets a coloured BACKGROUND block behind black text
    (see ``_keyword_box_events``); ``extra_events`` carries those box
    events and ``extra_styles`` their ViralBox style. The karaoke sweep
    (\\k) is ONLY a fallback when there are no box events: with both,
    the yellow karaoke and the black-on-colour box highlight different
    words at once (their timings drift when Whisper word times overlap)
    and the subtitles look like two fonts on top of each other
    (seen 2026-11-10).
    """
    groups = _group_words(words)
    header = _ass_header(size, position, highlight_color, width, height,
                         extra_styles=extra_styles)
    if style == "yellow":
        # Todo amarillo: el estilo Viral lleva primario = resaltado.
        hl = hex_to_ass_bgr(highlight_color)
        for i, ln in enumerate(header):
            if ln.startswith("Style: Viral,"):
                header[i] = ln.replace("&H00FFFFFF&", hl, 1)
                break
    lines = header + [
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    use_karaoke = (style == "viral" and not extra_events)
    for i, g in enumerate(groups):
        start, end = g[0]["s"], g[-1]["e"] + 0.15
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0]["s"] - 0.05)
        if use_karaoke:
            parts = []
            for w in g:
                dur = int(max(5, (w["e"] - w["s"]) * 100))
                parts.append(f"{{\\k{dur}}}{w['w'].strip().upper()}")
            text = " ".join(parts)
        else:
            text = " ".join(w["w"].strip().upper() for w in g)
        lines.append(f"Dialogue: 0,{_fmt(start)},{_fmt(end)},Viral,,0,0,0,,{text}")
    lines.extend(extra_events or [])
    return "\n".join(lines) + "\n"


def _ass_escape(text: str) -> str:
    """Escape ASS special chars in a word (braces/backslash)."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _keyword_box_events(words: list[dict], *, size: int, position: str,
                        highlight_color: str, width: int, height: int,
                        tmpdir: Path) -> tuple[list[str], list[str]]:
    """Per-word highlight BACKGROUND behind the sounding word.

    Returns (style_lines, event_lines) to append to the ASS file.
    Method: render every 3-word group once (plain text, same style) to
    raw frames with ffmpeg, measure each word's ink box with numpy,
    then emit one Layer-1 Dialogue per word — black text with a thick
    outline in the highlight colour, positioned with \\pos at the
    measured box — visible only while that word sounds. The outline
    merges into a solid block, so it reads as a coloured background
    behind black text (not just coloured text).

    Vertical placement is exact, no font metrics needed: the main
    event's bounding-box edge is known from MarginV (bottom/middle:
    box bottom = height - MarginV; top: box top = MarginV), so the box
    event uses \\an2/\\an8 + \\pos at the word's measured horizontal
    centre. Word splitting uses the N-1 widest ink gaps in the line
    (a space is ~30 px at size 76; inter-letter gaps are ~13 px).

    Best-effort by design: on ANY failure (no ffmpeg, no numpy, no
    ink found) it returns ([], []) and the render keeps the plain
    karaoke colours — the highlight box must never fail a render.
    """
    groups = _group_words(words)
    if not groups:
        return [], []
    try:
        import numpy as np
    except Exception:
        return [], []
    try:
        scale = width / 1080.0
        eff_size = max(20, int(round(size * scale)))
        margin_v = int(round((850 if position == "middle" else 320)
                             * (height / 1920.0)))
        # Probe ASS: one plain event per group, 1 s each, same style.
        probe_lines = _ass_header(size, position, highlight_color,
                                  width, height) + [
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for i, g in enumerate(groups):
            text = " ".join(w["w"].strip().upper() for w in g)
            probe_lines.append(
                f"Dialogue: 0,{_fmt(i)},{_fmt(i + 1)},Viral,,0,0,0,,{text}")
        probe_file = tmpdir / "probe.ass"
        probe_file.write_text("\n".join(probe_lines) + "\n", encoding="utf-8")
        n = len(groups)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=1:d={n}",
             "-vf", f"ass={probe_file}", "-frames:v", str(n),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=180)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError("probe render failed")
        frames = np.frombuffer(proc.stdout, dtype=np.uint8)
        frame_bytes = width * height * 3
        if frames.size < n * frame_bytes:
            raise RuntimeError("probe render short")
        frames = frames[:n * frame_bytes].reshape(n, height, width, 3)
        hl_bgr = hex_to_ass_bgr(highlight_color)
        style_line = (
            f"Style: ViralBox,DejaVu Sans,{eff_size},&H00000000&,&H00000000&,"
            f"{hl_bgr},&H00000000&,-1,0,0,0,100,100,0,0,1,12,0,2,0,0,0,1")
        min_gap = max(6, int(round(8 * scale)))
        if position == "top":
            an, y_pos = 8, margin_v
        else:  # bottom / middle: box bottom = height - MarginV
            an, y_pos = 2, height - margin_v
        events: list[str] = []
        # Índice por identidad para el siguiente de cada palabra (hay
        # palabras repetidas; words.index() devolvería la primera).
        _next_start = {}
        for _j, _w in enumerate(words):
            _next_start[id(_w)] = (words[_j + 1]["s"]
                                   if _j + 1 < len(words) else None)
        for i, g in enumerate(groups):
            gray = frames[i].max(axis=2)
            ink_cols = np.where(gray.max(axis=0) > 80)[0]
            if ink_cols.size == 0:
                continue
            # Si el grupo ocupa DOS líneas (palabras largas), las cajas
            # medidas por columnas mezclan ambas líneas y salen
            # descolocadas (una encima de otra, cortadas por el borde).
            # Mejor sin caja en ese grupo: el texto blanco sigue limpio.
            ink_rows = np.where(gray.max(axis=1) > 80)[0]
            bands = 1
            for _r in range(1, len(ink_rows)):
                if ink_rows[_r] - ink_rows[_r - 1] > 15:
                    bands += 1
            if bands > 1:
                continue
            # Ink runs (consecutive columns with ink).
            col_list = ink_cols.tolist()
            runs: list[tuple[int, int]] = []
            start_c = prev_c = col_list[0]
            for c in col_list[1:]:
                if c - prev_c > 2:
                    runs.append((start_c, prev_c))
                    start_c = c
                prev_c = c
            runs.append((start_c, prev_c))
            if len(runs) < len(g):
                continue
            if len(g) == 1:
                word_boxes = runs
            else:
                # The N-1 widest gaps between runs are the word spaces.
                gaps = [(runs[j + 1][0] - runs[j][1], j)
                        for j in range(len(runs) - 1)]
                gaps.sort(reverse=True)
                splits = sorted(j for _gap, j in gaps[:len(g) - 1])
                if any(gap < min_gap for gap, _j in gaps[:len(g) - 1]):
                    continue
                word_boxes = []
                prev = 0
                for j in splits:
                    word_boxes.append((runs[prev][0], runs[j][1]))
                    prev = j + 1
                word_boxes.append((runs[prev][0], runs[-1][1]))
            for w, (x0, x1) in zip(g, word_boxes):
                cx = (x0 + x1) // 2
                text = _ass_escape(w["w"].strip().upper())
                # Una sola caja a la vez: si la palabra siguiente empieza
                # antes de que termine esta (tiempos de Whisper solapados),
                # cortar ahí para que no se vean dos cajas a la vez.
                box_end = w["e"] + 0.05
                nxt = _next_start.get(id(w))
                if nxt is not None and nxt < box_end:
                    box_end = nxt
                events.append(
                    f"Dialogue: 1,{_fmt(w['s'])},{_fmt(box_end)},"
                    f"ViralBox,,0,0,0,,{{\\an{an}\\pos({cx},{y_pos})}}"
                    f"{text}")
        if not events:
            return [], []
        return [style_line], events
    except Exception as e:  # noqa: BLE001 — highlight box is best-effort
        logger.warning("keyword highlight boxes skipped (%s)", e)
        return [], []


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


def _cover_in_zone(zone_x: float, zone_y: float, zone_w: float,
                   zone_h: float, aspect: float) -> tuple[int, int, int, int]:
    """Crop de 'cover' dentro de una zona: el rectángulo más grande con
    el aspecto dado que cabe en la zona, centrado en ella. Devuelve
    (w, h, x, y) en píxeles, pares (yuv420-safe)."""
    if zone_w / zone_h >= aspect:
        cw, ch = zone_h * aspect, zone_h
        cx = zone_x + (zone_w - cw) / 2
        cy = zone_y
    else:
        cw, ch = zone_w, zone_w / aspect
        cx = zone_x
        cy = zone_y + (zone_h - ch) / 2
    return (int(round(cw / 2) * 2), int(round(ch / 2) * 2),
            int(round(cx / 2) * 2), int(round(cy / 2) * 2))


def _split_crop(w: int, h: int, corner: str = "top-left") -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Pantalla dividida (2026-11-11, petición de Juan): la cara del
    creador (facecam, típicamente en una esquina) arriba y el juego
    abajo. Cada panel es 1080x960 (aspecto 9/8).

    La facecam ocupa ~32% del ancho y ~38% del alto en su esquina
    (RDjavi: arriba a la izquierda). El juego se recorta del lado
    opuesto (ancho completo en alto) para no mostrar la facecam dos
    veces. Devuelve ((fcw, fch, fcx, fcy), (gw, gh, gx, gy)).
    """
    corner = (corner or "top-left").strip().lower()
    if corner not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        corner = "top-left"
    panel_aspect = 9 / 8
    fw, fh = w * 0.32, h * 0.38
    if corner == "top-left":
        fzone = (0.0, 0.0, fw, fh)
        gzone = (fw, 0.0, w - fw, float(h))
    elif corner == "top-right":
        fzone = (w - fw, 0.0, fw, fh)
        gzone = (0.0, 0.0, w - fw, float(h))
    elif corner == "bottom-left":
        fzone = (0.0, h - fh, fw, fh)
        gzone = (fw, 0.0, w - fw, float(h))
    else:  # bottom-right
        fzone = (w - fw, h - fh, fw, fh)
        gzone = (0.0, 0.0, w - fw, float(h))
    fc = _cover_in_zone(*fzone, panel_aspect)
    gm = _cover_in_zone(*gzone, panel_aspect)
    return fc, gm


def _detect_facecam_box(source: Path, start: float, duration: float,
                        corner: str = "top-left") -> tuple[int, int, int, int] | None:
    """Localiza el CUADRO de la facecam (2026-11-11, queja de Juan: el
    recorte que seguía la cara se movía; el cuadro completo debe quedar
    FIJO arriba).

    El cuadro de la facecam es un rectángulo fijo (overlay con borde)
    en una esquina. Se localiza por la cara (Haar, mediana de 3
    fotogramas — UN solo rectángulo para todo el clip, sin
    seguimiento) y se toma el cuadro completo alrededor de ella: un
    rectángulo fijo de 20% del ancho x 26% del alto centrado en la
    cara, que contiene el cuadro entero (RDjavi: ~14% x 19%) con
    margen. Devuelve (w, h, x, y) del cuadro. None si no hay cara.
    """
    import cv2

    try:
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if cascade.empty():
            return None
        vw, vh = probe_size(source)
        corner = (corner or "top-left").strip().lower()
        zw, zh = vw * 0.45, vh * 0.55
        if corner == "top-left":
            zx, zy = 0, 0
        elif corner == "top-right":
            zx, zy = vw - zw, 0
        elif corner == "bottom-left":
            zx, zy = 0, vh - zh
        else:
            zx, zy = vw - zw, vh - zh
        centers: list[tuple[float, float]] = []
        with tempfile.TemporaryDirectory(prefix="facecam_") as td:
            td = Path(td)
            for i, frac in enumerate((0.25, 0.5, 0.75)):
                t = start + duration * frac
                frame = td / f"f{i}.png"
                _run(["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                      "-i", str(source), "-frames:v", "1", str(frame)],
                     "facecam frame")
                img = cv2.imread(str(frame))
                if img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                roi = gray[int(zy):int(zy + zh), int(zx):int(zx + zw)]
                faces = cascade.detectMultiScale(
                    roi, scaleFactor=1.1, minNeighbors=4,
                    minSize=(30, 30))
                if len(faces) == 0:
                    continue
                x, y, fw, fh = max(faces, key=lambda b: b[2] * b[3])
                centers.append((zx + x + fw / 2, zy + y + fh / 2))
        if not centers:
            return None
        centers.sort(key=lambda c: c[0])
        cx, cy = centers[len(centers) // 2]
        # Cuadro completo alrededor de la cara: tamaño fijo, centrado
        # en la cara, con margen para el borde y el rótulo.
        bw, bh = vw * 0.20, vh * 0.26
        x = max(0.0, min(cx - bw / 2, vw - bw))
        y = max(0.0, min(cy - bh * 0.42, vh - bh))
        return (int(round(bw / 2) * 2), int(round(bh / 2) * 2),
                int(round(x / 2) * 2), int(round(y / 2) * 2))
    except Exception:
        return None


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
                   highlight_color: str = "#FADE4C",
                   brand_size: int = 68, brand_y: int = 90) -> str:
    """Filter chain from [vid] to [out]: frame + brand + title + ASS.

    Identical for every camera mode — the overlays always apply on top
    of the already-framed 9:16 video. The keyword highlight colour is
    its own setting (it used to follow the frame colour); the template
    preset supplies both.
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
                f":textfile={brand_file}:fontsize={int(round(brand_size * scale))}:fontcolor={frame_hex}:"
                f"x=(w-text_w)/2:y={int(round(brand_y * scale))}:box=1:boxcolor=black@0.55:boxborderw=20,"
            )
        if title_text and title_text.strip():
            title_file = tmpdir / "title.txt"
            title_file.write_text(title_text.strip(), encoding="utf-8")
            chain += (
                "drawtext=fontfile=" + FONT_BOLD +
                f":textfile={title_file}:fontsize={int(round(30 * scale))}:fontcolor=white:"
                f"x=(w-text_w)/2:y={int(round((brand_y + 90) * scale))}:box=1:boxcolor=black@0.55:boxborderw=14,"
            )
    if show_subtitles and words:
        box_styles, box_events = _keyword_box_events(
            words, size=subtitle_size, position=subtitle_position,
            highlight_color=highlight_color, width=out_w, height=out_h,
            tmpdir=tmpdir)
        ass_file = tmpdir / "clip.ass"
        ass_file.write_text(
            build_ass(words, size=subtitle_size, position=subtitle_position,
                      style=subtitle_style, highlight_color=highlight_color,
                      width=out_w, height=out_h,
                      extra_styles=box_styles, extra_events=box_events),
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


def _audio_fade_filter(duration: float) -> str:
    """Short audio fades so a cut never ends abruptly. Fast speech has
    almost no pause at sentence ends (seen 2026-11-10: a clip ending at
    'exponencial.' cut into 'Y todo por una razón...' with speech at full
    volume right up to the cut), so the fade makes the ending sound
    intentional instead of chopped."""
    out_start = max(0.0, duration - 0.4)
    return (f"[0:a]afade=t=in:st=0:d=0.15,"
            f"afade=t=out:st={out_start:.2f}:d=0.4[aout]")


# Duración de la portada/título de enganche al inicio del clip.
TITLE_CARD_SECONDS = 2.0


def _title_card(source: Path, cover_abs: float, title: str, out_w: int,
                out_h: int, scale: float, tmpdir: Path) -> Path:
    """Portada del clip (2026-11-11, petición de Juan): un fotograma fijo
    del video (el elegido con el control Portada) a pantalla completa
    9:16, con el título de enganche GRANDE y centrado encima, SIN marco
    amarillo, SIN marca @, SIN subtítulos — solo la portada y el texto,
    para que se vea limpio y enganche. Dura TITLE_CARD_SECONDS (2 s) y
    es silenciosa; el audio del clip empieza después. Devuelve el mp4
    de la portada (sin audio)."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title is empty")
    cover = tmpdir / "cover.png"
    # Fotograma de portada, recorte 9:16 centrado (como el clip).
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(max(0.0, cover_abs)), "-i", str(source),
        "-frames:v", "1",
        "-vf", f"crop=ih*9/16:ih:(iw-ow)/2:0,scale={out_w}:{out_h}",
        str(cover),
    ], "title card frame")
    # Título envuelto en líneas cortas para que se lea grande.
    wrapped = "\n".join(textwrap.wrap(title, width=18))
    title_file = tmpdir / "hook_title.txt"
    title_file.write_text(wrapped, encoding="utf-8")
    card = tmpdir / "card.mp4"
    size = int(round(96 * scale))
    # La portada lleva TAMBIÉN 2 s de audio en silencio (anullsrc), para
    # que tenga los mismos streams que el clip y el concat demuxer pueda
    # copiar sin re-codificar (el re-encode del concat filter era
    # lentísimo: 9+ min de CPU para un clip de 32 s).
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-loop", "1", "-i", str(cover),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(TITLE_CARD_SECONDS),
        "-vf",
        "drawtext=fontfile=" + FONT_BOLD +
        f":textfile={title_file}:fontsize={size}:fontcolor=white:"
        "x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.55:boxborderw=28:"
        "line_spacing=12",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(card),
    ], "title card video")
    return card


def _concat_title_card(card: Path, clip: Path, output: Path) -> None:
    """Antepone la portada (2 s) al clip con el concat DEMUXER y copia
    de streams (-c copy): sin re-codificar, es casi instantáneo. La
    portada trae 2 s de silencio, así que el audio del clip empieza
    justo cuando termina la portada, en sync con su video."""
    concat_list = card.parent / "concat.txt"
    concat_list.write_text(
        f"file '{card}'\nfile '{clip}'\n", encoding="utf-8")
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(output),
    ], "title card concat")


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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
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
        "-filter_complex", base + ";" + chain + ";" + _audio_fade_filter(duration),
        "-map", "[out]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(output),
    ], "follow final pass")
    return output


def render_clip(source: Path, start_in_source: float, duration: float,
                output: Path, words: list[dict] | None = None, *,
                frame_enabled: bool | None = None,
                frame_color: str | None = None,
                frame_width: int | None = None,
                brand_text: str = DEFAULT_BRAND_TEXT,
                title_text: str = "",
                subtitle_style: str = "viral",
                subtitle_size: int = 76,
                subtitle_position: str | None = None,
                show_subtitles: bool = True,
                highlight_color: str | None = None,
                template: str | None = None,
                brand_size: int | None = None,
                brand_y: int | None = None,
                quality: str = "1080p",
                camera_mode: str = "center",
                camera_person: str = "center",
                # Pantalla dividida (2026-11-11): esquina donde está la
                # facecam ("top-left" | "top-right" | "bottom-left" |
                # "bottom-right"). Solo se usa con camera_mode="split".
                facecam_corner: str = "top-left",
                # Portada / título de enganche (2026-11-11): el título gancho
                # (clip_title) se quema GRANDE sobre la portada al inicio
                # del clip, sin marco ni marca. cover_at es el offset (s)
                # dentro del clip para el fotograma de portada; None = 0.
                hook_title: str = "",
                cover_at: float | None = None) -> Path:
    """Render one clip. ``start_in_source`` is seconds into ``source`` file.

    ``quality``: "1080p" (1080x1920) or "720p" (720x1280) — marco, nombre
    y subtítulos escalan con la resolución.

    ``template``: nombre de plantilla ("Gancho grande" / "Limpio" /
    "Impacto"). Cuando viene, sus valores (marco, color, posición de
    subtítulos, color de palabra clave, tamaño de marca) aplican para
    cada parámetro que se dejó en None. Cuando es None, el render es
    personalizado: cada parámetro usa su propio valor o el default
    histórico (marco amarillo, subtítulos abajo, resaltado amarillo).
    Un nombre desconocido cae a "Gancho grande".

    ``camera_mode``: "center" (default, the approved center crop),
    "dual" (left half on top, right half below, for two-person
    interviews), "split" (facecam on top, game below — pantalla
    dividida para videos de gaming), "follow_speaker" (9:16 crop
    follows whoever is speaking, face detection + mouth-movement score)
    or "follow_person" (follows the face in the requested zone: ``camera_person`` left /
    center / right). Follow modes fall back to the center crop when no
    face is found, so tracking can never fail a render.
    """
    if duration <= 0:
        raise ValueError("duration must be > 0")
    mode = (camera_mode or "center").strip().lower()
    if mode not in ("center", "dual", "split", "follow_speaker", "follow_person"):
        mode = "center"
    person = (camera_person or "center").strip().lower()
    if person not in ("left", "center", "right"):
        person = "center"
    # Plantilla: preset para los parámetros que quedaron en None.
    preset = TEMPLATES.get((template or "").strip()) if template else None
    if template and preset is None:
        preset = TEMPLATES[DEFAULT_TEMPLATE]

    def _pick(value, key, default):
        if value is not None:
            return value
        if preset is not None and key in preset:
            return preset[key]
        return default

    frame_enabled = _pick(frame_enabled, "frame_enabled", True)
    frame_color = _pick(frame_color, "frame_color", DEFAULT_FRAME_COLOR)
    frame_width = _pick(frame_width, "frame_width", DEFAULT_FRAME_WIDTH)
    subtitle_position = _pick(subtitle_position, "subtitle_position", "bottom")
    highlight_color = _pick(highlight_color, "highlight_color", DEFAULT_FRAME_COLOR)
    brand_size = _pick(brand_size, "brand_size", 68)
    brand_y = _pick(brand_y, "brand_y", 90)
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
            highlight_color=highlight_color,
            brand_size=brand_size, brand_y=brand_y,
        )
        # El clip principal se renderiza a un temporal; la portada (si
        # hay título de enganche) se antepone después.
        main_path = tmpdir / "main.mp4"
        rendered = False
        if mode in ("follow_speaker", "follow_person"):
            cam = analyze_camera(source, start_in_source, duration, mode, person)
            if not cam["fallback"]:
                logger.info("camera %s: %s/%s samples with faces, "
                            "crop-x %.0f-%.0f, %s switches",
                            mode, cam["stats"]["samples_with_faces"],
                            cam["stats"]["samples"],
                            cam["stats"]["x_min"], cam["stats"]["x_max"],
                            cam["stats"]["switches"])
                _render_follow(source, start_in_source, duration,
                               main_path, cam["path"],
                               out_w=out_w, out_h=out_h,
                               tmpdir=tmpdir,
                               overlay_kwargs=overlay_kwargs)
                rendered = True
            else:
                logger.warning("camera %s: no faces found, using center crop",
                               mode)
                mode = "center"
        if not rendered:
            if mode == "dual":
                vw, vh = probe_size(source)
                cw, ch, cy, xl, xr = _dual_crop(vw, vh)
                base = (
                    f"[0:v]split=2[l][r];"
                    f"[l]crop={cw}:{ch}:{xl}:{cy},scale={out_w}:{out_h // 2}[top];"
                    f"[r]crop={cw}:{ch}:{xr}:{cy},scale={out_w}:{out_h // 2}[bot];"
                    f"[top][bot]vstack=inputs=2,setpts=PTS-STARTPTS[vid]"
                )
            elif mode == "split":
                # Pantalla dividida: facecam arriba, juego abajo.
                # La cara se localiza por detección (fija y precisa);
                # si no hay cara en la esquina, se usa el recorte fijo.
                # El panel superior muestra el cuadro COMPLETO (contain
                # + pad): nada del cuadro se recorta, queda fijo.
                vw, vh = probe_size(source)
                fc = _detect_facecam_box(source, start_in_source,
                                         duration, facecam_corner)
                if fc is None:
                    fc, _ = _split_crop(vw, vh, facecam_corner)
                _, (gw, gh, gx, gy) = _split_crop(vw, vh, facecam_corner)
                fcw, fch, fcx, fcy = fc
                half_h = out_h // 2
                base = (
                    f"[0:v]split=2[fc][gm];"
                    f"[fc]crop={fcw}:{fch}:{fcx}:{fcy},"
                    f"scale={out_w}:{half_h}:force_original_aspect_ratio=decrease,"
                    f"pad={out_w}:{half_h}:(ow-iw)/2:(oh-ih)/2:black[top];"
                    f"[gm]crop={gw}:{gh}:{gx}:{gy},scale={out_w}:{half_h}[bot];"
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
                "-filter_complex", base + ";" + chain + ";" + _audio_fade_filter(duration),
                "-map", "[out]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
                "-c:a", "aac", "-b:a", "128k", "-shortest", str(main_path),
            ], "render")
        # --- Portada / título de enganche (2026-11-11) ---
        # Si hay título gancho, el clip empieza con la portada (fotograma
        # elegido + título grande, sin marco ni marca) y luego el clip.
        hook = (hook_title or "").strip()
        if hook:
            cover_rel = 0.0 if cover_at is None else max(
                0.0, min(float(cover_at), duration))
            card = _title_card(source, start_in_source + cover_rel, hook,
                               out_w, out_h, scale, tmpdir)
            _concat_title_card(card, main_path, output)
        else:
            shutil.copyfile(main_path, output)
    return output
