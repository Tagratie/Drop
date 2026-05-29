# Audio Visualizer

GPU-rendered circular audio visualizer in the style of Trap Nation / NCS /
Monstercat. Live FFT analysis, multi-pass bloom, bass-reactive camera, GPU
particle bursts, procedural starfield background. Built on ModernGL + GLFW.

## Install

```bash
python -m venv .venv
# Windows:    .venv\Scripts\activate
# Linux/mac:  source .venv/bin/activate

pip install -r requirements.txt
```

PortAudio (sounddevice's backend) usually installs with pip on Windows.
On Linux: `sudo apt install libportaudio2`. On macOS: `brew install portaudio`.

## Run

```bash
# Use default microphone:
python main.py

# List audio devices to find the one you want:
python main.py --list-devices

# Use a specific device:
python main.py --device 5

# Windows: capture system audio (what's playing through your speakers):
python main.py --loopback

# Custom resolution / fullscreen:
python main.py --width 1920 --height 1080
python main.py --fullscreen
```

Press **ESC** to quit. The window is resizable; the renderer rebuilds its
framebuffers automatically on size change.

## Render to a video file

Instead of opening a window, render the visualizer for an audio file
straight to an `.mp4`. This is **offline** — it runs as fast as your GPU and
encoder allow, not in real time, and is deterministic.

```bash
# Render song.mp3 to a 1080p60 video with the music muxed in:
python main.py --file song.mp3 --render out.mp4 --width 1920 --height 1080 --fps 60

# Lighter/faster: 720p30
python main.py --file song.mp3 --render out.mp4 --fps 30
```

`--render` requires `--file` (the audio drives the visuals and is muxed into
the output) and needs **ffmpeg** (on PATH, or pass `--ffmpeg-path` — Drop
bundles one). Each frame is drawn to an offscreen framebuffer and piped to
ffmpeg, which encodes H.264 + AAC and trims to the shorter stream.

## System audio by OS

The mic path "just works" everywhere. System audio (what's coming out of
your speakers, fed back into the visualizer) is platform-specific:

- **Windows** — `--loopback`. Uses WASAPI loopback on the default output
  device. No extra software needed.
- **Linux** — Set up a PulseAudio monitor source on your sink (most pulse
  setups already have `<sink>.monitor` available). Find its index with
  `--list-devices`, then `python main.py --device <N>`.
- **macOS** — Install [BlackHole](https://existential.audio/blackhole/) or
  a similar virtual audio cable. Route audio to it via the multi-output
  device trick, find its index in `--list-devices`, then `--device <N>`.

## Architecture

```
main.py            GLFW window, OpenGL 3.3 core context, main loop
visualizer.py      Audio → ring magnitudes, camera state, particles
renderer.py        ModernGL passes: scene FBO → bloom ping-pong → composite
audio.py           sounddevice capture, rolling FFT, band binning, smoothing
shaders/           GLSL source — separate files, edit freely
```

### Per-frame pipeline

```
main loop ──► visualizer.update(dt)
              ├─ analyzer.update()                 (FFT + smoothing)
              ├─ map bands → ring vertex magnitudes
              ├─ update camera (zoom, shake, rot)
              └─ advance + spawn particles

main loop ──► visualizer.render(aspect)
              └─ renderer.render(...)
                 ├─ PASS 1: scene FBO
                 │   ├─ background (procedural stars + vignette)
                 │   ├─ waveform ring (additive)
                 │   ├─ inner core ring (additive)
                 │   └─ particles (additive points)
                 ├─ PASS 2: extract bright pixels → bloom_tex_a
                 ├─ PASS 3: ping-pong separable Gaussian blur
                 │   └─ N iterations of horizontal then vertical
                 └─ PASS 4: composite scene + bloom + chromatic aberration
                            + Reinhard tonemap → screen
```

## How the FFT smoothing works

There are **three** layers of smoothing, each addressing a different
problem:

1. **Spatial Gaussian across FFT bins** (`audio.py`, σ=1.2). Real FFT
   output is jagged at the bin level — single-bin spikes look like
   electrical noise. Gaussian-smoothing across the bands turns those
   jagged spikes into smooth lobes without losing the overall shape.

2. **Temporal exponential moving average** (`audio.py`, 0.85/0.15). Even
   smooth FFT magnitudes jitter frame-to-frame as audio chunks come in.
   `smooth = smooth*0.85 + new*0.15` gives ~200ms response time at
   ~60fps audio updates — peaks are tracked, single-frame noise filtered.

3. **Spatial Gaussian across ring vertices** (`visualizer.py`, σ=2.0).
   After interpolating bands onto 512 ring vertices, a final Gaussian
   along the ring (with `mode="wrap"` for seamless closure) turns any
   remaining transitions between adjacent bands into fluid curves.

The bass-pulse transient detector is intentionally fast — it spikes briefly
on kick drums and decays in ~150ms. That drives camera shake and particle
spawning. The slower bass EMA drives zoom and the inner core's breathing
pulse.

## How the bloom works

Standard separable Gaussian bloom:

1. **Threshold pass** (`bloom_extract.frag`). The scene texture is sampled
   and only pixels with perceived luminance > `u_threshold` survive — and
   even those have their brightness softly knee'd via `smoothstep`. This
   becomes the "bright pixels only" input to the blur.

2. **Separable Gaussian blur**. A full 2D Gaussian of N×N taps is just two
   1D Gaussians (one horizontal, one vertical) — `O(N)` instead of `O(N²)`.
   We ping-pong between two half-resolution framebuffers, doing H then V
   each iteration. Two iterations gives a wide soft halo; each iteration
   widens the kernel via `u_radius`.

3. **Composite** (`composite.frag`). The original scene is sampled with
   tiny per-channel radial offsets (chromatic aberration), then the
   blurred bright pass is added on top, scaled by `u_bloom_strength`.
   Reinhard tonemap (`x / (x + 0.85)`) prevents accumulated bloom from
   blowing out to pure white.

The whole bloom pipeline runs at half-resolution because blur quality is
indistinguishable at 2× scale but the cost is 4× lower.

## How the waveform feels organic

The ring isn't a stack of separate bars — it's a continuous triangle strip
with `n_segments × 2` vertices. Each pair (inner edge / outer edge) sits at
the same angle around the circle. Per frame, the CPU writes a per-vertex
"magnitude" attribute, and the vertex shader does

```glsl
r_inner = u_base_radius + magnitude * u_amplitude;
r_outer = r_inner + u_thickness;
```

so the entire ring breathes outward where the audio is loud. Combined with
the three-layer smoothing above and `mode="wrap"` Gaussian along the ring,
the contour stays smooth and continuous — no visible "bar" boundaries.

## Customization

Most "feel" parameters live in two places:

**`renderer.py` `__init__`** — visual style:

```python
self.prog_waveform["u_base_radius"].value = 0.42   # ring radius
self.prog_waveform["u_thickness"].value   = 0.012  # ring thickness
self.prog_waveform["u_amplitude"].value   = 0.35   # spike length
self.prog_waveform["u_color_core"].value  = (1.0, 1.0, 1.0)
self.prog_waveform["u_color_glow"].value  = (1.0, 0.40, 0.10)

self.prog_extract["u_threshold"].value         = 0.55  # bloom threshold
self.prog_composite["u_bloom_strength"].value  = 1.35
self.prog_composite["u_chromatic"].value       = 0.005
```

**`visualizer.py` `update`** — reactivity:

```python
target_zoom    = 1.0 + bass * 0.06 + pulse * 0.04   # zoom amount
self.rotation += dt * (0.04 + total_energy * 0.40)  # rotation speed
target_shake_x = (random) * pulse * 0.025           # shake amount
if pulse > 0.35:  # particle spawn threshold
    n_spawn = min(int(pulse * 18), 40)
```

**`audio.py`** — frequency response:

```python
SAMPLE_RATE = 48000
FFT_SIZE    = 2048    # bigger = better low-freq resolution, more latency
N_BANDS     = 96      # ring resolution will smooth this further

# Inside update():
bands = gaussian_filter1d(bands, sigma=1.2)            # FFT smoothing
bands = np.log1p(bands * 4.0) / 5.0                    # log scale + normalize
self.smoothed = self.smoothed * 0.85 + bands * 0.15    # temporal EMA
```

## Performance

Targets 1080p @ 60fps. Hot loop per frame:

| Pass             | Cost (rough)         | Notes                          |
|------------------|----------------------|--------------------------------|
| Audio FFT        | ~0.5ms               | 2048-pt rfft + 96-band binning |
| Background       | ~0.2ms               | Procedural in frag shader      |
| Waveform ring    | ~0.1ms               | 1026-vertex triangle strip     |
| Particles        | ~0.1ms               | Up to 800 point sprites        |
| Bloom extract    | ~0.3ms (half-res)    | Single fullscreen pass         |
| Bloom blur (x4)  | ~1.0ms (half-res)    | 2 iterations × H/V             |
| Composite        | ~0.4ms               | Full-res CA + tonemap          |
| **Total GPU**    | **~2ms**             | Well under 16.7ms (60fps)      |

CPU work is dominated by the FFT and gaussian filters — both numpy/scipy
calls into C. Should hit 144fps+ on modern hardware if you `swap_interval(0)`.
