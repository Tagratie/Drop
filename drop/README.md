# Drop

TikTok-style media downloader, organizer, and player for Windows. Pulls
audio and video from YouTube, Spotify, TikTok, and anywhere else
yt-dlp supports. Builds local libraries of "Edits" (video) and "Songs"
(audio), plays them in a vertical feed UI, and launches a GPU-rendered
audio visualizer for audio-only playback.

Built on Tkinter for the UI, python-vlc for playback, yt-dlp + ffmpeg
for downloads, and ModernGL + GLFW for the visualizer (in its own
subprocess).

## Features

- **Vertical feed playback** — TikTok-style swipe through library items,
  with mouse-wheel and keyboard navigation
- **One-input download** — paste any supported URL into the bar pill;
  Drop figures out what kind it is and routes accordingly
- **Spotify support without API keys** — scrapes the public oEmbed
  endpoint for track metadata, then searches YT Music for the audio.
  No client secret required.
- **Two library tabs** — "Edits" (video) and "Songs" (audio) with
  separate organization, search, and filter
- **Thumbnails generated locally** — FFmpeg keyframe seek pulls a frame
  from the middle of each video. Cached for fast scrolling.
- **Per-item resume positions** — Drop remembers where you left off in
  every video and picks back up there next time
- **Hover preview** — hover any item in list view, get a silent preview
  of the video playing in its tile
- **Built-in audio visualizer** — click "Open Visualizer" while playing
  an audio file, get a fullscreen GPU-rendered circular waveform that
  reacts only to that file (not other apps' audio)
- **Auto-update** on launch — Drop checks for and applies new releases
  silently via a private storage bucket
- **Custom widget toolkit** — rounded buttons, icon buttons, pill
  toggles, all drawn on Tk canvases for consistent rendering across
  Windows DPI/theme settings

## Install

Requires Python 3.11+ (developed on 3.14) on Windows.

```powershell
git clone https://github.com/tagratie/drop.git
cd drop

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

Bundled external binaries live at the repo root next to `drop.py`:

- `ffmpeg.exe` — thumbnail keyframe extraction, audio decode for the
  visualizer, format conversion in downloads
- `yt-dlp.exe` — primary downloader for video/audio URLs

If your fork doesn't ship these, drop them in next to `drop.py` —
Drop's `get_bin()` helper checks the repo root first, then PATH.

## Run

```powershell
# From source:
python drop.py

# Or run the package directly:
python -m drop

# The PyInstaller build:
.\dist\Drop.exe
```

Drop opens to the libraries view. Drag any URL onto the bar input or
type/paste it, and the download starts immediately. Click the gear in
the top-right for settings (resume toggle, library export/import).

## File structure

```
Drop/
├── drop.py                       Tiny entry point — bootstraps the package
├── drop.ico                      Window + EXE icon
├── Drop.spec                     PyInstaller build spec
├── ffmpeg.exe                    Bundled ffmpeg (used by cache + visualizer)
├── yt-dlp.exe                    Bundled yt-dlp (download backend)
├── requirements.txt              Python deps
│
├── drop/                         Main app package
│   ├── __main__.py               Allows `python -m drop`
│   ├── app.py                    Main App class — layout, navigation,
│   │                             settings panel, library view, bar input
│   ├── player.py                 VLC FeedPlayer — TikTok-style feed playback,
│   │                             audio-mode visualizer launcher canvas
│   ├── library.py                Library state — items, persistence, search
│   ├── downloader.py             URL → file orchestration, Spotify resolver,
│   │                             yt-dlp invocation, progress reporting
│   ├── cache.py                  Thumbnail + preview cache, ffmpeg keyframe
│   │                             extraction, parallel decode workers
│   ├── updater.py                Auto-update — checks remote, downloads,
│   │                             applies on next restart
│   ├── visualizer_launcher.py    Spawn + manage the GLFW visualizer subprocess
│   ├── widgets.py                Custom Tk widgets (RoundedButton,
│   │                             IconButton, DotsButton, TogglePill, ...)
│   ├── theme.py                  Colors, fonts, paths, format constants
│   ├── platform_win.py           Windows-specific helpers (HWND, NoWindow flag)
│   └── utils.py                  Misc helpers (detect_kind, humanize_size,
│                                  humanize_time, get_bin, open_path, ...)
│
├── visualizer/                   GPU audio visualizer (separate subproject —
│                                 see its own README)
│   ├── main.py
│   ├── audio.py
│   ├── renderer.py
│   ├── visualizer.py
│   ├── shaders/
│   └── requirements.txt
│
├── Libraries/                    Where downloaded media lives
│   ├── Edits/                    .mp4 video files
│   └── Songs/                    .mp3 audio files
│
├── build/                        PyInstaller intermediates
└── dist/                         PyInstaller output (Drop.exe lives here)
```

## How the major pieces work

### Library view + feed player
`app.py` owns the outer layout: top bar with library tabs (Edits /
Songs), the URL input pill, search, the library tile grid in list mode,
and the vertical feed in feed mode. Clicking any tile opens the feed
view starting at that item; the feed uses `FeedPlayer` (`player.py`)
which wraps a single persistent `vlc.MediaPlayer` and swaps media in
and out as the user scrolls. One VLC instance, many items.

### Downloads
`downloader.py` accepts a URL, calls `detect_kind()` from `utils.py` to
figure out what host it's from, and routes accordingly:

- **YouTube / TikTok / generic** — straight to `yt-dlp.exe` with format
  flags appropriate to the requested kind (mp4 best for video, m4a/mp3
  for audio)
- **Spotify** — fetches `https://open.spotify.com/oembed?url=...`,
  parses title + artist out of the response, runs a YT Music search
  (`ytsearch1:`) for the closest match, then hands that off to yt-dlp.
  No Spotify API credentials required, no `spotdl` dependency.

Progress is reported live via a Tk widget that shows the running
percent in the bar pill itself — the bar morphs from idle to a
progress strip as the download runs.

### Caching + thumbnails
`cache.py` generates a thumbnail for every video on first sight. Uses
ffmpeg with `-ss <middle>` to seek to a keyframe near the middle of
the video and grab a single frame. Workers are sized to `os.cpu_count`
so large library imports parallelize. Cached as `.jpg` in a sidecar
directory with a `v3` invalidation prefix in the filenames so an
algorithm change can blow the old cache without needing manual cleanup.

### Auto-update
`updater.py` polls a private storage bucket on app launch for a
`manifest.json` describing the latest version. If a newer one exists,
the new EXE is downloaded to a temp path, and on next restart Drop
swaps itself in atomically via a rename + relaunch. Skipped when
running from source.

### Audio visualizer integration
When you open an audio-only file in Drop and click the "Open
Visualizer" button on the player canvas, this happens:

1. Player reads VLC's current position via `get_time()`
2. VLC gets triple-silenced: `set_pause(1)` + `audio_set_mute(True)` +
   `audio_set_volume(0)`. Belt-and-suspenders to guarantee no
   double-audio with the visualizer.
3. `visualizer_launcher.py` spawns the visualizer subprocess with
   `--file <current track> --start <position> --ffmpeg-path <bundled
   ffmpeg> --status-file <temp file>`
4. The visualizer plays the file itself via ffmpeg + sounddevice and
   writes its current playback position to the status file every
   ~250ms
5. Drop polls the subprocess every 500ms. When it exits, Drop reads
   the last status line and seeks VLC to that position — so closing
   the viz "transfers position back" to Drop
6. VLC's audio settings are restored (mute, volume — but not pause;
   user resumes manually)

The visualizer is a standalone subproject and works without Drop. See
[the visualizer README](visualizer/README.md) for its own architecture,
CLI, and runtime controls.

## Building

PyInstaller builds the EXE from `Drop.spec`:

```powershell
pyinstaller Drop.spec
```

The output is `dist/Drop.exe` — single-file build with the icon, all
imports collected, and the bundled binaries (`ffmpeg.exe`, `yt-dlp.exe`)
included as data.

### Visualizer build

For the packaged build, the visualizer is built separately as its own
one-file EXE next to `Drop.exe`:

```powershell
cd visualizer
pyinstaller --onefile --windowed --name visualizer ^
    --add-data "shaders;shaders" main.py
copy dist\visualizer.exe ..\dist\
```

The launcher (`drop/visualizer_launcher.py`) auto-detects this at
runtime — when Drop is frozen it looks for `visualizer.exe` next to
`Drop.exe`; when running from source it uses `visualizer/main.py`
directly via the current Python interpreter. No config needed.

## Customization

Drop's appearance comes from `drop/theme.py`. The relevant constants:

- `BG`, `BG2`, `BG3` — background shades (darkest → lightest)
- `TEXT`, `MUTED`, `SOFT` — foreground text shades (brightest → dimmest)
- `ACCENT`, `ACCENT_D` — accent + accent dark
- `VIDEO_EXTS`, `AUDIO_EXTS` — extension sets used by `detect_kind`
- `DOWNLOADS` — default library root (defaults to `./Libraries`)
- `THUMB_DIR`, `THUMB_W` — thumbnail cache + width

Fonts are loaded by `app.py` using Segoe UI Mono with size variants for
buttons, labels, metadata, etc. — exposed on `self.f_btn`, `self.f_meta`,
`self.f_label`, `self.f_card_t`, `self.f_chip`.

## License

(your choice)

---

github.com/tagratie
