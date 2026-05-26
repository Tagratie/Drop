"""Visualizer orchestration.

Pulls FFT bands from the AudioAnalyzer, maps them onto the ring mesh,
updates the camera (zoom / shake / rotation), spawns and ages particles,
and tells the Renderer what to draw.

Keeping the audio→visual mapping in this file (not in renderer.py) means
the renderer stays pure GPU-side and this file is where you tune the
"feel" of the visualizer.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter1d


# How the FFT bands map around the ring:
#   "mirror"  - 0° bands going one way, mirrored back going the other,
#               so the visualizer is left/right symmetric (NCS / Monstercat style)
#   "wrap"    - bands go all the way around once
RING_MAPPING = "mirror"


class Visualizer:
    def __init__(self, analyzer, renderer, initial_rotation: float = 0.0,
                  settings=None):
        self.analyzer = analyzer
        self.renderer = renderer
        # Settings object — any object with the expected attribute names
        # works (bloom, shake, particles, chromatic, bg_drift, bg_twinkle,
        # bg_vignette). main.py wires up keyboard toggles for these.
        self.settings = settings

        # Camera state. Rotation is now a static value set at construction
        # time (typically from a CLI flag) and only changed by keyboard
        # input in main.py — it doesn't auto-drift anymore.
        self.zoom = 1.0
        self.rotation = initial_rotation
        self.shake_x = 0.0
        self.shake_y = 0.0

        # Particles: each entry is [x, y, vx, vy, life, size]. Plain Python
        # list because we frequently spawn / cull individuals — array
        # reallocation would dominate otherwise.
        self.particles: List[list] = []

        # Wall-clock seconds since visualizer start. Background shader uses
        # it for the twinkle phase.
        self.time = 0.0

        # Pre-allocated per-vertex magnitude buffer. Size matches the
        # renderer's ring (N_SEGMENTS + 1).
        self._ring_size = renderer.N_SEGMENTS + 1
        self._ring_mags = np.zeros(self._ring_size, dtype=np.float32)

        # Pre-built band-position lookup. For each ring vertex we know which
        # FFT band (or pair of bands, with a fractional weight) feeds it.
        # Precomputing this saves a pile of work per frame.
        n_bands = analyzer.n_bands
        self._band_index_lo = np.zeros(self._ring_size, dtype=np.int32)
        self._band_index_hi = np.zeros(self._ring_size, dtype=np.int32)
        self._band_frac     = np.zeros(self._ring_size, dtype=np.float32)
        for i in range(self._ring_size):
            t = i / max(self._ring_size - 1, 1)  # 0..1 around the ring
            if RING_MAPPING == "mirror":
                # 0..0.5 → 0..(n_bands-1), 0.5..1 → (n_bands-1)..0
                if t < 0.5:
                    band_pos = t * 2.0 * (n_bands - 1)
                else:
                    band_pos = (1.0 - t) * 2.0 * (n_bands - 1)
            else:
                band_pos = t * (n_bands - 1)
            lo = int(band_pos)
            hi = min(lo + 1, n_bands - 1)
            self._band_index_lo[i] = lo
            self._band_index_hi[i] = hi
            self._band_frac[i]     = band_pos - lo

    # ── per-frame update ───────────────────────────────────────────────────

    def update(self, dt: float) -> None:
        self.time += dt
        self.analyzer.update()

        bands = self.analyzer.smoothed
        bass  = self.analyzer.bass_smoothed
        pulse = self.analyzer.bass_pulse

        # ── Build per-vertex magnitudes for the ring ───────────────────────
        # Interpolate between adjacent bands using the precomputed indices.
        # This is fully vectorized — runs in microseconds.
        lo_vals = bands[self._band_index_lo]
        hi_vals = bands[self._band_index_hi]
        ring = lo_vals * (1.0 - self._band_frac) + hi_vals * self._band_frac

        # Extra spatial smoothing along the ring. The audio-side smoothing
        # already kills jagged FFT bins, but interpolation can still leave
        # sharp transitions where bands change. A small sigma here turns
        # the spikes into fluid organic lobes — the requested look.
        ring = gaussian_filter1d(ring, sigma=2.0, mode="wrap")

        self._ring_mags[:] = ring
        self.renderer.set_ring_magnitudes(self._ring_mags)

        # ── Camera ─────────────────────────────────────────────────────────
        # Camera zoom disabled by user request — the visualizer stays at
        # a constant scale and bass energy doesn't push it in/out. Bass
        # still drives the inner-core ring pulse and shake/particles.
        self.zoom = 1.0

        # Total energy across all bands — used by the background shader to
        # drive its subtle drift and by the shake target below.
        total_energy = float(bands.mean())
        self._energy = total_energy

        # Rotation: kept at 0 by request — the static framing reads more
        # cinematic with the bloom and bass-pulse than the slow drift did.
        # Leaving the field in place (not removed) so the renderer's
        # rotation uniform is still fed consistently.
        # self.rotation stays 0.

        # Shake: target jittered offset proportional to bass_pulse. We
        # generate a new random target each frame and smoothly lerp toward
        # it — pure per-frame random looks like seizures, lerped looks
        # like a real handheld camera in a club. Disabled by toggle.
        shake_on = self._toggle("shake", True)
        if shake_on:
            target_shake_x = (np.random.rand() - 0.5) * pulse * 0.025
            target_shake_y = (np.random.rand() - 0.5) * pulse * 0.025
        else:
            target_shake_x = 0.0
            target_shake_y = 0.0
        # Lerp toward target regardless — when shake is turned off, the
        # current value smoothly winds down to zero instead of snapping.
        self.shake_x = self.shake_x * 0.55 + target_shake_x * 0.45
        self.shake_y = self.shake_y * 0.55 + target_shake_y * 0.45

        # ── Particles ──────────────────────────────────────────────────────
        # Spawn on transient kicks. Toggleable — when disabled we stop
        # spawning new ones; existing particles continue to age out.
        if self._toggle("particles", True) and pulse > 0.35:
            n_spawn = min(int(pulse * 18), 40)
            for _ in range(n_spawn):
                angle = float(np.random.rand() * 2.0 * np.pi)
                speed = 0.25 + float(np.random.rand()) * 0.45
                self.particles.append([
                    0.0,                       # x (start at origin)
                    0.0,                       # y
                    float(np.cos(angle)) * speed,   # vx
                    float(np.sin(angle)) * speed,   # vy
                    1.0,                       # life
                    3.0 + float(np.random.rand()) * 5.0,  # size in pixels
                ])

        # Advance + cull. Inline loop is fine — 100s of particles, no need
        # to vectorize unless this becomes a hotspot.
        survivors = []
        for p in self.particles:
            p[0] += p[2] * dt              # x += vx * dt
            p[1] += p[3] * dt              # y += vy * dt
            p[2] *= 0.985                  # drag — slows over time
            p[3] *= 0.985
            p[4] -= dt * 0.65              # life decay
            if p[4] > 0.0:
                survivors.append(p)
        # Hard cap so a really busy section can't grow the list unbounded.
        if len(survivors) > 800:
            survivors = survivors[-800:]
        self.particles = survivors

        # Pack for the renderer: (x, y, size, life)
        self.renderer.set_particles(
            (p[0], p[1], p[5], max(0.0, p[4])) for p in self.particles
        )

    # ── settings access ───────────────────────────────────────────────────

    def _toggle(self, name: str, default: bool) -> bool:
        """Read a toggle from self.settings, falling back to default if no
        settings object was supplied or the attribute isn't defined."""
        if self.settings is None:
            return default
        return bool(getattr(self.settings, name, default))

    # ── render ─────────────────────────────────────────────────────────────

    def render(self, aspect: float) -> None:
        self.renderer.render(
            time     = self.time,
            aspect   = aspect,
            zoom     = self.zoom,
            rotation = self.rotation,
            shake    = (self.shake_x, self.shake_y),
            bass     = self.analyzer.bass_smoothed,
            pulse    = self.analyzer.bass_pulse,
            energy   = getattr(self, "_energy", 0.0),
            settings = self.settings,
        )
