"""Generate tools/icon.ico - a simple dark rounded tile with a white knight.

Used as the icon of the Desktop shortcut "Chess Move Selector".
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "icon.ico"
SIZE = 256

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([4, 4, SIZE - 4, SIZE - 4], radius=52,
                    fill=(26, 28, 34, 255), outline=(46, 204, 113, 255), width=9)

font = None
for path, size in ((r"C:\Windows\Fonts\seguisym.ttf", 170),
                   (r"C:\Windows\Fonts\arial.ttf", 150),
                   ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 150)):
    try:
        font = ImageFont.truetype(path, size)
        break
    except Exception:
        continue
if font is None:
    font = ImageFont.load_default()

glyph = "\u265e"  # black chess knight (renders as a solid knight shape)
bbox = d.textbbox((0, 0), glyph, font=font)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
x = (SIZE - w) / 2 - bbox[0]
y = (SIZE - h) / 2 - bbox[1]
d.text((x, y), glyph, font=font, fill=(240, 240, 240, 255))

img.save(OUT, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
sys.exit(0)
