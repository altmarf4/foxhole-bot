from __future__ import annotations

import io
import json
import logging
from functools import lru_cache
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

log = logging.getLogger(__name__)

MAP_DIR = Path(__file__).resolve().parent.parent / "resources" / "maps"
HEX_OUTPUT_WIDTH = 900
WORLD_OUTPUT_WIDTH = 1600
JPEG_QUALITY = 85

BACKGROUND = (18, 19, 21)
DOT = (237, 66, 69)
RING = (255, 255, 255)
OUTLINE = (0, 0, 0)
APPROXIMATE = (255, 166, 0)
HIGHLIGHT = (255, 214, 10)
LABEL_TEXT = (255, 255, 255)
LABEL_BACK = (18, 19, 21, 200)
HEX_NAME = (245, 245, 240)


def available() -> bool:
    return Image is not None and (MAP_DIR / "layout.json").exists()


@lru_cache(maxsize=1)
def layout() -> dict:
    return json.loads((MAP_DIR / "layout.json").read_text(encoding="utf-8"))


def has_hex(map_key: str | None) -> bool:
    return bool(map_key) and (MAP_DIR / f"{map_key}.webp").exists()


@lru_cache(maxsize=6)
def hex_image(map_key: str):
    with Image.open(MAP_DIR / f"{map_key}.webp") as source:
        return source.convert("RGBA")


@lru_cache(maxsize=1)
def world_image():
    with Image.open(MAP_DIR / "world.webp") as source:
        return source.convert("RGBA")


@lru_cache(maxsize=8)
def font(size: int):
    return ImageFont.load_default(size=size)


def flatten(image):
    canvas = Image.new("RGBA", image.size, BACKGROUND + (255,))
    canvas.alpha_composite(image)
    return canvas


def to_jpeg(image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def draw_marker(canvas, x: float, y: float, size: float, approximate: bool) -> None:
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if approximate:
        halo = size * 3.2
        draw.ellipse((x - halo, y - halo, x + halo, y + halo), fill=APPROXIMATE + (70,), outline=APPROXIMATE + (230,), width=max(2, round(size / 3)))
        inner = size * 0.6
        draw.ellipse((x - inner, y - inner, x + inner, y + inner), fill=APPROXIMATE + (255,), outline=OUTLINE + (255,), width=1)
    else:
        glow = size * 2.2
        draw.ellipse((x - glow, y - glow, x + glow, y + glow), fill=DOT + (60,))
        ring = size + max(2, size / 3)
        draw.ellipse((x - ring - 1, y - ring - 1, x + ring + 1, y + ring + 1), fill=OUTLINE + (255,))
        draw.ellipse((x - ring, y - ring, x + ring, y + ring), fill=RING + (255,))
        draw.ellipse((x - size, y - size, x + size, y + size), fill=DOT + (255,))
    canvas.alpha_composite(overlay)


def draw_label(canvas, text: str, x: float, y: float, size: int, offset: float) -> None:
    if not text:
        return
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    typeface = font(size)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=typeface)
    width, height = right - left, bottom - top
    pad = max(4, size // 3)
    box_x = x + offset
    if box_x + width + pad * 2 > canvas.width - 4:
        box_x = x - offset - width - pad * 2
    box_x = max(4, box_x)
    box_y = min(max(4, y - height / 2 - pad), canvas.height - height - pad * 2 - 4)
    draw.rounded_rectangle((box_x, box_y, box_x + width + pad * 2, box_y + height + pad * 2), radius=pad, fill=LABEL_BACK)
    draw.text((box_x + pad - left, box_y + pad - top), text, font=typeface, fill=LABEL_TEXT)
    canvas.alpha_composite(overlay)


def render_hex(map_key: str, x: float | None, y: float | None, *, label: str = "", approximate: bool = False) -> bytes | None:
    if not available() or not has_hex(map_key):
        return None
    source = hex_image(map_key)
    height = round(source.height * HEX_OUTPUT_WIDTH / source.width)
    canvas = flatten(source.resize((HEX_OUTPUT_WIDTH, height), Image.LANCZOS))
    if x is not None and y is not None:
        px, py = x * canvas.width, y * canvas.height
        draw_marker(canvas, px, py, 9, approximate)
        draw_label(canvas, label, px, py, 22, 34 if approximate else 22)
    return to_jpeg(canvas)


def hexagon(cx: float, cy: float, width: float, height: float) -> list[tuple[float, float]]:
    return [
        (cx - width / 2, cy),
        (cx - width / 4, cy - height / 2),
        (cx + width / 4, cy - height / 2),
        (cx + width / 2, cy),
        (cx + width / 4, cy + height / 2),
        (cx - width / 4, cy + height / 2),
    ]


def render_world(
    map_key: str | None,
    x: float | None,
    y: float | None,
    *,
    label: str = "",
    approximate: bool = False,
    names: dict[str, str] | None = None,
) -> bytes | None:
    if not available():
        return None
    data = layout()
    canvas = flatten(world_image())
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    hex_width, hex_height = data["hex_width"], data["hex_height"]
    for key, centre in data["hexes"].items():
        name = (names or {}).get(key)
        if name:
            draw.text(
                (centre["cx"], centre["cy"]),
                name,
                font=font(15),
                fill=HEX_NAME + (225,),
                anchor="mm",
                stroke_width=2,
                stroke_fill=OUTLINE + (200,),
            )
    target = data["hexes"].get(map_key or "")
    if target is not None:
        draw.polygon(hexagon(target["cx"], target["cy"], hex_width, hex_height), outline=HIGHLIGHT + (255,), width=5)
    canvas.alpha_composite(overlay)
    if target is not None and x is not None and y is not None:
        px = target["cx"] + (x - 0.5) * hex_width
        py = target["cy"] + (y - 0.5) * hex_height
        draw_marker(canvas, px, py, 7, approximate)
        draw_label(canvas, label, px, py, 20, 30 if approximate else 20)
    if canvas.width > WORLD_OUTPUT_WIDTH:
        canvas = canvas.resize((WORLD_OUTPUT_WIDTH, round(canvas.height * WORLD_OUTPUT_WIDTH / canvas.width)), Image.LANCZOS)
    return to_jpeg(canvas)
