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
TOWN_SIZE = 18


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


def label_box(canvas, text: str, x: float, y: float, size: int, offset: float):
    if not text:
        return None
    left, top, right, bottom = font(size).getbbox(text)
    width, height = right - left, bottom - top
    pad = max(4, size // 3)
    box_x = x + offset
    if box_x + width + pad * 2 > canvas.width - 4:
        box_x = x - offset - width - pad * 2
    box_x = max(4, box_x)
    box_y = min(max(4, y - height / 2 - pad), canvas.height - height - pad * 2 - 4)
    return (box_x, box_y, box_x + width + pad * 2, box_y + height + pad * 2), left, top, pad


def draw_label(canvas, text: str, x: float, y: float, size: int, offset: float) -> None:
    placed = label_box(canvas, text, x, y, size, offset)
    if placed is None:
        return
    box, left, top, pad = placed
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(box, radius=pad, fill=LABEL_BACK)
    draw.text((box[0] + pad - left, box[1] + pad - top), text, font=font(size), fill=LABEL_TEXT)
    canvas.alpha_composite(overlay)


def draw_towns(canvas, towns: list[tuple[str, float, float]], avoid: list[tuple[float, float, float, float]]) -> None:
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    typeface = font(TOWN_SIZE)
    taken = list(avoid)
    for name, x, y in towns:
        for shift in (0, -TOWN_SIZE * 1.6, TOWN_SIZE * 1.6):
            px, py = x * canvas.width, y * canvas.height + shift
            rect = draw.textbbox((px, py), name, font=typeface, anchor="mm", stroke_width=2)
            inside = rect[0] >= 2 and rect[1] >= 2 and rect[2] <= canvas.width - 2 and rect[3] <= canvas.height - 2
            if inside and not any(overlaps(rect, other) for other in taken):
                taken.append(rect)
                draw.text((px, py), name, font=typeface, fill=HEX_NAME + (235,), anchor="mm", stroke_width=2, stroke_fill=OUTLINE + (190,))
                break
    canvas.alpha_composite(overlay)


def render_hex(
    map_key: str,
    x: float | None,
    y: float | None,
    *,
    label: str = "",
    approximate: bool = False,
    towns: list[tuple[str, float, float]] | None = None,
) -> bytes | None:
    if not available() or not has_hex(map_key):
        return None
    source = hex_image(map_key)
    height = round(source.height * HEX_OUTPUT_WIDTH / source.width)
    canvas = flatten(source.resize((HEX_OUTPUT_WIDTH, height), Image.LANCZOS))
    marked = x is not None and y is not None
    offset = 34 if approximate else 22
    avoid = []
    if marked:
        px, py = x * canvas.width, y * canvas.height
        reach = 9 * (3.2 if approximate else 2.2)
        avoid.append((px - reach, py - reach, px + reach, py + reach))
        placed = label_box(canvas, label, px, py, 22, offset)
        if placed is not None:
            avoid.append(placed[0])
    draw_towns(canvas, towns or [], avoid)
    if marked:
        draw_marker(canvas, px, py, 9, approximate)
        draw_label(canvas, label, px, py, 22, offset)
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


def label_text(labels: list[str]) -> str:
    unique = list(dict.fromkeys(label for label in labels if label))
    text = ", ".join(unique[:3])
    if len(unique) > 3:
        text += f" +{len(unique) - 3} more"
    return text


def overlaps(first: tuple[float, float, float, float], second: tuple[float, float, float, float], margin: float = 3) -> bool:
    return not (
        first[2] + margin <= second[0]
        or second[2] + margin <= first[0]
        or first[3] + margin <= second[1]
        or second[3] + margin <= first[1]
    )


def place_labels(
    canvas,
    items: list[tuple[str, float, float, float]],
    size: int,
    obstacles: list[tuple[float, float, float, float]] | None = None,
) -> None:
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    typeface = font(size)
    pad = max(4, size // 3)
    taken: list[tuple[float, float, float, float]] = list(obstacles or [])
    for text, x, y, offset in items:
        if not text:
            continue
        left, top, right, bottom = draw.textbbox((0, 0), text, font=typeface)
        width, height = right - left + pad * 2, bottom - top + pad * 2
        candidates = [
            (x + offset, y - height / 2),
            (x - offset - width, y - height / 2),
            (x - width / 2, y - offset - height),
            (x - width / 2, y + offset),
        ]
        chosen = None
        for box_x, box_y in candidates:
            box_x = min(max(4, box_x), canvas.width - width - 4)
            box_y = min(max(4, box_y), canvas.height - height - 4)
            rect = (box_x, box_y, box_x + width, box_y + height)
            if chosen is None:
                chosen = rect
            if not any(overlaps(rect, other) for other in taken):
                chosen = rect
                break
        taken.append(chosen)
        draw.rounded_rectangle(chosen, radius=pad, fill=LABEL_BACK)
        draw.text((chosen[0] + pad - left, chosen[1] + pad - top), text, font=typeface, fill=LABEL_TEXT)
    canvas.alpha_composite(overlay)


def render_world_points(
    points: list[tuple[str | None, float | None, float | None, bool, str]],
    *,
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
            draw.text((centre["cx"], centre["cy"]), name, font=font(15), fill=HEX_NAME + (190,), anchor="mm", stroke_width=2, stroke_fill=OUTLINE + (170,))
    used = {key for key, _, _, _, _ in points if key in data["hexes"]}
    for key in used:
        centre = data["hexes"][key]
        draw.polygon(hexagon(centre["cx"], centre["cy"], hex_width, hex_height), outline=HIGHLIGHT + (200,), width=3)
    canvas.alpha_composite(overlay)
    clusters: list[dict] = []
    for key, x, y, approximate, label in points:
        centre = data["hexes"].get(key or "")
        if centre is None or x is None or y is None:
            continue
        px = centre["cx"] + (x - 0.5) * hex_width
        py = centre["cy"] + (y - 0.5) * hex_height
        for cluster in clusters:
            if abs(cluster["x"] - px) < 16 and abs(cluster["y"] - py) < 16:
                cluster["labels"].append(label)
                cluster["approximate"] = cluster["approximate"] and approximate
                break
        else:
            clusters.append({"x": px, "y": py, "labels": [label], "approximate": approximate})
    dots = []
    for cluster in clusters:
        draw_marker(canvas, cluster["x"], cluster["y"], 6, cluster["approximate"])
        radius = 19 if cluster["approximate"] else 10
        dots.append((cluster["x"] - radius, cluster["y"] - radius, cluster["x"] + radius, cluster["y"] + radius))
    place_labels(
        canvas,
        [(label_text(c["labels"]), c["x"], c["y"], 22 if c["approximate"] else 16) for c in clusters],
        17,
        dots,
    )
    if canvas.width > WORLD_OUTPUT_WIDTH:
        canvas = canvas.resize((WORLD_OUTPUT_WIDTH, round(canvas.height * WORLD_OUTPUT_WIDTH / canvas.width)), Image.LANCZOS)
    return to_jpeg(canvas)
