"""Platform settings (key/value, admin-editable).

- settings: defaults the admin sets from the Panel Admin > Configuración
  tab (e.g. default_brand_text, default_frame_enabled). New users get
  these defaults at registration (see auth.register_user).

Retiros: eliminados el 2026-11-10 por decisión de Juan (el usuario no
hará ningún retiro). La tabla withdrawal_requests queda en la base de
datos sin uso; no hay endpoints ni UI de retiros.
"""
import logging

from sqlalchemy import Column, String

from .db import Base

logger = logging.getLogger(__name__)

# Claves de configuración con su valor por defecto.
DEFAULT_SETTINGS = {
    "default_brand_text": "@marketingcaribe",
    "default_frame_enabled": "true",
}


class PlatformSetting(Base):
    __tablename__ = "settings"

    key = Column(String(80), primary_key=True)
    value = Column(String(500), nullable=False, default="")


def seed_settings(db) -> None:
    """Create default settings only when a key is missing."""
    for key, value in DEFAULT_SETTINGS.items():
        if db.query(PlatformSetting).filter(PlatformSetting.key == key).first() is None:
            db.add(PlatformSetting(key=key, value=value))
    db.commit()


def get_setting(db, key: str, default: str = "") -> str:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    return row.value if row is not None else default


def set_setting(db, key: str, value: str) -> PlatformSetting:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if row is None:
        row = PlatformSetting(key=key, value=value)
    else:
        row.value = value
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
