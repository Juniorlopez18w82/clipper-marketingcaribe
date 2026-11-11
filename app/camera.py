"""Camera tracking for clips: follow the speaker / a chosen person.

Juan 2026-11-10: in the editor there must be a "Cámara" choice —
centered (as always), double camera, follow the speaker, or follow one
chosen person.

How it works
-------------
Frames are extracted with the SYSTEM ffmpeg (2 fps, 640 px wide) —
OpenCV's bundled ffmpeg cannot decode the AV1 webm sources reliably
(seek/decode fails), while the system ffmpeg is the same decoder the
rest of the pipeline already uses. Faces are then detected with the
OpenCV Haar cascade (haarcascade_frontalface_default.xml, shipped
with opencv-python-headless).

Speaking score: for each detected face, the mouth region (lower third
of the face box) is compared with the previous sampled frame — the
mean absolute pixel difference is the "mouth-movement energy". The
face with the highest energy in a sample is the one speaking. This is
a heuristic, not lip reading: fast head movements also raise the
score, and a still speaker scores low. It works well enough for
interviews where one person talks at a time; overlapping voices or
both people moving will confuse it.

Hysteresis: the active speaker only changes when the challenger beats
the current speaker by 1.6x AND at least 1.5 s have passed since the
last switch, so the camera does not jump back and forth. When the
current speaker leaves the frame, the loudest visible face takes over
immediately. When no face is visible, the crop holds its last
position; when no face is detected in the whole clip, the render falls
back to the center crop — tracking can never fail a render.

Smoothing: the crop-x path is a 5-sample moving average with the pan
speed limited to 0.6 crop-widths per second, so the camera glides
instead of snapping.

``follow_person`` picks the largest detected face whose center falls
in the requested third of the frame (left / center / right) and holds
it; if that zone is empty it holds the last position, then centers.

``analyze_camera`` never raises: any failure (no OpenCV, ffmpeg error,
no faces) returns a center-crop path with ``fallback=True``.
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_DT = 0.5          # seconds between analysed frames
DETECT_WIDTH = 640       # analysis resolution (px wide)
HOLD_MIN_SWITCH_S = 1.5  # do not switch speaker more often than this
SPEAKER_RATIO = 1.6      # challenger must beat current speaker by this
SPEAK_FLOOR = 5.0        # mean abs diff below this = mouth not moving
MAX_PAN_PER_S = 0.6      # crop-widths per second, max pan speed
SMOOTH_WINDOW = 5        # moving-average window (samples)
MATCH_DIST_FRAC = 0.12   # face-center match radius, fraction of width


def probe_size(source: Path) -> tuple[int, int]:
    """ffprobe: (width, height) of the first video stream."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "json", str(source)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[-500:]}")
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("no video stream in source")
    return int(streams[0]["width"]), int(streams[0]["height"])


def _extract_frames(source: Path, start: float, duration: float,
                    tmpdir: Path) -> list[Path]:
    """System ffmpeg: one JPEG every SAMPLE_DT seconds, 640 px wide."""
    pattern = tmpdir / "f_%04d.jpg"
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", str(start), "-i", str(source), "-t", str(duration),
           "-vf", f"fps=1/{SAMPLE_DT},scale={DETECT_WIDTH}:-1",
           "-q:v", "4", str(pattern)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"frame extract failed: {proc.stderr[-500:]}")
    return sorted(tmpdir.glob("f_*.jpg"))


def analyze_camera(source: Path, start: float, duration: float,
                   mode: str = "follow_speaker",
                   person: str = "center") -> dict:
    """Follow path for a clip window. Returns::

        {"path": [[t_rel, x_px], ...],   # crop-x (source pixels) per sample
         "fallback": bool,               # True -> no face found, use center
         "stats": {"samples": int, "samples_with_faces": int,
                   "faces_total": int, "x_min": float, "x_max": float,
                   "switches": int, "speaker_energies": [float, ...]}}

    ``x_px`` is the left edge of the 9:16 crop (full source height) in
    source pixels, already smoothed and pan-limited. ``t_rel`` is
    seconds from the clip start.
    """
    mode = (mode or "follow_speaker").strip().lower()
    person = (person or "center").strip().lower()
    try:
        width, height = probe_size(source)
    except Exception as e:  # noqa: BLE001 — probe failure = center crop
        logger.warning("camera: probe failed (%s); using center crop", e)
        return _center_path(duration, None)
    crop_w = height * 9 / 16
    center_x = (width - crop_w) / 2

    try:
        import cv2
    except Exception as e:  # noqa: BLE001 — OpenCV missing = center crop
        logger.warning("camera: OpenCV unavailable (%s); using center crop", e)
        return _center_path(duration, (width, height))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            frames = _extract_frames(source, start, duration, Path(tmp))
            return _analyse_frames(frames, width, height, crop_w, center_x,
                                   mode, person, duration)
    except Exception as e:  # noqa: BLE001 — tracking must never fail a render
        logger.warning("camera: analysis failed (%s); using center crop", e)
        return _center_path(duration, (width, height))


def _center_path(duration: float, size: tuple[int, int] | None) -> dict:
    """Fallback path: constant center crop."""
    if size is None:
        cx = 0.0
    else:
        w, h = size
        cx = (w - h * 9 / 16) / 2
    n = max(1, int(duration / SAMPLE_DT) + 1)
    return {
        "path": [[round(i * SAMPLE_DT, 2), round(cx, 1)] for i in range(n)],
        "fallback": True,
        "stats": {"samples": n, "samples_with_faces": 0, "faces_total": 0,
                  "x_min": cx, "x_max": cx, "switches": 0,
                  "speaker_energies": []},
    }


def _mouth_energy(gray, prev_gray, box) -> float:
    """Mean abs frame difference in the mouth region (lower third)."""
    x, y, w, h = box
    mx, my = int(x + 0.2 * w), int(y + 0.65 * h)
    mw, mh = int(0.6 * w), int(0.35 * h)
    if prev_gray is None or mw <= 0 or mh <= 0:
        return 0.0
    roi = gray[my:my + mh, mx:mx + mw]
    proi = prev_gray[my:my + mh, mx:mx + mw]
    if roi.size == 0:
        return 0.0
    import cv2
    return float(cv2.absdiff(roi, proi).mean())


def _analyse_frames(frames: list[Path], width: int, height: int,
                    crop_w: float, center_x: float, mode: str,
                    person: str, duration: float) -> dict:
    import cv2

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        raise RuntimeError("Haar cascade could not be loaded")
    scale = width / DETECT_WIDTH  # detection coords -> source pixels
    match_dist = MATCH_DIST_FRAC * DETECT_WIDTH

    # Tracked faces: {id: {"cx": float, "energy": float, "seen": int}}
    tracked: dict[int, dict] = {}
    next_id = 0
    speaker_id: int | None = None
    last_switch_t = -1e9
    switches = 0
    energies: list[float] = []
    targets: list[float] = []  # target crop-x per sample (None = hold)
    samples_with_faces = 0
    faces_total = 0
    prev_gray = None

    def zone_of(cx: float) -> str:
        if cx < DETECT_WIDTH / 3:
            return "left"
        if cx < 2 * DETECT_WIDTH / 3:
            return "center"
        return "right"

    for i, frame_path in enumerate(frames):
        t = i * SAMPLE_DT
        img = cv2.imread(str(frame_path))
        if img is None:
            targets.append(None)
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50))
        faces = [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in faces]
        faces_total += len(faces)
        if faces:
            samples_with_faces += 1
        # Score each face and match it to a tracked id.
        scored = []
        # Drop faces not seen for >2 s so a stale position cannot match
        # a new face appearing near it.
        for fid in [f for f, v in tracked.items() if i - v["seen"] > 4]:
            del tracked[fid]
        for box in faces:
            x, y, w, h = box
            cx = x + w / 2
            energy = _mouth_energy(gray, prev_gray, box)
            best_id, best_d = None, match_dist
            for fid, f in tracked.items():
                d = abs(f["cx"] - cx)
                if d < best_d:
                    best_id, best_d = fid, d
            if best_id is None:
                best_id = next_id
                next_id += 1
            tracked[best_id] = {"cx": cx, "energy": energy, "seen": i}
            scored.append({"id": best_id, "cx": cx, "energy": energy,
                           "w": w, "box": box})
        prev_gray = gray
        if not scored:
            targets.append(None)
            continue
        energies.extend(s["energy"] for s in scored)

        if mode == "follow_person":
            in_zone = [s for s in scored if zone_of(s["cx"]) == person]
            if in_zone:
                # Largest face in the requested zone.
                pick = max(in_zone, key=lambda s: s["w"])
                targets.append(pick["cx"] * scale)
            else:
                targets.append(None)
            continue

        # follow_speaker: loudest mouth wins, with hold/hysteresis.
        loudest = max(scored, key=lambda s: s["energy"])
        if speaker_id is None:
            speaker_id = loudest["id"]
            last_switch_t = t
        else:
            cur = next((s for s in scored if s["id"] == speaker_id), None)
            if cur is None:
                speaker_id = loudest["id"]
                last_switch_t = t
                switches += 1
            elif (loudest["id"] != speaker_id
                    and loudest["energy"] > SPEAK_FLOOR
                    and loudest["energy"] > SPEAKER_RATIO * max(cur["energy"], 0.1)
                    and t - last_switch_t >= HOLD_MIN_SWITCH_S):
                speaker_id = loudest["id"]
                last_switch_t = t
                switches += 1
        active = next((s for s in scored if s["id"] == speaker_id), None)
        if active is None:
            active = loudest
        targets.append(active["cx"] * scale)

    # Fill holds with the last known FACE CENTER (not crop-x: the
    # conversion to crop-x happens once, below). Start at frame center.
    filled: list[float] = []
    last = width / 2
    saw_face = False
    for tgt in targets:
        if tgt is None:
            filled.append(last)
        else:
            saw_face = True
            last = tgt
            filled.append(tgt)
    if not saw_face:
        return _center_path(duration, (width, height))

    # To crop-x, clamped; then smooth (moving average) + pan-speed limit.
    raw = [min(max(cx - crop_w / 2, 0.0), width - crop_w) for cx in filled]
    smooth: list[float] = []
    for i in range(len(raw)):
        lo = max(0, i - SMOOTH_WINDOW + 1)
        smooth.append(sum(raw[lo:i + 1]) / (i + 1 - lo))
    max_delta = MAX_PAN_PER_S * crop_w * SAMPLE_DT
    limited: list[float] = []
    prev_x = smooth[0]
    for x in smooth:
        d = max(-max_delta, min(max_delta, x - prev_x))
        prev_x = prev_x + d
        limited.append(prev_x)

    path = [[round(i * SAMPLE_DT, 2), round(x, 1)]
            for i, x in enumerate(limited)]
    xs = [x for _, x in path]
    return {
        "path": path,
        "fallback": False,
        "stats": {"samples": len(frames),
                  "samples_with_faces": samples_with_faces,
                  "faces_total": faces_total,
                  "x_min": min(xs), "x_max": max(xs),
                  "switches": switches,
                  "speaker_energies": [round(e, 1) for e in energies]},
    }
