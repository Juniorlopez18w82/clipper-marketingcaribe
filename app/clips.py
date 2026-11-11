"""Clip candidates from a transcript.

Two selectors, same output shape (start/end/duration/text/words/score,
times RELATIVE to the downloaded section; main.py converts them to
absolute original-video times by adding the section start):

1. Heuristic (v1, ``build_candidates``): deliberately simple and
   explainable (no invented virality) — windows of 20-45 s that start/end
   on a speech segment boundary, scored by words-per-minute, exclamations
   and laughter. It does NOT understand topics, so it can cut a sentence
   in half; the user picks the window in the frontend.
2. LLM (v2, ``build_candidates_llm``): a real model (Groq llama, free
   tier) reads the whole transcript and picks up to 5 clips, each covering
   ONE complete topic/idea from its first sentence to its last. The code
   then snaps every cut to word boundaries, so a clip can never start or
   end mid-word. Any LLM failure (no key, 401/429/timeout, bad JSON, zero
   valid clips) returns None and the caller falls back to the heuristic —
   a failed LLM must never fail a clips job.

The user picks the window in the frontend; nothing is auto-published.
"""
import json
import logging
import math
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


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


# --- LLM selection via Groq (free tier) -----------------------------------
# Same key as Groq transcription (GROQ_API_KEY). No key -> the caller
# falls back to the heuristic; nothing here ever raises for a missing key.
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_LLM_PRIMARY = "llama-3.3-70b-versatile"
GROQ_LLM_FALLBACK = "llama-3.1-8b-instant"
GROQ_LLM_TIMEOUT = 90  # seconds per request
# The prompt asks for clips between min_len and max_len; validation is
# deliberately a little looser (15-60 s) so a good clip is not thrown
# away for being a couple of seconds short of the request.
LLM_MIN_SECONDS = 15.0
LLM_MAX_SECONDS = 60.0
LLM_MAX_CLIPS = 5
# Keep the prompt bounded for very long sections.
LLM_MAX_TRANSCRIPT_CHARS = 12000


def _flatten_words(segments: list[dict]) -> list[dict]:
    words = []
    for s in segments:
        for w in s.get("words", []):
            words.append({"w": w["w"], "s": w["s"], "e": w["e"]})
    return words


def _llm_prompt(segments: list[dict], max_clips: int,
                min_len: float, max_len: float) -> str:
    """Spanish prompt: the transcript with timestamps, strict JSON out."""
    lines = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if text:
            lines.append(f"[{float(s.get('start', 0)):.1f}-{float(s.get('end', 0)):.1f}] {text}")
    transcript = "\n".join(lines)
    if len(transcript) > LLM_MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:LLM_MAX_TRANSCRIPT_CHARS].rsplit("\n", 1)[0]
    return (
        "Eres un editor de clips virales para TikTok, Reels y Shorts. "
        "Te doy la transcripción de un video con marcas de tiempo en segundos.\n"
        f"Devuelve hasta {max_clips} clips, cada uno sobre UN solo tema o idea completa.\n"
        "Reglas obligatorias:\n"
        "- Cada clip debe empezar EXACTAMENTE donde empieza la idea o la frase, "
        "y terminar EXACTAMENTE donde termina la idea. Nunca cortes a mitad de "
        "una frase ni a mitad de una palabra.\n"
        f"- Duración entre {min_len:.0f} y {max_len:.0f} segundos por clip.\n"
        "- El clip debe tener un gancho en los primeros 3 segundos: algo "
        "sorprendente, una pregunta o una frase fuerte.\n"
        "- Los clips no pueden solaparse entre sí.\n"
        "- Si un tema continúa más allá del máximo, corta al final de una frase "
        "completa, nunca a mitad.\n"
        "- Devuelve SOLO JSON válido, sin texto fuera del JSON, con este formato:\n"
        '{"clips": [{"start": 12.3, "end": 48.0, '
        '"title": "título corto del tema", '
        '"reason": "por qué este momento engancha"}, ...]}\n'
        '- "start" y "end" son segundos (un decimal) relativos al inicio del video.\n'
        '- Si no hay ningún momento claro, devuelve {"clips": []}.\n\n'
        "Transcripción:\n" + transcript
    )


def _groq_chat(prompt: str) -> str | None:
    """One Groq chat completion. Tries the 70b model first, then the 8b
    fallback. Returns the raw content string, or None on any failure."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    for model in (GROQ_LLM_PRIMARY, GROQ_LLM_FALLBACK):
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system",
                 "content": "Eres un editor de clips virales. Respondes SOLO con JSON válido."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1500,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            GROQ_CHAT_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=GROQ_LLM_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning("Groq LLM %s failed (HTTP %s); trying next model",
                           model, e.code)
            continue
        except Exception as e:  # noqa: BLE001 — timeout / DNS / network, any
            logger.warning("Groq LLM %s failed (%s); trying next model",
                           model, e)
            continue
        if isinstance(payload, dict) and payload.get("error"):
            logger.warning("Groq LLM %s returned an error payload; "
                           "trying next model", model)
            continue
        try:
            return str(payload["choices"][0]["message"]["content"])
        except Exception as e:  # noqa: BLE001 — unexpected response shape
            logger.warning("Groq LLM %s response could not be read (%s)",
                           model, e)
            continue
    return None


def _parse_llm_clips(content: str) -> list[dict] | None:
    """Parse the LLM's JSON (bare list or {"clips": [...]}). Returns the
    raw item list, or None when it is not parseable JSON."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001 — not JSON at all
        logger.warning("Groq LLM returned non-JSON (%s); "
                       "falling back to heuristic", e)
        return None
    if isinstance(data, dict):
        items = data.get("clips")
    else:
        items = data
    if not isinstance(items, list):
        logger.warning("Groq LLM JSON has no clips list; "
                       "falling back to heuristic")
        return None
    return items


def _snap_range(start: float, end: float, words: list[dict]):
    """Snap a raw LLM time range to whole-word boundaries so a clip can
    never start or end mid-word. Returns (start, end, first_idx, last_idx)
    or None when no whole word falls inside the range."""
    first = next((i for i, w in enumerate(words) if w["e"] > start), None)
    last = next((i for i in range(len(words) - 1, -1, -1)
                 if words[i]["s"] < end), None)
    if first is None or last is None or last < first:
        return None
    return words[first]["s"], words[last]["e"], first, last


def _validate_llm_clips(items: list[dict], words: list[dict],
                        t_start: float, t_end: float) -> list[dict]:
    """Validate LLM clips hard and snap them to word boundaries. Drops:
    non-numeric or missing times, ranges outside the transcript, clips
    shorter than 15 s or longer than 60 s after snapping, clips with
    fewer than 8 words, and any clip overlapping one already kept
    (LLM order wins: better-ranked clips are kept first). Returns the
    kept clips in LLM order with snapped times."""
    kept = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)):
            continue
        if end <= start:
            continue
        # Clamp into the transcript, then snap to whole words.
        start = max(t_start, min(start, t_end))
        end = max(t_start, min(end, t_end))
        snapped = _snap_range(start, end, words)
        if snapped is None:
            continue
        s, e, first, last = snapped
        duration = e - s
        if duration < LLM_MIN_SECONDS or duration > LLM_MAX_SECONDS:
            continue
        if last - first + 1 < 8:
            continue
        if any(s < k["end"] and e > k["start"] for k in kept):
            continue
        text = " ".join(w["w"].strip() for w in words[first:last + 1]).strip()
        if not text:
            continue
        kept.append({
            "start": round(s, 2), "end": round(e, 2),
            "duration": round(duration, 2), "text": text,
            "words": last - first + 1,
            "title": str(item.get("title") or "")[:120],
            "reason": str(item.get("reason") or "")[:300],
        })
    return kept


def build_candidates_llm(segments: list[dict], max_clips: int = LLM_MAX_CLIPS,
                         min_len: float = 20.0,
                         max_len: float = LLM_MAX_SECONDS) -> list[dict] | None:
    """Topic-aware clip selection with a real LLM (Groq llama, free tier).

    Returns candidates in the EXACT shape the frontend already uses
    (start/end/duration/text/words/score, plus the LLM's title/reason),
    score 95/90/85... in LLM order, sorted by start. Returns None when
    the LLM was not attempted (no GROQ_API_KEY), failed, or produced zero
    valid clips — the caller must then fall back to ``build_candidates``.
    """
    if not segments:
        return None
    words = _flatten_words(segments)
    if len(words) < 8:
        return None
    t_start = min(float(s.get("start", 0) or 0) for s in segments)
    t_end = max(float(s.get("end", 0) or 0) for s in segments)
    if t_end <= t_start:
        return None
    prompt = _llm_prompt(segments, max_clips, min_len, max_len)
    content = _groq_chat(prompt)
    if content is None:
        return None
    items = _parse_llm_clips(content)
    if items is None:
        return None
    valid = _validate_llm_clips(items, words, t_start, t_end)
    if not valid:
        logger.warning("Groq LLM returned no valid clips; "
                       "falling back to heuristic")
        return None
    candidates = []
    for i, c in enumerate(valid[:max(1, max_clips)]):
        candidates.append({**c, "score": round(95 - 5 * i, 1)})
    candidates.sort(key=lambda c: c["start"])
    return candidates
