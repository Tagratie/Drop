"""ModernGL-based renderer for the audio visualizer.

Render pipeline per frame:
    1. Bind scene FBO. Draw background (procedural starfield + vignette).
    2. Draw the circular waveform ring on top, with additive blending.
       Per-vertex magnitudes were uploaded just before this from the audio.
    3. Draw the inner core ring on top of that.
    4. Draw particles (additive, point sprites).
    5. Bind bloom-extract FBO. Sample scene, threshold to bright pixels.
    6. Ping-pong horizontal+vertical blur over the bright-pixels texture
       a couple of iterations — that's the bloom blur.
    7. Bind default framebuffer (screen). Composite scene + bloom with
       chromatic aberration and a Reinhard tonemap.

Each pass owns its own VAO. Shaders live in shaders/ as separate files so
they're trivial to edit and read.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Tuple

import numpy as np
import moderngl


SHADER_DIR = Path(__file__).resolve().parent / "shaders"


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class Renderer:
    # ── ring geometry params ────────────────────────────────────────────────
    # n_segments controls smoothness of the ring; 512 is plenty smooth and
    # cheap to draw. The CPU writes a per-vertex magnitude buffer of size
    # (n_segments+1)*2 each frame, so this doesn't want to be huge.
    N_SEGMENTS = 512

    # Bloom is rendered at half-resolution. Blurring at half is ~4× cheaper
    # and the blur itself hides any loss of detail.
    BLOOM_SCALE = 0.5
    # Bloom iterations: each one is a horizontal+vertical blur pass. Two
    # gives a wide soft halo; more is mostly indistinguishable.
    BLOOM_ITERS = 2

    def __init__(self, ctx: moderngl.Context, width: int, height: int):
        self.ctx = ctx
        self.width = width
        self.height = height

        # ── programs ────────────────────────────────────────────────────────
        post_vert = _load(SHADER_DIR / "post.vert")
        self.prog_background = ctx.program(
            vertex_shader=post_vert,
            fragment_shader=_load(SHADER_DIR / "background.frag"),
        )
        self.prog_waveform = ctx.program(
            vertex_shader=_load(SHADER_DIR / "waveform.vert"),
            fragment_shader=_load(SHADER_DIR / "waveform.frag"),
        )
        self.prog_core = ctx.program(
            vertex_shader=_load(SHADER_DIR / "core.vert"),
            fragment_shader=_load(SHADER_DIR / "core.frag"),
        )
        self.prog_extract = ctx.program(
            vertex_shader=post_vert,
            fragment_shader=_load(SHADER_DIR / "bloom_extract.frag"),
        )
        self.prog_blur = ctx.program(
            vertex_shader=post_vert,
            fragment_shader=_load(SHADER_DIR / "blur.frag"),
        )
        self.prog_composite = ctx.program(
            vertex_shader=post_vert,
            fragment_shader=_load(SHADER_DIR / "composite.frag"),
        )
        self.prog_particle = ctx.program(
            vertex_shader=_load(SHADER_DIR / "particle.vert"),
            fragment_shader=_load(SHADER_DIR / "particle.frag"),
        )
        # Help overlay — textured quad sampled from a PIL-rendered RGBA
        # image of the keyboard shortcuts. Its own program because the
        # vertices are clip-space-direct (no aspect/zoom transform) and
        # we want straight-alpha blending, not the additive blending the
        # scene pass uses.
        self.prog_help = ctx.program(
            vertex_shader=_load(SHADER_DIR / "help_overlay.vert"),
            fragment_shader=_load(SHADER_DIR / "help_overlay.frag"),
        )

        # ── fullscreen quad ─────────────────────────────────────────────────
        # Two triangles covering clip space. Reused by every post-process pass.
        quad = np.array([
            -1.0, -1.0,
             1.0, -1.0,
            -1.0,  1.0,
             1.0, -1.0,
             1.0,  1.0,
            -1.0,  1.0,
        ], dtype="f4")
        self.quad_vbo = ctx.buffer(quad.tobytes())
        self.vao_background = ctx.vertex_array(
            self.prog_background, [(self.quad_vbo, "2f", "in_pos")]
        )
        self.vao_extract = ctx.vertex_array(
            self.prog_extract,   [(self.quad_vbo, "2f", "in_pos")]
        )
        self.vao_blur = ctx.vertex_array(
            self.prog_blur,      [(self.quad_vbo, "2f", "in_pos")]
        )
        self.vao_composite = ctx.vertex_array(
            self.prog_composite, [(self.quad_vbo, "2f", "in_pos")]
        )

        # ── ring mesh ───────────────────────────────────────────────────────
        # Triangle strip around the ring. For each angle step we emit two
        # vertices — inner (side=-1) and outer (side=+1). N_SEGMENTS+1 so
        # the strip closes seamlessly (last pair == first pair).
        n = self.N_SEGMENTS
        angles = np.empty((n + 1) * 2, dtype="f4")
        sides  = np.empty((n + 1) * 2, dtype="f4")
        for i in range(n + 1):
            a = (i / n) * 2.0 * np.pi
            angles[i * 2]     = a;  angles[i * 2 + 1] = a
            sides[i * 2]      = -1.0;  sides[i * 2 + 1]  = +1.0
        self._ring_n_verts = (n + 1) * 2

        self.wave_angle_buf = ctx.buffer(angles.tobytes())
        self.wave_side_buf  = ctx.buffer(sides.tobytes())
        # Per-vertex magnitude — dynamic, updated each frame from CPU.
        self._wave_mag_np  = np.zeros((n + 1) * 2, dtype="f4")
        self.wave_mag_buf  = ctx.buffer(self._wave_mag_np.tobytes(), dynamic=True)

        self.vao_waveform = ctx.vertex_array(
            self.prog_waveform,
            [
                (self.wave_angle_buf, "1f", "in_angle"),
                (self.wave_side_buf,  "1f", "in_side"),
                (self.wave_mag_buf,   "1f", "in_mag"),
            ],
        )

        # Inner core ring uses the same angle/side topology with smaller r
        # and constant width — no per-vertex magnitude needed.
        self.vao_core = ctx.vertex_array(
            self.prog_core,
            [
                (self.wave_angle_buf, "1f", "in_angle"),
                (self.wave_side_buf,  "1f", "in_side"),
            ],
        )

        # ── particle buffer ─────────────────────────────────────────────────
        # CPU manages a list of active particles. Each frame we pack their
        # state into a numpy array and upload before drawing. Cap at 1024
        # so worst-case upload stays trivially cheap.
        self._particle_cap = 1024
        # 4 floats per particle: x, y, size, life
        self._particle_np  = np.zeros((self._particle_cap, 4), dtype="f4")
        self.particle_buf  = ctx.buffer(
            reserve=self._particle_cap * 4 * 4, dynamic=True
        )
        self.vao_particle = ctx.vertex_array(
            self.prog_particle,
            [
                (self.particle_buf, "2f 1f 1f", "in_pos", "in_size", "in_life"),
            ],
        )
        self._n_particles = 0

        # ── framebuffers ────────────────────────────────────────────────────
        self._build_fbos(width, height)

        # ── help overlay (built last so we have a fully-init ctx) ──────────
        # PIL renders the shortcuts panel into an RGBA texture. The panel now
        # reflects live state — per-effect ON/OFF, the render-quality sliders,
        # and a render status line — so main() rebuilds it via
        # rebuild_help_texture() whenever that state changes (cheap, only on a
        # keypress / render event, never per frame). Each frame,
        # render_help_overlay() just recomputes the NDC quad corners so the
        # panel stays pinned to a fixed pixel offset from the top-left even
        # when the window is resized.
        #
        # These defaults let the panel build correctly here at construction
        # time, before main() has pushed the real state in.
        self._help_toggles = [
            ("Bloom", True), ("Camera shake", True), ("Particles", True),
            ("Chromatic aberration", True), ("Background drift", True),
            ("Star twinkle", False), ("Vignette", True),
        ]
        self._help_res_label = "1080p"
        self._help_res_index = 1
        self._help_res_n     = 4
        self._help_fps       = 60
        self._help_fps_index = 2
        self._help_fps_n     = 3
        self._help_status    = ""
        self.help_texture = None
        self.help_texture, self.help_tex_size = self._build_help_texture()
        # 4 vertices × (2 pos + 2 uv) floats = 32 bytes. Dynamic — rewritten
        # each frame.
        self.help_vbo = ctx.buffer(reserve=4 * 4 * 4, dynamic=True)
        self.vao_help = ctx.vertex_array(
            self.prog_help,
            [(self.help_vbo, "2f 2f", "in_pos", "in_uv")],
        )

        # Static uniform values that don't change per frame.
        self.prog_waveform["u_base_radius"].value = 0.42
        self.prog_waveform["u_thickness"].value   = 0.012
        self.prog_waveform["u_amplitude"].value   = 0.35
        self.prog_waveform["u_color_core"].value  = (1.0, 1.0, 1.0)
        self.prog_waveform["u_color_glow"].value  = (1.0, 0.40, 0.10)

        self.prog_core["u_radius"].value     = 0.40
        self.prog_core["u_thickness"].value  = 0.006
        self.prog_core["u_color"].value      = (1.0, 0.85, 0.55)
        self.prog_core["u_brightness"].value = 1.4

        self.prog_extract["u_threshold"].value = 0.55

        self.prog_composite["u_bloom_strength"].value = 1.35
        self.prog_composite["u_chromatic"].value      = 0.005

    # ── framebuffers / resize ──────────────────────────────────────────────

    def _build_fbos(self, w: int, h: int) -> None:
        """Allocate scene + bloom ping-pong framebuffers."""
        ctx = self.ctx

        # Scene render target — what passes 1-4 draw into. f1 (8-bit) is
        # fine here; we're not doing HDR.
        self.scene_tex = ctx.texture((w, h), components=4, dtype="f1")
        self.scene_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.scene_fbo = ctx.framebuffer(color_attachments=[self.scene_tex])

        # Bloom textures at half resolution.
        bw = max(2, int(w * self.BLOOM_SCALE))
        bh = max(2, int(h * self.BLOOM_SCALE))
        self.bloom_w, self.bloom_h = bw, bh
        self.bloom_tex_a = ctx.texture((bw, bh), components=4, dtype="f1")
        self.bloom_tex_b = ctx.texture((bw, bh), components=4, dtype="f1")
        for t in (self.bloom_tex_a, self.bloom_tex_b):
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.bloom_fbo_a = ctx.framebuffer(color_attachments=[self.bloom_tex_a])
        self.bloom_fbo_b = ctx.framebuffer(color_attachments=[self.bloom_tex_b])

    def rebuild_help_texture(self, *, toggles=None, res_label=None,
                             res_index=None, res_n=None, fps=None,
                             fps_index=None, fps_n=None, status=None) -> None:
        """Rebuild the overlay texture from new state. Cheap enough to call on
        any keypress that changes what the panel shows (effect toggles, the
        quality sliders, the render status). Only the args you pass are
        updated; the rest keep their current value. Releases the previous
        texture so we don't leak GPU memory."""
        if toggles   is not None: self._help_toggles   = toggles
        if res_label is not None: self._help_res_label = res_label
        if res_index is not None: self._help_res_index = res_index
        if res_n     is not None: self._help_res_n     = res_n
        if fps       is not None: self._help_fps       = fps
        if fps_index is not None: self._help_fps_index = fps_index
        if fps_n     is not None: self._help_fps_n     = fps_n
        if status    is not None: self._help_status    = status
        old = self.help_texture
        self.help_texture, self.help_tex_size = self._build_help_texture()
        if old is not None:
            try: old.release()
            except Exception: pass

    def _build_help_texture(self) -> Tuple[moderngl.Texture, Tuple[int, int]]:
        """Render the keyboard-shortcuts panel into an RGBA texture.

        Draws three sections — shortcuts, effect toggles (with live ON/OFF),
        and a RENDER section with two quality sliders (resolution + fps) and
        an optional status line — all in the same fonts/palette as before.
        Called at init and again by rebuild_help_texture() whenever the
        displayed state changes.

        Uses PIL to draw a rounded semi-transparent panel + glyphs + slider
        bars at native pixel resolution; the result is uploaded to a moderngl
        texture sampled as a textured quad each frame the overlay is visible.

        Returns (texture, (width, height)) so the render path knows the
        intrinsic pixel size to map into NDC."""
        from PIL import Image, ImageDraw

        # Layout. line_h is the base row height; a slider row also reserves
        # slider_h beneath its text line for the bar. label_x is where the
        # label column starts (leaving room for the key column on the left).
        pad       = 14
        line_h    = 18
        title_pad = 6
        label_x   = pad + 80
        slider_h  = 16
        min_gap   = 16

        title_font = self._find_font(15, mono=False)
        body_font  = self._find_font(13, mono=True)

        # Palette — same greys/blue header as before; accent matches the
        # waveform glow so the sliders / ON state read as "active".
        col_title  = (255, 255, 255, 255)
        col_header = (180, 190, 220, 255)
        col_key    = (200, 205, 215, 255)
        col_label  = (220, 220, 225, 255)
        col_on     = (255, 150,  70, 255)
        col_off    = (120, 125, 140, 255)
        col_value  = (235, 235, 240, 255)
        col_status = (150, 220, 160, 255)
        track_col  = ( 70,  72,  84, 255)
        fill_col   = (255, 130,  50, 255)
        tick_col   = (110, 114, 130, 255)
        handle_col = (240, 240, 245, 255)

        # Ordered rows. First element of each tuple is its kind.
        rows = [
            ("title",  "KEYBOARD SHORTCUTS"),
            ("blank",),
            ("kv", "ESC",   "Quit"),
            ("kv", "[   ]", "Rotate ±15°"),
            ("kv", "R",     "Reset rotation"),
            ("kv", "H",     "Hide this overlay"),
            ("blank",),
            ("header", "TOGGLES"),
        ]
        for i, (label, on) in enumerate(self._help_toggles, start=1):
            rows.append(("toggle", str(i), label, on))
        rows += [
            ("blank",),
            ("header", "RENDER"),
            ("kv", "Enter", "Render current song"),
            ("slider", "- / =", "Resolution", self._help_res_label,
             self._help_res_index, self._help_res_n),
            ("slider", ", / .", "FPS", str(self._help_fps),
             self._help_fps_index, self._help_fps_n),
        ]
        if self._help_status:
            rows.append(("status", self._help_status))

        tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

        def tw(text, font):
            try:
                b = tmp.textbbox((0, 0), text, font=font)
                return b[2] - b[0]
            except Exception:
                return tmp.textsize(text, font=font)[0]

        # ── measure: panel auto-fits its widest row ────────────────────────
        content_w = 0
        height = pad * 2
        for r in rows:
            kind = r[0]
            if kind == "title":
                content_w = max(content_w, tw(r[1], title_font))
                height += line_h + title_pad
            elif kind == "header":
                content_w = max(content_w, tw(r[1], body_font))
                height += line_h
            elif kind == "kv":
                content_w = max(content_w, (label_x - pad) + tw(r[2], body_font))
                height += line_h
            elif kind == "toggle":
                state = "[ON]" if r[3] else "[OFF]"
                content_w = max(content_w, (label_x - pad) + tw(r[2], body_font)
                                + min_gap + tw(state, body_font))
                height += line_h
            elif kind == "slider":
                content_w = max(content_w, (label_x - pad) + tw(r[2], body_font)
                                + min_gap + tw(r[3], body_font))
                height += line_h + slider_h
            elif kind == "status":
                content_w = max(content_w, tw(r[1], body_font))
                height += line_h
            else:  # blank
                height += line_h

        width  = max(int(content_w + pad * 2), label_x + 140 + pad)
        height = int(height)

        img  = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Semi-transparent rounded panel. Alpha 190/255 keeps text legible
        # over bright bass-pulse flashes.
        try:
            draw.rounded_rectangle([(0, 0), (width - 1, height - 1)],
                                   radius=10, fill=(10, 10, 14, 190))
        except AttributeError:
            draw.rectangle([(0, 0), (width - 1, height - 1)], fill=(10, 10, 14, 190))

        right_x = width - pad

        def right_text(y, text, font, fill):
            draw.text((right_x - tw(text, font), y), text, font=font, fill=fill)

        y = pad
        for r in rows:
            kind = r[0]
            if kind == "title":
                draw.text((pad, y), r[1], font=title_font, fill=col_title)
                y += line_h + title_pad
            elif kind == "header":
                draw.text((pad, y), r[1], font=body_font, fill=col_header)
                y += line_h
            elif kind == "kv":
                draw.text((pad,     y), r[1], font=body_font, fill=col_key)
                draw.text((label_x, y), r[2], font=body_font, fill=col_label)
                y += line_h
            elif kind == "toggle":
                _, keytext, label, on = r
                draw.text((pad,     y), keytext, font=body_font, fill=col_key)
                draw.text((label_x, y), label,   font=body_font, fill=col_label)
                right_text(y, "[ON]" if on else "[OFF]", body_font,
                           col_on if on else col_off)
                y += line_h
            elif kind == "slider":
                _, keytext, label, value, index, n = r
                draw.text((pad,     y), keytext, font=body_font, fill=col_key)
                draw.text((label_x, y), label,   font=body_font, fill=col_label)
                right_text(y, value, body_font, col_value)
                y += line_h
                # The bar spans the label column to the panel's right margin.
                bx0, bx1 = label_x, right_x
                by = y + slider_h // 2 - 1
                frac = 0.0 if n <= 1 else index / float(n - 1)
                hx = int(bx0 + frac * (bx1 - bx0))
                draw.line([(bx0, by), (bx1, by)], fill=track_col, width=4)
                draw.line([(bx0, by), (hx,  by)], fill=fill_col,  width=4)
                for s in range(n):
                    sx = int(bx0 + (0 if n <= 1 else s / float(n - 1)) * (bx1 - bx0))
                    draw.line([(sx, by - 4), (sx, by + 4)], fill=tick_col, width=1)
                draw.ellipse([(hx - 5, by - 5), (hx + 5, by + 5)], fill=handle_col)
                y += slider_h
            elif kind == "status":
                draw.text((pad, y), r[1], font=body_font, fill=col_status)
                y += line_h
            else:  # blank
                y += line_h

        # PIL renders top-down; OpenGL UVs go bottom-up. We don't flip the
        # bytes here — render_help_overlay()'s per-frame UVs use V=0 at the
        # top edge, V=1 at the bottom, achieving the flip.
        raw = img.tobytes()
        tex = self.ctx.texture((width, height), 4, raw)
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        try:
            tex.build_mipmaps()
            tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        except Exception:
            pass
        return tex, (width, height)

    @staticmethod
    def _find_font(size: int, mono: bool = False) -> "ImageFont.ImageFont":
        """Locate a usable TrueType font at `size`, preferring monospace
        when `mono` is set. Falls back across common system paths on
        Windows / Linux / macOS, and finally to PIL's bitmap default if
        nothing TTF is reachable."""
        from PIL import ImageFont
        mono_candidates = [
            "C:/Windows/Fonts/consola.ttf",
            "C:/Windows/Fonts/CascadiaMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/System/Library/Fonts/Menlo.ttc",
            "/Library/Fonts/Menlo.ttc",
        ]
        prop_candidates = [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Helvetica.ttc",
        ]
        for path in (mono_candidates if mono else prop_candidates):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        # Last resort — PIL's built-in bitmap font. Looks worse but works.
        return ImageFont.load_default()

    def render_help_overlay(self) -> None:
        """Draw the help overlay on top of whatever's currently on screen.

        Called after the composite pass (so it sits above bloom, CA, the
        tonemap, everything) when settings.show_help is true. Recomputes
        the NDC quad each call so the overlay's pixel offset from the
        window edge stays constant under resize."""
        if self.help_texture is None:
            return

        fb_w   = max(self.width,  1)
        fb_h   = max(self.height, 1)
        tex_w, tex_h = self.help_tex_size

        # 16px margin from the top-left corner of the window. Convert to NDC.
        margin = 16
        x0 = -1.0 + (margin)         / fb_w * 2.0   # left
        x1 = -1.0 + (margin + tex_w) / fb_w * 2.0   # right
        y1 =  1.0 - (margin)         / fb_h * 2.0   # top
        y0 =  1.0 - (margin + tex_h) / fb_h * 2.0   # bottom

        # Four vertices for a TRIANGLE_STRIP: (x0,y0) (x1,y0) (x0,y1) (x1,y1).
        # UVs are V-flipped (0 at top, 1 at bottom) to compensate for PIL's
        # top-down image layout vs. OpenGL's bottom-up sampling.
        verts = np.array([
            [x0, y0, 0.0, 1.0],
            [x1, y0, 1.0, 1.0],
            [x0, y1, 0.0, 0.0],
            [x1, y1, 1.0, 0.0],
        ], dtype="f4").tobytes()
        self.help_vbo.write(verts)

        ctx = self.ctx
        ctx.screen.use()
        ctx.viewport = (0, 0, self.width, self.height)
        ctx.enable(moderngl.BLEND)
        ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        self.help_texture.use(location=0)
        try:
            self.prog_help["u_tex"].value = 0
        except KeyError:
            pass
        self.vao_help.render(mode=moderngl.TRIANGLE_STRIP)
        ctx.disable(moderngl.BLEND)

    def resize(self, w: int, h: int) -> None:
        if (w, h) == (self.width, self.height):
            return
        self.width, self.height = w, h
        # Release the old textures and FBOs so we don't leak GPU memory.
        for r in (
            "scene_tex", "scene_fbo",
            "bloom_tex_a", "bloom_tex_b",
            "bloom_fbo_a", "bloom_fbo_b",
        ):
            obj = getattr(self, r, None)
            if obj is not None:
                try: obj.release()
                except Exception: pass
        self._build_fbos(w, h)

    # ── per-frame writes from the visualizer ───────────────────────────────

    def set_ring_magnitudes(self, mags: np.ndarray) -> None:
        """Upload per-vertex magnitudes. `mags` is length N_SEGMENTS+1.
        We duplicate each one — both inner and outer vertices at the same
        angle get the same magnitude."""
        n = self.N_SEGMENTS
        # Interleave: [m0, m0, m1, m1, ...]. Faster than a Python loop.
        self._wave_mag_np[0::2] = mags
        self._wave_mag_np[1::2] = mags
        self.wave_mag_buf.write(self._wave_mag_np.tobytes())

    def set_particles(self, particles: Iterable[Tuple[float, float, float, float]]) -> None:
        """particles is iterable of (x, y, size, life). Drops anything past cap."""
        plist = list(particles)
        n = min(len(plist), self._particle_cap)
        if n == 0:
            self._n_particles = 0
            return
        self._particle_np[:n] = plist[:n]
        # Only upload the slice we use — saves bandwidth for sparse frames.
        self.particle_buf.write(self._particle_np[:n].tobytes())
        self._n_particles = n

    # ── render ─────────────────────────────────────────────────────────────

    def render(
        self,
        *,
        time: float,
        aspect: float,
        zoom: float,
        rotation: float,
        shake: Tuple[float, float],
        bass: float,
        pulse: float,
        energy: float,
        settings=None,
        target=None,
    ) -> None:
        """Run the full pipeline. All transient uniforms come in via kwargs
        so the call site reads like documentation.

        `settings` is an optional object with boolean attributes that gate
        the post-processing effects. Missing attributes default to True
        so the renderer works fine if no settings are supplied.

        `target` is an optional moderngl.Framebuffer to composite the final
        image into instead of the screen. The offline video renderer passes
        an offscreen FBO here so it can read the frame back; live mode
        leaves it None and draws straight to ctx.screen."""
        ctx = self.ctx
        w, h = self.width, self.height

        def _on(attr: str) -> bool:
            return True if settings is None else bool(getattr(settings, attr, True))

        # ── PASS 1: background + waveform + core + particles → scene FBO ───
        self.scene_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 1.0)

        # Background — pass-through, no blending. Audio uniforms drive the
        # starfield (if shader uses them). Wrapped in try/except because
        # GLSL drops unused uniforms during compile — if a shader edit
        # stops using one of these, we don't want a KeyError to crash the
        # renderer. The shader is the source of truth for which uniforms
        # matter, not this list.
        ctx.disable(moderngl.BLEND)
        for name, val in (
            ("u_time",     time),
            ("u_aspect",   aspect),
            ("u_bass",     bass),
            ("u_pulse",    pulse),
            ("u_energy",   energy),
            ("u_twinkle",  1.0 if _on("bg_twinkle")  else 0.0),
            ("u_drift",    1.0 if _on("bg_drift")    else 0.0),
            ("u_vignette", 1.0 if _on("bg_vignette") else 0.0),
        ):
            try:
                self.prog_background[name].value = val
            except KeyError:
                pass
        self.vao_background.render(moderngl.TRIANGLES)

        # Additive blending for everything from here onward in the scene
        # pass — bright shapes pile up to white instead of overwriting.
        ctx.enable(moderngl.BLEND)
        ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE)

        # Waveform.
        self.prog_waveform["u_aspect"].value   = aspect
        self.prog_waveform["u_zoom"].value     = zoom
        self.prog_waveform["u_rotation"].value = rotation
        self.prog_waveform["u_shake"].value    = shake
        self.vao_waveform.render(moderngl.TRIANGLE_STRIP)

        # Inner core ring — pulses with bass.
        self.prog_core["u_aspect"].value   = aspect
        self.prog_core["u_zoom"].value     = zoom
        self.prog_core["u_rotation"].value = rotation * 0.4
        self.prog_core["u_shake"].value    = shake
        self.prog_core["u_bass"].value     = bass
        self.vao_core.render(moderngl.TRIANGLE_STRIP)

        # Particles. Toggleable — when off, we skip the draw entirely
        # (no GPU work, and existing particles keep aging out via the
        # visualizer's update without showing).
        if self._n_particles > 0 and _on("particles"):
            ctx.enable(moderngl.PROGRAM_POINT_SIZE)
            self.prog_particle["u_aspect"].value = aspect
            self.prog_particle["u_zoom"].value   = zoom
            self.prog_particle["u_shake"].value  = shake
            self.prog_particle["u_color"].value  = (1.0, 0.55, 0.25)
            self.vao_particle.render(moderngl.POINTS, vertices=self._n_particles)

        ctx.disable(moderngl.BLEND)

        # ── PASS 2: extract bright pixels → bloom_tex_a ────────────────────
        self.bloom_fbo_a.use()
        ctx.viewport = (0, 0, self.bloom_w, self.bloom_h)
        ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.scene_tex.use(0)
        self.prog_extract["u_tex"].value = 0
        self.vao_extract.render(moderngl.TRIANGLES)

        # ── PASS 3: ping-pong separable Gaussian blur ──────────────────────
        # Each iteration = one horizontal then one vertical pass. After
        # BLOOM_ITERS iterations the result ends back in bloom_tex_a.
        for iteration in range(self.BLOOM_ITERS):
            # Horizontal: read a, write b.
            self.bloom_fbo_b.use()
            ctx.clear(0.0, 0.0, 0.0, 1.0)
            self.bloom_tex_a.use(0)
            self.prog_blur["u_tex"].value        = 0
            self.prog_blur["u_direction"].value  = (1.0, 0.0)
            self.prog_blur["u_resolution"].value = (self.bloom_w, self.bloom_h)
            # Widen radius on later iterations to push the halo further out.
            self.prog_blur["u_radius"].value     = 1.0 + iteration * 1.5
            self.vao_blur.render(moderngl.TRIANGLES)

            # Vertical: read b, write a.
            self.bloom_fbo_a.use()
            ctx.clear(0.0, 0.0, 0.0, 1.0)
            self.bloom_tex_b.use(0)
            self.prog_blur["u_direction"].value  = (0.0, 1.0)
            self.vao_blur.render(moderngl.TRIANGLES)

        # ── PASS 4: composite scene + bloom → screen (or offscreen target) ─
        # Bloom and chromatic aberration are toggleable — we just zero
        # the strength uniforms when disabled. The bloom passes above
        # still run (negligible cost), but their contribution disappears.
        # `target` redirects the final image to an offscreen FBO for the
        # offline video renderer; None means draw to the visible screen.
        out_fbo = target if target is not None else ctx.screen
        out_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.scene_tex.use(0)
        self.bloom_tex_a.use(1)
        self.prog_composite["u_scene"].value           = 0
        self.prog_composite["u_bloom"].value           = 1
        self.prog_composite["u_bloom_strength"].value  = 1.35  if _on("bloom")     else 0.0
        self.prog_composite["u_chromatic"].value       = 0.005 if _on("chromatic") else 0.0
        self.vao_composite.render(moderngl.TRIANGLES)
