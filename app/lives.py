"""Live sessions — the "Lives" section (2026-11-10, Juan).

A LiveSession monitors one YouTube live: every ``interval_minutes`` the
monitor takes the most recent window (ending near the live edge, see
video.live_status), extracts up to ``clips_per_round`` clips with the
same LLM/heuristic pipeline as /api/clips, renders them, and stores
them as normal Clips linked by live_session_id — so they show up in
the Lives section as they are extracted, and also in Mis Clips.

Statuses: active (monitored), paused (kept, not processed), stopped
(user stopped it), ended (the live finished — video_info says
is_live False). A session never deletes its clips.
"""
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer,
                        String)

from . import auth  # noqa: F401 — registers the users table for the FK below
from .db import Base

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"
STATUS_ENDED = "ended"
ALL_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_STOPPED, STATUS_ENDED)

# Safety margin (s) before the estimated live edge: the HLS edge
# estimate is good to ~30 s, so the window ends 30 s before it —
# never request content that has not aired yet, and never leave a gap
# (the next round starts from last_end, which is the window end).
EDGE_SAFETY_SECONDS = 30.0


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    url = Column(String(500), nullable=False, default="")
    video_id = Column(String(32), nullable=False, default="")
    title = Column(String(255), nullable=False, default="")
    uploader = Column(String(255), nullable=False, default="")
    # Cover image URL from the live (yt-dlp thumbnail). Shown on the
    # session card so the live is recognizable at a glance.
    thumbnail = Column(String(500), nullable=False, default="")
    # Config (set by the user in the Lives section).
    interval_minutes = Column(Integer, nullable=False, default=10)
    clips_per_round = Column(Integer, nullable=False, default=3)
    min_len = Column(Float, nullable=False, default=20.0)
    max_len = Column(Float, nullable=False, default=45.0)
    status = Column(String(20), nullable=False, default=STATUS_ACTIVE)
    # Stream-time seconds processed up to (end of the last window).
    # None = first round (starts from live edge - interval).
    last_end = Column(Float, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    total_clips = Column(Integer, nullable=False, default=0)
    last_error = Column(String(500), nullable=True, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


def live_session_out(session: LiveSession, clip_count: int = 0) -> dict:
    url = session.url or ""
    return {
        "id": session.id,
        "user_id": session.user_id,
        "url": url,
        "platform": ("twitch" if "twitch.tv" in url.lower()
                     else "youtube"),
        "video_id": session.video_id,
        "title": session.title,
        "uploader": session.uploader,
        "thumbnail": session.thumbnail or "",
        "interval_minutes": session.interval_minutes,
        "clips_per_round": session.clips_per_round,
        "min_len": session.min_len,
        "max_len": session.max_len,
        "status": session.status,
        "last_end": session.last_end,
        "last_run_at": (session.last_run_at.isoformat()
                        if session.last_run_at else None),
        "total_clips": session.total_clips,
        "clip_count": clip_count,
        "last_error": session.last_error or "",
        "created_at": (session.created_at.isoformat()
                       if session.created_at else None),
    }
