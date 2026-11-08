"""Icon for quotaline: a rounded square badge with a vertical gradient and a single white glyph.

The glyph is a needle gauge (like a fuel gauge): an arc open at the bottom, the needle pointing
right and the pivot. It says "how much is left" without a single digit. Violet badge. Below 40 px a
simplified master is used (thicker arc and needle, no pivot) so it still reads.

Writes _icon.ico (Windows, 16 to 256 px) and _icon.png (256 px, for Linux file managers and
launchers). Needs Pillow: pip install pillow
"""
import math
import os
from PIL import Image, ImageDraw

S = 1024
OUT = os.path.dirname(os.path.abspath(__file__))
TOP, BOT = (154, 104, 236), (84, 44, 164)      # violet -> dark violet


def badge():
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    grad = Image.new('RGB', (1, S))
    for y in range(S):
        t = y / (S - 1)
        grad.putpixel((0, y), tuple(round(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3)))
    grad = grad.resize((S, S))
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def dot(d, x, y, r):
    d.ellipse([x - r, y - r, x + r, y + r], fill=255)


def stroke(d, pts, w):
    """Polyline with round caps."""
    d.line(pts, fill=255, width=w)
    for x, y in pts:
        dot(d, x, y, w / 2)


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def gauge(small):
    img = badge()
    m = Image.new('L', (S, S), 0); d = ImageDraw.Draw(m)
    w = int(S * (0.14 if small else 0.11))
    cx, cy = S * 0.50, S * 0.60
    r = S * (0.31 if small else 0.30)
    # arc open at the bottom: from 150 deg (bottom-left) over the top to 30 deg (bottom-right)
    a0, a1 = 150, 390
    d.arc([cx - r, cy - r, cx + r, cy + r], start=a0, end=a1, fill=255, width=w)
    for a in (a0, a1):
        x, y = polar(cx, cy, r - w / 2, a)
        dot(d, x, y, w / 2)
    # needle at about three quarters of the scale (up-right)
    ang = a0 + 0.72 * (a1 - a0)
    wa = int(w * (0.95 if small else 0.62))
    lx, ly = polar(cx, cy, r - w * (1.0 if small else 0.95), ang)
    stroke(d, [(cx, cy), (lx, ly)], wa)
    if not small:
        dot(d, cx, cy, w * 0.62)
    # soft shadow and white glyph
    sh = Image.new('RGBA', (S, S), (0, 0, 0, 0)); sh.paste((0, 0, 0, 90), (0, int(S * 0.014)), m)
    img.alpha_composite(sh)
    col = Image.new('RGBA', (S, S), (0, 0, 0, 0)); col.paste((255, 255, 255), (0, 0), m)
    img.alpha_composite(col)
    return img


rich, plain = gauge(False), gauge(True)
frames = [(plain if s < 40 else rich).resize((s, s), Image.LANCZOS)
          for s in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)]
dest = os.path.join(OUT, "_icon.ico")
frames[-1].save(dest, format="ICO", sizes=[(f.width, f.height) for f in frames], append_images=frames[:-1])
print("written", dest)
png = os.path.join(OUT, "_icon.png")
frames[-1].save(png, format="PNG")
print("written", png)
