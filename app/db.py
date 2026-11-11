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
def _ensure_user_pref_columns() -> None:
    """Add the clip-preference columns to an existing users table.

    create_all() only creates missing TABLES, never new columns — the
    live Render Postgres already has a users table from v1.4.0, so the
    new columns must be added with ALTER TABLE on boot.
    """
    from sqlalchemy import inspect

    cols = {
        "max_clips": "INTEGER DEFAULT 5",
        "min_clip_secs": "FLOAT DEFAULT 20.0",
        "max_clip_secs": "FLOAT DEFAULT 45.0",
        "quality": "VARCHAR(10) DEFAULT '1080p'",
        "subtitles_enabled": "BOOLEAN DEFAULT TRUE" if IS_POSTGRES else "BOOLEAN DEFAULT 1",
        "template": "VARCHAR(80) DEFAULT 'Gancho grande'",
    }
    try:
        existing = {c["name"] for c in inspect(engine).get_columns("users")}
        missing = [k for k in cols if k not in existing]
        if not missing:
            return
        with engine.begin() as conn:
            for name in missing:
                conn.exec_driver_sql(
                    f'ALTER TABLE users ADD COLUMN "{name}" {cols[name]}')
        logger.info("Migrated users table: added %s", ", ".join(missing))
    except Exception as e:  # noqa: BLE001 — migration is best-effort
        logger.warning("User columns migration failed: %s", e)


def _ensure_clip_columns() -> None:
    """Add the score/section_start columns to an existing clips table.

    Same pattern as _ensure_user_pref_columns: create_all() never adds
    columns to an existing table, so the live VPS SQLite and Render
    Postgres need ALTER TABLE on boot. Both engines support ADD COLUMN;
    the columns are nullable so old rows stay valid.
    """
    from sqlalchemy import inspect

    cols = {
        "score": "FLOAT",
        "section_start": "FLOAT",
        "cover_at": "FLOAT",
    }
    try:
        existing = {c["name"] for c in inspect(engine).get_columns("clips")}
        missing = [k for k in cols if k not in existing]
        if not missing:
            return
        with engine.begin() as conn:
            for name in missing:
                conn.exec_driver_sql(
                    f'ALTER TABLE clips ADD COLUMN "{name}" {cols[name]}')
        logger.info("Migrated clips table: added %s", ", ".join(missing))
    except Exception as e:  # noqa: BLE001 — migration is best-effort
        logger.warning("Clip columns migration failed: %s", e)


def init_db() -> None:
    """Create tables if they do not exist. Safe to call on every boot.

    Importing auth and clips_db registers the User and Clip models on
    Base. After creating tables, seed the two demo users if the users
    table is empty (see auth.seed_users). If the database is unreachable
    at boot (e.g. wrong DATABASE_URL), log a warning and let the app
    start — auth endpoints will surface the real error.
    """
    from . import auth, billing, clips_db, settings  # noqa: F401 — registers models

    try:
        Base.metadata.create_all(bind=engine)
        logger.info("DB ready (%s)", db_backend())
    except Exception as e:  # noqa: BLE001 — DB may not exist yet
        logger.warning("DB init failed (%s): %s", db_backend(), e)
        return
    _ensure_user_pref_columns()
    _ensure_clip_columns()
    try:
        db = SessionLocal()
        try:
            settings.seed_settings(db)
            auth.seed_users(db)
            billing.seed_billing(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("DB seed failed (%s): %s", db_backend(), e)
