#!/bin/sh
# Clipper entrypoint — starts Cloudflare WARP (wireproxy) if configured,
# then runs the FastAPI app. WARP is optional: if the tunnel cannot start
# (e.g. Render blocks outbound UDP), the app still starts without a proxy
# and video downloads fall back to the direct connection.
set -u

CONF=""
if [ -f /etc/secrets/wireproxy.conf ]; then
  CONF=/etc/secrets/wireproxy.conf
  echo "[entrypoint] WARP config found: Render Secret File /etc/secrets/wireproxy.conf"
elif [ -n "${WARP_PRIVATE_KEY:-}" ] && [ -n "${WARP_PEER_KEY:-}" ]; then
  mkdir -p /app/warp
  CONF=/app/warp/wireproxy.conf
  cat > "$CONF" <<EOF
[Interface]
PrivateKey = ${WARP_PRIVATE_KEY}
Address = ${WARP_ADDRESS_V4:-172.16.0.2/32}
EOF
  if [ -n "${WARP_ADDRESS_V6:-}" ]; then
    echo "Address = ${WARP_ADDRESS_V6}" >> "$CONF"
  fi
  cat >> "$CONF" <<EOF
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
  chmod 600 "$CONF"
  echo "[entrypoint] WARP config built from env vars"
fi

if [ -n "$CONF" ]; then
  wireproxy -c "$CONF" &
  WP_PID=$!
  echo "[entrypoint] wireproxy started (pid $WP_PID), waiting for tunnel…"
  WARP_OK=0
  i=0
  while [ $i -lt 30 ]; do
    if curl -s --max-time 5 --socks5-hostname 127.0.0.1:1080 https://api.ipify.org >/tmp/warp_ip 2>/dev/null; then
      echo "[entrypoint] WARP OK — exit IP: $(cat /tmp/warp_ip)"
      WARP_OK=1
      break
    fi
    i=$((i + 1))
    sleep 2
  done
  if [ "$WARP_OK" = "1" ]; then
    export YTDLP_PROXY="socks5://127.0.0.1:1080"
    export WARP_STATUS="ok"
  else
    echo "[entrypoint] WARP FAILED — tunnel did not come up (likely outbound UDP blocked). Continuing WITHOUT proxy."
    export WARP_STATUS="failed"
    kill $WP_PID 2>/dev/null || true
  fi
else
  echo "[entrypoint] No WARP config — running without proxy."
  export WARP_STATUS="not_configured"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
