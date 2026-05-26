"""Live audio capture + FFT analysis + smoothing.

Two capture sources, picked by the constructor:

  AudioAnalyzer       — captures from a device (mic / loopback / specific
                        microphone) using the `soundcard` library. Used
                        for standalone visualizer mode where you want it
                        to react to whatever's playing on the system.

  FileAudioAnalyzer   — plays an audio file directly via ffmpeg pipe +
                        sounddevice OutputStream, runs the same FFT
                        pipeline on the PCM stream as it plays. Used when
                        Drop launches the visualizer with `--file PATH` —
                        the visualizer becomes the audio source instead
                        of capturing system loopback, so it reacts to
                        ONLY that file and not other apps.

Both classes expose the same public surface:
    .start(), .stop(), .update(), .smoothed, .bass, .bass_smoothed,
    .bass_pulse, .n_bands, .samplerate

so visualizer.py / renderer.py don't care which kind they get.

Pipeline per update():
    1. Pull the latest FFT_SIZE samples from the rolling capture buffer
    2. Window with Hann, run rfft, take magnitude
    3. Bin into log-spaced frequency bands (~30Hz–16kHz)
    4. Gaussian-smooth across bands (kills jagged single-bin noise)
    5. Exponential-smooth over time (smooth = smooth*0.85 + new*0.15)
    6. Compute bass energy + bass-transient pulse for camera FX
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

import numpy as np
from scipy.ndimage import gaussian_filter1d


# `soundcard` does all the platform-specific stuff for us. If it's missing
# we fail loudly at start() with a clear install instruction — better than
# silently using a different backend that doesn't support loopback.
try:
    import soundcard as sc
    _SC_OK = True
except Exception:
    sc = None
    _SC_OK = False

# `sounddevice` only used by FileAudioAnalyzer for output playback.
try:
    import sounddevice as sd
    _SD_OK = True
except Exception:
    sd = None
    _SD_OK = False


# Audio params. 48kHz is the modern default; FFT_SIZE 2048 gives ~23Hz
# resolution at the low end and ~21ms of latency.
SAMPLE_RATE = 48000
BLOCK_SIZE  = 512
FFT_SIZE    = 2048
N_BANDS     = 96


class AudioAnalyzer:
    """Thread-safe live audio analyzer.

    Construct, call .start(), then call .update() once per frame to refresh
    the smoothed bands + bass + bass pulse.
    """

    def __init__(
        self,
        device:      Optional[int]  = None,
        device_name: Optional[str]  = None,
        loopback:    bool           = False,
        samplerate:  int            = SAMPLE_RATE,
        blocksize:   int            = BLOCK_SIZE,
        fft_size:    int            = FFT_SIZE,
        n_bands:     int            = N_BANDS,
    ):
        self.samplerate  = samplerate
        self.blocksize   = blocksize
        self.fft_size    = fft_size
        self.n_bands     = n_bands
        self.device      = device         # numeric index (see --list-devices)
        self.device_name = device_name    # substring match alternative
        self.loopback    = loopback

        # FFT window — pre-compute once.
        self.window = np.hanning(fft_size).astype(np.float32)

        # Log-spaced band edges, 30Hz to 16kHz.
        n_bins = fft_size // 2 + 1
        bin_freqs = np.linspace(0, samplerate / 2, n_bins)
        log_edges = np.logspace(np.log10(30), np.log10(16000), n_bands + 1)
        self._band_lo = np.searchsorted(bin_freqs, log_edges[:-1]).astype(np.int32)
        self._band_hi = np.searchsorted(bin_freqs, log_edges[1:]).astype(np.int32)
        # Guarantee at least one bin per band (collapse-safe for low sample rates).
        self._band_hi = np.maximum(self._band_hi, self._band_lo + 1)

        # Smoothed magnitudes — what the visualizer reads each frame.
        self.smoothed = np.zeros(n_bands, dtype=np.float32)
        self.raw      = np.zeros(n_bands, dtype=np.float32)

        # Bass: slow-following baseline + fast transient detector.
        self.bass          = 0.0
        self.bass_smoothed = 0.0
        self.bass_pulse    = 0.0

        # Rolling capture buffer — capture thread appends, update() reads.
        self._buf     = np.zeros(fft_size, dtype=np.float32)
        self._buf_pos = 0
        self._lock    = threading.Lock()

        # Capture thread state.
        self._stop_event: Optional[threading.Event] = None
        self._thread:     Optional[threading.Thread] = None

    # ── device picking ─────────────────────────────────────────────────────

    def _pick_capture_source(self):
        """Resolve --loopback / --device / --device-name → a soundcard
        Microphone (or virtual loopback "microphone")."""
        assert _SC_OK, "soundcard not importable"

        if self.loopback:
            # System audio. default_speaker() is the speaker we're currently
            # playing through. get_microphone(name, include_loopback=True)
            # returns a virtual mic capturing from that speaker — that's the
            # WASAPI loopback on Windows / PulseAudio monitor on Linux.
            speaker = sc.default_speaker()
            return sc.get_microphone(str(speaker.name), include_loopback=True)

        if self.device_name is not None:
            for mic in sc.all_microphones(include_loopback=True):
                if self.device_name.lower() in mic.name.lower():
                    return mic
            raise RuntimeError(
                f"No microphone matched name {self.device_name!r}. "
                f"Use --list-devices to see options."
            )

        if self.device is not None:
            mics = sc.all_microphones(include_loopback=True)
            if self.device < 0 or self.device >= len(mics):
                raise RuntimeError(
                    f"Device index {self.device} out of range "
                    f"(0..{len(mics) - 1})."
                )
            return mics[self.device]

        # Default: real default mic. Won't pick up system audio.
        return sc.default_microphone()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if not _SC_OK:
            raise RuntimeError(
                "The 'soundcard' library is required.\n"
                "Install with:  pip install soundcard"
            )

        mic = self._pick_capture_source()
        print(f"[audio] capturing from: {mic.name}", file=sys.stderr)

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._capture_loop, args=(mic,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._stop_event = None
        self._thread = None

    # ── capture thread ─────────────────────────────────────────────────────

    def _capture_loop(self, mic) -> None:
        """Continuously pull blocks and stuff them into the ring buffer.
        Runs on its own daemon thread.

        Loopback sources are always stereo on Windows — requesting channels=1
        errors with `Invalid number of channels` (the exact bug the previous
        sounddevice fallback hit). We open as stereo and downmix here."""
        try:
            with mic.recorder(
                samplerate=self.samplerate,
                channels=2,
                blocksize=self.blocksize,
            ) as rec:
                while not self._stop_event.is_set():
                    data = rec.record(numframes=self.blocksize)
                    # data is (frames, channels); downmix to mono.
                    if data.ndim > 1 and data.shape[1] > 1:
                        mono = data.mean(axis=1).astype(np.float32)
                    else:
                        mono = data.reshape(-1).astype(np.float32)
                    self._push(mono)
        except Exception as e:
            print(f"[audio] capture loop exited: {e}", file=sys.stderr)

    def _push(self, mono: np.ndarray) -> None:
        n = len(mono)
        with self._lock:
            end = self._buf_pos + n
            if end <= self.fft_size:
                self._buf[self._buf_pos:end] = mono
            else:
                split = self.fft_size - self._buf_pos
                self._buf[self._buf_pos:] = mono[:split]
                self._buf[: n - split]    = mono[split:]
            self._buf_pos = (self._buf_pos + n) % self.fft_size

    # ── main thread ────────────────────────────────────────────────────────

    def update(self) -> None:
        with self._lock:
            if self._buf_pos == 0:
                samples = self._buf.copy()
            else:
                samples = np.concatenate(
                    (self._buf[self._buf_pos:], self._buf[: self._buf_pos])
                )

        # Window + FFT.
        windowed = samples * self.window
        spectrum = np.abs(np.fft.rfft(windowed))

        # Bin into bands.
        bands = np.empty(self.n_bands, dtype=np.float32)
        for i in range(self.n_bands):
            lo, hi = self._band_lo[i], self._band_hi[i]
            bands[i] = spectrum[lo:hi].mean()

        # Spatial smoothing across bands (σ=1.2 kills bin-spike noise).
        bands = gaussian_filter1d(bands, sigma=1.2)

        # Log scale + normalize to roughly [0, ~1].
        bands = np.log1p(bands * 4.0) / 5.0
        np.clip(bands, 0.0, 1.5, out=bands)

        # Temporal EMA (the requested 0.85/0.15).
        self.smoothed[:] = self.smoothed * 0.85 + bands * 0.15
        self.raw[:]      = bands

        # Bass tracking + transient.
        n_bass = max(2, self.n_bands // 8)
        bass_now = float(bands[:n_bass].mean())
        self.bass_smoothed = self.bass_smoothed * 0.80 + bass_now * 0.20
        self.bass = bass_now
        excess = max(0.0, bass_now - self.bass_smoothed)
        self.bass_pulse = self.bass_pulse * 0.78 + excess * 6.0


# ═════════════════════════════════════════════════════════════════════════
# File-source analyzer
# ═════════════════════════════════════════════════════════════════════════


class FileAudioAnalyzer:
    """Plays an audio file via ffmpeg + sounddevice, runs FFT on the same
    PCM stream we're playing. Drop launches the visualizer with this mode
    so the viz reacts to ONLY the file Drop opened, not to other apps'
    audio on the system loopback.

    Architecture:
        ffmpeg -i file -f f32le -ar 48000 -ac 2 -  →  stdout pipe
              ↓
        capture thread reads PCM chunks  →  sounddevice.write(chunk)  (speakers)
                                         →  ring buffer  (FFT for viz)

    Same public surface as AudioAnalyzer: start/stop/update + .smoothed,
    .bass, .bass_smoothed, .bass_pulse, .n_bands, .samplerate. The
    Visualizer class doesn't have to care which analyzer it got.
    """

    def __init__(
        self,
        file_path:   str,
        ffmpeg_path: Optional[str] = None,
        start_seconds: float       = 0.0,
        status_file: Optional[str] = None,
        samplerate:  int           = SAMPLE_RATE,
        blocksize:   int           = BLOCK_SIZE,
        fft_size:    int           = FFT_SIZE,
        n_bands:     int           = N_BANDS,
    ):
        self.file_path     = file_path
        self.ffmpeg_path   = ffmpeg_path or self._find_ffmpeg()
        self.start_seconds = start_seconds
        self.status_file   = status_file
        self.samplerate    = samplerate
        self.blocksize     = blocksize
        self.fft_size      = fft_size
        self.n_bands       = n_bands

        # FFT window — pre-compute once.
        self.window = np.hanning(fft_size).astype(np.float32)

        # Log-spaced band edges, 30Hz to 16kHz.
        n_bins = fft_size // 2 + 1
        bin_freqs = np.linspace(0, samplerate / 2, n_bins)
        log_edges = np.logspace(np.log10(30), np.log10(16000), n_bands + 1)
        self._band_lo = np.searchsorted(bin_freqs, log_edges[:-1]).astype(np.int32)
        self._band_hi = np.searchsorted(bin_freqs, log_edges[1:]).astype(np.int32)
        self._band_hi = np.maximum(self._band_hi, self._band_lo + 1)

        # Smoothed magnitudes — what the visualizer reads each frame.
        self.smoothed = np.zeros(n_bands, dtype=np.float32)
        self.raw      = np.zeros(n_bands, dtype=np.float32)

        # Bass tracking + transient detector.
        self.bass          = 0.0
        self.bass_smoothed = 0.0
        self.bass_pulse    = 0.0

        # Rolling FFT-input buffer.
        self._buf     = np.zeros(fft_size, dtype=np.float32)
        self._buf_pos = 0
        self._lock    = threading.Lock()

        # Playback-position tracking. samples_played counts mono frames
        # we've actually pushed to the output stream. Position in seconds
        # is start_seconds + samples_played / samplerate. We write this
        # to status_file every ~250ms so Drop can sync back on close.
        self._samples_played = 0
        self._last_status_write = 0.0

        self._stop_event: Optional[threading.Event]  = None
        self._thread:     Optional[threading.Thread] = None
        self._proc:       Optional[subprocess.Popen] = None
        self._out_stream                              = None  # sd.OutputStream

    # ── helpers ────────────────────────────────────────────────────────────

    def _find_ffmpeg(self) -> str:
        """Locate ffmpeg. Caller can pass an explicit path; otherwise we
        look in PATH. Drop bundles its own and passes the path explicitly
        when launching, so this fallback only matters for standalone use."""
        found = shutil.which("ffmpeg")
        if found:
            return found
        return "ffmpeg"  # let it fail at Popen with a clear OSError

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if not _SD_OK:
            raise RuntimeError(
                "The 'sounddevice' library is required for --file mode.\n"
                "Install with:  pip install sounddevice"
            )

        # Spawn ffmpeg producing interleaved stereo float32 PCM at our
        # target sample rate. `-loglevel error` suppresses the verbose
        # progress chatter; `-nostdin` avoids it stealing terminal input
        # if the visualizer is run from a terminal.
        cmd = [
            self.ffmpeg_path,
            "-loglevel", "error",
            "-nostdin",
            "-ss", f"{self.start_seconds:.3f}",
            "-i", self.file_path,
            "-f", "f32le",
            "-ar", str(self.samplerate),
            "-ac", "2",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"ffmpeg not found at {self.ffmpeg_path!r}. Pass --ffmpeg-path "
                f"or install ffmpeg and put it on PATH."
            )

        # Open the speaker output. blocksize=0 lets the host pick something
        # reasonable; we feed it with .write() in arbitrary chunk sizes.
        self._out_stream = sd.OutputStream(
            samplerate = self.samplerate,
            channels   = 2,
            dtype      = "float32",
            blocksize  = 0,
        )
        self._out_stream.start()

        print(f"[audio] playing file: {self.file_path}", file=sys.stderr)

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Force-write the final position so Drop's post-close sync gets
        # the exact spot we stopped at, not whatever was 0–250ms stale.
        self._last_status_write = 0.0
        self._maybe_write_status()

        if self._stop_event is not None:
            self._stop_event.set()
        if self._proc is not None:
            try: self._proc.terminate()
            except Exception: pass
            try: self._proc.wait(timeout=1.0)
            except Exception:
                try: self._proc.kill()
                except Exception: pass
            self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._out_stream is not None:
            try: self._out_stream.stop()
            except Exception: pass
            try: self._out_stream.close()
            except Exception: pass
            self._out_stream = None

    # ── streaming thread ───────────────────────────────────────────────────

    def _stream_loop(self) -> None:
        """Read PCM chunks from ffmpeg, push to speakers, write to FFT buffer.
        Runs until ffmpeg's stdout closes (end of file) or stop() is called."""
        # 4 bytes/sample × 2 channels = 8 bytes/frame. Read in BLOCK_SIZE
        # frames per chunk — small enough for responsive FFT, big enough
        # that we're not making thousands of read calls per second.
        bytes_per_frame = 8
        chunk_frames = max(self.blocksize, 256)
        chunk_bytes = chunk_frames * bytes_per_frame

        while not self._stop_event.is_set():
            try:
                raw = self._proc.stdout.read(chunk_bytes)
            except Exception:
                break
            if not raw:
                # End of file — visualizer keeps running with frozen
                # FFT state (or silence as it decays). Just exit the loop.
                break

            # Interpret as interleaved stereo float32.
            stereo = np.frombuffer(raw, dtype=np.float32)
            if len(stereo) < 2:
                continue
            # Trim to even length (defensive — partial frames shouldn't
            # happen with bufsize=0 but ffmpeg sometimes emits shorts at EOF).
            if len(stereo) % 2 != 0:
                stereo = stereo[:-1]
            stereo = stereo.reshape(-1, 2)

            # Play through speakers. sd.write() blocks if the output queue
            # is full, which naturally paces our pull from ffmpeg — we
            # consume audio at exactly real-time speed.
            try:
                self._out_stream.write(stereo)
            except Exception as e:
                print(f"[audio] output write failed: {e}", file=sys.stderr)
                break

            # Downmix to mono for FFT.
            mono = stereo.mean(axis=1)
            self._push(mono)

            # Track position for status-file writes. samples_played
            # counts frames we've actually queued to the output stream;
            # since sd.write() blocks on full queue, this stays close to
            # what's actually audible.
            self._samples_played += len(mono)
            self._maybe_write_status()

    def _maybe_write_status(self) -> None:
        """Write current playback position to the status file at most
        once every 250ms. Used by Drop to seek itself to the viz's last
        position when the visualizer closes."""
        if not self.status_file:
            return
        now = time.monotonic()
        if now - self._last_status_write < 0.25:
            return
        self._last_status_write = now
        pos = self.start_seconds + self._samples_played / self.samplerate
        try:
            with open(self.status_file, "w", encoding="utf-8") as f:
                f.write(f"{pos:.3f}\n")
        except Exception:
            # Best-effort — losing a status write only means Drop's
            # post-close sync is up to 250ms stale.
            pass

    def _push(self, mono: np.ndarray) -> None:
        n = len(mono)
        with self._lock:
            end = self._buf_pos + n
            if end <= self.fft_size:
                self._buf[self._buf_pos:end] = mono
            else:
                split = self.fft_size - self._buf_pos
                self._buf[self._buf_pos:] = mono[:split]
                self._buf[: n - split]    = mono[split:]
            self._buf_pos = (self._buf_pos + n) % self.fft_size

    # ── main thread (FFT + smoothing) ──────────────────────────────────────
    # Same body as AudioAnalyzer.update() — kept inline rather than factored
    # to a shared base class because audio.py is a single short module and
    # the duplication makes it easy to tune one path without affecting the
    # other.

    def update(self) -> None:
        with self._lock:
            if self._buf_pos == 0:
                samples = self._buf.copy()
            else:
                samples = np.concatenate(
                    (self._buf[self._buf_pos:], self._buf[: self._buf_pos])
                )

        windowed = samples * self.window
        spectrum = np.abs(np.fft.rfft(windowed))

        bands = np.empty(self.n_bands, dtype=np.float32)
        for i in range(self.n_bands):
            lo, hi = self._band_lo[i], self._band_hi[i]
            bands[i] = spectrum[lo:hi].mean()

        bands = gaussian_filter1d(bands, sigma=1.2)
        bands = np.log1p(bands * 4.0) / 5.0
        np.clip(bands, 0.0, 1.5, out=bands)

        self.smoothed[:] = self.smoothed * 0.85 + bands * 0.15
        self.raw[:]      = bands

        n_bass = max(2, self.n_bands // 8)
        bass_now = float(bands[:n_bass].mean())
        self.bass_smoothed = self.bass_smoothed * 0.80 + bass_now * 0.20
        self.bass = bass_now
        excess = max(0.0, bass_now - self.bass_smoothed)
        self.bass_pulse = self.bass_pulse * 0.78 + excess * 6.0
