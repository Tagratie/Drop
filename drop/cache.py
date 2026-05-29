"""Thumbnail and preview-frame caches. Both run ffmpeg in worker threads.

Speed pass — what changed vs. the previous version, and why:

  • Keyframe seek (-ss BEFORE -i): jumps straight to the nearest keyframe
    instead of decoding from the start of the file. For large videos this
    is the single biggest win — easily 5–10× on long files.
  • -an -sn: skip audio + subtitle demuxing. We're pulling one video frame;
    the demuxer wasting time on parallel streams was pure overhead.
  • -threads 1: single ffmpeg thread. For a single-frame extract, the
    multi-thread setup cost is bigger than what threading saves. Also lets
    us safely run more concurrent ffmpegs (they each pin one core, not all).
  • -probesize / -analyzeduration cut to small values: ffmpeg's default is
    to read up to 5MB / 5s of stream to detect format details. We don't
    need that — we just want frame 1.
  • -vf scale=...:flags=fast_bilinear: bilinear instead of bicubic. Visual
    diff is invisible at thumbnail size; runtime diff isn't.
  • One pass instead of two: the old "if first try fails, retry from 0.0"
    fallback doubled wall time on every miss. We now do a single attempt
    using a small -ss (0.5s, picks first keyframe on most files) and accept
    rare misses — they fall back to no-thumb gracefully.
  • MAX_WORKERS scales with CPU count: with -threads 1, we can run more
    in parallel. Previously hard-capped at 4 — now uses available cores.
"""
import os
import re
import hashlib
import threading
import subprocess
from pathlib import Path

from .theme import THUMB_DIR, THUMB_W, NO_WINDOW
from .utils import get_ffmpeg


def _default_workers():
    """Pick a sensible parallel-ffmpeg count from the host's CPU count.
    With -threads 1 each subprocess pins ~one core, so we want roughly
    cpu_count workers, capped so we don't spawn 32 ffmpegs on a workstation."""
    try:
        n = os.cpu_count() or 4
    except Exception:
        n = 4
    return max(2, min(n, 8))


class ThumbnailCache:
    """Generate + cache video thumbnails on disk. A semaphore caps how many
    ffmpeg subprocesses run at once so prefetching a 50-item library doesn't
    fork 50 ffmpegs and stall the machine. Workers themselves are daemon
    threads so they don't keep the process alive at shutdown."""

    MAX_WORKERS = _default_workers()

    def __init__(self):
        self.dir = THUMB_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock      = threading.Lock()
        self._inflight  = set()       # paths currently being generated
        self._listeners = {}          # path -> list[callable(thumb_path or None)]
        self._ffmpeg    = get_ffmpeg()
        # Threads beyond MAX_WORKERS just block on the semaphore — cheap,
        # since they're idle and consume only ~KB of RAM each.
        self._sem       = threading.Semaphore(self.MAX_WORKERS)

    def _key_for(self, path):
        # Mix in mtime so re-encoded files invalidate the cache automatically.
        # Bumped version tag to v3 so the speed-pass invalidates v2 thumbs;
        # they were generated with slower settings + bicubic scaling.
        try:
            mt = int(os.path.getmtime(path))
        except Exception:
            mt = 0
        h = hashlib.sha1(f"{path}|{mt}|v3-w{THUMB_W}".encode("utf-8")).hexdigest()[:20]
        return self.dir / f"{h}.jpg"

    def get_cached(self, path):
        """Return the cached thumb path if it exists, else None."""
        if not path: return None
        thumb = self._key_for(path)
        return str(thumb) if thumb.exists() else None

    def request(self, path, callback):
        """Get-or-generate. callback(thumb_path | None) fires on the worker
        thread. Caller is responsible for after()-ing back into Tk if needed."""
        if not path:
            callback(None); return
        cached = self.get_cached(path)
        if cached:
            callback(cached); return
        if not self._ffmpeg:
            callback(None); return

        with self._lock:
            self._listeners.setdefault(path, []).append(callback)
            if path in self._inflight:
                return
            self._inflight.add(path)

        threading.Thread(target=self._gated_worker, args=(path,),
                          daemon=True).start()

    def prefetch_all(self, paths):
        """Pre-warm thumbnails for many paths in the background. No-op for
        paths already cached or already in flight. Lets us get ahead of the
        user scrolling rather than generating thumbs on demand. Listeners
        added later via request() will still receive the eventual result."""
        if not self._ffmpeg:
            return
        for path in paths:
            if not path:
                continue
            try:
                if not os.path.isfile(path):
                    continue
            except Exception:
                continue
            if self.get_cached(path):
                continue
            with self._lock:
                if path in self._inflight:
                    continue
                # Empty listener list — request() will append to it later if
                # the caller still wants the result delivered.
                self._listeners.setdefault(path, [])
                self._inflight.add(path)
            threading.Thread(target=self._gated_worker, args=(path,),
                              daemon=True).start()

    def _gated_worker(self, path):
        """Wait for a free slot, then run the actual ffmpeg call."""
        with self._sem:
            self._worker(path)

    def _worker(self, path):
        thumb = self._key_for(path)
        ok = False
        try:
            if os.path.exists(path):
                # Single fast extraction. See module docstring for what each
                # flag is doing.
                cmd = [
                    self._ffmpeg, "-y", "-loglevel", "error",
                    "-probesize", "1M",
                    "-analyzeduration", "0",
                    "-an", "-sn",
                    "-ss", "0.5",          # keyframe seek (-ss before -i)
                    "-i", path,
                    "-frames:v", "1",
                    "-vf", f"scale={THUMB_W}:-2:flags=fast_bilinear",
                    "-q:v", "3",           # was 2 — visually indistinguishable, smaller files
                    "-threads", "1",
                    str(thumb),
                ]
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                    timeout=10,
                )
                ok = (proc.returncode == 0
                      and thumb.exists()
                      and thumb.stat().st_size > 0)
        except Exception:
            ok = False

        result = str(thumb) if ok else None
        with self._lock:
            cbs = self._listeners.pop(path, [])
            self._inflight.discard(path)
        for cb in cbs:
            try: cb(result)
            except Exception: pass




class PreviewCache:
    """Generate + cache short-preview frames for hover playback.
    Samples N frames evenly across the video, writes to a per-video subfolder."""

    FRAMES = 6
    WIDTH  = 480
    MAX_WORKERS = _default_workers()

    def __init__(self):
        self.dir = THUMB_DIR.parent / "previews"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock      = threading.Lock()
        self._inflight  = set()
        self._listeners = {}
        self._ffmpeg    = get_ffmpeg()
        self._sem       = threading.Semaphore(self.MAX_WORKERS)

    def _key_for(self, path):
        try: mt = int(os.path.getmtime(path))
        except Exception: mt = 0
        h = hashlib.sha1(f"{path}|{mt}|{self.FRAMES}|v2".encode("utf-8")).hexdigest()[:20]
        return self.dir / h

    def get_cached(self, path):
        if not path: return None
        d = self._key_for(path)
        frames = sorted(d.glob("f*.jpg")) if d.exists() else []
        return [str(f) for f in frames] if len(frames) == self.FRAMES else None

    def request(self, path, callback):
        if not path:
            callback(None); return
        cached = self.get_cached(path)
        if cached:
            callback(cached); return
        if not self._ffmpeg:
            callback(None); return
        with self._lock:
            self._listeners.setdefault(path, []).append(callback)
            if path in self._inflight:
                return
            self._inflight.add(path)
        threading.Thread(target=self._gated_worker, args=(path,),
                          daemon=True).start()

    def _gated_worker(self, path):
        with self._sem:
            self._worker(path)

    def _worker(self, path):
        d = self._key_for(path)
        d.mkdir(parents=True, exist_ok=True)
        ok = False
        try:
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            # Probe duration via ffmpeg (cheap-ish, parses stderr)
            duration = self._probe_duration(path)
            if duration is None or duration < 1.5:
                # Too short for a preview montage.
                raise RuntimeError("too short")
            # Sample N timestamps evenly in [10%, 90%] of duration
            lo = duration * 0.10
            hi = duration * 0.90
            stamps = [lo + (hi - lo) * i / max(1, self.FRAMES - 1)
                      for i in range(self.FRAMES)]
            for i, t in enumerate(stamps):
                out = d / f"f{i:02d}.jpg"
                # Same speed flags as ThumbnailCache: keyframe seek, no audio,
                # single-thread, fast scaler.
                cmd = [
                    self._ffmpeg, "-y", "-loglevel", "error",
                    "-probesize", "1M",
                    "-analyzeduration", "0",
                    "-an", "-sn",
                    "-ss", f"{t:.2f}",
                    "-i", path,
                    "-frames:v", "1",
                    "-vf", f"scale={self.WIDTH}:-2:flags=fast_bilinear",
                    "-q:v", "4",
                    "-threads", "1",
                    str(out),
                ]
                proc = subprocess.run(cmd,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,
                                      creationflags=NO_WINDOW, timeout=10)
                if proc.returncode != 0 or not out.exists():
                    raise RuntimeError(f"frame {i} failed")
            ok = True
        except Exception:
            ok = False

        if ok:
            result = sorted(str(f) for f in d.glob("f*.jpg"))
        else:
            result = None
            # Clean up partial output
            try:
                for f in d.glob("f*.jpg"): f.unlink()
            except Exception: pass
        with self._lock:
            cbs = self._listeners.pop(path, [])
            self._inflight.discard(path)
        for cb in cbs:
            try: cb(result)
            except Exception: pass

    def _probe_duration(self, path):
        """Parse 'Duration: HH:MM:SS.xx' from ffmpeg stderr. Returns seconds."""
        try:
            cmd = [self._ffmpeg, "-i", path]
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE,
                                  creationflags=NO_WINDOW, timeout=8)
            err = proc.stderr.decode("utf-8", errors="replace")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
            if m:
                h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                return h * 3600 + mn * 60 + s
        except Exception:
            pass
        return None
