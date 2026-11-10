"""Clip records (DB) — the panel's "Mis Clips".

A Clip is created when a render job finishes. Since 2026-11-10 clips are
downloadable IMMEDIATELY after render — there is no admin approval for
clips (Juan reversed the flow: only payments/receipts need approval).
The status field stays in the DB for history but blocks nothing; new
clips are recorded as "listo".
Jobs themselves stay in data/jobs/ JSON files (v1); Clip is the durable,
per-user record the panel and the admin panel list.
"""
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer,
                        String)

from . import auth  # noqa: F401 — registers the users table for the FK below
from .db import Base

STATUS_PENDIENTE = "pendiente"  # legacy, ya no bloquea nada
STATUS_APROBADO = "aprobado"  # legacy, ya no bloquea nada
STATUS_RECHAZADO = "rechazado"  # legacy, ya no bloquea nada
STATUS_LISTO = "listo"  # estado actual: el clip está listo para descargar
ALL_STATUSES = (STATUS_PENDIENTE, STATUS_APROBADO, STATUS_RECHAZADO, STATUS_LISTO)


class Clip(Base):
    __tablename__ = "clips"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    video_url = Column(String(500), nullable=False, default="")
    title = Column(String(255), nullable=False, default="")
    uploader = Column(String(255), nullable=False, default="")
    start = Column(Float, nullable=False, default=0.0)
    end = Column(Float, nullable=False, default=0.0)
    status = Column(String(20), nullable=False, default=STATUS_PENDIENTE)
    job_id = Column(String(32), nullable=True)
    file = Column(String(255), nullable=True)  # rendered mp4 in data/outputs/
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


def clip_out(clip: Clip) -> dict:
    return {
        "id": clip.id,
        "user_id": clip.user_id,
        "video_url": clip.video_url,
        "title": clip.title,
        "uploader": clip.uploader,
        "start": clip.start,
        "end": clip.end,
        "status": clip.status,
        "job_id": clip.job_id,
        "file": clip.file,
        "download_url": f"/api/download/{clip.file}" if clip.file else None,
        "created_at": clip.created_at.isoformat() if clip.created_at else None,
    }


def create_clip(db, *, user_id, video_url, title, uploader, start, end,
                job_id, file) -> Clip:
    clip = Clip(user_id=user_id, video_url=video_url or "", title=title or "",
                uploader=uploader or "", start=float(start), end=float(end),
                status=STATUS_LISTO, job_id=job_id, file=file)
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip
