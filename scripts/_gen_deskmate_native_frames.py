#!/usr/bin/env python3
"""Generate the built-in 48x48 Deskmate native sprite pack.

The frames are deliberately generated from simple pixel primitives so the
native pack is editable, reproducible, and license-clean. It is not trying to
be photorealistic; the goal is a readable desktop companion with obvious
motion at the small 88 px render size used by the overlay.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Iterable

W = 48
H = 48

RGBA = tuple[int, int, int, int]

TRANSPARENT: RGBA = (0, 0, 0, 0)
OUTLINE: RGBA = (26, 34, 42, 255)
SHADOW: RGBA = (18, 24, 30, 92)
BODY: RGBA = (44, 184, 190, 255)
BODY_DARK: RGBA = (28, 126, 144, 255)
BODY_LIGHT: RGBA = (92, 224, 216, 255)
FACE: RGBA = (242, 252, 238, 255)
EYE: RGBA = (18, 34, 38, 255)
PINK: RGBA = (246, 112, 164, 255)
YELLOW: RGBA = (255, 206, 88, 255)
ORANGE: RGBA = (255, 150, 78, 255)
RED: RGBA = (226, 74, 74, 255)
BLUE: RGBA = (72, 142, 246, 255)
PURPLE: RGBA = (148, 104, 232, 255)
GREEN: RGBA = (94, 214, 128, 255)
WHITE: RGBA = (255, 255, 255, 255)


class Canvas:
    def __init__(self) -> None:
        self.pixels: list[list[RGBA]] = [
            [TRANSPARENT for _ in range(W)] for _ in range(H)
        ]

    def px(self, x: int, y: int, color: RGBA) -> None:
        if 0 <= x < W and 0 <= y < H:
            self.pixels[y][x] = color

    def rect(self, x: int, y: int, w: int, h: int, color: RGBA) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.px(xx, yy, color)

    def rounded_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        r: int,
        color: RGBA,
    ) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                dx = min(xx - x, x + w - 1 - xx)
                dy = min(yy - y, y + h - 1 - yy)
                if dx >= r or dy >= r or (dx - r + 1) ** 2 + (dy - r + 1) ** 2 <= r ** 2:
                    self.px(xx, yy, color)

    def line(self, x0: int, y0: int, x1: int, y1: int, color: RGBA) -> None:
        dx = abs(x1 - x0)
        sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        x, y = x0, y0
        while True:
            self.px(x, y, color)
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x += sx
            if e2 <= dx:
                err += dx
                y += sy

    def circle(self, cx: int, cy: int, r: int, color: RGBA) -> None:
        rr = r * r
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= rr:
                    self.px(x, y, color)

    def rows(self) -> list[bytes]:
        out: list[bytes] = []
        for row in self.pixels:
            b = bytearray()
            for r, g, b_, a in row:
                b.extend((r, g, b_, a))
            out.append(bytes(b))
        return out


def chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, level=9))
        + chunk(b"IEND", b"")
    )


def draw_shadow(c: Canvas, y: int = 40, w: int = 24, alpha: int = 92) -> None:
    color = (SHADOW[0], SHADOW[1], SHADOW[2], alpha)
    c.rounded_rect(24 - w // 2, y, w, 4, 2, color)


def draw_z(c: Canvas, x: int, y: int, scale: int = 1) -> None:
    c.rect(x, y, 4 * scale, scale, BLUE)
    c.line(x + 3 * scale, y, x, y + 3 * scale, BLUE)
    c.rect(x, y + 3 * scale, 4 * scale, scale, BLUE)


def draw_spark(c: Canvas, x: int, y: int, color: RGBA = YELLOW) -> None:
    c.line(x, y - 3, x, y + 3, color)
    c.line(x - 3, y, x + 3, y, color)
    c.px(x - 1, y - 1, color)
    c.px(x + 1, y - 1, color)
    c.px(x - 1, y + 1, color)
    c.px(x + 1, y + 1, color)


def draw_body(
    c: Canvas,
    *,
    ox: int = 0,
    oy: int = 0,
    eye: str = "open",
    mouth: str = "smile",
    arm: str = "rest",
    legs: str = "idle",
    accessory: str | None = None,
    body: RGBA = BODY,
    blush: bool = False,
) -> None:
    # Ears / antenna pads.
    c.rounded_rect(11 + ox, 9 + oy, 8, 8, 2, OUTLINE)
    c.rounded_rect(29 + ox, 9 + oy, 8, 8, 2, OUTLINE)
    c.rounded_rect(13 + ox, 11 + oy, 4, 4, 1, BODY_LIGHT)
    c.rounded_rect(31 + ox, 11 + oy, 4, 4, 1, BODY_LIGHT)

    # Arms behind body.
    if arm == "wave1":
        c.rounded_rect(35 + ox, 19 + oy, 5, 11, 2, OUTLINE)
        c.rounded_rect(37 + ox, 13 + oy, 5, 10, 2, OUTLINE)
        c.rounded_rect(38 + ox, 14 + oy, 2, 8, 1, body)
    elif arm == "wave2":
        c.rounded_rect(35 + ox, 17 + oy, 5, 11, 2, OUTLINE)
        c.rounded_rect(39 + ox, 9 + oy, 5, 10, 2, OUTLINE)
        c.rounded_rect(40 + ox, 10 + oy, 2, 8, 1, body)
    elif arm == "drag":
        c.rounded_rect(9 + ox, 15 + oy, 5, 15, 2, OUTLINE)
        c.rounded_rect(34 + ox, 15 + oy, 5, 15, 2, OUTLINE)
        c.rounded_rect(10 + ox, 16 + oy, 3, 12, 1, body)
        c.rounded_rect(35 + ox, 16 + oy, 3, 12, 1, body)
    else:
        c.rounded_rect(8 + ox, 23 + oy, 6, 10, 2, OUTLINE)
        c.rounded_rect(34 + ox, 23 + oy, 6, 10, 2, OUTLINE)
        c.rounded_rect(9 + ox, 24 + oy, 3, 7, 1, body)
        c.rounded_rect(36 + ox, 24 + oy, 3, 7, 1, body)

    # Main shell.
    c.rounded_rect(12 + ox, 12 + oy, 24, 26, 5, OUTLINE)
    c.rounded_rect(14 + ox, 14 + oy, 20, 22, 4, body)
    c.rect(17 + ox, 15 + oy, 14, 2, BODY_LIGHT)
    c.rect(32 + ox, 18 + oy, 2, 13, BODY_DARK)

    # Face screen.
    c.rounded_rect(17 + ox, 19 + oy, 14, 11, 3, OUTLINE)
    c.rounded_rect(18 + ox, 20 + oy, 12, 9, 2, FACE)

    if eye == "blink":
        c.rect(20 + ox, 24 + oy, 3, 1, EYE)
        c.rect(26 + ox, 24 + oy, 3, 1, EYE)
    elif eye == "sleep":
        c.line(20 + ox, 24 + oy, 23 + ox, 25 + oy, EYE)
        c.line(26 + ox, 25 + oy, 29 + ox, 24 + oy, EYE)
    elif eye == "x":
        for ex in (21 + ox, 27 + ox):
            c.line(ex - 2, 22 + oy, ex + 1, 25 + oy, EYE)
            c.line(ex + 1, 22 + oy, ex - 2, 25 + oy, EYE)
    elif eye == "look-left":
        c.rect(19 + ox, 22 + oy, 2, 4, EYE)
        c.rect(25 + ox, 22 + oy, 2, 4, EYE)
    elif eye == "look-right":
        c.rect(22 + ox, 22 + oy, 2, 4, EYE)
        c.rect(28 + ox, 22 + oy, 2, 4, EYE)
    else:
        c.rect(20 + ox, 22 + oy, 2, 5, EYE)
        c.rect(27 + ox, 22 + oy, 2, 5, EYE)

    if blush:
        c.rect(18 + ox, 27 + oy, 2, 1, PINK)
        c.rect(29 + ox, 27 + oy, 2, 1, PINK)

    if mouth == "o":
        c.rect(24 + ox, 27 + oy, 2, 2, EYE)
    elif mouth == "flat":
        c.rect(22 + ox, 28 + oy, 5, 1, EYE)
    elif mouth == "sad":
        c.rect(22 + ox, 28 + oy, 5, 1, EYE)
        c.px(21 + ox, 29 + oy, EYE)
        c.px(27 + ox, 29 + oy, EYE)
    elif mouth == "smile":
        c.rect(22 + ox, 27 + oy, 5, 1, EYE)
        c.px(21 + ox, 26 + oy, EYE)
        c.px(27 + ox, 26 + oy, EYE)

    if accessory == "magnifier":
        c.circle(35 + ox, 24 + oy, 4, OUTLINE)
        c.circle(35 + ox, 24 + oy, 2, (190, 235, 255, 210))
        c.line(38 + ox, 27 + oy, 42 + ox, 31 + oy, OUTLINE)
    elif accessory == "exclaim":
        c.rect(38 + ox, 15 + oy, 3, 10, YELLOW)
        c.rect(38 + ox, 27 + oy, 3, 3, YELLOW)
        c.rect(37 + ox, 14 + oy, 5, 1, OUTLINE)
        c.rect(37 + ox, 30 + oy, 5, 1, OUTLINE)
    elif accessory == "code":
        c.rect(8 + ox, 16 + oy, 7, 2, GREEN)
        c.rect(7 + ox, 18 + oy, 5, 2, GREEN)
        c.rect(8 + ox, 20 + oy, 7, 2, GREEN)

    # Feet in front.
    if legs == "run1":
        c.rounded_rect(15 + ox, 35 + oy, 7, 5, 2, OUTLINE)
        c.rounded_rect(27 + ox, 37 + oy, 8, 4, 2, OUTLINE)
        c.rect(17 + ox, 36 + oy, 4, 2, body)
        c.rect(29 + ox, 38 + oy, 4, 1, body)
    elif legs == "run2":
        c.rounded_rect(13 + ox, 37 + oy, 8, 4, 2, OUTLINE)
        c.rounded_rect(28 + ox, 35 + oy, 7, 5, 2, OUTLINE)
        c.rect(15 + ox, 38 + oy, 4, 1, body)
        c.rect(30 + ox, 36 + oy, 4, 2, body)
    elif legs == "tuck":
        c.rounded_rect(16 + ox, 35 + oy, 6, 3, 1, OUTLINE)
        c.rounded_rect(27 + ox, 35 + oy, 6, 3, 1, OUTLINE)
    else:
        c.rounded_rect(16 + ox, 36 + oy, 6, 4, 2, OUTLINE)
        c.rounded_rect(27 + ox, 36 + oy, 6, 4, 2, OUTLINE)
        c.rect(18 + ox, 37 + oy, 3, 1, body)
        c.rect(29 + ox, 37 + oy, 3, 1, body)


def frame(
    *,
    ox: int = 0,
    oy: int = 0,
    eye: str = "open",
    mouth: str = "smile",
    arm: str = "rest",
    legs: str = "idle",
    accessory: str | None = None,
    body: RGBA = BODY,
    blush: bool = False,
    shadow_y: int = 41,
    shadow_w: int = 24,
    extras: Iterable[tuple[str, tuple[int, ...]]] = (),
) -> Canvas:
    c = Canvas()
    draw_shadow(c, y=shadow_y, w=shadow_w)
    draw_body(
        c,
        ox=ox,
        oy=oy,
        eye=eye,
        mouth=mouth,
        arm=arm,
        legs=legs,
        accessory=accessory,
        body=body,
        blush=blush,
    )
    for kind, args in extras:
        if kind == "spark":
            draw_spark(c, args[0], args[1])
        elif kind == "z":
            draw_z(c, args[0], args[1], args[2])
    return c


def failed_frame(phase: int) -> Canvas:
    c = Canvas()
    draw_shadow(c, y=41, w=22)
    draw_body(
        c,
        body=RED if phase % 2 == 0 else ORANGE,
        eye="x",
        mouth="sad",
        arm="rest",
        legs="tuck",
    )
    c.rect(7, 12, 5, 4, RED)
    c.rect(37, 12, 5, 4, RED)
    return c


def sleeping_frame(phase: int) -> Canvas:
    c = frame(oy=4 + phase, eye="sleep", mouth="flat", legs="tuck", shadow_w=28)
    draw_z(c, 35, 10 - phase, 1)
    draw_z(c, 39, 4 - phase, 1)
    return c


def write_state(root: Path, state: str, frames: list[Canvas]) -> list[str]:
    out_dir = root / state
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for idx, canvas in enumerate(frames):
        rel = f"{state}/{idx:03d}.png"
        (root / rel).write_bytes(encode_png(canvas.rows()))
        paths.append(rel)
    return paths


def main() -> int:
    pack_root = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "packs"
        / "deskmate_native"
    )
    pack_root.mkdir(parents=True, exist_ok=True)

    states: dict[str, dict[str, object]] = {}
    specs: dict[str, tuple[int, list[Canvas]]] = {
        "idle": (
            5,
            [
                frame(),
                frame(oy=-1),
                frame(),
                frame(eye="blink"),
                frame(eye="look-left"),
                frame(eye="look-right"),
            ],
        ),
        "running": (
            9,
            [
                frame(ox=-1, oy=0, legs="run1", arm="drag"),
                frame(ox=1, oy=-1, legs="run2", arm="drag"),
                frame(ox=0, oy=0, legs="run1", arm="drag"),
                frame(ox=-1, oy=-1, legs="run2", arm="drag"),
            ],
        ),
        "running-right": (
            9,
            [
                frame(ox=1, legs="run1", eye="look-right"),
                frame(ox=2, oy=-1, legs="run2", eye="look-right"),
                frame(ox=1, legs="run1", eye="look-right"),
                frame(ox=0, oy=-1, legs="run2", eye="look-right"),
            ],
        ),
        "running-left": (
            9,
            [
                frame(ox=-1, legs="run2", eye="look-left"),
                frame(ox=-2, oy=-1, legs="run1", eye="look-left"),
                frame(ox=-1, legs="run2", eye="look-left"),
                frame(ox=0, oy=-1, legs="run1", eye="look-left"),
            ],
        ),
        "review": (
            6,
            [
                frame(accessory="magnifier", eye="look-right", mouth="flat"),
                frame(oy=-1, accessory="magnifier", eye="look-right", mouth="flat"),
                frame(accessory="code", eye="look-left", mouth="flat"),
                frame(eye="blink", accessory="code", mouth="flat"),
            ],
        ),
        "waiting": (
            6,
            [
                frame(accessory="exclaim", mouth="o", body=ORANGE),
                frame(ox=-1, accessory="exclaim", mouth="o", body=ORANGE),
                frame(ox=1, accessory="exclaim", mouth="flat", body=ORANGE),
                frame(eye="blink", accessory="exclaim", mouth="flat", body=ORANGE),
            ],
        ),
        "waving": (
            7,
            [
                frame(arm="wave1", blush=True),
                frame(arm="wave2", oy=-1, blush=True),
                frame(arm="wave1", blush=True),
                frame(arm="wave2", oy=-1, blush=True),
            ],
        ),
        "jumping": (
            8,
            [
                frame(oy=0, shadow_w=24),
                frame(oy=-5, legs="tuck", shadow_w=18),
                frame(oy=-9, legs="tuck", shadow_w=12, extras=(("spark", (11, 17)),)),
                frame(oy=-5, legs="tuck", shadow_w=18),
                frame(oy=0, shadow_w=24),
            ],
        ),
        "failed": (5, [failed_frame(0), failed_frame(1), failed_frame(0)]),
        "dozing": (
            2,
            [
                frame(oy=2, eye="sleep", mouth="flat", legs="tuck", extras=(("z", (36, 10, 1)),)),
                frame(oy=3, eye="sleep", mouth="flat", legs="tuck", extras=(("z", (38, 8, 1)),)),
            ],
        ),
        "sleeping": (2, [sleeping_frame(0), sleeping_frame(1)]),
        "waking": (
            6,
            [
                frame(oy=4, eye="sleep", mouth="flat", legs="tuck"),
                frame(oy=1, eye="blink", mouth="o", arm="wave1"),
                frame(oy=-3, eye="open", mouth="smile", arm="wave2", extras=(("spark", (38, 12)),)),
            ],
        ),
        "drag": (
            8,
            [
                frame(oy=-4, eye="open", mouth="o", arm="drag", legs="tuck", shadow_w=14),
                frame(oy=-5, ox=1, mouth="o", arm="drag", legs="tuck", shadow_w=12),
            ],
        ),
        "react-click": (
            8,
            [
                frame(oy=-2, mouth="o", blush=True, extras=(("spark", (10, 14)),)),
                frame(oy=-6, mouth="smile", blush=True, arm="wave2", extras=(("spark", (38, 11)),)),
                frame(oy=-2, mouth="smile", blush=True),
            ],
        ),
        # Legacy names are real states so older callers never lose motion.
        "working": (6, [frame(accessory="code"), frame(oy=-1, accessory="code")]),
        "thinking": (5, [frame(accessory="magnifier"), frame(eye="blink", accessory="magnifier")]),
        "alert": (5, [frame(accessory="exclaim", body=ORANGE), frame(ox=1, accessory="exclaim", body=ORANGE)]),
        "happy": (6, [frame(blush=True, arm="wave1"), frame(oy=-2, blush=True, arm="wave2")]),
    }

    for state, (fps, frames) in specs.items():
        states[state] = {
            "fps": fps,
            "frames": write_state(pack_root, state, frames),
        }

    manifest = {
        "spec_version": 1,
        "id": "deskmate_native",
        "display_name": "Deskmate Native",
        "author": "Deskmate",
        "canvas_size": [48, 48],
        "scale": 1,
        "palette": [
            "#1a222a", "#2cb8be", "#5ce0d8", "#f2fcee",
            "#ffce58", "#ff964e", "#e24a4a", "#9468e8",
        ],
        "avatar": {
            "default_style": "pixel",
            "supported_styles": ["pixel", "emoji"],
        },
        "states": states,
        "required_states": [
            "idle", "running", "review", "waiting", "waving", "jumping",
            "failed", "dozing", "sleeping", "waking", "drag", "react-click",
        ],
        "bubble_config": {
            "icon": "chat",
            "templates": {
                "greeting": "hi",
                "idle_nudge": "still here :)",
            },
        },
        "fallbacks": {
            "working": "running",
            "thinking": "review",
            "alert": "waiting",
            "happy": "jumping",
            "dozing": "idle",
            "sleeping": "idle",
            "waking": "jumping",
            "drag": "running",
            "react-click": "waving",
            "petting": "waving",
            "editing": "running",
            "testing": "waiting",
            "success": "jumping",
            "error": "failed",
            "celebrating": "jumping",
            "notification": "waving",
            "walking": "running",
            "walking_left": "running-left",
            "walking_right": "running-right",
            "running_left": "running-left",
            "running_right": "running-right",
        },
        "capabilities": [
            "chat", "proactive_nudge", "universal_sprite_states",
            "direct_reactions", "auto_rest", "native_sprite",
        ],
    }
    (pack_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {pack_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
