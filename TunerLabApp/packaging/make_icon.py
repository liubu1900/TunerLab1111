"""Generate ``tunerlab.ico`` without any third-party imaging library.

A 256x256 icon is composed from three shapes (rounded blue plate, a white "T",
a teal sine wave) with 3x3 supersampling, then written as a 32-bit BGRA ICO.

Run once before building::

    python packaging\\make_icon.py
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

SIZE = 256
SAMPLES = 3

BACKGROUND = (0x2F, 0x6F, 0x9F)
FOREGROUND = (0xFF, 0xFF, 0xFF)
ACCENT = (0x42, 0xC5, 0xB7)

PLATE = dict(x0=8, y0=8, x1=248, y1=248, r=46)
BAR_H = dict(x0=54, y0=54, x1=202, y1=92, r=8)
BAR_V = dict(x0=110, y0=88, x1=146, y1=190, r=8)
WAVE_X0, WAVE_X1 = 48, 208
WAVE_MID, WAVE_AMP, WAVE_THICK = 208.0, 13.0, 5.0


def _in_rounded_rect(x: float, y: float, x0: float, y0: float, x1: float,
                     y1: float, r: float) -> bool:
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _in_wave(x: float, y: float) -> bool:
    if not (WAVE_X0 <= x <= WAVE_X1):
        return False
    centre = WAVE_MID + WAVE_AMP * math.sin((x - WAVE_X0) / 21.0)
    return abs(y - centre) <= WAVE_THICK


def _sample(px: int, py: int) -> tuple[int, int, int, int]:
    """Colour and alpha of one pixel, supersampled."""
    plate = bar_h = bar_v = wave = 0
    offsets = [(i + 0.5) / SAMPLES for i in range(SAMPLES)]
    for oy in offsets:
        for ox in offsets:
            x, y = px + ox, py + oy
            if _in_rounded_rect(x, y, **PLATE):
                plate += 1
                if _in_rounded_rect(x, y, **BAR_H):
                    bar_h += 1
                elif _in_rounded_rect(x, y, **BAR_V):
                    bar_v += 1
                elif _in_wave(x, y):
                    wave += 1
    total = SAMPLES * SAMPLES
    if plate == 0:
        return 0, 0, 0, 0
    colour = list(BACKGROUND)
    # wave first, "T" on top of it
    if wave:
        a = wave / total
        colour = [c * (1 - a) + s * a for c, s in zip(colour, ACCENT)]
    glyph = (bar_h + bar_v) / total
    if glyph:
        colour = [c * (1 - glyph) + s * glyph for c, s in zip(colour, FOREGROUND)]
    alpha = plate / total
    return (int(round(colour[2])), int(round(colour[1])), int(round(colour[0])),
            int(round(alpha * 255)))


def build_pixels() -> bytes:
    rows = []
    for y in range(SIZE - 1, -1, -1):          # BMP rows are bottom-up
        row = bytearray()
        for x in range(SIZE):
            b, g, r, a = _sample(x, y)
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    return b''.join(rows)


def build_ico() -> bytes:
    pixels = build_pixels()
    header = struct.pack('<IiiHHIIiiII', 40, SIZE, SIZE * 2, 1, 32, 0,
                         len(pixels), 0, 0, 0, 0)
    image = header + pixels
    # 1-bit AND mask, all zero (fully opaque); rows padded to 4 bytes.
    mask_row = ((SIZE + 31) // 32) * 4
    image += b'\x00' * (mask_row * SIZE)

    entry = struct.pack('<BBBBHHII', 0, 0, 0, 0, 1, 32, len(image), 22)
    directory = struct.pack('<HHH', 0, 1, 1)
    return directory + entry + image


def main() -> int:
    target = Path(__file__).resolve().parent / 'tunerlab.ico'
    target.write_bytes(build_ico())
    print(f'wrote {target} ({target.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
