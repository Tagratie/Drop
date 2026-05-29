"""TikTok-style VLC player for the library feed view."""
import os
import sys
import time
import math
import colorsys
import subprocess
import threading
import tkinter as tk
from pathlib import Path

from .theme import (
    BG, BG2, BG3, ACCENT, ACCENT_D, TEXT, MUTED, SOFT, ERROR,
    VIDEO_EXTS, AUDIO_EXTS, NO_WINDOW,
)
from .utils import detect_kind, humanize_size, humanize_time, open_path, get_bin
from .widgets import (
    RoundedButton, DotsButton, InfoButton, IconButton, VolumeSlider,
    alert as _alert_modal,
)
from .visualizer_launcher import VisualizerLauncher

# Optional: real video playback via VLC.
try:
    import vlc
    _VLC_OK = True
except Exception:
    vlc = None
    _VLC_OK = False


class FeedPlayer:
    """Single VLC player + a stack of feed cards. One instance, swap media in/out."""

    def __init__(self, master, app):
        self.app    = app
        self.master = master
        self.items  = []     # list of dicts (library entries)
        self.index  = 0
        self.muted  = False  # play with sound by default
        # Per-app volume (0–100). Affects only this VLC instance via
        # audio_set_volume — NOT Windows' system mixer. Persisted in the
        # geom file so the user's preferred level survives restarts.
        self.volume = max(0, min(100, int(app._geom.get("volume", 80))))
        self._vlc_inst = None
        self._player   = None
        self._poll_id  = None
        self._loaded_idx = None   # idx whose position should be persisted on next switch
        # Aspect ratio of the currently loaded video, or 1.0 if unknown / audio.
        # Updated once VLC reports real dimensions for the playing media.
        # Always the DECODER-native aspect (w/h before any rotation).
        self._aspect = 1.0
        # Per-item display rotation in degrees: 0, 90, 180, or 270. Reset on
        # each _load_current() from the item's saved value. Applied to VLC
        # via the transform video-filter at load time; rotate_video() bumps
        # this and reloads the media in place to apply mid-playback.
        self._rotation = 0

        # Outer container.
        self.frame = tk.Frame(master, bg=BG)

        # Top header strip (title + meta) — sibling of the video surface, NOT a child.
        # VLC paints into the surface's window handle, which would cover anything inside it.
        self.overlay_top = tk.Frame(self.frame, bg=BG)

        # Title row: short title + a chevron that toggles full-text expansion.
        # When expanded, the full title overlays the top of the video instead
        # of pushing it down — keeps video sizing independent of title length.
        self.title_row = tk.Frame(self.overlay_top, bg=BG)
        self.title_row.pack(fill="x")

        self.title_lbl = tk.Label(self.title_row, text="", bg=BG, fg=TEXT,
                                   font=app.f_card_t, anchor="w",
                                   justify="left")
        self.title_lbl.pack(side="left", padx=4, pady=(2, 2))

        self.title_expand_btn = tk.Label(
            self.title_row, text="\u25BE", bg=BG, fg=SOFT,
            font=(app.f_btn[0], 10), cursor="hand2", padx=6,
        )
        # not packed by default; only shown when title is truncated
        self.title_expand_btn.bind("<Button-1>", lambda e: self._toggle_title_expand())
        self.title_expand_btn.bind("<Enter>",
            lambda e: self.title_expand_btn.configure(fg=TEXT))
        self.title_expand_btn.bind("<Leave>",
            lambda e: self.title_expand_btn.configure(fg=SOFT))

        self.meta_lbl = tk.Label(self.overlay_top, text="", bg=BG, fg=SOFT,
                                  font=app.f_meta, anchor="w")
        self.meta_lbl.pack(fill="x", padx=4, pady=(0, 6))
        self.overlay_top.pack(side="top", fill="x")

        # Title expansion state
        self._title_full      = ""    # full title from item
        self._title_collapsed = ""    # 5-word truncated form
        self._title_expanded  = False
        # Floating overlay for full title — placed over the surface when expanded.
        self.title_overlay = tk.Label(
            self.frame, text="", bg="#000", fg=TEXT,
            font=app.f_card_t, anchor="nw", justify="left",
            wraplength=460, padx=12, pady=10,
        )
        # not placed by default

        # Bottom controls strip — also a sibling, packed before the surface
        # so it owns its space. Generous top padding pushes the strip away
        # from the video frame so the controls breathe instead of crowding
        # the bottom edge of whatever's playing.
        self.overlay_bot = tk.Frame(self.frame, bg=BG)
        self.overlay_bot.pack(side="bottom", fill="x", pady=(18, 4))

        self.counter_lbl = tk.Label(self.overlay_bot, text="", bg=BG, fg=SOFT,
                                     font=app.f_meta)
        self.counter_lbl.pack(side="left", padx=(4, 12), pady=4)

        # Keyboard hint — hidden by default, toggled via info button.
        self.kbd_hint = tk.Label(
            self.overlay_bot,
            text="\u2191\u2193  switch \u00B7  \u2190\u2192  seek 5s \u00B7  SPACE  pause \u00B7  M  mute",
            bg=BG, fg=MUTED, font=app.f_meta,
        )
        self._kbd_visible = False
        # not packed by default

        # All three right-side controls share a fixed height for visual rhythm.
        BTN_H = 30

        # Volume slider — in-app level for THIS VLC player only. Sits at
        # the far-left of the right-side cluster so it reads as a distinct
        # control surface (not just another action button). Does not touch
        # Windows' system volume.
        self.volume_slider = VolumeSlider(
            self.overlay_bot,
            on_change=self._on_volume_changed,
            on_mute=self._on_volume_mute_clicked,
            initial=self.volume,
            muted=self.muted,
            width=120, height=BTN_H, radius=8,
        )
        self.volume_slider.pack(side="right", padx=(6, 0))

        # Info button — toggles the keyboard-shortcut hint above.
        self.info_btn = IconButton(
            self.overlay_bot, icon_name="info",
            command=self._toggle_kbd_hint,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            active_bg=ACCENT, active_fg="#000",
            width=BTN_H, height=BTN_H, radius=8, icon_size=18,
            active=False,
        )
        self.info_btn.pack(side="right", padx=(6, 0))

        self.open_btn = RoundedButton(
            self.overlay_bot, text="OPEN \u2197", command=self._open_external,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            font=app.f_chip, padx=10, pady=4, radius=8,
        )
        self.open_btn.configure(height=BTN_H)
        self.open_btn.pack(side="right", padx=(6, 0))

        # Rotate-90 button — only meaningful for video items. _load_current
        # toggles its visibility based on the loaded media's extension.
        self.rotate_btn = IconButton(
            self.overlay_bot, icon_name="rotate",
            command=self._rotate_current,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            width=BTN_H, height=BTN_H, radius=8, icon_size=18,
        )
        self.rotate_btn.pack(side="right", padx=(6, 0))

        # Three-dot menu — uses cached PNG icon for crisp rendering.
        self.menu_btn = IconButton(
            self.overlay_bot, icon_name="dots",
            command=self._menu_button_click,
            bg=BG2, fg=TEXT, hover_bg=BG3,
            width=BTN_H, height=BTN_H, radius=8, icon_size=18,
        )
        self.menu_btn.pack(side="right", padx=(6, 0))

        # The video surface fills whatever's left between header and controls.
        # We size it preserving the video's aspect ratio so non-square content
        # isn't squished into a square box.
        self.surface_holder = tk.Frame(self.frame, bg=BG)
        self.surface_holder.pack(side="top", fill="both", expand=True)

        # VLC owns this widget exclusively.
        self.surface = tk.Frame(self.surface_holder, bg="#000", highlightthickness=0)
        # No pack/grid — placed via _center_surface() on every Configure.
        self.surface_holder.bind("<Configure>", self._center_surface)

        # Center hint floats over the surface — but only briefly. Since it lives
        # *inside* the surface, VLC will paint over it once playback starts;
        # that's fine because we only show it when paused or for fallback states.
        self.center_hint = tk.Label(self.surface, text="", bg="#000", fg=TEXT,
                                     font=app.f_h1)

        # Audio visualizer canvas — child of surface_holder, NOT surface.
        # Surface is owned by VLC via set_hwnd(); on Windows that means VLC's
        # window paints over any Tk widget inside surface. Sibling-of-surface
        # placement lets Tk draw on the canvas while audio plays. For video
        # playback we hide the canvas so VLC's surface is visible.
        self.viz_canvas = tk.Canvas(self.surface_holder, bg=BG,
                                     highlightthickness=0, bd=0,
                                     takefocus=0)
        # Audio-only mode shows an "Open Visualizer" button on this canvas
        # that launches the external GLFW visualizer in its own window.
        # The visualizer captures system audio via loopback, so it follows
        # whatever VLC is currently playing — no inter-process audio
        # routing needed.
        self.viz_launcher = VisualizerLauncher()
        # Handles to the button widget + its canvas item + the <Configure>
        # binding that re-centers it on resize. All cleared in _stop_audio_viz.
        self._viz_btn      = None
        self._viz_btn_id   = None
        self._viz_btn_bind = None
        # Click-through on empty canvas area still toggles pause, matching
        # the surface click behavior.
        self.viz_canvas.bind("<Button-1>", self._toggle_pause)
        self.viz_canvas.bind("<Button-3>", self._menu_event)

        # Empty / fallback state for when there are no items
        self.empty_lbl = tk.Label(self.frame, text="", bg=BG, fg=MUTED,
                                   font=app.f_meta, justify="center")

        # Click on the surface = play/pause
        self.surface.bind("<Button-1>", self._toggle_pause)
        self.center_hint.bind("<Button-1>", self._toggle_pause)

        # Right-click on the surface opens the item menu
        self.surface.bind("<Button-3>", self._menu_event)

        # Wheel + keys for navigation
        self.frame.bind_all("<Up>",    self._wheel_prev_key, add="+")
        self.frame.bind_all("<Down>",  self._wheel_next_key, add="+")
        self.frame.bind_all("<Left>",  self._seek_back_key,  add="+")
        self.frame.bind_all("<Right>", self._seek_fwd_key,   add="+")

    # ── feed control ─────────────────────────────────────────────────────────
    def set_items(self, items):
        self.items = list(items)
        self.index = 0
        if not self.items:
            self._show_empty()
            return
        self._hide_empty()
        self._load_current()

    def next(self):
        if not self.items: return
        if self.index < len(self.items) - 1:
            self.index += 1
            self._load_current()

    def prev(self):
        if not self.items: return
        if self.index > 0:
            self.index -= 1
            self._load_current()

    # ── media loading ────────────────────────────────────────────────────────
    def _load_current(self):
        # Save the previous video's playback position before switching, so
        # the user can resume where they left off.
        self._save_resume_position()

        item = self.items[self.index]
        path = item.get("path")
        ext = Path(path).suffix.lower() if path else ""
        # Update overlay text
        self._set_title(item.get("title") or "—")
        bits = [item.get("source") or "—"]
        if item.get("size"):
            bits.append(humanize_size(item["size"]))
        bits.append(humanize_time(item.get("completed_at", time.time())))
        self.meta_lbl.configure(text="  ·  ".join(bits))
        self.counter_lbl.configure(text=f"{self.index + 1} / {len(self.items)}")

        # Remember which index is currently playing so the next transition
        # knows whose position to save.
        self._loaded_idx = self.index

        # Reset aspect to a neutral square until VLC reports the real one.
        # Otherwise rapid-switching could leave a stale aspect from the
        # previous video while the new one is still loading.
        self._aspect = 1.0
        # Pull this video's saved rotation. Normalize bad values (legacy
        # data, manual edits) back to 0 so VLC doesn't get a garbage angle.
        rot = item.get("rotation", 0) or 0
        self._rotation = rot if rot in (0, 90, 180, 270) else 0
        self._do_center_surface()

        # Rotate button is only useful for video.
        self._set_rotate_btn_visible(ext in VIDEO_EXTS)

        if not path or not os.path.exists(path):
            self._show_unavailable("File not found")
            return

        if ext in VIDEO_EXTS and _VLC_OK:
            self._play_video(path)
        elif ext in AUDIO_EXTS and _VLC_OK:
            self._play_audio(path)
        else:
            # Unsupported in-app — show a card with OPEN button
            self._show_unavailable(
                "VLC not installed" if not _VLC_OK else "Open externally"
            )

    def _save_resume_position(self):
        """Stash the currently-playing item's position into the library."""
        if not getattr(self.app, "resume_enabled", True):
            return
        loaded = getattr(self, "_loaded_idx", None)
        if loaded is None or not self._player:
            return
        if loaded < 0 or loaded >= len(self.items):
            return
        try:
            ms     = self._player.get_time()
            length = self._player.get_length()
        except Exception:
            return
        if ms < 0 or length <= 0:
            return
        # Don't save resume if we barely watched it (under 5s) or finished it
        # (within 5s of the end) — both are "no resume needed" signals.
        if ms < 5000 or ms > length - 5000:
            self._clear_resume_position(loaded)
            return
        item = self.items[loaded]
        item["resume_at"] = int(ms)
        # Persist the change. items_in returns the actual list, so mutating
        # an element mutates the library — but we still need to save() to disk.
        try:
            self.app.library.save()
        except Exception:
            pass

    def _clear_resume_position(self, idx):
        if idx is None or not (0 <= idx < len(self.items)):
            return
        item = self.items[idx]
        if "resume_at" in item:
            try:
                del item["resume_at"]
                self.app.library.save()
            except Exception:
                pass

    def _ensure_vlc(self, rotation=0):
        """Lazy-create the VLC instance + media player. If a previous
        instance exists but was built for a different rotation, tear it down
        and rebuild — libvlc 3.x's `transform` video-filter only applies
        reliably when it's an INSTANCE option (set at vlc.Instance creation
        time). Adding it as a per-media option reloads the decoder but the
        filter never makes it into the render pipeline, which is why earlier
        attempts produced a flicker without an actual rotation."""
        want = rotation if rotation in (90, 180, 270) else 0
        if self._vlc_inst is not None and getattr(self, "_inst_rotation", 0) == want:
            return
        # Tear down whatever we have. release() drops the C-side handles —
        # without it, repeated rotations would leak ~3-4MB per cycle.
        if self._player is not None:
            try: self._player.stop()
            except Exception: pass
            try: self._player.release()
            except Exception: pass
            self._player = None
        if self._vlc_inst is not None:
            try: self._vlc_inst.release()
            except Exception: pass
            self._vlc_inst = None
        # OSD suppression: --no-video-title-show alone isn't enough on some
        # libvlc builds — when a media is set the player briefly flashes
        # the source URI/path through the generic OSD channel anyway.
        # --no-osd + --video-title-timeout=0 belt-and-braces the popup
        # so a rotation reload doesn't show "C:\Users\…\file.mp4" for a
        # frame before VLC's first real frame paints.
        args = [
            "--quiet",
            "--no-video-title-show",
            "--no-osd",
            "--video-title-timeout=0",
        ]
        if want:
            # transform is libvlc's discrete 90°/180°/270°/flip filter. Cheap
            # (no resample), pixel-perfect for multiples of 90°.
            args += ["--video-filter=transform", f"--transform-type={want}"]
        self._vlc_inst = vlc.Instance(*args)
        self._player = self._vlc_inst.media_player_new()
        self._inst_rotation = want
        self._attach_surface()

    def _attach_surface(self):
        try:
            handle = self.surface.winfo_id()
            if sys.platform == "win32":
                self._player.set_hwnd(handle)
            elif sys.platform == "darwin":
                self._player.set_nsobject(handle)
            else:
                self._player.set_xwindow(handle)
        except Exception:
            pass

    def _play_video(self, path):
        # Pass the current rotation so _ensure_vlc rebuilds the instance
        # with the matching transform filter if needed.
        self._ensure_vlc(self._rotation)
        self.center_hint.place_forget()
        # Switching to video — kill any audio visualizer leftover from the
        # previous track. VLC paints the surface for video; the canvas
        # would only get covered anyway, but the render-loop after() call
        # would still be running and wasting cycles.
        self._stop_audio_viz()
        try:
            media = self._vlc_inst.media_new(path)
            media.add_option("input-repeat=65535")  # loop
            self._player.set_media(media)
            self._player.audio_set_mute(self.muted)
            self._player.play()
            self._apply_volume()
            # Seek to saved resume position if there is one and the toggle is on.
            self._maybe_seek_resume()
            # Once VLC reads the file far enough to know its dimensions,
            # re-fit the surface to match the real aspect ratio. Polled
            # because video_get_size() returns (0, 0) until then.
            self._wait_for_video_size()
        except Exception as e:
            self._show_unavailable(str(e))

    def _rotate_current(self):
        """Player-toolbar handler: rotate whatever is loaded right now.
        Also nudges the host app to re-render the matching library tile's
        thumbnail so the still image picks up the new orientation —
        otherwise it'd stay stale until the user re-entered the library."""
        if 0 <= self.index < len(self.items):
            self.rotate_video(self.index, step=90)
            refresh = getattr(self.app, "_refresh_tile_thumb", None)
            if callable(refresh):
                try: refresh(self.index)
                except Exception: pass

    def _set_rotate_btn_visible(self, on):
        """Show the rotate button only when a video is loaded — audio has
        no frame to spin, so the button would be confusing on audio items."""
        btn = getattr(self, "rotate_btn", None)
        if btn is None:
            return
        try:
            if on:
                # Re-pack just before the dots menu (to its left). Order from
                # right→left in overlay_bot is: menu, rotate, open, info, …
                # so anchoring it next to menu_btn keeps the visual rhythm.
                btn.pack(side="right", padx=(6, 0),
                         before=self.menu_btn)
            else:
                btn.pack_forget()
        except Exception:
            pass

    def rotate_video(self, idx, step=90):
        """Bump rotation for item `idx` by `step` degrees (default 90),
        persist it, and re-apply VLC's transform filter if that item is the
        one currently loaded.

        VLC can't change the transform filter on a live media — the filter
        is part of the decoder pipeline that gets set up at media_new(). So
        when we're rotating the currently-playing video, we capture the
        position, stop, rebuild the media with the new transform option,
        play, then seek back. Brief pause but no perceived position loss."""
        if not (0 <= idx < len(self.items)):
            return
        item = self.items[idx]
        path = item.get("path")
        ext = Path(path).suffix.lower() if path else ""
        # Audio has no video output to rotate; silently skip rather than
        # store a value that would never be applied.
        if ext not in VIDEO_EXTS:
            return

        cur = int(item.get("rotation", 0) or 0)
        new_rot = (cur + step) % 360
        if new_rot not in (0, 90, 180, 270):
            new_rot = 0
        item["rotation"] = new_rot
        try: self.app.library.save()
        except Exception: pass

        if getattr(self, "_loaded_idx", None) != idx or not self._player:
            return  # not currently playing this one — nothing else to do

        # Capture position + play state so the reload feels seamless.
        pos_ms = 0
        was_playing = False
        try:
            pos_ms = max(0, self._player.get_time())
            was_playing = self._player.is_playing() == 1
        except Exception:
            pass

        # Tear down any stale rotation overlay from a previous build of
        # this code path — defensive, since we no longer create one.
        prev_overlay = getattr(self, "_rot_overlay", None)
        if prev_overlay is not None:
            self._destroy_rotation_overlay(prev_overlay)
            self._rot_overlay = None

        self._rotation = new_rot

        # Direct rebuild — no snapshot, no animation. Every overlay-based
        # transition we tried (rotating PNG, pre-rendered keyframes,
        # first-frame-poll handoff) introduced its own visual artifact:
        # frozen thumbnails stuck in a corner, mid-animation jitter,
        # stale image overlapping live VLC output. libvlc 3.x just can't
        # change `transform-type` without an instance rebuild, and the
        # rebuild has an unavoidable ~150-400ms gap before its first
        # frame paints. A brief clean black during that gap reads as a
        # natural cut and survives rapid rotate-clicks without spawning
        # racing overlays.
        try:
            self._ensure_vlc(new_rot)
            media = self._vlc_inst.media_new(path)
            media.add_option("input-repeat=65535")
            # Bake the saved playback position into the media itself via
            # libvlc's :start-time option. Previously we called play() then
            # set_time(pos) on a 140ms delay — but VLC decoded and PAINTED
            # the file's frame 0 in that window, which is precisely what
            # ffmpeg used to extract the cached thumbnail. The user saw it
            # as "flashes to the thumbnail when rotating." With :start-time
            # baked in, the first decoded frame is already at the saved
            # position and frame 0 is never painted.
            if pos_ms > 100:
                media.add_option(f":start-time={pos_ms / 1000.0:.3f}")
            self._player.set_media(media)
            self._player.audio_set_mute(self.muted)
            self._player.play()
            self._apply_volume()
            self._do_center_surface()

            # Capture this generation's player object. Any deferred call
            # below that touches libvlc must verify self._player is still
            # the SAME object — _ensure_vlc(new_rot) inside a follow-up
            # rotation releases the C handle, and a bound method like
            # `self._player.pause` would call into freed memory and
            # access-violate. The identity check makes the deferred work
            # a no-op once the player has been swapped out.
            my_player = self._player
            if not was_playing:
                # Poll for libvlc's first-frame signal and pause AS SOON
                # AS it arrives, instead of waiting a fixed 220ms. That
                # fixed wait let VLC play ~tenth of a second of decoded
                # frames forward before the snap-back fired, visible to
                # the user as "video plays a bit further each rotation"
                # even though the position resets after. video_get_size
                # going nonzero is the cheapest "first frame is ready"
                # signal libvlc exposes.
                def _pause_when_ready(p=my_player, waited=0):
                    if self._player is not p:
                        return
                    try:
                        w, h = p.video_get_size(0)
                    except Exception:
                        w = h = 0
                    if (w > 0 and h > 0) or waited >= 500:
                        try:
                            p.pause()
                            if pos_ms > 0:
                                p.set_time(int(pos_ms))
                        except Exception:
                            pass
                        return
                    self.frame.after(10, lambda: _pause_when_ready(p, waited + 10))
                # Start polling 10ms after play() so libvlc has a tick to
                # actually start the decoder pipeline.
                self.frame.after(10, _pause_when_ready)
        except Exception:
            pass

    def _destroy_rotation_overlay(self, overlay):
        if overlay is None:
            return
        overlay["alive"] = False
        try: overlay["label"].destroy()
        except Exception: pass

    def _wait_for_video_size(self, attempts=24):
        """Poll VLC for the loaded video's real dimensions, then re-fit the
        surface so the aspect ratio is preserved. Bounded so we don't poll
        forever on broken files. ~3.5 seconds total at 150ms intervals."""
        if attempts <= 0 or not self._player:
            return
        try:
            w, h = self._player.video_get_size(0)
            if w > 0 and h > 0:
                self._aspect = w / h
                self._do_center_surface()
                return
        except Exception:
            pass
        try:
            self.frame.after(150,
                              lambda: self._wait_for_video_size(attempts - 1))
        except Exception:
            pass

    def _maybe_seek_resume(self):
        if not getattr(self.app, "resume_enabled", True):
            return
        item = self.items[self.index] if 0 <= self.index < len(self.items) else None
        if not item:
            return
        ms = item.get("resume_at")
        if not ms or ms < 5000:
            return
        # VLC needs a beat to actually start playback before set_time works
        # reliably. Defer briefly.
        def _seek():
            try:
                if self._player and self._player.is_playing():
                    self._player.set_time(int(ms))
                    # Brief on-screen confirmation
                    self.center_hint.configure(
                        text=f"\u23EF  Resumed at {ms // 60000}:{(ms // 1000) % 60:02d}"
                    )
                    self.center_hint.place(relx=0.5, rely=0.5, anchor="center")
                    self.frame.after(900,
                                      lambda: self.center_hint.place_forget())
            except Exception:
                pass
        self.frame.after(220, _seek)

    def _play_audio(self, path):
        # Same as video — VLC just won't render anything but plays sound.
        # Audio has no frame to rotate, so always request a rotation-0
        # instance (cheap no-op if we already have one).
        self._ensure_vlc(0)
        # Tease + visualizer prompt while we set up. The text stays visible
        # for ~1.5s before the visualizer takes over; if numpy is missing
        # or FFT fails, the text stays as the audio fallback.
        self.center_hint.configure(text="\u266B  AUDIO")
        self.center_hint.place(relx=0.5, rely=0.5, anchor="center")
        # Keep the audio "frame" square (aspect=1.0 from _load_current reset).
        try:
            media = self._vlc_inst.media_new(path)
            media.add_option("input-repeat=65535")
            self._player.set_media(media)
            self._player.audio_set_mute(False)  # audio files unmuted by default
            # Audio files force-unmute, so reflect that in the slider too —
            # otherwise the speaker glyph and the actual audio state drift.
            self.muted = False
            try: self.volume_slider.set_muted(False)
            except Exception: pass
            self._player.play()
            self._apply_volume()
        except Exception as e:
            self._show_unavailable(str(e))
            return
        # Spin up the visualizer. It pre-computes FFT in a worker thread,
        # then the render loop pulls data based on VLC's playback time.
        self._start_audio_viz(path)

    def _show_unavailable(self, msg):
        self.stop()
        self.center_hint.configure(text=msg)
        self.center_hint.place(relx=0.5, rely=0.5, anchor="center")

    # ── controls ─────────────────────────────────────────────────────────────
    def _toggle_pause(self, _e=None):
        if not self._player: return
        now = time.time()
        if now - getattr(self, "_pause_toggle_at", 0) < 0.10:
            return
        self._pause_toggle_at = now
        try:
            if self._player.is_playing():
                self._player.pause()
                self.center_hint.configure(text="\u23F8  PAUSED")
                self.center_hint.place(relx=0.5, rely=0.5, anchor="center")
            else:
                self._player.play()
                self.center_hint.place_forget()
        except Exception:
            pass

    def toggle_mute(self):
        # Triggered by the M key OR by clicking the speaker in the volume
        # slider. Flips VLC's per-player mute state and syncs the slider's
        # glyph so the UI matches whatever the actual audio state is.
        self.muted = not self.muted
        if self._player:
            try: self._player.audio_set_mute(self.muted)
            except Exception: pass
        try: self.volume_slider.set_muted(self.muted)
        except Exception: pass
        # Brief on-screen confirmation
        self.center_hint.configure(text="MUTED" if self.muted else "SOUND ON")
        self.center_hint.place(relx=0.5, rely=0.5, anchor="center")
        if getattr(self, "_mute_hint_after", None):
            try: self.frame.after_cancel(self._mute_hint_after)
            except Exception: pass
        self._mute_hint_after = self.frame.after(
            600, lambda: self.center_hint.place_forget())

    # ── volume ───────────────────────────────────────────────────────────────
    def _apply_volume(self):
        """Push self.volume into the live VLC player. Called after every
        media swap because libvlc resets volume per-media on some
        backends, and after the user drags the slider."""
        if not self._player:
            return
        try:
            self._player.audio_set_volume(int(self.volume))
        except Exception:
            pass
        # While the external visualizer is running we temporarily mute the
        # internal VLC. Keep its restore-target in sync with the user's
        # current preference so closing the viz doesn't snap volume back
        # to whatever it was when viz started.
        if hasattr(self, "_viz_saved_vol"):
            self._viz_saved_vol = int(self.volume)

    def _on_volume_changed(self, value):
        """Slider callback — wires the visual control to the actual VLC
        player. Only affects this app's audio stream; Windows' system
        volume is untouched."""
        self.volume = max(0, min(100, int(value)))
        # Dragging the bar above 0 implicitly un-mutes — matches what every
        # other player does (YouTube, VLC's own UI, Spotify). Without this,
        # the slider can look like it's set to 60% but produce no sound,
        # which reads as broken.
        if self.muted and self.volume > 0:
            self.muted = False
            try: self._player.audio_set_mute(False)
            except Exception: pass
            try: self.volume_slider.set_muted(False)
            except Exception: pass
        self._apply_volume()
        # Persist via the app's debounced save path. Dragging fires many
        # _on_volume_changed calls; piggy-back on the same _save_pending
        # flag _on_configure uses so we coalesce to one disk write per
        # 500ms regardless of drag speed.
        try:
            self.app._geom["volume"] = self.volume
            if not self.app._save_pending:
                self.app._save_pending = True
                self.app.root.after(500, self.app._do_save_geom)
        except Exception:
            pass

    def _on_volume_mute_clicked(self):
        """Slider speaker-icon click — same path as the M shortcut."""
        self.toggle_mute()

    # ── title truncation + expand ────────────────────────────────────────────
    MAX_TITLE_WORDS = 5

    def _set_title(self, full_title):
        self._title_full = full_title or "—"
        words = self._title_full.split()
        if len(words) > self.MAX_TITLE_WORDS:
            self._title_collapsed = " ".join(words[:self.MAX_TITLE_WORDS]) + "\u2026"
            needs_btn = True
        else:
            self._title_collapsed = self._title_full
            needs_btn = False
        # Reset to collapsed state on every new video
        self._title_expanded = False
        self.title_lbl.configure(text=self._title_collapsed)
        try:
            self.title_overlay.place_forget()
        except Exception:
            pass
        # Show / hide the expand chevron
        if needs_btn:
            self.title_expand_btn.configure(text="\u25BE")
            if not self.title_expand_btn.winfo_ismapped():
                self.title_expand_btn.pack(side="left")
        else:
            try: self.title_expand_btn.pack_forget()
            except Exception: pass

    def _toggle_title_expand(self):
        if not self._title_full or self._title_collapsed == self._title_full:
            return
        self._title_expanded = not self._title_expanded
        if self._title_expanded:
            # Place full-text overlay over the top of the surface holder.
            try:
                # Wrap based on current holder width minus padding
                holder_w = self.surface_holder.winfo_width()
                self.title_overlay.configure(
                    text=self._title_full,
                    wraplength=max(200, holder_w - 24),
                )
                # Position over the surface_holder (which sits between top/bot)
                # We use place with in_=surface_holder so it follows resizes.
                self.title_overlay.place(
                    in_=self.surface_holder, x=0, y=0, relwidth=1, anchor="nw",
                )
                self.title_overlay.lift()
            except Exception:
                pass
            self.title_expand_btn.configure(text="\u25B4")  # up chevron
        else:
            try: self.title_overlay.place_forget()
            except Exception: pass
            self.title_expand_btn.configure(text="\u25BE")  # down chevron

    # ── keyboard-hint toggle ─────────────────────────────────────────────────
    def _toggle_kbd_hint(self):
        # Throttle: rapid pack/unpack of the hint label can deadlock Tk's
        # geometry manager on Windows when SPACE/pause events fire in parallel.
        now = time.time()
        if now - getattr(self, "_kbd_toggle_at", 0) < 0.20:
            return
        self._kbd_toggle_at = now

        self._kbd_visible = not self._kbd_visible
        if self._kbd_visible:
            # Pack between counter and the right-side buttons.
            self.kbd_hint.pack(side="left", pady=4, after=self.counter_lbl)
        else:
            try: self.kbd_hint.pack_forget()
            except Exception: pass
        try: self.info_btn.set_active(self._kbd_visible)
        except Exception: pass

    def _open_external(self):
        if not self.items: return
        path = self.items[self.index].get("path")
        if path: open_path(path)

    def _menu_button_click(self):
        # Pop menu near the button
        if not self.items: return
        btn = self.menu_btn
        x = btn.winfo_rootx()
        y = btn.winfo_rooty()
        class _E: pass
        e = _E(); e.x_root = x; e.y_root = y
        self.app._show_card_menu(e, self.index)

    def _menu_event(self, event):
        if not self.items: return
        self.app._show_card_menu(event, self.index)

    def _wheel_prev_key(self, _e):
        if self.app.mode == "library" and self.app.lib_view == "feed":
            self.prev()

    def _wheel_next_key(self, _e):
        if self.app.mode == "library" and self.app.lib_view == "feed":
            self.next()

    def _seek_back_key(self, _e):
        if self.app.mode == "library" and self.app.lib_view == "feed":
            self.seek_relative(-5000)

    def _seek_fwd_key(self, _e):
        if self.app.mode == "library" and self.app.lib_view == "feed":
            self.seek_relative(+5000)

    def seek_relative(self, ms):
        """Seek the player by `ms` milliseconds (negative = backward)."""
        if not self._player:
            return
        try:
            cur = self._player.get_time()  # ms; -1 if no media
            length = self._player.get_length()
            if cur < 0:
                return
            new = cur + ms
            if length > 0:
                new = max(0, min(new, max(0, length - 200)))
            else:
                new = max(0, new)
            self._player.set_time(int(new))
            # Briefly show a seek indicator
            arrow = "\u23E9" if ms > 0 else "\u23EA"
            self.center_hint.configure(text=f"{arrow}  {abs(ms)//1000}s")
            self.center_hint.place(relx=0.5, rely=0.5, anchor="center")
            if getattr(self, "_seek_hint_after", None):
                try: self.frame.after_cancel(self._seek_hint_after)
                except Exception: pass
            self._seek_hint_after = self.frame.after(
                500, lambda: self.center_hint.place_forget())
        except Exception:
            pass

    # ── audio visualizer ───────────────────────────────────────────────────
    # Audio-only mode used to render an inline PIL-based "bars in a ring"
    # visualizer directly on viz_canvas. That's been removed; we now just
    # show a clean canvas-native launcher (icon + title + pill button) that
    # opens the external GLFW visualizer in its own window.
    #
    # The external visualizer plays the audio FILE itself (via ffmpeg +
    # sounddevice) rather than capturing system audio loopback — which
    # means it reacts to ONLY Drop's audio, not to other apps. Drop's VLC
    # gets paused while the visualizer is open so we don't have two
    # streams playing the same file out of sync.

    def _start_audio_viz(self, audio_path):
        """Audio-only playback: park the VLC surface offscreen and draw
        the visualizer launcher UI on viz_canvas. Remembers audio_path
        so the launcher click can pass it to the external visualizer."""
        self._stop_audio_viz()
        # Remember the file path — the launcher button needs it to tell
        # the external visualizer which file to play.
        self._viz_audio_path = audio_path

        # Park the VLC-owned surface offscreen. HWND stays valid so audio
        # playback continues; the win32 window paints where we don't see.
        try:
            self.surface.place(x=-9999, y=-9999, width=200, height=200)
        except Exception as e:
            print(f"[viz] surface offscreen failed: {e}", file=sys.stderr)

        # Show the canvas; clear any residue from a previous track.
        try:
            self.viz_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        except Exception as e:
            print(f"[viz] place failed: {e}", file=sys.stderr)
            return

        self._draw_viz_launcher()
        # Redraw on resize so the launcher stays centered.
        self._viz_btn_bind = self.viz_canvas.bind(
            "<Configure>", lambda _e: self._draw_viz_launcher(), add="+",
        )
        # Start the Spotify-style bouncing bar animation. Stops cleanly
        # via _stop_audio_viz when audio playback ends or switches.
        self._viz_anim_active = True
        self._animate_viz_bars()

    def _draw_viz_launcher(self):
        """Draw the visualizer launcher UI on viz_canvas — icon + title +
        subtitle + pill button. Pure canvas drawing (no nested widgets)
        so we get pixel-perfect control and consistent rendering across
        Windows DPI/theme settings."""
        canvas = self.viz_canvas
        canvas.delete("viz_ui")

        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 50 or h < 50:
            return

        cx, cy = w // 2, h // 2

        # ── Four-bar waveform icon ─────────────────────────────────────
        # Animated like the Spotify "now playing" indicator — each bar
        # has its own min/max height and phase offset, and _animate_viz_bars
        # updates their coords on a 30fps tick. The per-bar tags
        # ("viz_bar_0".."viz_bar_3") let the animation address each one
        # by tag — robust across redraws on canvas <Configure>.
        self._viz_bar_cfg = [
            # (x_offset, min_height, max_height, phase_offset_radians)
            (-24,  6, 22, 0.0),
            ( -8,  6, 34, 1.7),
            (  8,  6, 38, 3.4),
            ( 24,  6, 24, 5.1),
        ]
        bar_w = 6
        icon_y = cy - 95
        for i, (ox, hmin, hmax, _ph) in enumerate(self._viz_bar_cfg):
            h0 = (hmin + hmax) // 2     # midway height for initial render
            x = cx + ox - bar_w // 2
            canvas.create_rectangle(
                x, icon_y - h0 // 2, x + bar_w, icon_y + h0 // 2,
                fill=SOFT, outline="",
                tags=("viz_ui", f"viz_bar_{i}"),
            )

        # ── Title + subtitle ───────────────────────────────────────────
        canvas.create_text(
            cx, cy - 40,
            text="VISUALIZER",
            fill=TEXT,
            font=self.app.f_card_t,
            tags="viz_ui",
        )
        canvas.create_text(
            cx, cy - 12,
            text="Opens in a separate window",
            fill=MUTED,
            font=self.app.f_meta,
            tags="viz_ui",
        )

        # ── Pill button ────────────────────────────────────────────────
        # White pill with dark text — matches Drop's active-tab pill style
        # (the "Songs" / "Edits" highlighted buttons in the songs view).
        # Built from two end-cap ovals + a center rectangle, all sharing
        # the viz_btn_bg tag so a single itemconfig recolors them all for
        # hover state.
        btn_w = 200
        btn_h = 48
        btn_x1 = cx - btn_w // 2
        btn_x2 = cx + btn_w // 2
        btn_y1 = cy + 20
        btn_y2 = btn_y1 + btn_h
        r = btn_h // 2

        canvas.create_oval(
            btn_x1, btn_y1, btn_x1 + 2 * r, btn_y2,
            fill=TEXT, outline="",
            tags=("viz_ui", "viz_btn", "viz_btn_bg"),
        )
        canvas.create_oval(
            btn_x2 - 2 * r, btn_y1, btn_x2, btn_y2,
            fill=TEXT, outline="",
            tags=("viz_ui", "viz_btn", "viz_btn_bg"),
        )
        canvas.create_rectangle(
            btn_x1 + r, btn_y1, btn_x2 - r, btn_y2,
            fill=TEXT, outline="",
            tags=("viz_ui", "viz_btn", "viz_btn_bg"),
        )
        canvas.create_text(
            cx, (btn_y1 + btn_y2) // 2,
            text="Open Visualizer",
            fill=BG, font=self.app.f_btn,
            tags=("viz_ui", "viz_btn"),
        )

        # All-in-one click target via the shared viz_btn tag.
        canvas.tag_bind("viz_btn", "<Button-1>", self._on_viz_btn_click)
        canvas.tag_bind("viz_btn", "<Enter>",    self._on_viz_btn_enter)
        canvas.tag_bind("viz_btn", "<Leave>",    self._on_viz_btn_leave)

    def _on_viz_btn_click(self, _e=None):
        """Launch the external visualizer playing the current audio file.
        Silences Drop's VLC so we don't have double-audio. Passes Drop's
        current playback position so the visualizer picks up where Drop
        left off (and vice versa: on viz close, we read its final position
        and seek Drop to match)."""
        # If the visualizer is already running, this click closes it.
        # The polling task started below will detect the exit and run
        # the close-side logic (read status, seek VLC, restore audio).
        if self.viz_launcher.is_running():
            self.viz_launcher.stop()
            return

        # ── Silence VLC ────────────────────────────────────────────────
        # Triple-silence: pause + mute + volume=0. Belt-and-suspenders
        # because VLC's pause state can be lagged by buffered audio
        # frames already in flight. Mute is instant in WASAPI; volume=0
        # is redundant insurance for any backend that ignores mute.
        try:
            if self._player:
                try:
                    self._viz_saved_vol = int(self._player.audio_get_volume())
                except Exception:
                    self._viz_saved_vol = 100
                self._viz_was_muted = bool(self._player.audio_get_mute())
                self._player.set_pause(1)
                self._player.audio_set_mute(True)
                self._player.audio_set_volume(0)
        except Exception as e:
            print(f"[viz] vlc silence failed: {e}", file=sys.stderr)

        # ── Capture current Drop position ─────────────────────────────
        # VLC's get_time() returns milliseconds (or -1 if no media).
        # Pass as seconds to the visualizer so it starts at the same spot.
        start_seconds = 0.0
        try:
            if self._player:
                t = self._player.get_time()
                if t is not None and t >= 0:
                    start_seconds = t / 1000.0
        except Exception:
            pass

        # ── Status file for return-trip position sync ─────────────────
        # The visualizer writes its current playback position to this
        # file every ~250ms. When the viz exits, we read the last line
        # and seek Drop's VLC to match — so closing the viz "transfers
        # position back" to Drop.
        import tempfile
        status_path = Path(tempfile.gettempdir()) / f"drop_viz_status_{os.getpid()}.txt"
        try:
            status_path.unlink()
        except Exception:
            pass
        self._viz_status_file = status_path

        file_path = getattr(self, "_viz_audio_path", None)

        # Tell the visualizer where Drop's ffmpeg.exe lives so it can
        # decode whatever format the audio file is in.
        ffmpeg_path = None
        try:
            ffmpeg_path = get_bin("ffmpeg")
        except Exception:
            pass

        started = self.viz_launcher.start(
            file_path     = str(file_path)   if file_path   else None,
            ffmpeg_path   = str(ffmpeg_path) if ffmpeg_path else None,
            start_seconds = start_seconds,
            status_file   = str(status_path),
        )

        if not started:
            # Launch failed — undo the silence so user isn't left hanging.
            self._restore_vlc_audio()
            # In --windowed builds stderr is invisible, so surface the
            # launcher's reason via the themed modal. last_error is set by
            # VisualizerLauncher whenever start() returns False; fall back
            # to a generic message just in case.
            err = getattr(self.viz_launcher, "last_error", None) \
                  or "Couldn't open the visualizer."
            try:
                _alert_modal(self.frame.winfo_toplevel(),
                             "Visualizer", err, error=True)
            except Exception:
                pass
            return

        # Start polling for the visualizer subprocess to exit. When it
        # does, we read its status file + seek VLC + restore audio.
        self._viz_exit_poll_start()

    def _viz_exit_poll_start(self) -> None:
        """Begin a 500ms-interval polling task that watches for the
        visualizer subprocess to exit."""
        self._viz_exit_poll_cancel()
        try:
            self._viz_exit_poll_id = self.frame.after(500, self._poll_viz_exit)
        except Exception:
            pass

    def _viz_exit_poll_cancel(self) -> None:
        """Cancel any scheduled exit-poll. Safe to call repeatedly."""
        if getattr(self, "_viz_exit_poll_id", None):
            try:
                self.frame.after_cancel(self._viz_exit_poll_id)
            except Exception:
                pass
            self._viz_exit_poll_id = None

    def _poll_viz_exit(self) -> None:
        """Check if the visualizer subprocess has exited; if so, read
        its last-position file and seek Drop's VLC. Otherwise reschedule.

        This runs on Tk's mainloop via after(), so we never block — each
        invocation is cheap (a poll() syscall + maybe a small file read)."""
        self._viz_exit_poll_id = None
        if self.viz_launcher.is_running():
            # Still running — keep polling.
            try:
                self._viz_exit_poll_id = self.frame.after(500, self._poll_viz_exit)
            except Exception:
                pass
            return

        # Visualizer exited. Read its last reported position from the
        # status file and seek Drop to match. Then restore audio settings.
        status_file = getattr(self, "_viz_status_file", None)
        if status_file and status_file.exists():
            try:
                txt = status_file.read_text(encoding="utf-8").strip()
                if txt:
                    pos_sec = float(txt.splitlines()[-1])
                    pos_ms = max(0, int(pos_sec * 1000))
                    if self._player:
                        # set_time only works on seekable media — for an
                        # mp3/m4a file it always does. -1 from VLC means
                        # not seekable; we'd just be a no-op there.
                        try:
                            self._player.set_time(pos_ms)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[viz] failed to read status file: {e}", file=sys.stderr)
            try: status_file.unlink()
            except Exception: pass
        self._viz_status_file = None

        # Restore VLC audio settings (mute/volume). Pause is NOT restored
        # on purpose — leaving Drop paused after the viz closes is the
        # expected UX; user clicks Drop's play to resume.
        self._restore_vlc_audio()

    def _restore_vlc_audio(self):
        """Undo the mute/volume changes from _on_viz_btn_click. Doesn't
        unpause — leaving Drop paused after the viz closes is the
        expected UX (user resumes manually via Drop's play button)."""
        try:
            if self._player:
                if getattr(self, "_viz_saved_vol", None) is not None:
                    self._player.audio_set_volume(self._viz_saved_vol)
                self._player.audio_set_mute(getattr(self, "_viz_was_muted", False))
        except Exception:
            pass

    def _on_viz_btn_enter(self, _e=None):
        # Subtle off-white on hover. SOFT is the closest theme constant
        # to "slightly grayed white" without hardcoding a hex.
        self.viz_canvas.itemconfig("viz_btn_bg", fill=SOFT)
        self.viz_canvas.config(cursor="hand2")

    def _on_viz_btn_leave(self, _e=None):
        self.viz_canvas.itemconfig("viz_btn_bg", fill=TEXT)
        self.viz_canvas.config(cursor="")

    def _animate_viz_bars(self):
        """Bounce the four bars of the launcher icon like Spotify's
        "now playing" indicator. Each bar oscillates between its min/max
        height with its own phase offset, so they're never in lockstep.

        Re-reads the canvas size every tick so resizes don't desync the
        animation from the redrawn launcher. The bar tags survive
        canvas.delete("viz_ui") + recreate because _draw_viz_launcher
        re-adds the same tags on each redraw."""
        if not getattr(self, "_viz_anim_active", False):
            return
        canvas = self.viz_canvas
        try:
            cw = canvas.winfo_width()
            ch = canvas.winfo_height()
        except Exception:
            return
        if cw < 50 or ch < 50:
            # Canvas not laid out yet — try again in a moment.
            try:
                self._viz_anim_after = canvas.after(50, self._animate_viz_bars)
            except Exception:
                pass
            return

        cx = cw // 2
        cy = ch // 2
        icon_y = cy - 95
        bar_w = 6

        # 5.0 rad/s × time → ~0.8Hz overall envelope, fast enough to feel
        # alive but not seizure-inducing. Phase offsets in self._viz_bar_cfg
        # spread the peaks across the cycle.
        t = time.time() * 5.0
        for i, (ox, hmin, hmax, phase) in enumerate(self._viz_bar_cfg):
            # sin → [-1,1]; rescale to [0,1]; lerp between hmin and hmax.
            level = 0.5 + 0.5 * math.sin(t + phase)
            h = hmin + (hmax - hmin) * level
            x = cx + ox - bar_w // 2
            try:
                canvas.coords(
                    f"viz_bar_{i}",
                    x, icon_y - h / 2,
                    x + bar_w, icon_y + h / 2,
                )
            except Exception:
                pass

        # 33ms = 30fps. Smooth enough; matches the visualizer's framerate
        # philosophy and doesn't peg a CPU core.
        try:
            self._viz_anim_after = canvas.after(33, self._animate_viz_bars)
        except Exception:
            pass

    def _stop_audio_viz(self):
        """Tear down the launcher UI and close the external visualizer.
        Restores the VLC surface to its on-screen position for video."""
        # Cancel the exit-poll task so it doesn't run after we tear down.
        self._viz_exit_poll_cancel()

        try:
            self.viz_launcher.stop()
        except Exception:
            pass

        # Clean up any leftover status file.
        status_file = getattr(self, "_viz_status_file", None)
        if status_file:
            try: status_file.unlink()
            except Exception: pass
            self._viz_status_file = None

        # Restore VLC audio state if we silenced it for a visualizer
        # session. Safe to call unconditionally — _restore_vlc_audio
        # is a no-op if there's nothing to restore.
        self._restore_vlc_audio()

        # Halt the bar animation loop. The next scheduled tick (if any)
        # will see _viz_anim_active=False and return immediately.
        self._viz_anim_active = False
        if getattr(self, "_viz_anim_after", None):
            try:
                self.frame.after_cancel(self._viz_anim_after)
            except Exception:
                pass
            self._viz_anim_after = None

        # Remove the <Configure> binding so callbacks don't fire later.
        if getattr(self, "_viz_btn_bind", None):
            try:
                self.viz_canvas.unbind("<Configure>", self._viz_btn_bind)
            except Exception:
                pass
            self._viz_btn_bind = None

        # Clear the canvas + reset state.
        try:
            self.viz_canvas.delete("viz_ui")
        except Exception:
            pass
        self._viz_audio_path = None
        self._viz_btn      = None
        self._viz_btn_id   = None

        # Hide the canvas and bring the surface back on-screen.
        try: self.viz_canvas.place_forget()
        except Exception: pass
        try: self._do_center_surface()
        except Exception: pass


    # ── lifecycle ────────────────────────────────────────────────────────────
    def stop(self):
        # Capture position before tearing the player state down.
        self._save_resume_position()
        # Stop the visualizer render loop — it'd otherwise keep redrawing
        # against a stopped player and wasting cycles.
        self._stop_audio_viz()
        # Tear down any in-flight rotation overlay so its deferred
        # first-frame poll doesn't outlive the player it was watching.
        rot = getattr(self, "_rot_overlay", None)
        if rot is not None:
            self._destroy_rotation_overlay(rot)
            self._rot_overlay = None
        if self._player:
            try: self._player.stop()
            except Exception: pass

    def shutdown(self):
        self.stop()
        try:
            if self._player: self._player.release()
            if self._vlc_inst: self._vlc_inst.release()
        except Exception:
            pass

    def _center_surface(self, event=None):
        """Resize the video surface to a rectangle preserving the current
        video's aspect ratio. Debounced so window-drag doesn't thrash VLC."""
        # Apply the layout cheaply if it's just a content swap, otherwise
        # debounce to coalesce drag events.
        if event is None:
            self._do_center_surface()
            return
        if getattr(self, "_center_after", None):
            try: self.frame.after_cancel(self._center_after)
            except Exception: pass
        self._center_after = self.frame.after(80, self._do_center_surface)

    def _do_center_surface(self):
        self._center_after = None
        holder = self.surface_holder
        W = holder.winfo_width()
        H = holder.winfo_height()
        if W < 2 or H < 2:
            return
        aspect = self._aspect if self._aspect and self._aspect > 0 else 1.0
        # When rotated 90° or 270°, the displayed frame is the source rotated
        # on its side — effective on-screen aspect is the reciprocal. Without
        # this swap, a vertical phone clip rotated to horizontal would be
        # squished into a portrait-sized surface and lose half its content.
        if getattr(self, "_rotation", 0) in (90, 270):
            aspect = 1.0 / aspect
        # Fit a rectangle of `aspect` into (W, H), centered. If the holder
        # is wider than the video aspect, height is the limiting dimension;
        # otherwise width is. Same logic as CSS `object-fit: contain`.
        if W / H > aspect:
            new_h = H
            new_w = int(round(H * aspect))
        else:
            new_w = W
            new_h = int(round(W / aspect))
        new_w = max(40, new_w)
        new_h = max(40, new_h)
        x = (W - new_w) // 2
        y = max(0, (H - new_h) // 2)
        self.surface.place(x=x, y=y, width=new_w, height=new_h)

    def _show_empty(self):
        try: self.surface.place_forget()
        except Exception: pass
        self.empty_lbl.configure(
            text="No items in this library yet.\nDownload something, then tap +."
        )
        self.empty_lbl.pack(in_=self.surface_holder, fill="both", expand=True)

    def _hide_empty(self):
        try: self.empty_lbl.pack_forget()
        except Exception: pass
        # Re-show surface via _center_surface; layout settles asynchronously
        self.surface_holder.update_idletasks()
        self._center_surface()
