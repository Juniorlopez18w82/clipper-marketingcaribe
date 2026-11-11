"""Clip candidates from a transcript.

Two selectors, same output shape (start/end/duration/text/words/score,
times RELATIVE to the downloaded section; main.py converts them to
absolute original-video times by adding the section start):

1. Heuristic (v1, ``build_candidates``): deliberately simple and
   explainable (no invented virality) — windows of 20-45 s that start/end
   on a speech segment boundary, scored by words-per-minute, exclamations
   and laughter. It does NOT understand topics, so it can cut a sentence
   in half; the user picks the window in the frontend.
2. LLM (v2, ``build_candidates_llm``): a real model (Groq gpt-oss, free
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
        # Terminar en final de frase cuando sea posible, para no cortar
        # la idea a mitad de frase (el corte por max_len cae donde caiga).
        # Mejor aún si tras esa frase el hablante hace una pausa.
        # (La búsqueda del índice es por identidad: hay palabras repetidas
        # con el mismo texto y tiempos, y words.index() devolvería la primera.)
        def _idx_of(target):
            return next((i for i, w in enumerate(words) if w is target), -1)

        paused_end = None
        last_sent_end = None
        for w in reversed(win_words):
            if _is_sentence_end(w["w"]) and w["e"] - start >= min_len:
                if last_sent_end is None:
                    last_sent_end = w
                idx = _idx_of(w)
                gap = (words[idx + 1]["s"] - w["e"]) if 0 <= idx < len(words) - 1 else 99.0
                if gap >= PAUSE_SECONDS:
                    paused_end = w
                    break
        end_word = paused_end or last_sent_end
        if end_word is not None:
            # Cortar por posición (no por tiempo de fin): con los solapes
            # de Whisper, palabras de la frase siguiente pueden terminar
            # antes que la última palabra de la frase y colarse en el texto.
            _ew = next((i for i, x in enumerate(win_words) if x is end_word), -1)
            if _ew >= 0:
                win_words = win_words[:_ew + 1]
            else:
                win_words = [x for x in win_words if x["e"] <= end_word["e"]]
            clip_end = end_word["e"]
        # Si la palabra siguiente empieza antes de que termine la última
        # (los tiempos de Whisper se solapan), cortar ahí para no incluir
        # el inicio de la frase siguiente ni en el audio ni en subtítulos.
        _idx = _idx_of(win_words[-1])
        if 0 <= _idx < len(words) - 1 and words[_idx + 1]["s"] < clip_end:
            clip_end = words[_idx + 1]["s"]
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
GROQ_LLM_PRIMARY = "openai/gpt-oss-120b"
GROQ_LLM_FALLBACK = "openai/gpt-oss-20b"
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
        "una frase ni a mitad de una palabra. El final debe ser un punto "
        "final (.), nunca una coma. Prefiere terminar donde el hablante hace "
        "una pausa o la idea concluye: si la frase siguiente continúa la misma "
        "idea sin pausa, extiende el clip hasta que la idea quede completa.\n"
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
    """One Groq chat completion. Tries gpt-oss-120b first, then gpt-oss-20b.
    Returns the raw content string, or None on any failure."""
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
            # 4000, no 1500: gpt-oss gasta tokens razonando y esos cuentan
            # en max_tokens; con 1500 el modelo no llegaba a emitir el JSON
            # y Groq devolvía HTTP 400 json_validate_failed (visto
            # 2026-11-10 en el video de la entrevista: los dos modelos
            # fallaron y el selector cayó al heurístico).
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            GROQ_CHAT_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     # Groq (Cloudflare) 403s Python-urllib's default UA.
                     "User-Agent": "ClipViral/1.5.0"})
        try:
            with urllib.request.urlopen(req, timeout=GROQ_LLM_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            logger.warning("Groq LLM %s failed (HTTP %s %s); trying next model",
                           model, e.code, detail)
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


_SENTENCE_END = (".", "!", "?", "…")

# Pausa mínima tras el final de frase para que el corte suene limpio
# (el hablante respira). Por debajo de esto el hablante sigue hablando
# y el corte suena a mitad de pensamiento.
PAUSE_SECONDS = 0.4


def _is_sentence_end(word: str) -> bool:
    """True when the word ends a sentence (final ., !, ? or …)."""
    w = (word or "").strip().rstrip("\"'”»)]}")
    return w.endswith(_SENTENCE_END)


def _snap_to_sentences(first: int, last: int, words: list[dict],
                       min_seconds: float, max_seconds: float) -> tuple[int, int]:
    """Move a word range to sentence boundaries: the start goes back to
    the first word of the sentence containing ``first``; the end goes
    forward to the last word of the sentence containing ``last`` (or,
    if that would make the clip longer than ``max_seconds``, back to
    the previous sentence end). Unchanged when no sentence boundary
    can be found in the needed direction. (2026-11-10: the LLM ended a
    clip after 'diríamos,' — a comma — cutting the sentence '...con
    muchas experiencias variadas... pero no lograba ser contratado.'
    Word snapping alone cannot catch that; only sentence snapping can.)"""
    end_t = words[last]["e"]
    # Start: back to the word after the previous sentence end, if the
    # clip still fits in max_seconds.
    for i in range(first - 1, -1, -1):
        if _is_sentence_end(words[i]["w"]):
            new_first = i + 1
            if end_t - words[new_first]["s"] <= max_seconds:
                first = new_first
            break
    # End: forward to the next sentence end, preferring one followed by
    # a real pause (the speaker breathes there, so the cut sounds clean).
    # A period with no pause after it (gap ~0, or negative because
    # Whisper word times overlap) sounds cut off mid-thought — seen
    # 2026-11-10: a clip ending at 'exponencial.' (gap -0.42 s) cut
    # into 'Y todo por una razón...' and the viewer heard the next
    # sentence start and stop. If no paused end fits, keep the first
    # sentence end (the audio fade in the render softens it).
    start_t = words[first]["s"]
    first_sent_end = None
    paused_end = None
    for i in range(last, len(words)):
        if not _is_sentence_end(words[i]["w"]):
            continue
        if words[i]["e"] - start_t > max_seconds:
            break
        if first_sent_end is None:
            first_sent_end = i
        gap = (words[i + 1]["s"] - words[i]["e"]) if i + 1 < len(words) else 99.0
        if gap >= PAUSE_SECONDS:
            paused_end = i
            break
    if paused_end is not None:
        last = paused_end
    elif first_sent_end is not None:
        last = first_sent_end
    # If the end is still not a sentence end, fall back to the last
    # sentence end at or before it (only if the clip stays long enough).
    if not _is_sentence_end(words[last]["w"]):
        for i in range(last, first, -1):
            if _is_sentence_end(words[i]["w"]):
                if words[i]["e"] - start_t >= min_seconds:
                    last = i
                break
    if last < first:
        last = first
    return first, last


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
        # Clamp into the transcript, then snap to whole words and then
        # to sentence boundaries (never cut a sentence mid-way).
        start = max(t_start, min(start, t_end))
        end = max(t_start, min(end, t_end))
        snapped = _snap_range(start, end, words)
        if snapped is None:
            continue
        s, e, first, last = snapped
        first, last = _snap_to_sentences(first, last, words,
                                         LLM_MIN_SECONDS, LLM_MAX_SECONDS)
        s, e = words[first]["s"], words[last]["e"]
        # Si la palabra siguiente empieza antes de que termine la última
        # (los tiempos de Whisper se solapan), cortar ahí: si no, el audio
        # y los subtítulos incluyen el inicio de la frase siguiente
        # (visto 2026-11-10: un clip que terminaba en 'contratado.' 60.26
        # incluía 'Hasta' 59.56, primera palabra de la frase siguiente).
        if last + 1 < len(words) and words[last + 1]["s"] < e:
            e = words[last + 1]["s"]
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
    """Topic-aware clip selection with a real LLM (Groq gpt-oss, free tier).

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
