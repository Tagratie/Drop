"""Visual constants: colors, fonts, paths, file-extension sets."""
from pathlib import Path
import sys

# ── colors ───────────────────────────────────────────────────────────────────
BG       = "#0a0a0a"
BG2      = "#161616"
BG3      = "#222222"
BORDER   = "#2a2a2a"
ACCENT   = "#ffffff"
ACCENT_D = "#cccccc"
TEXT     = "#f4f4f4"
MUTED    = "#666666"
SOFT     = "#a8a8a8"
ERROR    = "#ff5e5e"
KIND_COLORS = {
    "video":   "#ffffff",
    "audio":   "#ffffff",
    "spotify": "#ffffff",
}

# ── paths ────────────────────────────────────────────────────────────────────
DOWNLOADS    = Path.home() / "Downloads"
CONFIG_DIR   = Path.home() / ".drop"
LIBRARY_FILE = CONFIG_DIR / "library.json"
WINDOW_FILE  = CONFIG_DIR / "window.json"

THUMB_DIR = CONFIG_DIR / "thumbs"
THUMB_W   = 720  # cached thumb width; height auto.

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def script_dir():
    """Folder containing the running .py (or the bundled .exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # When run from the package, __file__ resolves inside drop/.
    # The script_dir we care about is the parent of the package.
    return Path(__file__).resolve().parent.parent


LIBRARIES_DIR = CONFIG_DIR / "Libraries"

# ── file extension sets ──────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".flv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
