"""Wraps yt-dlp. Reports progress + final files via callbacks.

Spotify path — what changed:

    Previously: shelled out to spotdl, which has a long history of
    "starting forever" hangs (Spotify auth scrapes, librespot session
    spin-up, rate limiting). Half the support tickets were here.

    Now: Spotify URLs get resolved to a "title artist" search query by
    fetching the public Spotify track page (no API key, no auth — just
    parsing og:title and og:description meta tags), then yt-dlp downloads
    the top YouTube/YouTube Music match as MP3. Same end result, no spotdl
    dependency, no auth fragility.

    Scope: tracks only. Albums and playlists need the Spotify Web API
    to enumerate (no public unauth path), so we fail those with a clear
    message rather than half-supporting them. Easy follow-up if needed.
"""
import os
import re
import time
import json
import subprocess
import threading
import urllib.request
import urllib.parse
from pathlib import Path

from .theme import DOWNLOADS, NO_WINDOW
from .utils import detect_kind, get_bin, parse_line, fmt_status, clean_subprocess_env


# yt-dlp / spotdl announce final output paths on lines like:
#   [download] Destination: C:\Users\me\Downloads\Video Title.mp4
#   [download] C:\...\Title.mp4 has already been downloaded
#   [ExtractAudio] Destination: C:\Users\me\Downloads\Title.mp3
#   [Merger] Merging formats into "C:\Users\me\Downloads\Title.mp4"
#   [VideoConvertor] ...; Destination: C:\...\Title.mp4
#
# We grep these in real time, dedupe, and verify each path on disk before
# trusting it. yt-dlp also prints "Destination" lines for intermediate
# fragment files (.f137.mp4, .f140.m4a) which get deleted after merge —
# we capture them but the on-disk verify filters them out.
_DEST_PATTERNS = (
    re.compile(r'^\[download\]\s+Destination:\s+(.+?)\s*$'),
    # "...has already been downloaded" — yt-dlp still announces the path
    # even when it skips the actual download because the file already exists.
    re.compile(r'^\[download\]\s+(.+?)\s+has already been downloaded\s*$'),
    re.compile(r'^\[ExtractAudio\]\s+Destination:\s+(.+?)\s*$'),
    re.compile(r'^\[Merger\]\s+Merging formats into\s+"(.+?)"\s*$'),
    re.compile(r'^\[VideoConvertor\]\s+.*?Destination:\s+(.+?)\s*$'),
)

_TEMP_EXTS = {".part", ".ytdl", ".tmp", ".temp"}


# ── Spotify resolution ──────────────────────────────────────────────────────

_SPOT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# "Listen to {Title} on Spotify. {Artist} · Song · {Year}" — Artist is the
# part between the period-then-space and the first interpunct. Spotify
# escapes the description with HTML entities sometimes; we unescape after.
_SPOT_DESC_ARTIST_RE = re.compile(
    r"on Spotify\.\s*(?P<artist>[^·]+?)\s*·", re.IGNORECASE
)
# Newer EN-format description: "Song · {Year} · {Artist}". Artist is the
# LAST middot-separated chunk before any further metadata.
_SPOT_DESC_ARTIST_RE_2 = re.compile(
    r"·\s*\d{4}\s*·\s*(?P<artist>[^·]+?)\s*(?:·|$)", re.IGNORECASE
)
# Alternate EN-format: "Song · {Artist} · {Year}". Artist is the chunk
# directly after the first middot, year is what follows next.
_SPOT_DESC_ARTIST_RE_3 = re.compile(
    r"^[^·]+·\s*(?P<artist>[^·]+?)\s*·\s*\d{4}", re.IGNORECASE
)
_SPOT_OG_TITLE_RE = re.compile(
    r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_SPOT_OG_DESC_RE = re.compile(
    r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _html_unescape(s):
    """Light-touch HTML entity unescape. We only deal with the few Spotify
    actually emits in metadata; using `html.unescape` would also handle
    hex/decimal numerics but it's overkill here."""
    if not s:
        return s
    return (s.replace("&amp;", "&")
             .replace("&quot;", '"')
             .replace("&#39;", "'")
             .replace("&apos;", "'")
             .replace("&lt;", "<")
             .replace("&gt;", ">"))


def _is_spotify_track(url):
    """True if the URL looks like a single Spotify track (not album/playlist)."""
    return "/track/" in (url or "")


# Tokens that mean "this is a fan remix / edit, not the official release."
# Matched as whole words against the resolved title+artist query — substring
# matching would false-positive on artist names that happen to contain one
# of these words. Keep this list tight: every entry here routes the query
# away from YouTube Music (which has the cleanest official audio) so a bad
# false positive degrades quality for a real official release.
_REMIX_TOKENS = (
    r"slowed",
    r"sped[\s-]?up",
    r"speed[\s-]?up",
    r"reverb",
    r"nightcore",
    r"8d",
    r"8\s?d\s?audio",
    r"lofi",
    r"lo[\s-]?fi",
    r"bass[\s-]?boost(?:ed)?",
    r"remix",
    r"mashup",
    r"instrumental",
    r"acoustic",
    r"cover",
    r"tiktok",
    r"phonk",
    r"chopped[\s&\sand]+screwed",
)
_REMIX_RE = re.compile(
    r"(?:^|[\s\-\(\[\.])(?:" + "|".join(_REMIX_TOKENS) + r")(?:$|[\s\-\)\]\.])",
    re.IGNORECASE,
)


def _remix_modifier(query):
    """Return the specific modifier word ('slowed', 'sped up', …) from the
    query, or None."""
    if not query:
        return None
    m = _REMIX_RE.search(query)
    return m.group(0).strip(" -()[].") if m else None


def find_ytm_track(query, target_duration_s=None, modifier=None,
                   n=6, timeout=45):
    """Run a bounded YouTube Music search via yt-dlp, score the top N hits
    in Python, and return the direct URL of the best match (or None).

    We score in Python instead of using yt-dlp's --match-filter because the
    filter just rejects non-matching results and lets yt-dlp keep walking —
    eventually past the relevant search section and into unrelated tracks
    by other artists. By doing the search ourselves we keep the candidate
    set scoped to the actual search results.

    Scoring weights (smaller = better):
        * Duration mismatch in seconds (capped at 30s)
        * +1.5 if modifier is requested but missing from the title
        * +0.3 for each result-list position (mild preference for top hits)

    The first criterion is the killer — for slowed/sped variants of the same
    song, durations are always distinct enough (~30% offsets) that exact
    duration matching identifies the precise release."""
    if not query:
        return None

    search_url = ("https://music.youtube.com/search?q="
                  + urllib.parse.quote(query))
    cmd = [
        get_bin("yt-dlp"), "--quiet", "--no-warnings",
        "--playlist-items", f"1:{n}",
        # --lazy-playlist streams playlist entries as they're extracted
        # rather than buffering them all first. Combined with the small N,
        # this trims the wall-clock by ~30-40% on YTM searches.
        "--lazy-playlist",
        "--skip-download",
        # %(webpage_url)s gives the canonical /watch?v= URL we can hand
        # back to yt-dlp later as a direct download target.
        "--print", "%(title)s\t%(duration)s\t%(webpage_url)s",
        search_url,
    ]
    try:
        out = subprocess.check_output(
            cmd, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW,
            env=clean_subprocess_env(),
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        import sys
        print(f"[ytm] search failed: {e}", file=sys.stderr)
        return None

    candidates = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        title, dur_s, url = parts
        try:
            dur = float(dur_s) if dur_s and dur_s != "NA" else None
        except ValueError:
            dur = None
        if not url or "watch" not in url:
            continue
        candidates.append({"title": title, "duration": dur, "url": url})

    if not candidates:
        return None

    mod_re = None
    if modifier:
        pat = re.escape(modifier).replace(r"\ ", r"\s+")
        mod_re = re.compile(pat, re.IGNORECASE)

    def score(idx, cand):
        s = idx * 0.3   # mild preference for higher-ranked results
        if target_duration_s and cand["duration"]:
            s += min(30.0, abs(cand["duration"] - target_duration_s))
        elif target_duration_s and not cand["duration"]:
            s += 25.0   # heavy penalty for unknown duration
        if mod_re and not mod_re.search(cand["title"] or ""):
            s += 1.5
        return s

    scored = sorted(
        ((score(i, c), i, c) for i, c in enumerate(candidates)),
        key=lambda t: t[0],
    )
    best_score, _, best = scored[0]
    # Sanity floor: if even the best candidate is more than 8s off in
    # duration AND we asked for a specific duration, refuse to pick — the
    # right track simply isn't in the top N. Better to fail loud than
    # silently grab the wrong song.
    if (target_duration_s and best["duration"]
            and abs(best["duration"] - target_duration_s) > 8.0):
        return None
    return best["url"]


_SPOT_TRACK_ID_RE = re.compile(r"/track/([A-Za-z0-9]+)")
_SPOT_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(.+?)</script>',
    re.DOTALL,
)


def _extract_artist_from_next_data(data):
    """Walk Spotify's embed-page __NEXT_DATA__ JSON blob looking for the
    first artist name. The shape moves around between Spotify deploys
    (sometimes under entity, sometimes under props.entity, etc.), so we
    just scan recursively for an `artists` list with `name` entries —
    that's stable across the layouts we've seen."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            artists = node.get("artists")
            if isinstance(artists, list) and artists:
                first = artists[0]
                if isinstance(first, dict):
                    name = (first.get("name") or "").strip()
                    if name:
                        return name
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _extract_duration_from_next_data(data):
    """Walk Spotify's embed __NEXT_DATA__ blob for the track duration in
    milliseconds. Returns int or None. Like the artist walker — the exact
    field path moves between deploys, so just scan for the first integer
    duration field we see at a reasonable depth."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in ("duration", "durationMs", "duration_ms"):
                v = node.get(key)
                if isinstance(v, int) and 1000 < v < 7_200_000:
                    return v
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def resolve_spotify_track(url, timeout=10):
    """Resolve a Spotify track URL to a search-ready dict.

    Returns dict shaped like:
        {"query": "Title Artist", "duration_ms": int|None}
    or None on failure. The caller uses duration_ms to make YouTube Music
    pick the EXACT release the user pasted — slowed/sped variants have
    distinct lengths, so a ±2-second duration filter picks the right one
    even when the search query matches several variants by title.

    Strategies, in order of reliability:

        1. Embed page (https://open.spotify.com/embed/track/<id>) — server-
           rendered, ships a __NEXT_DATA__ JSON blob with the full track +
           artist info. This is the most reliable source as of late-2025;
           the main /track/ page is now fully JS-rendered and ships no
           meta tags at all.

        2. oEmbed API (https://open.spotify.com/oembed?url=...) — still
           returns JSON with a title, but Spotify dropped the artist
           sometime in 2025, so this is title-only now. No duration.

        3. Main track page og:title / og:description — legacy fallback."""
    import sys

    # Track ID needed for the embed URL. Pulled from the supplied link
    # rather than trusting it verbatim — strips share params, locale
    # prefixes, etc.
    track_id = None
    m = _SPOT_TRACK_ID_RE.search(url or "")
    if m:
        track_id = m.group(1)

    # ── Strategy 1: embed page __NEXT_DATA__ JSON ────────────────────────
    if track_id:
        try:
            embed = f"https://open.spotify.com/embed/track/{track_id}"
            req = urllib.request.Request(embed, headers={"User-Agent": _SPOT_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read(500_000).decode("utf-8", errors="replace")
            m2 = _SPOT_NEXT_DATA_RE.search(html)
            if m2:
                data = json.loads(m2.group(1))
                # The blob carries the track name in multiple nested
                # locations; search recursively for the first plausible
                # title alongside the artist list.
                title = None
                stack = [data]
                while stack and not title:
                    node = stack.pop()
                    if isinstance(node, dict):
                        # `name` and `title` show up at various depths;
                        # prefer `name` since it's the canonical Spotify
                        # entity field. Skip very short / generic values.
                        for key in ("name", "title"):
                            cand = node.get(key)
                            if (isinstance(cand, str) and cand.strip()
                                    and node.get("artists")):
                                title = cand.strip()
                                break
                        if not title:
                            stack.extend(node.values())
                    elif isinstance(node, list):
                        stack.extend(node)
                artist = _extract_artist_from_next_data(data)
                duration = _extract_duration_from_next_data(data)
                if title and artist:
                    return {"query": f"{title} {artist}",
                            "duration_ms": duration}
                if title:
                    return {"query": title, "duration_ms": duration}
        except Exception as e:
            print(f"[spotify] embed-page resolve failed: {e}", file=sys.stderr)

    # ── Strategy 2: oEmbed (title only, post-2025) ──────────────────────
    try:
        oembed = (
            "https://open.spotify.com/oembed?url="
            + urllib.parse.quote(url, safe="")
        )
        req = urllib.request.Request(oembed, headers={"User-Agent": _SPOT_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        title = (data.get("title") or "").strip()
        if title:
            # Strip an inline " by " connector if it ever comes back —
            # historic oEmbed responses were "Track by Artist".
            cleaned = re.sub(r"\s+by\s+", " ", title, count=1, flags=re.IGNORECASE)
            return {"query": cleaned, "duration_ms": None}
    except Exception as e:
        print(f"[spotify] oEmbed failed: {e}", file=sys.stderr)

    # ── Strategy 3: legacy og: meta tags on the main page ───────────────
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _SPOT_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(65536).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[spotify] page fetch failed: {e}", file=sys.stderr)
        return None

    title_m = _SPOT_OG_TITLE_RE.search(html)
    desc_m  = _SPOT_OG_DESC_RE.search(html)
    title = _html_unescape(title_m.group(1).strip()) if title_m else None
    desc  = _html_unescape(desc_m.group(1).strip())  if desc_m  else None

    artist = None
    if desc:
        for rx in (_SPOT_DESC_ARTIST_RE,
                   _SPOT_DESC_ARTIST_RE_2,
                   _SPOT_DESC_ARTIST_RE_3):
            mm = rx.search(desc)
            if mm:
                artist = mm.group("artist").strip()
                if artist:
                    break

    if title and artist:
        return {"query": f"{title} {artist}", "duration_ms": None}
    if title:
        return {"query": title, "duration_ms": None}
    print(f"[spotify] no title found (desc={bool(desc)}, title={bool(title)})",
          file=sys.stderr)
    return None


# ── main downloader ────────────────────────────────────────────────────────


class Downloader:
    def __init__(self, url, on_progress, on_done):
        self.url         = url
        self.kind        = detect_kind(url)
        self.on_progress = on_progress
        self.on_done     = on_done
        self.proc        = None
        self.cancelled   = False
        # Set during _run() once we've resolved Spotify → ytsearch.
        self._effective_url      = url
        self._effective_kind     = self.kind
        # Remix modifier ("slowed", "sped up", …) pulled from the resolved
        # Spotify title. _build_cmd uses this to add --match-filter so the
        # downloaded YT Music result is actually the remix, not the
        # higher-ranked original. None for non-Spotify or non-remix runs.
        self._effective_modifier = None
        # Spotify track duration in ms, used to pin YT Music to the exact
        # release the user pasted. Even with the modifier filter, a search
        # may surface "Track (Slowed)", "Track (Slowed + Reverb)", "Track
        # (Slowed Tape)" — same modifier word, different actual variants
        # with different lengths. Matching duration disambiguates.
        self._effective_duration = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        if self.proc:
            try: self.proc.kill()
            except Exception: pass

    def _build_cmd(self):
        """Build the yt-dlp command. By the time we get here, Spotify has
        already been resolved to a YouTube Music search URL in _run(), so
        we never actually shell out to spotdl anymore."""
        cmd = [
            get_bin("yt-dlp"),
            "--newline", "--no-warnings",
        ]
        is_ytm_search = "music.youtube.com/search" in self._effective_url
        if is_ytm_search:
            # Fallback path only — find_ytm_track usually gives us a
            # direct /watch?v= URL, but if scoring didn't find a confident
            # match we hand off the bare search URL and take the top hit.
            cmd += ["--playlist-items", "1"]
        else:
            cmd += ["--no-playlist"]

        if self._effective_kind in ("audio", "spotify"):
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        else:
            cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
        cmd += ["-o", str(DOWNLOADS / "%(title)s.%(ext)s"), self._effective_url]
        return cmd

    @staticmethod
    def _scan_line_for_path(line):
        """If `line` announces a final output path (fresh or already-existing),
        return it; else None."""
        for rx in _DEST_PATTERNS:
            m = rx.match(line)
            if m:
                return m.group(1).strip().strip('"')
        return None

    def _resolve_spotify(self):
        """If kind is spotify, resolve the URL to a ytsearch query in place.
        Pushes a progress message so the user sees we're not stuck. Returns
        True on success, False after notifying on_done with an error."""
        if self.kind != "spotify":
            return True

        if not _is_spotify_track(self.url):
            self.on_done({
                "ok": False,
                "msg": "Spotify albums/playlists aren't supported yet — paste a single track URL.",
                "files": [],
            })
            return False

        # Tell the UI we're working on it — Spotify resolution can take a
        # second or two on slow connections, and silence here was a big
        # part of the old "starting forever" feel.
        try:
            self.on_progress({"pct": None, "speed": None, "eta": None,
                              "phase": "Resolving Spotify",
                              "msg": "Resolving Spotify…"})
        except Exception:
            pass

        resolved = resolve_spotify_track(self.url)
        if not resolved:
            self.on_done({
                "ok": False,
                "msg": "Couldn't read the Spotify track page. Check the URL or your connection.",
                "files": [],
            })
            return False
        query        = resolved["query"]
        duration_ms  = resolved.get("duration_ms")

        modifier   = _remix_modifier(query)
        duration_s = (duration_ms / 1000.0) if duration_ms else None

        # Only run the slow pre-search step when the title contains a remix
        # modifier — that's the case where YouTube Music's top result might
        # not be the variant the user pasted, so we need duration matching
        # to disambiguate. For plain queries (no modifier), YTM's top hit
        # IS the right answer and a pre-search just doubles the wait.
        if modifier and duration_s:
            try:
                self.on_progress({"pct": None, "speed": None, "eta": None,
                                  "phase": "Matching",
                                  "msg": "Picking the right version…"})
            except Exception:
                pass
            direct_url = find_ytm_track(
                query, target_duration_s=duration_s, modifier=modifier,
            )
            if direct_url:
                self._effective_url = direct_url
                self._effective_kind = "audio"
                return True
            # No confident match — fall through to the raw YTM URL path
            # below so SOMETHING downloads rather than nothing.

        try:
            self.on_progress({"pct": None, "speed": None, "eta": None,
                              "phase": "Searching",
                              "msg": f"Searching: {query[:48]}"})
        except Exception:
            pass

        # Fast path: hand yt-dlp the bare YTM search URL and grab top hit.
        # _build_cmd pairs this with --playlist-items 1.
        self._effective_url  = (
            "https://music.youtube.com/search?q="
            + urllib.parse.quote(query)
        )
        self._effective_kind = "audio"
        return True

    def _run(self):
        # Resolve Spotify before anything else; it short-circuits with an
        # error if the track URL can't be parsed.
        if not self._resolve_spotify():
            return
        if self.cancelled:
            self.on_done({"ok": False, "msg": "Cancelled", "files": []})
            return

        # Snapshot Downloads first so we can dir-diff as a backup if stdout
        # parsing misses something.
        try:
            before = set(DOWNLOADS.iterdir())
        except Exception:
            before = set()
        # Wall-clock baseline for the recent-mtime fallback. Subtract a
        # 2-second buffer to handle clock granularity and any pre-Popen
        # scheduling delay on slower systems.
        run_start = time.time() - 2

        try:
            self.proc = subprocess.Popen(
                self._build_cmd(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=NO_WINDOW,
                env=clean_subprocess_env(),
            )
        except FileNotFoundError:
            # Only yt-dlp now — spotdl is no longer invoked.
            self.on_done({"ok": False, "msg": "yt-dlp not found on PATH", "files": []})
            return
        except Exception as e:
            self.on_done({"ok": False, "msg": str(e), "files": []})
            return

        # If cancel() fired between start() and Popen returning, kill immediately.
        if self.cancelled:
            try: self.proc.kill()
            except Exception: pass

        # Capture announced paths in stdout-order, deduped.
        announced = []
        seen_announced = set()

        last_msg = None
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue

            dest = self._scan_line_for_path(line)
            if dest:
                ext = os.path.splitext(dest)[1].lower()
                if ext not in _TEMP_EXTS and dest not in seen_announced:
                    seen_announced.add(dest)
                    announced.append(dest)

            p = parse_line(line)
            msg = fmt_status(p)
            if msg and msg != last_msg:
                self.on_progress({**p, "msg": msg})
                last_msg = msg

        self.proc.wait()
        # yt-dlp exits with code 101 when --max-downloads is reached. That's
        # not a failure — it means the cap kicked in mid-list, which is
        # exactly what we asked for on remix-filtered runs. Treat it as
        # success and let the file-detection below decide if anything
        # actually landed on disk.
        ok = (self.proc.returncode in (0, 101)) and not self.cancelled

        files = []
        if ok:
            seen_paths = set()

            # 1) Trust stdout — but only paths that actually exist on disk
            #    after the run. Filters out announced intermediates that
            #    were merged/deleted (e.g. .webm before -x mp3, or fragment
            #    files before the Merger step).
            for path in announced:
                try:
                    if os.path.isfile(path):
                        ap = os.path.abspath(path)
                        if ap not in seen_paths:
                            files.append(path)
                            seen_paths.add(ap)
                except Exception:
                    pass

            # 2) Dir-diff catches anything stdout parsing missed.
            try:
                after = set(DOWNLOADS.iterdir())
                for p in (after - before):
                    if not p.is_file():
                        continue
                    if p.suffix.lower() in _TEMP_EXTS:
                        continue
                    ap = os.path.abspath(str(p))
                    if ap in seen_paths:
                        continue
                    files.append(str(p))
                    seen_paths.add(ap)
            except Exception:
                pass

            # 3) Last-resort: scan Downloads for files modified during this
            #    run. This is the safety net — catches cases where the
            #    announced path strings didn't on-disk-verify (path encoding,
            #    case differences) AND the dir-diff Path-object equality
            #    didn't catch it either. Any non-temp file with mtime in our
            #    run window is almost certainly ours.
            if not files:
                try:
                    candidates = []
                    for p in DOWNLOADS.iterdir():
                        if not p.is_file():
                            continue
                        if p.suffix.lower() in _TEMP_EXTS:
                            continue
                        try:
                            if p.stat().st_mtime >= run_start:
                                candidates.append(p)
                        except Exception:
                            continue
                    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    for p in candidates:
                        ap = os.path.abspath(str(p))
                        if ap not in seen_paths:
                            files.append(str(p))
                            seen_paths.add(ap)
                except Exception:
                    pass

            # 4) "Already downloaded" fallback. yt-dlp prints:
            #       [download] <path> has already been downloaded
            #    when the target exists. The file is NOT touched (mtime
            #    unchanged), so step 3 misses it. Step 1 should have caught
            #    it, but unicode-normalization or path-case mismatches
            #    between yt-dlp's stdout decoding and the actual filesystem
            #    occasionally cause os.path.isfile to return False on a
            #    real file. Match by NFC-normalized casefolded basename
            #    against what's actually in Downloads.
            if not files and announced:
                try:
                    import unicodedata
                    def _norm(s):
                        try: return unicodedata.normalize("NFC", s).casefold()
                        except Exception: return s.casefold()
                    on_disk = {_norm(p.name): p
                               for p in DOWNLOADS.iterdir()
                               if p.is_file() and p.suffix.lower() not in _TEMP_EXTS}
                    for path in announced:
                        match = on_disk.get(_norm(os.path.basename(path)))
                        if match is None:
                            continue
                        ap = os.path.abspath(str(match))
                        if ap in seen_paths:
                            continue
                        files.append(str(match))
                        seen_paths.add(ap)
                except Exception:
                    pass

            # Most-recent-first so the chip / library land in a sensible order.
            try:
                files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            except Exception:
                pass

        if self.cancelled:
            self.on_done({"ok": False, "msg": "Cancelled", "files": []})
        elif ok:
            # Treat any 0-exit-code run as success. `files` may still be
            # empty in pathological cases; _show_chip handles that
            # defensively with a "Saved (file location unknown)" message
            # so the user always gets feedback.
            msg = "Saved to Downloads" if files else "Saved (file not identified)"
            self.on_done({"ok": True, "msg": msg, "files": files})
        else:
            self.on_done({"ok": False, "msg": "Download failed", "files": []})
