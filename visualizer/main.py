"""Entry point. Opens a GLFW window with an OpenGL 3.3 core context, wires
up audio + visualizer + renderer, and runs the main loop.

CLI:
    python main.py                              # default mic
    python main.py --loopback                   # capture system audio (cross-platform)
    python main.py --list-devices               # see all available capture sources
    python main.py --device 5                   # pick by index
    python main.py --device-name "Speakers"     # pick by name substring
    python main.py --width 1920 --height 1080
    python main.py --fullscreen
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

# When packaged with PyInstaller --windowed, the bootloader sets sys.stdout
# and sys.stderr to None. Any later `sys.stdout.write(...)` or `print(...)`
# then raises AttributeError and pops up the fatal-error dialog. Redirect
# to a sink so the existing [fps] / [audio] prints become harmless no-ops.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import glfw
import moderngl

from audio      import AudioAnalyzer, FileAudioAnalyzer
from renderer   import Renderer
from visualizer import Visualizer


def list_devices() -> None:
    """Print every capture-capable device, including loopback ones.
    The soundcard library auto-creates a loopback "microphone" for each
    speaker on Windows + Linux; macOS users need BlackHole or similar."""
    try:
        import soundcard as sc
    except ImportError:
        print("soundcard not installed. Run:  pip install soundcard", file=sys.stderr)
        return

    mics = sc.all_microphones(include_loopback=True)
    print("Capture devices (microphones + loopback):\n")
    for i, m in enumerate(mics):
        loop_tag = "  [LOOPBACK]" if getattr(m, "isloopback", False) else ""
        print(f"  {i:2d}: {m.name}{loop_tag}")

    try:
        default_mic = sc.default_microphone()
        default_spk = sc.default_speaker()
        print(f"\nDefault microphone: {default_mic.name}")
        print(f"Default speaker:    {default_spk.name}")
        print(f"\n--loopback will capture from the default speaker.")
    except Exception:
        pass


class Settings:
    """Runtime-toggleable effect settings. Each attribute is read by
    visualizer.py / renderer.py / the shaders; flipping them takes effect
    on the next frame. Defaults to everything on.

    Add new toggles here, wire them into the consumers, then add a key
    binding in TOGGLE_KEYS below — three-step process, fully local."""

    def __init__(self) -> None:
        self.bloom       = True   # bloom / glow post-pass
        self.shake       = True   # bass-pulse camera shake
        self.particles   = True   # bass-hit point-sprite bursts
        self.chromatic   = True   # subtle radial chromatic aberration
        self.bg_drift    = True   # slow inward radial motion of bg stars
        self.bg_twinkle  = False  # per-star sin() brightness variation — off
                                  # by default so background stars stay solid
                                  # and clearly visible (toggle 6 to enable)
        self.bg_vignette = True   # soft radial darkening of edges
        self.show_help   = True   # in-window keyboard-shortcuts overlay —
                                  # visible by default; H toggles it off/on


# Keyboard → setting attribute mapping. The first column is the GLFW key
# constant, second is the Settings attribute name, third is the human label
# shown in help / status output.
TOGGLE_KEYS = [
    ("KEY_1", "bloom",       "Bloom (glow)"),
    ("KEY_2", "shake",       "Camera shake"),
    ("KEY_3", "particles",   "Particles"),
    ("KEY_4", "chromatic",   "Chromatic aberration"),
    ("KEY_5", "bg_drift",    "BG drift"),
    ("KEY_6", "bg_twinkle",  "BG twinkle (flicker)"),
    ("KEY_7", "bg_vignette", "BG vignette"),
]


def print_help(settings: Settings) -> None:
    """Print the controls + current toggle state to the console."""
    print()
    print("=" * 44)
    print("  Audio Visualizer — Controls")
    print("=" * 44)
    print("  ESC        Quit")
    print("  [ / ]      Rotate \u00B115\u00B0")
    print("  R          Reset rotation")
    print("  H          Toggle on-screen shortcuts overlay")
    print()
    print("  Effect toggles (current state shown):")
    for i, (_key, attr, label) in enumerate(TOGGLE_KEYS, start=1):
        state = "ON " if getattr(settings, attr) else "OFF"
        print(f"   {i}         [{state}]  {label}")
    print("=" * 44)
    print()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Real-time audio visualizer")
    p.add_argument("--width",  type=int, default=1280, help="Window width (default 1280)")
    p.add_argument("--height", type=int, default=720,  help="Window height (default 720)")
    p.add_argument("--device", type=int, default=None,
                    help="Capture device index. Use --list-devices to see options.")
    p.add_argument("--device-name", type=str, default=None,
                    help="Capture device name substring match (alternative to --device).")
    p.add_argument("--loopback", action="store_true",
                    help="Capture system audio (what you hear) via the default speaker's loopback.")
    p.add_argument("--file", type=str, default=None,
                    help="Play this audio file directly (mp3, wav, m4a, ...). "
                         "Takes precedence over --loopback. Decoded via ffmpeg, "
                         "so format support is whatever ffmpeg supports.")
    p.add_argument("--ffmpeg-path", type=str, default=None,
                    help="Path to ffmpeg.exe to use for --file decoding. "
                         "Defaults to ffmpeg on PATH.")
    p.add_argument("--start", type=float, default=0.0,
                    help="Start playback at this many seconds into the file "
                         "(--file mode only).")
    p.add_argument("--status-file", type=str, default=None,
                    help="Path to a file the visualizer will write its current "
                         "playback position (seconds, single float) to every "
                         "~250ms. Used by Drop to sync position on viz close.")
    p.add_argument("--list-devices", action="store_true",
                    help="Print capture devices and exit.")
    p.add_argument("--fullscreen", action="store_true",
                    help="Start fullscreen on the primary monitor.")
    p.add_argument("--rotation", type=float, default=90.0,
                    help="Initial waveform rotation in degrees (default 90). "
                         "Adjust at runtime with [ and ] keys, R resets.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_devices:
        list_devices()
        return 0

    # ── GLFW ───────────────────────────────────────────────────────────────
    if not glfw.init():
        print("GLFW init failed", file=sys.stderr)
        return 1

    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
    glfw.window_hint(glfw.SAMPLES, 4)

    monitor = glfw.get_primary_monitor() if args.fullscreen else None
    if args.fullscreen and monitor:
        mode = glfw.get_video_mode(monitor)
        win_w, win_h = mode.size.width, mode.size.height
    else:
        win_w, win_h = args.width, args.height

    window = glfw.create_window(win_w, win_h, "Audio Visualizer", monitor, None)
    if not window:
        glfw.terminate()
        print("Failed to create GLFW window", file=sys.stderr)
        return 1

    glfw.make_context_current(window)
    glfw.swap_interval(1)

    glfw.make_context_current(window)
    glfw.swap_interval(1)

    # ── ModernGL ───────────────────────────────────────────────────────────
    ctx = moderngl.create_context()
    fb_w, fb_h = glfw.get_framebuffer_size(window)
    renderer = Renderer(ctx, fb_w, fb_h)

    # ── Audio ──────────────────────────────────────────────────────────────
    # File mode (Drop launches us this way) plays a specific file via
    # ffmpeg + sounddevice, so the visualizer reacts to ONLY that file's
    # audio. Standalone mode (no --file) captures system audio loopback.
    if args.file:
        analyzer = FileAudioAnalyzer(
            file_path     = args.file,
            ffmpeg_path   = args.ffmpeg_path,
            start_seconds = args.start,
            status_file   = args.status_file,
        )
    else:
        analyzer = AudioAnalyzer(
            device      = args.device,
            device_name = args.device_name,
            loopback    = args.loopback,
        )
    try:
        analyzer.start()
    except Exception as e:
        print(f"[audio] failed to open input: {e}", file=sys.stderr)
        print("[audio] visualizer will run with silent input.", file=sys.stderr)

    initial_rotation = math.radians(args.rotation)
    settings = Settings()
    viz = Visualizer(
        analyzer, renderer,
        initial_rotation = initial_rotation,
        settings         = settings,
    )

    # Resolve the TOGGLE_KEYS table once into (glfw_keycode, attr, label).
    # Done at runtime because we can't import glfw constants at module
    # top-level in a way that survives lazy import patterns.
    toggle_table = [(getattr(glfw, k), a, l) for k, a, l in TOGGLE_KEYS]

    print_help(settings)

    # Key callback — bound here (after viz/settings exist) so the handler
    # can close over them. ROT_STEP for [ ] rotation, R resets to the
    # --rotation value, ESC quits, H reprints help, digit keys toggle
    # effects per TOGGLE_KEYS.
    ROT_STEP = math.radians(15.0)

    def on_key(_w, key, _scan, action, _mods):
        if action not in (glfw.PRESS, glfw.REPEAT):
            return
        if key == glfw.KEY_ESCAPE:
            glfw.set_window_should_close(_w, True)
            return
        if key == glfw.KEY_LEFT_BRACKET:
            viz.rotation -= ROT_STEP
            return
        if key == glfw.KEY_RIGHT_BRACKET:
            viz.rotation += ROT_STEP
            return
        if key == glfw.KEY_R:
            viz.rotation = initial_rotation
            return
        if key == glfw.KEY_H:
            settings.show_help = not settings.show_help
            return
        for kc, attr, label in toggle_table:
            if key == kc:
                new = not getattr(settings, attr)
                setattr(settings, attr, new)
                print(f"[toggle] {label}: {'ON' if new else 'OFF'}")
                return

    glfw.set_key_callback(window, on_key)

    # ── Main loop ──────────────────────────────────────────────────────────
    last = time.perf_counter()
    fps_acc = 0
    fps_t   = last

    while not glfw.window_should_close(window):
        now = time.perf_counter()
        dt = now - last
        last = now

        fb_w, fb_h = glfw.get_framebuffer_size(window)
        if (fb_w, fb_h) != (renderer.width, renderer.height):
            renderer.resize(fb_w, fb_h)

        aspect = fb_w / max(fb_h, 1)
        viz.update(dt)
        viz.render(aspect)

        # On-screen keyboard-shortcuts overlay. Drawn AFTER the visualizer
        # so it sits above bloom + chromatic aberration + tonemap — i.e.
        # untouched by the post stack. Toggle with H.
        if settings.show_help:
            renderer.render_help_overlay()

        glfw.swap_buffers(window)
        glfw.poll_events()

        fps_acc += 1
        if now - fps_t >= 1.0:
            sys.stdout.write(f"\r[fps] {fps_acc:>4d}    ")
            sys.stdout.flush()
            fps_acc = 0
            fps_t = now

    sys.stdout.write("\n")
    analyzer.stop()
    glfw.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
