"""In-process job store (v1: no Redis). Jobs are JSON files in data/jobs/."""
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import JOBS_DIR

_lock = threading.Lock()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def create(kind: str, payload: dict) -> dict:
    job = {
        "id": new_id(), "kind": kind, "status": "queued",
        "progress": 0, "message": "En cola…",
        "payload": payload, "result": None, "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save(job)
    return job


def save(job: dict) -> None:
    with _lock:
        _path(job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2),
                                     encoding="utf-8")


def get(job_id: str) -> dict | None:
    p = _path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_active() -> list:
    """Trabajos en curso (queued / processing / running), los más recientes
    primero. Los archivos JSON viejos (done / error) se ignoran. Nunca
    levanta una excepción: un archivo corrupto se salta."""
    out = []
    try:
        files = sorted(JOBS_DIR.glob("*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return out
    for f in files[:50]:
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") in ("queued", "processing", "running"):
            out.append(job)
    return out


def update(job_id: str, **fields) -> dict | None:
    job = get(job_id)
    if job is None:
        return None
    job.update(fields)
    save(job)
    return job
