"""Clip candidates from a transcript.

Two selectors, same output shape (start/end/duration/text/words/score,
times RELATIVE to the downloaded section; main.py converts them to
absolute original-video times by adding the section start):

1. Heuristic (v1, ``build_candidates``): deliberately simple and
   explainable (no invented virality) — windows of 20-45 s that start/end
   on a speech segment boundary, scored by words-per-minute, exclamations
   and laughter. It does NOT understand topics, so it can cut a sentence
   in half; the user picks the window in the frontend.
2. LLM (v2, ``build_candidates_llm``): a real model (Gemini Flash por
   defecto desde 2026-11-18, Groq como respaldo; el admin elige en
   Configuración) reads the whole transcript and picks up to 5 clips, each covering
   ONE complete topic/idea from its first sentence to its last. The code
   then snaps every cut to word boundaries, so a clip can never start or
   end mid-word. Any LLM failure (no key, 401/429/timeout, bad JSON, zero
   valid clips) returns None and the caller falls back to the heuristic —
   a failed LLM must never fail a clips job.

The user picks the window in the frontend; nothing is auto-published.
"""
import concurrent.futures
import json
import logging
import math
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def build_candidates(segments: list[dict], min_len: float = 20.0,
                     max_len: float = 45.0,
                     max_gap: float = 4.0) -> list[dict]:
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
        # v210: margen final (igual que en la ruta LLM): 0.5 s de respiro
        # tras la última palabra, sin entrar en la palabra siguiente, sin
        # pasar del final real del video (última palabra de la
        # transcripción) y sin superar la duración máxima pedida.
        _midx = _idx_of(win_words[-1])
        if 0 <= _midx < len(words) - 1:
            clip_end = min(clip_end + 0.5, words[_midx + 1]["s"],
                           words[-1]["e"], start + max_len)
        else:
            clip_end = min(clip_end + 0.5, words[-1]["e"], start + max_len)
        if clip_end - start < min_len:
            continue
        text = " ".join(w["w"].strip() for w in win_words).strip()
        duration = clip_end - start
        wpm = len(win_words) / max(duration / 60.0, 0.01)
        # Largest internal gap — a long silence makes a bad clip.
        # (v159: el límite se puede relajar como último recurso para
        # videos con pausas largas — música, reacciones — y no devolver 0.)
        gaps = [b["s"] - a["e"] for a, b in zip(win_words, win_words[1:])]
        max_gap_found = max(gaps) if gaps else 0.0
        if max_gap_found > max_gap:
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
LLM_MAX_TRANSCRIPT_CHARS = 16000
# Partir la transcripción en partes con cruce (v137, Juan: analizar TODO
# el video sin perder ningún momento, aunque el viral esté al final).
# Cada parte se analiza EN PARALELO con la IA (mismo tiempo que una
# sola); el cruce (final de una parte + inicio de la siguiente) evita
# que una historia se corte en el límite entre dos partes.
LLM_CHUNK_SECONDS = 600.0  # 10 min por parte (v139: 20 min daba ~8700 tokens y Groq 413)
LLM_CHUNK_OVERLAP_SECONDS = 120.0  # 2 min de cruce entre partes
LLM_CHUNK_MAX_PARTS = 8  # tope de partes (videos larguísimos)


def _flatten_words(segments: list[dict]) -> list[dict]:
    words = []
    for s in segments:
        for w in s.get("words", []):
            words.append({"w": w["w"], "s": w["s"], "e": w["e"]})
    return words


def _sample_transcript(segments: list[dict], budget: int) -> str:
    """Reparte `budget` caracteres por todo el video.

    Divide los segmentos en bloques de tiempo y toma de cada bloque un
    trozo contiguo (líneas completas, en orden), para que la IA vea
    ideas completas del inicio, el medio y el final, no solo el
    principio. Devuelve el texto ya unido por líneas.
    """
    if not segments:
        return ""
    starts = [float(s.get("start", 0) or 0) for s in segments]
    ends = [float(s.get("end", 0) or 0) for s in segments]
    t0, t1 = min(starts), max(ends)
    if t1 <= t0:
        return "\n".join(
            f"[{float(s.get('start', 0)):.1f}-{float(s.get('end', 0)):.1f}] "
            f"{(s.get('text') or '').strip()}" for s in segments)[:budget]
    nblocks = 8
    per_block = max(1, budget // nblocks)
    span = (t1 - t0) / nblocks
    kept: list[str] = []
    for b in range(nblocks):
        b0 = t0 + b * span
        b1 = t0 + (b + 1) * span if b < nblocks - 1 else t1 + 1
        used = 0
        for s in segments:
            st = float(s.get("start", 0) or 0)
            if st < b0 or st >= b1:
                continue
            text = (s.get("text") or "").strip()
            if not text:
                continue
            line = (f"[{float(s.get('start', 0)):.1f}-"
                    f"{float(s.get('end', 0)):.1f}] {text}")
            if used + len(line) + 1 > per_block and kept:
                break
            kept.append(line)
            used += len(line) + 1
    out = "\n".join(kept)
    return out[:budget].rsplit("\n", 1)[0] if len(out) > budget else out


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
        # Antes se cortaba a los primeros N caracteres: la IA solo veía
        # el principio del video y los clips salían de los primeros
        # minutos. Ahora se reparte el presupuesto por TODO el video:
        # se divide en bloques de tiempo y de cada bloque se toma un
        # trozo contiguo (para que la IA vea ideas completas del inicio,
        # el medio y el final).
        transcript = _sample_transcript(segments, LLM_MAX_TRANSCRIPT_CHARS)
    return (
        "Eres un editor de clips virales para TikTok, Reels y Shorts. "
        "Te doy la transcripción de un video con marcas de tiempo en segundos.\n"
        f"Devuelve hasta {max_clips} clips, cada uno sobre UN solo tema o idea completa.\n"
        "QUÉ BUSCAR — elige SOLO momentos que puedan hacerse virales, "
        "de interés real, de risa o exclusivos:\n"
        "- Algo gracioso: un chiste, una burla, una reacción, un fail, un momento incómodo.\n"
        "- Algo sorprendente o impactante: un giro inesperado, una revelación, un dato que nadie espera.\n"
        "- Algo polémico o una opinión fuerte: una crítica, un debate, algo que genera discusión.\n"
        "- Algo exclusivo: una confesión, un secreto, información que no se escucha en otro lado.\n"
        "- Algo emotivo: una historia personal, una reacción genuina, un momento tenso.\n"
        "- Una frase que la gente querría compartir o comentar.\n"
        "QUÉ EVITAR — NUNCA elijas estos momentos, aunque parezcan un tema completo:\n"
        "- La INTRODUCCIÓN del video: saludos, bienvenidas, presentación del canal o del video, "
        "'hoy vamos a hablar de...', 'en este video...'. Los primeros 60 segundos casi siempre son intro.\n"
        "- El FINAL del video: despedidas, 'eso fue todo', 'nos vemos en el próximo'.\n"
        "- Llamados a la acción: 'suscríbete', 'dale like', 'comenta', 'sígueme'.\n"
        "- Relleno y transiciones: 'bueno, entonces...', 'como les decía...', pausas largas, muletillas.\n"
        "- Explicaciones aburridas o contexto sin punch: si no tiene gracia, sorpresa, polémica o exclusividad, NO lo elijas.\n"
        "- Si un momento no es claramente gracioso, sorprendente, polémico, exclusivo o emotivo, NO lo incluyas. "
        "Mejor devolver menos clips que rellenar con trozos al azar.\n"
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
        '"reason": "por qué este momento engancha: gracioso/sorprendente/polémico/exclusivo/emotivo"}, ...]}\n'
        '- "start" y "end" son segundos (un decimal) relativos al inicio del video.\n'
        '- "reason" debe decir la cualidad viral concreta (gracioso, sorprendente, polémico, exclusivo o emotivo) y por qué.\n'
        '- Si no hay ningún momento claro, devuelve {"clips": []}.\n\n'
        "Transcripción:\n" + transcript
    )


def _groq_chat(prompt: str) -> str | None:
    """One Groq chat completion. Tries gpt-oss-120b first, then gpt-oss-20b.
    Returns the raw content string, or None on any failure."""
    key = _llm_key("groq")
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
            # Structured Outputs con esquema ESTRICTO (2026-11-11): antes
            # era json_object (solo "JSON válido", sin garantía de forma)
            # y a veces Groq devolvía 400 json_validate_failed. Con
            # strict:true la generación está restringida al esquema, así
            # que la respuesta SIEMPRE trae {clips:[{start,end,title,
            # reason}]} en el formato exacto. En modo estricto todos los
            # campos deben ser required y todo objeto additionalProperties
            # false. Funciona en gpt-oss-120b y gpt-oss-20b (los dos
            # modelos de este selector).
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "clips",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "clips": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "start": {"type": "number"},
                                        "end": {"type": "number"},
                                        "title": {"type": "string"},
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["start", "end",
                                                 "title", "reason"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["clips"],
                        "additionalProperties": False,
                    },
                },
            },
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            GROQ_CHAT_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     # Groq (Cloudflare) 403s Python-urllib's default UA.
                     "User-Agent": "Clipzio Pro/1.5.0"})
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


# --- LLM selection via Gemini (gemini-2.5-flash) ---------------------------
# 2026-11-18, Juan: cambiar a Gemini Flash, que elige mejor. Casi gratis
# (~$0.006 por video de 1 h). Misma interfaz que _groq_chat: devuelve el
# JSON crudo o None. Salida estructurada con responseMimeType +
# responseSchema (el equivalente de Gemini al strict JSON de Groq). OJO:
# el esquema de Gemini es un subconjunto de JSON Schema y NO acepta
# additionalProperties, así que este esquema va sin ellos.
GEMINI_CHAT_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   "gemini-2.5-flash:generateContent")
GEMINI_LLM_TIMEOUT = 90  # seconds per request
_GEMINI_CLIPS_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "title", "reason"],
            },
        },
    },
    "required": ["clips"],
}


def _gemini_chat(prompt: str) -> str | None:
    """One Gemini completion (gemini-2.5-flash).
    Returns the raw JSON string, or None on any failure."""
    key = _llm_key("gemini")
    if not key:
        return None
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text":
            "Eres un editor de clips virales. Respondes SOLO con JSON válido."}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4000,
            "responseMimeType": "application/json",
            "responseSchema": _GEMINI_CLIPS_SCHEMA,
        },
    }, ensure_ascii=False).encode("utf-8")
    # La clave de Gemini solo tiene [A-Za-z0-9_-], segura en la URL.
    req = urllib.request.Request(
        GEMINI_CHAT_URL + "?key=" + key, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "Clipzio Pro/1.5.0"})
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_LLM_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        logger.warning("Gemini LLM failed (HTTP %s %s)", e.code, detail)
        return None
    except Exception as e:  # noqa: BLE001 — timeout / DNS / network, any
        logger.warning("Gemini LLM failed (%s)", e)
        return None
    if isinstance(payload, dict) and payload.get("error"):
        logger.warning("Gemini LLM returned an error payload: %s",
                       str(payload["error"])[:200])
        return None
    try:
        return str(payload["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:  # noqa: BLE001 — unexpected response shape
        logger.warning("Gemini LLM response could not be read (%s)", e)
        return None


# --- Proveedor de IA (configurable desde el Panel Admin) -------------------
# El admin elige "groq" o "gemini" en Configuración; las claves se leen
# de aquí (ajustes) y si están vacías, del .env. Si el proveedor elegido
# falla o no tiene clave, se prueba el otro; si ninguno funciona, el
# selector cae al heurístico (nunca tumba el trabajo).
LLM_PROVIDER = "gemini"
_LLM_KEYS = {"groq": None, "gemini": None}


def configure_llm(provider=None, groq_key=None, gemini_key=None):
    """Aplica la configuración de IA elegida por el admin (o del arranque)."""
    global LLM_PROVIDER
    if provider in ("groq", "gemini"):
        LLM_PROVIDER = provider
    if groq_key is not None:
        _LLM_KEYS["groq"] = groq_key or None
    if gemini_key is not None:
        _LLM_KEYS["gemini"] = gemini_key or None


def _llm_key(provider: str) -> str | None:
    key = _LLM_KEYS.get(provider)
    if key:
        return key
    return os.environ.get("GROQ_API_KEY" if provider == "groq"
                          else "GEMINI_API_KEY")


def _llm_chat(prompt: str) -> str | None:
    """Una llamada a la IA que elige los clips, con el proveedor que el
    admin configuró (primero) y el otro como respaldo."""
    order = [LLM_PROVIDER,
             "gemini" if LLM_PROVIDER == "groq" else "groq"]
    for provider in order:
        fn = _groq_chat if provider == "groq" else _gemini_chat
        content = fn(prompt)
        if content is not None:
            return content
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
                        t_start: float, t_end: float,
                        abs_start: float = 0.0,
                        min_len: float = LLM_MIN_SECONDS,
                        max_len: float = LLM_MAX_SECONDS) -> list[dict]:
    """Validate LLM clips hard and snap them to word boundaries. Drops:
    non-numeric or missing times, ranges outside the transcript, clips
    outside the requested duration (a little looser: up to 5 s shorter
    than min_len / 5 s longer than max_len, so a good clip is not thrown
    away for being a couple of seconds off), clips with fewer than 8
    words, clips that start inside the video intro (absolute start <
    15 s — greetings/channel bumper, never viral), and any clip
    overlapping one already kept (LLM order wins: better-ranked clips
    are kept first). Returns the kept clips in LLM order with snapped
    times. (v139: antes usaba 15-60 s fijos aunque se pidieran 60-90 s,
    así la validación tiraba casi todos los clips de la IA.)"""
    v_min = max(8.0, min_len - 5.0)
    v_max = max_len + 5.0
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
        # Intro del video: los primeros 15 s absolutos son saludo/bumper,
        # nunca un momento viral. Solo aplica si el rango analizado
        # empieza en el inicio del video (abs_start ≈ 0).
        if abs_start + s < 15.0:
            continue
        first, last = _snap_to_sentences(first, last, words,
                                         v_min, v_max)
        s, e = words[first]["s"], words[last]["e"]
        # v209: margen final — que el clip no se corte justo al terminar
        # la última palabra. Se deja un respiro de hasta 0.5 s (la
        # pausa natural del hablante), sin entrar en la palabra
        # siguiente, sin pasar del final de la transcripción y sin
        # superar la duración máxima. La palabra queda completa y el
        # corte suena limpio en vez de abrupto.
        _margin = 0.5
        if last + 1 < len(words):
            e = min(e + _margin, words[last + 1]["s"], t_end)
        else:
            e = min(e + _margin, t_end)
        e = min(e, s + v_max)
        # Si la palabra siguiente empieza antes de que termine la última
        # (los tiempos de Whisper se solapan), cortar ahí: si no, el audio
        # y los subtítulos incluyen el inicio de la frase siguiente
        # (visto 2026-11-10: un clip que terminaba en 'contratado.' 60.26
        # incluía 'Hasta' 59.56, primera palabra de la frase siguiente).
        if last + 1 < len(words) and words[last + 1]["s"] < e:
            e = words[last + 1]["s"]
        duration = e - s
        if duration < v_min or duration > v_max:
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


def _llm_chunk_ranges(t_start: float, t_end: float) -> list[tuple]:
    """Rangos de tiempo de las partes, con cruce (v137).

    Cada parte cubre LLM_CHUNK_SECONDS; la siguiente empieza
    LLM_CHUNK_OVERLAP_SECONDS antes de que termine la anterior, así el
    final de una parte y el inicio de la siguiente se analizan juntos y
    ninguna historia se corta en el límite. La última parte llega hasta
    el final del video. Si el video es larguísimo, las partes se
    agrandan para no pasar de LLM_CHUNK_MAX_PARTS.
    """
    duration = t_end - t_start
    if duration <= 0:
        return []
    chunk = LLM_CHUNK_SECONDS
    overlap = LLM_CHUNK_OVERLAP_SECONDS
    step = chunk - overlap
    n = max(1, math.ceil((duration - overlap) / step))
    if n > LLM_CHUNK_MAX_PARTS:
        chunk = (duration + (LLM_CHUNK_MAX_PARTS - 1) * overlap) / LLM_CHUNK_MAX_PARTS
        step = chunk - overlap
        n = LLM_CHUNK_MAX_PARTS
    ranges = []
    for i in range(n):
        r0 = t_start + i * step
        r1 = t_end if i == n - 1 else min(t_end, r0 + chunk)
        ranges.append((r0, r1))
    return ranges


def _llm_chunk_candidates(chunk_segments: list[dict], max_clips: int,
                          min_len: float, max_len: float,
                          abs_start: float) -> list[dict] | None:
    """Una llamada a la IA para UNA parte de la transcripción.

    Misma lógica que el selector de una sola llamada (prompt, Groq,
    parseo, validación, puntuación 95/90/85... en orden de la IA),
    pero solo sobre los segmentos de esta parte. Devuelve None si la
    parte falla (la parte no aporta nada; las demás siguen).
    """
    if not chunk_segments:
        return None
    words = _flatten_words(chunk_segments)
    if len(words) < 8:
        return None
    t_start = min(float(s.get("start", 0) or 0) for s in chunk_segments)
    t_end = max(float(s.get("end", 0) or 0) for s in chunk_segments)
    if t_end <= t_start:
        return None
    prompt = _llm_prompt(chunk_segments, max_clips, min_len, max_len)
    content = _llm_chat(prompt)
    if content is None:
        return None
    items = _parse_llm_clips(content)
    if items is None:
        return None
    valid = _validate_llm_clips(items, words, t_start, t_end,
                                abs_start=abs_start,
                                min_len=min_len, max_len=max_len)
    if not valid:
        return None
    candidates = []
    for i, c in enumerate(valid[:max(1, max_clips)]):
        candidates.append({**c, "score": round(95 - 5 * i, 1)})
    candidates.sort(key=lambda c: c["start"])
    return candidates


def _clips_overlap(a: dict, b: dict) -> bool:
    """True si dos candidatos son el mismo momento (v137).

    El mismo momento puede salir en dos partes por el cruce; se
    detecta por inicio casi igual o por solape de más de la mitad del
    clip más corto.
    """
    if abs(a["start"] - b["start"]) <= 10.0:
        return True
    lo = max(a["start"], b["start"])
    hi = min(a["end"], b["end"])
    if hi <= lo:
        return False
    shorter = min(a["end"] - a["start"], b["end"] - b["start"])
    return (hi - lo) > 0.5 * shorter


def build_candidates_llm(segments: list[dict], max_clips: int = LLM_MAX_CLIPS,
                         min_len: float = 20.0,
                         max_len: float = LLM_MAX_SECONDS,
                         abs_start: float = 0.0) -> list[dict] | None:
    """Topic-aware clip selection with a real LLM (provider configurable:
    Gemini Flash por defecto desde 2026-11-18, Groq como respaldo).

    Returns candidates in the EXACT shape the frontend already uses
    (start/end/duration/text/words/score, plus the LLM's title/reason),
    score 95/90/85... in LLM order, sorted by start. Returns None when
    the LLM was not attempted (no key for either provider), failed, or
    produced zero valid clips — the caller must then fall back to
    ``build_candidates``.
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
    # v137: partir TODO el video en partes con cruce y analizar cada
    # parte EN PARALELO (mismo tiempo que una sola llamada). Cada parte
    # recibe toda la atención de la IA, así ningún momento se pierde,
    # esté al principio, en el medio o al final.
    ranges = _llm_chunk_ranges(t_start, t_end)
    chunks = []
    for (r0, r1) in ranges:
        part = [s for s in segments
                if r0 <= float(s.get("start", 0) or 0) < r1
                or (r0 <= float(s.get("end", 0) or 0) < r1)]
        if part:
            chunks.append(part)
    if not chunks:
        return None
    # Cada parte se analiza en paralelo; se etiqueta cada candidato
    # con su parte para el reparto final.
    per_part: list[list[dict]] = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(chunks)) as ex:
        futures = [ex.submit(_llm_chunk_candidates, part, max_clips,
                             min_len, max_len, abs_start)
                   for part in chunks]
        for idx, f in enumerate(futures):
            try:
                cands = f.result()
            except Exception as e:  # noqa: BLE001 — una parte no tumba el resto
                logger.warning("Groq LLM chunk failed (%s)", e)
                cands = None
            if cands:
                for c in cands:
                    c["_part"] = idx
                per_part.append(cands)
    if not per_part:
        logger.warning("Groq LLM returned no valid clips; "
                       "falling back to heuristic")
        return None
    # Quitar duplicados (mismo momento en dos partes por el cruce):
    # se queda el de mayor puntuación.
    all_candidates: list[dict] = [c for lst in per_part for c in lst]
    all_candidates.sort(key=lambda c: (-c["score"], c["start"]))
    unique: list[dict] = []
    for c in all_candidates:
        if all(not _clips_overlap(c, p) for p in unique):
            unique.append(c)
    # Reparto por partes (round-robin): el mejor de cada parte, luego
    # el segundo mejor de cada parte, etc. Así TODO el video queda
    # representado — incluido el final — y ningún momento se pierde por
    # estar en una parte tardía. Si hay más partes que clips pedidos,
    # se eligen las partes repartidas por todo el video (primera y
    # última incluidas).
    by_part: dict[int, list[dict]] = {}
    for c in unique:
        by_part.setdefault(c["_part"], []).append(c)
    for lst in by_part.values():
        lst.sort(key=lambda c: (-c["score"], c["start"]))
    part_ids = sorted(by_part.keys())
    n_want = max(1, max_clips)
    if len(part_ids) > n_want:
        if n_want == 1:
            chosen_ids = [part_ids[-1]]
        else:
            chosen_ids = sorted({round(i * (len(part_ids) - 1) / (n_want - 1))
                                 for i in range(n_want)})
            chosen_ids = [part_ids[i] for i in chosen_ids]
    else:
        chosen_ids = part_ids
    picked: list[dict] = []
    rnd = 0
    while len(picked) < n_want:
        added = False
        for pid in chosen_ids:
            lst = by_part.get(pid, [])
            if rnd < len(lst):
                cand = lst[rnd]
                if cand not in picked:
                    picked.append(cand)
                    added = True
                    if len(picked) >= n_want:
                        break
        if not added:
            break
        rnd += 1
    if len(picked) < n_want:
        rest = [c for c in unique if c not in picked]
        rest.sort(key=lambda c: (-c["score"], c["start"]))
        for c in rest:
            if len(picked) >= n_want:
                break
            picked.append(c)
    for c in picked:
        c.pop("_part", None)
    picked.sort(key=lambda c: c["start"])
    return picked
