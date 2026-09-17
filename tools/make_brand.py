"""Draw the brand images and check their sizes.

Original artwork: three rising steps on a dark tile, the last one under a
check, for a harness that improved and held. Supersampled and downscaled so
the edges are clean at every size Home Assistant serves.

    python tools/make_brand.py

Writes icon.png 256x256, icon@2x.png 512x512, logo.png 512x256 and
logo@2x.png 1024x512 into the integration's brand/ directory and refuses to
finish if any measured size is off.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BRAND = os.path.join(
    ROOT, "custom_components", "agent_harness_performance_tracker", "brand"
)
S = 4  # supersampling factor

TILE = (18, 26, 38, 255)
STEP = (86, 190, 240, 255)
STEP_DIM = (56, 110, 150, 255)
CHECK = (110, 220, 140, 255)
TEXT = (232, 238, 244, 255)
ACCENT = (86, 190, 240, 255)

SPECS = {
    "icon.png": (256, 256),
    "icon@2x.png": (512, 512),
    "logo.png": (512, 256),
    "logo@2x.png": (1024, 512),
}


def tile(size: int) -> Image.Image:
    """The square mark at one size, drawn at S times the size then reduced."""
    w = size * S
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(w * 0.18)
    d.rounded_rectangle((0, 0, w - 1, w - 1), radius=r, fill=TILE)

    # Three steps, each higher than the last, left to right.
    pad = w * 0.16
    gap = w * 0.05
    width = (w - 2 * pad - 2 * gap) / 3
    base = w - pad
    heights = (0.22, 0.40, 0.60)
    for i, frac in enumerate(heights):
        x0 = pad + i * (width + gap)
        top = base - (w - 2 * pad) * frac
        colour = STEP if i == 2 else STEP_DIM
        d.rounded_rectangle(
            (x0, top, x0 + width, base), radius=int(w * 0.03), fill=colour
        )

    # A check above the last step: the version that improved and held.
    cx = pad + 2 * (width + gap) + width / 2
    cy = base - (w - 2 * pad) * heights[2] - w * 0.13
    stroke = int(w * 0.045)
    arm = w * 0.07
    d.line(
        [
            (cx - arm, cy),
            (cx - arm * 0.25, cy + arm * 0.75),
            (cx + arm * 1.1, cy - arm * 0.8),
        ],
        fill=CHECK,
        width=stroke,
        joint="curve",
    )
    return img.resize((size, size), Image.LANCZOS)


def logo(width: int, height: int) -> Image.Image:
    """Tile at the left, the name at the right, transparent background."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mark = tile(height)
    img.paste(mark, (0, 0), mark)
    d = ImageDraw.Draw(img)
    x = height + width * 0.05
    avail = width - x - width * 0.03

    def fitted(text: str, frac: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        size = int(height * frac)
        while size > 8:
            f = ImageFont.load_default(size=size)
            if d.textlength(text, font=f) <= avail:
                return f
            size -= 2
        return ImageFont.load_default(size=8)

    d.text((x, height * 0.20), "Harness", font=fitted("Harness", 0.34), fill=TEXT)
    d.text((x, height * 0.58), "Tracker", font=fitted("Tracker", 0.26), fill=ACCENT)
    return img


def main() -> int:
    os.makedirs(BRAND, exist_ok=True)
    for name, (w, h) in SPECS.items():
        image = tile(w) if w == h else logo(w, h)
        image.save(os.path.join(BRAND, name), "PNG", optimize=True)
    bad = 0
    for name, (w, h) in SPECS.items():
        path = os.path.join(BRAND, name)
        with Image.open(path) as im:
            got = im.size
            mode = im.mode
        ok = got == (w, h) and mode == "RGBA"
        bad += not ok
        print(
            f"  {name:12s} {got[0]}x{got[1]} {mode} "
            f"{os.path.getsize(path)} bytes {'ok' if ok else 'WRONG'}"
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
