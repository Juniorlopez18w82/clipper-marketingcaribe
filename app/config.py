"""Settings. Everything is local-first; no YouTube API key, no cloud key."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CLIPPER_DATA_DIR", BASE_DIR / "data"))
SOURCES_DIR = DATA_DIR / "sources"
OUTPUTS_DIR = DATA_DIR / "outputs"
JOBS_DIR = DATA_DIR / "jobs"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
RECEIPTS_DIR = DATA_DIR / "receipts"  # comprobantes de pago subidos

for d in (SOURCES_DIR, OUTPUTS_DIR, JOBS_DIR, TRANSCRIPTS_DIR, RECEIPTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Whisper model: "tiny" (fast, rough) or "base" (proven 2026-11-10, better ES).
# "small" is more accurate but slow on CPU-only machines.
WHISPER_MODEL = os.environ.get("CLIPPER_WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("CLIPPER_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("CLIPPER_WHISPER_COMPUTE", "int8")

# Defaults approved by Juan 2026-11-10 (full-bleed 9:16, Bulin test clip).
DEFAULT_FRAME_COLOR = "#FADE4C"
DEFAULT_FRAME_WIDTH = 14
DEFAULT_BRAND_TEXT = "@marketingcaribe"
DEFAULT_TITLE_TEXT = ""
DEFAULT_SUBTITLE_SIZE = 76
# Max concurrent heavy jobs (download/transcribe/render) in-process.
MAX_WORKERS = int(os.environ.get("CLIPPER_MAX_WORKERS", "2"))

FONT_BOLD = os.environ.get(
    "CLIPPER_FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)
