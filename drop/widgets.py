"""Custom Canvas-drawn widgets: rounded buttons, cards, icon buttons.

Press-feedback flash:
    Every clickable canvas widget here now calls _flash_canvas_button(self)
    on click — same pattern the BACK button used to hand-roll for itself.
    Briefly inverts the bg/fg to a contrasting pair, reverts after ~110ms,
    re-respects hover state on revert. The command runs immediately (the
    flash is async via after()) so there's no perceived input lag.
"""
import tkinter as tk
import tkinter.font as tkfont

from .theme import BG, BG2, BG3, BORDER, ACCENT, ACCENT_D, TEXT, MUTED, SOFT, ERROR
from .utils import _rrect_pts


# ── press-feedback flash (shared by every canvas button below) ──────────────

_FLASH_MS = 110

# Attribute names a button might have for color state. We touch whichever
# ones are present on the instance.
_FLASH_ATTRS = ("_bg", "_fg", "_hover", "_abg", "_afg")


def _flash_canvas_button(btn):
    """Brief press-feedback flash. Inverts to a contrasting bg/fg for
    ~_FLASH_MS, then restores — and rechecks hover state, so a cursor
    that's still over the button comes back to hover-bg, not idle-bg.

    Safe to call on any canvas button in this module: handles RoundedButton's
    set_state path, BookButton/DotsButton/SpeakerButton/InfoButton/SearchButton's
    direct draw, and IconButton's active-state colors. No-op on disabled
    buttons so a flash can't sneak past a 'set_state(enabled=False)'."""
    if not getattr(btn, "_enabled", True):
        return
    # Snapshot whichever color attrs the button actually has.
    saved = {a: getattr(btn, a) for a in _FLASH_ATTRS if hasattr(btn, a)}
    if not saved:
        return  # not a canvas button we know how to flash

    orig_bg = saved.get("_bg")
    # If the button already lives on the accent color (GET button, active
    # IconButton), flashing TO accent does nothing visible. Flip to the
    # neutral palette instead.
    if orig_bg in (ACCENT, ACCENT_D):
        target_bg, target_fg = BG2, TEXT
    else:
        target_bg, target_fg = ACCENT, "#000"

    # Apply flash colors. We mutate the same attrs the button's _draw uses,
    # so the next _draw call paints the flash; on revert, we restore.
    btn._bg = target_bg
    btn._fg = target_fg
    if "_hover" in saved: btn._hover = target_bg
    if "_abg"   in saved: btn._abg   = target_bg
    if "_afg"   in saved: btn._afg   = target_fg
    try:
        btn._draw(target_bg)
    except Exception:
        pass

    def _revert():
        for k, v in saved.items():
            setattr(btn, k, v)
        # Re-check hover so a still-hovered button comes back to its
        # hover color rather than its idle color.
        try:
            x = btn.winfo_pointerx() - btn.winfo_rootx()
            y = btn.winfo_pointery() - btn.winfo_rooty()
            hovered = (0 <= x < btn.winfo_width()
                       and 0 <= y < btn.winfo_height())
        except Exception:
            hovered = False
        try:
            btn._draw(saved.get("_hover", saved.get("_bg")) if hovered
                      else saved.get("_bg"))
        except Exception:
            pass

    try:
        btn.after(_FLASH_MS, _revert)
    except Exception:
        # Window already destroyed mid-flash — let it slide.
        pass


# ── widgets ─────────────────────────────────────────────────────────────────


class RoundedButton(tk.Canvas):
    """A button drawn as a rounded rectangle on a Canvas."""

    def __init__(self, master, text, command=None,
                 bg=ACCENT, fg="#000", hover_bg=ACCENT_D,
                 font=None, padx=14, pady=6, radius=10,
                 min_width=0, **kw):
        self._text   = text
        self._bg     = bg
        self._fg     = fg
        self._hover  = hover_bg
        self._radius = radius
        self._font   = font
        self._cmd    = command
        self._padx   = padx
        self._pady   = pady
        self._enabled = True

        f = tkfont.Font(font=font) if font else tkfont.nametofont("TkDefaultFont")
        tw = max(min_width, f.measure(text) + padx * 2)
        th = f.metrics("linespace") + pady * 2

        super().__init__(master, width=tw, height=th,
                         bg=master.cget("bg"), highlightthickness=0,
                         bd=0, **kw)
        self.bind("<Configure>", lambda e: self._draw(self._bg))
        self.bind("<Enter>",     lambda e: self._enabled and self._draw(self._hover))
        self.bind("<Leave>",     lambda e: self._enabled and self._draw(self._bg))
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw(self._bg)

    def _draw(self, fill):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                            smooth=True, fill=fill, outline="")
        self.create_text(w // 2, h // 2, text=self._text,
                         fill=self._fg, font=self._font)

    def _on_click(self, _e):
        if self._enabled and self._cmd:
            _flash_canvas_button(self)
            self._cmd()

    def flash(self):
        """Public flash trigger — useful when you want a button to flash
        without a real click (e.g. keyboard-shortcut feedback)."""
        _flash_canvas_button(self)

    def set_text(self, text):
        self._text = text
        self._draw(self._bg)

    def set_state(self, *, bg=None, fg=None, hover_bg=None, text=None, enabled=None):
        if bg is not None:        self._bg = bg
        if fg is not None:        self._fg = fg
        if hover_bg is not None:  self._hover = hover_bg
        if text is not None:      self._text = text
        if enabled is not None:   self._enabled = enabled
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw(self._bg)




class RoundedCard(tk.Canvas):
    """A rounded panel with a child Frame for content (use .inner)."""

    def __init__(self, master, bg=BG2, radius=12, **kw):
        super().__init__(master, bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg     = bg
        self._radius = radius
        self.inner   = tk.Frame(self, bg=bg)
        self._win    = self.create_window(0, 0, anchor="nw", window=self.inner)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, e):
        self.delete("rect")
        self.create_polygon(_rrect_pts(0, 0, e.width, e.height, self._radius),
                            smooth=True, fill=self._bg, outline="", tags="rect")
        self.tag_lower("rect")
        self.itemconfigure(self._win, width=e.width, height=e.height)




class BookButton(tk.Canvas):
    """Square-ish button with a hand-drawn book glyph instead of text."""

    def __init__(self, master, command=None,
                 bg=BG2, fg=TEXT, hover_bg=BG3,
                 size=48, radius=12, **kw):
        super().__init__(master, width=size, height=size,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg      = bg
        self._fg      = fg
        self._hover   = hover_bg
        self._radius  = radius
        self._cmd     = command
        self._enabled = True

        self.bind("<Configure>", lambda e: self._draw(self._bg))
        self.bind("<Enter>",     lambda e: self._enabled and self._draw(self._hover))
        self.bind("<Leave>",     lambda e: self._enabled and self._draw(self._bg))
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw(self._bg)

    def _draw(self, fill):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        # Background pill
        self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                            smooth=True, fill=fill, outline="")
        # Book glyph: filled rounded rect (cover) with an inset spine line.
        cx, cy = w // 2, h // 2
        bw = max(int(min(w, h) * 0.5), 14)
        bh = int(bw * 0.85)
        x1, y1 = cx - bw // 2, cy - bh // 2
        x2, y2 = x1 + bw, y1 + bh
        self.create_polygon(_rrect_pts(x1, y1, x2, y2, 2),
                            smooth=True, fill=self._fg, outline="")
        # Spine: thin line in the background color near the left edge
        sx = x1 + max(3, bw // 5)
        self.create_line(sx, y1 + 2, sx, y2 - 2, fill=fill, width=2)
        # Subtle horizontal "page" hint
        self.create_line(sx + 3, y1 + bh // 2, x2 - 3, y1 + bh // 2,
                         fill=fill, width=1)

    def _on_click(self, _e):
        if self._enabled and self._cmd:
            _flash_canvas_button(self)
            self._cmd()

    def flash(self):
        _flash_canvas_button(self)

    def configure_height(self, h):
        """Match a sibling's height precisely."""
        self.configure(height=h, width=h)
        self._draw(self._bg)




class DotsButton(tk.Canvas):
    """Vertical-three-dots menu button — dots drawn on the canvas so they
    sit perfectly centered (text-based ⋮ never centers cleanly across fonts)."""

    def __init__(self, master, command=None,
                 bg=BG2, fg=TEXT, hover_bg=BG3,
                 size=32, radius=8, **kw):
        super().__init__(master, width=size, height=size,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg      = bg
        self._fg      = fg
        self._hover   = hover_bg
        self._radius  = radius
        self._cmd     = command
        self._enabled = True

        self.bind("<Configure>", lambda e: self._draw(self._bg))
        self.bind("<Enter>",     lambda e: self._enabled and self._draw(self._hover))
        self.bind("<Leave>",     lambda e: self._enabled and self._draw(self._bg))
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw(self._bg)

    def _draw(self, fill):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        # Pill background
        self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                            smooth=True, fill=fill, outline="")
        # Three dots, vertically centered
        cx, cy = w // 2, h // 2
        dot_r  = max(1, min(w, h) // 12)
        gap    = max(3, min(w, h) // 6)
        for dy in (-gap, 0, gap):
            self.create_oval(cx - dot_r, cy + dy - dot_r,
                              cx + dot_r, cy + dy + dot_r,
                              fill=self._fg, outline="")

    def _on_click(self, _e):
        if self._enabled and self._cmd:
            _flash_canvas_button(self)
            self._cmd()

    def flash(self):
        _flash_canvas_button(self)




class SpeakerButton(tk.Canvas):
    """Speaker icon: filled cone + sound waves; cross overlay when muted.
    Hand-drawn so we don't depend on emoji metrics."""

    def __init__(self, master, command=None,
                 bg=BG2, fg=TEXT, hover_bg=BG3,
                 width=44, height=30, radius=8, muted=False, **kw):
        super().__init__(master, width=width, height=height,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg     = bg
        self._fg     = fg
        self._hover  = hover_bg
        self._radius = radius
        self._cmd    = command
        self._muted  = muted
        self._enabled = True

        self.bind("<Configure>", lambda e: self._draw(self._bg))
        self.bind("<Enter>",     lambda e: self._enabled and self._draw(self._hover))
        self.bind("<Leave>",     lambda e: self._enabled and self._draw(self._bg))
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw(self._bg)

    def set_muted(self, muted):
        self._muted = muted
        self._draw(self._bg)

    def _draw(self, fill):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        # Pill bg
        self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                            smooth=True, fill=fill, outline="")

        # Speaker geometry — centered, scales with height.
        cy = h // 2
        # Place the icon centered horizontally too.
        ox = w // 2 - 7  # left edge of speaker, leaving room for waves on right
        # Box body (the back of the speaker)
        bx1, by1 = ox, cy - 3
        bx2, by2 = ox + 4, cy + 3
        self.create_rectangle(bx1, by1, bx2, by2, fill=self._fg, outline="")
        # Cone (triangle expanding to the right)
        cx1, cy1 = bx2, cy - 6
        cx2, cy2 = bx2 + 6, cy - 6
        cx3, cy3 = bx2 + 6, cy + 6
        cx4, cy4 = bx2, cy + 6
        self.create_polygon(bx2, cy - 3, cx2, cy1, cx3, cy3, bx2, cy + 3,
                            fill=self._fg, outline="")

        if self._muted:
            # Diagonal cross from upper-right to lower-left of the icon area
            x1 = ox - 2
            x2 = ox + 14
            y1 = cy - 8
            y2 = cy + 8
            # Black halo line for contrast on hover
            self.create_line(x1, y2, x2, y1, fill=fill, width=4)
            self.create_line(x1, y2, x2, y1, fill=ERROR, width=2)
        else:
            # Two arc-ish "sound waves" right of the cone
            wx = ox + 10
            for i, dx in enumerate((3, 6)):
                self.create_arc(
                    wx, cy - 5 - dx, wx + dx * 2, cy + 5 + dx,
                    start=-45, extent=90,
                    style="arc", outline=self._fg, width=2,
                )

    def _on_click(self, _e):
        if self._enabled and self._cmd:
            _flash_canvas_button(self)
            self._cmd()

    def flash(self):
        _flash_canvas_button(self)




class InfoButton(tk.Canvas):
    """Circled-i icon with two states: idle (outline) and active (filled).
    Click toggles via .set_active() externally."""

    def __init__(self, master, command=None,
                 bg=BG2, fg=TEXT, hover_bg=BG3,
                 size=30, radius=8, active=False, **kw):
        super().__init__(master, width=size, height=size,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg     = bg
        self._fg     = fg
        self._hover  = hover_bg
        self._radius = radius
        self._cmd    = command
        self._active = active
        self._enabled = True

        self.bind("<Configure>", lambda e: self._draw(self._bg))
        self.bind("<Enter>",     lambda e: self._enabled and self._draw(self._hover))
        self.bind("<Leave>",     lambda e: self._enabled and self._draw(self._bg))
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw(self._bg)

    def set_active(self, on):
        self._active = bool(on)
        self._draw(self._bg)

    def _draw(self, fill):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        # Pill bg — accent when active, normal when idle
        bg_fill = ACCENT if self._active else fill
        self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                            smooth=True, fill=bg_fill, outline="")
        glyph = "#000" if self._active else self._fg
        # Circle outline
        cx, cy = w // 2, h // 2
        r = max(6, min(w, h) // 3)
        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                         outline=glyph, width=2)
        # i-glyph proportions chosen so dot + stem are visually symmetric
        # within the circle. Dot near the top, stem near the bottom, with
        # equal padding from the circle's edges and a small gap between them.
        pad      = r * 0.20      # gap between circle edge and figure
        gap      = r * 0.18      # gap between dot and stem
        avail    = 2 * r - 2 * pad - gap     # vertical space for dot + stem
        dot_h    = avail * 0.30
        stem_h   = avail * 0.70
        dot_top  = cy - r + pad
        dot_bot  = dot_top + dot_h
        stem_top = dot_bot + gap
        stem_bot = stem_top + stem_h
        dot_r    = dot_h / 2
        stem_w   = max(1, dot_h * 0.40)      # stem thinner than dot diameter
        # Dot
        dot_cx = cx
        dot_cy = (dot_top + dot_bot) / 2
        self.create_oval(dot_cx - dot_r, dot_cy - dot_r,
                          dot_cx + dot_r, dot_cy + dot_r,
                          fill=glyph, outline="")
        # Stem (rounded ends — drawn as a rectangle with two semicircle caps)
        self.create_rectangle(cx - stem_w, stem_top,
                               cx + stem_w, stem_bot,
                               fill=glyph, outline="")
        # Round the stem caps for a refined feel
        self.create_oval(cx - stem_w, stem_top - stem_w,
                          cx + stem_w, stem_top + stem_w,
                          fill=glyph, outline="")
        self.create_oval(cx - stem_w, stem_bot - stem_w,
                          cx + stem_w, stem_bot + stem_w,
                          fill=glyph, outline="")

    def _on_click(self, _e):
        if self._enabled and self._cmd:
            _flash_canvas_button(self)
            self._cmd()

    def flash(self):
        _flash_canvas_button(self)


# ── main GUI ─────────────────────────────────────────────────────────────────


class SearchButton(tk.Canvas):
    """Magnifying glass icon with optional 'close' (×) state.
    Hand-drawn so it doesn't depend on emoji rendering."""

    def __init__(self, master, command=None,
                 bg=BG2, fg=TEXT, hover_bg=BG3,
                 width=42, height=30, radius=10, closing=False, **kw):
        super().__init__(master, width=width, height=height,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg     = bg
        self._fg     = fg
        self._hover  = hover_bg
        self._radius = radius
        self._cmd    = command
        self._closing = closing
        self._enabled = True

        self.bind("<Configure>", lambda e: self._draw(self._bg))
        self.bind("<Enter>",     lambda e: self._enabled and self._draw(self._hover))
        self.bind("<Leave>",     lambda e: self._enabled and self._draw(self._bg))
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw(self._bg)

    def set_closing(self, closing):
        self._closing = bool(closing)
        self._draw(self._bg)

    def _draw(self, fill):
        import math
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        # Pill bg
        self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                            smooth=True, fill=fill, outline="")
        glyph = self._fg
        cx, cy = w / 2, h / 2

        if self._closing:
            # × glyph
            r = min(w, h) * 0.22
            self.create_line(cx - r, cy - r, cx + r, cy + r,
                              fill=glyph, width=2, capstyle="round")
            self.create_line(cx - r, cy + r, cx + r, cy - r,
                              fill=glyph, width=2, capstyle="round")
            return

        # Magnifying glass — lens + diagonal handle.
        r = min(w, h) * 0.22
        # Center the whole figure (lens + handle) inside the button.
        # The figure's bounding box stretches from (lens_left) to
        # (handle_end). For visual centering, shift the lens slightly up-left.
        ox = cx - r * 0.35
        oy = cy - r * 0.35
        # Lens
        self.create_oval(ox - r, oy - r, ox + r, oy + r,
                         outline=glyph, width=2)
        # Handle from circle's lower-right edge outward at 45°
        sx = ox + r * math.cos(math.radians(45))
        sy = oy + r * math.sin(math.radians(45))
        ex = sx + r * 1.0
        ey = sy + r * 1.0
        self.create_line(sx, sy, ex, ey,
                          fill=glyph, width=2, capstyle="round")

    def _on_click(self, _e):
        if self._enabled and self._cmd:
            _flash_canvas_button(self)
            self._cmd()

    def flash(self):
        _flash_canvas_button(self)


class IconButton(tk.Canvas):
    """Rounded button that renders a cached PNG icon. Replaces the older
    Canvas-drawn glyph buttons (DotsButton, SearchButton, InfoButton, etc.)
    with crisper PIL-rendered art.

    `minimal=True` mode: no pill background, just the icon. Hover dims the
    icon's tint to `hover_fg` instead of changing the bg. Used for the
    list-view row menu (⋮) where a full pill would compete with the row
    card behind it."""

    def __init__(self, master, icon_name, command=None,
                 bg=BG2, fg=TEXT, hover_bg=BG3, active_bg=ACCENT, active_fg="#000",
                 width=42, height=30, radius=10, icon_size=18,
                 active=False, hover_fg=None, minimal=False, **kw):
        super().__init__(master, width=width, height=height,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg      = bg
        self._fg      = fg
        self._hover   = hover_bg
        self._hover_fg = hover_fg if hover_fg is not None else fg
        self._abg     = active_bg
        self._afg     = active_fg
        self._radius  = radius
        self._cmd     = command
        self._enabled = True
        self._active  = active
        self._minimal = bool(minimal)
        self._icon_name = icon_name
        self._icon_size = icon_size
        self._photo_idle   = None
        self._photo_active = None
        self._photo_hover  = None
        self._refs         = []  # keep references alive
        self._load_icons()

        self.bind("<Configure>", lambda e: self._draw(self._bg, hover=False))
        self.bind("<Enter>",     lambda e: self._enabled and self._draw(self._hover, hover=True))
        self.bind("<Leave>",     lambda e: self._enabled and self._draw(self._bg, hover=False))
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw(self._bg, hover=False)

    def _load_icons(self):
        # Lazy import to avoid circular dep if utils ever imports widgets.
        from .utils import ensure_ui_icon
        try:
            from PIL import Image as PImage, ImageTk
        except Exception:
            return
        # Idle: fg tint. Active: active-fg tint. Hover (minimal mode): hover_fg tint.
        tones = [
            (self._fg,       "_photo_idle"),
            (self._afg,      "_photo_active"),
            (self._hover_fg, "_photo_hover"),
        ]
        for tone, target in tones:
            p = ensure_ui_icon(self._icon_name, self._icon_size, tone)
            if not p:
                continue
            try:
                pil = PImage.open(p).convert("RGBA")
                ph = ImageTk.PhotoImage(pil)
                self._refs.append(ph)
                setattr(self, target, ph)
            except Exception:
                pass

    def set_icon(self, icon_name):
        self._icon_name = icon_name
        self._photo_idle   = None
        self._photo_active = None
        self._photo_hover  = None
        self._refs.clear()
        self._load_icons()
        self._draw(self._bg, hover=False)

    def set_active(self, on):
        self._active = bool(on)
        self._draw(self._bg, hover=False)

    def _draw(self, fill, hover=False):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        # In minimal mode, skip the pill background entirely — the icon
        # sits flush on whatever's behind it. Hover dims the icon instead.
        if not self._minimal:
            bg_fill = self._abg if self._active else fill
            self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                                smooth=True, fill=bg_fill, outline="")
        # Icon: pick the right tinted PhotoImage based on state.
        if self._active and self._photo_active is not None:
            photo = self._photo_active
        elif hover and self._minimal and self._photo_hover is not None:
            photo = self._photo_hover
        else:
            photo = self._photo_idle
        if photo is not None:
            self.create_image(w // 2, h // 2, image=photo)

    def _on_click(self, _e):
        if self._enabled and self._cmd:
            _flash_canvas_button(self)
            self._cmd()

    def flash(self):
        _flash_canvas_button(self)


class TogglePill(tk.Canvas):
    """A pill-shaped on/off toggle. Same visual language as the rest of the
    icon buttons. Click anywhere on it to flip state.

    Note: TogglePill intentionally skips the press-flash. The knob slide IS
    the press feedback, and a flash on top of that reads as visual noise."""

    def __init__(self, master, command=None,
                 on=False, width=44, height=24,
                 bg_off=BG3, bg_on=ACCENT,
                 knob_off=TEXT, knob_on="#000",
                 **kw):
        super().__init__(master, width=width, height=height,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._on       = on
        self._cmd      = command
        self._bg_off   = bg_off
        self._bg_on    = bg_on
        self._knob_off = knob_off
        self._knob_on  = knob_on

        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>",  self._on_click)
        self.configure(cursor="hand2")
        self._draw()

    def set_on(self, on):
        self._on = bool(on)
        self._draw()

    def is_on(self):
        return self._on

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        radius = h // 2
        # Track
        track_color = self._bg_on if self._on else self._bg_off
        self.create_polygon(_rrect_pts(0, 0, w, h, radius),
                            smooth=True, fill=track_color, outline="")
        # Knob
        knob_pad = max(2, h // 8)
        knob_d = h - 2 * knob_pad
        if self._on:
            kx = w - knob_pad - knob_d
        else:
            kx = knob_pad
        knob_color = self._knob_on if self._on else self._knob_off
        self.create_oval(kx, knob_pad, kx + knob_d, knob_pad + knob_d,
                         fill=knob_color, outline="")

    def _on_click(self, _e):
        self._on = not self._on
        self._draw()
        if self._cmd:
            self._cmd(self._on)


class VolumeSlider(tk.Canvas):
    """Compact in-app volume control: small speaker glyph on the left, a
    horizontal level pill on the right.

    Click the speaker = toggle mute. Click or drag the bar = set level.
    Mouse wheel over the widget = ±5%.

    The widget is fully decoupled from any audio backend — host code wires
    `on_change(value)` to whatever it wants (e.g. one VLC media-player's
    audio_set_volume). This is what keeps the slider from touching system
    volume: it only calls back to the caller, never anything global."""

    def __init__(self, master, on_change=None, on_mute=None,
                 initial=80, muted=False,
                 width=120, height=30, bg=BG2, hover_bg=BG3,
                 fill=ACCENT, track=BG3, radius=8, icon_size=16, **kw):
        super().__init__(master, width=width, height=height,
                         bg=master.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._bg        = bg
        self._hover_bg  = hover_bg
        self._fill      = fill
        self._track     = track
        self._radius    = radius
        self._on_change = on_change
        self._on_mute   = on_mute
        self._value     = max(0, min(100, int(initial)))
        self._muted     = bool(muted)
        self._hover     = False
        self._dragging  = False
        self._icon_size = icon_size

        # PIL-rendered speaker icons — crisper than canvas primitives at
        # small sizes, same rendering pipeline as the rest of the app's
        # buttons. Loaded lazily so widgets.py doesn't depend on PIL at
        # import time.
        self._icon_speaker = None
        self._icon_mute    = None
        self._icon_refs    = []  # PhotoImage refs kept alive
        self._load_icons()

        # Layout regions are recomputed in _draw based on canvas size so the
        # widget reflows gracefully if its container resizes.
        self._spk_w  = 0  # speaker hit-area width (left edge → x)
        self._bar_x1 = 0
        self._bar_x2 = 0

        self.bind("<Configure>",       lambda e: self._draw())
        self.bind("<Enter>",           self._on_enter)
        self.bind("<Leave>",           self._on_leave)
        self.bind("<Button-1>",        self._on_press)
        self.bind("<B1-Motion>",       self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<MouseWheel>",      self._on_wheel)
        # Linux wheel events arrive as Button-4/5 rather than MouseWheel.
        self.bind("<Button-4>",        lambda e: self._on_wheel_step(+5))
        self.bind("<Button-5>",        lambda e: self._on_wheel_step(-5))
        self.configure(cursor="hand2")
        self._draw()

    def _load_icons(self):
        from .utils import ensure_ui_icon
        try:
            from PIL import Image as PImage, ImageTk
        except Exception:
            return
        for name, attr in (("speaker", "_icon_speaker"),
                           ("speaker_mute", "_icon_mute")):
            p = ensure_ui_icon(name, self._icon_size, TEXT)
            if not p:
                continue
            try:
                pil = PImage.open(p).convert("RGBA")
                ph = ImageTk.PhotoImage(pil)
                self._icon_refs.append(ph)
                setattr(self, attr, ph)
            except Exception:
                pass

    # ── public API ──────────────────────────────────────────────────────────
    def get_value(self):
        return self._value

    def is_muted(self):
        return self._muted

    def set_value(self, value, fire=False):
        v = max(0, min(100, int(value)))
        if v == self._value:
            return
        self._value = v
        self._draw()
        if fire and self._on_change:
            try: self._on_change(self._value)
            except Exception: pass

    def set_muted(self, muted):
        self._muted = bool(muted)
        self._draw()

    # ── event handlers ──────────────────────────────────────────────────────
    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _on_press(self, e):
        # Speaker icon area → mute toggle. Bar area → set value.
        if e.x < self._spk_w:
            if self._on_mute:
                try: self._on_mute()
                except Exception: pass
            return
        self._dragging = True
        self._set_from_x(e.x)

    def _on_drag(self, e):
        if not self._dragging:
            return
        self._set_from_x(e.x)

    def _on_release(self, _e):
        self._dragging = False

    def _on_wheel(self, e):
        # Wheel notch: ±5%. delta sign differs by platform but Windows is
        # the dominant target — positive delta = scroll up = volume up.
        delta = getattr(e, "delta", 0) or 0
        self._on_wheel_step(5 if delta > 0 else -5)

    def _on_wheel_step(self, step):
        self.set_value(self._value + step, fire=True)
        return "break"

    def _set_from_x(self, x):
        bar_w = max(1, self._bar_x2 - self._bar_x1)
        pct   = (x - self._bar_x1) / bar_w * 100
        self.set_value(pct, fire=True)

    # ── drawing ─────────────────────────────────────────────────────────────
    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        # Rounded pill background — brightens on hover for affordance.
        bg = self._hover_bg if self._hover else self._bg
        self.create_polygon(_rrect_pts(0, 0, w, h, self._radius),
                            smooth=True, fill=bg, outline="")

        # Speaker glyph on the left — square hit-area equal to the height,
        # icon centered inside it. Uses the PIL-rendered PNG for crispness.
        spk_w = h
        self._spk_w = spk_w
        photo = self._icon_mute if self._muted else self._icon_speaker
        if photo is not None:
            self.create_image(spk_w / 2, h / 2, image=photo)

        # Bar takes the remaining width, with breathing room on both sides.
        pad_l = spk_w
        pad_r = 10
        bar_h = max(4, int(h * 0.22))
        bx1 = pad_l
        bx2 = w - pad_r
        by1 = (h - bar_h) // 2
        by2 = by1 + bar_h
        self._bar_x1, self._bar_x2 = bx1, bx2
        r = bar_h // 2
        # Track (background)
        self.create_polygon(_rrect_pts(bx1, by1, bx2, by2, r),
                            smooth=True, fill=self._track, outline="")
        # Fill — dimmer when muted so the level reads as "set but silent".
        if self._value > 0:
            fill_color = MUTED if self._muted else self._fill
            full_w = bx2 - bx1
            fill_w = full_w * (self._value / 100.0)
            # Keep the fill at least 1 cap wide so low values still render
            # as a visible nub rather than a sliver one px wide.
            fx2 = bx1 + max(r * 2, fill_w)
            fx2 = min(bx2, fx2)
            self.create_polygon(_rrect_pts(bx1, by1, fx2, by2, r),
                                smooth=True, fill=fill_color, outline="")


# ── modal dialogs ────────────────────────────────────────────────────────────


class Modal(tk.Toplevel):
    """Borderless dark modal that matches Drop's main-window look.

    Replaces tkinter.simpledialog / messagebox for the cases where we want
    the popup to feel like part of the app rather than a Win95 system dialog.
    Use the module-level helpers (ask_text / alert / confirm) instead of
    instantiating directly — they handle the modal loop and return values."""

    _PAD       = 18    # outer content padding
    _RADIUS    = 12
    _MIN_W     = 360
    _BTN_GAP   = 10

    def __init__(self, parent, title, message=None, *,
                 entry=False, initial="", placeholder="",
                 ok_text="OK", cancel_text="Cancel",
                 show_cancel=True, accent=ACCENT, accent_fg="#000"):
        super().__init__(parent, bg=BG2)
        self.withdraw()                 # hidden until placed
        self.overrideredirect(True)
        self.transient(parent)
        self.attributes("-topmost", True)
        self.resizable(False, False)

        self._result   = None
        self._entry_w  = None
        self._parent   = parent
        self._title    = title
        self._message  = message or ""
        self._entry_on = entry
        self._initial  = initial
        self._placeholder = placeholder

        f_title  = ("Consolas", 11, "bold")
        f_body   = ("Consolas", 9)
        f_entry  = ("Consolas", 10)

        # Card-style container with a 1px accent border. We draw the border
        # via outer/inner frames rather than highlightthickness so we can keep
        # rounded DWM corners on Win11 without the border bleeding.
        outer = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        body = tk.Frame(outer, bg=BG2)
        body.pack(fill="both", expand=True)

        pad = tk.Frame(body, bg=BG2)
        pad.pack(fill="both", expand=True, padx=self._PAD, pady=self._PAD)

        tk.Label(pad, text=title, bg=BG2, fg=TEXT,
                 font=f_title, anchor="w").pack(fill="x")
        tk.Frame(pad, bg=accent, height=2).pack(fill="x", pady=(8, 12))

        if self._message:
            tk.Label(pad, text=self._message, bg=BG2, fg=SOFT,
                     font=f_body, anchor="w", justify="left",
                     wraplength=self._MIN_W - self._PAD * 2).pack(
                         fill="x", pady=(0, 12 if entry else 16))

        if entry:
            ent = tk.Entry(pad, bg=BG3, fg=TEXT, font=f_entry,
                           insertbackground=TEXT,
                           relief="flat", borderwidth=0,
                           highlightthickness=1,
                           highlightbackground=BORDER,
                           highlightcolor=accent)
            ent.pack(fill="x", ipady=8, pady=(0, 16))
            if initial:
                ent.insert(0, initial)
                ent.select_range(0, "end")
            elif placeholder:
                # Lightweight placeholder: gray text removed on first focus.
                ent.insert(0, placeholder)
                ent.configure(fg=MUTED)
                def _clear_ph(_e, e=ent, ph=placeholder):
                    if e.get() == ph:
                        e.delete(0, "end")
                        e.configure(fg=TEXT)
                ent.bind("<FocusIn>", _clear_ph, add="+")
            self._entry_w = ent
            # Return "break" so these don't bubble up to the app's
            # bind_all("<Return>") / bind_all("<Key-Escape>") on root —
            # otherwise pressing Enter would also fire the grid's
            # _grid_key_enter, and pressing Escape would dismiss the modal
            # AND immediately trigger _on_back_pressed (kicking the user out
            # of library mode on the same keystroke).
            ent.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])
            ent.bind("<Escape>", lambda _e: (self._on_cancel(), "break")[1])

        # Buttons row (right-aligned).
        btn_row = tk.Frame(pad, bg=BG2)
        btn_row.pack(fill="x")
        spacer = tk.Frame(btn_row, bg=BG2)
        spacer.pack(side="left", fill="x", expand=True)

        ok = RoundedButton(btn_row, ok_text, command=self._on_ok,
                           bg=accent, fg=accent_fg, hover_bg=ACCENT_D,
                           font=("Consolas", 9, "bold"), padx=16, pady=6,
                           radius=8, min_width=80)
        ok.pack(side="right")

        if show_cancel:
            cancel = RoundedButton(btn_row, cancel_text, command=self._on_cancel,
                                   bg=BG3, fg=TEXT, hover_bg=BORDER,
                                   font=("Consolas", 9, "bold"),
                                   padx=16, pady=6, radius=8, min_width=80)
            cancel.pack(side="right", padx=(0, self._BTN_GAP))

        # Close on Escape even if focus isn't in the entry. "break" stops
        # the bubble to root's bind_all("<Key-Escape>", on_escape_global)
        # — without it, the Escape that dismisses the modal also triggers
        # the library's "go back" handler and the user lands in bar mode.
        self.bind("<Escape>", lambda _e: (self._on_cancel(), "break")[1])
        # Click outside doesn't dismiss — modal is grab_set-blocking.
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def show(self):
        """Display the modal, block until dismissed, return the result.

        Result is:
          * the entered string (or None) for entry modals
          * True/False for confirm modals
          * True (always) for alert modals (Cancel hidden)"""
        self.update_idletasks()
        # Center over parent.
        try:
            px = self._parent.winfo_rootx()
            py = self._parent.winfo_rooty()
            pw = self._parent.winfo_width()
            ph = self._parent.winfo_height()
        except Exception:
            px, py, pw, ph = 0, 0, 0, 0
        w = max(self.winfo_reqwidth(), self._MIN_W)
        h = self.winfo_reqheight()
        x = px + (pw - w) // 2
        y = py + (ph - h) // 3   # slightly above center reads more naturally
        self.geometry(f"{w}x{h}+{x}+{y}")

        # DWM rounded corners on Win11 (no-op elsewhere). Imported lazily so
        # widgets.py keeps zero platform-coupling at import time.
        try:
            from .platform_win import round_window_corners, set_window_region_rounded
            self.after(20, lambda: round_window_corners(self))
            self.after(40, lambda: set_window_region_rounded(self, radius=self._RADIUS))
        except Exception:
            pass

        self.deiconify()
        self.lift()
        if self._entry_w is not None:
            self._entry_w.focus_set()
        else:
            self.focus_set()
        self.grab_set()
        self.wait_window(self)
        return self._result

    def _on_ok(self):
        if self._entry_w is not None:
            val = self._entry_w.get()
            if self._placeholder and val == self._placeholder:
                val = ""
            self._result = val
        else:
            self._result = True
        self._dismiss()

    def _on_cancel(self):
        # Entry mode: None means cancelled. Confirm mode: False.
        self._result = None if self._entry_w is not None else False
        self._dismiss()

    def _dismiss(self):
        try: self.grab_release()
        except Exception: pass
        try: self.destroy()
        except Exception: pass


def ask_text(parent, title, message, initial="", placeholder="",
             ok_text="OK", cancel_text="Cancel"):
    """Themed replacement for simpledialog.askstring. Returns the entered
    string, or None if the user cancelled / closed the dialog."""
    return Modal(parent, title, message,
                 entry=True, initial=initial, placeholder=placeholder,
                 ok_text=ok_text, cancel_text=cancel_text).show()


def alert(parent, title, message, error=False, ok_text="OK"):
    """Themed replacement for messagebox.showinfo / .showerror. The `error`
    flag swaps the accent stripe to the theme's ERROR red so failures
    register at a glance. Returns True when dismissed (the only outcome)."""
    return Modal(parent, title, message,
                 entry=False, show_cancel=False,
                 accent=(ERROR if error else ACCENT),
                 accent_fg=("#000" if not error else "#000"),
                 ok_text=ok_text).show()


def confirm(parent, title, message, ok_text="OK", cancel_text="Cancel"):
    """Themed replacement for messagebox.askyesno. Returns True/False."""
    return bool(Modal(parent, title, message,
                      entry=False, show_cancel=True,
                      ok_text=ok_text, cancel_text=cancel_text).show())
