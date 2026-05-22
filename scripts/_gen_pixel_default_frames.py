#!/usr/bin/env python3
"""One-shot frame generator for assets/packs/pixel_default.

Produces six 8x8 RGBA PNGs that match the cat-face mask used by Swift's
``AvatarRenderer.pixelMask()`` and the mood palette in
``AvatarRgbColor`` (see DeskmateApp/Sources/DeskmateCore/AvatarRenderer.swift).

This script is intentionally hand-written (zlib + CRC32 only) so it
runs in a clean Python install with no Pillow dependency. The generated
files are checked into the repo; rerun this script only to refresh
them after a palette/mask edit.

Usage::

    python3 scripts/_gen_pixel_default_frames.py
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

# 8x8 mask — values match AvatarRenderer.pixelMask() exactly:
#   0 = transparent
#   1 = body colour
#   2 = accent colour (eyes / mouth)
_BASE_MASK: tuple[tuple[int, ...], ...] = (
    (0, 1, 0, 0, 0, 0, 1, 0),
    (1, 1, 1, 0, 0, 1, 1, 1),
    (1, 1, 1, 1, 1, 1, 1, 1),
    (1, 2, 1, 1, 1, 1, 2, 1),
    (1, 1, 1, 1, 1, 1, 1, 1),
    (1, 1, 1, 2, 2, 1, 1, 1),
    (0, 1, 1, 1, 1, 1, 1, 0),
    (0, 0, 1, 1, 1, 1, 0, 0),
)

# Palette mirrors the mood pairs in AvatarRenderer.swift.
# (body, accent) tuples expressed as (r, g, b).
_PALETTES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "idle":     ((170, 172, 178), (120, 124, 132)),  # gray / gray
    "working":  ((66, 133, 244),  (14, 182, 210)),    # blue / cyan
    "thinking": ((138, 94, 228),  (84, 58, 186)),     # purple / indigo
    "alert":    ((246, 156, 64),  (226, 74, 74)),     # orange / red
    "happy":    ((234, 120, 180), (246, 156, 64)),    # pink / orange
}


def _idle_blink_mask() -> tuple[tuple[int, ...], ...]:
    """Variant of the base mask with the eye row collapsed to body
    colour, giving a one-frame "blink" loop for the idle state."""
    rows = [list(row) for row in _BASE_MASK]
    # Row 3 originally has accent (eye) at columns 1 and 6. Promote them
    # back to body colour so the eyes disappear for one frame.
    eye_row = rows[3]
    for col in (1, 6):
        if eye_row[col] == 2:
            eye_row[col] = 1
    return tuple(tuple(row) for row in rows)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _encode_png_rgba(rows: list[bytes]) -> bytes:
    """Encode an RGBA image where each row is already 4*W bytes.
    Spec'd for a fixed 8x8 size; that's all we need."""
    width = len(rows[0]) // 4
    height = len(rows)
    raw = b"".join(b"\x00" + row for row in rows)  # filter byte = None per row
    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        8,   # bit depth
        6,   # colour type RGBA
        0,   # compression
        0,   # filter
        0,   # interlace
    )
    idat = zlib.compress(raw, level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _render(
    mask: tuple[tuple[int, ...], ...],
    palette: tuple[tuple[int, int, int], tuple[int, int, int]],
) -> bytes:
    body, accent = palette
    rows: list[bytes] = []
    for row in mask:
        pixels = bytearray()
        for cell in row:
            if cell == 0:
                pixels.extend((0, 0, 0, 0))
            elif cell == 1:
                pixels.extend((body[0], body[1], body[2], 255))
            elif cell == 2:
                pixels.extend((accent[0], accent[1], accent[2], 255))
            else:  # pragma: no cover — unreachable for the static mask
                raise ValueError(f"unknown mask cell {cell!r}")
        rows.append(bytes(pixels))
    return _encode_png_rgba(rows)


def main() -> int:
    pack_root = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "packs"
        / "pixel_default"
    )
    if not pack_root.is_dir():
        sys.stderr.write(f"pack root missing: {pack_root}\n")
        return 1

    targets = {
        "idle/000.png":     _render(_BASE_MASK, _PALETTES["idle"]),
        "idle/001.png":     _render(_idle_blink_mask(), _PALETTES["idle"]),
        "working/000.png":  _render(_BASE_MASK, _PALETTES["working"]),
        "thinking/000.png": _render(_BASE_MASK, _PALETTES["thinking"]),
        "alert/000.png":    _render(_BASE_MASK, _PALETTES["alert"]),
        "happy/000.png":    _render(_BASE_MASK, _PALETTES["happy"]),
    }

    for rel, data in targets.items():
        path = pack_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path.relative_to(pack_root.parent.parent.parent)} ({len(data)}B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
