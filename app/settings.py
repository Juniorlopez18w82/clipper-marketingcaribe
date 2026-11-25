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
    # Plantilla que usan por defecto los videos (2026-11-10, Juan:
    # "en configuración salga como tipos de plantillas"): Gancho
    # grande | Limpio | Impacto.
    "default_template": "Gancho grande",
    # Color de fondo de la palabra clave en los subtítulos.
    "subtitle_highlight_color": "#FADE4C",
    # IA que elige los clips (2026-11-18, Juan: cambiar a Gemini Flash,
    # y poder elegirla desde el Panel Admin + agregar claves): "groq"
    # (gpt-oss-120b/20b, gratis) o "gemini" (gemini-2.5-flash, ~$0.006
    # por video de 1 h, elige mejor). Las claves viven aquí (las pone el
    # admin) y si están vacías se usa el .env. Nunca se devuelven
    # completas por la API (GET las enmascara, POST ignora la máscara).
    "llm_provider": "gemini",
    "groq_api_key": "",
    "gemini_api_key": "",
    # YouTube Data API v3 (v225, 2026-11-23, Juan): clave oficial para
    # leer el borde de un live cuando yt-dlp está bloqueado por el
    # bot-check. La pone el admin en Panel Admin > Configuración; si
    # está vacía se usa YOUTUBE_API_KEY del .env. Nunca se devuelve
    # completa por la API (GET la enmascara, POST ignora la máscara).
    # La clave está restringida por referer a https://clipziopro.com/,
    # así que cada llamada manda Referer: https://clipziopro.com/.
    "youtube_api_key": "",
    # Modo mantenimiento (2026-11-19, Juan): "true" = solo los admins
    # pueden entrar; los usuarios no pueden loguearse y los que están
    # dentro son expulsados (get_current_user devuelve 401). Se activa
    # desde Panel Admin > Configuración.
    "maintenance_mode": "false",
    # Caché inteligente (v205): "true" = reutilizar descargas y
    # transcripciones de trabajos anteriores del mismo video y rango.
    # cache_max_age_days: la limpieza automática borra archivos de
    # caché con más de N días (por defecto 30).
    "cache_enabled": "true",
    "cache_max_age_days": "30",
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
