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
| POST | `/api/auth/register` | `{name, email, password}` → usuario + token JWT |
| POST | `/api/auth/login` | `{email, password}` → token JWT |
| GET | `/api/auth/me` | Con token `Authorization: Bearer <token>` → usuario actual |
| PATCH | `/api/auth/me` | Nombre, `brand_text`, `frame_enabled` |
| GET | `/api/me/stats`, `/api/me/clips` | Mis contadores y mis clips |
| GET | `/api/plans` | Planes activos (público) |
| POST/PUT/DELETE | `/api/admin/plans` | CRUD de planes (solo admin) |
| GET | `/api/payment-methods` | Métodos activos (usuario logueado) |
| POST/PUT/DELETE | `/api/admin/payment-methods` | CRUD de métodos (solo admin) |
| POST | `/api/subscriptions` | Compra: form multipart `plan_id`, `payment_method_id`, `receipt` (imagen/PDF ≤ 5 MB) → solicitud Pendiente |
| GET | `/api/subscriptions/mine` | Mis solicitudes + suscripción activa |
| GET | `/api/admin/subscriptions` | Todas las solicitudes (solo admin) |
| POST | `/api/admin/subscriptions/{id}/approve` | Aprobar → crea suscripción +30 días y desbloquea al usuario |
| POST | `/api/admin/subscriptions/{id}/reject` | Rechazar (nota opcional) |
| GET | `/api/admin/receipts/{file}` | Ver el comprobante (solo admin) |
| GET | `/api/admin/users`, `/api/admin/clips` | Usuarios y clips (solo admin) |
| POST | `/api/admin/clips/{id}/approve` | Aprobar clip → habilita descarga |
| POST | `/api/admin/clips/{id}/reject` | Rechazar clip |
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

## Base de datos y usuarios

Usuarios con PostgreSQL en producción y SQLite local:

- **En Render:** el Blueprint (`render.yaml`) crea una base `clipper-db`
  (plan free: 90 días, 1 GB) y la conecta como `DATABASE_URL`, más un
  `JWT_SECRET` generado. Las tablas se crean solas al arrancar (`init_db`).
- **En local:** sin `DATABASE_URL` la app usa SQLite `data/clipper.db`
  (no se sube al repo). Para probar con Postgres local, pon
  `DATABASE_URL=postgresql+psycopg://usuario:clave@localhost:5432/clipper`.
- Claves con **bcrypt** (nunca se guardan en texto plano), sesiones **JWT**
  de 7 días. En v1 los endpoints de clips/render siguen públicos, pero si
  el cliente manda su token, el `user_id` queda registrado en el job.
- **Mi cuenta** (arriba a la derecha en la web): registro, login, logout;
  el token se guarda en `localStorage`.

### Migrar la base gratis antes de que caduque (sin pagar)

La base free de Render caduca a los 90 días. Antes de esa fecha, exporta
y restaura en un destino gratis sin caducidad (Supabase o Neon, ambos
PostgreSQL gratis) o en una base nueva de Render:

```bash
# 1. Exportar desde Render (la URL externa está en el dashboard de la base)
pg_dump "postgresql://clipper:CLAVE@HOST:5432/clipper?sslmode=require" > clipper-backup.sql
# 2. Crear la base destino (Supabase/Neon) y restaurar
psql "postgresql://usuario:CLAVE@HOST_DESTINO:5432/postgres?sslmode=require" < clipper-backup.sql
# 3. Cambiar DATABASE_URL en Render (Environment) a la nueva URL y redeploy
```

Los datos que se migran son solo la tabla `users` (los videos/clips son
archivos en `data/`, que en Render free también son efímeros — ver
"Limitaciones honestas").

## Planes y pagos (flujo manual, sin pasarela)

El usuario elige un plan en la sección **Planes**, elige un método
bancario, ve los datos (banco, cuenta, titular, instrucciones), sube su
comprobante (imagen o PDF, máx. 5 MB, guardado en `data/receipts/`) y la
solicitud queda **Pendiente**. El admin la aprueba o rechaza en el
**Panel Admin → Solicitudes de pago** (puede ver el comprobante). Al
aprobar se crea la suscripción (+30 días según el plan) y el usuario queda
desbloqueado; al rechazar sigue sin plan.

Planes seed (al arrancar, si no hay planes): **Full RD$ 3,500/30 días**
(más popular), **Pro RD$ 2,000/30 días**, **Básico RD$ 1,000/30 días**.
Métodos de pago: **no se siembra ninguna cuenta real**; solo un método de
ejemplo **inactivo** ("Ejemplo — Banco") para mostrar la forma. El admin
crea los métodos reales desde el Panel Admin (también puede crear, editar
y eliminar planes).

**Bloqueo sin plan:** sin suscripción activa, Inicio, Mis Clips, Crear
Clip y Mi Cuenta salen bloqueadas con "Debes contratar un plan para usar
el clipper" y un botón a Planes. Solo Planes y Mi Cuenta quedan
accesibles. En el backend, `POST /api/clips` y `POST /api/render`
devuelven **403** si el usuario **logueado** no tiene plan activo.
**Decisión v1:** sin token los endpoints siguen públicos (para no romper
el flujo actual del creador); el bloqueo total por login se hará cuando el
creador exija cuenta.

## Probar el flujo completo de planes/pagos

```bash
# 1. Admin: crear un método de pago
TOKEN_ADMIN=$(curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@marketingcaribe.com","password":"Admin1234!"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -X POST localhost:8000/api/admin/payment-methods -H "Authorization: Bearer $TOKEN_ADMIN" -H 'Content-Type: application/json' -d '{"name":"Transferencia Banco Popular","bank":"Banco Popular","account_number":"123-456789-0","account_holder":"MarketingCaribe SRL","account_type":"Corriente","instructions":"Pon tu nombre en el concepto","is_active":true}'
# 2. Usuario normal: ver planes y comprar con comprobante
TOKEN_USER=$(curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' -d '{"email":"usuario@marketingcaribe.com","password":"Usuario1234!"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl localhost:8000/api/plans
curl -X POST localhost:8000/api/subscriptions -H "Authorization: Bearer $TOKEN_USER" -F plan_id=1 -F payment_method_id=1 -F receipt=@comprobante.jpg
# 3. Sin plan, clips da 403 con token:
curl -X POST localhost:8000/api/clips -H "Authorization: Bearer $TOKEN_USER" -H 'Content-Type: application/json' -d '{"url":"https://www.youtube.com/watch?v=lvPIofmDIGk","start":50,"end":80}'
# 4. Admin: ver y aprobar la solicitud (id 1)
curl localhost:8000/api/admin/subscriptions -H "Authorization: Bearer $TOKEN_ADMIN"
curl -X POST localhost:8000/api/admin/subscriptions/1/approve -H "Authorization: Bearer $TOKEN_ADMIN" -H 'Content-Type: application/json' -d '{}'
# 5. Usuario desbloqueado: secciones visibles y /api/clips ya no da 403
curl localhost:8000/api/subscriptions/mine -H "Authorization: Bearer $TOKEN_USER"
```

## Limitaciones honestas

- **Transcripción imperfecta:** Whisper local (`base`) se equivoca con slang dominicano rápido y voces que se montan (probado en el clip de Bulin: algunas palabras salen mal). Por eso los subtítulos son **editables antes de exportar** (campo de texto en el paso 3 → `edited_words`). Un modelo mayor (`small`) o Whisper por API mejora la precisión.
- **Sin captions de YouTube:** el video de ejemplo no tiene subtítulos automáticos; siempre se transcribe localmente.
- **Videos privados / solo miembros / con restricción de edad** no se pueden leer solo con pegar el link.
- **Live en curso:** se puede procesar cuando termina (queda como VOD); capturar en vivo requiere grabar el stream mientras transmite (no en v1).
- **v1 tenía:** cuentas de usuario, pagos, cola con Redis, ni seguimiento de cara (el recorte 9:16 es centrado fijo). Usuarios ya existen (registro/login JWT + PostgreSQL, ver arriba) y pagos manuales con planes también (ver "Planes y pagos"); falta: cola con Redis, seguimiento de cara.

## Falta para producción

- Límites por usuario/plan (hoy el plan desbloquea todo, sin contar videos/clips).
- Pasarela de pago automática (hoy es manual con comprobante) y almacenamiento/limpieza de archivos viejos.
- Cola real (Redis + worker) y escalado: hoy los jobs son en proceso, máx. 2 a la vez.
- Seguimiento de cara para el recorte 9:16 (hoy es centro fijo).
- Publicación directa a TikTok / Instagram / YouTube Shorts (hoy es descarga de MP4).
- Moderación / derechos: solo usar videos propios o con permiso.
