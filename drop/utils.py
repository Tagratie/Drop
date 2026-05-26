"""Stateless helpers — paths, text, parsing, icon generation."""
import os
import re
import sys
import shutil
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
import time

from PIL import Image, ImageDraw

from .theme import (
    NO_WINDOW, ACCENT, VIDEO_EXTS, AUDIO_EXTS, script_dir,
)


def get_ffmpeg():
    """Locate ffmpeg.exe — bundled next to the script wins, then PATH, then VLC's bundle."""
    candidates = []
    base = script_dir() if "_script_dir" in globals() else Path(__file__).parent
    candidates.append(base / "ffmpeg.exe")
    if sys.platform == "win32":
        # VLC ships ffmpeg-like libs but not ffmpeg.exe. Just rely on PATH/local.
        pass
    for c in candidates:
        if c.exists():
            return str(c)
    # PATH fallback
    found = shutil.which("ffmpeg")
    return found


# ── paths & theme ────────────────────────────────────────────────────────────

DOWNLOADS    = Path.home() / "Downloads"
CONFIG_DIR   = Path.home() / ".drop"
LIBRARY_FILE = CONFIG_DIR / "library.json"
WINDOW_FILE  = CONFIG_DIR / "window.json"

# Where managed copies live — sits next to the script (or the frozen exe)


def get_bin(name):
    """Locate a bundled executable like yt-dlp.exe / spotdl.exe / ffmpeg.exe.
    When frozen by PyInstaller, lives next to the .exe in _MEIPASS.
    Otherwise lives next to the entry script (parent of the drop/ package).
    Falls back to the plain name so it can resolve on PATH."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        # __file__ is drop/utils.py — go up one level to the script dir.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(base, f"{name}.exe")
    return local if os.path.exists(local) else name


def clean_subprocess_env():
    """Return an env dict safe for spawning sibling executables from a frozen
    Drop.exe. PyInstaller's bootloader sets _MEIPASS2 / _PYI_ARCHIVE_FILE /
    _PYI_APPLICATION_HOME_DIR to coordinate with itself on restart — when the
    child is ALSO a PyInstaller binary (yt-dlp.exe, visualizer.exe), inheriting
    these makes the child's bootloader think it should reuse the parent's
    _MEIPASS extraction. The child then can't find its own Python/runtime and
    exits within a few seconds with no useful output. Stripping these vars
    (and Drop's _MEIPASS from PATH) restores normal startup for the child.
    No-op when running from source."""
    env = os.environ.copy()
    if not getattr(sys, "frozen", False):
        return env
    for k in ("_MEIPASS2", "_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR",
              "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC"):
        env.pop(k, None)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        try:
            mp = os.path.normcase(os.path.abspath(meipass))
            kept = [p for p in env.get("PATH", "").split(os.pathsep)
                    if p and os.path.normcase(os.path.abspath(p)) != mp]
            env["PATH"] = os.pathsep.join(kept)
        except Exception:
            pass
    return env




def is_spotify(url): return "open.spotify.com" in url

def is_audio(url):   return any(d in url for d in ("soundcloud.com", "bandcamp.com"))




def detect_kind(url):
    if not url: return None
    if is_spotify(url): return "spotify"
    if is_audio(url):   return "audio"
    if url.startswith(("http://", "https://")): return "video"
    return None




def url_source(url):
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "") or "—"
    except Exception:
        return "—"




def humanize_size(n):
    if n is None: return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(n); i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024; i += 1
    return f"{n:.1f} {units[i]}"




def humanize_time(ts):
    delta = time.time() - ts
    if delta < 60:      return "just now"
    if delta < 3600:    return f"{int(delta/60)}m ago"
    if delta < 86400:   return f"{int(delta/3600)}h ago"
    if delta < 86400*7: return f"{int(delta/86400)}d ago"
    return datetime.fromtimestamp(ts).strftime("%b %d")




def open_path(p):
    p = str(p)
    if not os.path.exists(p):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.run(["open", p])
        else:
            subprocess.run(["xdg-open", p])
        return True
    except Exception:
        return False




def reveal_path(p):
    p = Path(p)
    try:
        if sys.platform == "win32" and p.exists():
            subprocess.run(["explorer", "/select,", str(p)], creationflags=NO_WINDOW)
            return True
        if sys.platform == "darwin" and p.exists():
            subprocess.run(["open", "-R", str(p)])
            return True
        return open_path(p.parent if p.parent.exists() else p)
    except Exception:
        return False


_BAD_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')




_BAD_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def safe_dirname(name):
    """Make a library name safe to use as a folder name."""
    cleaned = _BAD_FS_CHARS.sub("_", (name or "").strip()).rstrip(" .")
    return cleaned or "Library"




def unique_path(target: Path) -> Path:
    """Return target if free, else target with ' (2)', ' (3)', ... suffix."""
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    n = 2
    while True:
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


# ── File pickers ─────────────────────────────────────────────────────────────
# tkinter.filedialog wraps the modern Vista+ IFileOpenDialog under the hood
# on Windows (Tk 8.6.4+), so this gives us the same chrome as File Explorer.




# ── geometry helper for rounded shapes (used by widgets) ────────────────────
def _rrect_pts(x1, y1, x2, y2, r):
    """Points for a smooth-polygon rounded rectangle (Tk Canvas)."""
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    return [
        x1+r, y1,  x1+r, y1,  x2-r, y1,  x2-r, y1,
        x2,   y1,
        x2,   y1+r, x2, y1+r, x2, y2-r,  x2, y2-r,
        x2,   y2,
        x2-r, y2,  x2-r, y2,  x1+r, y2,  x1+r, y2,
        x1,   y2,
        x1,   y2-r, x1, y2-r, x1, y1+r,  x1, y1+r,
        x1,   y1,
    ]


# ── general utils ────────────────────────────────────────────────────────────




# ── output parsing for yt-dlp / spotdl ──────────────────────────────────────
PCT_RE     = re.compile(r"(\d+(?:\.\d+)?)%")
SPEED_RE   = re.compile(r"at\s+(\S+/s)", re.I)
ETA_RE     = re.compile(r"ETA\s+(\S+)", re.I)
MERGE_RE   = re.compile(r"Merg(er|ing)", re.I)
EXTRACT_RE = re.compile(r"Extract(Audio|ing audio)", re.I)


def _humanize_speed(s):
    s = s.replace("iB", "B")
    m = re.match(r"([\d.]+)([KMG]?B/s)", s)
    if not m: return s
    return f"{float(m.group(1)):.1f} {m.group(2)}"




def parse_line(line):
    out = {"pct": None, "speed": None, "eta": None, "phase": None}
    if not line: return out
    if MERGE_RE.search(line):   out["phase"] = "Merging";  return out
    if EXTRACT_RE.search(line): out["phase"] = "Encoding"; return out
    m = PCT_RE.search(line)
    if m:
        out["pct"] = float(m.group(1))
        s = SPEED_RE.search(line)
        if s: out["speed"] = _humanize_speed(s.group(1))
        e = ETA_RE.search(line)
        if e and e.group(1).lower() != "unknown": out["eta"] = e.group(1)
        return out
    if "Downloading webpage" in line or "Extracting URL" in line or line.startswith(("[youtube]", "[info]")):
        out["phase"] = "Fetching"
    elif "format(s):" in line or "Destination" in line:
        out["phase"] = "Starting"
    return out




def fmt_status(p):
    if p["pct"] is not None:
        bits = [f"{int(p['pct'])}%"]
        if p["speed"]: bits.append(p["speed"])
        if p["eta"]:   bits.append(p["eta"])
        return " · ".join(bits)
    return (p["phase"] + "…") if p["phase"] else None


# ── library (multi) ──────────────────────────────────────────────────────────




# ── icon generator ──────────────────────────────────────────────────────────
def make_icon(size=64):
    """Drop's app glyph: a filled disc with a download arrow + tray base.
    Proportions tuned to stay legible at 16px while looking refined at 256px.

    Rendered at 4× and downsampled with Lanczos so the disc edge + arrowhead
    diagonal get proper anti-aliasing. PIL's ellipse/polygon are pure-pixel
    with no AA, so rendering natively at the target size leaves jaggy edges
    that read as cheap on the taskbar."""
    # Tiny sizes skip supersampling — the per-size legibility floors below
    # depend on target-size pixel counts, and 16→64→16 round-trip just blurs
    # the 1px ring out of existence.
    scale = 1 if size < 32 else 4
    s = size * scale

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Floors are computed at the *target* size (so legibility logic is
    # unchanged) then scaled up to the render canvas.
    pad  = max(1, size // 32) * scale
    ring = max(1, size // 64) * scale

    # Disc: filled accent with a thin dark ring so it has an edge on either
    # light or dark backgrounds (Windows taskbar, Explorer thumbnails, etc.).
    d.ellipse([pad, pad, s - pad, s - pad], fill=ACCENT)
    d.ellipse([pad, pad, s - pad, s - pad],
              outline="#0a0a0a", width=ring)

    # All horizontal geometry snaps to a common center column and even widths
    # so the left/right edges land on identical sub-pixel positions. Without
    # this, fractional coordinates from `s * 0.46` etc. let PIL's polygon
    # rasterizer treat the two diagonals asymmetrically, and the arrowhead
    # reads as visibly tilted after Lanczos downsample.
    def even(v): return int(round(v / 2)) * 2  # snap to even integer
    cx           = s // 2
    arrow_top    = int(round(s * 0.22))
    shaft_bottom = int(round(s * 0.55))
    head_bottom  = int(round(s * 0.74))
    base_top     = int(round(s * 0.80))
    base_bottom  = int(round(s * 0.88))

    shaft_w = even(max(3 * scale, s * 0.16))
    head_w  = even(max(6 * scale, s * 0.46))
    base_w  = even(s * 0.58)

    fg = "#0a0a0a"

    d.rectangle(
        [cx - shaft_w // 2, arrow_top, cx + shaft_w // 2, shaft_bottom],
        fill=fg,
    )
    d.polygon(
        [(cx - head_w // 2, shaft_bottom),
         (cx + head_w // 2, shaft_bottom),
         (cx, head_bottom)],
        fill=fg,
    )
    d.rectangle(
        [cx - base_w // 2, base_top, cx + base_w // 2, base_bottom],
        fill=fg,
    )

    # Force perfect horizontal symmetry. On an even-width canvas the true
    # center sits *between* two pixels, so the apex/center column lands
    # half-a-pixel off and the polygon rasterizer produces visibly tilted
    # diagonals (plus PIL's ellipse has its own slight L/R bias). Averaging
    # with the mirror is a 1-line guarantee that left == right.
    img = Image.blend(img, img.transpose(Image.FLIP_LEFT_RIGHT), 0.5)

    if scale > 1:
        img = img.resize((size, size), Image.LANCZOS)
    return img




def ensure_app_icon_file():
    """Write a multi-resolution .ico next to the script if it isn't there.
    Returns the path. Used both by Tk (iconbitmap) and by PyInstaller."""
    icon_path = script_dir() / "drop.ico"
    if icon_path.exists():
        return str(icon_path)
    try:
        _write_drop_ico(icon_path)
        return str(icon_path)
    except Exception:
        return None


def _write_drop_ico(icon_path):
    """Render every frame at its native size (so make_icon's per-size
    legibility floors actually apply) and pack a PNG-compressed .ico by hand.
    Pillow's ICO writer with the `sizes=` arg downsamples a single master and
    produces a degenerate 256 frame (~2KB) that Windows 11 picks for the
    taskbar — hence the well-known blur. Manual packing avoids that path."""
    import struct
    from io import BytesIO

    sizes = [16, 24, 32, 48, 64, 128, 256]
    encoded = []
    for s in sizes:
        buf = BytesIO()
        make_icon(s).save(buf, format="PNG", optimize=True)
        encoded.append((s, buf.getvalue()))

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(encoded))     # ICONDIR
    offset = 6 + 16 * len(encoded)
    for s, blob in encoded:
        # width/height of 0 means 256 in the ICO spec.
        out += struct.pack(
            "<BBBBHHII",
            0 if s == 256 else s, 0 if s == 256 else s,
            0, 0, 1, 32,
            len(blob), offset,
        )
        offset += len(blob)
    for _, blob in encoded:
        out += blob

    with open(icon_path, "wb") as f:
        f.write(out)



# ── UI icon PNGs (search / dots / info / gear / layout) ─────────────────────

ICON_CACHE_DIR = Path.home() / ".drop" / "icons"
ICON_VERSION   = "v4"   # bump to force regeneration


def _icon_path(name, size, color_tag):
    return ICON_CACHE_DIR / f"{ICON_VERSION}-{name}-{size}-{color_tag}.png"


def _color_tag(rgb):
    return "%02x%02x%02x" % rgb


def _hex_to_rgb(s):
    s = s.lstrip("#")
    if len(s) == 3:
        # Shorthand like "#000" or "#fff" — expand each digit
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        # Defensive fallback — return black so we don't crash on malformed input
        return (0, 0, 0)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def ensure_ui_icon(name, size, color="#f4f4f4"):
    """Generate-once-and-cache a small monochrome PNG for a UI button.
    `name` is one of: search, search_close, dots, info, info_active,
    gear, grid, list, plus, rotate, check, speaker, speaker_mute, trash.
    Returns the file path as str, or None on failure."""
    if isinstance(color, str):
        rgb = _hex_to_rgb(color)
    else:
        rgb = tuple(color)
    tag = _color_tag(rgb)
    out = _icon_path(name, size, tag)
    if out.exists():
        return str(out)
    try:
        ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        img = _draw_ui_icon(name, size, rgb)
        if img is None:
            return None
        img.save(str(out), format="PNG")
        return str(out)
    except Exception:
        return None


def _draw_ui_icon(name, size, rgb):
    """Render a single UI icon at `size`×`size`. PIL's antialiasing is much
    better than Tk's, hence why we generate once and cache."""
    import math
    # Render at 4x and downscale for clean antialiasing
    SCALE = 4
    S = size * SCALE
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fg = rgb + (255,)
    cx, cy = S / 2, S / 2

    if name == "search":
        # Magnifying glass: lens (circle) + diagonal handle
        r = S * 0.22
        ox = cx - r * 0.35
        oy = cy - r * 0.35
        # Lens
        line_w = max(2, int(S * 0.06))
        d.ellipse([ox - r, oy - r, ox + r, oy + r],
                  outline=fg, width=line_w)
        # Handle from circle's lower-right edge outward at 45°
        sx = ox + r * math.cos(math.radians(45))
        sy = oy + r * math.sin(math.radians(45))
        ex = sx + r * 1.0
        ey = sy + r * 1.0
        d.line([(sx, sy), (ex, ey)], fill=fg, width=line_w)

    elif name == "search_close":
        # × glyph
        r = S * 0.22
        line_w = max(2, int(S * 0.06))
        d.line([(cx - r, cy - r), (cx + r, cy + r)], fill=fg, width=line_w)
        d.line([(cx - r, cy + r), (cx + r, cy - r)], fill=fg, width=line_w)

    elif name == "dots":
        # Vertical three dots
        dot_r = S * 0.08
        gap   = S * 0.16
        for dy in (-gap, 0, gap):
            d.ellipse([cx - dot_r, cy + dy - dot_r,
                       cx + dot_r, cy + dy + dot_r], fill=fg)

    elif name in ("info", "info_active"):
        # Circled-i. Active version flips the colors at the canvas level
        # (we just draw the same glyph in the requested color).
        r = S * 0.36
        line_w = max(2, int(S * 0.06))
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  outline=fg, width=line_w)
        # Dot near top, stem below
        pad   = r * 0.25
        gap_  = r * 0.18
        avail = 2 * r - 2 * pad - gap_
        dot_h = avail * 0.30
        stem_h = avail * 0.70
        dot_top = cy - r + pad
        dot_bot = dot_top + dot_h
        stem_top = dot_bot + gap_
        stem_bot = stem_top + stem_h
        dot_r = dot_h / 2
        stem_w = max(1, dot_h * 0.40)
        # Dot
        dcx, dcy = cx, (dot_top + dot_bot) / 2
        d.ellipse([dcx - dot_r, dcy - dot_r,
                   dcx + dot_r, dcy + dot_r], fill=fg)
        # Stem with rounded caps
        d.rectangle([cx - stem_w, stem_top, cx + stem_w, stem_bot], fill=fg)
        d.ellipse([cx - stem_w, stem_top - stem_w,
                   cx + stem_w, stem_top + stem_w], fill=fg)
        d.ellipse([cx - stem_w, stem_bot - stem_w,
                   cx + stem_w, stem_bot + stem_w], fill=fg)

    elif name == "gear":
        # Stylized gear: outer ring with teeth + inner hole
        r_outer = S * 0.36
        r_tooth = S * 0.44
        r_inner = S * 0.16
        line_w  = max(2, int(S * 0.06))
        # Teeth: 8 small rectangles around the circle
        for i in range(8):
            a = math.radians(i * 45)
            tx = cx + r_outer * math.cos(a)
            ty = cy + r_outer * math.sin(a)
            tx2 = cx + r_tooth * math.cos(a)
            ty2 = cy + r_tooth * math.sin(a)
            d.line([(tx, ty), (tx2, ty2)], fill=fg,
                   width=int(S * 0.10))
        # Outer ring
        d.ellipse([cx - r_outer, cy - r_outer,
                   cx + r_outer, cy + r_outer],
                  outline=fg, width=line_w)
        # Inner hole — punch transparency
        d.ellipse([cx - r_inner, cy - r_inner,
                   cx + r_inner, cy + r_inner],
                  fill=(0, 0, 0, 0))

    elif name == "grid":
        # 2x2 grid of squares (means "switch to grid layout")
        b = S * 0.18  # half-size of each square
        gap = S * 0.06
        for sx in (-1, 1):
            for sy in (-1, 1):
                xc = cx + sx * (b + gap / 2)
                yc = cy + sy * (b + gap / 2)
                d.rounded_rectangle(
                    [xc - b, yc - b, xc + b, yc + b],
                    radius=int(b * 0.25), fill=fg,
                )

    elif name == "list":
        # Three horizontal bars (means "switch to list layout")
        bar_h = S * 0.10
        gap = S * 0.10
        bar_w = S * 0.62
        x1 = cx - bar_w / 2
        x2 = cx + bar_w / 2
        for i in (-1, 0, 1):
            yc = cy + i * (bar_h + gap)
            d.rounded_rectangle(
                [x1, yc - bar_h / 2, x2, yc + bar_h / 2],
                radius=int(bar_h * 0.4), fill=fg,
            )

    elif name == "plus":
        # Big plus
        r = S * 0.30
        line_w = int(S * 0.12)
        d.line([(cx - r, cy), (cx + r, cy)], fill=fg, width=line_w)
        d.line([(cx, cy - r), (cx, cy + r)], fill=fg, width=line_w)

    elif name == "rotate":
        # Clockwise rotation icon: ~270° arc with a solid-triangle arrowhead
        # at one end. The triangle is sized substantially relative to the
        # arc so the rotation direction reads at any icon size — the old
        # chevron version got lost at 18px.
        r = S * 0.30
        line_w = max(2, int(S * 0.11))
        # PIL arc angles: 0° = 3 o'clock, increasing clockwise. Drawing from
        # 30° clockwise around to 300° (just past 12 o'clock) leaves a gap
        # at the top-right where the arrowhead lives.
        start_deg, end_deg = 30, 300
        d.arc([cx - r, cy - r, cx + r, cy + r],
              start=start_deg, end=end_deg, fill=fg, width=line_w)
        # Endpoint of the arc (where the arrowhead attaches).
        end_rad = math.radians(end_deg)
        ex = cx + r * math.cos(end_rad)
        ey = cy + r * math.sin(end_rad)
        # Filled-triangle arrowhead. Tip points in the direction of motion
        # (clockwise tangent = arc angle + 90°). Base sits perpendicular,
        # straddling the arc's radial line so it visually springs from the
        # arc terminus.
        tan = end_rad + math.radians(90)
        tip = (ex + S * 0.20 * math.cos(tan),
               ey + S * 0.20 * math.sin(tan))
        # Base vertices along the radial direction (one outside the arc,
        # one inside).
        base_half = S * 0.12
        b1 = (ex + base_half * math.cos(end_rad),
              ey + base_half * math.sin(end_rad))
        b2 = (ex - base_half * math.cos(end_rad),
              ey - base_half * math.sin(end_rad))
        d.polygon([tip, b1, b2], fill=fg)

    elif name == "check":
        # Bold checkmark glyph
        line_w = max(2, int(S * 0.12))
        d.line([(cx - S * 0.22, cy + S * 0.02),
                (cx - S * 0.06, cy + S * 0.18),
                (cx + S * 0.24, cy - S * 0.18)],
               fill=fg, width=line_w, joint="curve")

    elif name in ("speaker", "speaker_mute"):
        # Loudspeaker silhouette: small square base on the left + a wedge
        # flaring out to a tall trapezoidal mouth. One filled polygon (no
        # outline) so the silhouette stays clean at every size.
        #   muted   → glyph only, plus a red 'X' to the right
        #   speaker → glyph + two concentric sound-wave arcs to the right
        base_h = S * 0.30   # height of the square base on the left
        mouth_h = S * 0.62  # height of the trapezoid's open end
        x_base_l = cx - S * 0.42
        x_base_r = cx - S * 0.20
        x_mouth  = cx + S * 0.04
        d.polygon([
            (x_base_l, cy - base_h / 2),
            (x_base_r, cy - base_h / 2),
            (x_mouth,  cy - mouth_h / 2),
            (x_mouth,  cy + mouth_h / 2),
            (x_base_r, cy + base_h / 2),
            (x_base_l, cy + base_h / 2),
        ], fill=fg)

        if name == "speaker":
            # Two concentric sound-wave arcs to the right of the mouth.
            # Both arcs are open on the left, so they read as "sound coming
            # out of the speaker" rather than just circles.
            line_w = max(2, int(S * 0.07))
            for r, span in ((S * 0.18, 70), (S * 0.32, 80)):
                ax = x_mouth + S * 0.04
                d.arc([ax - r, cy - r, ax + r, cy + r],
                      start=-span / 2, end=span / 2,
                      fill=fg, width=line_w)
        else:
            # Muted: tight red X right of the mouth. Sized so it visually
            # balances the cone instead of overpowering it.
            line_w = max(2, int(S * 0.09))
            ax = x_mouth + S * 0.10
            xr = S * 0.18
            # Pure red: easy to read as "off" at small sizes without
            # depending on luminance contrast.
            red = (228, 60, 60, 255)
            d.line([(ax, cy - xr), (ax + 2 * xr, cy + xr)],
                   fill=red, width=line_w)
            d.line([(ax, cy + xr), (ax + 2 * xr, cy - xr)],
                   fill=red, width=line_w)

    elif name == "trash":
        # Trash can: lid bar with handle on top, body with two vertical lines
        line_w = max(2, int(S * 0.08))
        # Lid bar
        d.rectangle([cx - S * 0.32, cy - S * 0.28,
                     cx + S * 0.32, cy - S * 0.20], fill=fg)
        # Handle
        d.rectangle([cx - S * 0.12, cy - S * 0.36,
                     cx + S * 0.12, cy - S * 0.28], fill=fg)
        # Body — rounded rectangle outline
        d.rounded_rectangle(
            [cx - S * 0.26, cy - S * 0.18,
             cx + S * 0.26, cy + S * 0.32],
            radius=int(S * 0.05), outline=fg, width=line_w,
        )
        # Vertical streaks inside
        for dx in (-0.10, 0.10):
            d.line([(cx + S * dx, cy - S * 0.06),
                    (cx + S * dx, cy + S * 0.22)],
                   fill=fg, width=max(1, int(S * 0.04)))

    else:
        return None

    # Downscale with antialiasing
    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:
        resample = Image.LANCZOS
    return img.resize((size, size), resample)
