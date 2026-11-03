from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

log = logging.getLogger(__name__)

ICON_DIR = Path(__file__).resolve().parent.parent / "resources" / "icons"
ICON_ALIASES = {"RareMaterials": "FacilityMaterials12"}

TILE = 64
LABEL = 18
TILE_HEIGHT = TILE + LABEL
GAP = 6
PADDING = 12
HEADER_HEIGHT = 30
COLUMNS = 10
MAX_TILES = 400

BACKGROUND = (18, 19, 21)
SLOT = (42, 45, 49)
SLOT_GONE = (30, 32, 35)
LABEL_BACK = (32, 34, 38)
BORDER = (62, 66, 72)
HEADER_TEXT = (201, 205, 210)
RULE = (58, 62, 68)
ICON = (235, 237, 240)
ICON_GONE = (105, 109, 115)
QUANTITY = (255, 255, 255)
OUTLINE = (0, 0, 0)
GAIN = (87, 242, 135)
LOSS = (237, 66, 69)


@dataclass
class Tile:
    code: str
    name: str
    quantity: int
    crated: bool = False
    delta: int | None = None
    gone: bool = False


def available() -> bool:
    return Image is not None


def format_quantity(value: int) -> str:
    value = int(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 10_000:
        return f"{sign}{value}"
    if value < 1_000_000:
        return f"{sign}{value / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{sign}{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"


def format_delta(value: int) -> str:
    return ("+" if value > 0 else "") + format_quantity(value)


def initials(name: str) -> str:
    words = [word for word in name.replace("-", " ").split() if word[:1].isalnum()]
    return "".join(word[0] for word in words[:3]).upper() or "?"


def icon_path(code: str, crated: bool) -> Path | None:
    for candidate in (code, ICON_ALIASES.get(code)):
        if not candidate:
            continue
        if crated:
            path = ICON_DIR / f"{candidate}-crated.png"
            if path.exists():
                return path
        path = ICON_DIR / f"{candidate}.png"
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1024)
def icon_mask(code: str, crated: bool, size: int):
    path = icon_path(code, crated)
    if path is None:
        return None
    try:
        with Image.open(path) as source:
            mask = source.convert("L")
    except OSError:
        log.warning("Could not read icon %s", path)
        return None
    if mask.size != (size, size):
        mask = mask.resize((size, size), Image.LANCZOS)
    return mask


@lru_cache(maxsize=16)
def font(size: int):
    return ImageFont.load_default(size=size)


def draw_tile(canvas, draw, tile: Tile, x: int, y: int) -> None:
    draw.rectangle((x, y, x + TILE - 1, y + TILE_HEIGHT - 1), fill=LABEL_BACK, outline=BORDER)
    draw.rectangle((x, y, x + TILE - 1, y + TILE - 1), fill=SLOT_GONE if tile.gone else SLOT, outline=BORDER)
    mask = icon_mask(tile.code, tile.crated, TILE)
    if mask is not None:
        canvas.paste(ICON_GONE if tile.gone else ICON, (x, y, x + TILE, y + TILE), mask)
    else:
        draw.text(
            (x + TILE // 2, y + TILE // 2),
            initials(tile.name),
            font=font(16),
            fill=ICON_GONE if tile.gone else ICON,
            anchor="mm",
        )
    draw.text(
        (x + TILE // 2, y + TILE + LABEL // 2),
        format_quantity(tile.quantity),
        font=font(14),
        fill=ICON_GONE if tile.gone else QUANTITY,
        anchor="mm",
    )
    if tile.delta:
        draw.text(
            (x + 4, y + 3),
            format_delta(tile.delta),
            font=font(13),
            fill=GAIN if tile.delta > 0 else LOSS,
            anchor="la",
            stroke_width=2,
            stroke_fill=OUTLINE,
        )


def render(sections: list[tuple[str, list[Tile]]], *, columns: int = COLUMNS) -> bytes | None:
    if not available():
        return None
    shown: list[tuple[str, list[Tile]]] = []
    budget = MAX_TILES
    hidden = 0
    for title, tiles in sections:
        if not tiles:
            continue
        kept = tiles[: max(0, budget)]
        hidden += len(tiles) - len(kept)
        budget -= len(kept)
        if kept:
            shown.append((title, kept))
    if not shown:
        return None
    width = PADDING * 2 + columns * TILE + (columns - 1) * GAP
    height = PADDING
    for _, tiles in shown:
        rows = math.ceil(len(tiles) / columns)
        height += HEADER_HEIGHT + rows * TILE_HEIGHT + (rows - 1) * GAP + PADDING
    if hidden:
        height += HEADER_HEIGHT
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    y = PADDING
    for title, tiles in shown:
        draw.text((PADDING, y + HEADER_HEIGHT - 10), title, font=font(16), fill=HEADER_TEXT, anchor="ls")
        draw.line((PADDING, y + HEADER_HEIGHT - 4, width - PADDING, y + HEADER_HEIGHT - 4), fill=RULE, width=1)
        y += HEADER_HEIGHT
        for index, tile in enumerate(tiles):
            row, column = divmod(index, columns)
            draw_tile(canvas, draw, tile, PADDING + column * (TILE + GAP), y + row * (TILE_HEIGHT + GAP))
        rows = math.ceil(len(tiles) / columns)
        y += rows * TILE_HEIGHT + (rows - 1) * GAP + PADDING
    if hidden:
        draw.text((PADDING, y + HEADER_HEIGHT - 12), f"...and {hidden} more not shown", font=font(15), fill=HEADER_TEXT, anchor="ls")
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


CARD_WIDTH = 132
CARD_HEIGHT = 136
CARD_COLUMNS = 5
MAX_CARDS = 50
SUBTITLE = (150, 155, 162)
FRESHNESS = [(87, 242, 135), (235, 237, 240), (255, 166, 0), (237, 66, 69)]


@dataclass
class Card:
    code: str
    name: str
    amount: str
    title: str
    subtitle: str = ""
    crated: bool = False
    badge: str = ""
    level: int = 0


def fit_text(draw, text: str, typeface, width: int) -> str:
    if draw.textlength(text, font=typeface) <= width:
        return text
    while text and draw.textlength(text + "...", font=typeface) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def draw_card(canvas, draw, card: Card, x: int, y: int) -> None:
    level = max(0, min(card.level, len(FRESHNESS) - 1))
    draw.rectangle((x, y, x + CARD_WIDTH - 1, y + CARD_HEIGHT - 1), fill=SLOT, outline=FRESHNESS[level] if level >= 2 else BORDER, width=2 if level >= 2 else 1)
    icon_x = x + (CARD_WIDTH - TILE) // 2
    icon_y = y + 10
    mask = icon_mask(card.code, card.crated, TILE)
    if mask is not None:
        canvas.paste(ICON, (icon_x, icon_y, icon_x + TILE, icon_y + TILE), mask)
    else:
        draw.text((icon_x + TILE // 2, icon_y + TILE // 2), initials(card.name), font=font(16), fill=ICON, anchor="mm")
    if card.badge:
        draw.text((x + 6, y + 5), card.badge, font=font(12), fill=FRESHNESS[level], anchor="la", stroke_width=2, stroke_fill=OUTLINE)
    middle = x + CARD_WIDTH // 2
    width = CARD_WIDTH - 10
    draw.text((middle, y + 88), fit_text(draw, card.amount, font(15), width), font=font(15), fill=QUANTITY, anchor="mm")
    draw.text((middle, y + 108), fit_text(draw, card.title, font(13), width), font=font(13), fill=HEADER_TEXT, anchor="mm")
    if card.subtitle:
        draw.text((middle, y + 124), fit_text(draw, card.subtitle, font(11), width), font=font(11), fill=SUBTITLE, anchor="mm")


def render_cards(cards: list[Card], *, columns: int = CARD_COLUMNS) -> bytes | None:
    if not available() or not cards:
        return None
    shown = cards[:MAX_CARDS]
    hidden = len(cards) - len(shown)
    columns = max(1, min(columns, len(shown)))
    rows = math.ceil(len(shown) / columns)
    width = PADDING * 2 + columns * CARD_WIDTH + (columns - 1) * GAP
    height = PADDING * 2 + rows * CARD_HEIGHT + (rows - 1) * GAP + (HEADER_HEIGHT if hidden else 0)
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    for index, card in enumerate(shown):
        row, column = divmod(index, columns)
        draw_card(canvas, draw, card, PADDING + column * (CARD_WIDTH + GAP), PADDING + row * (CARD_HEIGHT + GAP))
    if hidden:
        draw.text((PADDING, height - PADDING), f"...and {hidden} more not shown", font=font(15), fill=HEADER_TEXT, anchor="ls")
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_icon(code: str, name: str, *, crated: bool = False, size: int = 128) -> bytes | None:
    if not available():
        return None
    canvas = Image.new("RGB", (size, size), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    tile = Tile(code, name, 0, crated)
    draw.rectangle((0, 0, size - 1, size - 1), fill=SLOT, outline=BORDER)
    mask = icon_mask(tile.code, tile.crated, size)
    if mask is not None:
        canvas.paste(ICON, (0, 0, size, size), mask)
    else:
        draw.text((size // 2, size // 2), initials(name), font=font(size // 4), fill=ICON, anchor="mm")
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
