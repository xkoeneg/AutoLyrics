# AutoLyrics

A lightweight floating lyrics viewer for Windows. It reads the currently playing
track **and its playback position** from Windows media controls (Spotify, YouTube in
Chrome/Edge/Brave, etc.) and shows time-synced lyrics in a small dark, resizable
window that lives in the system tray.

## Lyrics sources (automatic fallback)
1. **YouTube uploaded Closed Captions** - for browser playback, the matching YouTube
   video is found via `yt-dlp` (title + duration match) and its captions are used.
2. **LRCLIB synced (.lrc)** - looked up by title / artist / album / duration.
3. **YouTube auto-generated captions** - timed fallback (`ALLOW_AUTO_CC` in `app.py`).
4. **AI transcription (last resort)** - only if nothing above was found and the
   matched YouTube video is <= 15 min. The audio is downloaded to a temp folder,
   transcribed locally with `faster-whisper` (`small` model, CPU, 2 threads, below-normal
   priority), then deleted. Only the resulting lyrics text is cached.
   - The model (~500MB) downloads once, to `%APPDATA%\AutoLyrics\models`.
   - Takes roughly 1-3 minutes per song; progress shows in the window. Result is cached.
   - Labelled "AI-generated" - may contain errors. Toggle from the tray menu
     (**AI lyrics (last resort)**). Force a language with `AI_LANGUAGE = "tl"`.
5. **LRCLIB plain text** - shown un-synced if nothing timed exists.

The footer shows which source is active. When nothing is found, the window lists which
layers missed, and details are written to `%APPDATA%\AutoLyrics\debug.log`.

## Timing
- Lyrics follow the real SMTC timeline, so seeking, pausing and resuming re-sync
  automatically.
- Glitch-proof: stale / zero positions reported by browsers are ignored, jitter is
  smoothed, and a seek is accepted once the next poll confirms it. The app also sticks to
  one media session, so two players can't make the lyrics flip-flop. Ignored glitches are
  noted in `%APPDATA%\AutoLyrics\debug.log`.
- `-0.5s` / `+0.5s` nudge the timing (`+` = lyrics appear earlier). Click the value
  in the middle to reset. Offsets are remembered per track.

## Run from source
    pip install -r requirements.txt
    python app.py            # add --minimized to start in the tray

Requires Windows 10/11 and Python 3.11 or 3.12.

## Build
Push a version tag and GitHub Actions attaches `AutoLyrics-windows.zip` to a Release
(extract it and run `AutoLyrics\AutoLyrics.exe`):

    git tag v1.3.2
    git push origin v1.3.2

## Usage
- Tray icon click: show/hide. Right-click: Show/Hide, Always on Top, Exit.
- Close button hides to tray; use the tray's Exit to quit.
- Scroll the lyrics manually; auto-follow resumes after 4 seconds (or instantly on seek).
