FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
# wireproxy 1.1.3 — static Go binary, no deps. Used only if a WARP config
# is provided (Render Secret File /etc/secrets/wireproxy.conf or env vars);
# see entrypoint.sh. If the tunnel cannot start, the app runs without proxy.
RUN curl -sL https://github.com/pufferffish/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz \
  | tar xz -C /usr/local/bin wireproxy \
 && chmod +x /usr/local/bin/wireproxy
# deno 2.x — runtime JS para yt-dlp (2026-11-11): sin un runtime JS,
# yt-dlp salta el cliente tv_embedded ("Skipping unsupported client")
# y la descarga cae al cliente web (bloqueado por YouTube) y sale
# corrupta — eso rompió el render dual de qjn_zvadIdM. Con deno,
# tv_embedded vuelve a dar hasta 1080p.
RUN curl -sL https://github.com/denoland/deno/releases/download/v2.4.0/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
 && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extract('deno', '/usr/local/bin')" \
 && chmod +x /usr/local/bin/deno && rm /tmp/deno.zip \
 && deno --version
# Node.js 22 (v219, 2026-11-23) — runtime del servidor PO Token bgutil.
# YouTube exige Proof-of-Origin en IPs de datacenter; el servidor
# bgutil (puerto 4416) genera el token y el plugin de yt-dlp lo usa solo.
RUN curl -sL https://nodejs.org/dist/v22.23.5/node-v22.23.5-linux-x64.tar.gz -o /tmp/node.tar.gz \
 && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \
 && rm /tmp/node.tar.gz \
 && node --version && npm --version
# Servidor PO Token bgutil 2.0.2 (v219): se compila aquí y el entrypoint
# lo arranca en 127.0.0.1:4416 antes de uvicorn.
RUN curl -sL https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/2.0.2.tar.gz -o /tmp/bgutil.tar.gz \
 && mkdir -p /opt/bgutil && tar -xzf /tmp/bgutil.tar.gz -C /opt/bgutil --strip-components=1 \
 && rm /tmp/bgutil.tar.gz \
 && cd /opt/bgutil/server && npm ci && npx tsc \
 && test -f build/main.js && echo "bgutil server built OK"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY frontend ./frontend
# Modelos del detector de caras DNN (OpenCV SSD, v190): sin ellos la
# detección de facecam usa Haar (más débil, no ve a Speed). ~10.6 MB.
COPY face_models ./face_models
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
 && mkdir -p data/sources data/outputs data/jobs data/transcripts
EXPOSE 8000
CMD ["/app/entrypoint.sh"]
