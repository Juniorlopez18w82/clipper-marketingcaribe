"""Database layer — PostgreSQL on Render, SQLite fallback locally.

DATABASE_URL (set by Render from the clipper-db service) -> PostgreSQL.
Without DATABASE_URL (local dev) -> SQLite file data/clipper.db.

Why Postgres in production: Render's web-service disk is ephemeral — a
SQLite file would be wiped on every redeploy/restart, losing all users.
Postgres lives in a separate managed service and survives deploys.
Migration before the free tier expires (90 days): pg_dump the Render DB
and restore it into Supabase/Neon free (no expiry) or a new Render DB —
see README.md "Base de datos y usuarios".
"""
import logging
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATA_DIR

logger = logging.getLogger(__name__)

Base = declarative_base()


def _database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        # Render / SQLAlchemy: postgres:// is deprecated, psycopg3 wants
        # postgresql+psycopg://. Render's own URL starts with postgres://.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    return f"sqlite:///{DATA_DIR / 'clipper.db'}"


DATABASE_URL = _database_url()
IS_POSTGRES = DATABASE_URL.startswith("postgresql")

_engine_kwargs: dict = {"pool_pre_ping": True}
if not IS_POSTGRES:
    # SQLite: one file, allow use from FastAPI threads.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI dependency: one session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_backend() -> str:
    return "postgres" if IS_POSTGRES else "sqlite"


def init_db() -> None:
    """Create tables if they do not exist. Safe to call on every boot.

    Importing auth and clips_db registers the User and Clip models on
    Base. After creating tables, seed the two demo users if the users
    table is empty (see auth.seed_users). If the database is unreachable
    at boot (e.g. wrong DATABASE_URL), log a warning and let the app
    start — auth endpoints will surface the real error.
    """
    from . import auth, billing, clips_db  # noqa: F401 — registers models

    try:
        Base.metadata.create_all(bind=engine)
        logger.info("DB ready (%s)", db_backend())
    except Exception as e:  # noqa: BLE001 — DB may not exist yet
        logger.warning("DB init failed (%s): %s", db_backend(), e)
        return
    try:
        db = SessionLocal()
        try:
            auth.seed_users(db)
            billing.seed_billing(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("DB seed failed (%s): %s", db_backend(), e)
