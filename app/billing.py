"""Planes, métodos de pago y solicitudes de suscripción (compra con comprobante).

Flujo (manual, sin pasarela): el usuario elige un plan, elige un método
bancario, ve los datos (banco, cuenta, titular, instrucciones), sube su
comprobante (imagen o PDF, máx. 5 MB, en data/receipts/) y la solicitud
queda Pendiente. El admin la aprueba o rechaza en el Panel Admin. Al
aprobar se crea la suscripción del usuario (starts_at/ends_at = +30 días
según el plan) y el usuario queda desbloqueado.

Sin suscripción activa el usuario está BLOQUEADO: el panel muestra
"Debes contratar un plan para usar el clipper" y los endpoints de
clips/render devuelven 403 cuando el usuario va logueado (sin token
siguen públicos en v1 para no romper el flujo actual — ver README).
Excepción: el ADMIN (is_admin=True) nunca está bloqueado ni necesita
plan — has_active_subscription() devuelve True siempre para el admin.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey,
                        Integer, JSON, String)

from . import auth  # noqa: F401 — registers the users table for the FKs
from .db import Base

logger = logging.getLogger(__name__)

REQ_PENDING = "pending"
REQ_APPROVED = "approved"
REQ_REJECTED = "rejected"


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False)
    # Precio en DÓLARES (US$). Los planes reales son Free $0, Pro $25 y
    # Gold $35 (2026-11-12, Juan). La columna vieja price_dop (RD$) queda
    # en la base de datos sin usarse; el equivalente en moneda local se
    # calcula en el frontend con la tasa actual del dólar (ver app.js).
    price_usd = Column(Float, nullable=False, default=0)
    duration_days = Column(Integer, nullable=False, default=30)
    features = Column(JSON, nullable=False, default=list)  # lista de textos
    is_popular = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    bank = Column(String(120), nullable=False, default="")
    account_number = Column(String(80), nullable=False, default="")
    account_holder = Column(String(160), nullable=False, default="")
    account_type = Column(String(60), nullable=False, default="")
    instructions = Column(String(500), nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)


class PaymentRequest(Base):
    """Solicitud de compra (tabla payment_requests)."""
    __tablename__ = "payment_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    payment_method_id = Column(Integer, ForeignKey("payment_methods.id"),
                               nullable=False)
    receipt_file = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default=REQ_PENDING)
    admin_note = Column(String(500), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    decided_at = Column(DateTime(timezone=True), nullable=True)


class UserSubscription(Base):
    """Suscripción activa del usuario (tabla user_subscriptions)."""
    __tablename__ = "user_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


# --- Seed: los 3 planes reales, en US$, en este orden. ---
# Free $0 (1 día de prueba), Pro $25/mes, Gold $35/mes (2026-11-12, Juan).
# Los Lives solo están en Gold. El equivalente en moneda local se muestra
# en el frontend con la tasa actual del dólar (APIs gratuitas, ver app.js).
SEED_PLANS = [
    {"name": "Free", "price_usd": 0, "duration_days": 1, "is_popular": False,
     "sort_order": 1, "features": [
        "1 día de acceso gratis",
        "Acceso ilimitado por 24 horas",
        "Prueba de las herramientas de IA",
        "Clips de videos largos a cortos",
        "Funciones principales de la plataforma",
    ]},
    {"name": "Pro", "price_usd": 25, "duration_days": 30, "is_popular": False,
     "sort_order": 2, "features": [
        "Videos largos a clips cortos",
        "IA que detecta momentos virales",
        "Subtítulos automáticos",
        "Edición de clips",
        "Adaptación para TikTok, Reels y Shorts",
        "Herramientas de optimización de contenido",
        "Funciones profesionales",
    ]},
    {"name": "Gold", "price_usd": 35, "duration_days": 30, "is_popular": True,
     "sort_order": 3, "features": [
        "Todo lo del plan Pro",
        "Generación avanzada de clips con IA",
        "Procesamiento de videos largos",
        "Subtítulos automáticos avanzados",
        "Edición profesional de clips",
        "Optimización para redes sociales",
        "Procesamiento de Lives en vivo",
        "Conversión de Lives a clips cortos",
        "Momentos destacados durante el LIVE",
        "Acceso completo premium",
    ]},
]

# Solo un ejemplo INACTIVO para mostrar la forma. El admin crea las
# cuentas reales desde el Panel Admin; aquí NO hay cuentas bancarias reales.
SEED_METHOD_EXAMPLE = {
    "name": "Ejemplo — Banco",
    "bank": "Banco de ejemplo",
    "account_number": "000-000000-0",
    "account_holder": "Nombre de ejemplo",
    "account_type": "Ahorros",
    "instructions": "Método de ejemplo inactivo. El admin debe crear los métodos reales desde el Panel Admin.",
    "is_active": False,
}


def seed_billing(db) -> None:
    """Seed planes y método de ejemplo solo si las tablas están vacías.

    Migración de planes 2026-11-12 (Juan: planes reales Free/Pro/Gold en
    US$): si ya existen planes viejos (Full/Pro/Básico en RD$), se
    actualizan EN SU LUGAR para no romper las suscripciones existentes
    (Full→Gold, Básico→Free, Pro se queda Pro con el precio nuevo). Los
    planes que no están en SEED_PLANS se desactivan (no se borran, para
    no romper el historial de solicitudes y suscripciones).
    """
    existing = db.query(Plan).all()
    if not existing:
        for p in SEED_PLANS:
            db.add(Plan(**p))
        db.commit()
        logger.info("Seed plans created: Free, Pro, Gold (US$)")
    else:
        # Renombres de la migración (solo si el destino aún no existe).
        names = {p.name for p in existing}
        for old_name, new_name in (("Full", "Gold"), ("Básico", "Free"),
                                   ("Basico", "Free")):
            if old_name in names and new_name not in names:
                row = next(p for p in existing if p.name == old_name)
                row.name = new_name
                names.discard(old_name)
                names.add(new_name)
                logger.info("Plan renombrado: %s → %s", old_name, new_name)
        wanted = {p["name"]: p for p in SEED_PLANS}
        for row in existing:
            if row.name in wanted:
                p = wanted[row.name]
                row.price_usd = p["price_usd"]
                row.duration_days = p["duration_days"]
                row.features = p["features"]
                row.is_popular = p["is_popular"]
                row.sort_order = p["sort_order"]
                row.is_active = True
            else:
                row.is_active = False
        for name, p in wanted.items():
            if name not in {r.name for r in existing}:
                db.add(Plan(**p))
        db.commit()
        logger.info("Plans synced to Free/Pro/Gold (US$)")
    if db.query(PaymentMethod).count() == 0:
        db.add(PaymentMethod(**SEED_METHOD_EXAMPLE))
        db.commit()
        logger.info("Seed payment method created: Ejemplo — Banco (inactivo)")


def plan_out(p: Plan) -> dict:
    return {
        "id": p.id, "name": p.name, "price_usd": float(p.price_usd or 0),
        "duration_days": p.duration_days, "features": p.features or [],
        "is_popular": bool(p.is_popular), "is_active": bool(p.is_active),
        "sort_order": p.sort_order,
    }


def method_out(m: PaymentMethod) -> dict:
    return {
        "id": m.id, "name": m.name, "bank": m.bank,
        "account_number": m.account_number, "account_holder": m.account_holder,
        "account_type": m.account_type, "instructions": m.instructions,
        "is_active": bool(m.is_active),
    }


def request_out(r: PaymentRequest, plan=None, method=None, user=None) -> dict:
    return {
        "id": r.id, "user_id": r.user_id,
        "user_name": user.name if user else None,
        "user_email": user.email if user else None,
        "plan_id": r.plan_id, "plan_name": plan.name if plan else None,
        "payment_method_id": r.payment_method_id,
        "payment_method_name": method.name if method else None,
        "receipt_file": r.receipt_file,
        "receipt_url": f"/api/admin/receipts/{r.receipt_file}",
        "status": r.status, "admin_note": r.admin_note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
    }


def subscription_out(s: UserSubscription, plan=None) -> dict:
    return {
        "id": s.id, "user_id": s.user_id, "plan_id": s.plan_id,
        "plan_name": plan.name if plan else None,
        "starts_at": s.starts_at.isoformat() if s.starts_at else None,
        "ends_at": s.ends_at.isoformat() if s.ends_at else None,
        "status": s.status,
    }


def get_active_subscription(db, user_id: int):
    """Suscripción activa y vigente del usuario, o None."""
    now = datetime.now(timezone.utc)
    return (db.query(UserSubscription)
            .filter(UserSubscription.user_id == user_id,
                    UserSubscription.status == "active",
                    UserSubscription.ends_at > now)
            .order_by(UserSubscription.ends_at.desc()).first())


def has_active_subscription(db, user_id: int) -> bool:
    """True si el usuario tiene suscripción activa y vigente.

    El ADMIN nunca necesita plan: is_admin=True siempre desbloquea
    (2026-11-10: el admin veía el bloqueo de "contrata un plan").
    """
    user = db.query(auth.User).filter(auth.User.id == user_id).first()
    if user is not None and getattr(user, "is_admin", False):
        return True
    return get_active_subscription(db, user_id) is not None


def approve_request(db, req: PaymentRequest, admin_note: str = ""):
    """Aprueba la solicitud y crea la suscripción (+duration_days)."""
    plan = db.query(Plan).filter(Plan.id == req.plan_id).first()
    if plan is None:
        raise ValueError("El plan de la solicitud ya no existe")
    now = datetime.now(timezone.utc)
    # Desactivar suscripciones activas anteriores del usuario.
    for old in (db.query(UserSubscription)
                .filter(UserSubscription.user_id == req.user_id,
                        UserSubscription.status == "active").all()):
        old.status = "expired"
        db.add(old)
    sub = UserSubscription(
        user_id=req.user_id, plan_id=req.plan_id, starts_at=now,
        ends_at=now + timedelta(days=plan.duration_days or 30),
        status="active")
    db.add(sub)
    # El campo plan del usuario refleja su plan activo (antes quedaba en
    # "free" aunque la suscripción estuviera aprobada).
    owner = db.query(auth.User).filter(auth.User.id == req.user_id).first()
    if owner is not None:
        owner.plan = plan.name
        db.add(owner)
    req.status = REQ_APPROVED
    req.admin_note = admin_note or ""
    req.decided_at = now
    db.add(req)
    db.commit()
    db.refresh(sub)
    db.refresh(req)
    return sub


def assign_plan(db, user_id: int, plan_id: int) -> UserSubscription:
    """Asigna una membresía MANUALMENTE (admin, sin solicitud de pago
    ni comprobante): crea la suscripción activa (+duration_days del
    plan) y actualiza user.plan. Desactiva suscripciones activas
    anteriores del usuario, igual que approve_request."""
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if plan is None:
        raise ValueError("El plan no existe")
    user = db.query(auth.User).filter(auth.User.id == user_id).first()
    if user is None:
        raise ValueError("El usuario no existe")
    now = datetime.now(timezone.utc)
    for old in (db.query(UserSubscription)
                .filter(UserSubscription.user_id == user_id,
                        UserSubscription.status == "active").all()):
        old.status = "expired"
        db.add(old)
    sub = UserSubscription(
        user_id=user_id, plan_id=plan_id, starts_at=now,
        ends_at=now + timedelta(days=plan.duration_days or 30),
        status="active")
    db.add(sub)
    user.plan = plan.name
    db.add(user)
    db.commit()
    db.refresh(sub)
    return sub


def reject_request(db, req: PaymentRequest, admin_note: str = ""):
    req.status = REQ_REJECTED
    req.admin_note = admin_note or ""
    req.decided_at = datetime.now(timezone.utc)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req
