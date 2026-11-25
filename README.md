# Clipzio Pro

SaaS de recorte de videos (FastAPI + ffmpeg) que corre en Docker en el VPS (puerto 80 → 8000). Versión 1.5.0.

Snapshot del código desplegado en `/root/clipviral` el 2026-11-24.

## No incluido en este repo
- `.env` (claves): ver `.env.example`.
- `warp/wireproxy.conf` (clave WireGuard de WARP): ver `warp/wireproxy.conf.example`.
- `data/` (base de datos SQLite, videos, transcripciones, clips de usuarios).
- Archivos binarios (siguen en el VPS): `app/fonts/*.ttf`, `app/models/face_detection_yunet_2023mar.onnx`,
  `face_models/*` (onnx, caffemodel, prototxt), `frontend/demos/*.mp4`, `frontend/*.png|jpg`, `frontend/logos/banreservas.png`.
