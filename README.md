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

## Synced / Plain
The footer has **Synced** and **Plain** buttons. Plain shows the whole lyric sheet (derived from
the timed lines when a source has no plain text) and you scroll it yourself. Your choice is
remembered; a song that only has one version simply shows that one.

## Sync accuracy
Some browsers (notably Firefox) only publish a real playback position after a pause/play.
AutoLyrics watches for the player writing a new timeline and re-syncs by itself:
- **Seeking forward / backward** is detected automatically. A real timeline is followed at once;
  an empty one triggers a quick pause/play (about 0.15 s) that makes the browser publish the true
  position. Dragging the progress bar causes only one re-sync, after you let go.
- **Opening the app mid-song** does the same on the first sample.
- Safety limits: nudges are at least 2.5 s apart, max. 3 per minute, and back off to 20 s if a
  browser never gives a real timeline.
- The **Sync** button does it on demand. Turn the automatic part off in the tray menu
  (*Auto re-sync*).

## Timing
- Lyrics follow the real SMTC timeline, so seeking, pausing and resuming re-sync
  automatically.
- Some browsers drop the media timeline mid-song (duration 0, position 0). That empty
  timeline is ignored and the lyrics keep running on the local clock until a real one returns.
- Ads / other media (a much shorter duration reported mid-song) freeze the lyrics; the song
  resumes from the frozen point and the stale pre-ad timeline is ignored.
- Glitch-proof: stale / zero positions reported by browsers are ignored, jitter is
  smoothed, and a seek is accepted once the next poll confirms it. The app also sticks to
  one media session, so two players can't make the lyrics flip-flop. Ignored glitches are
  noted in `%APPDATA%\AutoLyrics\debug.log` (tray menu: **Open debug log**), including any time the
  highlighted line jumps backwards, so sync problems can be traced.
- `-0.5s` / `+0.5s` nudge the timing (`+` = lyrics appear earlier). Click the value
  in the middle to reset. Offsets are remembered per track.

## Run from source
    pip install -r requirements.txt
    python app.py            # add --minimized to start in the tray

Requires Windows 10/11 and Python 3.11 or 3.12.

## Taskbar icon
The icon is embedded in `AutoLyrics.exe` (`assets/icon.ico`, built with `--icon`), so pinning
looks right. Put the `AutoLyrics` folder somewhere permanent first (e.g. `C:\Apps\AutoLyrics`),
then right-click `AutoLyrics.exe` > **Pin to taskbar**. If an old icon sticks, unpin, run
`ie4uinit.exe -show` in a terminal, and pin again. Edit the design in `make_icon_image`
(app.py) and run `python tools/make_icon.py` to regenerate the files.

## Build
Push a version tag and GitHub Actions attaches `AutoLyrics-windows.zip` to a Release
(extract it and run `AutoLyrics\AutoLyrics.exe`):

    git tag v1.4.2
    git push origin v1.4.2

## Usage
- Tray icon click: show/hide. Right-click: Show/Hide, Always on Top, Exit.
- Close button hides to tray; use the tray's Exit to quit.
- Scroll the lyrics manually; auto-follow resumes after 4 seconds (or instantly on seek).
