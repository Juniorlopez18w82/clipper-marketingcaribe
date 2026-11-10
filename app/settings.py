"""Platform settings (key/value, admin-editable) + withdrawal requests.

- settings: defaults the admin sets from the Panel Admin > Configuración
  tab (e.g. default_brand_text, default_frame_enabled). New users get
  these defaults at registration (see auth.register_user).
- withdrawal_requests: a user asks for a withdrawal from Mi Cuenta
  (amount in RD$, method, details). The admin approves or rejects it in
  the Panel Admin > Retiros tab. NOTE: there are no balances/earnings
  yet — the amount is simply what the user requests.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from . import auth  # noqa: F401 — registers the users table for the FKs
from .db import Base

logger = logging.getLogger(__name__)

WD_PENDING = "pending"
WD_APPROVED = "approved"
WD_REJECTED = "rejected"

# Claves de configuración con su valor por defecto.
DEFAULT_SETTINGS = {
    "default_brand_text": "@marketingcaribe",
    "default_frame_enabled": "true",
}


class PlatformSetting(Base):
    __tablename__ = "settings"

    key = Column(String(80), primary_key=True)
    value = Column(String(500), nullable=False, default="")


class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount_dop = Column(Integer, nullable=False)  # monto pedido, en RD$
    method = Column(String(120), nullable=False, default="")
    details = Column(String(500), nullable=False, default="")
    status = Column(String(20), nullable=False, default=WD_PENDING)
    admin_note = Column(String(500), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    decided_at = Column(DateTime(timezone=True), nullable=True)


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


def withdrawal_out(w: WithdrawalRequest, user=None) -> dict:
    return {
        "id": w.id, "user_id": w.user_id,
        "user_name": user.name if user else None,
        "user_email": user.email if user else None,
        "amount_dop": w.amount_dop, "method": w.method,
        "details": w.details, "status": w.status,
        "admin_note": w.admin_note,
        "created_at": w.created_at.isoformat() if w.created_at else None,
        "decided_at": w.decided_at.isoformat() if w.decided_at else None,
    }
