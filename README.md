# AutoLyrics

A lightweight floating lyrics viewer for Windows. It reads the currently playing
track from Windows media controls (Spotify, YouTube in Chrome/Edge/Brave, etc.),
fetches synced lyrics from [LRCLIB](https://lrclib.net) and shows them in a small
dark, resizable window that lives in the system tray.

## Run from source
    pip install -r requirements.txt
    python app.py            # add --minimized to start in the tray

Requires Windows 10/11 and Python 3.11 or 3.12.

## Build the .exe
Push a version tag and GitHub Actions attaches `AutoLyrics.exe` to a Release:

    git tag v1.0.0
    git push origin v1.0.0

## Usage
- Tray icon click: show/hide. Right-click: Show/Hide, Always on Top, Exit.
- Close button hides to tray; use the tray's Exit to quit.
- Scroll the lyrics manually; auto-follow resumes after 4 seconds.
