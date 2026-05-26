"""Watches Downloads for a fresh drop.py and notifies the app."""
import os
import re
import sys
import time
import threading
from pathlib import Path


class UpdateWatcher:
    """Polls the Downloads folder for a new drop.py. When one appears, hands
    its path to the app via on_update(path). The app decides what to do."""

    POLL_INTERVAL = 2.0
    SETTLE_SECONDS = 1.5    # ignore files whose mtime changed within this window
    MIN_BYTES = 5 * 1024
    REQUIRED_TOKEN = "class App"  # must appear in the file to be a real Drop script

    NAME_RE = re.compile(r"^drop(?:\s*\(\d+\))?\.py$", re.IGNORECASE)

    def __init__(self, watch_dir, on_update):
        self.watch_dir = Path(watch_dir)
        self.on_update = on_update
        self._stop = False
        # Remember files we've already offered so we don't re-prompt every poll.
        self._seen = set()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop = True

    def _loop(self):
        # Skip running entirely when bundled as an EXE — there's no .py to replace.
        if getattr(sys, "frozen", False):
            return
        while not self._stop:
            try:
                self._scan()
            except Exception:
                pass
            for _ in range(int(self.POLL_INTERVAL * 10)):
                if self._stop: return
                time.sleep(0.1)

    def _scan(self):
        if not self.watch_dir.exists():
            return
        candidates = []
        try:
            for p in self.watch_dir.iterdir():
                if not p.is_file(): continue
                if not self.NAME_RE.match(p.name): continue
                candidates.append(p)
        except Exception:
            return
        if not candidates:
            return
        # Newest first
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in candidates:
            key = (str(p), int(p.stat().st_mtime))
            if key in self._seen:
                continue
            if not self._looks_legit(p):
                # Mark seen anyway so we don't keep checking the same junk file
                self._seen.add(key)
                continue
            self._seen.add(key)
            try:
                self.on_update(str(p))
            except Exception:
                pass
            return  # one update at a time

    def _looks_legit(self, p):
        try:
            st = p.stat()
            if st.st_size < self.MIN_BYTES:
                return False
            # Still being written?
            if time.time() - st.st_mtime < self.SETTLE_SECONDS:
                return False
            # Quick content sanity — read first 8KB.
            with open(p, "rb") as f:
                head = f.read(8192).decode("utf-8", errors="replace")
            return self.REQUIRED_TOKEN in head
        except Exception:
            return False


