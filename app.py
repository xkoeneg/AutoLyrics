"""
AutoLyrics - a lightweight floating lyrics viewer for Windows.

* Reads the current track (Title / Artist / Duration) AND the live playback
  position from the Windows System Media Transport Controls (SMTC) via
  `winsdk` in a background thread, so lyrics re-sync after seek / pause.
* Lyrics come from a 3-layer fallback chain:
    1. YouTube Closed Captions (browser sources only, via yt-dlp)
    2. Synced (.lrc) lyrics from the free LRCLIB API
    3. Plain-text lyrics from LRCLIB
* -0.5s / +0.5s buttons nudge the timing live (remembered per track).
* Shows them in a frameless, resizable, dark Fluent-style PyQt6 window.
* Lives in the system tray (pystray): click toggles the window.
"""
from __future__ import annotations

import asyncio
import bisect
import ctypes
import gc
import json
import operator
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import reduce
from pathlib import Path

import pystray
import requests
from PIL import Image, ImageDraw
from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSettings,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

try:  # Windows only
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
except Exception:  # pragma: no cover
    MediaManager = None
    PlaybackStatus = None

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

try:
    import yt_dlp
except Exception:  # pragma: no cover - CC layer simply gets skipped
    yt_dlp = None

APP_NAME = "AutoLyrics"
APP_VERSION = "1.3.6"
LRCLIB_API = "https://lrclib.net/api"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (https://github.com/your-name/AutoLyrics)"

POLL_INTERVAL = 0.25     # seconds between SMTC polls
LYRIC_LEAD = 0.25        # highlight a line slightly early (seconds)
USER_SCROLL_PAUSE = 4.0  # seconds auto-scroll pauses after manual scrolling
SYNC_TOLERANCE = 0.6     # s; smaller position differences are just smoothed out
SYNC_SMOOTH = 0.3        # 0..1: how quickly small drift is corrected
SEEK_CONFIRM = 1.5       # s a big position jump must keep making sense before we obey
AD_MAX = 90.0            # s; after this a 'different duration' is treated as the new normal
OFFSET_STEP = 0.5        # manual timing nudge (s)
OFFSET_LIMIT = 30.0

# AI transcription (last resort, YouTube sources only)
AI_MODEL = "small"        # "base" = lighter (~150MB) but weaker at Tagalog
AI_THREADS = 2            # CPU threads used while transcribing
AI_MAX_SECONDS = 900      # skip videos longer than this (15 min)
AI_LANGUAGE = None        # None = auto-detect; or force e.g. "tl" (Tagalog), "en"

# YouTube Closed Captions layer
ALLOW_AUTO_CC = True     # last-resort synced source: YouTube auto-generated captions
# Caption language order when a video has several tracks (first match wins).
# Filipino first: English tracks on OPM videos are usually translations.
CC_LANG_PREF = ("tl", "fil", "en")
CC_DURATION_TOLERANCE = 3.0   # s; search hit must match the playing video length
# Players that can never be YouTube. Everything else (any browser, PWA, Firefox's
# hash-style app ids, YouTube Music desktop apps...) is checked against YouTube.
NON_YOUTUBE_HINTS = ("spotify", "applemusic", "itunes", "tidal", "deezer", "amazonmusic",
                     "amazon music", "zunemusic", "groove", "foobar", "musicbee",
                     "vlc", "wmplayer", "aimp", "winamp")

# ----------------------------------------------------------------------------
# Look & feel
# ----------------------------------------------------------------------------
MARGIN = 8               # transparent outer margin = resize grip zone
RADIUS = 12
BG_COLOR = QColor(28, 28, 32, 246)
BORDER_COLOR = QColor(255, 255, 255, 28)
ACCENT = "#60cdff"

LINE_DIM = ("background:transparent;color:rgba(255,255,255,105);"
            "font-size:18px;font-weight:500;padding:6px 10px;")
LINE_ACTIVE = ("background:transparent;color:#ffffff;"
               "font-size:18px;font-weight:700;padding:6px 10px;")
LINE_PLAIN = ("background:transparent;color:rgba(255,255,255,215);"
              "font-size:16px;font-weight:500;padding:3px 10px;")


# ----------------------------------------------------------------------------
# Lyrics model, LRC parsing, title cleanup
# ----------------------------------------------------------------------------
@dataclass
class Lyrics:
    synced: list[tuple[float, str]] = field(default_factory=list)
    plain: str = ""
    instrumental: bool = False
    source: str = ""            # "YouTube CC", "LRCLIB · synced", "LRCLIB · plain"

    def to_json(self) -> dict:
        return {"synced": self.synced, "plain": self.plain,
                "instrumental": self.instrumental, "source": self.source}

    @staticmethod
    def from_json(d: dict) -> "Lyrics":
        return Lyrics([(float(t), s) for t, s in d.get("synced", [])],
                      d.get("plain", ""), bool(d.get("instrumental", False)),
                      d.get("source", ""))


_TS = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")


def parse_lrc(text: str | None) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        lyric = raw[stamps[-1].end():].strip()
        for m in stamps:
            secs = int(m.group(1)) * 60 + float(m.group(2).replace(":", "."))
            out.append((secs, lyric))
    out.sort(key=lambda x: x[0])
    return out


_NOISE = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*\b(official|lyrics?|lyric video|music video|video|"
    r"audio|visuali[sz]er|hd|hq|4k|remaster(?:ed)?|explicit|mv)\b[^\)\]\}]*[\)\]\}]",
    re.I)
_FEAT = re.compile(
    r"\s*[\(\[]\s*(?:feat|ft|featuring)\.?\s[^\)\]]*[\)\]]|\s+(?:feat|ft)\.?\s.*$", re.I)
_TRAILING = re.compile(r"\s*[|｜].*$")


def clean_title(t: str) -> str:
    t = _NOISE.sub("", t)
    t = _FEAT.sub("", t)
    t = _TRAILING.sub("", t)
    return " ".join(t.split()).strip(" -–—")


def clean_artist(a: str) -> str:
    a = re.sub(r"\s*-\s*Topic$", "", a, flags=re.I)
    a = re.sub(r"\s*VEVO$", "", a, flags=re.I)
    a = re.sub(r"\s+(official|music)$", "", a, flags=re.I)
    return " ".join(a.split())


def build_candidates(title: str, artist: str) -> list[tuple[str, str]]:
    """(track, artist) variants to try, most literal first."""
    out: list[tuple[str, str]] = []

    def add(t: str, a: str) -> None:
        t, a = t.strip(), a.strip()
        if t and (t, a) not in out:
            out.append((t, a))

    ct, ca = clean_title(title), clean_artist(artist)
    add(title, artist)
    add(ct, ca)
    parts = re.split(r"\s[-–—]\s", ct, maxsplit=1)   # "Artist - Song" on YouTube
    if len(parts) == 2:
        add(parts[1], parts[0])
    return out


# ----------------------------------------------------------------------------
# LRCLIB client
# ----------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def _api_get(path: str, params: dict):
    r = _session.get(f"{LRCLIB_API}{path}", params=params, timeout=8)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _to_lyrics(data: dict | None) -> Lyrics | None:
    if not data:
        return None
    synced = parse_lrc(data.get("syncedLyrics"))
    plain = (data.get("plainLyrics") or "").strip()
    instrumental = bool(data.get("instrumental"))
    if not (synced or plain or instrumental):
        return None
    source = "LRCLIB · synced" if synced else "LRCLIB · plain"
    return Lyrics(synced, plain, instrumental, source)


def _pick_best(results: list, duration: float) -> dict | None:
    if not isinstance(results, list):
        return None
    scored = []
    for r in results:
        diff = abs((r.get("duration") or 0) - duration) if duration > 0 else 0
        if duration > 0 and diff > 10:      # almost certainly a different recording
            continue
        has_text = bool(r.get("syncedLyrics") or r.get("plainLyrics") or r.get("instrumental"))
        if not has_text:
            continue
        scored.append((0 if r.get("syncedLyrics") else 1, diff, r))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


def fetch_lyrics(title: str, artist: str, album: str, duration: float
                 ) -> tuple[Lyrics | None, bool]:
    """Returns (lyrics_or_None, network_failed)."""
    completed_any = False
    failed_any = False

    def call(path: str, params: dict):
        nonlocal completed_any, failed_any
        try:
            data = _api_get(path, params)
            completed_any = True
            return data
        except (requests.RequestException, ValueError):
            failed_any = True
            return None

    cands = build_candidates(title, artist)

    # 1) exact signature lookups
    for i, (t, a) in enumerate(cands):
        if not a:
            continue
        params = {"track_name": t, "artist_name": a}
        if i == 0 and album:
            params["album_name"] = album
        if duration > 0:
            params["duration"] = int(round(duration))
        lyr = _to_lyrics(call("/get", params))
        if lyr:
            return lyr, False

    # 2) fuzzy search
    for t, a in cands[:3]:
        q = f"{a} {t}".strip()
        best = _pick_best(call("/search", {"q": q}) or [], duration)
        lyr = _to_lyrics(best)
        if lyr:
            return lyr, False

    t, a = cands[-1] if cands else (title, artist)
    params = {"track_name": t}
    if a:
        params["artist_name"] = a
    lyr = _to_lyrics(_pick_best(call("/search", params) or [], duration))
    if lyr:
        return lyr, False

    return None, (failed_any and not completed_any)


# ----------------------------------------------------------------------------
# Layer 1: YouTube Closed Captions (browser sources)
# ----------------------------------------------------------------------------
_STOP_WORDS = {"official", "video", "lyrics", "lyric", "audio", "music", "hd", "hq",
               "mv", "the", "feat", "ft", "visualizer"}


def may_be_youtube(app_id: str) -> bool:
    a = (app_id or "").lower()
    return not any(h in a for h in NON_YOUTUBE_HINTS)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower())
            if len(w) > 1 and w not in _STOP_WORDS}


def _find_youtube_video(title: str, artist: str, duration: float) -> str | None:
    """Search YouTube and return the video id that matches the playing media.

    SMTC does not expose the page URL, so we identify the video by title
    overlap + (when known) matching duration."""
    ct, ca = clean_title(title), clean_artist(artist)
    want = _tokens(ct) or _tokens(title)
    if not want:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": True, "socket_timeout": 8}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch5:{ca} {ct}".strip(), download=False)

    best: tuple[float, float, str] | None = None
    for e in (res or {}).get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        overlap = len(want & _tokens(e.get("title", ""))) / len(want)
        if overlap < 0.5:
            continue
        vdur = float(e.get("duration") or 0)
        diff = abs(vdur - duration) if (duration > 0 and vdur > 0) else 0.0
        if duration > 0 and vdur > 0 and diff > CC_DURATION_TOLERANCE:
            continue                      # different upload (e.g. lyric video / cover)
        if duration <= 0 and overlap < 0.8:
            continue                      # no duration to verify -> be stricter
        cand = (diff, -overlap, vid)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


def _log_path() -> Path:
    return Path(os.getenv("APPDATA") or Path.home()) / APP_NAME / "debug.log"


def _open_log() -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        os.startfile(str(path))                   # type: ignore[attr-defined]
    except Exception:
        pass


def _log(msg: str) -> None:
    """Append to %APPDATA%/AutoLyrics/debug.log (helps when lyrics are not found)."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 200_000:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass


def _base_lang(code: str) -> str:
    c = (code or "").lower().replace("_", "-").split("-")[0]
    return "tl" if c == "fil" else c


def _pick_caption_track(pool: dict, strict: bool = False):
    """Choose a json3 caption track -> (language_key, format) or None.
    strict=True (auto captions) only accepts the original-language '*-orig' track,
    never machine translations."""
    keys = [k for k in pool if not k.startswith("live_chat")]
    if strict:
        ordered = [k for k in keys if k.endswith("-orig")]
    else:
        ordered = [k for pref in CC_LANG_PREF for k in keys
                   if _base_lang(k) == _base_lang(pref)] + keys
    for k in ordered:
        for fmt in pool.get(k) or []:
            if fmt.get("ext") == "json3" and fmt.get("url"):
                return k, fmt
    return None


def _parse_json3(data: dict, dedupe: bool = False) -> list[tuple[float, str]]:
    cues: list[tuple[float, float, str]] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        text = " ".join("".join(sg.get("utf8", "") for sg in segs).split())
        if not text:
            continue
        if re.fullmatch(r"[\[\(].*[\]\)]", text):    # [Music], (Applause) ...
            text = ""
        start = ev["tStartMs"] / 1000.0
        cues.append((start, start + ev.get("dDurationMs", 0) / 1000.0, text))
    cues.sort(key=lambda c: c[0])

    if dedupe:                                    # auto captions repeat rolling lines
        kept: list[tuple[float, float, str]] = []
        for c in cues:
            if kept and c[2] and c[2] == kept[-1][2]:
                continue
            kept.append(c)
        cues = kept

    out: list[tuple[float, str]] = []
    for i, (start, end, text) in enumerate(cues):
        if text or not out or out[-1][1]:         # collapse consecutive blanks
            out.append((start, text))
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        if end > start and (nxt is None or nxt - end > 2.0) and out[-1][1]:
            out.append((end, ""))                 # instrumental gap -> shows a note
    return out


def fetch_youtube_cc(title: str, artist: str, duration: float, trace: list[str],
                     ctx: dict | None = None) -> tuple[Lyrics | None, Lyrics | None]:
    """Returns (manual_cc, auto_cc). auto_cc is only looked up when manual is missing."""
    if yt_dlp is None:
        trace.append("Captions: yt-dlp missing")
        return None, None
    vid = _find_youtube_video(title, artist, duration)
    if not vid:
        trace.append("Captions: no matching YouTube video")
        return None, None
    if ctx is not None:
        ctx["vid"] = vid                      # lets the AI layer reuse the matched video
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "socket_timeout": 8}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)

    def load(pool: dict, strict: bool, source: str, dedupe: bool) -> Lyrics | None:
        picked = _pick_caption_track(pool, strict)
        if not picked:
            return None
        lang, track = picked
        r = requests.get(track["url"], timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        lines = _parse_json3(r.json(), dedupe)
        if sum(1 for _, t in lines if t) >= 3:        # ignore near-empty tracks
            trace.append(f"Captions: {source} [{lang}]")
            return Lyrics(synced=lines, source=f"{source} · {lang}")
        return None

    manual = load(info.get("subtitles") or {}, False, "YouTube CC", False)
    if manual:
        return manual, None
    auto = None
    if ALLOW_AUTO_CC:
        auto = load(info.get("automatic_captions") or {}, True, "YouTube auto-CC", True)
    trace.append("Captions: no uploaded CC" + (", auto-CC found" if auto else ", no auto-CC"))
    return None, auto


def fetch_best_lyrics(title: str, artist: str, album: str, duration: float,
                      app_id: str, trace: list[str] | None = None,
                      ctx: dict | None = None) -> tuple[Lyrics | None, bool]:
    """CC -> LRCLIB synced -> auto-CC -> LRCLIB plain. Returns (lyrics, network_failed).
    `trace` collects a short human-readable reason for each layer that missed."""
    trace = trace if trace is not None else []
    cc_manual = cc_auto = None
    if may_be_youtube(app_id):
        try:
            cc_manual, cc_auto = fetch_youtube_cc(title, artist, duration, trace, ctx)
        except Exception as exc:                      # never let CC break the fallback
            trace.append(f"Captions: error ({type(exc).__name__})")
            _log(f"CC error for {artist} - {title}: {exc!r}")
        if cc_manual:
            return cc_manual, False
    else:
        trace.append(f"Captions: skipped (native player: {app_id})")

    lyr, net_fail = fetch_lyrics(title, artist, album, duration)
    if lyr and lyr.synced:
        return lyr, False
    if cc_auto:
        return cc_auto, False
    trace.append("LRCLIB: offline" if net_fail else
                 ("LRCLIB: plain text only" if lyr else "LRCLIB: not found"))
    return lyr, net_fail


# ----------------------------------------------------------------------------
# Last resort: AI transcription of the YouTube audio (faster-whisper, CPU, int8)
# ----------------------------------------------------------------------------
_ai_lock = threading.Lock()
_HALLUCINATION = re.compile(
    r"thanks for watching|please subscribe|amara\.org|subtitles? by|www\.", re.I)


def _set_low_priority(on: bool) -> None:
    """BELOW_NORMAL while transcribing so browsers / games keep priority."""
    if sys.platform != "win32":
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000 if on else 0x20)
    except Exception:
        pass


def _segments_to_lines(segs) -> list[tuple[float, str]]:
    """Whisper segments -> synced lines, dropping typical hallucinations."""
    cues: list[tuple[float, float, str]] = []
    run = 0
    for sg in segs:
        text = " ".join((getattr(sg, "text", "") or "").split())
        if not text or _HALLUCINATION.search(text):
            continue
        nsp = getattr(sg, "no_speech_prob", 0.0) or 0.0
        lp = getattr(sg, "avg_logprob", 0.0) or 0.0
        if (nsp > 0.6 and lp < -1.0) or lp < -2.0:
            continue
        run = run + 1 if cues and cues[-1][2] == text else 0
        if run >= 2:                                  # looping repetition
            continue
        cues.append((float(sg.start), float(sg.end), text))

    out: list[tuple[float, str]] = []
    for i, (start, end, text) in enumerate(cues):
        out.append((start, text))
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        if end > start and (nxt is None or nxt - end > 2.0):
            out.append((end, ""))
    return out


def transcribe_youtube(vid: str, should_cancel, on_status) -> Lyrics | None:
    """Download the audio to a temp folder, transcribe it, delete the audio."""
    if yt_dlp is None:
        return None
    from faster_whisper import WhisperModel            # lazy: zero cost until needed

    models_dir = Path(os.getenv("APPDATA") or Path.home()) / APP_NAME / "models"
    with tempfile.TemporaryDirectory(prefix="autolyrics_") as tmp:
        on_status("Downloading audio…", "AI lyrics (no captions or lyrics found)")
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "socket_timeout": 10,
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": os.path.join(tmp, "audio.%(ext)s")}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={vid}"])
        files = sorted(Path(tmp).glob("audio.*"))
        if not files or should_cancel():
            return None

        first_run = not models_dir.exists() or not any(models_dir.iterdir())
        on_status("Loading AI model…",
                  "First time only: downloading ~500MB model" if first_run
                  else "AI lyrics (no captions or lyrics found)")
        model = WhisperModel(AI_MODEL, device="cpu", compute_type="int8",
                             cpu_threads=AI_THREADS, download_root=str(models_dir))
        try:
            if should_cancel():
                return None
            segments, info = model.transcribe(
                str(files[0]), language=AI_LANGUAGE, beam_size=1, vad_filter=True,
                condition_on_previous_text=False, word_timestamps=False)
            total = float(getattr(info, "duration", 0) or 0)
            collected = []
            for sg in segments:                       # generator: work happens here
                if should_cancel():
                    return None
                collected.append(sg)
                if total > 0:
                    on_status(f"Transcribing… {min(99, int(sg.end / total * 100))}%",
                              "AI lyrics - may contain errors")
        finally:
            del model
            gc.collect()                              # give the ~1GB RAM back
    lines = _segments_to_lines(collected)
    if sum(1 for _, t in lines if t) < 3:
        return None
    return Lyrics(synced=lines, source="AI-generated · may have errors")


class LyricsCache:
    """Tiny JSON cache in %APPDATA%/AutoLyrics so replays are instant/offline."""
    MAX = 300

    def __init__(self) -> None:
        base = Path(os.getenv("APPDATA") or Path.home()) / APP_NAME
        self.path = base / "cache.json"
        self.data: dict[str, dict] = {}
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            pass

    @staticmethod
    def key(title: str, artist: str) -> str:
        return f"{artist.strip().lower()}|{title.strip().lower()}"

    def get(self, title: str, artist: str) -> Lyrics | None:
        d = self.data.get(self.key(title, artist))
        return Lyrics.from_json(d) if d else None

    def put(self, title: str, artist: str, lyr: Lyrics) -> None:
        k = self.key(title, artist)
        self.data.pop(k, None)
        self.data[k] = lyr.to_json()
        while len(self.data) > self.MAX:
            self.data.pop(next(iter(self.data)))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data), encoding="utf-8")
        except Exception:
            pass


# ----------------------------------------------------------------------------
# SMTC watcher (background thread + asyncio)
# ----------------------------------------------------------------------------
class MediaWatcher(QObject):
    track_changed = pyqtSignal(str, str, str, float, str)   # title, artist, album, duration, app_id
    no_media = pyqtSignal()
    playback = pyqtSignal(float, bool, float, float)   # position, is_playing, duration, age
    unavailable = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sticky = ""            # app id of the session we are following
        self._last_raw: tuple[float, float, str] | None = None   # for jump logging

    def start(self) -> None:
        if MediaManager is None:
            self.unavailable.emit()
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._loop())
        except Exception as exc:  # pragma: no cover
            print("MediaWatcher stopped:", exc, file=sys.stderr)

    def _pick_session(self, manager):
        """Stick to one session so two players never make the lyrics flip-flop."""
        sessions = list(manager.get_sessions())

        def is_playing(sess) -> bool:
            try:
                return sess.get_playback_info().playback_status == PlaybackStatus.PLAYING
            except Exception:
                return False

        mine = [x for x in sessions if x.source_app_user_model_id == self._sticky]
        if mine and is_playing(mine[0]):
            return mine[0]
        for sess in sessions:                    # switch only to something that is playing
            if is_playing(sess):
                self._sticky = sess.source_app_user_model_id
                return sess
        if mine:                                 # nothing playing: stay on the paused one
            return mine[0]
        cur = manager.get_current_session()
        if cur is not None:
            self._sticky = cur.source_app_user_model_id
        return cur

    async def _snapshot(self, manager):
        session = self._pick_session(manager)
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        title = (props.title or "").strip()
        if not title:
            return None
        artist = (props.artist or "").strip()
        album = (props.album_title or "").strip()
        info = session.get_playback_info()
        playing = info.playback_status == PlaybackStatus.PLAYING
        app_id = session.source_app_user_model_id or ""
        try:
            rate = float(info.playback_rate or 1.0)
        except Exception:
            rate = 1.0

        position, duration, age = 0.0, 0.0, 0.0
        try:
            tl = session.get_timeline_properties()
            position = tl.position.total_seconds()
            raw = position
            duration = max(0.0, tl.end_time.total_seconds() - tl.start_time.total_seconds())
            if tl.last_updated_time:
                updated = tl.last_updated_time
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
                if playing:
                    position += age * rate
            if duration > 0:
                position = min(position, duration)
            self._note_jump(title, raw, position, duration, age, playing)
        except Exception:
            pass
        return title, artist, album, duration, position, playing, app_id, age

    def _note_jump(self, title: str, raw: float, position: float, duration: float,
                   age: float, playing: bool) -> None:
        """Write unexpected SMTC position jumps to debug.log (diagnostics only)."""
        now = time.monotonic()
        prev, self._last_raw = self._last_raw, (now, position, title)
        if prev and prev[2] == title:
            expected = prev[1] + ((now - prev[0]) if playing else 0.0)
            if abs(position - expected) > 2.0:
                _log(f"smtc jump: {expected:.1f}s -> {position:.1f}s "
                     f"(raw {raw:.1f}s, age {age:.1f}s, dur {duration:.0f}s, "
                     f"playing={playing})")

    async def _loop(self) -> None:
        manager = None
        last_key: tuple[str, str] | None = None
        had_media = False
        cand_key: tuple[str, str] | None = None   # a new track must stay stable 2 polls
        cand_n = 0
        miss = 0                                  # consecutive polls without media
        while not self._stop.is_set():
            try:
                if manager is None:
                    manager = await MediaManager.request_async()
                snap = await self._snapshot(manager)
                if snap is None:
                    miss += 1
                    if had_media and miss >= 4:           # ignore ~1s metadata hiccups
                        had_media, last_key, cand_key, cand_n = False, None, None, 0
                        self.no_media.emit()
                else:
                    miss = 0
                    title, artist, album, duration, pos, playing, app_id, age = snap
                    key = (title, artist)
                    if key != last_key:
                        cand_n = cand_n + 1 if key == cand_key else 1
                        cand_key = key
                        # stable for 2 polls; wait up to ~1.5s for the duration to appear
                        if cand_n >= 2 and (duration > 0 or cand_n >= 6):
                            cand_key, cand_n = None, 0
                            last_key, had_media = key, True
                            self.track_changed.emit(title, artist, album, duration, app_id)
                            self.playback.emit(pos, playing, duration, age)
                    else:
                        cand_key, cand_n = None, 0
                        self.playback.emit(pos, playing, duration, age)
            except Exception as exc:
                print("SMTC error:", exc, file=sys.stderr)
                manager = None
                await asyncio.sleep(2.0)
            await asyncio.sleep(POLL_INTERVAL)


# ----------------------------------------------------------------------------
# UI building blocks
# ----------------------------------------------------------------------------
class ElidedLabel(QLabel):
    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._full = ""
        self.setMinimumWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full(text)

    def set_full(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(self.width() - 2, 10)))


class TitleBar(QWidget):
    minimize_clicked = pyqtSignal()
    close_clicked = pyqtSignal()
    pin_toggled = pyqtSignal(bool)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setStyleSheet("""
            QLabel#title{color:rgba(255,255,255,175);font-size:12px;font-weight:600;}
            QPushButton{background:transparent;border:none;border-radius:6px;
                color:rgba(255,255,255,200);font-family:'Segoe MDL2 Assets';font-size:10px;}
            QPushButton:hover{background:rgba(255,255,255,26);}
            QPushButton:checked{color:%s;}
            QPushButton#close:hover{background:#c42b1c;color:white;}
        """ % ACCENT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 6, 0)
        lay.setSpacing(2)

        title = QLabel(f"♪  AutoLyrics <span style='color:#7a7a80;font-weight:400;'>v{APP_VERSION}</span>")
        title.setObjectName("title")
        lay.addWidget(title)
        lay.addStretch()

        self.pin_btn = self._button("\uE718", "Always on top", "pin")
        self.pin_btn.setCheckable(True)
        self.pin_btn.toggled.connect(self._on_pin)
        self.min_btn = self._button("\uE921", "Minimize", "min")
        self.min_btn.clicked.connect(self.minimize_clicked)
        self.close_btn = self._button("\uE8BB", "Hide to tray", "close")
        self.close_btn.clicked.connect(self.close_clicked)
        for b in (self.pin_btn, self.min_btn, self.close_btn):
            lay.addWidget(b)

    def _button(self, glyph: str, tip: str, name: str) -> QPushButton:
        b = QPushButton(glyph)
        b.setObjectName(name)
        b.setToolTip(tip)
        b.setFixedSize(36, 28)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.ArrowCursor)
        return b

    def _on_pin(self, on: bool) -> None:
        self.pin_btn.setText("\uE840" if on else "\uE718")
        self.pin_toggled.emit(on)

    def set_pinned(self, on: bool) -> None:
        self.pin_btn.blockSignals(True)
        self.pin_btn.setChecked(on)
        self.pin_btn.setText("\uE840" if on else "\uE718")
        self.pin_btn.blockSignals(False)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle:
                handle.startSystemMove()   # native drag: works with Aero Snap
        super().mousePressEvent(e)


class SmoothScrollArea(QScrollArea):
    user_scrolled = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self.viewport().setStyleSheet("background:transparent;")
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._target = 0

    def animate_to(self, y: int, ms: int = 380) -> None:
        bar = self.verticalScrollBar()
        y = max(bar.minimum(), min(bar.maximum(), int(y)))
        self._target = y
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(y)
        self._anim.setDuration(ms)
        self._anim.start()

    def wheelEvent(self, e) -> None:
        base = (self._target
                if self._anim.state() == QPropertyAnimation.State.Running
                else self.verticalScrollBar().value())
        self.animate_to(base - e.angleDelta().y() * 0.9, 300)
        self.user_scrolled.emit()
        e.accept()


class LyricsView(SmoothScrollArea):
    def __init__(self) -> None:
        super().__init__()
        self.container = QWidget()
        self.container.setStyleSheet("background:transparent;")
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setSpacing(2)
        self.setWidget(self.container)

        self.labels: list[QLabel] = []
        self.synced = False
        self.active = -1
        self._user_until = 0.0
        self._needs_center = False
        self.user_scrolled.connect(self._on_user_scroll)

    # -- content -----------------------------------------------------------
    def set_lines(self, lines: list[str], synced: bool) -> None:
        for lbl in self.labels:
            self.vbox.removeWidget(lbl)
            lbl.setParent(None)
            lbl.deleteLater()
        self.labels.clear()
        self.synced, self.active, self._needs_center = synced, -1, False
        self._user_until = 0.0

        for text in lines:
            lbl = QLabel(text if text.strip() else ("♪" if synced else " "))
            lbl.setWordWrap(True)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            lbl.setStyleSheet(LINE_DIM if synced else LINE_PLAIN)
            self.vbox.addWidget(lbl)
            self.labels.append(lbl)
        self._apply_margins()
        self._anim.stop()
        self.verticalScrollBar().setValue(0)

    def _apply_margins(self) -> None:
        pad = self.viewport().height() // 2 if self.synced else 16
        self.vbox.setContentsMargins(12, pad, 12, pad)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_margins()
        self._needs_center = True

    # -- sync --------------------------------------------------------------
    def _on_user_scroll(self) -> None:
        self._user_until = time.monotonic() + USER_SCROLL_PAUSE
        self._needs_center = True

    def force_follow(self) -> None:
        """Cancel the manual-scroll pause (used after a seek)."""
        self._user_until = 0.0
        self._needs_center = True

    def set_active(self, idx: int) -> None:
        if not self.synced or idx == self.active:
            return
        if 0 <= self.active < len(self.labels):
            self.labels[self.active].setStyleSheet(LINE_DIM)
        self.active = idx
        if 0 <= idx < len(self.labels):
            self.labels[idx].setStyleSheet(LINE_ACTIVE)
        self._needs_center = True

    def maybe_recenter(self) -> None:
        if not (self.synced and self._needs_center):
            return
        if time.monotonic() < self._user_until:
            return
        self._needs_center = False
        if self.active < 0:
            self.animate_to(0)
            return
        lbl = self.labels[self.active]
        self.animate_to(lbl.y() + lbl.height() // 2 - self.viewport().height() // 2)


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
def _cursor_for(edges) -> Qt.CursorShape | None:
    if edges is None:
        return None
    L, R = bool(edges & Qt.Edge.LeftEdge), bool(edges & Qt.Edge.RightEdge)
    T, B = bool(edges & Qt.Edge.TopEdge), bool(edges & Qt.Edge.BottomEdge)
    if (L and T) or (R and B):
        return Qt.CursorShape.SizeFDiagCursor
    if (R and T) or (L and B):
        return Qt.CursorShape.SizeBDiagCursor
    if L or R:
        return Qt.CursorShape.SizeHorCursor
    return Qt.CursorShape.SizeVerCursor


class LyricsWindow(QWidget):
    lyrics_ready = pyqtSignal(int, object, bool, str)   # token, Lyrics|None, net_failed, trace
    always_on_top_changed = pyqtSignal(bool)
    track_text_changed = pyqtSignal(str)
    status_msg = pyqtSignal(int, str, str)         # token, main, sub  (AI progress)
    ai_changed = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.cache = LyricsCache()
        self.always_on_top = self.settings.value("always_on_top", True, type=bool)
        self.ai_enabled = self.settings.value("ai_enabled", True, type=bool)

        flags = Qt.WindowType.FramelessWindowHint
        if self.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(280, 320)

        # state
        self.token = 0
        self.times: list[float] = []
        self._pos_base = 0.0
        self._pos_stamp = time.monotonic()
        self._playing = False
        self.offset = 0.0              # manual timing offset (s), per track
        self._fresh = True             # next SMTC sample is accepted as-is (new track)
        self._jump: tuple[float, float] | None = None   # unconfirmed position jump
        self._track_dur = 0.0          # longest duration seen for this track
        self._last_idx = -1            # last highlighted line (rewind diagnostics)
        self._lost = False             # SMTC timeline went empty mid-track
        self._anomaly = False          # SMTC shows other media (an ad?) -> lyrics frozen
        self._anomaly_at = 0.0
        self._stale = False            # after an ad: ignore the old timeline until refreshed
        self._anomaly_end = 0.0
        self._track_key = ""
        self._last_toggle = 0.0
        self._fade: QPropertyAnimation | None = None

        self._build_ui()
        self._restore_geometry()
        self.titlebar.set_pinned(self.always_on_top)

        self.lyrics_ready.connect(self._on_lyrics_ready)
        self.status_msg.connect(self._on_status_msg)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

        self.show_status("Waiting for music…",
                         "Play something in Spotify, YouTube, or any media app.")

    # -- construction ------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        root.setSpacing(0)

        self.titlebar = TitleBar(self)
        self.titlebar.close_clicked.connect(self.hide)
        self.titlebar.minimize_clicked.connect(self.showMinimized)
        self.titlebar.pin_toggled.connect(self.set_always_on_top)
        root.addWidget(self.titlebar)

        # track header
        self.info = QWidget()
        self.info.setCursor(Qt.CursorShape.ArrowCursor)
        info_lay = QVBoxLayout(self.info)
        info_lay.setContentsMargins(18, 0, 18, 8)
        info_lay.setSpacing(0)
        self.track_label = ElidedLabel()
        self.track_label.setStyleSheet(
            "color:white;font-size:15px;font-weight:700;background:transparent;")
        self.artist_label = ElidedLabel()
        self.artist_label.setStyleSheet(
            "color:rgba(255,255,255,140);font-size:12px;background:transparent;")
        info_lay.addWidget(self.track_label)
        info_lay.addWidget(self.artist_label)
        root.addWidget(self.info)
        self.info.hide()

        # stacked content: 0 = status, 1 = lyrics
        self.stack = QStackedWidget()
        self.stack.setCursor(Qt.CursorShape.ArrowCursor)
        status = QWidget()
        s_lay = QVBoxLayout(status)
        s_lay.setContentsMargins(24, 0, 24, 40)
        s_lay.addStretch()
        self.status_main = QLabel()
        self.status_main.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_main.setWordWrap(True)
        self.status_main.setStyleSheet(
            "color:rgba(255,255,255,225);font-size:20px;font-weight:600;background:transparent;")
        self.status_sub = QLabel()
        self.status_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_sub.setWordWrap(True)
        self.status_sub.setStyleSheet(
            "color:rgba(255,255,255,120);font-size:13px;background:transparent;")
        s_lay.addWidget(self.status_main)
        s_lay.addSpacing(6)
        s_lay.addWidget(self.status_sub)
        s_lay.addStretch()

        self.view = LyricsView()
        self.stack.addWidget(status)
        self.stack.addWidget(self.view)
        root.addWidget(self.stack, 1)

        # footer: lyric source + manual timing offset
        self.footer = QWidget()
        self.footer.setCursor(Qt.CursorShape.ArrowCursor)
        self.footer.setStyleSheet("""
            QLabel#source{color:rgba(255,255,255,110);font-size:11px;background:transparent;}
            QPushButton{background:rgba(255,255,255,18);border:none;border-radius:6px;
                color:rgba(255,255,255,215);font-size:11px;font-weight:600;padding:4px 9px;}
            QPushButton:hover{background:rgba(255,255,255,40);}
            QPushButton:pressed{background:rgba(96,205,255,70);}
            QPushButton#offval{background:transparent;color:%s;min-width:44px;}
            QPushButton#offval:hover{background:rgba(255,255,255,18);}
        """ % ACCENT)
        f_lay = QHBoxLayout(self.footer)
        f_lay.setContentsMargins(16, 2, 12, 10)
        f_lay.setSpacing(4)
        self.source_label = QLabel()
        self.source_label.setObjectName("source")
        f_lay.addWidget(self.source_label)
        f_lay.addStretch()

        self.offset_box = QWidget()
        o_lay = QHBoxLayout(self.offset_box)
        o_lay.setContentsMargins(0, 0, 0, 0)
        o_lay.setSpacing(2)
        self.off_minus = QPushButton(f"\u2212{OFFSET_STEP:g}s")
        self.off_minus.setToolTip("Lyrics appear later")
        self.off_val = QPushButton("0.0s")
        self.off_val.setObjectName("offval")
        self.off_val.setToolTip("Timing offset - click to reset")
        self.off_plus = QPushButton(f"+{OFFSET_STEP:g}s")
        self.off_plus.setToolTip("Lyrics appear earlier")
        for b in (self.off_minus, self.off_val, self.off_plus):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            o_lay.addWidget(b)
        self.off_minus.clicked.connect(lambda: self.nudge_offset(-OFFSET_STEP))
        self.off_plus.clicked.connect(lambda: self.nudge_offset(+OFFSET_STEP))
        self.off_val.clicked.connect(lambda: self.set_offset(0.0))
        f_lay.addWidget(self.offset_box)
        root.addWidget(self.footer)
        self.footer.hide()

    def _restore_geometry(self) -> None:
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(400, 580)
            area = QGuiApplication.primaryScreen().availableGeometry()
            self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 24)

    # -- painting & edge resizing -------------------------------------------
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # near-invisible fill so the transparent resize border still receives the mouse
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN),
                            RADIUS, RADIUS)
        p.fillPath(path, BG_COLOR)
        p.setPen(QPen(BORDER_COLOR, 1))
        p.drawPath(path)

    def _edges_at(self, pos: QPoint):
        r, flags = self.rect(), []
        if pos.x() <= MARGIN:
            flags.append(Qt.Edge.LeftEdge)
        if pos.x() >= r.width() - MARGIN:
            flags.append(Qt.Edge.RightEdge)
        if pos.y() <= MARGIN:
            flags.append(Qt.Edge.TopEdge)
        if pos.y() >= r.height() - MARGIN:
            flags.append(Qt.Edge.BottomEdge)
        return reduce(operator.or_, flags) if flags else None

    def mouseMoveEvent(self, e) -> None:
        cur = _cursor_for(self._edges_at(e.position().toPoint()))
        if cur is None:
            self.unsetCursor()
        else:
            self.setCursor(cur)
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            edges = self._edges_at(e.position().toPoint())
            if edges is not None and self.windowHandle():
                self.windowHandle().startSystemResize(edges)
                return
        super().mousePressEvent(e)

    def closeEvent(self, e) -> None:    # Alt+F4 etc. -> hide to tray
        e.ignore()
        self.hide()

    # -- window actions ----------------------------------------------------
    def toggle_visibility(self) -> None:
        now = time.monotonic()
        if now - self._last_toggle < 0.4:      # a double-click counts as one toggle
            return
        self._last_toggle = now
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.bring_to_front()

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def set_always_on_top(self, on: bool) -> None:
        self.always_on_top = on
        self.titlebar.set_pinned(on)
        self.settings.setValue("always_on_top", on)
        applied = False
        if sys.platform == "win32" and self.isVisible():
            try:
                user32 = ctypes.windll.user32
                user32.SetWindowPos.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                # HWND_TOPMOST=-1 / HWND_NOTOPMOST=-2 ; NOSIZE|NOMOVE|NOACTIVATE
                user32.SetWindowPos(int(self.winId()), -1 if on else -2,
                                    0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                applied = True
            except Exception:
                applied = False
        if not applied:
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            if visible:
                self.show()
        self.always_on_top_changed.emit(on)

    def toggle_always_on_top(self) -> None:
        self.set_always_on_top(not self.always_on_top)

    # -- manual timing offset ----------------------------------------------
    def _load_offsets(self) -> dict:
        try:
            return json.loads(self.settings.value("offsets", "{}", type=str))
        except Exception:
            return {}

    def _store_offset(self) -> None:
        if not self._track_key:
            return
        data = self._load_offsets()
        data.pop(self._track_key, None)
        if abs(self.offset) > 1e-6:
            data[self._track_key] = self.offset
        while len(data) > 300:
            data.pop(next(iter(data)))
        self.settings.setValue("offsets", json.dumps(data))

    def set_offset(self, value: float) -> None:
        self.offset = round(max(-OFFSET_LIMIT, min(OFFSET_LIMIT, value)), 2)
        self.off_val.setText(f"{self.offset:+.1f}s" if self.offset else "0.0s")
        self._store_offset()
        self.view.force_follow()

    def nudge_offset(self, delta: float) -> None:
        self.set_offset(self.offset + delta)

    def save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("always_on_top", self.always_on_top)
        self.settings.sync()

    # -- states ------------------------------------------------------------
    def _fade_in(self) -> None:
        if self._fade:
            self._fade.stop()
        eff = QGraphicsOpacityEffect(self.stack)
        eff.setOpacity(0.0)
        self.stack.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(240)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.finished.connect(lambda: self.stack.setGraphicsEffect(None))
        anim.start()
        self._fade = anim

    def show_status(self, main: str, sub: str = "") -> None:
        self.footer.hide()
        self.status_main.setText(main)
        self.status_sub.setText(sub)
        self.status_sub.setVisible(bool(sub))
        self.stack.setCurrentIndex(0)
        self._fade_in()

    def show_lyrics_view(self, source: str = "", synced: bool = False) -> None:
        self.source_label.setText(source)
        self.offset_box.setVisible(synced)       # offset is meaningless for plain text
        self.footer.show()
        self.stack.setCurrentIndex(1)
        self._fade_in()

    def show_unavailable(self) -> None:
        self.show_status("Media access unavailable",
                         "The 'winsdk' package could not be loaded. "
                         "Run: pip install winsdk (Windows only).")

    # -- media events (queued from the watcher thread) ----------------------
    def on_no_media(self) -> None:
        self.token += 1
        self.times = []
        self._track_key, self._last_idx = "", -1
        _log("media: nothing playing")
        self.info.hide()
        self.track_text_changed.emit("")
        self.show_status("Waiting for music…",
                         "Play something in Spotify, YouTube, or any media app.")

    def on_playback(self, position: float, playing: bool,
                    duration: float = 0.0, age: float = 0.0) -> None:
        """Feed SMTC samples into a local clock that survives bad data:
        * an EMPTY timeline (duration 0, position 0) in the middle of a track - some
          browsers drop the timeline halfway through - is ignored and the local clock
          keeps running until a real timeline comes back;
        * ads / other media (much shorter duration) freeze the lyrics, and the song
          resumes from the frozen point - the stale pre-ad timeline is ignored until
          the browser pushes a fresh update (or you press pause/play);
        * big position jumps are only obeyed after SEEK_CONFIRM seconds of agreement;
        * small drift is smoothed."""
        now = time.monotonic()
        expected = self._pos_base + ((now - self._pos_stamp) if self._playing else 0.0)
        self._track_dur = max(self._track_dur, duration)

        # 0) timeline lost: no duration although this track had one -> position is junk
        if duration <= 0 and self._track_dur > 0:
            if not self._lost:
                self._lost, self._jump = True, None
                _log(f"sync: SMTC timeline empty at clock {expected:.1f}s "
                     f"(position {position:.1f}s ignored) - using local clock")
            if playing != self._playing:                    # still honour play / pause
                self._pos_base, self._pos_stamp, self._playing = expected, now, playing
            return
        if self._lost:
            self._lost, self._jump = False, None
            _log(f"sync: SMTC timeline is back (reports {position:.1f}s, "
                 f"clock {expected:.1f}s)")

        # 1) different media (ad) -> freeze
        odd = (duration > 0 and self._track_dur - duration > 8.0
               and duration < self._track_dur * 0.8)
        if odd and self._anomaly and now - self._anomaly_at > AD_MAX:
            self._track_dur, odd = duration, False          # it is the new normal
            self._anomaly, self._fresh = False, True
            _log(f"sync: duration stayed {duration:.0f}s for {AD_MAX:.0f}s - accepting it")
        if odd:
            if not self._anomaly:
                self._anomaly, self._anomaly_at, self._jump = True, now, None
                self._pos_base, self._pos_stamp, self._playing = expected, now, False
                _log(f"sync: media duration {duration:.0f}s vs song {self._track_dur:.0f}s "
                     f"(ad?) - lyrics frozen at {expected:.1f}s")
            return
        if self._anomaly:
            self._anomaly, self._stale, self._anomaly_end = False, True, now
            self._pos_base, self._pos_stamp, self._playing = expected, now, playing
            _log(f"sync: back to the song - resumed from {expected:.1f}s")
            return

        # 2) new track / play / pause: take the sample as it is
        if self._fresh or playing != self._playing:
            self._fresh = self._stale = False
            self._jump = None
            self._pos_base, self._pos_stamp, self._playing = position, now, playing
            return

        # 3) after an ad the old timeline is stale until the browser sends an update
        if self._stale:
            if age < (now - self._anomaly_end) - 0.5:      # updated after the ad ended
                self._stale = False
            else:
                return                                      # keep the local clock

        delta = position - expected
        if abs(delta) <= SYNC_TOLERANCE:                    # normal: low-pass the jitter
            self._jump = None
            self._pos_base = expected + delta * SYNC_SMOOTH
            self._pos_stamp = now
            return

        # 4) big difference: a seek or a glitch? Obey only if it stays consistent
        if self._jump is not None:
            jump_pos, jump_at = self._jump
            candidate = jump_pos + ((now - jump_at) if playing else 0.0)
            if abs(position - candidate) <= SYNC_TOLERANCE:
                if now - jump_at >= (SEEK_CONFIRM if playing else 0.5):
                    self._jump = None
                    self._pos_base, self._pos_stamp = position, now
                    _log(f"sync: seek accepted {expected:.1f}s -> {position:.1f}s")
                    if self.times:
                        self.view.force_follow()
                return                                      # still waiting for confirmation
        _log(f"sync: ignored position {position:.1f}s (expected {expected:.1f}s)")
        self._jump = (position, now)

    def on_track_changed(self, title: str, artist: str, album: str,
                         duration: float, app_id: str = "") -> None:
        new_key = LyricsCache.key(title, artist)
        if new_key == self._track_key and self.times:
            # Same song, only the metadata text changed: keep lyrics, scroll and clock.
            self.track_label.set_full(title)
            self.artist_label.set_full(artist)
            _log(f"track: metadata refresh ignored ({artist} - {title})")
            return
        _log(f"track: {artist} - {title} [{duration:.0f}s, {app_id}] "
             f"(clock was {self._pos_base:.1f}s)")
        self._last_idx = -1
        self.token += 1
        token = self.token
        self.times = []
        self._fresh, self._jump = True, None
        self._track_dur, self._anomaly, self._stale = duration, False, False
        self._lost = False
        self._track_key = LyricsCache.key(title, artist)
        self.offset = float(self._load_offsets().get(self._track_key, 0.0))
        self.off_val.setText(f"{self.offset:+.1f}s" if self.offset else "0.0s")
        self.track_label.set_full(title)
        self.artist_label.set_full(artist)
        self.artist_label.setVisible(bool(artist))
        self.info.show()
        self.track_text_changed.emit(f"{artist} – {title}" if artist else title)

        cached = self.cache.get(title, artist)
        if cached:
            self._apply_lyrics(cached, title, artist)
            return

        self.show_status(
            "Loading lyrics…",
            "Checking captions, then LRCLIB…" if may_be_youtube(app_id) else "")
        threading.Thread(target=self._fetch_worker,
                         args=(token, title, artist, album, duration, app_id),
                         daemon=True).start()

    def _fetch_worker(self, token, title, artist, album, duration, app_id) -> None:
        trace: list[str] = []
        ctx: dict = {}
        try:
            lyr, net_fail = fetch_best_lyrics(title, artist, album, duration,
                                              app_id, trace, ctx)
        except Exception as exc:
            _log(f"fetch error for {artist} - {title}: {exc!r}")
            lyr, net_fail = None, True
        if lyr is None and not net_fail and self.ai_enabled and ctx.get("vid"):
            lyr = self._run_ai(token, ctx["vid"], duration, trace)
        _log(f"{artist} - {title} [{duration:.0f}s, {app_id}] -> "
             f"{lyr.source if lyr else 'NONE'} | {' | '.join(trace)}")
        self.lyrics_ready.emit(token, lyr, net_fail, " · ".join(trace))

    def _run_ai(self, token: int, vid: str, duration: float, trace: list[str]):
        if duration > AI_MAX_SECONDS:
            trace.append("AI: video too long")
            return None
        if not _ai_lock.acquire(blocking=False):
            trace.append("AI: busy with another song")
            return None
        _set_low_priority(True)
        try:
            lyr = transcribe_youtube(
                vid,
                should_cancel=lambda: token != self.token,
                on_status=lambda m, sub="": self.status_msg.emit(token, m, sub))
            if lyr is None and token == self.token:
                trace.append("AI: no clear vocals detected")
            return lyr
        except ImportError:
            trace.append("AI: faster-whisper not installed")
        except Exception as exc:
            trace.append(f"AI: error ({type(exc).__name__})")
            _log(f"AI error for {vid}: {exc!r}")
        finally:
            _set_low_priority(False)
            _ai_lock.release()
        return None

    def _on_status_msg(self, token: int, main: str, sub: str) -> None:
        # progress text only - no fade, and never replaces already shown lyrics
        if token != self.token or self.stack.currentIndex() != 0:
            return
        self.status_main.setText(main)
        self.status_sub.setText(sub)
        self.status_sub.setVisible(bool(sub))

    def toggle_ai(self) -> None:
        self.ai_enabled = not self.ai_enabled
        self.settings.setValue("ai_enabled", self.ai_enabled)
        self.ai_changed.emit(self.ai_enabled)

    def _on_lyrics_ready(self, token: int, lyr, net_fail: bool, trace: str = "") -> None:
        if token != self.token:
            return                              # stale result for an older track
        title, artist = self.track_label._full, self.artist_label._full
        if lyr is None:
            self.times = []
            if net_fail:
                self.show_status("Couldn't reach LRCLIB", "Check your internet connection.")
            else:
                head = f"{artist} – {title}" if artist else title
                self.show_status("No lyrics found",
                                 f"{head}\n{trace}" if trace else head)
            return
        self.cache.put(title, artist, lyr)
        self._apply_lyrics(lyr, title, artist)

    def _apply_lyrics(self, lyr: Lyrics, title: str, artist: str) -> None:
        self._last_idx = -1
        _log(f"ui: lyrics applied ({lyr.source or 'cached'}, {len(lyr.synced)} lines) "
             f"at clock {self._pos_base:.1f}s")
        if lyr.synced:
            self.times = [t for t, _ in lyr.synced]
            self.view.set_lines([s for _, s in lyr.synced], synced=True)
            self.show_lyrics_view(lyr.source or "Cached", synced=True)
        elif lyr.plain:
            self.times = []
            self.view.set_lines(lyr.plain.splitlines(), synced=False)
            self.show_lyrics_view(lyr.source or "LRCLIB · plain", synced=False)
        else:
            self.times = []
            self.show_status("Instrumental", f"{artist} – {title}" if artist else title)

    # -- sync tick ---------------------------------------------------------
    def _tick(self) -> None:
        if not self.times or not self.isVisible() or self.isMinimized():
            return
        pos = self._pos_base
        if self._playing:
            pos += time.monotonic() - self._pos_stamp
        pos += self.offset
        idx = bisect.bisect_right(self.times, pos + LYRIC_LEAD) - 1
        if self._last_idx >= 0 and idx < self._last_idx - 2:
            _log(f"ui: line jumped back {self._last_idx}->{idx} at {pos:.1f}s "
                 f"(base {self._pos_base:.1f}s, playing={self._playing}, "
                 f"offset {self.offset:+.1f}s, anomaly={self._anomaly}, stale={self._stale})")
        self._last_idx = idx
        self.view.set_active(idx)
        self.view.maybe_recenter()


# ----------------------------------------------------------------------------
# Tray icon
# ----------------------------------------------------------------------------
def make_icon_image(size: int = 64) -> Image.Image:
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size * 0.22, fill=(32, 32, 36, 255))
    accent = (96, 205, 255, 255)
    d.ellipse((16 * s, 38 * s, 32 * s, 52 * s), fill=accent)          # note head
    d.rectangle((29 * s, 14 * s, 33 * s, 46 * s), fill=accent)         # stem
    d.polygon([(33 * s, 14 * s), (48 * s, 20 * s), (48 * s, 28 * s), (33 * s, 22 * s)],
              fill=accent)                                             # flag
    return img


def pil_to_pixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class TrayBridge(QObject):
    """pystray runs in its own thread; these signals hop back to the Qt thread."""
    toggle = pyqtSignal()
    toggle_aot = pyqtSignal()
    toggle_ai = pyqtSignal()
    quit = pyqtSignal()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main() -> int:
    if sys.platform != "win32":
        print("AutoLyrics only runs on Windows (SMTC is a Windows API).")
        return 1
    for stream in ("stdout", "stderr"):          # windowed exe: libs that write here would crash
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w"))
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AutoLyrics.App")
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(QIcon(pil_to_pixmap(make_icon_image(128))))

    win = LyricsWindow()
    watcher = MediaWatcher()
    watcher.track_changed.connect(win.on_track_changed)
    watcher.no_media.connect(win.on_no_media)
    watcher.playback.connect(win.on_playback)
    watcher.unavailable.connect(win.show_unavailable)

    # --- tray -------------------------------------------------------------
    bridge = TrayBridge()
    bridge.toggle.connect(win.toggle_visibility)
    bridge.toggle_aot.connect(win.toggle_always_on_top)
    bridge.toggle_ai.connect(win.toggle_ai)

    menu = pystray.Menu(
        pystray.MenuItem("Show/Hide Lyrics", lambda icon, item: bridge.toggle.emit(),
                         default=True),                  # default = left-click action
        pystray.MenuItem("Always on Top", lambda icon, item: bridge.toggle_aot.emit(),
                         checked=lambda item: win.always_on_top),
        pystray.MenuItem("AI lyrics (last resort)", lambda icon, item: bridge.toggle_ai.emit(),
                         checked=lambda item: win.ai_enabled),
        pystray.MenuItem("Open debug log", lambda icon, item: _open_log()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda icon, item: bridge.quit.emit()),
    )
    tray = pystray.Icon(APP_NAME, make_icon_image(64), APP_NAME, menu)

    win.always_on_top_changed.connect(lambda _on: tray.update_menu())
    win.ai_changed.connect(lambda _on: tray.update_menu())

    def update_tooltip(text: str) -> None:
        try:
            tray.title = (f"{APP_NAME} – {text}" if text else APP_NAME)[:120]
        except Exception:
            pass

    win.track_text_changed.connect(update_tooltip)

    def quit_app() -> None:
        watcher.stop()
        win.save_settings()
        try:
            tray.stop()
        except Exception:
            pass
        app.quit()

    bridge.quit.connect(quit_app)
    app.aboutToQuit.connect(win.save_settings)

    tray.run_detached()
    if "--minimized" not in sys.argv:
        win.show()
    watcher.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
