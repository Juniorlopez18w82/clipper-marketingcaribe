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
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY frontend ./frontend
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
 && mkdir -p data/sources data/outputs data/jobs data/transcripts
EXPOSE 8000
CMD ["/app/entrypoint.sh"]
