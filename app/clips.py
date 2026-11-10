"""Clip candidates from a transcript.

v1 scoring is deliberately simple and explainable (no invented virality):
- windows of 20-45 s that start/end on a speech segment boundary
- more words per minute = more energy, small bonus for ! / ? / laughter
- windows must contain at least 8 words and no gap > 4 s inside

The user picks the window in the frontend; nothing is auto-published.
Times here are RELATIVE to the downloaded section; main.py converts them
to absolute original-video times by adding the section start.
"""
import math


def build_candidates(segments: list[dict], min_len: float = 20.0,
                     max_len: float = 45.0) -> list[dict]:
    if not segments:
        return []
    # Flatten words with segment text for scoring.
    words = []
    for s in segments:
        for w in s.get("words", []):
            words.append({"w": w["w"], "s": w["s"], "e": w["e"]})
    if len(words) < 8:
        return []

    candidates = []
    # Slide a window starting at each segment start.
    starts = [s["start"] for s in segments]
    for start in starts:
        end = start + max_len
        win_words = [w for w in words if start <= w["s"] < end]
        if len(win_words) < 8:
            continue
        # End at the last word before max_len, but at least min_len.
        last = win_words[-1]["e"]
        if last - start < min_len:
            continue
        # Trim to the last word that keeps length <= max_len.
        win_words = [w for w in win_words if w["e"] - start <= max_len]
        clip_end = win_words[-1]["e"]
        if clip_end - start < min_len:
            continue
        text = " ".join(w["w"].strip() for w in win_words).strip()
        duration = clip_end - start
        wpm = len(win_words) / max(duration / 60.0, 0.01)
        # Largest internal gap — a long silence makes a bad clip.
        gaps = [b["s"] - a["e"] for a, b in zip(win_words, win_words[1:])]
        max_gap = max(gaps) if gaps else 0.0
        if max_gap > 4.0:
            continue
        score = round(
            min(wpm / 150.0, 1.0) * 70
            + min(len(text) / 200.0, 1.0) * 15
            + (10 if any(c in text for c in "!?¡¿") else 0)
            + (5 if "ja" in text.lower() else 0),
            1,
        )
        candidates.append({
            "start": round(start, 2), "end": round(clip_end, 2),
            "duration": round(duration, 2), "text": text,
            "words": len(win_words), "score": score,
        })

    # De-duplicate overlapping windows: keep the best score per ~10 s bucket.
    candidates.sort(key=lambda c: c["score"], reverse=True)
    picked: list[dict] = []
    for c in candidates:
        if all(abs(c["start"] - p["start"]) > 10 for p in picked):
            picked.append(c)
        if len(picked) >= 8:
            break
    picked.sort(key=lambda c: c["start"])
    return picked
