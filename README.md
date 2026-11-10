# Clipper — MarketingCaribe

Plataforma para sacar clips verticales de videos y lives de YouTube, **sin API de YouTube**: pegas el link, el sistema descarga el rango, lo transcribe con Whisper local, propone clips y renderiza en **9:16 completo (1080×1920)** para Reels / TikTok / Shorts.

Estilo aprobado por Juan el 2026-11-10 (clip de prueba Bulin 47 / Alofoke):
marco amarillo `#FADE4C`, nombre `@marketingcaribe` arriba, subtítulos virales
blancos en negrita, 3 palabras a la vez, palabra en amarillo.

## Configuración del marco (nuevo)

En **Configuración** hay:

| Control | Qué hace |
|---|---|
| Interruptor **Con marco / Sin marco** | `frame_enabled` (por defecto `true`) |
| Campo **Nombre que saldrá en el marco** | `brand_text` editable, por defecto `@marketingcaribe` |

- **Con marco (`frame_enabled=true`)**: se dibuja el marco amarillo y el nombre arriba, como en el clip de Bulin aprobado.
- **Sin marco (`frame_enabled=false`)**: **no** se dibuja ni el marco ni el nombre. El video sale limpio (los subtítulos siguen, si están activados).
- Si el nombre queda vacío, se mantiene el marco pero sin nombre.
- El cambio se ve en la **vista previa en vivo** antes de exportar: al apagar el interruptor desaparecen el borde amarillo y el nombre, y los campos de color/grosor/nombre se desactivan.

El endpoint `POST /api/render` acepta `frame_enabled: boolean` (defecto `true`) y `brand_text: string` (defecto `@marketingcaribe`).

## Cómo correrlo local

```bash
cd clipper-platform
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
# Abrir http://127.0.0.1:8000
```

> La primera transcripción descarga el modelo Whisper (~150 MB para `base`).
> En CPU tarda: un rango de 60 s se transcribe en ~30–60 s.

## Con Docker

```bash
docker compose up --build
# http://localhost:8000
```

## Despliegue

| Opción | Cómo | Costo aprox. |
|---|---|---|
| **Railway** | Conecta el repo, `railway up`. El `Dockerfile` ya está. | ~$5–20/mes según uso |
| **Render** | Web Service → Docker, plan Starter con 2 GB RAM. | ~$7/mes + disco |
| **VPS** | `docker compose up -d` en Ubuntu 22.04/24.04, con Nginx + HTTPS. | ~$5–10/mes |

Nada de esto se ha publicado todavía: este repo es local, no sube nada a ningún servidor.

## API

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/health` | Salud |
| POST | `/api/video-info` | `{url}` → título, autor, duración (yt-dlp, sin API) |
| POST | `/api/clips` | `{url, start, end, model}` → job: descarga el rango, transcribe, devuelve candidatos con `start/end/text/score` |
| POST | `/api/render` | `{job_id\|url, start, end, frame_enabled, brand_text, frame_color, frame_width, title_text, show_subtitles, subtitle_style, subtitle_size, subtitle_position, edited_words}` → job de render |
| GET | `/api/jobs/{id}` | Estado del job |
| GET | `/api/download/{file}` | MP4 renderizado |

Ejemplo (mismo clip de prueba de Bulin, 0:50–1:17 del original):

```bash
curl -X POST localhost:8000/api/video-info -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=lvPIofmDIGk"}'
curl -X POST localhost:8000/api/clips -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=lvPIofmDIGk","start":20,"end":80,"model":"base"}'
# …esperar el job… luego:
curl -X POST localhost:8000/api/render -H 'Content-Type: application/json' \
  -d '{"job_id":"<id>","start":50,"end":77,"frame_enabled":true,"brand_text":"@marketingcaribe"}'
# Sin marco:
curl -X POST localhost:8000/api/render -H 'Content-Type: application/json' \
  -d '{"job_id":"<id>","start":50,"end":77,"frame_enabled":false}'
```

## Limitaciones honestas

- **Transcripción imperfecta:** Whisper local (`base`) se equivoca con slang dominicano rápido y voces que se montan (probado en el clip de Bulin: algunas palabras salen mal). Por eso los subtítulos son **editables antes de exportar** (campo de texto en el paso 3 → `edited_words`). Un modelo mayor (`small`) o Whisper por API mejora la precisión.
- **Sin captions de YouTube:** el video de ejemplo no tiene subtítulos automáticos; siempre se transcribe localmente.
- **Videos privados / solo miembros / con restricción de edad** no se pueden leer solo con pegar el link.
- **Live en curso:** se puede procesar cuando termina (queda como VOD); capturar en vivo requiere grabar el stream mientras transmite (no en v1).
- **v1 no tiene:** cuentas de usuario, pagos, cola con Redis, ni seguimiento de cara (el recorte 9:16 es centrado fijo). Ver “Falta para producción” abajo.

## Falta para producción

- Autenticación (cuentas, login) y límites por usuario/plan.
- Pagos (suscripción) y almacenamiento/limpieza de archivos viejos.
- Cola real (Redis + worker) y escalado: hoy los jobs son en proceso, máx. 2 a la vez.
- Seguimiento de cara para el recorte 9:16 (hoy es centro fijo).
- Publicación directa a TikTok / Instagram / YouTube Shorts (hoy es descarga de MP4).
- Moderación / derechos: solo usar videos propios o con permiso.
