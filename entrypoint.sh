#!/bin/sh
# Clipper entrypoint — starts Cloudflare WARP (wireproxy) if configured,
# then runs the FastAPI app. WARP is optional: if the tunnel cannot start
# (e.g. Render blocks outbound UDP), the app still starts without a proxy
# and video downloads fall back to the direct connection.
#
# PROXY TYPE MATTERS (bug found 2026-11-10): yt-dlp cuts the section with
# ffmpeg, and ffmpeg fetches the remote googlevideo URL ITSELF. ffmpeg
# cannot use a SOCKS5 proxy — yt-dlp even warns "does not support SOCKS
# proxies" and hands ffmpeg HTTP_PROXY=socks5://..., which ffmpeg ignores,
# so the download fails with "ffmpeg exited with code 8" on Render.
# ffmpeg DOES understand an HTTP proxy (HTTP CONNECT). wireproxy exposes
# one too ([http] section), so this script always adds an HTTP proxy
# listener (127.0.0.1:1081) next to the SOCKS5 one (127.0.0.1:1080) and
# prefers it: YTDLP_PROXY=http://127.0.0.1:1081. SOCKS5 is kept only as a
# fallback (video info works, section downloads do not).
set -u

# v214 (sin desplegar): protege la clave WARP nueva.
# Lección 2026-11-22: reintentar el handshake en bucle hace que
# Cloudflare bloquee la clave (pasó 2 veces en un día; la clave vieja
# quedó bloqueada y hubo que regenerarla). Si WARP falló hace poco,
# el entrypoint espera 5 min antes de intentar de nuevo, y solo lo
# intenta una vez por arranque. El marcador vive en /app/data, que es
# volumen persistente en el VPS (./data:/app/data).
COOLDOWN_FILE="/app/data/warp_cooldown_until"

HTTP_PORT_DEFAULT=1081
CONF=""
CONF_SRC=""

if [ -f /etc/secrets/wireproxy.conf ]; then
  CONF_SRC=/etc/secrets/wireproxy.conf
  echo "[entrypoint] WARP config found: Render Secret File /etc/secrets/wireproxy.conf"
elif [ -n "${WARP_PRIVATE_KEY:-}" ] && [ -n "${WARP_PEER_KEY:-}" ]; then
  mkdir -p /app/warp
  CONF_SRC=/app/warp/wireproxy.conf
  cat > "$CONF_SRC" <<EOF
[Interface]
PrivateKey = ${WARP_PRIVATE_KEY}
Address = ${WARP_ADDRESS_V4:-172.16.0.2/32}
EOF
  if [ -n "${WARP_ADDRESS_V6:-}" ]; then
    echo "Address = ${WARP_ADDRESS_V6}" >> "$CONF_SRC"
  fi
  cat >> "$CONF_SRC" <<EOF
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = ${WARP_PEER_KEY}
AllowedIPs = 0.0.0.0/0
AllowedIPs = ::/0
Endpoint = engage.cloudflareclient.com:2408
PersistentKeepalive = 25

[Socks5]
BindAddress = 127.0.0.1:1080
EOF
  chmod 600 "$CONF_SRC"
  echo "[entrypoint] WARP config built from env vars"
fi

if [ -n "$CONF_SRC" ]; then
  # Work on a copy: the Render Secret File may be mounted read-only, and
  # we must never modify the secret in place.
  mkdir -p /app/warp
  cp "$CONF_SRC" /app/warp/wireproxy.conf
  chmod 600 /app/warp/wireproxy.conf
  CONF=/app/warp/wireproxy.conf
  # Add the HTTP proxy listener if the config does not already have one.
  if ! grep -q '^\[http\]' "$CONF"; then
    printf '\n[http]\nBindAddress = 127.0.0.1:%s\n' "$HTTP_PORT_DEFAULT" >> "$CONF"
    echo "[entrypoint] Added [http] proxy 127.0.0.1:$HTTP_PORT_DEFAULT to WARP config"
  fi
  # Port of the [http] listener (1081 unless the config already had one).
  HTTP_PORT=$(grep -A1 '^\[http\]' "$CONF" | grep -i 'BindAddress' | head -1 | sed -E 's/.*:([0-9]+).*/\1/')
  HTTP_PORT=${HTTP_PORT:-$HTTP_PORT_DEFAULT}
fi

if [ -n "$CONF" ]; then
  # Enfriamiento: si el último intento de WARP falló hace <5 min, esperar
  # hasta cumplir los 5 min antes de un solo intento limpio. Evita que un
  # reinicio en bucle (restart: unless-stopped) queme la clave nueva.
  if [ -f "$COOLDOWN_FILE" ]; then
    WAIT_UNTIL=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    case "$WAIT_UNTIL" in ''|*[!0-9]*) WAIT_UNTIL=0 ;; esac
    if [ "$NOW" -lt "$WAIT_UNTIL" ]; then
      WAIT=$((WAIT_UNTIL - NOW))
      echo "[entrypoint] WARP falló hace poco — esperando ${WAIT}s antes de intentar de nuevo (evita que Cloudflare bloquee la clave)."
      sleep "$WAIT"
    fi
  fi
  wireproxy -c "$CONF" &
  WP_PID=$!
  echo "[entrypoint] wireproxy started (pid $WP_PID), waiting for tunnel…"
  SOCKS_OK=0
  i=0
  while [ $i -lt 30 ]; do
    if curl -s --max-time 5 --socks5-hostname 127.0.0.1:1080 https://api.ipify.org >/tmp/warp_ip 2>/dev/null; then
      echo "[entrypoint] WARP OK — exit IP: $(cat /tmp/warp_ip)"
      SOCKS_OK=1
      break
    fi
    i=$((i + 1))
    sleep 2
  done
  HTTP_OK=0
  if [ "$SOCKS_OK" = "1" ]; then
    i=0
    while [ $i -lt 5 ]; do
      if curl -s --max-time 5 -x "http://127.0.0.1:$HTTP_PORT" https://api.ipify.org >/tmp/warp_ip_http 2>/dev/null; then
        echo "[entrypoint] WARP HTTP proxy OK — exit IP: $(cat /tmp/warp_ip_http)"
        HTTP_OK=1
        break
      fi
      i=$((i + 1))
      sleep 2
    done
  fi
  if [ "$HTTP_OK" = "1" ]; then
    export YTDLP_PROXY="http://127.0.0.1:$HTTP_PORT"
    export WARP_STATUS="ok"
    rm -f "$COOLDOWN_FILE" 2>/dev/null || true
  elif [ "$SOCKS_OK" = "1" ]; then
    echo "[entrypoint] WARP HTTP proxy did not come up; falling back to SOCKS5 (video info works, section downloads will fail — ffmpeg cannot use SOCKS5)."
    export YTDLP_PROXY="socks5://127.0.0.1:1080"
    export WARP_STATUS="socks_only"
    rm -f "$COOLDOWN_FILE" 2>/dev/null || true
  else
    echo "[entrypoint] WARP FAILED — tunnel did not come up (likely outbound UDP blocked). Continuing WITHOUT proxy."
    export WARP_STATUS="failed"
    # Marcar el fallo: el próximo arranque esperará 5 min antes de
    # intentar de nuevo, para no quemar la clave con reintentos.
    mkdir -p "$(dirname "$COOLDOWN_FILE")" 2>/dev/null || true
    echo $(( $(date +%s) + 300 )) > "$COOLDOWN_FILE" 2>/dev/null || true
    kill $WP_PID 2>/dev/null || true
  fi
else
  echo "[entrypoint] No WARP config — running without proxy."
  export WARP_STATUS="not_configured"
fi

# PO Token server (bgutil, v219, 2026-11-23): YouTube exige
# Proof-of-Origin en IPs de datacenter y sin él _raw_info cae con
# bot-block. El servidor genera el token en 127.0.0.1:4416 y el plugin
# de yt-dlp lo usa solo (sin él, yt-dlp sigue sin PO Token pero la app
# arranca igual). No es crítico: si no levanta, solo se pierde el token.
if [ -f /opt/bgutil/server/build/main.js ]; then
  node /opt/bgutil/server/build/main.js --port 4416 &
  POT_PID=$!
  echo "[entrypoint] bgutil PO Token server started (pid $POT_PID, port 4416)"
  i=0
  while [ $i -lt 8 ]; do
    if curl -s --max-time 3 http://127.0.0.1:4416/ping >/dev/null 2>&1; then
      echo "[entrypoint] bgutil PO Token server OK (port 4416)"
      break
    fi
    i=$((i + 1))
    sleep 2
  done
  if [ $i -ge 8 ]; then
    echo "[entrypoint] bgutil PO Token server did not answer /ping — continuing without PO Token."
  fi
else
  echo "[entrypoint] bgutil server not found — PO Token unavailable."
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
