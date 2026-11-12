"""Automatic intelligent framing for interviews (v70, 2026-11-12).

The system recognises the composition of each shot and builds a
timeline the renderer follows, so an interview clip frames the right
person (or both) without anyone touching a control:

* SINGLE — one person in shot: a vertical 9:16 crop centred on them,
  head + face + shoulders (the crop zooms in when the face is small,
  never out).
* DUAL — two people in the same shot: two INDEPENDENT crops, one
  person on top and the other below, each centred on its person.
  Never a single wide crop trying to fit two distant people.
* Scene change — a hard cut re-evaluates the layout immediately
  (no hysteresis: interviews cut from one speaker to the other and
  the frame must follow at once).
* UNKNOWN — no face in a shot: hold the last valid layout and crop,
  no sudden moves. A clip that starts with no face uses the centre.

How it works (all CPU, all in the VPS, no new stack)
---------------------------------------------------
1. Frames are sampled at 2 fps, 640 px wide, with the system ffmpeg
   (same as app/camera.py).
2. Faces are detected with YuNet (OpenCV DNN, model
   ``app/models/face_detection_yunet_2023mar.onnx``) — far more
   reliable than the Haar cascade (profiles, angles, small faces).
   If the model file is missing, it falls back to the Haar cascade
   already used by app/camera.py, so framing can never fail a render.
3. Scene cuts are detected from the mean absolute difference between
   consecutive sampled frames (hard cuts spike; interviews cut hard).
4. People are tracked between frames by face-centre distance
   (persistent ids, same idea as camera.py). Mouth-movement energy
   and the transcript words are only tiebreakers when a match is
   ambiguous — the CAMERA COMPOSITION (how many faces, where they
   are) decides the layout, never the mouth alone.
5. The clip is split into shots at the cuts. Each shot gets ONE
   layout by majority vote of its samples (so a single missed
   detection cannot flip the frame), and the crop follows the
   target face(s) smoothly inside the shot. Short shots (<0.75 s,
   usually a flash) are merged into the previous one.

``analyze_framing`` never raises: any failure returns a centre-crop
timeline (fallback=True), exactly like ``analyze_camera``.
"""
import json
import logging
import math
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_DT = 0.5          # seconds between analysed frames
DETECT_WIDTH = 640       # analysis resolution (px wide)
DIFF_WIDTH = 320         # width for the scene-cut difference
# Mean abs frame difference (0-255) above this = a hard cut. Tuned on
# the Alofoke interview (qjn_zvadIdM): real cuts spike well above 40,
# inside a shot the diff stays under ~12 even with head movement.
SCENE_DIFF_THRESHOLD = 28.0
YUNET_SCORE_MIN = 0.6    # YuNet confidence below this = not a face
# A face shorter than this fraction of the frame height is background
# (a poster, a crowd, a TV in the back) — it does not count as a person.
MIN_FACE_H_FRAC = 0.07
MATCH_DIST_FRAC = 0.12   # face-centre match radius, fraction of width
TRACK_STALE_S = 2.0      # drop a tracked face not seen for this long
MIN_SHOT_S = 0.75        # shots shorter than this merge into the previous
SMOOTH_WINDOW = 3        # moving-average window for the crop (samples)

_MODEL_PATH = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"


def _model_available() -> bool:
    return _MODEL_PATH.is_file() and _MODEL_PATH.stat().st_size > 100000


def detector_name() -> str:
    """Which face detector analyze_framing() will use."""
    return "yunet" if _model_available() else "haar"


def _even(n: float) -> int:
    return int(round(n / 2) * 2)


def _single_crop(box, W: int, H: int) -> tuple[int, int, int, int]:
    """9:16 crop on ONE person: head + face + shoulders.

    The crop height is ~3.8x the face height (head is ~1x, shoulders
    add ~2.8x), floored at 45% of the frame so a small face never
    forces an extreme zoom, capped at the full frame. The face sits
    ~30% down from the top of the crop, so the head is never cut.
    Never zooms OUT: width follows height (9:16), it never widens to
    fit a second person.
    """
    fx, fy, fw, fh = box
    Hc = min(max(fh * 3.8, 0.45 * H), float(H))
    Wc = Hc * 9 / 16
    cx = fx + fw / 2
    cy = fy + fh / 2
    x = min(max(cx - Wc / 2, 0.0), max(0.0, W - Wc))
    y = min(max(cy - Hc * 0.30, 0.0), max(0.0, H - Hc))
    return _even(Wc), _even(Hc), _even(x), _even(y)


def _dual_crop(box, W: int, H: int) -> tuple[int, int, int, int]:
    """One 9:8 panel crop on ONE person (top or bottom half of a DUAL).

    Same head+shoulders framing as _single_crop, but the panel aspect
    is 9:8 (1080x960). Each person gets its own independent crop, so
    two distant people are never squeezed into one vertical window.
    """
    fx, fy, fw, fh = box
    Hp = min(max(fh * 3.5, 0.50 * H), float(H))
    Wp = Hp * 9 / 8
    cx = fx + fw / 2
    cy = fy + fh / 2
    x = min(max(cx - Wp / 2, 0.0), max(0.0, W - Wp))
    y = min(max(cy - Hp * 0.30, 0.0), max(0.0, H - Hp))
    return _even(Wp), _even(Hp), _even(x), _even(y)


def _probe_size(source: Path) -> tuple[int, int]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "json", str(source)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[-500:]}")
    streams = json.loads(proc.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("no video stream in source")
    return int(streams[0]["width"]), int(streams[0]["height"])


def _extract_frames(source: Path, start: float, duration: float,
                    tmpdir: Path) -> list[Path]:
    """System ffmpeg: one JPEG every SAMPLE_DT seconds, 640 px wide."""
    pattern = tmpdir / "fr_%04d.jpg"
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", str(start), "-i", str(source), "-t", str(duration),
           "-vf", f"fps=1/{SAMPLE_DT},scale={DETECT_WIDTH}:-1",
           "-q:v", "4", str(pattern)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"frame extract failed: {proc.stderr[-500:]}")
    return sorted(tmpdir.glob("fr_*.jpg"))


class _FaceDetector:
    """YuNet when the model file exists, Haar cascade otherwise."""

    def __init__(self):
        import cv2
        self._cv2 = cv2
        self._yunet = None
        self._haar = None
        self.kind = "haar"
        if _model_available():
            try:
                self._yunet = cv2.FaceDetectorYN_create(
                    str(_MODEL_PATH), "", (DETECT_WIDTH, 360))
                self.kind = "yunet"
            except Exception as e:  # noqa: BLE001 — fall back to Haar
                logger.warning("framing: YuNet init failed (%s); using Haar", e)
        if self._yunet is None:
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if not cascade.empty():
                self._haar = cascade

    def detect(self, img, frame_h: int):
        """Return [(x, y, w, h, score)] in DETECT_WIDTH coords."""
        cv2 = self._cv2
        h, w = img.shape[:2]
        if self._yunet is not None:
            try:
                self._yunet.setInputSize((w, h))
                _, faces = self._yunet.detect(img)
            except Exception:
                faces = None
            if faces is None:
                return []
            out = []
            for f in faces:
                x, y, fw, fh = (float(v) for v in f[:4])
                score = float(f[14]) if len(f) > 14 else 1.0
                if score < YUNET_SCORE_MIN:
                    continue
                if fh < MIN_FACE_H_FRAC * frame_h:
                    continue
                out.append((x, y, fw, fh, score))
            return out
        if self._haar is None:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = self._haar.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50))
        out = []
        for (x, y, fw, fh) in found:
            if fh < MIN_FACE_H_FRAC * frame_h:
                continue
            out.append((float(x), float(y), float(fw), float(fh), 1.0))
        return out


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


def _speech_at(words: list[dict] | None, t0: float, t1: float) -> bool:
    """True when any transcript word overlaps [t0, t1) (clip-relative)."""
    if not words:
        return False
    for wd in words:
        try:
            s, e = float(wd.get("s", 0)), float(wd.get("e", 0))
        except (TypeError, ValueError):
            continue
        if s < t1 and e > t0:
            return True
    return False


def _smooth_crops(crops: list[tuple[int, int, int, int] | None]
                  ) -> list[tuple[int, int, int, int] | None]:
    """Moving average over the crop params (per shot), holding gaps."""
    out: list[tuple[int, int, int, int] | None] = []
    last = None
    for c in crops:
        if c is None:
            out.append(last)
            continue
        last = c
        out.append(c)
    # Smooth with the window, but never across a None (hold) boundary:
    # the hold already copied the last valid crop, so a plain average
    # over the filled list is safe.
    filled = [c for c in out]
    sm: list[tuple[int, int, int, int] | None] = []
    for i, c in enumerate(filled):
        if c is None:
            sm.append(None)
            continue
        lo = max(0, i - SMOOTH_WINDOW + 1)
        win = [x for x in filled[lo:i + 1] if x is not None]
        if not win:
            sm.append(c)
            continue
        sm.append(tuple(int(round(sum(v[k] for v in win) / len(win) / 2) * 2)
                        for k in range(4)))
    return sm


def analyze_framing(source: Path, start: float, duration: float,
                    words: list[dict] | None = None) -> dict:
    """Analyse one clip window and return its framing timeline::

        {"samples": [{"t": float, "mode": "single"|"dual"|"unknown",
                      "crop": (w,h,x,y) | None,            # single
                      "crops": [(w,h,x,y), (w,h,x,y)] | None,  # dual
                      "persons": [id, ...], "faces": int,
                      "cut": bool, "speech": bool}, ...],
         "segments": [{"t0": float, "t1": float,
                       "mode": "single"|"dual"|"unknown",
                       "persons": [id, ...]}, ...],
         "fallback": bool, "detector": "yunet"|"haar",
         "stats": {"samples": int, "shots": int, "cuts": int,
                   "faces_total": int, "single_s": float,
                   "dual_s": float, "unknown_s": float}}

    ``samples`` are every SAMPLE_DT seconds from the clip start;
    ``segments`` merge consecutive samples with the same confirmed
    layout. The renderer samples this timeline at each chunk midpoint.
    Never raises: any failure returns a centre-crop timeline.
    """
    try:
        width, height = _probe_size(source)
    except Exception as e:  # noqa: BLE001 — probe failure = centre crop
        logger.warning("framing: probe failed (%s); using centre crop", e)
        return _centre_timeline(duration)
    try:
        import cv2
    except Exception as e:  # noqa: BLE001 — no OpenCV = centre crop
        logger.warning("framing: OpenCV unavailable (%s); using centre crop", e)
        return _centre_timeline(duration, (width, height))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            frames = _extract_frames(source, start, duration, Path(tmp))
            return _analyse_frames(frames, width, height, duration, words)
    except Exception as e:  # noqa: BLE001 — framing must never fail a render
        logger.warning("framing: analysis failed (%s); using centre crop", e)
        return _centre_timeline(duration, (width, height))


def _centre_timeline(duration: float, size: tuple[int, int] | None = None
                     ) -> dict:
    """Fallback timeline: constant centre 9:16 crop, mode unknown."""
    if size is None:
        crop = None
    else:
        w, h = size
        cw = h * 9 / 16
        crop = (_even(cw), _even(h), _even((w - cw) / 2), 0)
    n = max(1, int(duration / SAMPLE_DT) + 1)
    samples = [{"t": round(i * SAMPLE_DT, 2), "mode": "unknown",
                "crop": crop, "crops": None, "persons": [],
                "faces": 0, "cut": False,
                "speech": False} for i in range(n)]
    return {
        "samples": samples,
        "segments": [{"t0": 0.0, "t1": round(duration, 2),
                      "mode": "unknown", "persons": []}],
        "fallback": True, "detector": detector_name(),
        "stats": {"samples": n, "shots": 1, "cuts": 0, "faces_total": 0,
                  "single_s": 0.0, "dual_s": 0.0,
                  "unknown_s": round(duration, 2)},
    }


def _analyse_frames(frames: list[Path], width: int, height: int,
                    duration: float, words: list[dict] | None) -> dict:
    import cv2

    detector = _FaceDetector()
    scale = width / DETECT_WIDTH
    match_dist = MATCH_DIST_FRAC * DETECT_WIDTH
    n = len(frames)

    # --- Per-sample raw data: faces (tracked ids), cut flags. ---
    raw: list[dict] = []
    tracked: dict[int, dict] = {}
    next_id = 0
    prev_gray = None
    prev_small = None
    faces_total = 0
    cuts = 0
    for i, frame_path in enumerate(frames):
        t = i * SAMPLE_DT
        img = cv2.imread(str(frame_path))
        if img is None:
            raw.append({"t": t, "faces": [], "cut": False})
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Scene cut: mean abs diff vs the previous sampled frame.
        small = cv2.resize(gray, (DIFF_WIDTH,
                                  max(1, int(DIFF_WIDTH * gray.shape[0] / gray.shape[1]))))
        cut = False
        if prev_small is not None:
            diff = float(cv2.absdiff(small, prev_small).mean())
            if diff > SCENE_DIFF_THRESHOLD:
                cut = True
                cuts += 1
        prev_small = small
        # A cut resets tracking: faces after a cut are new people until
        # proven otherwise (positions can jump to the other speaker).
        if cut:
            tracked = {}
        faces = detector.detect(img, img.shape[0])
        faces_total += len(faces)
        # Score + match each face to a tracked id (mouth energy breaks
        # ties when two tracked faces are equally close).
        scored = []
        for fid in [f for f, v in tracked.items()
                    if i - v["seen"] > int(TRACK_STALE_S / SAMPLE_DT)]:
            del tracked[fid]
        for (x, y, fw, fh, score) in faces:
            cx, cy = x + fw / 2, y + fh / 2
            energy = _mouth_energy(gray, prev_gray, (x, y, fw, fh))
            best_id, best_d, best_e = None, match_dist, -1.0
            for fid, f in tracked.items():
                d = abs(f["cx"] - cx) + abs(f["cy"] - cy) * 0.5
                if d < best_d or (d == best_d and f["energy"] < energy):
                    best_id, best_d, best_e = fid, d, f["energy"]
            if best_id is None:
                best_id = next_id
                next_id += 1
            tracked[best_id] = {"cx": cx, "cy": cy, "energy": energy,
                                "seen": i}
            scored.append({"id": best_id, "cx": cx, "cy": cy,
                           "box": (x, y, fw, fh), "score": score,
                           "energy": energy})
        prev_gray = gray
        # Two largest faces by area (a third face in the background is
        # ignored — DUAL is the two main people).
        scored.sort(key=lambda s: s["box"][2] * s["box"][3], reverse=True)
        raw.append({"t": t, "faces": scored[:2], "cut": cut})
    if n == 0:
        return _centre_timeline(duration, (width, height))

    # --- Shots: split at cuts, merge shots shorter than MIN_SHOT_S. ---
    bounds = [0]
    for i in range(1, n):
        if raw[i]["cut"]:
            bounds.append(i)
    bounds.append(n)
    shots: list[tuple[int, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        if shots and (b - a) * SAMPLE_DT < MIN_SHOT_S:
            # Tiny shot (flash / false cut): fold into the previous one.
            pa, _ = shots[-1]
            shots[-1] = (pa, b)
        else:
            shots.append((a, b))

    # --- Layout per shot by majority vote; targets = most seen ids. ---
    samples: list[dict] = [None] * n  # type: ignore[list-item]
    segments: list[dict] = []
    single_s = dual_s = unknown_s = 0.0
    last_valid = None  # (mode, crop/crops, persons) to hold on UNKNOWN
    for (a, b) in shots:
        shot = raw[a:b]
        # Majority face-count in this shot.
        counts = {0: 0, 1: 0, 2: 0}
        for s in shot:
            counts[min(len(s["faces"]), 2)] += 1
        n_faces = max(counts, key=lambda k: counts[k])
        # Most frequent ids in the shot (for the targets).
        id_hits: dict[int, int] = {}
        for s in shot:
            for f in s["faces"]:
                id_hits[f["id"]] = id_hits.get(f["id"], 0) + 1
        top_ids = sorted(id_hits, key=lambda i: -id_hits[i])
        if n_faces == 0:
            mode = "unknown"
        elif n_faces == 1:
            mode = "single"
        else:
            mode = "dual"
        # Crops per sample inside the shot (follow the target face(s),
        # smoothed; UNKNOWN holds the last valid crop).
        shot_crops: list[tuple[int, int, int, int] | None] = []
        shot_crops2: list[tuple[tuple[int, int, int, int],
                                tuple[int, int, int, int]] | None] = []
        for s in shot:
            if mode == "single":
                tgt = None
                for f in s["faces"]:
                    if f["id"] == top_ids[0]:
                        tgt = f
                        break
                if tgt is None and s["faces"]:
                    tgt = s["faces"][0]
                if tgt is None:
                    shot_crops.append(None)
                    shot_crops2.append(None)
                    continue
                box = tuple(v * scale for v in tgt["box"])
                shot_crops.append(_single_crop(box, width, height))
                shot_crops2.append(None)
            elif mode == "dual":
                picks = []
                for pid in top_ids[:2]:
                    for f in s["faces"]:
                        if f["id"] == pid:
                            picks.append(f)
                            break
                if len(picks) < 2:
                    picks = s["faces"][:2]
                if len(picks) < 2:
                    shot_crops.append(None)
                    shot_crops2.append(None)
                    continue
                # Top panel = the person on the LEFT, bottom = RIGHT.
                picks.sort(key=lambda f: f["cx"])
                c1 = _dual_crop(tuple(v * scale for v in picks[0]["box"]),
                                width, height)
                c2 = _dual_crop(tuple(v * scale for v in picks[1]["box"]),
                                width, height)
                shot_crops.append(None)
                shot_crops2.append((c1, c2))
            else:
                shot_crops.append(None)
                shot_crops2.append(None)
        if mode == "single":
            shot_crops = _smooth_crops(shot_crops)
        elif mode == "dual":
            # Smooth each panel independently.
            tops = _smooth_crops([c[0] if c else None for c in shot_crops2])
            bots = _smooth_crops([c[1] if c else None for c in shot_crops2])
            shot_crops2 = [(t, b) if (t and b) else None
                           for t, b in zip(tops, bots)]
        # Hold the last valid layout on UNKNOWN (or centre at the start).
        # The sample keeps mode "unknown" for the timeline, but carries
        # the held crop(s) so the renderer just keeps framing the same
        # way: it reads crop (single) or crops (dual), whichever is set.
        if last_valid is None:
            cw = height * 9 / 16
            hold_crop = (_even(cw), _even(height),
                         _even((width - cw) / 2), 0)
            hold_crops = None
            persons_h: list[int] = []
        elif last_valid[0] == "single":
            hold_crop, hold_crops = last_valid[1], None
            persons_h = last_valid[3]
        else:
            hold_crop, hold_crops = None, last_valid[2]
            persons_h = last_valid[3]
        if mode == "unknown":
            for j, s in enumerate(shot):
                samples[a + j] = {
                    "t": s["t"], "mode": "unknown",
                    "crop": hold_crop, "crops": hold_crops,
                    "persons": persons_h, "faces": len(s["faces"]),
                    "cut": s["cut"],
                    "speech": _speech_at(words, s["t"], s["t"] + SAMPLE_DT),
                }
            unknown_s += (b - a) * SAMPLE_DT
            segments.append({"t0": round(a * SAMPLE_DT, 2),
                             "t1": round(min(b * SAMPLE_DT, duration), 2),
                             "mode": "unknown", "persons": persons_h})
            continue
        persons = top_ids[:2] if mode == "dual" else top_ids[:1]
        for j, s in enumerate(shot):
            if mode == "single":
                c = shot_crops[j]
                samples[a + j] = {
                    "t": s["t"], "mode": "single", "crop": c,
                    "crops": None, "persons": persons,
                    "faces": len(s["faces"]), "cut": s["cut"],
                    "speech": _speech_at(words, s["t"], s["t"] + SAMPLE_DT),
                }
                if c is not None:
                    last_valid = ("single", c, None, persons)
            else:
                c = shot_crops2[j]
                samples[a + j] = {
                    "t": s["t"], "mode": "dual", "crop": None,
                    "crops": c, "persons": persons,
                    "faces": len(s["faces"]), "cut": s["cut"],
                    "speech": _speech_at(words, s["t"], s["t"] + SAMPLE_DT),
                }
                if c is not None:
                    last_valid = ("dual", None, c, persons)
        if mode == "single":
            single_s += (b - a) * SAMPLE_DT
        else:
            dual_s += (b - a) * SAMPLE_DT
        segments.append({"t0": round(a * SAMPLE_DT, 2),
                         "t1": round(min(b * SAMPLE_DT, duration), 2),
                         "mode": mode, "persons": persons})
    # Fill any gaps (should not happen) with the centre crop.
    for i, s in enumerate(samples):
        if s is None:
            cw = height * 9 / 16
            samples[i] = {"t": round(i * SAMPLE_DT, 2), "mode": "unknown",
                          "crop": (_even(cw), _even(height),
                                   _even((width - cw) / 2), 0),
                          "crops": None, "persons": [], "faces": 0,
                          "cut": False,
                          "speech": _speech_at(words, i * SAMPLE_DT,
                                               i * SAMPLE_DT + SAMPLE_DT)}
    return {
        "samples": samples,
        "segments": segments,
        "fallback": False,
        "detector": detector.kind,
        "stats": {"samples": n, "shots": len(shots), "cuts": cuts,
                  "faces_total": faces_total,
                  "single_s": round(single_s, 1),
                  "dual_s": round(dual_s, 1),
                  "unknown_s": round(unknown_s, 1)},
    }
