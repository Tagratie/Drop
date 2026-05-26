"""Launches the standalone GLFW audio visualizer as a separate process.

The visualizer captures system audio via loopback (soundcard library on
Windows / PulseAudio monitor on Linux), so it picks up whatever Drop is
playing through VLC automatically — no inter-process audio routing or
shared memory needed.

Two execution modes:
  * Dev (running Drop from source): launches `python visualizer/main.py`
    using the sibling visualizer/ folder.
  * Frozen (Drop packaged as Drop.exe): looks for visualizer.exe next to
    Drop.exe. You build that one-file EXE separately:

        cd visualizer
        pyinstaller --onefile --windowed --name visualizer ^
            --add-data "shaders;shaders" main.py

    Then copy dist/visualizer.exe next to dist/Drop.exe. The launcher
    finds it automatically.

Why a separate process and not a thread:
  GLFW window creation is main-thread-only on most platforms. Drop's
  main thread is busy with Tk's mainloop. Subprocess gets the visualizer
  its own main thread and gives us crash isolation — a visualizer
  segfault doesn't take Drop down with it.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional


def _is_dev() -> bool:
    return not getattr(sys, "frozen", False)


def _find_dev_main() -> Optional[Path]:
    """Locate visualizer/main.py when running from source. Walks up from
    this file looking for a sibling visualizer/ folder."""
    here = Path(__file__).resolve().parent
    for level in (here, here.parent, here.parent.parent):
        candidate = level / "visualizer" / "main.py"
        if candidate.is_file():
            return candidate
    return None


def _find_frozen_exe() -> Optional[Path]:
    """Look for visualizer.exe alongside Drop.exe in a packaged build."""
    if _is_dev():
        return None
    exe_dir = Path(sys.executable).resolve().parent
    cand = exe_dir / ("visualizer.exe" if sys.platform == "win32" else "visualizer")
    return cand if cand.is_file() else None


class VisualizerLauncher:
    """Manages a single visualizer subprocess.

    Public API: start(), stop(), toggle(), is_running(). Thread-safe via
    an internal lock — Drop can call toggle() from any Tk callback and
    stop() from window-close without races.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        # Last user-facing failure reason from start(). Cleared on every
        # start() attempt; consumers (the UI) read this when start() returns
        # False to decide what to show.
        self.last_error: Optional[str] = None

    # ── public API ─────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """True if the visualizer subprocess is still alive. Cleans up the
        stale handle if the user closed the visualizer window themselves."""
        with self._lock:
            if self._proc is None:
                return False
            if self._proc.poll() is not None:
                self._proc = None
                return False
            return True

    def start(
        self,
        loopback:      bool             = True,
        file_path:     Optional[str]    = None,
        ffmpeg_path:   Optional[str]    = None,
        start_seconds: Optional[float]  = None,
        status_file:   Optional[str]    = None,
        rotation:      Optional[float]  = None,
        fullscreen:    bool             = False,
    ) -> bool:
        """Launch the visualizer if not already running.

        Returns True if a new process was started, False if one was already
        running or the launch failed.

        Args:
            loopback:      capture system audio loopback (default if no
                           file_path is given)
            file_path:     if set, the visualizer plays this audio file
                           directly using ffmpeg + sounddevice, ignoring
                           loopback. This is the "Drop-only audio" path —
                           viz reacts to exactly this file, not other apps.
            ffmpeg_path:   path to ffmpeg.exe to use for file decoding.
                           Drop bundles one; this lets us pass it so the
                           visualizer doesn't need ffmpeg on PATH.
            start_seconds: offset into the file to start playback at.
                           Lets us hand off Drop's current play position.
            status_file:   path the visualizer should write its current
                           playback position to every ~250ms (single-line
                           float, seconds). Drop reads this on viz exit
                           to seek itself back to where the viz left off.
            rotation:      initial waveform rotation in degrees
            fullscreen:    start fullscreen on primary monitor
        """
        with self._lock:
            # Recheck under lock to make start() race-free.
            if self._proc is not None and self._proc.poll() is None:
                return False
            self._proc = None
            self.last_error = None

            cmd = self._build_command(
                loopback      = loopback,
                file_path     = file_path,
                ffmpeg_path   = ffmpeg_path,
                start_seconds = start_seconds,
                status_file   = status_file,
                rotation      = rotation,
                fullscreen    = fullscreen,
            )
            if cmd is None:
                # _build_command already set last_error with a user-readable
                # reason; just return False so the caller can surface it.
                return False

            try:
                kwargs = {}
                if sys.platform == "win32":
                    # Suppress the cmd window unconditionally — even in dev,
                    # the visualizer's [audio]/[fps] prints aren't worth the
                    # popup. If you actually want them, run `python
                    # visualizer/main.py` directly outside of Drop.
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                if not _is_dev():
                    # Strip Drop.exe's _MEIPASS coordination vars before
                    # launching another PyInstaller binary — see
                    # drop.utils.clean_subprocess_env for the why.
                    try:
                        from .utils import clean_subprocess_env
                        kwargs["env"] = clean_subprocess_env()
                    except Exception:
                        pass
                self._proc = subprocess.Popen(cmd, **kwargs)
                return True
            except Exception as e:
                print(f"[viz] launch failed: {e}", file=sys.stderr)
                self.last_error = f"Couldn't start the visualizer:\n{e}"
                self._proc = None
                return False

    def stop(self, timeout: float = 2.0) -> None:
        """Terminate the visualizer if running. Idempotent — safe to call
        on shutdown even if no visualizer was ever started."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    def toggle(self, **kwargs) -> bool:
        """Stop if running, otherwise start. Returns the new state.
        Wire this directly to a button command."""
        if self.is_running():
            self.stop()
            return False
        return self.start(**kwargs)

    # ── internals ──────────────────────────────────────────────────────────

    def _build_command(
        self,
        loopback:      bool,
        file_path:     Optional[str],
        ffmpeg_path:   Optional[str],
        start_seconds: Optional[float],
        status_file:   Optional[str],
        rotation:      Optional[float],
        fullscreen:    bool,
    ) -> Optional[List[str]]:
        """Resolve the visualizer entrypoint and assemble the command line.

        Tries frozen path first (visualizer.exe next to Drop.exe), then
        falls back to dev (python main.py in sibling visualizer/)."""
        # Frozen: bundled EXE.
        exe = _find_frozen_exe()
        if exe is not None:
            cmd: List[str] = [str(exe)]
        elif not _is_dev():
            # Frozen but visualizer.exe is missing. Do NOT fall through to the
            # dev branch — sys.executable here is Drop.exe, so [sys.executable,
            # "-u", main.py] would just spawn another Drop window. Tell the
            # user clearly and bail.
            msg = ("visualizer.exe is missing.\n\n"
                   "Drop expects it to sit in the same folder as Drop.exe. "
                   "Build it with:\n  pyinstaller visualizer.spec\n"
                   "and copy dist/visualizer.exe next to Drop.exe.")
            print("[viz] " + msg.replace("\n", "\n      "), file=sys.stderr)
            self.last_error = msg
            return None
        else:
            # Dev: python -u for unbuffered output (so [audio]/[fps] prints
            # appear in real time, not in 4KB chunks).
            dev_main = _find_dev_main()
            if dev_main is None:
                msg = ("Couldn't find visualizer/main.py.\n\n"
                       "Expected a sibling visualizer/ folder next to drop.py.")
                print("[viz] " + msg.replace("\n", "\n      "), file=sys.stderr)
                self.last_error = msg
                return None
            cmd = [sys.executable, "-u", str(dev_main)]

        # File-source mode wins over loopback if both are supplied —
        # because "play this file" is more specific than "capture whatever".
        if file_path:
            cmd.extend(["--file", file_path])
            if ffmpeg_path:
                cmd.extend(["--ffmpeg-path", ffmpeg_path])
            if start_seconds is not None and start_seconds > 0:
                cmd.extend(["--start", f"{start_seconds:.3f}"])
            if status_file:
                cmd.extend(["--status-file", status_file])
        elif loopback:
            cmd.append("--loopback")

        if rotation is not None:
            cmd.extend(["--rotation", str(rotation)])
        if fullscreen:
            cmd.append("--fullscreen")
        return cmd
