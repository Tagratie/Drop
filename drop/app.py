"""Top-level coordinator: window, title bar, two views (bar + library),
update banner, downloader/library wiring."""
import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

import pystray
from PIL import Image, ImageDraw

try:
    from PIL import ImageTk
except Exception:
    ImageTk = None

# Optional VLC for hover-preview player (App owns a separate one from FeedPlayer)
try:
    import vlc
    _VLC_OK = True
except Exception:
    vlc = None
    _VLC_OK = False

from .theme import (
    BG, BG2, BG3, BORDER, ACCENT, ACCENT_D, TEXT, MUTED, SOFT, ERROR,
    KIND_COLORS, DOWNLOADS, CONFIG_DIR, LIBRARY_FILE, WINDOW_FILE,
    VIDEO_EXTS, AUDIO_EXTS,
)
from .utils import (
    detect_kind, url_source, humanize_size, humanize_time,
    open_path, reveal_path, _rrect_pts,
    make_icon, ensure_app_icon_file,
)
from .platform_win import (
    round_window_corners, set_window_region_rounded, clear_window_region,
    fix_borderless_alt_tab, get_work_area,
    pick_files_modern, pick_folder_modern,
    minimize_window,
    set_taskbar_icon, set_app_user_model_id,
)
from .widgets import (
    RoundedButton, RoundedCard, BookButton, DotsButton, InfoButton,
    SearchButton, IconButton, TogglePill,
    ask_text as _ask_text_modal,
    alert    as _alert_modal,
)
from .cache import ThumbnailCache, PreviewCache
from .library import Library, migrate_libraries_dir
from .downloader import Downloader
from .updater import UpdateWatcher
from .player import FeedPlayer


class App:
    """Coordinator. Owns the window, the downloader, and two swappable views."""
    BAR_W      = 540
    BAR_H_S    = 108     # idle:   title bar + bar pill
    BAR_H_M    = 144     # busy:   + status row
    BAR_H_L    = 172     # done:   + chip
    BAR_PILL_H = 52      # height of the input pill
    LIB_W      = 540
    LIB_H      = 660
    PLACEHOLDER = "Paste a URL…"

    def __init__(self, library):
        self.library      = library
        self.current      = None     # active Downloader
        self._pct         = 0
        self._save_pending = False
        self.last_files   = []       # paths from the most recent download
        self.last_url     = None
        self.mode         = "bar"
        self._render_locked = False
        self._import_queue = None
        self._import_total = 0
        self._import_done  = 0
        self._settings_panel = None
        self._selected_idx = None   # keyboard-selected tile in grid view
        # Multi-select state. When _selection_mode is True, tiles render a
        # corner checkbox, clicks toggle selection instead of opening the
        # feed view, and a bottom action bar offers bulk delete/move.
        self._selection_mode = False
        self._selected_idxs  = set()
        # Loaded from saved geometry so it persists across launches.
        # _geom isn't loaded yet here — we'll resolve the actual value just
        # below once _load_geom() runs.
        self.lib_layout    = "grid"

        # Maximize state tracking (we manage it ourselves since overrideredirect
        # disables the native maximize/restore behavior).
        self._maximized       = False
        self._restore_geom    = None  # (x, y, w, h) saved before maximize
        self._snap_armed      = False  # cursor in top hot-zone during current drag
        self._title_drag_data = None  # in-progress title-bar drag state

        self.root = tk.Tk()
        self.root.title("Drop")
        self.root.configure(bg=BG)

        # Strip native chrome BEFORE first map so things settle cleanly.
        self.root.overrideredirect(True)

        self._geom = self._load_geom()
        # Now that _geom is loaded, pull the persisted layout choice.
        self.lib_layout = self._geom.get("lib_layout", "grid")
        # Resume-where-you-left-off toggle. Default: on.
        self.resume_enabled = bool(self._geom.get("resume_enabled", True))
        # Hover-preview playback in the library grid. Default: OFF because
        # VLC warm-up on every hover is heavy, and most users don't realize
        # it's running until they wonder why their fans spin up while
        # scrolling. A one-shot hint on first launch points at the toggle.
        # Default off — the hover-preview pipeline is currently buggy enough
        # (focus theft, occasional VLC instance leak, surface placement on
        # rapidly-scrolled grids) that opt-in is the safer default. Users
        # who want it flip the Settings toggle, persisted in _geom.
        self.preview_enabled = bool(self._geom.get("preview_enabled", False))
        self._pick_fonts()
        self._set_window_icon()

        # Custom title bar — slim drag strip with min/close buttons
        self._build_title_bar()

        # Top accent line under the title bar
        tk.Frame(self.root, bg=ACCENT, height=2).pack(fill="x")

        # Update banner — hidden until UpdateWatcher finds a new drop.py.
        self._build_update_banner()

        # Container that swaps between home and library views
        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(fill="both", expand=True)

        # Global loading overlay — child of container, not lib_body, so it
        # can cover BOTH home_frame and library_frame during the transition.
        # The previous design lived inside lib_body, which doesn't exist on
        # screen until library_frame is packed — meaning the loading screen
        # could only appear AFTER the transition started, not during it.
        # By parenting on container, we can lift it on top of either view.
        self.lib_loading = tk.Frame(self.container, bg=BG)
        self._lib_loading_label = tk.Label(
            self.lib_loading, text="Loading\u2026",
            bg=BG, fg=TEXT, font=("Consolas", 18, "bold"),
        )
        self._lib_loading_label.place(relx=0.5, rely=0.5, anchor="center")

        self._build_home()
        self._build_library()

        # Resize grips — invisible regions at window edges so a borderless
        # window can still be resized like a normal one.
        self._build_resize_grips()

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Configure>", self._on_configure)

        self._show_home()

        # Order matters: alt-tab fix re-shows the window, then we apply rounding.
        self.root.after(50,  lambda: fix_borderless_alt_tab(self.root))
        self.root.after(150, self._refresh_corners)

        # Auto-update watcher — looks for a fresh drop.py in Downloads.
        self._pending_update_path = None
        self.update_watcher = UpdateWatcher(
            DOWNLOADS,
            on_update=lambda p: self.root.after(0, self._on_update_found, p),
        )
        self.update_watcher.start()

    # ── geometry ─────────────────────────────────────────────────────────────
    def _load_geom(self):
        try:
            geom = json.loads(WINDOW_FILE.read_text())
        except Exception:
            return {}
        # Clamp saved x/y to the current work area. Without this, a position
        # saved on a now-disconnected monitor (e.g. x=-2500 from a left-side
        # second screen) strands the window off-screen on every launch and
        # looks like "the program won't open."
        x, y = geom.get("x"), geom.get("y")
        if x is not None and y is not None:
            try:
                left, top, right, bottom = get_work_area()
                w = self.BAR_W
                h = self.BAR_H_S
                # Require at least a 60px sliver to remain visible on each axis
                # — enough that the user can grab the title bar and drag it back.
                if x + w < left + 60 or x > right - 60 or \
                   y + h < top + 60  or y > bottom - 60:
                    geom.pop("x", None)
                    geom.pop("y", None)
            except Exception:
                pass
        return geom

    def _on_configure(self, event):
        if event.widget is not self.root:
            return
        # Debounce a corner refresh; SetWindowRgn must be re-applied each resize.
        if getattr(self, "_corner_after", None):
            try: self.root.after_cancel(self._corner_after)
            except Exception: pass
        self._corner_after = self.root.after(60, self._refresh_corners)

        if self._save_pending:
            return
        self._save_pending = True
        self.root.after(500, self._do_save_geom)

    def _do_save_geom(self):
        self._save_pending = False
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._geom["x"] = self.root.winfo_x()
            self._geom["y"] = self.root.winfo_y()
            if self.mode == "library":
                self._geom["lib_w"] = self.root.winfo_width()
                self._geom["lib_h"] = self.root.winfo_height()
            self._geom["lib_layout"] = getattr(self, "lib_layout", "grid")
            self._geom["resume_enabled"] = bool(getattr(self, "resume_enabled", True))
            self._geom["preview_enabled"] = bool(getattr(self, "preview_enabled", False))
            # Player volume (0–100). Written by the volume slider via
            # _on_volume_changed, restored when FeedPlayer is constructed.
            feed = getattr(self, "feed", None)
            if feed is not None and hasattr(feed, "volume"):
                self._geom["volume"] = int(feed.volume)
            WINDOW_FILE.write_text(json.dumps(self._geom))
        except Exception:
            pass

    def _bar_height(self):
        """Bar-mode window height is locked to BAR_H_S — small. The pill
        absorbs status (inline progress strip at its bottom edge) and the
        post-download chip (in-pill DONE state with filename + + LIB button).
        Window height never changes in bar mode → no jitter, no dead zone."""
        return self.BAR_H_S

    def _apply_mode_geometry(self):
        # Don't fight the maximized state — keep the window full-screen
        # until the user actively restores it.
        if self._maximized:
            self.root.minsize(self.BAR_W, self.BAR_H_S)
            self.root.maxsize(0, 0)  # Tk: 0,0 == no maximum
            self._set_resize_grips_enabled(True)
            return
        x = self._geom.get("x")
        y = self._geom.get("y")
        if self.mode == "bar":
            w = self.BAR_W
            h = self.BAR_H_S
            # Hard-lock both dimensions: minsize == maxsize means Tk refuses
            # any user-driven resize. Kills jitter and accidental drag-resize.
            self.root.minsize(self.BAR_W, self.BAR_H_S)
            self.root.maxsize(self.BAR_W, self.BAR_H_S)
            # No reason to show resize grips on a window that can't resize —
            # would just confuse the cursor.
            self._set_resize_grips_enabled(False)
        else:
            w = self._geom.get("lib_w", self.LIB_W)
            h = self._geom.get("lib_h", self.LIB_H)
            self.root.minsize(440, 480)
            self.root.maxsize(0, 0)
            self._set_resize_grips_enabled(True)
        if x is not None and y is not None:
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self.root.geometry(f"{w}x{h}")

    def _set_resize_grips_enabled(self, on):
        """Show / hide the eight edge+corner resize grips. Used to fully
        disable resize in bar mode so the cursor doesn't change to a
        resize-arrow over edges that wouldn't actually resize anything."""
        grips = getattr(self, "_resize_grips", None)
        if not grips:
            return
        for frame, place_kw in grips:
            try:
                if on:
                    frame.place(**place_kw)
                    frame.lift()
                else:
                    frame.place_forget()
            except Exception:
                pass

    # ── fonts & icon ─────────────────────────────────────────────────────────
    def _pick_fonts(self):
        avail = set(tkfont.families())
        mono  = next((f for f in ("IBM Plex Mono", "JetBrains Mono", "Cascadia Mono",
                                  "Consolas", "Menlo", "Courier New") if f in avail),
                     "TkFixedFont")
        self.f_input    = (mono, 10)
        self.f_btn      = (mono, 9, "bold")
        self.f_chip     = (mono, 8, "bold")
        self.f_status   = (mono, 9)
        self.f_meta     = (mono, 8)
        self.f_label    = (mono, 9, "bold")
        self.f_tab      = (mono, 8, "bold")
        self.f_card_t   = (mono, 11, "bold")
        self.f_libicon  = (mono, 14, "bold")
        self.f_back     = (mono, 9, "bold")
        self.f_h1       = (mono, 11, "bold")

    def _set_window_icon(self):
        # Prefer a real .ico — Windows uses it for taskbar/alt-tab/explorer
        # at the right size automatically. Fall back to a PNG iconphoto on
        # other platforms or if .ico generation failed.
        ico = ensure_app_icon_file()
        if ico and sys.platform == "win32":
            try:
                # Stable AUMID so the taskbar groups under our own icon, not
                # python.exe / explorer. Has to be set before the window's
                # taskbar button is registered for it to take effect.
                set_app_user_model_id("Drop.Player.1")
                # iconbitmap covers the title-bar/alt-tab small icon. The
                # taskbar's ICON_BIG, however, isn't set by Tk on Windows —
                # set_taskbar_icon sends WM_SETICON manually so the big
                # frame from the .ico actually gets used.
                self.root.iconbitmap(default=ico)
                set_taskbar_icon(self.root, ico)
                return
            except Exception:
                pass
        if ImageTk is None: return
        try:
            self._icon_photo = ImageTk.PhotoImage(make_icon())
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    # ── UPDATE BANNER ────────────────────────────────────────────────────────
    def _build_update_banner(self):
        """Hidden by default. _on_update_found() shows it; users either click
        UPDATE (we install + relaunch) or LATER (we hide and remember the path
        so the user can click UPDATE later from the same banner)."""
        self.update_bar = tk.Frame(self.root, bg=ACCENT)
        # Not packed yet.

        inner = tk.Frame(self.update_bar, bg=ACCENT)
        inner.pack(fill="x", padx=12, pady=6)

        self.update_lbl = tk.Label(
            inner,
            text="New version detected — install and relaunch?",
            bg=ACCENT, fg="#000", font=self.f_label, anchor="w",
        )
        self.update_lbl.pack(side="left")

        self.update_yes = RoundedButton(
            inner, text="UPDATE", command=self._apply_update,
            bg="#000", fg=ACCENT, hover_bg="#222",
            font=self.f_chip, padx=10, pady=4, radius=8,
        )
        self.update_yes.pack(side="right", padx=(6, 0))

        self.update_no = RoundedButton(
            inner, text="LATER", command=self._dismiss_update,
            bg=ACCENT_D, fg="#000", hover_bg="#bce500",
            font=self.f_chip, padx=10, pady=4, radius=8,
        )
        self.update_no.pack(side="right", padx=(6, 0))

    def _on_update_found(self, src_path):
        """Watcher tells us a new drop.py appeared in Downloads."""
        # If we're already showing one, only replace if newer.
        if self._pending_update_path:
            try:
                if os.path.getmtime(src_path) <= os.path.getmtime(self._pending_update_path):
                    return
            except Exception:
                pass
        self._pending_update_path = src_path
        # Show banner (idempotent — packing twice is harmless)
        try:
            self.update_bar.pack(fill="x", before=self.container)
        except Exception:
            try: self.update_bar.pack(fill="x")
            except Exception: pass

    def _dismiss_update(self):
        # Don't forget the path; user might click UPDATE later via the same banner.
        try: self.update_bar.pack_forget()
        except Exception: pass

    def _apply_update(self):
        src = self._pending_update_path
        if not src or not os.path.exists(src):
            self._dismiss_update()
            return

        # When frozen, we can't replace ourselves (the watcher shouldn't even fire
        # but defend anyway).
        if getattr(sys, "frozen", False):
            messagebox.showinfo(
                "Drop",
                "Auto-update is only supported when running from source.\n"
                "For the bundled EXE, replace it manually.",
                parent=self.root,
            )
            return

        # We live inside the `drop/` package now. The shim that's invoked is the
        # `drop.py` next to the package folder. That's what we replace.
        package_dir = Path(__file__).resolve().parent
        target = package_dir.parent / "drop.py"
        try:
            # Backup current
            backup = target.with_suffix(".py.bak")
            try:
                if backup.exists(): backup.unlink()
                shutil.copy2(target, backup)
            except Exception:
                pass

            # Move src into place, overwriting the running script.
            # On Windows you can replace a .py while it's running because
            # CPython reads the source once at import — we're safe.
            shutil.move(src, target)
        except Exception as e:
            messagebox.showerror("Drop", f"Couldn't apply update:\n{e}",
                                 parent=self.root)
            return

        # Relaunch: spawn a fresh Python process pointing at the same script,
        # then exit ours. The new process starts immediately.
        try:
            self.update_watcher.stop()
        except Exception:
            pass
        try:
            args = [sys.executable, str(target)] + sys.argv[1:]
            # Detach so the new process survives our exit on Windows.
            kwargs = {}
            if sys.platform == "win32":
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS         = 0x00000008
                kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
                kwargs["close_fds"] = True
            subprocess.Popen(args, **kwargs)
        except Exception as e:
            messagebox.showerror("Drop",
                                 f"Update applied but couldn't relaunch:\n{e}\n\n"
                                 "Start Drop again manually.",
                                 parent=self.root)
        # Clean exit
        try:
            if hasattr(self, "feed"): self.feed.shutdown()
        except Exception: pass
        try: self.root.destroy()
        except Exception: pass
        os._exit(0)

    # ── manual resize on borderless window ────────────────────────────────────
    GRIP_PX = 5     # how thick each edge grip is

    def _build_resize_grips(self):
        """Borderless windows have no native resize handles. Place thin
        invisible Frames around the edges and corners, each with a cursor
        style and drag bindings, so the user can resize like normal."""
        gp = self.GRIP_PX
        # Native Windows resize cursors via Tk's `size_*` family — these map
        # to IDC_SIZENWSE / IDC_SIZENESW / IDC_SIZENS / IDC_SIZEWE on Win32 so
        # the cursor matches what File Explorer shows on its own resize edges.
        grips = [
            ("n",  dict(relx=0,    y=0,            relwidth=1, height=gp),  "size_ns",     "n"),
            ("s",  dict(relx=0,    rely=1, y=-gp,  relwidth=1, height=gp),  "size_ns",     "s"),
            ("w",  dict(x=0,       rely=0,         relheight=1, width=gp),  "size_we",     "w"),
            ("e",  dict(relx=1, x=-gp, rely=0,     relheight=1, width=gp),  "size_we",     "e"),
            ("nw", dict(x=0,       y=0,            width=gp, height=gp),    "size_nw_se",  "nw"),
            ("ne", dict(relx=1, x=-gp, y=0,        width=gp, height=gp),    "size_ne_sw", "ne"),
            ("sw", dict(x=0, rely=1, y=-gp,        width=gp, height=gp),    "size_ne_sw", "sw"),
            ("se", dict(relx=1, x=-gp, rely=1, y=-gp,
                         width=gp, height=gp),                              "size_nw_se",  "se"),
        ]
        self._resize_state = None
        # Track widget+placement so _set_resize_grips_enabled can hide them
        # in bar mode (where the window is locked and resizing is disabled).
        self._resize_grips = []
        for _name, place_kw, cursor, dirs in grips:
            f = tk.Frame(self.root, bg=BG, cursor=cursor)
            f.place(**place_kw)
            f.lift()
            f.bind("<Button-1>",        lambda e, d=dirs: self._resize_press(e, d))
            f.bind("<B1-Motion>",       self._resize_drag)
            f.bind("<ButtonRelease-1>", self._resize_release)
            self._resize_grips.append((f, place_kw))

    def _resize_press(self, event, dirs):
        if self._maximized:
            return
        self._resize_state = {
            "dirs":  dirs,
            "x0":    event.x_root,
            "y0":    event.y_root,
            "win_x": self.root.winfo_x(),
            "win_y": self.root.winfo_y(),
            "win_w": self.root.winfo_width(),
            "win_h": self.root.winfo_height(),
        }

    def _resize_drag(self, event):
        st = self._resize_state
        if not st: return
        dx = event.x_root - st["x0"]
        dy = event.y_root - st["y0"]
        x, y, w, h = st["win_x"], st["win_y"], st["win_w"], st["win_h"]
        min_w, min_h = 360, 200

        if "e" in st["dirs"]:
            w = max(min_w, w + dx)
        if "w" in st["dirs"]:
            new_w = max(min_w, w - dx)
            x = x + (w - new_w)
            w = new_w
        if "s" in st["dirs"]:
            h = max(min_h, h + dy)
        if "n" in st["dirs"]:
            new_h = max(min_h, h - dy)
            y = y + (h - new_h)
            h = new_h

        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _resize_release(self, _event):
        self._resize_state = None
        self.root.after(40, self._refresh_corners)


    # ── CUSTOM TITLE BAR ─────────────────────────────────────────────────────
    TITLE_BAR_H = 28
    SNAP_HOTZONE_PX = 8
    DRAG_THRESHOLD_PX = 4

    def _build_title_bar(self):
        """Slim draggable strip across the top with app title + min/max/close.

        Window-control glyphs use the Segoe MDL2 Assets / Segoe Fluent Icons
        font (ships on Win10+) so the icons match what File Explorer renders
        on its own title bar. Falls back to Unicode geometric glyphs if the
        font isn't installed."""
        self.title_bar = tk.Frame(self.root, bg=BG, height=self.TITLE_BAR_H)
        self.title_bar.pack(fill="x", side="top")
        self.title_bar.pack_propagate(False)

        # Pick the icon font once. MDL2 Assets is on Win10+; Fluent Icons is
        # the renamed-but-compatible Win11 successor. Fall back to default.
        avail = set(tkfont.families())
        if "Segoe Fluent Icons" in avail:
            ctrl_font = ("Segoe Fluent Icons", 10)
            G_MIN, G_MAX, G_RESTORE, G_CLOSE = "\uE921", "\uE922", "\uE923", "\uE8BB"
        elif "Segoe MDL2 Assets" in avail:
            ctrl_font = ("Segoe MDL2 Assets", 10)
            G_MIN, G_MAX, G_RESTORE, G_CLOSE = "\uE921", "\uE922", "\uE923", "\uE8BB"
        else:
            # Unicode fallback. Same glyphs as the previous build.
            ctrl_font = (self.f_btn[0], 10)
            G_MIN, G_MAX, G_RESTORE, G_CLOSE = "\u2013", "\u25A1", "\u2750", "\u2715"
        self._title_glyphs = (G_MIN, G_MAX, G_RESTORE, G_CLOSE)

        # App title — left-aligned, doubles as a drag handle
        self.title_lbl = tk.Label(self.title_bar, text="DROP", bg=BG, fg=SOFT,
                                   font=self.f_label, padx=12)
        self.title_lbl.pack(side="left")

        # Window controls on the right (close last so it's furthest right)
        self.btn_close = tk.Label(self.title_bar, text=G_CLOSE, bg=BG, fg=SOFT,
                                   font=ctrl_font, cursor="hand2",
                                   padx=14, pady=4)
        self.btn_close.pack(side="right")
        self.btn_close.bind("<Button-1>", lambda e: self.hide())
        self.btn_close.bind("<Enter>",
            lambda e: (self.btn_close.configure(bg=ERROR, fg="#000")))
        self.btn_close.bind("<Leave>",
            lambda e: (self.btn_close.configure(bg=BG, fg=SOFT)))

        self.btn_max = tk.Label(self.title_bar, text=G_MAX, bg=BG, fg=SOFT,
                                 font=ctrl_font, cursor="hand2",
                                 padx=14, pady=4)
        self.btn_max.pack(side="right")
        self.btn_max.bind("<Button-1>", lambda e: self._toggle_maximize())
        self.btn_max.bind("<Enter>",
            lambda e: (self.btn_max.configure(bg=BG3, fg=TEXT)))
        self.btn_max.bind("<Leave>",
            lambda e: (self.btn_max.configure(bg=BG, fg=SOFT)))

        self.btn_min = tk.Label(self.title_bar, text=G_MIN, bg=BG, fg=SOFT,
                                 font=ctrl_font, cursor="hand2",
                                 padx=14, pady=4)
        self.btn_min.pack(side="right")
        self.btn_min.bind("<Button-1>", lambda e: self._minimize())
        self.btn_min.bind("<Enter>",
            lambda e: (self.btn_min.configure(bg=BG3, fg=TEXT)))
        self.btn_min.bind("<Leave>",
            lambda e: (self.btn_min.configure(bg=BG, fg=SOFT)))

        # Drag region: title bar background + the title label
        for w in (self.title_bar, self.title_lbl):
            w.bind("<Button-1>",        self._title_press)
            w.bind("<B1-Motion>",       self._title_drag)
            w.bind("<ButtonRelease-1>", self._title_release)
            w.bind("<Double-Button-1>", lambda e: self._toggle_maximize())

    # ── window dragging + snap-to-top maximize ───────────────────────────────
    def _title_press(self, event):
        # Capture starting positions in screen coords + window origin.
        self._title_drag_data = {
            "x_root0": event.x_root,
            "y_root0": event.y_root,
            "win_x0":  self.root.winfo_x(),
            "win_y0":  self.root.winfo_y(),
            "active":  False,
        }
        self._snap_armed = False

    def _title_drag(self, event):
        st = self._title_drag_data
        if not st: return
        dx = event.x_root - st["x_root0"]
        dy = event.y_root - st["y_root0"]
        if not st["active"]:
            if (dx * dx + dy * dy) < (self.DRAG_THRESHOLD_PX ** 2):
                return
            st["active"] = True
            # If we were maximized and the user drags, restore to a window
            # that's anchored under the cursor — same as Windows native behavior.
            if self._maximized:
                self._restore_to_cursor(event.x_root)
                # Re-baseline the drag origin for the new position
                st["x_root0"] = event.x_root
                st["y_root0"] = event.y_root
                st["win_x0"]  = self.root.winfo_x()
                st["win_y0"]  = self.root.winfo_y()
                dx = dy = 0

        new_x = st["win_x0"] + dx
        new_y = st["win_y0"] + dy
        self.root.geometry(f"+{new_x}+{new_y}")

        # Arm snap-to-maximize if cursor approaches the very top of the screen.
        wa = get_work_area()
        self._snap_armed = (event.y_root <= wa[1] + self.SNAP_HOTZONE_PX)

    def _title_release(self, event):
        st = self._title_drag_data
        self._title_drag_data = None
        if not st or not st["active"]:
            return
        if self._snap_armed and not self._maximized:
            self._maximize()
        self._snap_armed = False

    def _restore_to_cursor(self, cursor_x_root):
        """Restore from maximized state, anchoring the new window so the
        cursor stays roughly over the title bar where it was clicked."""
        if not self._restore_geom:
            # We don't have a saved size — fall back to defaults.
            w, h = self.BAR_W, self._bar_height()
            x = max(0, cursor_x_root - w // 2)
            y = 30
        else:
            x, y, w, h = self._restore_geom
            x = max(0, cursor_x_root - w // 2)
        self._maximized = False
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self._sync_max_button()
        self.root.after(20, self._refresh_corners)

    # ── min/max/close ────────────────────────────────────────────────────────
    def _maximize(self):
        if self._maximized: return
        self._restore_geom = (self.root.winfo_x(), self.root.winfo_y(),
                              self.root.winfo_width(), self.root.winfo_height())
        wa = get_work_area()
        x, y, r, b = wa
        self._maximized = True
        self.root.geometry(f"{r - x}x{b - y}+{x}+{y}")
        self._sync_max_button()
        # No rounded corners while maximized — Windows-native behavior.
        self.root.after(20, lambda: clear_window_region(self.root))

    def _restore_window(self):
        if not self._maximized: return
        if not self._restore_geom: return
        x, y, w, h = self._restore_geom
        self._maximized = False
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self._sync_max_button()
        self.root.after(20, self._refresh_corners)

    def _toggle_maximize(self):
        if self._maximized: self._restore_window()
        else:               self._maximize()

    def _sync_max_button(self):
        # Use the glyph set we picked in _build_title_bar (MDL2 / Fluent /
        # Unicode fallback). Index 1 = maximize, index 2 = restore.
        try:
            G_MIN, G_MAX, G_RESTORE, G_CLOSE = self._title_glyphs
            self.btn_max.configure(text=G_RESTORE if self._maximized else G_MAX)
        except Exception:
            pass

    def _minimize(self):
        # Tk's iconify() is broken with overrideredirect(True) on Windows —
        # delegate to the platform helper, which uses Win32 ShowWindow.
        minimize_window(self.root)

    def _refresh_corners(self):
        """Apply DWM rounded corners; fall back to GDI region clip if needed."""
        if self._maximized:
            clear_window_region(self.root)
            return
        round_window_corners(self.root)
        # Belt-and-suspenders for Win10 / failed DWM call:
        set_window_region_rounded(self.root, radius=10)

    # ── HOME VIEW ────────────────────────────────────────────────────────────
    def _build_home(self):
        # Pill state machine — drives _set_bar_state_*. "idle" is editable
        # GET-button mode; "busy" is mid-download with progress; "done" is
        # post-download with the filename in the entry and a + LIB action.
        self._bar_state = "idle"

        self.home_frame = tk.Frame(self.container, bg=BG)
        self.home_frame.columnconfigure(0, weight=1)

        # Row 0: bar pill + book button
        self.bar_row = tk.Frame(self.home_frame, bg=BG)
        self.bar_row.grid(row=0, column=0, sticky="ew",
                          padx=12, pady=(12, 12))

        self.input_card = RoundedCard(self.bar_row, bg=BG2, radius=14,
                                      height=self.BAR_PILL_H)
        self.input_card.pack(side="left", fill="x", expand=True)
        inner = self.input_card.inner
        inner.configure(bg=BG2)

        self.kind_stripe = tk.Frame(inner, bg=BORDER, width=3)
        self.kind_stripe.pack(side="left", fill="y", padx=(10, 8), pady=10)

        self.entry = tk.Entry(inner, bg=BG2, fg=TEXT, insertbackground=ACCENT,
                              relief="flat", borderwidth=0, font=self.f_input,
                              highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=2)
        self._set_placeholder()
        self.entry.bind("<FocusIn>",   self._on_focus_in)
        self.entry.bind("<FocusOut>",  self._on_focus_out)
        self.entry.bind("<KeyRelease>", self._on_input)
        self.entry.bind("<Return>",    lambda e: self._on_action_click())
        self.entry.bind("<Escape>",    lambda e: self._on_escape())
        # Clear the placeholder *before* the keystroke/paste lands, so the
        # first character doesn't get appended to "Paste a URL…" with the
        # placeholder fg still in effect. FocusIn alone isn't enough — after
        # _dismiss_chip the entry can already have focus, so no FocusIn fires
        # when the user starts typing.
        self.entry.bind("<KeyPress>",  self._on_keypress, add="+")
        self.entry.bind("<<Paste>>",   self._on_paste,    add="+")

        self.action_btn = RoundedButton(
            inner, text="GET", command=self._on_action_click,
            bg=ACCENT, fg="#000", hover_bg=ACCENT_D,
            font=self.f_btn, padx=10, pady=4, radius=8, min_width=52,
        )
        # Slightly inset from the pill so its corners breathe.
        self.action_btn.configure(height=self.BAR_PILL_H - 14)
        self.action_btn.pack(side="right", padx=(0, 6), pady=7)

        # In-pill close button — visible only in DONE state, lets the user
        # dismiss the post-download chip without moving away from the pill.
        # Packed (and unpacked) by _set_bar_state_done / _idle.
        self.pill_close = tk.Label(
            inner, text="\u00D7", bg=BG2, fg=MUTED, cursor="hand2",
            font=(self.f_btn[0], 14, "bold"), padx=6,
        )
        self.pill_close.bind("<Button-1>", lambda e: self._dismiss_chip())
        self.pill_close.bind("<Enter>",
            lambda e: self.pill_close.configure(fg=TEXT))
        self.pill_close.bind("<Leave>",
            lambda e: self.pill_close.configure(fg=MUTED))
        # Not packed initially.

        # Inline progress strip — 2px tall canvas placed at the bottom edge
        # of the pill, inset to clear the rounded corners. Replaces the
        # separate status_row that used to expand the window.
        self.bar_progress = tk.Canvas(
            inner, height=2, bg=BG2,
            highlightthickness=0, bd=0, takefocus=0,
        )
        # rely=1, y=-3 = 3px above the bottom edge; relx/relwidth inset
        # symmetrically so the strip stays inside the rounded corner radius.
        self.bar_progress.place(relx=0.025, rely=1.0, y=-3,
                                 relwidth=0.95, height=2)
        self.bar_progress.bind("<Configure>", lambda e: self._draw_bar_progress())

        # Square library button (three bars), same height as the pill.
        self.lib_icon_btn = RoundedButton(
            self.bar_row, text="\u2261", command=self._show_library,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            font=self.f_libicon, padx=10, pady=4, radius=14, min_width=self.BAR_PILL_H,
        )
        self.lib_icon_btn.configure(height=self.BAR_PILL_H, width=self.BAR_PILL_H)
        self.lib_icon_btn.pack(side="left", padx=(8, 0))

        # Row 1 (status_row) and Row 2 (chip_card) are constructed but
        # NEVER gridded under Option A. They live as logical containers we
        # still write to (status text, chip files) so existing code paths
        # continue to work, but visually everything shows inside the pill.
        self.status_row = tk.Frame(self.home_frame, bg=BG)
        self.status = tk.Label(self.status_row, text="", bg=BG, fg=SOFT,
                               font=self.f_status, anchor="w")
        self.status.pack(fill="x", padx=2, pady=(0, 4))
        self.prog_canvas = tk.Canvas(self.status_row, height=6, bg=BG,
                                     highlightthickness=0, bd=0)
        self.prog_canvas.pack(fill="x", padx=2)
        self.prog_canvas.bind("<Configure>", lambda e: self._redraw_progress())
        # Intentionally not gridded.

        # Row 2: completion chip — also not gridded. Kept for the
        # _on_chip_add_click menu code which references self.last_files.
        self.chip_card = RoundedCard(self.home_frame, bg=BG2, radius=12, height=46)
        chip = self.chip_card.inner
        chip.configure(bg=BG2)
        tk.Label(chip, text="\u2713", bg=BG2, fg=ACCENT,
                 font=(self.f_btn[0], 12, "bold")).pack(side="left", padx=(12, 6))
        self.chip_label = tk.Label(chip, text="", bg=BG2, fg=TEXT,
                                    font=self.f_input, anchor="w")
        self.chip_label.pack(side="left", fill="x", expand=True)
        self.chip_add_btn = RoundedButton(
            chip, text="+ ADD TO LIBRARY", command=self._on_chip_add_click,
            bg=ACCENT, fg="#000", hover_bg=ACCENT_D,
            font=self.f_chip, padx=10, pady=4, radius=10,
        )
        self.chip_add_btn.pack(side="left", padx=(8, 4))
        self.chip_close = tk.Label(chip, text="\u00D7", bg=BG2, fg=MUTED,
                                    font=(self.f_btn[0], 14), cursor="hand2",
                                    padx=10)
        self.chip_close.pack(side="left")
        self.chip_close.bind("<Button-1>", lambda e: self._dismiss_chip())
        self.chip_close.bind("<Enter>", lambda e: self.chip_close.configure(fg=TEXT))
        self.chip_close.bind("<Leave>", lambda e: self.chip_close.configure(fg=MUTED))

        # Initial state: idle. No status row, no chip — pill is editable
        # and ready for a paste. Window stays at BAR_H_S forever in bar mode.
        self._set_bar_state_idle()

    # ── pill state machine (Option A) ────────────────────────────────────────
    def _set_bar_state_idle(self):
        """Editable pill, GET button, no progress, no dismiss."""
        self._bar_state = "idle"
        self._pct = 0
        self._stop_busy_dots()
        # Entry: editable, restore placeholder if empty
        try:
            self.entry.configure(state="normal", fg=TEXT)
            self._on_input(None)  # refresh kind_stripe color from current text
        except Exception:
            pass
        # Action button: GET
        try:
            self.action_btn.set_state(text="GET", bg=ACCENT, fg="#000",
                                       hover_bg=ACCENT_D, enabled=True)
        except Exception:
            pass
        # Hide the pill_close
        try: self.pill_close.pack_forget()
        except Exception: pass
        # Clear the progress strip
        self._draw_bar_progress()

    BUSY_GREEN  = "#22c55e"   # active / in-progress
    BUSY_GREEN_HOVER = "#16a34a"
    # Dots-animation frames. No padding — Tk centers the bounding box of
    # whatever string we hand it, so any leading/trailing space shifts the
    # dot mass off-center. Bare dots: each frame's bbox is centered on the
    # button, so the middle of the dot row always sits at the button's
    # center even as the count cycles 1→2→3→2.
    _BUSY_DOT_FRAMES = ("·", "··", "···", "··")

    def _start_busy_dots(self):
        """Kick off the cycling dots label on action_btn. Idempotent — cancels
        any prior loop before starting a fresh one."""
        self._stop_busy_dots()
        self._busy_dots_i = 0
        self._tick_busy_dots()

    def _tick_busy_dots(self):
        # Stop if we left busy state between scheduling and firing.
        if self._bar_state != "busy":
            self._busy_dots_after = None
            return
        frame = self._BUSY_DOT_FRAMES[self._busy_dots_i % len(self._BUSY_DOT_FRAMES)]
        try: self.action_btn.set_state(text=frame)
        except Exception: pass
        self._busy_dots_i += 1
        self._busy_dots_after = self.root.after(280, self._tick_busy_dots)

    def _stop_busy_dots(self):
        after = getattr(self, "_busy_dots_after", None)
        if after:
            try: self.root.after_cancel(after)
            except Exception: pass
        self._busy_dots_after = None

    def _set_bar_state_busy(self, phase=None):
        """Green action button with a cycling-dots label so the user reads
        'something is happening right now'. Click still cancels via the
        existing `if self.current: self.current.cancel()` branch."""
        self._bar_state = "busy"
        try:
            self.kind_stripe.configure(bg=self.BUSY_GREEN)
        except Exception:
            pass
        try:
            self.action_btn.set_state(text="·", bg=self.BUSY_GREEN, fg="#000",
                                       hover_bg=self.BUSY_GREEN_HOVER, enabled=True)
        except Exception:
            pass
        # Kick off the dots animation. Cancelled by _set_bar_state_idle /
        # _set_bar_state_done so we don't leave a stray after() loop running.
        self._start_busy_dots()
        try: self.pill_close.pack_forget()
        except Exception: pass
        # Progress strip becomes visible as _set_progress() pushes pct.

    def _set_bar_state_done(self, files):
        """Filename in entry (read-only), green stripe, + LIB button, × dismiss.

        Replaces the old chip_card popup with an inline state on the pill,
        so the window stays small."""
        self._bar_state = "done"
        self._stop_busy_dots()
        # Pick a display title — first file's stem, truncated.
        title = "Saved"
        if files:
            try:
                from pathlib import Path as _P
                t = _P(files[0]).stem
                if len(t) > 36:
                    t = t[:33] + "\u2026"
                title = t
            except Exception:
                title = str(files[0])
        # Entry: show filename, lock against editing.
        try:
            self.entry.configure(state="normal", fg=TEXT)
            self.entry.delete(0, "end")
            self.entry.insert(0, title)
            self.entry.configure(state="readonly", readonlybackground=BG2)
        except Exception:
            pass
        # Kind stripe: success green.
        try:
            self.kind_stripe.configure(bg="#4ade80")
        except Exception:
            pass
        # Action button: + LIB. Click dispatches to _on_chip_add_click via
        # _on_action_click (which now branches on _bar_state).
        try:
            self.action_btn.set_state(text="+ LIB", bg=ACCENT, fg="#000",
                                       hover_bg=ACCENT_D, enabled=True)
        except Exception:
            pass
        # Show the dismiss × to the immediate left of the action button.
        # No `before=` — Tk's pack manager processes side="right" widgets
        # in pack-list order, rightmost first. Since action_btn is already
        # in the list, pill_close gets the next slot to the LEFT.
        try:
            self.pill_close.pack(side="right", padx=(0, 6))
        except Exception:
            pass
        # Progress strip stays full at 100% as a subtle "done" indicator.
        self._pct = 100
        self._draw_bar_progress()

    # ── LIBRARY VIEW ─────────────────────────────────────────────────────────
    def _build_library(self):
        self.library_frame = tk.Frame(self.container, bg=BG)

        # Header: back + title + clear
        self.lib_head = tk.Frame(self.library_frame, bg=BG)
        self.lib_head.pack(fill="x", padx=12, pady=(12, 0))

        self.back_btn = RoundedButton(
            self.lib_head, text="← BACK", command=self._on_back_pressed,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            font=self.f_back, padx=12, pady=6, radius=10,
        )
        self.back_btn.pack(side="left")

        self.lib_title_lbl = tk.Label(self.lib_head, text="LIBRARY", bg=BG, fg=TEXT,
                                       font=self.f_h1)
        self.lib_title_lbl.pack(side="left", padx=(14, 0))

        self.clear_btn = tk.Label(self.lib_head, text="clear", bg=BG, fg=MUTED,
                                   font=self.f_meta, cursor="hand2")
        self.clear_btn.pack(side="right")
        self.clear_btn.bind("<Button-1>", lambda e: self._clear_library())
        self.clear_btn.bind("<Enter>", lambda e: self.clear_btn.configure(fg=ERROR))
        self.clear_btn.bind("<Leave>", lambda e: self.clear_btn.configure(fg=MUTED))

        # Tabs row: tabs on the left, "+ ADD" button anchored to the far right
        self.tabs_row = tk.Frame(self.library_frame, bg=BG)
        self.tabs_row.pack(fill="x", padx=12, pady=(10, 10))

        # Right side: the action buttons cluster — order is (right→left):
        # [+ import]  [layout]  [search]  [settings]   tabs...
        # Each is packed side="right" in reverse visual order.
        self.import_btn = IconButton(
            self.tabs_row, icon_name="plus", command=self._on_import_click,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            width=42, height=30, radius=10, icon_size=18,
        )
        self.import_btn.pack(side="right")

        self.layout_btn = IconButton(
            self.tabs_row,
            icon_name="grid" if self.lib_layout == "list" else "list",
            command=self._toggle_lib_layout,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            width=42, height=30, radius=10, icon_size=18,
        )
        self.layout_btn.pack(side="right", padx=(0, 6))

        # Selection-mode toggle. When active, tiles get a corner checkbox
        # and clicks toggle selection. Highlighted (active=True) while on
        # so it reads as a sticky state, not a one-shot action.
        self.select_btn = IconButton(
            self.tabs_row, icon_name="check",
            command=self._toggle_selection_mode,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            active_bg=ACCENT, active_fg="#000",
            width=42, height=30, radius=10, icon_size=18,
            active=False,
        )
        self.select_btn.pack(side="right", padx=(0, 6))

        self.search_btn = IconButton(
            self.tabs_row, icon_name="search", command=self._toggle_search,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            width=42, height=30, radius=10, icon_size=18,
        )
        self.search_btn.pack(side="right", padx=(0, 6))

        self.settings_btn = IconButton(
            self.tabs_row, icon_name="gear", command=self._open_settings,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            width=42, height=30, radius=10, icon_size=18,
        )
        self.settings_btn.pack(side="right", padx=(0, 6))

        # Search bar (collapsed by default). When opened, replaces the tab
        # chips area with an Entry filtering the active library's items.
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            self.tabs_row, textvariable=self.search_var,
            bg=BG2, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=self.f_label, bd=0,
        )
        self.search_entry.bind("<KeyRelease>", lambda e: self._apply_search())
        self.search_entry.bind("<Escape>",     lambda e: self._toggle_search())
        # Not packed by default

        # Left side: the tab chips live in their own sub-frame so they can
        # flow freely without being pushed by the import button.
        self.tabs_chip_row = tk.Frame(self.tabs_row, bg=BG)
        self.tabs_chip_row.pack(side="left", fill="x", expand=True)

        self._search_active = False
        self._search_query  = ""

        # Body host — swap between grid and feed
        self.lib_body = tk.Frame(self.library_frame, bg=BG)
        self.lib_body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # Grid sub-view: scrollable canvas with thumbnail tiles
        self.grid_frame = tk.Frame(self.lib_body, bg=BG)
        # confine=True (Tk default, but explicit here) prevents yview from
        # ever moving outside scrollregion. Combined with our top-clamp in
        # _set_grid_scrollregion this kills the scroll-into-the-void bug.
        self.grid_canvas = tk.Canvas(self.grid_frame, bg=BG,
                                      highlightthickness=0, bd=0,
                                      confine=True)
        self.grid_canvas.pack(side="left", fill="both", expand=True)

        self.grid_inner = tk.Frame(self.grid_canvas, bg=BG)
        self.grid_window = self.grid_canvas.create_window(
            (0, 0), window=self.grid_inner, anchor="nw"
        )
        self.grid_inner.bind(
            "<Configure>",
            lambda e: self._set_grid_scrollregion(),
        )
        self.grid_canvas.bind("<Configure>", self._on_grid_resize)

        # Feed sub-view: the existing FeedPlayer
        self.feed_frame = tk.Frame(self.lib_body, bg=BG)
        self.feed = FeedPlayer(self.feed_frame, self)
        self.feed.frame.pack(fill="both", expand=True)

        # Default: grid visible, feed hidden
        self.grid_frame.pack(fill="both", expand=True)
        self.lib_view = "grid"

        # ── Selection-mode action bar (hidden by default) ────────────────
        # Built once; pack/forget toggles its visibility. Lives at the
        # bottom of the library_frame so it stays anchored as the user
        # scrolls through the grid.
        self._build_select_toolbar()

        # NOTE: self.lib_loading is constructed at container level (in
        # _build_main) so it can cover the entire bar→library transition,
        # not just in-library re-renders. _show_lib_loading places it.

        # Caches
        self.thumbs   = ThumbnailCache()
        self._tile_widgets = []   # (tile_card, thumb_label, photo_ref)
        self._photo_refs   = []   # keep PhotoImage refs alive
        self._grid_cols    = 2
        self._last_grid_w  = 0

        self._bind_library_keys()

    def _show_lib_loading(self):
        """Cover lib_body with the loading overlay. Forces an idle-task
        flush so the overlay actually paints before any heavy work."""
        try:
            self.lib_loading.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.lib_loading.lift()
            self.root.update_idletasks()
        except Exception:
            pass

    def _hide_lib_loading(self):
        """Take the loading overlay back off so the user sees content."""
        try:
            self.lib_loading.place_forget()
        except Exception:
            pass

    def _bind_library_keys(self):
        """Wheel scrolls the grid; navigates the feed."""
        # Widget-level wheel handler for the grid view. Pre-clamps at the
        # top edge so we never call yview_scroll when we're already at y=0
        # (which would briefly overshoot into negative space, leaving a
        # visible empty row above the first tile until confine=True kicks
        # in). Returns "break" to suppress Tk's default Canvas class
        # binding — without this, every wheel notch fires twice (class
        # binding + our handler) and effectively scrolls 2x.
        def on_grid_wheel(event):
            if self.mode != "library":
                return
            if getattr(self, "_active_hover", None) is not None:
                self._cancel_all_hovers()
            delta = event.delta if hasattr(event, "delta") and event.delta else 0
            step = -int(delta / 40) if sys.platform != "darwin" else -int(delta)
            if not step:
                return "break"
            # Hard stop at top: skip the scroll entirely if already at y=0
            # and the user is trying to scroll up. Tk's confine=True clamps
            # AFTER the scroll, which means a single frame of overshoot is
            # still visible. Pre-clamping eliminates that frame.
            if step < 0:
                try:
                    if self.grid_canvas.yview()[0] <= 0.001:
                        return "break"
                except Exception:
                    pass
            self.grid_canvas.yview_scroll(step, "units")
            # Belt and braces — clamp after scroll too.
            try:
                if self.grid_canvas.yview()[0] < 0:
                    self.grid_canvas.yview_moveto(0)
            except Exception:
                pass
            return "break"

        # Bind directly on the canvas and the inner frame. Tile widgets are
        # children of grid_inner — wheel events on them propagate up to
        # grid_inner, where this binding catches and breaks them.
        self.grid_canvas.bind("<MouseWheel>", on_grid_wheel)
        self.grid_inner.bind("<MouseWheel>",  on_grid_wheel)
        self.grid_canvas.bind("<Button-4>",   lambda e: on_grid_wheel(
            type("E", (), {"delta": 120})()) or "break")
        self.grid_canvas.bind("<Button-5>",   lambda e: on_grid_wheel(
            type("E", (), {"delta": -120})()) or "break")

        # bind_all wheel handler — only fires when wheel happens OUTSIDE
        # grid_canvas/grid_inner (e.g. over the back button area), or when
        # we're in feed mode. Grid mode wheels are handled by on_grid_wheel
        # above, which returns "break" to stop bind_all from also firing.
        def on_wheel(event):
            if self.mode != "library":
                return
            if self.lib_view == "grid":
                return  # grid wheel is handled by on_grid_wheel
            delta = event.delta if hasattr(event, "delta") and event.delta else 0
            if delta == 0:
                return
            now = time.time()
            if now - getattr(self, "_last_wheel", 0) < 0.18:
                return
            self._last_wheel = now
            if delta > 0: self.feed.prev()
            else:         self.feed.next()

        # Escape = back-button shortcut while in library mode. Library mode
        # only — bar mode's Escape clears the entry / dismisses DONE state
        # via the Entry's own <Escape> binding.
        def on_escape_global(_event=None):
            if self.mode != "library":
                return
            # If a settings panel is open, let it close itself first.
            if getattr(self, "_settings_panel", None) is not None:
                return
            # Selection mode catches Escape before falling back to "go back".
            # Otherwise hitting Escape with a 30-item selection would dump
            # the user out of the library entirely, which is way too easy.
            if self._selection_mode:
                self._exit_selection_mode()
                return
            self._on_back_pressed()

        self.root.bind_all("<MouseWheel>", on_wheel, add="+")
        self.root.bind_all("<Button-4>",
                           lambda e: self._wheel_up() if self.mode == "library" else None,
                           add="+")
        self.root.bind_all("<Button-5>",
                           lambda e: self._wheel_down() if self.mode == "library" else None,
                           add="+")
        self.root.bind_all("<Key-Escape>", on_escape_global, add="+")
        self.root.bind_all("<space>",
                           lambda e: self.feed._toggle_pause()
                           if self.mode == "library" and self.lib_view == "feed"
                           else None,
                           add="+")
        self.root.bind_all("<m>",
                           lambda e: self.feed.toggle_mute()
                           if self.mode == "library" and self.lib_view == "feed"
                           else None,
                           add="+")

        # Grid-view keyboard navigation. These check lib_view inside the
        # handler so they don't fight the feed-view arrow handlers.
        self.root.bind_all("<Up>",     self._grid_key_up,     add="+")
        self.root.bind_all("<Down>",   self._grid_key_down,   add="+")
        self.root.bind_all("<Left>",   self._grid_key_left,   add="+")
        self.root.bind_all("<Right>",  self._grid_key_right,  add="+")
        self.root.bind_all("<Return>", self._grid_key_enter,  add="+")
        self.root.bind_all("<Delete>", self._grid_key_delete, add="+")

    # ── grid keyboard navigation ─────────────────────────────────────────────
    def _grid_active(self):
        return self.mode == "library" and self.lib_view == "grid"

    def _grid_key_up(self, _e=None):
        if not self._grid_active(): return
        cols = self._grid_cols if self.lib_layout == "grid" else 1
        self._move_selection(-cols)

    def _grid_key_down(self, _e=None):
        if not self._grid_active(): return
        cols = self._grid_cols if self.lib_layout == "grid" else 1
        self._move_selection(+cols)

    def _grid_key_left(self, _e=None):
        if not self._grid_active(): return
        # In list mode left/right are no-ops (single column).
        if self.lib_layout == "list": return
        self._move_selection(-1)

    def _grid_key_right(self, _e=None):
        if not self._grid_active(): return
        if self.lib_layout == "list": return
        self._move_selection(+1)

    def _grid_key_enter(self, _e=None):
        if not self._grid_active(): return
        idx = self._selected_idx
        if idx is None:
            # Nothing selected — select the first tile so the next press works.
            self._set_selection(0)
            return
        self._open_feed_at(idx)

    def _grid_key_delete(self, _e=None):
        if not self._grid_active(): return
        idx = self._selected_idx
        if idx is None: return
        items = self.library.items_in(self.library.active)
        if not (0 <= idx < len(items)): return
        # Confirm so accidental Delete doesn't nuke an item silently.
        title = items[idx].get("title", "this item")
        ok = messagebox.askyesno(
            "Drop", f"Remove \u201c{title}\u201d from this library?",
            parent=self.root,
        )
        if not ok: return
        self.library.remove(self.library.active, idx)
        # Adjust selection so it stays on something visible
        new_count = len(self.library.items_in(self.library.active))
        if new_count == 0:
            self._selected_idx = None
        else:
            self._selected_idx = min(idx, new_count - 1)
        self._render_grid()
        if self._selected_idx is not None:
            self._highlight_selection()

    def _move_selection(self, delta):
        items = self.library.items_in(self.library.active)
        if not items: return
        if self._selected_idx is None:
            new_idx = 0
        else:
            new_idx = self._selected_idx + delta
            new_idx = max(0, min(new_idx, len(items) - 1))
        self._set_selection(new_idx)

    def _set_selection(self, idx):
        if not (0 <= idx < len(self.library.items_in(self.library.active))):
            return
        # Clear previous highlight
        self._clear_selection_highlight()
        self._selected_idx = idx
        self._highlight_selection()
        self._scroll_selection_into_view()

    def _clear_selection_highlight(self):
        """Remove any visual highlight from the previously selected tile."""
        if self._selected_idx is None: return
        prev = self._tiles_by_idx.get(self._selected_idx)
        if prev is None: return
        try:
            # RoundedCard exposes its rect via tag "rect".
            prev.itemconfigure("rect", outline="", width=0)
        except Exception:
            pass

    def _highlight_selection(self):
        """Draw an accent border around the selected tile."""
        idx = self._selected_idx
        if idx is None: return
        card = self._tiles_by_idx.get(idx)
        if card is None: return
        try:
            card.itemconfigure("rect", outline=ACCENT, width=2)
        except Exception:
            pass

    def _scroll_selection_into_view(self):
        """Make sure the selected tile is visible in the scrollable canvas."""
        idx = self._selected_idx
        if idx is None: return
        card = self._tiles_by_idx.get(idx)
        if card is None: return
        try:
            self.grid_inner.update_idletasks()
            # Compute the card's y range relative to the inner frame.
            card_y  = card.winfo_y()
            card_h  = card.winfo_height()
            inner_h = max(self.grid_inner.winfo_height(), 1)
            top    = card_y / inner_h
            bottom = (card_y + card_h) / inner_h
            # Current visible range
            cur_top, cur_bot = self.grid_canvas.yview()
            if top < cur_top:
                self.grid_canvas.yview_moveto(max(0, top - 0.02))
            elif bottom > cur_bot:
                # Scroll so card_bottom aligns with viewport bottom
                viewport_h = cur_bot - cur_top
                self.grid_canvas.yview_moveto(max(0, bottom - viewport_h + 0.02))
        except Exception:
            pass

    def _wheel_up(self):
        if self.lib_view == "grid":
            self.grid_canvas.yview_scroll(-2, "units")
        else:
            self.feed.prev()

    def _wheel_down(self):
        if self.lib_view == "grid":
            self.grid_canvas.yview_scroll(2, "units")
        else:
            self.feed.next()

    def _on_grid_resize(self, event):
        # Keep inner frame width matched to canvas (cheap, do every time)
        self.grid_canvas.itemconfigure(self.grid_window, width=event.width)
        new_cols = max(2, min(4, event.width // 220))
        # Cancel any pending re-render
        if getattr(self, "_grid_resize_after", None):
            try: self.root.after_cancel(self._grid_resize_after)
            except Exception: pass
            self._grid_resize_after = None
        # Schedule a debounced re-render
        self._pending_grid_w = event.width
        self._pending_grid_cols = new_cols
        self._grid_resize_after = self.root.after(160, self._flush_grid_resize)

    def _flush_grid_resize(self):
        self._grid_resize_after = None
        if self.lib_view != "grid":
            return
        # Honor the transition lock — _show_library will render itself.
        if getattr(self, "_render_locked", False):
            return
        # In list mode, re-render so the row widths track the canvas width.
        # We only do this when the width changed by a meaningful amount to
        # avoid spam during a continuous drag.
        if self.lib_layout == "list":
            new_w = self._pending_grid_w
            if abs(new_w - self._last_grid_w) > 32:
                self._last_grid_w = new_w
                self._render_grid()
            else:
                self.grid_inner.update_idletasks()
                self._set_grid_scrollregion()
            return
        new_cols = self._pending_grid_cols
        new_w    = self._pending_grid_w
        if new_cols != self._grid_cols:
            self._grid_cols   = new_cols
            self._last_grid_w = new_w
            self._render_grid()
        else:
            self._last_grid_w = new_w
            self.grid_inner.update_idletasks()
            self._set_grid_scrollregion()

    def _set_grid_scrollregion(self):
        """Set grid_canvas's scrollregion from the inner frame's required
        height — not bbox('all'). bbox can include items that briefly have
        out-of-frame coordinates during a re-render, which is what made
        scrollregion's top dip negative and let the user scroll into the
        void above the content. winfo_reqheight reads the geometry-manager's
        target size for the frame, which never goes negative.

        Top is always pinned to 0; right is the canvas width. Combined with
        confine=True on the canvas, this hard-stops scroll-above-content."""
        try:
            self.grid_inner.update_idletasks()
            h = max(self.grid_inner.winfo_reqheight(), 1)
            w = max(self.grid_canvas.winfo_width(), 1)
            self.grid_canvas.configure(scrollregion=(0, 0, w, h))
            # If a recent yview drifted into negative space (race against
            # a re-render), pull it back to 0 explicitly.
            top = self.grid_canvas.yview()[0]
            if top < 0:
                self.grid_canvas.yview_moveto(0)
        except Exception:
            pass

    # ── back button: contextual ──────────────────────────────────────────────
    def _on_back_pressed(self):
        # In-flight gate. Stays True for the full transition AND for a
        # grace window after, so a "did-it-work?" second tap landing 200-
        # 400ms after the first can't ride the gap and chain feed→grid→
        # home in one perceived press.
        if getattr(self, "_back_in_flight", False):
            return
        self._back_in_flight = True

        # Any lingering center_hint text on the feed (PAUSED, seek arrow,
        # mute toast) would otherwise hover over the surface for the 120ms
        # the after() schedules. Hiding it up front means the back press
        # reads as one clean swap instead of "PAUSED text briefly then
        # next view."
        if hasattr(self, "feed"):
            try: self.feed.center_hint.place_forget()
            except Exception: pass

        # Route off what's actually mapped on the screen, not lib_view.
        # The flag can drift out of sync with reality if a background
        # event (e.g. a library re-render triggered by a finished
        # download or a refresh) calls _show_grid while we're in feed
        # mode. winfo_ismapped() always tells the truth about what the
        # user is currently looking at.
        feed_visible = False
        try:
            feed_visible = self.feed_frame.winfo_ismapped()
        except Exception:
            pass
        target = self._show_grid if feed_visible else self._show_home

        def _run():
            try: target()
            finally:
                # Hold the lock-out 250ms past the transition. The button
                # flash is ~110ms; if the user clicks twice within a single
                # "did anything happen?" moment, both clicks land inside
                # this window and only the first is honored.
                self.root.after(250, self._clear_back_flight)
        self.root.after(120, _run)

    def _clear_back_flight(self):
        self._back_in_flight = False


    # ── view switching ───────────────────────────────────────────────────────
    def _show_home(self):
        if self.home_frame.winfo_ismapped():
            return
        # If the library was maximized, restore the window first — bar mode
        # is locked at 540×108 and a maximized borderless window with a
        # tiny pill marooned in the corner looks broken. Restoring before
        # the mode switch means _apply_mode_geometry will then size us
        # correctly to the small bar window.
        if self._maximized:
            try: self._restore_window()
            except Exception: pass
        # Stop playback when leaving the library — both feed and hover preview.
        if hasattr(self, "feed"):
            self.feed.stop()
        self._cancel_all_hovers()
        # Close settings if open
        if getattr(self, "_settings_panel", None) is not None:
            self._close_settings()
        try: self.library_frame.pack_forget()
        except Exception: pass
        self.home_frame.pack(fill="both", expand=True)
        self.mode = "bar"
        self.root.update_idletasks()
        self._apply_mode_geometry()

    def _show_library(self):
        if self.library_frame.winfo_ismapped():
            return
        # Show the loading overlay BEFORE we tear down the home view. With
        # the overlay parented on container, it can cover home_frame, the
        # window resize, the empty library_frame, AND the heavy render —
        # the user never sees any intermediate state, just "Loading…" then
        # the finished library.
        self._show_lib_loading()
        try: self.home_frame.pack_forget()
        except Exception: pass
        # Mode change must come BEFORE rendering so geometry helpers know.
        self.mode = "library"
        if self.lib_view != "grid":
            try: self.feed_frame.pack_forget()
            except Exception: pass
            self.feed.stop()
            self.grid_frame.pack(fill="both", expand=True)
            self.lib_view = "grid"

        # Pre-compute the target column count from the geometry we're about
        # to apply, so _render_grid produces tiles at the correct size on
        # the FIRST paint. This kills the empty-grid flash you'd otherwise
        # see while Tk grows the window.
        target_w = self._geom.get("lib_w", self.LIB_W)
        # Grid canvas inner width ≈ target window width minus side padding.
        self._grid_cols = max(2, min(4, max(target_w - 24, 320) // 220))

        # Suppress resize-driven re-renders while we're transitioning.
        self._render_locked = True

        # Pack and resize. Loading overlay stays lifted on top throughout.
        self.library_frame.pack(fill="both", expand=True)
        self._apply_mode_geometry()
        # Re-lift the loading overlay — packing library_frame can re-stack.
        try: self.lib_loading.lift()
        except Exception: pass
        self.root.update_idletasks()

        # Render. _render_library defers via after_idle so the loading
        # screen actually paints before any blocking work begins.
        self._render_library()

        self._render_locked = False

    # ── input behavior ───────────────────────────────────────────────────────
    def _set_placeholder(self):
        if not self.entry.get():
            self.entry.insert(0, self.PLACEHOLDER)
            self.entry.configure(fg=MUTED)

    def _clear_placeholder(self):
        if self.entry.get() == self.PLACEHOLDER:
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)

    def _on_focus_in(self, _e):
        # If we're in DONE state and the user clicks back into the entry,
        # they want to start a new URL — auto-dismiss the post-download
        # state and let them type. Without this, the readonly filename
        # would block input until they clicked × first.
        if self._bar_state == "done":
            self._dismiss_chip()
            # _dismiss_chip resets to idle; fall through to normal focus.
        self._clear_placeholder()
        # No clipboard auto-paste — user types or presses Ctrl+V (Tk handles).

    def _on_focus_out(self, _e):
        self._set_placeholder()

    def _on_keypress(self, e):
        # Drop the placeholder right before a key would insert into the entry.
        # Skip Ctrl-chords (Ctrl+V is handled by <<Paste>> below; Ctrl+A et al
        # don't insert) and pure-modifier presses (Shift, arrows) whose
        # `e.char` is empty.
        if e.state & 0x4:  # Control modifier bit
            return
        if not e.char:
            return
        self._clear_placeholder()

    def _on_paste(self, _e):
        self._clear_placeholder()

    def _on_input(self, _e):
        url = self.entry.get().strip()
        if url == self.PLACEHOLDER: url = ""
        kind = detect_kind(url)
        self.kind_stripe.configure(bg=KIND_COLORS.get(kind, BORDER))

    def _on_action_click(self):
        # In DONE state the action button is "+ LIB" — opens the library
        # picker via the existing chip-add menu code path, which already
        # knows how to read self.last_files.
        if self._bar_state == "done":
            self._on_chip_add_click()
            return
        if self.current: self.current.cancel()
        else:            self._start_download()

    def _on_escape(self):
        if self.current:
            self.current.cancel()
        elif self._bar_state == "done":
            self._dismiss_chip()
        else:
            self.entry.delete(0, "end")
            self._on_input(None)

    def _start_download(self):
        url = self.entry.get().strip()
        if not url or url == self.PLACEHOLDER: return
        if not url.startswith(("http://", "https://")):
            self._show_status("Enter a valid URL", fg=ERROR)
            return

        # Switch the pill into BUSY state — STOP button, accent stripe,
        # progress strip starts filling. Window stays at BAR_H_S.
        self._set_bar_state_busy(phase="Starting")
        self._show_status("Starting\u2026", fg=SOFT)
        self._set_progress(0)

        self.current = Downloader(
            url=url,
            on_progress=lambda p: self.root.after(0, self._on_progress, p),
            on_done    =lambda r, _u=url: self.root.after(0, self._on_done, _u, r),
        )
        self.current.start()

    def _on_progress(self, p):
        msg = p.get("msg")
        if msg:
            self._show_status(msg, fg=SOFT)
        pct   = p.get("pct")
        phase = p.get("phase")
        # Reserve the top 10% of the bar for post-processing (audio extract,
        # merge) which yt-dlp reports as text-phase lines with no
        # percentage. Without this scaling the bar hits 100% then sits
        # there for several seconds during encoding — looks frozen.
        if pct is not None:
            self._set_progress(pct * 0.9)
        elif phase in ("Encoding", "Merging"):
            # Post-processing started — show 95% so the user sees the bar
            # advance even though there's no per-percentage signal here.
            self._set_progress(95)
        # No pct, no terminal phase → status message updated above but
        # the bar keeps its current value. _set_progress used to clamp to
        # 0 on None which made the bar flicker back at every "Searching…"
        # / "Fetching…" line; suppressing that here.

    def _on_done(self, url, result):
        self.current = None

        # If we're processing an import queue, auto-add to the active library
        # and advance to the next URL without user interaction.
        is_queue_run = bool(getattr(self, "_import_queue", None) is not None
                            and getattr(self, "_import_total", 0))

        if result["ok"]:
            self._set_progress(100)
            files = list(result.get("files", []))
            self.last_files = files
            self.last_url   = url

            if is_queue_run:
                # In a queue run, the pill flickers through DONE per URL
                # would be noisy — keep it BUSY-looking and just chain.
                self._show_status(
                    f"Imported {self._import_done} / {self._import_total}",
                    fg=ACCENT,
                )
                # Auto-add every imported file to the active library
                for fp in files:
                    try:
                        self._add_to_library(self.library.active, url, fp)
                    except Exception:
                        pass
                # Chain next URL after a short beat
                self.root.after(400, self._advance_import_queue)
            else:
                # Single download: switch the pill into DONE state.
                # Filename in the entry, + LIB button, × dismiss, green stripe.
                self._set_bar_state_done(files)
                # _show_chip is now a thin wrapper that just records files;
                # state transition is what drives the visual.
                self._show_chip(files)
        else:
            # Error: stay in idle (revert from BUSY), surface the message
            # via the status label briefly, no DONE-state visuals.
            self._set_bar_state_idle()
            self._show_status(result["msg"], fg=ERROR)
            if is_queue_run:
                # Skip failed URL and continue
                self.root.after(800, self._advance_import_queue)
            else:
                self.root.after(3000, self._hide_status_if_idle)

    def _advance_import_queue(self):
        """Continue a JSON-import queue. Cleans up state when done."""
        q = getattr(self, "_import_queue", None)
        if q is None: return
        if not q:
            # Queue empty — done.
            total = self._import_total
            self._import_queue = None
            self._import_total = 0
            self._import_done  = 0
            self._show_status(f"Imported {total} URL{'s' if total != 1 else ''}.",
                              fg=ACCENT)
            self.root.after(3500, self._hide_status_if_idle)
            return
        self._next_in_queue()

    def _hide_status_if_idle(self):
        # Locked-pill layout: the status row is never gridded, so there's
        # nothing to hide. Errors clear themselves when the user starts a
        # new download (via _set_bar_state_busy).
        return

    def _set_busy(self, on):
        # Bridge for code paths that still call _set_busy (e.g. external
        # callers). Delegates to the state machine.
        if on:
            self._set_bar_state_busy()
        else:
            # Don't auto-revert to idle — the BUSY → DONE / IDLE decision
            # is made in _on_done based on success/failure.
            pass

    def _show_status(self, text="", fg=SOFT):
        # Status label is no longer visible in the pill; we still update
        # self.status (off-screen widget) so anything that reads it keeps
        # working. Surface phase text via the entry's placeholder while
        # BUSY so the user sees what's happening.
        self.status.configure(text=text or "", fg=fg)
        if self._bar_state == "busy" and text:
            try:
                # Set placeholder-style text in the entry, but only if the
                # user hasn't typed anything since pasting their URL.
                # We don't want to clobber their input.
                cur = self.entry.get()
                # Show the phase as the kind_stripe colour cue alone — the
                # entry text remains the URL the user pasted.
                # (Intentional: no entry overwrite here.)
                _ = cur  # keep ref so linters don't complain
            except Exception:
                pass

    def _refit_bar(self):
        # No-op under Option A — the window is locked at BAR_H_S, no refit
        # needed. Kept as a stub so older callers don't error.
        return

    def _set_progress(self, pct):
        # Treat None as "leave alone" rather than "reset to 0". That way
        # text-phase messages (Resolving, Searching, Encoding) don't reset
        # the bar between data-bearing updates. Callers that genuinely
        # want to reset pass 0 explicitly.
        if pct is not None:
            self._pct = pct
        self._redraw_progress()

    def _redraw_progress(self):
        # Original prog_canvas (off-screen now under Option A — kept harmless
        # so any code that pokes self.prog_canvas doesn't crash).
        self.prog_canvas.delete("all")
        w = max(self.prog_canvas.winfo_width(), 1)
        h = max(self.prog_canvas.winfo_height(), 1)
        self.prog_canvas.create_polygon(_rrect_pts(0, 0, w, h, h // 2),
                                        smooth=True, fill=BG2, outline="")
        if self._pct and self._pct > 0:
            fw = max(int(w * (self._pct / 100.0)), h)
            self.prog_canvas.create_polygon(_rrect_pts(0, 0, fw, h, h // 2),
                                            smooth=True, fill=ACCENT, outline="")
        # New: also paint the in-pill 2px progress strip — this is what
        # the user actually sees under Option A.
        self._draw_bar_progress()

    def _draw_bar_progress(self):
        """Draw the 2px progress strip at the bottom of the pill. Called
        from _redraw_progress and the strip's own <Configure> handler."""
        try:
            self.bar_progress.delete("all")
        except Exception:
            return
        w = max(self.bar_progress.winfo_width(), 1)
        pct = self._pct or 0
        if pct <= 0:
            return
        fw = int(w * pct / 100)
        if fw < 1:
            return
        # Stripe color: green when DONE (matches kind_stripe), accent otherwise.
        color = "#4ade80" if self._bar_state == "done" else ACCENT
        self.bar_progress.create_rectangle(0, 0, fw, 2,
                                            fill=color, outline="")

    # ── completion chip ──────────────────────────────────────────────────────
    def _show_chip(self, files):
        # Under Option A this is now a thin recorder — the visual transition
        # to DONE state happens in _set_bar_state_done called from _on_done.
        # Keep updating chip_label / chip_card content for any code that
        # still reads them (e.g. a future feature reusing the popup).
        self.last_files = files or []
        if not files:
            try: self.chip_label.configure(text="Saved (file location unknown)")
            except Exception: pass
            return
        if len(files) == 1:
            try:
                t = Path(files[0]).stem
                if len(t) > 36: t = t[:33] + "\u2026"
                self.chip_label.configure(text=t)
            except Exception: pass
        else:
            try: self.chip_label.configure(text=f"{len(files)} files saved")
            except Exception: pass

    def _dismiss_chip(self):
        # Reset back to idle pill state — entry editable + empty,
        # placeholder, GET button, neutral stripe, no progress.
        self.last_files = []
        self.last_url   = None
        self._set_progress(0)
        self._set_bar_state_idle()
        # Make sure the entry is empty + placeholder shown.
        try:
            self.entry.configure(state="normal")
            self.entry.delete(0, "end")
            self._set_placeholder()
        except Exception:
            pass
        self.status.configure(text="Ready", fg=MUTED)

    def _on_chip_add_click(self):
        if not self.last_files:
            # Tell the user instead of silently swallowing the click. This
            # is the failure mode that used to look like "+ LIB doesn't do
            # shit" — the download succeeded but file-detection couldn't
            # pin down where yt-dlp actually wrote the file.
            _alert_modal(
                self.root, "Couldn't add",
                "Drop couldn't locate the downloaded file.\n\n"
                "It might already be on disk under a different name, or "
                "yt-dlp wrote it somewhere unexpected. Check your Downloads "
                "folder and import it manually with the + button in the "
                "library.",
                error=True,
            )
            return
        menu = tk.Menu(self.root, tearoff=0,
                       bg=BG2, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#000",
                       borderwidth=0)
        for name in self.library.names:
            menu.add_command(label=name,
                             command=lambda n=name: self._file_to_library(n))
        menu.add_separator()
        menu.add_command(label="+ New library…",
                         command=self._file_to_new_library)
        # Anchor below the actual visible '+ LIB' button (action_btn in the
        # pill). The legacy `chip_add_btn` lives inside a now-hidden chip_card
        # widget, so popping the menu there sent it off-screen — user clicked
        # again, missed, and ended up bouncing through state transitions.
        btn = self.action_btn
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 2
        try:    menu.tk_popup(x, y)
        finally: menu.grab_release()

    def _file_to_library(self, lib_name):
        files = list(self.last_files)
        url   = self.last_url or ""
        added = 0
        for f in files:
            new_path = self.library.place_in(lib_name, f)
            if new_path:
                self._add_to_library(lib_name, url, new_path)
                added += 1
        self._dismiss_chip()
        self._show_status(f"Added · {lib_name}", fg=ACCENT)
        self.root.after(2000, self._hide_status_if_idle)

    def _file_to_new_library(self):
        name = self._ask_lib_name("New library")
        if not name: return
        if not self.library.create(name):
            messagebox.showerror("Drop", f"A library named “{name}” already exists.",
                                 parent=self.root)
            return
        self.library.set_active(name)  # so the new library is what they see if they open it
        self._file_to_library(name)

    def _ask_lib_name(self, title, initial=""):
        return _ask_text_modal(self.root, title, "Library name",
                               initial=initial,
                               placeholder="e.g. Edits, Songs, Late-night")

    # ── library data + rendering ─────────────────────────────────────────────
    def _add_to_library(self, lib, url, path):
        try: size = os.path.getsize(path)
        except Exception: size = None
        self.library.add(lib, {
            "title":        Path(path).stem,
            "path":         os.path.abspath(path),
            "url":          url or "",   # remembered for JSON export
            "kind":         detect_kind(url),
            "source":       url_source(url),
            "size":         size,
            "completed_at": time.time(),
        })

    def _clear_library(self):
        if not self.library.items_in(self.library.active):
            return
        if messagebox.askyesno(
            "Clear library",
            f"Remove all entries from “{self.library.active}”?\n\n"
            "Files in the library folder are not deleted.",
            parent=self.root,
        ):
            self.library.clear(self.library.active)
            self._render_library()

    def _render_library(self):
        # Two-stage render so the loading overlay actually paints before we
        # start blocking the main thread with grid construction:
        #
        #   1. Show the overlay + force a paint via update_idletasks
        #   2. Defer the heavy work to after_idle so Tk gets a full event
        #      loop iteration to flush the paint to screen. Without this,
        #      _render_grid would start blocking immediately and the user
        #      would only ever see the finished grid — never the overlay.
        self._show_lib_loading()
        self._lib_loading_started_at = time.time()
        self.root.after_idle(self._render_library_impl)

    def _render_library_impl(self):
        try:
            self.lib_title_lbl.configure(text=self.library.active.upper())
            self._render_tabs()
            if self.lib_view == "feed":
                self._show_grid()
            else:
                self._render_grid()
        finally:
            # Enforce a minimum visible time on the loading overlay. With a
            # small library, _render_grid can finish in <30ms; instant-hide
            # is a flicker. 200ms is the human-perceptible floor — anything
            # shorter reads as a flash, anything longer feels sluggish.
            elapsed_ms = (time.time() - self._lib_loading_started_at) * 1000
            remaining = max(50, 200 - int(elapsed_ms))
            self.root.after(remaining, self._hide_lib_loading)

    def _render_grid(self):
        # Cancel any in-flight hover previews from the old grid.
        self._cancel_all_hovers()

        # Clear old tiles
        for w in self.grid_inner.winfo_children():
            w.destroy()
        self._tile_widgets = []
        self._photo_refs   = []
        self._tiles_by_idx = {}
        self._tile_menu_btns = {}
        self._drag_state   = None
        self._drag_target  = None

        # Reset any column/row configuration left over from a previous
        # render. Without this, switching from grid (4 weighted columns
        # with uniform="cells") to list (1 column) leaves the phantom
        # columns 1..3 still asserting layout share, which strangles
        # column 0's width down to ~1/4 of the canvas.
        try:
            cols, rows = self.grid_inner.grid_size()
        except Exception:
            cols, rows = (8, 200)  # safe upper bound
        for c in range(cols):
            self.grid_inner.columnconfigure(c, weight=0, uniform="", minsize=0)
        for r in range(rows):
            self.grid_inner.rowconfigure(r, weight=0, minsize=0)

        items = self.library.items_in(self.library.active)

        # Pre-fetch all thumbnails in parallel so they're warm by the time
        # tiles scroll into view, instead of generating per cell on demand.
        # The cache caps concurrent ffmpegs internally so this is safe even
        # for large libraries.
        try:
            self.thumbs.prefetch_all(
                [it.get("path") for it in items if it.get("path")]
            )
        except Exception:
            pass

        # Apply active search filter (case-insensitive substring on title/source).
        q = (self._search_query or "").strip().lower()
        if q:
            visible = [(i, it) for i, it in enumerate(items)
                       if q in (it.get("title", "") or "").lower()
                       or q in (it.get("source", "") or "").lower()]
        else:
            visible = list(enumerate(items))

        # Pin favorites to the top-left, preserving each side's relative order
        # (stable sort). Underlying library order is untouched, so drag-reorder
        # still operates on real indices — after a drag the re-render just
        # bubbles favorites back up.
        visible.sort(key=lambda p: not bool(p[1].get("favorite", False)))

        if not visible:
            msg = ("No items match \u201c%s\u201d." % q if q
                   else "No items in this library yet.\nDownload something, then tap +.")
            tk.Label(self.grid_inner, text=msg, bg=BG, fg=MUTED,
                     font=self.f_meta, justify="center", pady=40).pack()
            return

        if self.lib_layout == "list":
            self._render_list_rows(visible)
        else:
            self._render_grid_tiles(visible)

        # Ensure scrollregion updates immediately (clamped — top pinned to 0).
        self.grid_inner.update_idletasks()
        self._set_grid_scrollregion()
        self.grid_canvas.yview_moveto(0)

        # Re-apply selection highlight if the selected idx is still valid.
        if self._selected_idx is not None:
            n = len(self.library.items_in(self.library.active))
            if self._selected_idx >= n:
                self._selected_idx = None
            else:
                self._highlight_selection()

    def _render_grid_tiles(self, visible):
        cols = self._grid_cols
        for c in range(cols):
            self.grid_inner.columnconfigure(c, weight=1, uniform="cells")

        target_w = self._geom.get("lib_w", self.LIB_W)
        cw = max(self.grid_canvas.winfo_width(), target_w - 24, 320)
        tile_w_now = max(140, (cw - 8 * cols) // cols)
        tile_h_now = min(int(tile_w_now * 16 / 9), 360)
        rows_needed = (len(visible) + cols - 1) // cols
        for r in range(rows_needed):
            self.grid_inner.rowconfigure(r, minsize=tile_h_now)

        for slot, (real_idx, item) in enumerate(visible):
            row = slot // cols
            col = slot % cols
            self._build_tile(real_idx, item).grid(
                row=row, column=col, padx=4, pady=4, sticky="nsew"
            )

    def _render_list_rows(self, visible):
        """Compact list view — one row per item: thumb + title + meta."""
        # Single column, full width
        self.grid_inner.columnconfigure(0, weight=1)
        for r in range(len(visible)):
            self.grid_inner.rowconfigure(r, minsize=140)

        for slot, (real_idx, item) in enumerate(visible):
            self._build_list_row(real_idx, item).grid(
                row=slot, column=0, padx=4, pady=2, sticky="ew"
            )

    def _build_list_row(self, idx, item):
        """A single list row: thumbnail + title + meta + ⋮ menu.
        Sized generously so each row reads like its own card."""
        path = item.get("path") or ""
        ext  = Path(path).suffix.lower()
        is_video = ext in VIDEO_EXTS

        # Width: prefer the actual current window width minus library padding,
        # since the canvas's `winfo_width()` can be stale during initial render
        # or while transitioning. We want rows that fill the full window.
        win_w = self.root.winfo_width()
        if win_w < 100:
            win_w = self._geom.get("lib_w", self.LIB_W)
        # 24px library padding (12 each side) + 8px grid pad + 4px row pad
        row_w = max(320, win_w - 24 - 8 - 8)

        ROW_H   = 132
        THUMB_W = 208
        THUMB_H = 117

        row = RoundedCard(self.grid_inner, bg=BG2, radius=12)
        row.configure(width=row_w, height=ROW_H)
        inner = row.inner
        inner.configure(bg=BG2)

        # Left thumbnail — fixed 16:9 size
        thumb_holder = tk.Frame(inner, bg="#0a0a0a",
                                 width=THUMB_W, height=THUMB_H)
        thumb_holder.pack(side="left", padx=8, pady=8)
        thumb_holder.pack_propagate(False)
        thumb_lbl = tk.Label(thumb_holder, bg="#0a0a0a", text="\u25B6",
                              fg=SOFT, font=self.f_h1)
        thumb_lbl.pack(fill="both", expand=True)

        if is_video and path and os.path.exists(path):
            self._request_thumb(path, thumb_lbl, THUMB_W, THUMB_H,
                                rotation=int(item.get("rotation", 0) or 0))
        elif ext in AUDIO_EXTS:
            thumb_lbl.configure(text="\u266B", font=self.f_h1)

        # Right side: title + meta stacked, with breathing room
        text_col = tk.Frame(inner, bg=BG2)
        text_col.pack(side="left", fill="both", expand=True,
                       padx=(8, 8), pady=12)

        title_lbl = tk.Label(text_col, text=item.get("title") or "—",
                              bg=BG2, fg=TEXT, font=self.f_card_t,
                              anchor="w", justify="left",
                              wraplength=max(200, row_w - THUMB_W - 90))
        title_lbl.pack(fill="x", anchor="w", pady=(0, 4))

        bits = [item.get("source") or "—"]
        if item.get("size"):
            bits.append(humanize_size(item["size"]))
        bits.append(humanize_time(item.get("completed_at", time.time())))
        meta_lbl = tk.Label(text_col, text="  \u00B7  ".join(bits),
                             bg=BG2, fg=SOFT, font=self.f_meta,
                             anchor="w", justify="left")
        meta_lbl.pack(fill="x", anchor="w")

        # Selection checkbox between thumb and text (only visible when
        # selection mode is on). Tiny enough not to disturb the layout when
        # hidden — pack_forget removes the slot entirely.
        self._attach_select_checkbox(row, inner, idx, list_row=True)

        # Menu dots on the far right — minimal hover (no pill bg, dots just dim)
        menu_btn = IconButton(
            inner, icon_name="dots",
            command=lambda i=idx: self._show_card_menu_at_btn(i),
            bg=BG2, fg=TEXT, hover_fg=SOFT,
            width=32, height=32, icon_size=18,
            minimal=True,
        )
        menu_btn.pack(side="right", padx=8)
        # Stash the button so the menu can pop up directly under it.
        self._tile_menu_btns = getattr(self, "_tile_menu_btns", {})
        self._tile_menu_btns[idx] = menu_btn

        # Hover effect: lift the row bg slightly
        def on_enter(_e=None):
            try: row.itemconfigure("rect", fill=BG3)
            except Exception: pass
            inner.configure(bg=BG3); text_col.configure(bg=BG3)
            title_lbl.configure(bg=BG3); meta_lbl.configure(bg=BG3)
        def on_leave(e=None):
            # Filter spurious leaves to children
            try:
                rx, ry = row.winfo_rootx(), row.winfo_rooty()
                w, h   = row.winfo_width(), row.winfo_height()
                if e is not None and (rx <= e.x_root < rx+w and ry <= e.y_root < ry+h):
                    return
            except Exception: pass
            try: row.itemconfigure("rect", fill=BG2)
            except Exception: pass
            inner.configure(bg=BG2); text_col.configure(bg=BG2)
            title_lbl.configure(bg=BG2); meta_lbl.configure(bg=BG2)

        clickable = (row, inner, text_col, title_lbl, meta_lbl, thumb_holder, thumb_lbl)
        for w in clickable:
            w.bind("<Button-1>",  lambda e, i=idx: self._row_clicked(i))
            w.bind("<Button-3>",  lambda e, i=idx: self._show_card_menu(e, i))
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

        self._tiles_by_idx = getattr(self, "_tiles_by_idx", {})
        self._tiles_by_idx[idx] = row
        # Stash the thumb label + its render dims on the row so
        # _refresh_tile_thumb can re-request it in place after a rotation
        # without rebuilding the whole list.
        row._thumb_lbl  = thumb_lbl
        row._thumb_dims = (THUMB_W, THUMB_H)
        return row

    def _row_clicked(self, idx):
        """List-view click: same selection-aware dispatch as grid tiles."""
        if self._selection_mode:
            self._toggle_selection(idx)
        else:
            self._open_feed_at(idx)

    def _show_card_menu_at_btn(self, idx):
        """Pop the per-item menu next to the list-row's ⋮ button."""
        btn = getattr(self, "_tile_menu_btns", {}).get(idx)
        if btn is None or not btn.winfo_exists():
            return
        class _E: pass
        e = _E()
        # Menu pops up just below the button, aligned to its right edge so
        # it doesn't run off the side of the window.
        e.x_root = btn.winfo_rootx()
        e.y_root = btn.winfo_rooty() + btn.winfo_height() + 2
        self._show_card_menu(e, idx)

    def _build_tile(self, idx, item):
        """A single grid tile: thumb + title overlay + kind badge."""
        # Derive size from the target window width rather than canvas width,
        # so we can build tiles at the right size BEFORE the canvas is mapped.
        target_w = self._geom.get("lib_w", self.LIB_W)
        cw = max(self.grid_canvas.winfo_width(), target_w - 24, 320)
        tile_w = max(140, (cw - 8 * self._grid_cols) // self._grid_cols)
        tile_h = min(int(tile_w * 16 / 9), 360)

        path = item.get("path") or ""
        kind = (item.get("kind") or "video").lower()
        ext  = Path(path).suffix.lower()
        is_video = ext in VIDEO_EXTS
        is_audio = ext in AUDIO_EXTS

        card = RoundedCard(self.grid_inner, bg=BG2, radius=12)
        # No fixed width/height + no pack_propagate: tile fills its cell.
        inner = card.inner
        inner.configure(bg=BG2)

        # Layered widgets: thumb fills, title overlay at bottom
        thumb_lbl = tk.Label(inner, bg="#000", text="",
                             fg=MUTED, font=self.f_meta)
        thumb_lbl.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Bottom gradient-ish band: just a darker label area
        band = tk.Frame(inner, bg="#0c0c0c")
        band.place(relx=0, rely=1.0, relwidth=1, anchor="sw", height=42)

        is_fav = bool(item.get("favorite", False))
        # Canvas-based heart so the widget footprint is fixed regardless of
        # whether ♡ and ♥ have different glyph widths in the system font —
        # toggling visibly "fills in" the same shape, no size jump.
        FAV_RED = "#ff4d6d"
        HEART_W, HEART_H = 22, 20
        heart = tk.Canvas(band, width=HEART_W, height=HEART_H, bg="#0c0c0c",
                          highlightthickness=0, bd=0, cursor="hand2")
        heart.create_text(HEART_W // 2, HEART_H // 2,
                          text=("♥" if is_fav else "♡"),
                          fill=FAV_RED,
                          font=(self.f_meta[0], 14, "bold"),
                          tags="glyph")
        # Pack BEFORE the text column so side="right" takes its slot first
        # and the title doesn't stretch into the heart's space.
        if not self._selection_mode:
            heart.pack(side="right", padx=(0, 8))
        card._fav_heart = heart
        card._fav_is = is_fav
        card._fav_glyph_font = (self.f_meta[0], 14, "bold")

        def _toggle_fav(_e=None, i=idx):
            self._toggle_favorite(i)
            return "break"  # don't fall through to tile click
        heart.bind("<Button-1>", _toggle_fav)

        # Text column on the left of the band. Wrapping title + meta in their
        # own frame lets the heart claim the right edge cleanly without the
        # title's fill="x" overlapping it.
        text_col = tk.Frame(band, bg="#0c0c0c")
        text_col.pack(side="left", fill="both", expand=True)

        title_text = item.get("title") or "—"
        if len(title_text) > 38:
            title_text = title_text[:35] + "…"
        title_lbl = tk.Label(text_col, text=title_text, bg="#0c0c0c", fg=TEXT,
                             font=self.f_meta, anchor="w")
        title_lbl.pack(fill="x", padx=8, pady=(6, 0))

        meta_text = item.get("source") or "—"
        meta_lbl = tk.Label(text_col, text=meta_text, bg="#0c0c0c", fg=SOFT,
                            font=(self.f_meta[0], 7), anchor="w")
        meta_lbl.pack(fill="x", padx=8)

        # Kind label top-right. No backdrop — just bold caption text floating
        # over the thumb's top corner. Same "no ugly box" treatment as the
        # heart: tight bg matching the thumb area + zero padding so only the
        # glyph bbox shows.
        kind_text = kind.upper() if is_video else ("AUDIO" if is_audio else "FILE")
        badge = tk.Label(inner, text=kind_text, bg="#000", fg=TEXT,
                         font=(self.f_meta[0], 7, "bold"),
                         padx=0, pady=0)
        badge.place(relx=1.0, rely=0, x=-8, y=8, anchor="ne")

        # Hover state — band color shift + preview playback.
        # Tk fires <Leave> when crossing into child widgets; we filter that out
        # by checking whether the cursor is still inside the card bounds.
        tile_state = {"hovering": False}

        def really_left(event):
            try:
                rx = card.winfo_rootx()
                ry = card.winfo_rooty()
                w  = card.winfo_width()
                h  = card.winfo_height()
                return not (rx <= event.x_root < rx + w
                            and ry <= event.y_root < ry + h)
            except Exception:
                return True

        def on_enter(_e=None):
            if tile_state["hovering"]: return
            tile_state["hovering"] = True
            for w in (band, text_col, title_lbl, meta_lbl, heart):
                try: w.configure(bg="#181818")
                except Exception: pass
            # Skip preview playback while selecting — a moving preview over
            # a checkbox would just fight the user's attention, and the
            # cancel-on-click toggle would feel laggy if VLC was warming up.
            if self._selection_mode:
                return
            # Honor the user's "preview on hover" preference — off by default
            # since spinning up VLC on every tile costs noticeably more
            # battery/CPU than the static thumbnail.
            if not getattr(self, "preview_enabled", False):
                return
            # Kick off preview playback if we have a real video.
            if is_video and path and os.path.exists(path):
                self._start_hover_preview(idx, thumb_lbl, path)

        def on_leave(e=None):
            if e is not None and not really_left(e):
                return
            if not tile_state["hovering"]: return
            tile_state["hovering"] = False
            for w in (band, text_col, title_lbl, meta_lbl, heart):
                try: w.configure(bg="#0c0c0c")
                except Exception: pass
            self._stop_hover_preview(idx, thumb_lbl)

        clickable = (card, inner, thumb_lbl, band, title_lbl, meta_lbl, badge)
        for w in clickable:
            w.bind("<Button-1>",       lambda e, i=idx, c=card: self._tile_press(e, i, c))
            w.bind("<B1-Motion>",      lambda e, i=idx, c=card: self._tile_drag(e, i, c))
            w.bind("<ButtonRelease-1>", lambda e, i=idx, c=card: self._tile_release(e, i, c))
            w.bind("<Button-3>", lambda e, i=idx: self._show_card_menu(e, i))
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

        # Selection-mode checkbox in the top-left corner. Built unconditionally
        # so we can flip its visibility cheaply without re-rendering tiles,
        # and stashed on the card so _toggle_selection can repaint only one.
        self._attach_select_checkbox(card, inner, idx)

        # Save mapping idx -> card so we can reach the drop target's card.
        # Also stash the band on the card so drag-target highlight can find it.
        card._drop_band = band
        self._tiles_by_idx = getattr(self, "_tiles_by_idx", {})
        self._tiles_by_idx[idx] = card

        # Initial state: placeholder
        if not path or not os.path.exists(path):
            thumb_lbl.configure(text="missing", fg=MUTED)
        elif is_audio:
            thumb_lbl.configure(text="\u266B", fg=SOFT,
                                 font=(self.f_btn[0], 28, "bold"))
        elif is_video:
            thumb_lbl.configure(text="\u25B6", fg=MUTED,
                                 font=(self.f_btn[0], 22))
            self._request_thumb(path, thumb_lbl, tile_w, tile_h,
                                rotation=int(item.get("rotation", 0) or 0))
        else:
            thumb_lbl.configure(text=ext.lstrip("."), fg=MUTED)

        self._tile_widgets.append((card, thumb_lbl))
        # Same stash as the list row \u2014 lets _refresh_tile_thumb update a
        # single tile in place when its rotation changes.
        card._thumb_lbl  = thumb_lbl
        card._thumb_dims = (tile_w, tile_h)
        return card

    # ── tile press / drag / release ──────────────────────────────────────────
    DRAG_THRESHOLD = 8  # px before press becomes drag. Generous so casual clicks count.
    # Drag-animation tunables.
    DRAG_EASE         = 0.28    # how aggressively tiles glide to target / frame
    DRAG_GHOST_ALPHA  = 0.9     # ghost Toplevel opacity — conveys "lifted" without motion

    def _tile_press(self, event, idx, card):
        # Capture the press; we don't know yet if it's click-or-drag.
        self._drag_state = {
            "idx":     idx,
            "card":    card,
            "x0":      event.x_root,
            "y0":      event.y_root,
            "active":  False,   # becomes True once we cross the threshold
        }
        self._drag_target = None  # idx the cursor is hovering over

    def _tile_drag(self, event, idx, card):
        st = self._drag_state
        if not st: return

        dx = event.x_root - st["x0"]
        dy = event.y_root - st["y0"]
        if not st["active"]:
            if (dx * dx + dy * dy) < (self.DRAG_THRESHOLD * self.DRAG_THRESHOLD):
                return  # still within click slop
            st["active"] = True
            # Don't start drag visuals during selection mode or when a search
            # filter is hiding part of the grid (reorder semantics get fuzzy
            # against a non-contiguous view).
            if self._selection_mode or self._search_query:
                # Old-style behavior: dim + cursor change; release will still
                # treat this as a swap via _tile_release's fallback.
                try:
                    card.configure(cursor="exchange")
                    band = getattr(card, "_drop_band", None)
                    if band is not None:
                        band.configure(bg=BG3)
                except Exception:
                    pass
            else:
                # Full visual drag: snapshot the source, lift a ghost
                # Toplevel, switch grid to place() so we can animate the
                # other tiles flowing around the cursor.
                self._begin_visual_drag(idx, card)

        if st.get("visual_drag"):
            # Move the ghost + update the target slot if the cursor crossed
            # into a different tile's territory.
            self._update_visual_drag(event.x_root, event.y_root)
        else:
            # Fallback (search/selection): old drop-target highlight only.
            target_idx = self._tile_under_cursor(event.x_root, event.y_root)
            if target_idx == idx:
                target_idx = None
            if target_idx != self._drag_target:
                self._set_drop_target(self._drag_target, False)
                self._drag_target = target_idx
                self._set_drop_target(target_idx, True)

    def _tile_release(self, event, idx, card):
        st = self._drag_state
        if not st:
            return

        dx = event.x_root - st["x0"]
        dy = event.y_root - st["y0"]
        was_drag = (dx * dx + dy * dy) >= (self.DRAG_THRESHOLD * self.DRAG_THRESHOLD)
        target  = self._drag_target

        # End the visual drag (animates ghost into final slot + restores grid).
        if st.get("visual_drag"):
            self._end_visual_drag(committed=was_drag)
            self._drag_state  = None
            self._drag_target = None
            return

        # Restore source tile visuals (search/selection fallback path).
        try:
            card.configure(cursor="hand2")
            band = getattr(card, "_drop_band", None)
            if band is not None:
                band.configure(bg="#0c0c0c")
        except Exception:
            pass
        self._set_drop_target(target, False)

        source_idx = st["idx"]
        self._drag_state  = None
        self._drag_target = None

        if not was_drag:
            if self._selection_mode:
                self._toggle_selection(source_idx)
                return
            self._open_feed_at(source_idx)
            return

        if target is None or target == source_idx:
            return

        self.library.reorder(self.library.active, source_idx, target)
        self._render_grid()

    def _tile_under_cursor(self, x_root, y_root):
        """Return idx of the tile the cursor is over, or None."""
        for i, c in self._tiles_by_idx.items():
            try:
                if not c.winfo_exists():
                    continue
                rx = c.winfo_rootx()
                ry = c.winfo_rooty()
                w  = c.winfo_width()
                h  = c.winfo_height()
                if rx <= x_root < rx + w and ry <= y_root < ry + h:
                    return i
            except Exception:
                continue
        return None

    def _set_drop_target(self, idx, on):
        """Highlight or un-highlight the drop target tile via its band."""
        if idx is None: return
        c = self._tiles_by_idx.get(idx)
        if not c:
            return
        try:
            band = getattr(c, "_drop_band", None)
            if band is None:
                return
            band.configure(bg=ACCENT if on else "#0c0c0c")
            for child in band.winfo_children():
                child.configure(bg=ACCENT if on else "#0c0c0c",
                                 fg="#000" if on else (TEXT if child.cget("font") == self.f_meta else SOFT))
        except Exception:
            pass

    # ── visual drag (ghost + live reflow) ────────────────────────────────────
    def _begin_visual_drag(self, source_idx, card):
        """Lift the source card off the grid: snapshot it into a floating
        Toplevel that rotates+jiggles under the cursor, then re-place all
        sibling tiles so we can animate them flowing around the gap.

        This requires switching tiles from grid() to place() so we can hand
        out precise (x, y) positions per tile. The original grid layout is
        restored in _end_visual_drag."""
        try:
            from PIL import ImageGrab, Image, ImageTk
        except Exception:
            # No PIL → no ghost. Fall back to the dim+cursor path.
            try: card.configure(cursor="exchange")
            except Exception: pass
            return

        # Build the visible-order list (real_idx, item). Drag-mode is only
        # entered when no search filter is active (caller already checked),
        # so the visible list is just the full library in real order.
        items = self.library.items_in(self.library.active)
        visible_idxs = list(range(len(items)))
        if source_idx not in visible_idxs:
            return  # source was filtered out mid-press — bail
        src_slot = visible_idxs.index(source_idx)

        # Compute slot dimensions from one of the live tiles. cell_w/h is
        # the full grid cell footprint (tile + 4px padding each side).
        ref = self._tiles_by_idx.get(source_idx) or card
        try:
            ref.update_idletasks()
            tile_w = ref.winfo_width()
            tile_h = ref.winfo_height()
        except Exception:
            tile_w, tile_h = 200, 280
        cell_w = tile_w + 8   # matches `padx=4` in _render_grid_tiles
        cell_h = tile_h + 8

        # Snapshot the source card BEFORE we hide it. ImageGrab is the
        # most reliable way to get a faithful rendering — it captures
        # whatever the user actually sees, including thumbnail, title, and
        # any in-flight hover preview frame.
        try:
            x = card.winfo_rootx(); y = card.winfo_rooty()
            w = card.winfo_width(); h = card.winfo_height()
            ghost_img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        except Exception:
            ghost_img = None
        if ghost_img is None:
            return

        # Switch every tile from grid to place. capture position-on-canvas
        # for each so we have a starting point for the slide animation.
        tile_positions = {}
        for i, c in self._tiles_by_idx.items():
            try:
                c.update_idletasks()
                tx = c.winfo_x()
                ty = c.winfo_y()
                tw = c.winfo_width()
                th = c.winfo_height()
                c.grid_forget()
                c.place(x=tx, y=ty, width=tw, height=th)
                tile_positions[i] = [float(tx), float(ty), tw, th]
            except Exception:
                pass

        # Hide the source from the grid (its content is now in the ghost).
        try:
            card.place_forget()
        except Exception:
            pass

        # Build the ghost Toplevel.
        ghost_top = tk.Toplevel(self.root)
        ghost_top.overrideredirect(True)
        ghost_top.attributes("-topmost", True)
        try: ghost_top.attributes("-alpha", self.DRAG_GHOST_ALPHA)
        except Exception: pass
        ghost_lbl = tk.Label(ghost_top, bd=0, highlightthickness=0, bg=BG)
        ghost_lbl.pack()

        self._drag_state.update({
            "visual_drag":     True,
            "source_idx":      source_idx,
            "src_visible_slot": src_slot,
            "current_slot":    src_slot,
            "visible_idxs":    visible_idxs,
            "cell_w":          cell_w,
            "cell_h":          cell_h,
            "tile_w":          tile_w,
            "tile_h":          tile_h,
            "tile_positions":  tile_positions,    # current (animated) x/y
            "tile_targets":    {},                 # idx -> target (x, y)
            "ghost_top":       ghost_top,
            "ghost_lbl":       ghost_lbl,
            "ghost_image":     ghost_img,
            "ghost_size":      (w, h),
            "ghost_phase":     0.0,
            "ghost_anchor_dx": w // 2,             # offset so cursor stays
            "ghost_anchor_dy": h // 2,             #   centered under the grip
            "alive":           True,
        })

        # Compute initial targets (source removed from its slot, others
        # close up). Without this the placed tiles sit exactly where grid
        # put them — no visible "gap closes" effect on drag start.
        self._recompute_drag_targets(src_slot)
        # Kick off the per-frame animation loop.
        self._drag_anim_tick()
        # Initial ghost paint + position.
        self._update_ghost_visual()

    def _update_visual_drag(self, x_root, y_root):
        """Called on every B1-Motion. Slides the ghost to the cursor and
        recomputes which slot the cursor is hovering over."""
        st = self._drag_state
        if not st or not st.get("visual_drag"):
            return

        # Move ghost.
        gtop = st["ghost_top"]
        ax = st["ghost_anchor_dx"]
        ay = st["ghost_anchor_dy"]
        try:
            # Ghost top-left so the original click point on the card stays
            # under the cursor — feels like the card is literally held.
            gtop.geometry(f"+{int(x_root - ax)}+{int(y_root - ay)}")
        except Exception:
            pass

        # Determine which slot the cursor is over (in grid_inner coords).
        new_slot = self._slot_under_cursor(x_root, y_root)
        if new_slot is None or new_slot == st["current_slot"]:
            return
        st["current_slot"] = new_slot
        self._recompute_drag_targets(new_slot)

    def _slot_under_cursor(self, x_root, y_root):
        """Return the visible-list slot index that the cursor is over."""
        st = self._drag_state
        if not st: return None
        try:
            gi = self.grid_inner
            local_x = x_root - gi.winfo_rootx()
            local_y = y_root - gi.winfo_rooty()
        except Exception:
            return None
        if local_x < 0 or local_y < 0:
            return None
        cell_w = st["cell_w"]
        cell_h = st["cell_h"]
        cols   = max(1, self._grid_cols)
        col = min(cols - 1, max(0, int(local_x // cell_w)))
        row = max(0, int(local_y // cell_h))
        slot = row * cols + col
        n = len(st["visible_idxs"])
        return min(slot, n - 1) if n > 0 else None

    def _recompute_drag_targets(self, hover_slot):
        """Given that the source belongs at `hover_slot`, compute the (x, y)
        each non-source tile should glide to. Slots run row-major across
        the existing grid_cols columns."""
        st = self._drag_state
        if not st: return
        src_idx = st["source_idx"]
        cols    = max(1, self._grid_cols)
        cell_w  = st["cell_w"]
        cell_h  = st["cell_h"]
        # Build the "what the order would look like" list with the source
        # taken out and re-inserted at hover_slot. Then each remaining tile
        # gets the (row, col) of its slot in that new order.
        order = list(st["visible_idxs"])
        order.remove(src_idx)
        # We don't actually need to insert the source — it's the ghost — but
        # we need its slot accounted for so the others go around it.
        order.insert(hover_slot, src_idx)

        targets = {}
        for slot, idx in enumerate(order):
            if idx == src_idx:
                continue  # ghost handles this one
            row = slot // cols
            col = slot % cols
            # +4 to compensate for the per-cell padx/pady=4 in the grid.
            x = col * cell_w + 4
            y = row * cell_h + 4
            targets[idx] = (x, y)
        st["tile_targets"] = targets

    def _drag_anim_tick(self):
        """Per-frame animator: glides each tile from its current placed
        position toward its target. Self-rescheduling until _end_visual_drag
        flips `alive` off. The ghost is painted statically in
        _begin_visual_drag and not touched here — no jiggle, no rotation."""
        st = self._drag_state
        if not st or not st.get("alive"):
            return

        ease = self.DRAG_EASE
        positions = st["tile_positions"]
        targets   = st["tile_targets"]
        for idx, (tx, ty) in targets.items():
            card = self._tiles_by_idx.get(idx)
            if not card:
                continue
            cur = positions.get(idx)
            if cur is None:
                cur = [tx, ty, st["tile_w"], st["tile_h"]]
                positions[idx] = cur
            cx, cy, cw, ch = cur
            nx = cx + (tx - cx) * ease
            ny = cy + (ty - cy) * ease
            if abs(nx - tx) < 0.5 and abs(ny - ty) < 0.5:
                nx, ny = float(tx), float(ty)
            cur[0], cur[1] = nx, ny
            try: card.place(x=int(nx), y=int(ny), width=cw, height=ch)
            except Exception: pass

        self.root.after(16, self._drag_anim_tick)

    def _update_ghost_visual(self):
        """Paint the ghost image into its Label once. Static — no rotation,
        no per-frame redraw. The alpha-transparent Toplevel does all the
        'lifted' communication we need."""
        st = self._drag_state
        if not st or not st.get("ghost_image"):
            return
        try:
            from PIL import ImageTk
        except Exception:
            return
        try:
            photo = ImageTk.PhotoImage(st["ghost_image"])
            st["ghost_lbl"].configure(image=photo)
            st["ghost_lbl"].image = photo
            w, h = st["ghost_size"]
            st["ghost_top"].geometry(f"{w}x{h}")
        except Exception:
            pass

    def _end_visual_drag(self, committed):
        """Stop animating, commit the reorder if the cursor moved at all,
        and rebuild the grid through the normal renderer so layout state
        is fully restored (grid() with weights, no leftover place()s).

        The rebuild destroys every tile widget and recreates them, which
        normally produces a hard black flash because grid_inner's bg shows
        through during the gap. We mask that by snapshotting the visible
        area first and laying it on top until the new tiles are painted."""
        st = self._drag_state
        if not st or not st.get("visual_drag"):
            return
        st["alive"] = False

        try: st["ghost_top"].destroy()
        except Exception: pass

        if committed:
            src_idx     = st["source_idx"]
            visible_idxs = st["visible_idxs"]
            target_slot = st["current_slot"]
            src_slot    = st["src_visible_slot"]
            if target_slot != src_slot:
                target_real = visible_idxs[target_slot]
                self.library.reorder(self.library.active, src_idx, target_real)

        # Snapshot the visible grid area so the user never sees the
        # destroy-and-rebuild gap.
        cover = self._build_grid_flash_cover()
        self._render_grid()
        # Let the new tiles paint once, then drop the cover. Two frames'
        # worth of delay (~32ms) gives Tk time to flush before the cover
        # disappears, so the user sees: old snapshot → new tiles, with
        # nothing in between.
        if cover is not None:
            self.root.update_idletasks()
            self.root.after(32, lambda: self._destroy_grid_flash_cover(cover))

    def _build_grid_flash_cover(self):
        """Snapshot the grid_canvas region and place a Label with that
        image over the canvas so a re-render doesn't show through. Returns
        the cover widget or None if PIL/ImageGrab isn't available."""
        try:
            from PIL import ImageGrab, ImageTk
        except Exception:
            return None
        canv = self.grid_canvas
        try:
            canv.update_idletasks()
            x = canv.winfo_rootx()
            y = canv.winfo_rooty()
            w = canv.winfo_width()
            h = canv.winfo_height()
            if w < 2 or h < 2:
                return None
            snap = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            photo = ImageTk.PhotoImage(snap)
        except Exception:
            return None
        cover = tk.Label(canv, image=photo, bd=0, highlightthickness=0, bg=BG)
        cover.image = photo
        cover.place(x=0, y=0, width=w, height=h)
        cover.lift()
        return cover

    def _destroy_grid_flash_cover(self, cover):
        try: cover.destroy()
        except Exception: pass

    def _request_thumb(self, path, label, w, h, rotation=0):
        """Get/generate thumbnail and place it on the label when ready.

        `rotation` mirrors the per-item rotation field (0/90/180/270) so
        the static thumb in the grid/list view matches the orientation
        VLC will actually play the video in. Rotation is applied at
        display time, not in the cached thumb file — that way changing
        rotation never invalidates the ffmpeg-generated cache."""
        if ImageTk is None:
            return  # No PIL.ImageTk = can't display

        def deliver(thumb_path):
            # Always run on Tk main thread
            self.root.after(0, lambda: self._apply_thumb(label, thumb_path, w, h, rotation))

        self.thumbs.request(path, deliver)

    def _apply_thumb(self, label, thumb_path, w, h, rotation=0):
        if not thumb_path or not label.winfo_exists():
            return
        try:
            from PIL import Image as PImage, ImageFilter
            img = PImage.open(thumb_path)
            # Match the on-screen orientation VLC will use. VLC's
            # --transform-type=N rotates CLOCKWISE; PIL's ROTATE_*
            # constants are COUNTERCLOCKWISE, so 90 CW maps to ROTATE_270
            # and 270 CW maps to ROTATE_90. Transpose is a pure axis swap
            # (no resampling), so it's both faster and lossless compared
            # to .rotate(angle).
            rot = int(rotation) % 360
            transpose_op = {
                90:  PImage.ROTATE_270,
                180: PImage.ROTATE_180,
                270: PImage.ROTATE_90,
            }.get(rot)
            if transpose_op is not None:
                img = img.transpose(transpose_op)
            img = self._fit_cover(img, w, h)
            # Light sharpen — counters the softening from the LANCZOS downscale.
            try:
                img = img.filter(ImageFilter.UnsharpMask(radius=0.6,
                                                         percent=120,
                                                         threshold=2))
            except Exception:
                pass
            photo = ImageTk.PhotoImage(img)
            self._photo_refs.append(photo)  # prevent GC
            label.configure(image=photo, text="")
            label.image = photo
            label._static_photo = photo  # remember for hover-restore
        except Exception:
            pass

    @staticmethod
    def _fit_cover(img, w, h):
        """Resize like CSS object-fit:cover — scale up + center crop."""
        from PIL import Image as PImage
        iw, ih = img.size
        if iw == 0 or ih == 0:
            return img
        s = max(w / iw, h / ih)
        nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
        try:
            resample = PImage.Resampling.LANCZOS
        except AttributeError:
            resample = PImage.LANCZOS
        img = img.resize((nw, nh), resample)
        x = (nw - w) // 2
        y = (nh - h) // 2
        return img.crop((x, y, x + w, y + h))

    # ── hover preview playback (live VLC) ────────────────────────────────────
    HOVER_DELAY_MS = 140   # delay before kicking off playback after enter
    # 140ms is the sweet spot: long enough to filter out fast sweeps across
    # the grid (so we don't spin up VLC for every tile), short enough that
    # genuine "park on this tile" hovers feel instant.

    def _on_hover_surface_click(self, _event):
        """Click on the previewed video → open the feed at that tile."""
        st = getattr(self, "_active_hover", None)
        if not st: return
        idx = st.get("idx")
        if idx is None: return
        self._open_feed_at(idx)

    def _on_hover_surface_menu(self, event):
        """Right-click on the previewed video → tile context menu."""
        st = getattr(self, "_active_hover", None)
        if not st: return
        idx = st.get("idx")
        if idx is None: return
        self._show_card_menu(event, idx)

    def _ensure_hover_player(self):
        """Lazily create a single dedicated VLC player + a Frame surface
        we can move over whichever tile is currently hovered."""
        if not _VLC_OK:
            return False
        if getattr(self, "_hover_surface", None) is None:
            # The video paint surface — VLC takes its HWND, so Tk can't get
            # mouse events here on Windows once playback starts.
            self._hover_surface = tk.Frame(self.root, bg="#000",
                                           highlightthickness=0)

            # A click-catcher Toplevel that floats over the surface. Toplevels
            # are real OS windows, so they receive mouse events independently
            # of whatever VLC does to its render surface. We make it
            # almost-transparent (alpha=0.01) so it doesn't visually obscure
            # the video, and bind clicks on it.
            self._hover_click = tk.Toplevel(self.root)
            self._hover_click.overrideredirect(True)   # no titlebar
            self._hover_click.configure(bg="#000")
            try:
                # alpha 0.01 is below Windows' click-through threshold on some
                # builds; 0.10 stays interactive while remaining nearly invisible
                # over a video.
                self._hover_click.attributes("-alpha", 0.10)
                self._hover_click.attributes("-topmost", True)
            except Exception:
                pass
            self._hover_click.withdraw()  # hidden until placed over surface
            # Bind clicks/menu on the toplevel itself
            self._hover_click.bind("<Button-1>", self._on_hover_surface_click)
            self._hover_click.bind("<Button-3>", self._on_hover_surface_menu)
            self._hover_click.configure(cursor="hand2")
        if getattr(self, "_hover_vlc", None) is None:
            try:
                self._hover_vlc = vlc.Instance(
                    "--quiet", "--no-video-title-show",
                    "--input-repeat=65535",
                )
                self._hover_player = self._hover_vlc.media_player_new()
                # Attach surface
                hwnd = self._hover_surface.winfo_id()
                if sys.platform == "win32":
                    self._hover_player.set_hwnd(hwnd)
                elif sys.platform == "darwin":
                    self._hover_player.set_nsobject(hwnd)
                else:
                    self._hover_player.set_xwindow(hwnd)
            except Exception:
                self._hover_vlc = None
                return False
        return True

    def _start_hover_preview(self, idx, label, path):
        """Place the shared hover-surface over `label` and play `path` from t=0."""
        # Cancel any pending starts and stop any current playback
        self._stop_hover_preview(idx, label, restore=False)

        # Defer start a bit — if the user is just sweeping the grid, we don't
        # want to spin up VLC for every tile they pass over.
        if not hasattr(self, "_hover_state"):
            self._hover_state = {}
        state = {
            "idx":   idx,
            "label": label,
            "path":  path,
            "after": None,
            "alive": True,
            "playing": False,
        }
        self._hover_state[idx] = state

        state["after"] = self.root.after(
            self.HOVER_DELAY_MS,
            lambda: self._do_start_hover(state),
        )

    def _do_start_hover(self, state):
        if not state["alive"]:
            return
        label = state["label"]
        if not label.winfo_exists():
            return
        if not self._ensure_hover_player():
            return  # No VLC — fall back silently to static thumb

        # Compute label's screen position relative to root
        try:
            lx = label.winfo_rootx() - self.root.winfo_rootx()
            ly = label.winfo_rooty() - self.root.winfo_rooty()
            lw = label.winfo_width()
            lh = label.winfo_height()
        except Exception:
            return
        if lw < 4 or lh < 4:
            return

        try:
            self._hover_surface.place(x=lx, y=ly, width=lw, height=lh)
            self._hover_surface.lift()
            # Position the click-catcher Toplevel using SCREEN coords (rootx/y)
            # since Toplevels live in screen space, not parent-relative.
            sx = label.winfo_rootx()
            sy = label.winfo_rooty()
            # deiconify FIRST — geometry() on a withdrawn window can be ignored
            # by some Windows builds.
            self._hover_click.deiconify()
            self._hover_click.geometry(f"{lw}x{lh}+{sx}+{sy}")
            try:
                self._hover_click.attributes("-topmost", True)
                self._hover_click.lift()
            except Exception:
                pass
            media = self._hover_vlc.media_new(state["path"])
            media.add_option("input-repeat=65535")
            self._hover_player.set_media(media)
            self._hover_player.play()
            # Mirror the user's in-app volume onto the hover player so the
            # preview matches what they'd hear in the feed. Failure is
            # non-fatal — VLC's default volume isn't great, but the preview
            # would still be functional.
            try:
                feed = getattr(self, "feed", None)
                vol = int(getattr(feed, "volume", 80))
                self._hover_player.audio_set_volume(vol)
            except Exception:
                pass
            state["playing"] = True
            self._active_hover = state
        except Exception:
            pass

    def _stop_hover_preview(self, idx, label, restore=True):
        if not hasattr(self, "_hover_state"):
            return
        state = self._hover_state.pop(idx, None)
        if state:
            state["alive"] = False
            if state.get("after"):
                try: self.root.after_cancel(state["after"])
                except Exception: pass
            # If this was the currently-playing one, stop VLC and hide surfaces
            if getattr(self, "_active_hover", None) is state:
                self._active_hover = None
                if getattr(self, "_hover_player", None) is not None:
                    try: self._hover_player.stop()
                    except Exception: pass
                if getattr(self, "_hover_surface", None) is not None:
                    try: self._hover_surface.place_forget()
                    except Exception: pass
                if getattr(self, "_hover_click", None) is not None:
                    try: self._hover_click.withdraw()
                    except Exception: pass
        # Static thumb is already on the label — nothing to restore visually,
        # since we never modified the label's image (the surface sat over it).

    # ── grid <-> feed transitions ────────────────────────────────────────────
    def _show_grid(self):
        try: self.feed_frame.pack_forget()
        except Exception: pass
        self.feed.stop()
        self.grid_frame.pack(fill="both", expand=True)
        self.lib_view = "grid"
        # Skip a full grid rebuild on the back path. The tiles we built
        # the last time _render_library ran are still in grid_inner — no
        # one moved them. Rebuilding from scratch destroyed every widget
        # and re-created them, which on a non-trivial library shows as a
        # ~500ms-1s black gap between the feed unmapping and the new
        # tiles painting ("just a black screen for like a second").
        # Anything that ACTUALLY mutates the grid (add/remove/move/clear/
        # rotation refresh) already calls _render_library or
        # _refresh_tile_thumb itself, so the tiles will be up to date by
        # the time we land here.
        if not getattr(self, "_tile_widgets", None):
            self._render_grid()

    def _open_feed_at(self, idx):
        items = self.library.items_in(self.library.active)
        if not items: return
        self._cancel_all_hovers()
        try: self.grid_frame.pack_forget()
        except Exception: pass
        self.feed_frame.pack(fill="both", expand=True)
        self.lib_view = "feed"
        self.root.update_idletasks()
        # Defer so VLC has a real window handle to attach to AND so the hover
        # player has time to release the audio device on Windows. Without this
        # gap, two libvlc instances briefly fight over WASAPI and the feed
        # player can come up silent.
        def _start():
            self.feed.set_items(items)
            self.feed.index = max(0, min(idx, len(items) - 1))
            self.feed._load_current()
        self.root.after(120, _start)

    def _render_tabs(self):
        # Only blow away the tab chips, not the import button (which lives in tabs_row directly)
        for w in self.tabs_chip_row.winfo_children():
            w.destroy()
        for name in self.library.names:
            active = name == self.library.active
            chip = RoundedButton(
                self.tabs_chip_row, text=name,
                command=lambda n=name: self._switch_to(n),
                bg=ACCENT if active else BG2,
                fg="#000" if active else TEXT,
                hover_bg=ACCENT_D if active else BG3,
                font=self.f_tab, padx=10, pady=4, radius=10,
            )
            chip.pack(side="left", padx=(0, 6))
            chip.bind("<Button-3>", lambda e, n=name: self._tab_menu(e, n))
        plus = RoundedButton(
            self.tabs_chip_row, text="+",
            command=self._new_library,
            bg=BG, fg=SOFT, hover_bg=BG2,
            font=self.f_tab, padx=10, pady=4, radius=10,
        )
        plus.pack(side="left")

    def _switch_to(self, name):
        # Reset search state when switching libraries
        if self._search_query:
            self.search_var.set("")
            self._search_query = ""
        if self._search_active:
            self._toggle_search()  # collapse the bar
        # Drop the selection — indices would be meaningless against a new
        # list of items.
        if self._selection_mode:
            self._exit_selection_mode()
        self.library.set_active(name)
        self._render_library()

    def _new_library(self):
        name = self._ask_lib_name("New library")
        if not name: return
        if not self.library.create(name):
            messagebox.showerror("Drop", f"A library named “{name}” already exists.",
                                 parent=self.root)
            return
        self.library.set_active(name)
        self._render_library()

    # ── selection mode ───────────────────────────────────────────────────────
    def _attach_select_checkbox(self, card, inner, idx, list_row=False):
        """Add a small canvas checkbox overlaid on a tile/row and expose
        `_redraw_checkbox` on the card so _toggle_selection can repaint just
        that one widget instead of the whole library."""
        size = 22
        # Overlay placement keeps the row/tile's existing layout intact and
        # works the same for both views. bg matches what's behind the
        # corner — black thumbnail area for grid, near-black thumb holder
        # for list — so the canvas margin blends in until selected.
        bg_behind = "#0a0a0a" if list_row else "#000"
        box = tk.Canvas(inner, width=size, height=size,
                        bg=bg_behind, highlightthickness=0, bd=0)
        # Tuck inside the thumbnail's top-left corner. List rows have a
        # bigger left padding so the offset is slightly bigger there.
        ox, oy = (14, 14) if list_row else (8, 8)
        box.place(relx=0, rely=0, x=ox, y=oy, anchor="nw")

        def redraw():
            box.delete("all")
            on   = idx in self._selected_idxs
            # Unselected fill matches the canvas bg so the square reads as
            # an outline-only chip floating over the thumbnail. Tk canvas
            # doesn't support alpha colors (#RRGGBBAA fails), so we lean
            # on solid-color matching for the see-through look.
            fill = ACCENT if on else bg_behind
            outl = ACCENT if on else TEXT
            # Rounded square — _rrect_pts gives the polygon points.
            box.create_polygon(_rrect_pts(1, 1, size - 1, size - 1, 5),
                               smooth=True, fill=fill, outline=outl, width=2)
            if on:
                # Inline checkmark — three line segments forming a ✓.
                box.create_line(6, 12, 10, 16, 17, 7,
                                fill="#000", width=2, capstyle="round")
            # Cursor signals it's interactive when the mode is active.
            try: box.configure(cursor="hand2" if self._selection_mode else "")
            except Exception: pass

        # Click on the checkbox always toggles selection — entering selection
        # mode implicitly if needed. Useful as a discovery affordance: a user
        # who's never toggled the mode can just click a checkbox and the
        # whole experience boots up.
        def on_click(_e):
            self._toggle_selection(idx)
            return "break"
        box.bind("<Button-1>", on_click)

        # Visibility: only shown in selection mode. We always create the
        # widget so a future mode-enter doesn't have to rebuild the grid.
        if not self._selection_mode:
            try: box.place_forget()
            except Exception: pass
        redraw()

        # Cache so _toggle_selection / mode changes can find this widget.
        card._select_box = box
        card._redraw_checkbox = redraw

    def _build_select_toolbar(self):
        """The bulk-actions bar: lives at the bottom of library_frame and
        is hidden until selection mode is on. Wrapped in a separator-topped
        frame so it reads as a docked toolbar rather than floating UI."""
        self.select_toolbar = tk.Frame(self.library_frame, bg=BG2)
        # Top border so the toolbar reads as separate from the content above.
        tk.Frame(self.select_toolbar, bg=BORDER, height=1).pack(fill="x")

        inner = tk.Frame(self.select_toolbar, bg=BG2)
        inner.pack(fill="x", padx=12, pady=10)

        self._select_count_lbl = tk.Label(
            inner, text="0 selected", bg=BG2, fg=TEXT, font=self.f_label,
        )
        self._select_count_lbl.pack(side="left")

        # Right-cluster: Cancel, Move to ▾, Delete (visual order RTL).
        self._select_delete_btn = RoundedButton(
            inner, text="DELETE", command=self._bulk_delete_selected,
            bg=ERROR, fg="#000", hover_bg="#ff8080",
            font=self.f_chip, padx=14, pady=6, radius=8, min_width=84,
        )
        self._select_delete_btn.pack(side="right")

        self._select_move_btn = RoundedButton(
            inner, text="MOVE TO…", command=self._bulk_move_selected,
            bg=BG3, fg=TEXT, hover_bg=BORDER,
            font=self.f_chip, padx=14, pady=6, radius=8, min_width=92,
        )
        self._select_move_btn.pack(side="right", padx=(0, 8))

        self._select_cancel_btn = RoundedButton(
            inner, text="CANCEL", command=self._exit_selection_mode,
            bg=BG3, fg=TEXT, hover_bg=BORDER,
            font=self.f_chip, padx=14, pady=6, radius=8, min_width=80,
        )
        self._select_cancel_btn.pack(side="right", padx=(0, 8))
        # Hidden until mode is on.

    def _toggle_selection_mode(self):
        if self._selection_mode:
            self._exit_selection_mode()
        else:
            self._enter_selection_mode()

    def _enter_selection_mode(self):
        self._selection_mode = True
        self._selected_idxs.clear()
        try: self.select_btn.set_active(True)
        except Exception: pass
        # Tear down any preview that was mid-playback when the user toggled
        # the mode — on_enter won't fire again until they hover out + back in.
        try: self._cancel_all_hovers()
        except Exception: pass
        self._show_select_toolbar()
        self._refresh_select_toolbar()
        self._render_library()

    def _exit_selection_mode(self):
        if not self._selection_mode and not self._selected_idxs:
            return
        self._selection_mode = False
        self._selected_idxs.clear()
        try: self.select_btn.set_active(False)
        except Exception: pass
        self._hide_select_toolbar()
        self._render_library()

    def _show_select_toolbar(self):
        try: self.select_toolbar.pack(side="bottom", fill="x")
        except Exception: pass

    def _hide_select_toolbar(self):
        try: self.select_toolbar.pack_forget()
        except Exception: pass

    def _refresh_select_toolbar(self):
        """Update the count label + dim destructive buttons when nothing is
        selected. Cheaper than re-rendering on every checkbox click."""
        n = len(self._selected_idxs)
        try:
            self._select_count_lbl.configure(
                text=f"{n} selected" if n != 1 else "1 selected"
            )
        except Exception:
            pass
        enabled = n > 0
        for btn in (self._select_delete_btn, self._select_move_btn):
            try:
                btn.set_state(enabled=enabled)
            except Exception:
                pass

    def _toggle_selection(self, idx):
        """Flip selection state for `idx` and refresh the toolbar count.
        Safe to call when selection mode isn't on — auto-enters it so a
        click on a checkbox always 'just works'."""
        if not self._selection_mode:
            self._enter_selection_mode()
        if idx in self._selected_idxs:
            self._selected_idxs.remove(idx)
        else:
            self._selected_idxs.add(idx)
        self._refresh_select_toolbar()
        # Repaint only the affected tile rather than the whole library.
        tile = getattr(self, "_tiles_by_idx", {}).get(idx)
        redraw = getattr(tile, "_redraw_checkbox", None) if tile else None
        if callable(redraw):
            try: redraw()
            except Exception: pass

    # ── bulk actions ─────────────────────────────────────────────────────────
    def _bulk_delete_selected(self):
        """Remove selected entries from the active library. Asks once whether
        to also delete the underlying files on disk."""
        if not self._selected_idxs:
            return
        n = len(self._selected_idxs)
        # Two-step prompt: first confirm the count, then ask about files.
        if not messagebox.askyesno(
            "Delete selected",
            f"Remove {n} item{'s' if n != 1 else ''} from “{self.library.active}”?",
            parent=self.root,
        ):
            return
        delete_files = messagebox.askyesno(
            "Delete files too?",
            "Also delete the underlying files from disk?\n\n"
            "Choose No to remove the entries only; the files stay where they are.",
            parent=self.root,
        )

        # Remove in DESCENDING order so earlier indices don't shift while we
        # mutate the underlying list.
        active = self.library.active
        items  = self.library.items_in(active)
        for idx in sorted(self._selected_idxs, reverse=True):
            if not (0 <= idx < len(items)):
                continue
            if delete_files:
                p = items[idx].get("path")
                if p:
                    try: os.remove(p)
                    except Exception: pass
            self.library.remove(active, idx)
        self._exit_selection_mode()

    def _bulk_move_selected(self):
        """Move every selected item to a chosen target library. Pops a small
        chooser so the user can pick from existing libs or create a new one."""
        if not self._selected_idxs:
            return
        others = [n for n in self.library.names if n != self.library.active]

        menu = tk.Menu(self.root, tearoff=0,
                       bg=BG2, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#000",
                       borderwidth=0)
        for n in others:
            menu.add_command(label=n, command=lambda t=n: self._do_bulk_move(t))
        if others:
            menu.add_separator()
        menu.add_command(label="New library…",
                         command=self._do_bulk_move_new)
        # Drop the menu just above the move button.
        btn = self._select_move_btn
        try:
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() - 4
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _do_bulk_move(self, target):
        active = self.library.active
        # Sort descending for safe iteration during list mutation.
        for idx in sorted(self._selected_idxs, reverse=True):
            self.library.move_file(active, idx, target)
            self.library.move(active, idx, target)
        self._exit_selection_mode()

    def _do_bulk_move_new(self):
        name = self._ask_lib_name("New library")
        if not name:
            return
        if not self.library.create(name):
            _alert_modal(self.root, "Drop",
                         f"A library named “{name}” already exists.",
                         error=True)
            return
        self._do_bulk_move(name)

    # ── layout toggle ────────────────────────────────────────────────────────
    def _toggle_lib_layout(self):
        self.lib_layout = "list" if self.lib_layout == "grid" else "grid"
        try:
            self.layout_btn.set_icon(
                "grid" if self.lib_layout == "list" else "list"
            )
        except Exception:
            pass
        self._do_save_geom()
        if self.lib_view == "grid":
            self._render_grid()

    # ── search ───────────────────────────────────────────────────────────────
    def _toggle_search(self):
        self._search_active = not self._search_active
        if self._search_active:
            try: self.tabs_chip_row.pack_forget()
            except Exception: pass
            self.search_entry.pack(side="left", fill="x", expand=True,
                                    padx=(2, 8), ipady=4)
            self.search_entry.focus_set()
            self.search_btn.set_icon("search_close")
        else:
            try: self.search_entry.pack_forget()
            except Exception: pass
            self.tabs_chip_row.pack(side="left", fill="x", expand=True)
            self.search_btn.set_icon("search")
            if self._search_query:
                self.search_var.set("")
                self._search_query = ""
                if self.lib_view == "grid":
                    self._render_grid()

    def _apply_search(self):
        q = self.search_var.get().strip()
        if q == self._search_query:
            return
        self._search_query = q
        if self.lib_view == "grid":
            self._render_grid()

    # ── settings panel ───────────────────────────────────────────────────────
    def _open_settings(self):
        """Show the inline settings panel. If already open, close it."""
        if getattr(self, "_settings_panel", None) is not None:
            self._close_settings()
            return
        # Any hover-preview Toplevel currently visible would float ABOVE the
        # settings panel (toplevels ignore the place() z-order), so kill it
        # before we open. Without this, you can land in settings with a tiny
        # video clip stuck floating over the form.
        try: self._cancel_all_hovers()
        except Exception: pass
        # Build the panel as a child of library_frame so it overlays the
        # whole library view (including grid/feed body), but not the title bar.
        panel = tk.Frame(self.library_frame, bg=BG, highlightthickness=0)
        # Place it covering everything below the title bar
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        panel.lift()
        self._settings_panel = panel

        # Inner card so the contents don't hug the edges
        wrap = tk.Frame(panel, bg=BG)
        wrap.place(relx=0.5, rely=0.04, anchor="n", relwidth=0.7)

        # Header row: title + close X
        header = tk.Frame(wrap, bg=BG)
        header.pack(fill="x", pady=(0, 6))
        tk.Label(header, text="SETTINGS", bg=BG, fg=TEXT,
                 font=(self.f_btn[0], 14, "bold"), anchor="w").pack(side="left")

        close_btn = IconButton(
            header, icon_name="search_close", command=self._close_settings,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            width=32, height=32, radius=10, icon_size=16,
        )
        close_btn.pack(side="right")

        tk.Frame(wrap, bg=ACCENT, height=2).pack(fill="x", pady=(2, 18))

        # Library section
        tk.Label(wrap, text="LIBRARY", bg=BG, fg=SOFT,
                 font=self.f_meta, anchor="w").pack(fill="x", pady=(0, 8))

        def closing(fn):
            def _do():
                fn()
                self._close_settings()
            return _do

        RoundedButton(
            wrap, text="EXPORT LIBRARY TO JSON",
            command=closing(self._export_library_json),
            bg=BG2, fg=TEXT, hover_bg=BG3,
            font=self.f_chip, padx=14, pady=10, radius=10,
        ).pack(fill="x", pady=4)

        RoundedButton(
            wrap, text="IMPORT LIBRARY FROM JSON",
            command=closing(self._import_library_json),
            bg=BG2, fg=TEXT, hover_bg=BG3,
            font=self.f_chip, padx=14, pady=10, radius=10,
        ).pack(fill="x", pady=4)

        tk.Label(
            wrap,
            text="Export saves URLs + titles for the active library to a JSON\n"
                 "file you can share. Import re-downloads each URL into the\n"
                 "active library.",
            bg=BG, fg=MUTED, font=self.f_meta, justify="left", anchor="w",
        ).pack(fill="x", pady=(8, 18))

        # Playback section
        tk.Label(wrap, text="PLAYBACK", bg=BG, fg=SOFT,
                 font=self.f_meta, anchor="w").pack(fill="x", pady=(0, 8))

        # Resume toggle row: label on left, pill on right
        toggle_row = tk.Frame(wrap, bg=BG2)
        toggle_row.pack(fill="x", pady=4)

        toggle_inner = tk.Frame(toggle_row, bg=BG2)
        toggle_inner.pack(fill="x", padx=14, pady=10)

        tk.Label(toggle_inner, text="Resume where I left off",
                 bg=BG2, fg=TEXT, font=self.f_label, anchor="w"
                 ).pack(side="left", fill="x", expand=True)

        self.resume_toggle = TogglePill(
            toggle_inner, on=self.resume_enabled,
            command=self._set_resume_enabled,
            width=44, height=24,
        )
        self.resume_toggle.pack(side="right")

        tk.Label(
            wrap,
            text="When on, Drop remembers your position in each video and\n"
                 "picks up where you left off the next time you open it.",
            bg=BG, fg=MUTED, font=self.f_meta, justify="left", anchor="w",
        ).pack(fill="x", pady=(8, 16))

        # Hover-preview toggle — same row pattern as Resume.
        preview_row = tk.Frame(wrap, bg=BG2)
        preview_row.pack(fill="x", pady=4)

        preview_inner = tk.Frame(preview_row, bg=BG2)
        preview_inner.pack(fill="x", padx=14, pady=10)

        tk.Label(preview_inner, text="Preview video on hover",
                 bg=BG2, fg=TEXT, font=self.f_label, anchor="w"
                 ).pack(side="left", fill="x", expand=True)

        self.preview_toggle = TogglePill(
            preview_inner, on=self.preview_enabled,
            command=self._set_preview_enabled,
            width=44, height=24,
        )
        self.preview_toggle.pack(side="right")

        tk.Label(
            wrap,
            text="Plays a muted preview when you hover a tile in the library.\n"
                 "UI integration is a bit clunky right now, but it's definitely\n"
                 "usable — off by default while it gets polished.",
            bg=BG, fg=MUTED, font=self.f_meta, justify="left", anchor="w",
        ).pack(fill="x", pady=(8, 0))

        # ── About section ─────────────────────────────────────────────
        # Subtle attribution at the bottom of the settings panel.
        tk.Label(
            wrap, text="ABOUT", bg=BG, fg=SOFT,
            font=self.f_meta, anchor="w",
        ).pack(fill="x", pady=(24, 8))
        tk.Label(
            wrap,
            text="Drop\ngithub.com/tagratie",
            bg=BG, fg=MUTED, font=self.f_meta, justify="left", anchor="w",
        ).pack(fill="x", pady=(0, 8))

        # Esc closes the panel
        self.root.bind("<Escape>", self._on_settings_escape, add="+")

    def _set_resume_enabled(self, on):
        self.resume_enabled = bool(on)
        self._do_save_geom()

    def _set_preview_enabled(self, on):
        self.preview_enabled = bool(on)
        # Stop any preview that happened to be running when the user flipped
        # the switch off — otherwise the current hover plays out one last
        # time, which feels like the toggle did nothing.
        if not self.preview_enabled:
            try: self._cancel_all_hovers()
            except Exception: pass
        self._do_save_geom()

    def _close_settings(self):
        panel = getattr(self, "_settings_panel", None)
        if panel is None:
            return
        try: panel.destroy()
        except Exception: pass
        self._settings_panel = None
        try: self.root.unbind("<Escape>")
        except Exception: pass

    def _on_settings_escape(self, _e=None):
        if getattr(self, "_settings_panel", None) is not None:
            self._close_settings()

    # ── library JSON export / import ─────────────────────────────────────────
    def _export_library_json(self):
        active = self.library.active
        items = self.library.items_in(active)
        # Only export items that have a real URL — local imports aren't shareable,
        # and items downloaded with older Drop versions didn't store the URL.
        rows = [
            {"url": it.get("url", ""),
             "title": it.get("title", ""),
             "kind":  it.get("kind", "")}
            for it in items if it.get("url")
        ]
        if not rows:
            local_count = sum(1 for it in items if it.get("source") == "local")
            no_url_count = len(items) - local_count - len(rows)
            details = []
            if local_count:
                details.append(f"{local_count} local import"
                                + ("s" if local_count != 1 else ""))
            if no_url_count:
                details.append(f"{no_url_count} item"
                                + ("s" if no_url_count != 1 else "")
                                + " saved before URL tracking was added")
            tail = (" (" + ", ".join(details) + ")") if details else ""
            messagebox.showinfo(
                "Drop",
                f"Nothing to export{tail}.\n\n"
                "Local imports can't be shared. Items downloaded going forward\n"
                "will be exportable; existing ones won't be.",
                parent=self.root,
            )
            return
        try:
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                title="Export library to JSON",
                defaultextension=".json",
                filetypes=[("JSON", ["*.json"]), ("All files", ["*.*"])],
                initialfile=f"{active}.drop.json",
                parent=self.root,
            )
        except Exception:
            path = ""
        if not path: return
        try:
            payload = {
                "drop_library_export": 1,
                "name": active,
                "items": rows,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            messagebox.showinfo(
                "Drop",
                f"Exported {len(rows)} URL{'s' if len(rows)!=1 else ''} "
                f"from \u201c{active}\u201d.",
                parent=self.root,
            )
        except Exception as e:
            messagebox.showerror("Drop", f"Couldn't export:\n{e}",
                                 parent=self.root)

    def _import_library_json(self):
        try:
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title="Import library from JSON",
                filetypes=[("JSON", ["*.json"]), ("All files", ["*.*"])],
                parent=self.root,
            )
        except Exception:
            path = ""
        if not path: return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Drop", f"Couldn't read JSON:\n{e}",
                                 parent=self.root)
            return
        # Accept either our export format or a plain list of URL strings.
        urls = []
        if isinstance(data, dict) and "items" in data:
            for it in data.get("items", []):
                if isinstance(it, dict) and it.get("url"):
                    urls.append(it["url"])
                elif isinstance(it, str):
                    urls.append(it)
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, str): urls.append(it)
                elif isinstance(it, dict) and it.get("url"): urls.append(it["url"])

        if not urls:
            messagebox.showinfo(
                "Drop", "No URLs found in that JSON file.", parent=self.root,
            )
            return

        ok = messagebox.askyesno(
            "Drop",
            f"Import {len(urls)} URL{'s' if len(urls)!=1 else ''} into "
            f"\u201c{self.library.active}\u201d?\n\n"
            "Drop will download each one in sequence.",
            parent=self.root,
        )
        if not ok: return
        self._start_import_queue(urls)

    def _start_import_queue(self, urls):
        """Kick off a sequential queue of downloads via the existing pipeline."""
        self._import_queue = list(urls)
        self._import_total = len(urls)
        self._import_done  = 0
        # Make sure we're in bar mode so the user sees progress.
        if self.mode == "library":
            self._show_home()
        self._next_in_queue()

    def _next_in_queue(self):
        if not getattr(self, "_import_queue", None):
            return
        if self.current is not None:
            # An existing download is still running — chain after it.
            return
        url = self._import_queue.pop(0)
        self._import_done += 1
        self.entry.delete(0, "end")
        self.entry.insert(0, url)
        # Reuse the standard download flow
        self._on_action()

    # ── importing local files ────────────────────────────────────────────────
    def _on_import_click(self):
        """Show a small menu next to the import button."""
        btn  = self.import_btn
        menu = tk.Menu(self.root, tearoff=0,
                       bg=BG2, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#000",
                       borderwidth=0)
        menu.add_command(label="Add files…",  command=self._import_files)
        menu.add_command(label="Add folder…", command=self._import_folder)
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height()
        try: menu.tk_popup(x, y)
        finally: menu.grab_release()

    def _import_files(self):
        media_pat = ["*" + e for e in (VIDEO_EXTS | AUDIO_EXTS)]
        filetypes = [
            ("Media files", media_pat),
            ("Video files", ["*" + e for e in VIDEO_EXTS]),
            ("Audio files", ["*" + e for e in AUDIO_EXTS]),
            ("All files",   ["*.*"]),
        ]
        paths = pick_files_modern(self.root, title="Add files to library",
                                   filetypes=filetypes, multi=True)
        if not paths:
            return
        self._import_paths(paths)

    def _import_folder(self):
        folder = pick_folder_modern(self.root,
                                     title="Add folder to library")
        if not folder:
            return
        # Walk the folder for media files (one level deep is usually enough,
        # but we recurse so subfolders work too).
        found = []
        for root, _dirs, files in os.walk(folder):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
                    found.append(os.path.join(root, f))
        if not found:
            messagebox.showinfo("Drop",
                                "No video or audio files found in that folder.",
                                parent=self.root)
            return
        if len(found) > 50:
            if not messagebox.askyesno(
                "Drop",
                f"Found {len(found)} media files. Import all into "
                f"\u201c{self.library.active}\u201d?",
                parent=self.root,
            ):
                return
        self._import_paths(found)

    def _import_paths(self, paths):
        """Copy each file into the active library and add a row for it."""
        if not paths: return
        active = self.library.active
        added  = 0
        for src in paths:
            try:
                if not os.path.isfile(src):
                    continue
                managed = self.library.place_in(active, src)
                if not managed:
                    continue
                ext = Path(managed).suffix.lower()
                kind = "video" if ext in VIDEO_EXTS else (
                       "audio" if ext in AUDIO_EXTS else None)
                self._add_to_library(active, "", managed)
                # Patch the kind we just stored (since detect_kind needs a URL)
                items = self.library.items_in(active)
                if items and kind:
                    items[0]["kind"]   = kind
                    items[0]["source"] = "local"
                    self.library.save()
                added += 1
            except Exception:
                continue
        if added:
            self._render_library()
            # If we're on the bar, hop to library so they see what landed
            if self.mode == "bar":
                self._show_library()
        else:
            messagebox.showwarning("Drop", "Couldn't import any of those files.",
                                   parent=self.root)

    def _tab_menu(self, event, name):
        menu = tk.Menu(self.root, tearoff=0,
                       bg=BG2, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#000",
                       borderwidth=0)
        menu.add_command(label="Rename…", command=lambda: self._rename_library(name))
        if len(self.library.names) > 1:
            menu.add_command(label="Delete", command=lambda: self._delete_library(name))
        try:    menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _rename_library(self, old):
        new = self._ask_lib_name("Rename library", initial=old)
        if not new or new == old: return
        if not self.library.rename(old, new):
            messagebox.showerror("Drop", "Couldn't rename (name already in use).",
                                 parent=self.root)
            return
        self._render_library()

    def _delete_library(self, name):
        if not messagebox.askyesno(
            "Delete library",
            f"Delete library “{name}”? Items in it are removed from Drop.\n\n"
            "Files in the library folder on disk are not deleted.",
            parent=self.root,
        ): return
        self.library.delete_lib(name)
        self._render_library()

    # ── card menu (right-click on the feed) ──────────────────────────────────
    def _show_card_menu(self, event, idx):
        active = self.library.active
        items  = self.library.items_in(active)
        if not (0 <= idx < len(items)): return
        item = items[idx]
        path = item.get("path")

        menu = tk.Menu(self.root, tearoff=0,
                       bg=BG2, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#000",
                       borderwidth=0)
        is_fav = bool(item.get("favorite", False))
        menu.add_command(
            label=("♥  Unfavorite" if is_fav else "♡  Favorite"),
            command=lambda i=idx: self._toggle_favorite(i),
        )
        menu.add_separator()
        menu.add_command(label="Open",        command=lambda: open_path(path))
        menu.add_command(label="Open folder", command=lambda: reveal_path(path))

        # Rotation is only meaningful for video files. Label includes the
        # post-click value so the user sees what tapping it will produce —
        # less surprising than a bare "Rotate" when the player already
        # shows it rotated.
        ext = Path(path).suffix.lower() if path else ""
        if ext in VIDEO_EXTS:
            cur_rot = int(item.get("rotation", 0) or 0)
            next_rot = (cur_rot + 90) % 360
            menu.add_command(
                label=f"Rotate 90°  ({cur_rot}° → {next_rot}°)",
                command=lambda i=idx: self._rotate_card(i),
            )

        others = [n for n in self.library.names if n != active]
        if others:
            move_menu = tk.Menu(menu, tearoff=0,
                                bg=BG2, fg=TEXT,
                                activebackground=ACCENT, activeforeground="#000",
                                borderwidth=0)
            for n in others:
                move_menu.add_command(
                    label=n,
                    command=lambda i=idx, target=n: self._move_card(i, target),
                )
            move_menu.add_separator()
            move_menu.add_command(label="New library…",
                                  command=lambda i=idx: self._move_card_new(i))
            menu.add_cascade(label="Move to", menu=move_menu)
        else:
            menu.add_command(label="Move to new library…",
                             command=lambda i=idx: self._move_card_new(i))
        menu.add_separator()
        menu.add_command(label="Remove from library",
                         command=lambda: self._remove_card(idx))
        menu.add_command(label="Delete file",
                         command=lambda: self._delete_card_file(idx))
        try:    menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _move_card(self, idx, target):
        self.library.move_file(self.library.active, idx, target)
        self.library.move(self.library.active, idx, target)
        self._render_library()

    def _move_card_new(self, idx):
        name = self._ask_lib_name("New library")
        if not name: return
        if not self.library.create(name):
            messagebox.showerror("Drop", f"A library named “{name}” already exists.",
                                 parent=self.root)
            return
        self.library.move_file(self.library.active, idx, name)
        self.library.move(self.library.active, idx, name)
        self._render_library()

    def _toggle_favorite(self, idx):
        """Flip the heart on items[active][idx]. When the item is *becoming*
        a favorite, fires a particle burst at the heart and slides a ghost
        of the tile to the new top-left slot — masks the grid re-render
        flash. Unfavoriting just re-renders (no slide needed, the user
        clicked a top-row item to demote it)."""
        try: self._cancel_all_hovers()
        except Exception: pass
        items = self.library.items_in(self.library.active)
        if not (0 <= idx < len(items)):
            return
        tile = (getattr(self, "_tiles_by_idx", {}) or {}).get(idx)

        # Fill the heart on the source tile BEFORE we snapshot it, so the
        # ghost shows the filled state mid-slide instead of popping from
        # outline → filled at the landing. Full update() (not just
        # update_idletasks) forces an actual paint cycle so ImageGrab sees
        # the new pixels rather than the stale outline.
        if tile is not None and tile.winfo_exists():
            try:
                heart = getattr(tile, "_fav_heart", None)
                if heart is not None:
                    heart.delete("glyph")
                    heart.create_text(
                        int(heart.winfo_reqwidth() / 2),
                        int(heart.winfo_reqheight() / 2),
                        text="♥", fill="#ff4d6d",
                        font=getattr(tile, "_fav_glyph_font",
                                     (self.f_meta[0], 14, "bold")),
                        tags="glyph",
                    )
                    heart.update()
            except Exception:
                pass

        becoming_fav, new_idx = self.library.toggle_favorite(
            self.library.active, idx
        )

        if not (becoming_fav and tile is not None and tile.winfo_exists()):
            self._render_library()
            return

        try: self._fire_heart_particles(tile)
        except Exception: pass
        try: self._animate_fav_slide(new_idx, tile)
        except Exception: self._render_library()

    def _fire_heart_particles(self, tile):
        """Tiny heart burst at the favorite button. Lives on a Toplevel with
        -transparentcolor on Windows so the particles float over the app
        without a visible backdrop. Survives the grid re-render that follows
        the favorite toggle."""
        heart = getattr(tile, "_fav_heart", None)
        if heart is None or not heart.winfo_exists():
            return
        try:
            heart.update_idletasks()
            cx = heart.winfo_rootx() + heart.winfo_width() / 2
            cy = heart.winfo_rooty() + heart.winfo_height() / 2
        except Exception:
            return

        FIELD = 110   # half-extent of the particle field
        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        try:
            overlay.attributes("-topmost", True)
            # Pick an unlikely color as the transparent key — anything that
            # happens to use this RGB in the canvas becomes see-through.
            overlay.attributes("-transparentcolor", "#010203")
        except Exception:
            pass
        overlay.geometry(f"{FIELD*2}x{FIELD*2}+{int(cx-FIELD)}+{int(cy-FIELD)}")
        canvas = tk.Canvas(overlay, width=FIELD * 2, height=FIELD * 2,
                           bg="#010203", highlightthickness=0, bd=0)
        canvas.pack()

        import math, random, time
        parts = []
        for i in range(7):
            angle = (i / 7) * 2 * math.pi + random.uniform(-0.18, 0.18)
            speed = random.uniform(45, 78)
            parts.append({
                "vx":   math.cos(angle) * speed,
                "vy":   math.sin(angle) * speed - 22,   # slight upward bias
                "id":   canvas.create_text(
                            FIELD, FIELD, text="♥", fill="#ff4d6d",
                            font=(self.f_meta[0],
                                  random.choice((10, 11, 12)), "bold")),
            })

        DURATION = 460
        start = time.perf_counter()
        def tick():
            elapsed = (time.perf_counter() - start) * 1000
            t = min(elapsed / DURATION, 1.0)
            ease = 1 - (1 - t) ** 2     # ease-out quad on motion
            for p in parts:
                nx = FIELD + p["vx"] * ease * 1.4
                ny = FIELD + p["vy"] * ease * 1.4 + 0.5 * 200 * t * t
                try:
                    canvas.coords(p["id"], nx, ny)
                    k = 1.0 - t
                    r = int(0xff * k + 0x01 * (1 - k))
                    g = int(0x4d * k + 0x02 * (1 - k))
                    b = int(0x6d * k + 0x03 * (1 - k))
                    canvas.itemconfigure(p["id"], fill=f"#{r:02x}{g:02x}{b:02x}")
                except Exception:
                    pass
            if t < 1.0:
                self.root.after(16, tick)
            else:
                try: overlay.destroy()
                except Exception: pass
        tick()

    def _animate_fav_slide(self, idx, tile):
        """Snapshot the favorited tile, trigger the grid re-render (which
        moves the real tile to the top-left), then slide a Toplevel ghost
        from the original screen position to the new one. The ghost sits on
        top of the freshly-rebuilt grid so the user only sees a smooth slide,
        not a re-render flash."""
        try:
            from PIL import ImageGrab, ImageTk
        except Exception:
            self._render_library()
            return

        try:
            tile.update_idletasks()
            sx = tile.winfo_rootx()
            sy = tile.winfo_rooty()
            sw = tile.winfo_width()
            sh = tile.winfo_height()
        except Exception:
            self._render_library()
            return
        if sw < 4 or sh < 4:
            self._render_library()
            return

        try:
            shot = ImageGrab.grab(bbox=(sx, sy, sx + sw, sy + sh))
        except Exception:
            self._render_library()
            return

        # Re-render so the real grid reflects the new order. The favorited
        # item is now at idx in the underlying list, which sorts to the
        # top-left in the visible layout.
        self._render_library()
        self.root.update_idletasks()

        target = (getattr(self, "_tiles_by_idx", {}) or {}).get(idx)
        if target is None or not target.winfo_exists():
            return
        try:
            target.update_idletasks()
            tx = target.winfo_rootx()
            ty = target.winfo_rooty()
        except Exception:
            return

        ghost = tk.Toplevel(self.root)
        ghost.overrideredirect(True)
        try: ghost.attributes("-topmost", True)
        except Exception: pass
        photo = ImageTk.PhotoImage(shot)
        lbl = tk.Label(ghost, image=photo, bd=0, highlightthickness=0, bg=BG)
        lbl.image = photo  # keep reference alive
        lbl.pack()
        ghost.geometry(f"{sw}x{sh}+{sx}+{sy}")

        import time
        start = time.perf_counter()
        DURATION = 280
        def tick():
            elapsed = (time.perf_counter() - start) * 1000
            t = min(elapsed / DURATION, 1.0)
            ease = 1 - (1 - t) ** 3   # ease-out cubic — quick lift, soft land
            x = int(sx + (tx - sx) * ease)
            y = int(sy + (ty - sy) * ease)
            try: ghost.geometry(f"{sw}x{sh}+{x}+{y}")
            except Exception: return
            if t < 1.0:
                self.root.after(16, tick)
            else:
                try: ghost.destroy()
                except Exception: pass
        tick()

    def _rotate_card(self, idx):
        """Rotate the video at idx by 90°. Delegates to FeedPlayer so the
        rotation applies live if the item is currently playing; otherwise
        it just persists for next playback. Also re-paints the grid/list
        tile's thumbnail so the still image in the library matches the
        new orientation — without this the thumb would stay stale until
        the next time the user re-entered the library view."""
        if not hasattr(self, "feed"):
            return
        self.feed.rotate_video(idx, step=90)
        self._refresh_tile_thumb(idx)

    def _refresh_tile_thumb(self, idx):
        """Re-request the thumb for one tile after its rotation changed.
        Cheap because the ffmpeg-generated thumb file is unchanged — only
        the display-time transpose is different — so this just queues an
        in-place PIL transpose + re-fit on the existing label."""
        tile = getattr(self, "_tiles_by_idx", {}).get(idx)
        if tile is None:
            return
        lbl = getattr(tile, "_thumb_lbl", None)
        dims = getattr(tile, "_thumb_dims", None)
        if lbl is None or dims is None:
            return
        items = self.library.items_in(self.library.active)
        if not (0 <= idx < len(items)):
            return
        item = items[idx]
        path = item.get("path")
        if not path or not os.path.exists(path):
            return
        w, h = dims
        self._request_thumb(path, lbl, w, h,
                            rotation=int(item.get("rotation", 0) or 0))

    def _remove_card(self, idx):
        # A single remove shifts every later index by one — any selection
        # set we were holding would now reference the wrong items. Clear it.
        if self._selection_mode:
            self._exit_selection_mode()
        self.library.remove(self.library.active, idx)
        self._render_library()

    def _delete_card_file(self, idx):
        items = self.library.items_in(self.library.active)
        if not (0 <= idx < len(items)): return
        item = items[idx]
        path = item.get("path")
        if path and os.path.exists(path):
            if not messagebox.askyesno("Delete file",
                                       f"Permanently delete:\n{path}?",
                                       parent=self.root):
                return
            try:
                os.remove(path)
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=self.root)
                return
        # Same index-shift hazard as _remove_card — drop any selection.
        if self._selection_mode:
            self._exit_selection_mode()
        self.library.remove(self.library.active, idx)
        self._render_library()

    # ── show / hide / quit ───────────────────────────────────────────────────
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        # The window region clears when withdrawn — reapply.
        self.root.after(40, self._refresh_corners)
        if self.mode == "bar":
            self.entry.focus_set()

    def hide(self):
        self._do_save_geom()
        if hasattr(self, "feed"):
            self.feed.stop()
        self._cancel_all_hovers()
        self.root.withdraw()

    def quit(self):
        if hasattr(self, "feed"):
            self.feed.shutdown()
        # Release the hover-preview player too
        try:
            if getattr(self, "_hover_player", None):
                self._hover_player.stop()
                self._hover_player.release()
            if getattr(self, "_hover_vlc", None):
                self._hover_vlc.release()
        except Exception:
            pass
        try: self.root.destroy()
        except Exception: pass

    def _cancel_all_hovers(self):
        """Stop any in-flight hover preview cleanly. Called on grid re-render,
        view switch, hide-to-tray, etc."""
        if not hasattr(self, "_hover_state"):
            return
        for st in list(self._hover_state.values()):
            st["alive"] = False
            if st.get("after"):
                try: self.root.after_cancel(st["after"])
                except Exception: pass
        self._hover_state = {}
        self._active_hover = None
        if getattr(self, "_hover_player", None) is not None:
            try: self._hover_player.stop()
            except Exception: pass
        if getattr(self, "_hover_surface", None) is not None:
            try: self._hover_surface.place_forget()
            except Exception: pass
        if getattr(self, "_hover_click", None) is not None:
            try: self._hover_click.withdraw()
            except Exception: pass




# ── tray ─────────────────────────────────────────────────────────────────────



def run_tray(app):
    def on_show(_i=None, _it=None): app.root.after(0, app.show)
    def on_open(_i=None, _it=None): open_path(DOWNLOADS)
    def on_quit(icon, _it=None):
        icon.stop()
        app.root.after(0, app.quit)

    menu = pystray.Menu(
        pystray.MenuItem("Show Drop",      on_show, default=True),
        pystray.MenuItem("Open Downloads", on_open),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit",           on_quit),
    )
    pystray.Icon("Drop", make_icon(), "Drop", menu).run()




def main():
    migrate_libraries_dir(LIBRARY_FILE)
    library = Library(LIBRARY_FILE)
    app = App(library)
    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    app.root.mainloop()


if __name__ == "__main__":
    main()
