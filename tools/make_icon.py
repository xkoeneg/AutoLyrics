"""Regenerate assets/icon.ico (and icon.png) from the icon drawn in app.py.

    python tools/make_icon.py

The .exe icon (PyInstaller --icon) and the in-app window/tray icon share one drawing
function, `make_icon_image`, so they always match. Each size is rendered separately
(small sizes use a bolder note) instead of shrinking the big one.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import make_icon_image  # noqa: E402

SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def main() -> None:
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    frames = {s: make_icon_image(s) for s in SIZES}
    frames[256].save(
        out / "icon.ico", format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=[frames[s] for s in SIZES if s != 256],
    )
    frames[256].save(out / "icon.png")
    print("wrote", out / "icon.ico", "and", out / "icon.png")


if __name__ == "__main__":
    main()
