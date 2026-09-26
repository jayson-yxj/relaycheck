"""Generate the relaycheck mark: ``relaycheck.ico`` and ``relaycheck.png``.

Run it from the repo root after changing the geometry:

    E:\\anaconda\\python.exe gui\\make_icon.py

Why a script instead of a checked-in binary blob
------------------------------------------------
The mark is three primitives. Keeping the geometry in source means the icon can
be re-derived, reviewed as a diff, and re-rendered at whatever size a future
platform wants — a committed ``.ico`` cannot. It also documents the two
constraints the design actually has to satisfy:

1. **It has to survive 16 px.** That is the size Windows uses in Explorer's
   small-icon view and in the taskbar at small taskbar settings, and it is the
   only size where a mark either works or turns to mush. Candidates with thin
   strokes (arrows) or fine interior detail (a magnifier's handle) were drawn and
   discarded for exactly this reason. Two bars and a slash is what is left after
   that filter.
2. **It has to read as the product, not as a generic security shield.** ``≠`` is
   the whole claim: what you asked for is not what came back.

The only rule that matters here is that nothing is drawn at the size it ships at.
Every dimension is a fraction of ``S`` (=1024) and gets LANCZOS-downsampled, which
is the difference between a clean edge and a staircase.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent

#: Windows picks from these: 16 = Explorer list/small icons and the taskbar at
#: small settings, 24 = taskbar at 125% DPI, 32 = desktop/taskbar at 100%,
#: 48 = Explorer medium, 256 = Explorer extra-large and the Alt-Tab switcher.
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Supersample canvas. Every coordinate below is a fraction of this.
S = 1024

#: slate-900 / slate-50 / red-600. The ink ground keeps the white bars legible on
#: a light taskbar; the red is the same "something is wrong" red the result card
#: uses for a high-severity verdict, so the icon states the product's vocabulary
#: before the window is even open.
INK = (15, 23, 42, 255)
PAPER = (248, 250, 252, 255)
RED = (220, 38, 38, 255)

#: Corner radius as a fraction of the side. At 16 px this is ~3.5 px, which still
#: reads as "rounded square" rather than as a smudge.
RADIUS = 0.22


def render(size: int = S) -> Image.Image:
    """Draw the mark at ``size`` px, supersampled from the master canvas."""
    master = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(master)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=RADIUS * S, fill=INK)

    # Two bars and a slash: "≠". Deliberately thicker than a typographic not-equal
    # (0.115 vs a font's ~0.08) because the whole mark has to carry 16 px on its own.
    bar = int(0.115 * S)
    for y in (0.385, 0.615):
        d.line([0.215 * S, y * S, 0.785 * S, y * S], fill=PAPER, width=bar)
    d.line(
        [0.665 * S, 0.185 * S, 0.335 * S, 0.815 * S],
        fill=RED,
        width=int(0.088 * S),
    )
    if size == S:
        return master
    return master.resize((size, size), Image.LANCZOS)


def main() -> int:
    ico = OUT_DIR / "relaycheck.ico"
    png = OUT_DIR / "relaycheck.png"

    # PIL's ICO writer resizes *its base image* to each requested size, so hand it
    # the supersampled master rather than a pre-shrunk copy — it would otherwise
    # resize an already-downsampled bitmap and every small frame would be soft.
    master = render()
    master.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])

    # The window icon goes through ``tk.PhotoImage``, which reads PNG but not ICO.
    render(256).save(png, format="PNG")

    with Image.open(ico) as check:
        got = sorted(check.info.get("sizes", []))
    print(f"wrote {ico}  ({ico.stat().st_size} bytes, sizes={[s[0] for s in got]})")
    print(f"wrote {png}  ({png.stat().st_size} bytes, 256x256)")
    if [s[0] for s in got] != list(SIZES):
        print(f"warning: expected sizes {list(SIZES)}, got {[s[0] for s in got]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
