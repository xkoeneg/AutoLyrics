# AutoLyrics

A lightweight floating lyrics viewer for Windows. It reads the currently playing
track **and its playback position** from Windows media controls (Spotify, YouTube in
Chrome/Edge/Brave, etc.) and shows time-synced lyrics in a small dark, resizable
window that lives in the system tray.

## Lyrics sources (automatic fallback)
1. **YouTube Closed Captions** – for browser playback, the matching YouTube video is
   found via `yt-dlp` (title + duration match) and its uploaded captions are used.
   Great for music videos with long intros or acoustic covers.
2. **LRCLIB synced (.lrc)** – looked up by title / artist / album / duration.
3. **LRCLIB plain text** – shown un-synced if nothing better exists.

The footer shows which source is active. Auto-generated captions are ignored by
default; set `ALLOW_AUTO_CC = True` in `app.py` to accept them.

## Timing
- Lyrics follow the real SMTC timeline, so seeking, pausing and resuming re-sync
  automatically.
- `-0.5s` / `+0.5s` nudge the timing (`+` = lyrics appear earlier). Click the value
  in the middle to reset. Offsets are remembered per track.

## Run from source
    pip install -r requirements.txt
    python app.py            # add --minimized to start in the tray

Requires Windows 10/11 and Python 3.11 or 3.12.

## Build the .exe
Push a version tag and GitHub Actions attaches `AutoLyrics.exe` to a Release:

    git tag v1.1.0
    git push origin v1.1.0

## Usage
- Tray icon click: show/hide. Right-click: Show/Hide, Always on Top, Exit.
- Close button hides to tray; use the tray's Exit to quit.
- Scroll the lyrics manually; auto-follow resumes after 4 seconds (or instantly on seek).
