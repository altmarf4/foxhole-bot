from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import PRIORITIES, Colour
from foxbot.core import entries, itemimages, mapimages, timeutil
from foxbot.core import settings as keys
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.catalog import CATEGORY_ORDER, Catalog, Item, search_key
from foxbot.core.locations import Location
from foxbot.core.permissions import check_admin, check_group, interaction_member
from foxbot.core.text import chunk_lines, clip, fit_board_embeds, md, paginate_embeds, plural
from foxbot.core.ui import (
    BaseModal,
    BaseView,
    EmbedPaginator,
    PickerView,
    PickOption,
    deny,
    edit,
    option,
    reply,
    select_field,
    select_value,
    text_field,
    text_value,
)
from foxbot.features.inventory.parsers import (
    FACTIONS,
    SOURCE_LABELS,
    ParsedStock,
    ParseError,
    canonical_store_type,
    decode_upload,
    format_game_time,
    normalise,
    parse_inventory,
    stock_key,
)

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

KIND = "inventory"
GROUP = "inventory"
STOCKPILE_GROUP = "stockpile"
STOCKPILES_EXTENSION = "foxbot.features.stockpiles"
KEEP_SNAPSHOTS = 50
HISTORY_OPTIONS = 25
PASTE_LIMIT = 4000
UPLOAD_LIMIT = 1024 * 1024
UPLOAD_EXTENSIONS = (".csv", ".tsv", ".txt")
MAX_STOCKS_PER_IMPORT = 25
TAB_INVENTORY = "inventory"
TAB_CHANGES = "changes"
TAB_MAP = "map"
TAB_LABELS = {TAB_INVENTORY: "Inventory", TAB_CHANGES: "Changes", TAB_MAP: "Map"}
MAP_HEX = "hex"
MAP_WORLD = "world"
TAB_PAGE_BUDGET = 3000
NAME_WIDTH = 24
TABLE_HEADER = f"{'Item':<{NAME_WIDTH}} {'Crates':>6} {'Loose':>6} {'Total':>9}"
UNRECOGNISED = "Unrecognised"
GRID_FILENAME = "inventory.png"
MAP_FILENAME = "map.jpg"
ICON_FILENAME = "item.png"
CATEGORIES = [*CATEGORY_ORDER, UNRECOGNISED]
STOCKPILE_PREFIX = "sp:"
SNAPSHOT_PREFIX = "sn:"
EXPORT_COLUMNS = [
    "Stockpile",
    "Hex",
    "Town",
    "Type",
    "Item",
    "Code",
    "Category",
    "Crated",
    "Quantity",
    "Per Crate",
    "Total Units",
    "Imported At",
    "Imported By",
]
CSV_STOCKPILE_HELP = (
    "In game, open the stockpile, press **Copy to Clipboard** and paste it below. The name, hex, town and storage type "
    "are read from the first line. The reserve code is not part of the export, so type it here."
)
PASTE_HELP = (
    "In game, open the stockpile, press **Copy to Clipboard**, then paste here.\n"
    "FIR and Foxhole Stockpiles app exports work too. For long lists use `/inventory upload` with a file."
)
BOARD_HELP = (
    "Press **Import Contents** and paste a stockpile's in-game **Copy to Clipboard** text, or upload a FIR / "
    "Foxhole Stockpiles export with `/inventory upload`. **Find Item**, **Totals** and **Export CSV** answer only you."
)


@dataclass
class Holding:
    code: str
    name: str
    category: str
    crates: int = 0
    loose: int = 0
    units: int = 0
    stockpiles: set[int] = field(default_factory=set)


@dataclass
class Change:
    name: str
    crated: bool
    old: int
    new: int
    weight: int

    @property
    def delta(self) -> int:
        return self.new - self.old


@dataclass
class ImportOutcome:
    snapshot_id: int
    hex: str
    region: str
    store_type: str
    name: str
    known_location: bool
    unknown: list[str]
    warning: str | None = None
    kept: bool = False


def stockpiles_module(bot: FoxBot):
    return bot.extensions.get(STOCKPILES_EXTENSION)


async def guild_faction(bot: FoxBot, guild_id: int) -> str | None:
    value = await bot.settings.get(guild_id, keys.FACTION)
    value = str(value).casefold() if value else None
    return value if value in FACTIONS else None


async def fetch_snapshot(bot: FoxBot, guild_id: int, snapshot_id: int) -> dict | None:
    return await bot.db.fetchone(
        "SELECT * FROM inventory_snapshots WHERE guild_id = ? AND id = ?",
        (guild_id, snapshot_id),
    )


async def fetch_items(bot: FoxBot, snapshot_id: int) -> list[dict]:
    return await bot.db.fetchall("SELECT * FROM inventory_items WHERE snapshot_id = ?", (snapshot_id,))


async def items_for(bot: FoxBot, snapshot_ids: list[int]) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {snapshot_id: [] for snapshot_id in snapshot_ids}
    for start in range(0, len(snapshot_ids), 500):
        chunk = snapshot_ids[start : start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        rows = await bot.db.fetchall(f"SELECT * FROM inventory_items WHERE snapshot_id IN ({placeholders})", tuple(chunk))
        for row in rows:
            result[row["snapshot_id"]].append(row)
    return result


async def fetch_stockpile(bot: FoxBot, guild_id: int, stockpile_id: str) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM stockpiles WHERE guild_id = ? AND id = ?", (guild_id, stockpile_id))


async def guild_stockpiles(bot: FoxBot, guild_id: int) -> dict[str, dict]:
    rows = await bot.db.fetchall("SELECT * FROM stockpiles WHERE guild_id = ?", (guild_id,))
    return {row["id"]: row for row in rows}


async def prune(tx, guild_id: int, key: str, stockpile_id: str | None) -> None:
    await tx.execute(
        "DELETE FROM inventory_snapshots WHERE guild_id = ? AND stock_key = ? AND id NOT IN ("
        "SELECT id FROM inventory_snapshots WHERE guild_id = ? AND stock_key = ? ORDER BY imported_at DESC, id DESC LIMIT ?)",
        (guild_id, key, guild_id, key, KEEP_SNAPSHOTS),
    )
    if stockpile_id is not None:
        await tx.execute(
            "DELETE FROM inventory_snapshots WHERE guild_id = ? AND stockpile_id = ? AND id NOT IN ("
            "SELECT id FROM inventory_snapshots WHERE guild_id = ? AND stockpile_id = ? ORDER BY imported_at DESC, id DESC LIMIT ?)",
            (guild_id, stockpile_id, guild_id, stockpile_id, KEEP_SNAPSHOTS),
        )


async def save_snapshot(
    bot: FoxBot,
    guild_id: int,
    stock: ParsedStock,
    *,
    source: str,
    faction: str | None,
    stockpile_id: str | None,
    user_id: int,
    adopt_history: bool = True,
    now: int | None = None,
) -> int:
    now = now if now is not None else timeutil.now()
    key = stock_key(stock.hex, stock.region, stock.store_type, stock.name)
    async with bot.db.transaction() as tx:
        cursor = await tx.execute(
            "INSERT INTO inventory_snapshots (guild_id, stockpile_id, stock_key, hex, region, store_type, name, source, "
            "faction, game_time, imported_by, imported_at, map_x, map_y) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                stockpile_id,
                key,
                stock.hex,
                stock.region,
                stock.store_type,
                stock.name,
                source,
                faction,
                stock.game_time,
                user_id,
                now,
                stock.map_x,
                stock.map_y,
            ),
        )
        snapshot_id = cursor.lastrowid
        await cursor.close()
        await tx.executemany(
            "INSERT INTO inventory_items (snapshot_id, item_code, item_name, category, crated, quantity, per_crate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (snapshot_id, item.code, item.name, item.category, int(item.crated), item.quantity, item.per_crate)
                for item in stock.items
            ],
        )
        if stockpile_id is not None and adopt_history:
            await tx.execute(
                "UPDATE inventory_snapshots SET stockpile_id = ? WHERE guild_id = ? AND stock_key = ? AND stockpile_id IS NULL",
                (stockpile_id, guild_id, key),
            )
        await prune(tx, guild_id, key, stockpile_id)
    return snapshot_id


async def link_snapshot(bot: FoxBot, snapshot: dict, stockpile_id: str) -> None:
    async with bot.db.transaction() as tx:
        await tx.execute(
            "UPDATE inventory_snapshots SET stockpile_id = ? WHERE guild_id = ? AND (id = ? OR (stock_key = ? AND stockpile_id IS NULL))",
            (stockpile_id, snapshot["guild_id"], snapshot["id"], snapshot["stock_key"]),
        )
        await prune(tx, snapshot["guild_id"], snapshot["stock_key"], stockpile_id)
    bot.boards.request_refresh(snapshot["guild_id"], KIND)


def group_key(snapshot: dict) -> str:
    if snapshot["stockpile_id"]:
        return STOCKPILE_PREFIX + snapshot["stockpile_id"]
    return "key:" + snapshot["stock_key"]


def group_filter(snapshot: dict) -> tuple[str, tuple]:
    if snapshot["stockpile_id"]:
        return "stockpile_id = ?", (snapshot["stockpile_id"],)
    return "stockpile_id IS NULL AND stock_key = ?", (snapshot["stock_key"],)


async def previous_snapshot(bot: FoxBot, snapshot: dict) -> dict | None:
    where, params = group_filter(snapshot)
    return await bot.db.fetchone(
        f"SELECT * FROM inventory_snapshots WHERE guild_id = ? AND {where} "
        "AND (imported_at < ? OR (imported_at = ? AND id < ?)) ORDER BY imported_at DESC, id DESC LIMIT 1",
        (snapshot["guild_id"], *params, snapshot["imported_at"], snapshot["imported_at"], snapshot["id"]),
    )


async def latest_in_group(bot: FoxBot, snapshot: dict) -> dict | None:
    where, params = group_filter(snapshot)
    return await bot.db.fetchone(
        f"SELECT * FROM inventory_snapshots WHERE guild_id = ? AND {where} ORDER BY imported_at DESC, id DESC LIMIT 1",
        (snapshot["guild_id"], *params),
    )


async def snapshot_history(bot: FoxBot, snapshot: dict) -> list[dict]:
    where, params = group_filter(snapshot)
    return await bot.db.fetchall(
        f"SELECT * FROM inventory_snapshots WHERE guild_id = ? AND {where} ORDER BY imported_at DESC, id DESC LIMIT ?",
        (snapshot["guild_id"], *params, KEEP_SNAPSHOTS),
    )


async def snapshot_counts(bot: FoxBot, snapshot_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    if not snapshot_ids:
        return {}
    marks = ", ".join("?" for _ in snapshot_ids)
    rows = await bot.db.fetchall(
        "SELECT snapshot_id, COUNT(DISTINCT item_code) AS types, "
        "SUM(CASE WHEN crated = 1 THEN quantity ELSE 0 END) AS crates, "
        "SUM(CASE WHEN crated = 0 THEN quantity ELSE 0 END) AS loose "
        f"FROM inventory_items WHERE snapshot_id IN ({marks}) GROUP BY snapshot_id",
        tuple(snapshot_ids),
    )
    return {row["snapshot_id"]: (row["types"], row["crates"] or 0, row["loose"] or 0) for row in rows}


def history_window(count: int, index: int, size: int = HISTORY_OPTIONS) -> range:
    start = max(0, min(index - size // 2, count - size))
    return range(start, min(count, start + size))


def history_time(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(int(timestamp), dt.UTC).strftime("%d %b %Y %H:%M UTC")


async def latest_for_stockpile(bot: FoxBot, guild_id: int, stockpile_id: str) -> dict | None:
    return await bot.db.fetchone(
        "SELECT * FROM inventory_snapshots WHERE guild_id = ? AND stockpile_id = ? ORDER BY imported_at DESC, id DESC LIMIT 1",
        (guild_id, stockpile_id),
    )


async def latest_snapshots(bot: FoxBot, guild_id: int) -> list[dict]:
    rows = await bot.db.fetchall(
        "SELECT * FROM inventory_snapshots WHERE guild_id = ? ORDER BY imported_at DESC, id DESC",
        (guild_id,),
    )
    seen: set[str] = set()
    result = []
    for row in rows:
        key = group_key(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


async def can_see_stockpile(bot: FoxBot, member: discord.Member, stockpile: dict | None) -> bool:
    if stockpile is None or not stockpile["private"]:
        return True
    module = stockpiles_module(bot)
    if module is None:
        return await bot.perms.is_admin(member)
    return await module.can_access(bot, member, stockpile)


async def can_view_contents(bot: FoxBot, member: discord.Member, stockpile: dict) -> bool:
    if stockpile["private"]:
        return await can_see_stockpile(bot, member, stockpile)
    if await bot.perms.has(member, GROUP):
        return True
    module = stockpiles_module(bot)
    return module is not None and await module.can_access(bot, member, stockpile)


async def visible_latest(bot: FoxBot, member: discord.Member) -> list[tuple[dict, dict | None]]:
    stockpiles = await guild_stockpiles(bot, member.guild.id)
    result = []
    for snapshot in await latest_snapshots(bot, member.guild.id):
        stockpile = None
        if snapshot["stockpile_id"]:
            stockpile = stockpiles.get(snapshot["stockpile_id"])
            if stockpile is None or not await can_see_stockpile(bot, member, stockpile):
                continue
        result.append((snapshot, stockpile))
    return result


async def remembered_link(bot: FoxBot, member: discord.Member, stock: ParsedStock) -> dict | None:
    key = stock_key(stock.hex, stock.region, stock.store_type, stock.name)
    stockpile = await bot.db.fetchone(
        "SELECT stockpiles.* FROM inventory_snapshots JOIN stockpiles ON stockpiles.id = inventory_snapshots.stockpile_id "
        "WHERE inventory_snapshots.guild_id = ? AND inventory_snapshots.stock_key = ? "
        "ORDER BY inventory_snapshots.imported_at DESC, inventory_snapshots.id DESC LIMIT 1",
        (member.guild.id, key),
    )
    if stockpile is None or not await can_see_stockpile(bot, member, stockpile):
        return None
    return stockpile


async def match_stockpile(bot: FoxBot, member: discord.Member, stock: ParsedStock) -> dict | None:
    visible: list[dict] = []
    exact: list[dict] = []
    if normalise(stock.name):
        rows = await bot.db.fetchall("SELECT * FROM stockpiles WHERE guild_id = ? ORDER BY created_at", (member.guild.id,))
        candidates = [row for row in rows if normalise(row["name"]) == normalise(stock.name)]
        if normalise(stock.hex):
            candidates = [row for row in candidates if normalise(row["hex"]) == normalise(stock.hex)]
        if normalise(stock.region):
            candidates = [row for row in candidates if normalise(row["region"]) == normalise(stock.region)]
        visible = [row for row in candidates if await can_see_stockpile(bot, member, row)]
        exact = [row for row in visible if normalise(row["store_type"]) == normalise(stock.store_type)]
    if len(exact) == 1:
        return exact[0]
    linked = await remembered_link(bot, member, stock)
    if linked is not None:
        return linked
    if not exact and len(visible) == 1:
        return visible[0]
    return None


async def linkable_stockpiles(bot: FoxBot, member: discord.Member, hex_name: str) -> tuple[list[dict], bool]:
    rows = await bot.db.fetchall("SELECT * FROM stockpiles WHERE guild_id = ? ORDER BY name", (member.guild.id,))
    visible = [row for row in rows if await can_see_stockpile(bot, member, row)]
    in_hex = [row for row in visible if normalise(hex_name) and normalise(row["hex"]) == normalise(hex_name)]
    if in_hex:
        return in_hex, True
    return visible, False


def header_mismatch(stock: ParsedStock, stockpile: dict) -> bool:
    pairs = [
        (stock.hex, stockpile["hex"]),
        (stock.region, stockpile["region"]),
        (stock.store_type, stockpile["store_type"]),
        (stock.name, stockpile["name"]),
    ]
    return any(normalise(ours) and normalise(ours) != normalise(theirs) for ours, theirs in pairs)


def display_name(snapshot: dict, stockpile: dict | None) -> str:
    if stockpile is not None:
        return stockpile["name"]
    return snapshot["name"] or "Unnamed stockpile"


def display_hex(snapshot: dict, stockpile: dict | None) -> str:
    return (stockpile or snapshot)["hex"]


def display_region(snapshot: dict, stockpile: dict | None) -> str:
    return (stockpile or snapshot)["region"]


def display_type(snapshot: dict, stockpile: dict | None) -> str:
    return (stockpile or snapshot)["store_type"]


def display_location(snapshot: dict, stockpile: dict | None) -> str:
    return entries.location_label(display_hex(snapshot, stockpile), display_region(snapshot, stockpile))


def describe(snapshot: dict, stockpile: dict | None) -> str:
    prefix = "[Private] " if stockpile is not None and stockpile["private"] else ""
    kind = display_type(snapshot, stockpile)
    suffix = f" ({md(kind)})" if kind else ""
    return f"{prefix}**{md(display_name(snapshot, stockpile))}** - {md(display_location(snapshot, stockpile))}{suffix}"


def plain_label(snapshot: dict, stockpile: dict | None) -> str:
    return f"{display_name(snapshot, stockpile)} - {display_location(snapshot, stockpile)}"


def stock_label(stock: ParsedStock | ImportOutcome) -> str:
    location = entries.location_label(stock.hex, stock.region)
    kind = f" ({stock.store_type})" if stock.store_type else ""
    return f"{stock.name or 'Unnamed stockpile'} - {location}{kind}"


def category_of(row: dict) -> str:
    return row["category"] or UNRECOGNISED


def category_index(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)


def row_units(row: dict) -> int:
    return row["quantity"] * row["per_crate"] if row["crated"] else row["quantity"]


def amount_text(crates: int, loose: int, units: int) -> str:
    if crates and loose:
        return f"{plural(crates, 'crate')} + {loose} loose = **{units}** units"
    if crates:
        return f"{plural(crates, 'crate')} = **{units}** units"
    return f"**{loose}** loose"


def holdings(rows: list[dict], marker: int = 0) -> dict[str, Holding]:
    result: dict[str, Holding] = {}
    for row in rows:
        holding = result.get(row["item_code"])
        if holding is None:
            holding = Holding(row["item_code"], row["item_name"], category_of(row))
            result[row["item_code"]] = holding
        if row["crated"]:
            holding.crates += row["quantity"]
        else:
            holding.loose += row["quantity"]
        holding.units += row_units(row)
        holding.stockpiles.add(marker)
    return result


def sorted_sections(grouped: dict[str, list[itemimages.Tile]]) -> list[tuple[str, list[itemimages.Tile]]]:
    return [
        (category, sorted(tiles, key=lambda tile: (tile.gone, tile.name.casefold(), not tile.crated)))
        for category, tiles in sorted(grouped.items(), key=lambda item: (category_index(item[0]), item[0]))
    ]


def grid_sections(rows: list[dict], previous_rows: list[dict] | None = None) -> list[tuple[str, list[itemimages.Tile]]]:
    current = {(row["item_code"], bool(row["crated"])): row for row in rows}
    old = {(row["item_code"], bool(row["crated"])): row for row in previous_rows or []}
    grouped: dict[str, list[itemimages.Tile]] = {}
    for key, row in current.items():
        delta = None
        if previous_rows is not None:
            before = old.get(key)
            delta = (row["quantity"] - (before["quantity"] if before else 0)) or None
        grouped.setdefault(category_of(row), []).append(
            itemimages.Tile(row["item_code"], row["item_name"], row["quantity"], key[1], delta)
        )
    if previous_rows is not None:
        for key, row in old.items():
            if key not in current:
                grouped.setdefault(category_of(row), []).append(
                    itemimages.Tile(row["item_code"], row["item_name"], 0, key[1], -row["quantity"], True)
                )
    return sorted_sections(grouped)


def totals_sections(holdings_by_category: dict[str, list[Holding]]) -> list[tuple[str, list[itemimages.Tile]]]:
    grouped: dict[str, list[itemimages.Tile]] = {}
    for category, items in holdings_by_category.items():
        tiles = grouped.setdefault(category, [])
        for holding in items:
            if holding.crates:
                tiles.append(itemimages.Tile(holding.code, holding.name, holding.crates, True))
            if holding.loose:
                tiles.append(itemimages.Tile(holding.code, holding.name, holding.loose, False))
    return sorted_sections(grouped)


async def render_grid(sections: list[tuple[str, list[itemimages.Tile]]]) -> bytes | None:
    if not itemimages.available() or not any(tiles for _, tiles in sections):
        return None
    return await asyncio.to_thread(itemimages.render, sections)


def image_file(image: bytes | None, filename: str) -> list[discord.File]:
    if image is None or not filename:
        return []
    return [discord.File(io.BytesIO(image), filename=filename)]


def diff_items(old_rows: list[dict], new_rows: list[dict]) -> list[Change]:
    old = {(row["item_code"], bool(row["crated"])): row for row in old_rows}
    new = {(row["item_code"], bool(row["crated"])): row for row in new_rows}
    changes = []
    for key in old.keys() | new.keys():
        before = old.get(key)
        after = new.get(key)
        old_quantity = before["quantity"] if before else 0
        new_quantity = after["quantity"] if after else 0
        if old_quantity == new_quantity:
            continue
        reference = after or before
        weight = abs(new_quantity - old_quantity) * (reference["per_crate"] if key[1] else 1)
        changes.append(Change(reference["item_name"], key[1], old_quantity, new_quantity, weight))
    changes.sort(key=lambda c: (-c.weight, c.name.casefold(), c.crated))
    return changes


def available_tabs() -> list[str]:
    return [TAB_INVENTORY, TAB_CHANGES, TAB_MAP]


def count_text(value: int) -> str:
    return f"{value:,}" if value else "-"


def number_row(holding: Holding) -> str:
    name = clip(holding.name.replace("`", "'"), NAME_WIDTH)
    return f"{name:<{NAME_WIDTH}} {count_text(holding.crates):>6} {count_text(holding.loose):>6} {holding.units:>9,}"


def pack_tables(sections: list[tuple[str, list[str]]], budget: int = TAB_PAGE_BUDGET) -> list[str]:
    pages: list[list[str]] = [[]]
    used = 0
    for title, rows in sections:
        index = 0
        continued = False
        while index < len(rows):
            label = f"**{md(title)}**" + (" (continued)" if continued else "")
            size = len(label) + len(TABLE_HEADER) + 12
            take: list[str] = []
            while index < len(rows) and used + size + len(rows[index]) + 1 <= budget:
                take.append(rows[index])
                size += len(rows[index]) + 1
                index += 1
            if not take:
                if pages[-1]:
                    pages.append([])
                    used = 0
                    continue
                take.append(rows[index])
                index += 1
            block = f"{label}\n```\n{TABLE_HEADER}\n" + "\n".join(take) + "\n```"
            pages[-1].append(block)
            used += len(block) + 1
            continued = True
    return ["\n".join(page) for page in pages if page]


def stock_totals(rows: list[dict]) -> str:
    held = holdings(rows)
    crates = sum(h.crates for h in held.values())
    loose = sum(h.loose for h in held.values())
    return f"{plural(len(held), 'item type')} | {crates:,} crates | {loose:,} loose"


def numbers_pages(rows: list[dict]) -> list[str]:
    grouped: dict[str, list[Holding]] = {}
    for holding in holdings(rows).values():
        grouped.setdefault(holding.category, []).append(holding)
    sections = [
        (category, [number_row(h) for h in sorted(grouped[category], key=lambda h: h.name.casefold())])
        for category in sorted(grouped, key=lambda c: (category_index(c), c))
    ]
    pages = pack_tables(sections)
    if not pages:
        return ["The stockpile was empty."]
    pages[0] = f"{stock_totals(rows)}\nTotal = crates x items per crate + loose.\n\n{pages[0]}"
    return pages


def change_line(change: Change) -> str:
    unit = "crates" if change.crated else "loose"
    return f"{md(change.name)} ({unit}): {change.old:,} -> **{change.new:,}**"


def import_label(snapshot: dict) -> str:
    return f"import by <@{snapshot['imported_by']}> {timeutil.relative(snapshot['imported_at'])}"


def changes_pages(rows: list[dict], previous_rows: list[dict] | None, previous: dict | None, current: dict) -> list[str]:
    if previous_rows is None or previous is None:
        return ["This is the first import of this stockpile, so there is nothing to compare yet."]
    lines = [f"**Before:** {import_label(previous)}", f"**After:** {import_label(current)}"]
    changes = diff_items(previous_rows, rows)
    if not changes:
        return ["\n".join(lines) + "\n\nNothing changed."]
    lines.append("")
    lines.extend(change_line(change) for change in changes)
    return [page.strip() for page in chunk_lines(lines, TAB_PAGE_BUDGET)]


def image_body(rows: list[dict], previous_rows: list[dict] | None) -> str:
    text = stock_totals(rows)
    if previous_rows is not None:
        text += (
            "\nGreen and red numbers on the icons show what changed since the import before, and greyed icons ran out. "
            "See **Changes** for before and after."
        )
    return text


@dataclass
class MapSpot:
    key: str | None
    hex: str
    region: str
    x: float | None = None
    y: float | None = None
    source: str = "none"


async def map_spot(bot: FoxBot, snapshot: dict, stockpile: dict | None) -> MapSpot:
    hex_name = display_hex(snapshot, stockpile)
    region = display_region(snapshot, stockpile)
    key = bot.locations.map_key(hex_name)
    if key is None:
        return MapSpot(None, hex_name, region)
    if snapshot.get("map_x") is not None and snapshot.get("map_y") is not None and normalise(snapshot["hex"]) == normalise(hex_name):
        return MapSpot(key, hex_name, region, snapshot["map_x"], snapshot["map_y"], "export")
    where, params = group_filter(snapshot)
    row = await bot.db.fetchone(
        f"SELECT hex, map_x, map_y FROM inventory_snapshots WHERE guild_id = ? AND {where} "
        "AND map_x IS NOT NULL AND map_y IS NOT NULL ORDER BY imported_at DESC, id DESC LIMIT 1",
        (snapshot["guild_id"], *params),
    )
    if row is not None and normalise(row["hex"]) == normalise(hex_name):
        return MapSpot(key, hex_name, region, row["map_x"], row["map_y"], "export")
    position = bot.locations.position(hex_name, region)
    if position is not None:
        return MapSpot(key, hex_name, region, position[0], position[1], "town")
    return MapSpot(key, hex_name, region, source="hex")


def map_body(spot: MapSpot) -> str:
    where = f"**{md(entries.location_label(spot.hex, spot.region))}**"
    if spot.source == "export":
        return f"{where}\nExact spot from the in-game export (X {spot.x:.3f}, Y {spot.y:.3f})."
    if spot.source == "town":
        return (
            f"{where}\nApproximate: the map marker of {md(spot.region)}. "
            "Import the in-game Copy to Clipboard text to get the exact spot."
        )
    if spot.source == "hex":
        return f"{where}\nOnly the hex is known, so no dot is shown."
    return f"{where}\nThis location is not on the war map, so there is no map to show."


async def render_map(bot: FoxBot, spot: MapSpot, mode: str, label: str) -> bytes | None:
    if spot.key is None or not mapimages.available():
        return None
    approximate = spot.source == "town"
    if mode == MAP_WORLD:
        names = {hex_.key: hex_.name for hex_ in bot.locations.hexes.values()}
        return await asyncio.to_thread(
            mapimages.render_world, spot.key, spot.x, spot.y, label=label, approximate=approximate, names=names
        )
    return await asyncio.to_thread(mapimages.render_hex, spot.key, spot.x, spot.y, label=label, approximate=approximate)


async def tab_pages(
    bot: FoxBot,
    tab: str,
    *,
    snapshot: dict,
    stockpile: dict | None,
    rows: list[dict],
    previous_rows: list[dict] | None,
    previous: dict | None,
    map_mode: str,
) -> tuple[str, list[str], bytes | None, str]:
    if tab == TAB_CHANGES:
        return TAB_CHANGES, changes_pages(rows, previous_rows, previous, snapshot), None, ""
    if tab == TAB_MAP:
        spot = await map_spot(bot, snapshot, stockpile)
        image = await render_map(bot, spot, map_mode, display_name(snapshot, stockpile))
        return TAB_MAP, [map_body(spot)], image, MAP_FILENAME
    image = await render_grid(grid_sections(rows, previous_rows))
    if image is not None:
        return TAB_INVENTORY, [image_body(rows, previous_rows)], image, GRID_FILENAME
    return TAB_INVENTORY, numbers_pages(rows), None, ""


def import_notes(outcome: ImportOutcome) -> list[str]:
    notes = []
    if outcome.unknown:
        names = ", ".join(md(name) for name in outcome.unknown)
        notes.append(clip(f"Unrecognised ({len(outcome.unknown)}): {names}. Saved under the pasted names.", 700))
    if (outcome.hex or outcome.region) and not outcome.known_location:
        notes.append("The location was not found on the war map and was saved as written in the export.")
    if outcome.warning:
        notes.append(outcome.warning)
    return notes


def add_map_switch(view: discord.ui.View, mode: str, factory, row: int) -> None:
    for value, label in ((MAP_HEX, "Hex map"), (MAP_WORLD, "World map")):
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary if value == mode else discord.ButtonStyle.secondary,
            row=row,
            disabled=value == mode,
        )
        button.callback = factory(value)
        view.add_item(button)


def add_tab_buttons(view: discord.ui.View, current: str, factory, row: int) -> None:
    for tab in available_tabs():
        button = discord.ui.Button(
            label=TAB_LABELS[tab],
            style=discord.ButtonStyle.primary if tab == current else discord.ButtonStyle.secondary,
            row=row,
            disabled=tab == current,
        )
        button.callback = factory(tab)
        view.add_item(button)


def add_page_buttons(view: discord.ui.View, page: int, count: int, previous, following, row: int) -> None:
    if count <= 1:
        return
    back = discord.ui.Button(label="Previous part", style=discord.ButtonStyle.secondary, row=row, disabled=page == 0)
    back.callback = previous
    view.add_item(back)
    view.add_item(discord.ui.Button(label=f"Part {page + 1}/{count}", style=discord.ButtonStyle.secondary, row=row, disabled=True))
    forward = discord.ui.Button(label="Next part", style=discord.ButtonStyle.secondary, row=row, disabled=page >= count - 1)
    forward.callback = following
    view.add_item(forward)


def summary_embed(
    outcome: ImportOutcome,
    snapshot: dict,
    stockpile: dict | None,
    rows: list[dict],
    *,
    body: str,
    tab_label: str,
    index: int,
    total: int,
) -> discord.Embed:
    title = "Contents imported" + (f" ({index}/{total})" if total > 1 else "")
    if stockpile is not None:
        colour = Colour.PRIVATE if stockpile["private"] else Colour.SUCCESS
    else:
        colour = Colour.MUTED if outcome.kept else Colour.WARNING
    embed = discord.Embed(title=title, description=clip(body, 4000), colour=colour)
    if stockpile is not None:
        where = describe(snapshot, stockpile)
    elif outcome.kept:
        where = f"Not tracked (kept unlinked): {md(stock_label(outcome))}"
    else:
        where = f"Not tracked: {md(stock_label(outcome))}\nTrack it as a stockpile, link it to an existing one, or keep it unlinked."
    embed.add_field(name="Stockpile", value=clip(where, 1024), inline=False)
    held = holdings(rows)
    crates = sum(h.crates for h in held.values())
    loose = sum(h.loose for h in held.values())
    embed.add_field(name="Items", value=f"{plural(len(held), 'item type')}: {crates} crates, {loose} loose", inline=True)
    if outcome.unknown:
        names = ", ".join(md(name) for name in outcome.unknown)
        embed.add_field(
            name=f"Unrecognised ({len(outcome.unknown)})",
            value=clip(f"{names}\nSaved under the pasted names.", 800),
            inline=False,
        )
    if (outcome.hex or outcome.region) and not outcome.known_location:
        embed.add_field(name="Location", value="Not found on the war map, saved as written in the export.", inline=False)
    if outcome.warning:
        embed.add_field(name="Warning", value=clip(outcome.warning, 1024), inline=False)
    embed.set_footer(text=f"{tab_label} | Snapshot {snapshot['id']} | {SOURCE_LABELS.get(snapshot['source'], snapshot['source'])}")
    return embed


def contents_header(snapshot: dict, stockpile: dict | None) -> list[str]:
    source = SOURCE_LABELS.get(snapshot["source"], snapshot["source"])
    head = [
        describe(snapshot, stockpile),
        f"Imported by <@{snapshot['imported_by']}> {timeutil.relative(snapshot['imported_at'])} from the {source}",
    ]
    if snapshot["game_time"]:
        head.append(f"Game clock when copied: {format_game_time(snapshot['game_time'])}")
    if stockpile is None:
        head.append("Not linked to a tracked stockpile.")
    return head


def empty_contents_embed(stockpile: dict | None) -> discord.Embed:
    name = stockpile["name"] if stockpile is not None else "Stockpile"
    return discord.Embed(
        title=clip(f"Inventory: {name}", 256),
        description="No contents have been imported for this stockpile yet. Press **Import new contents** and paste "
        "the in-game **Copy to Clipboard** text.",
        colour=Colour.PRIVATE if stockpile is not None and stockpile["private"] else Colour.INFO,
    )


def resolve_query(catalog: Catalog, query: str, faction: str | None) -> Item | None:
    query = query.strip()
    item = catalog.get(query) or catalog.lookup(query, faction)
    if item is not None:
        return item
    key = search_key(query)
    if not key:
        return None
    for candidate in catalog.search(query, faction, limit=5):
        if key in search_key(candidate.name) or any(key in search_key(name) for name in candidate.names.values()):
            return candidate
    return None


async def run_import(
    bot: FoxBot,
    interaction: discord.Interaction,
    guild_id: int,
    text: str,
    *,
    stockpile: dict | None = None,
    origin: ContentsView | None = None,
) -> None:
    faction = await guild_faction(bot, guild_id)
    try:
        result = parse_inventory(text, bot.catalog, faction)
    except ParseError as error:
        await deny(interaction, str(error))
        return
    if stockpile is not None and len(result.stocks) > 1:
        await deny(interaction, "That text holds several stockpiles. Paste the contents of one stockpile here.")
        return
    if len(result.stocks) > MAX_STOCKS_PER_IMPORT:
        await deny(interaction, f"That holds {len(result.stocks)} stockpiles. Import at most {MAX_STOCKS_PER_IMPORT} at once.")
        return
    if stockpile is None and any(not stock.has_header for stock in result.stocks):
        await deny(
            interaction,
            "The first line with the hex, town and stockpile name is missing. Copy the whole text from the game, "
            "or import from the stockpile's **Inventory** panel to save it for that stockpile.",
        )
        return
    member = await interaction_member(interaction, guild_id)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return
    types = await bot.settings.get(guild_id, keys.STORAGE_TYPES)
    outcomes: list[ImportOutcome] = []
    for stock in result.stocks:
        known = True
        if stock.hex or stock.region:
            location = bot.locations.resolve(stock.hex, stock.region)
            stock.hex, stock.region, known = location.hex, location.region, location.known
        stock.store_type = canonical_store_type(stock.store_type, types)
        warning = None
        adopt = True
        if stockpile is not None:
            target = stockpile
            if not stock.has_header:
                stock.hex, stock.region = stockpile["hex"], stockpile["region"]
                stock.store_type, stock.name = stockpile["store_type"], stockpile["name"]
                known = True
            elif header_mismatch(stock, stockpile):
                adopt = False
                warning = (
                    f"The pasted header says **{md(stock_label(stock))}**, which does not match **{md(stockpile['name'])}** "
                    f"({md(entries.location_label(stockpile['hex'], stockpile['region']))}). "
                    "The contents were saved for this stockpile anyway."
                )
        else:
            target = await match_stockpile(bot, member, stock)
        snapshot_id = await save_snapshot(
            bot,
            guild_id,
            stock,
            source=result.source,
            faction=result.faction,
            stockpile_id=target["id"] if target is not None else None,
            user_id=interaction.user.id,
            adopt_history=adopt,
        )
        outcomes.append(
            ImportOutcome(
                snapshot_id=snapshot_id,
                hex=stock.hex,
                region=stock.region,
                store_type=stock.store_type,
                name=stock.name,
                known_location=known,
                unknown=list(stock.unknown),
                warning=warning,
            )
        )
    bot.boards.request_refresh(guild_id, KIND)
    notes = [f"Imported {plural(len(outcomes), 'stockpile')} from the {SOURCE_LABELS[result.source]}."]
    if result.skipped:
        notes.append(f"{plural(result.skipped, 'line')} could not be read and {'was' if result.skipped == 1 else 'were'} skipped.")
    intro = "\n".join(notes)
    if len(outcomes) == 1:
        outcome = outcomes[0]
        if origin is not None:
            origin.outcome = outcome
            origin.intro = intro
            origin.snapshot_id = outcome.snapshot_id
            origin.stockpile_id = None
            origin.selected_id = outcome.snapshot_id
            origin.page = 0
            await origin.show(interaction)
            return
        view = ContentsView(bot, guild_id, interaction.user.id, snapshot_id=outcome.snapshot_id, outcome=outcome, intro=intro)
        await view.send(interaction)
        return
    view = ImportResultView(bot, guild_id, interaction.user.id, outcomes, intro=intro)
    await view.start(interaction)


class PasteModal(BaseModal):
    def __init__(self, bot: FoxBot, guild_id: int, *, stockpile: dict | None = None, origin: ContentsView | None = None):
        super().__init__(title="Import Stockpile Contents")
        self.bot = bot
        self.guild_id = guild_id
        self.origin = origin
        self.stockpile_id = stockpile["id"] if stockpile is not None else None
        help_text = PASTE_HELP
        if stockpile is not None:
            help_text += f"\nThe contents will be saved for **{md(stockpile['name'])}**."
        self.help = discord.ui.TextDisplay(help_text)
        self.contents = text_field(
            "Stockpile contents",
            placeholder="Terminus - Rising Loom - Seaport - Public - X: ... then one item per line",
            paragraph=True,
            max_length=PASTE_LIMIT,
        )
        self.add_item(self.help)
        self.add_item(self.contents)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.guild_id):
            return
        stockpile = None
        if self.stockpile_id is not None:
            stockpile = await fetch_stockpile(self.bot, self.guild_id, self.stockpile_id)
            if stockpile is None:
                await deny(interaction, "That stockpile no longer exists.")
                return
            member = await interaction_member(interaction, self.guild_id)
            if member is None or not await can_view_contents(self.bot, member, stockpile):
                await deny(interaction, "You do not have access to that stockpile.")
                return
        raw = str(self.contents.component.value or "")
        if len(raw) >= PASTE_LIMIT - 10:
            await deny(
                interaction,
                "That text reached Discord's 4000 character limit for forms, so the end of it was probably cut off. "
                "Nothing was saved. Paste it into a .txt file and use `/inventory upload` instead.",
            )
            return
        await run_import(self.bot, interaction, self.guild_id, raw, stockpile=stockpile, origin=self.origin)


class FindModal(BaseModal, title="Find an Item"):
    def __init__(self, bot: FoxBot):
        super().__init__()
        self.bot = bot
        self.query = text_field("Item", placeholder="e.g. Loughcaster, 7.62mm, Basic Materials", max_length=100)
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await show_find(self.bot, interaction, text_value(self.query))


class ImportActions:
    def _button(self, label: str, style: discord.ButtonStyle, callback, *, row: int, disabled: bool = False) -> None:
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        if callback is not None:
            button.callback = callback
        self.add_item(button)

    def add_import_actions(self, snapshot: dict, row: int) -> None:
        outcome = self.current
        if outcome is None or outcome.kept or snapshot["stockpile_id"] is not None:
            return
        if stockpiles_module(self.bot) is not None:
            self._button("Track as stockpile", discord.ButtonStyle.success, self._track_public, row=row)
            self._button("Track as private stockpile", discord.ButtonStyle.primary, self._track_private, row=row)
        self._button("Link to existing", discord.ButtonStyle.primary, self._link, row=row)
        self._button("Keep unlinked", discord.ButtonStyle.secondary, self._keep, row=row)

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        return await check_group(interaction, GROUP, self.guild_id)

    async def _snapshot(self, interaction: discord.Interaction) -> dict | None:
        if not await self._allowed(interaction):
            return None
        snapshot = await fetch_snapshot(self.bot, self.guild_id, self.current.snapshot_id)
        if snapshot is None:
            await self.show(interaction)
            return None
        if snapshot["stockpile_id"]:
            await self.show(interaction, notice="These contents are already linked to a stockpile.")
            return None
        return snapshot

    async def _track_public(self, interaction: discord.Interaction) -> None:
        await self._track(interaction, private=False)

    async def _track_private(self, interaction: discord.Interaction) -> None:
        await self._track(interaction, private=True)

    async def _track(self, interaction: discord.Interaction, *, private: bool) -> None:
        snapshot = await self._snapshot(interaction)
        if snapshot is None:
            return
        if not await check_group(interaction, STOCKPILE_GROUP, self.guild_id):
            return
        module = stockpiles_module(self.bot)
        if module is None:
            await deny(interaction, "Stockpile tracking is not available right now.")
            return
        outcome = self.current
        snapshot_id = snapshot["id"]

        async def saved(modal_interaction: discord.Interaction, row: dict, feedback: str) -> None:
            member = await interaction_member(modal_interaction, self.guild_id)
            current = await fetch_snapshot(self.bot, self.guild_id, snapshot_id)
            if member is None or not await self.bot.perms.has(member, GROUP) or current is None:
                await self.show(modal_interaction, notice=f"Stockpile added. {feedback}\nThe imported contents could not be linked to it.")
                return
            await link_snapshot(self.bot, current, row["id"])
            await self.show(
                modal_interaction,
                notice=f"Now tracking **{md(row['name'])}**. {feedback}\nThe imported contents are linked to it.",
            )

        types = await self.bot.settings.get(self.guild_id, keys.STORAGE_TYPES)
        location = Location(outcome.hex, outcome.region, outcome.known_location) if (outcome.hex or outcome.region) else None
        modal = module.StockpileModal(
            self.bot,
            self.guild_id,
            types,
            saved,
            private=private,
            location=location,
            prefill={"name": outcome.name, "store_type": outcome.store_type},
        )
        await interaction.response.send_modal(modal)

    async def _link(self, interaction: discord.Interaction) -> None:
        snapshot = await self._snapshot(interaction)
        if snapshot is None:
            return
        member = await interaction_member(interaction, self.guild_id)
        rows, in_hex = await linkable_stockpiles(self.bot, member, snapshot["hex"])
        if not rows:
            await self.show(interaction, notice="There are no tracked stockpiles you can link these contents to.")
            return
        snapshot_id = snapshot["id"]

        async def picked(pick_interaction: discord.Interaction, value: str) -> None:
            if not await self._allowed(pick_interaction):
                return
            picker_member = await interaction_member(pick_interaction, self.guild_id)
            stockpile = await fetch_stockpile(self.bot, self.guild_id, value)
            if stockpile is None or picker_member is None or not await can_see_stockpile(self.bot, picker_member, stockpile):
                await deny(pick_interaction, "That stockpile no longer exists or you cannot see it.")
                return
            current = await fetch_snapshot(self.bot, self.guild_id, snapshot_id)
            if current is None:
                await self.show(pick_interaction)
                return
            if current["stockpile_id"]:
                await self.show(pick_interaction, notice="These contents are already linked to a stockpile.")
                return
            await link_snapshot(self.bot, current, stockpile["id"])
            await self.show(pick_interaction, notice=f"Linked to **{md(stockpile['name'])}**.")

        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)

        async def go_back(back_interaction: discord.Interaction) -> None:
            await self.show(back_interaction)

        back.callback = go_back
        options = [
            PickOption(
                row["id"],
                f"{row['name']} - {entries.location_label(row['hex'], row['region'])}",
                f"{row['store_type']}{' | Private' if row['private'] else ''}",
            )
            for row in rows
        ]
        picker = PickerView(
            options,
            picked,
            owner_id=interaction.user.id,
            placeholder="Pick the stockpile these contents belong to...",
            extra_buttons=[back],
        )
        where = f"in **{md(snapshot['hex'])}**" if in_hex else "(none are tracked in that hex, so all are listed)"
        await edit(
            interaction,
            content=f"Link **{md(stock_label(self.current))}** to a tracked stockpile {where}.",
            embed=None,
            attachments=[],
            view=picker,
        )

    async def _keep(self, interaction: discord.Interaction) -> None:
        snapshot = await self._snapshot(interaction)
        if snapshot is None:
            return
        self.current.kept = True
        await self.show(interaction, notice="Kept unlinked. The contents still show up in `/find`, `/inventory totals` and exports.")


class ImportResultView(ImportActions, BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int, outcomes: list[ImportOutcome], *, intro: str):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.outcomes = outcomes
        self.intro = intro
        self.page = 0
        self.tab = TAB_INVENTORY
        self.tab_page = 0
        self.map_mode = MAP_HEX

    @property
    def current(self) -> ImportOutcome:
        return self.outcomes[self.page]

    async def _state(self, interaction: discord.Interaction) -> tuple[str | None, dict | None, dict | None]:
        snapshot = await fetch_snapshot(self.bot, self.guild_id, self.current.snapshot_id)
        if snapshot is None:
            return "This import no longer exists. It may have been replaced by newer imports.", None, None
        stockpile = None
        if snapshot["stockpile_id"]:
            stockpile = await fetch_stockpile(self.bot, self.guild_id, snapshot["stockpile_id"])
            member = await interaction_member(interaction, self.guild_id)
            if stockpile is None or member is None or not await can_see_stockpile(self.bot, member, stockpile):
                return "You no longer have access to the stockpile these contents belong to.", None, None
        return None, snapshot, stockpile

    async def _render(self, interaction: discord.Interaction) -> tuple[str | None, discord.Embed | None, bytes | None, str]:
        error, snapshot, stockpile = await self._state(interaction)
        self.clear_items()
        if len(self.outcomes) > 1:
            self._button("Previous", discord.ButtonStyle.secondary, self._previous, row=0, disabled=self.page == 0)
            self._button(f"{self.page + 1}/{len(self.outcomes)}", discord.ButtonStyle.secondary, None, row=0, disabled=True)
            self._button("Next", discord.ButtonStyle.secondary, self._next, row=0, disabled=self.page >= len(self.outcomes) - 1)
        if error is not None:
            return error, None, None, ""
        rows = await fetch_items(self.bot, snapshot["id"])
        previous = await previous_snapshot(self.bot, snapshot)
        previous_rows = await fetch_items(self.bot, previous["id"]) if previous is not None else None
        self.tab, pages, image, filename = await tab_pages(
            self.bot,
            self.tab,
            snapshot=snapshot,
            stockpile=stockpile,
            rows=rows,
            previous_rows=previous_rows,
            previous=previous,
            map_mode=self.map_mode,
        )
        self.tab_page = max(0, min(self.tab_page, len(pages) - 1))
        add_tab_buttons(self, self.tab, self._tab_switcher, row=1)
        if self.tab == TAB_MAP:
            add_map_switch(self, self.map_mode, self._map_switcher, row=2)
        else:
            add_page_buttons(self, self.tab_page, len(pages), self._previous_page, self._next_page, row=2)
        self.add_import_actions(snapshot, row=3)
        part_text = f" part {self.tab_page + 1}/{len(pages)}" if len(pages) > 1 else ""
        embed = summary_embed(
            self.current,
            snapshot,
            stockpile,
            rows,
            body=pages[self.tab_page],
            tab_label=f"{TAB_LABELS[self.tab]}{part_text}",
            index=self.page + 1,
            total=len(self.outcomes),
        )
        if image is not None:
            embed.set_image(url=f"attachment://{filename}")
        return None, embed, image, filename

    async def start(self, interaction: discord.Interaction) -> None:
        error, embed, image, filename = await self._render(interaction)
        content = self.intro if error is None else f"{self.intro}\n{error}"
        await reply(interaction, content, embed=embed, view=self if self.children else None, files=image_file(image, filename) or None)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        error, embed, image, filename = await self._render(interaction)
        content = error or notice or self.intro
        await edit(interaction, content=content, embed=embed, attachments=image_file(image, filename), view=self if self.children else None)

    async def _previous(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        self.page = max(0, self.page - 1)
        self.tab_page = 0
        await self.show(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        self.page = min(len(self.outcomes) - 1, self.page + 1)
        self.tab_page = 0
        await self.show(interaction)

    def _tab_switcher(self, tab: str):
        async def switch(interaction: discord.Interaction) -> None:
            if not await self._allowed(interaction):
                return
            self.tab = tab
            self.tab_page = 0
            await self.show(interaction)

        return switch

    def _map_switcher(self, mode: str):
        async def switch(interaction: discord.Interaction) -> None:
            if not await self._allowed(interaction):
                return
            self.map_mode = mode
            await self.show(interaction)

        return switch

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        self.tab_page = max(0, self.tab_page - 1)
        await self.show(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        self.tab_page += 1
        await self.show(interaction)


class ContentsView(ImportActions, BaseView):
    def __init__(
        self,
        bot: FoxBot,
        guild_id: int,
        owner_id: int,
        *,
        stockpile_id: str | None = None,
        snapshot_id: int | None = None,
        outcome: ImportOutcome | None = None,
        intro: str | None = None,
    ):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.stockpile_id = stockpile_id
        self.snapshot_id = snapshot_id
        self.outcome = outcome
        self.intro = intro
        self.tab = TAB_INVENTORY
        self.map_mode = MAP_HEX
        self.page = 0
        self.image: bytes | None = None
        self.image_name = ""
        self.selected_id: int | None = outcome.snapshot_id if outcome is not None else None
        self.history_ids: list[int] = []
        self.linked = False

    @property
    def current(self) -> ImportOutcome | None:
        return self.outcome

    async def _resolve(self, interaction: discord.Interaction) -> tuple[str | None, dict | None, dict | None]:
        member = await interaction_member(interaction, self.guild_id)
        if member is None:
            return "This only works inside the server.", None, None
        if self.stockpile_id is None:
            snapshot = await fetch_snapshot(self.bot, self.guild_id, self.snapshot_id) if self.snapshot_id else None
            if snapshot is None:
                return "Those contents no longer exist. They may have been replaced by newer imports.", None, None
            if snapshot["stockpile_id"]:
                self.stockpile_id = snapshot["stockpile_id"]
            else:
                if not await self.bot.perms.has(member, GROUP):
                    return "You need the **Inventory** permission to see these contents.", None, None
                return None, await latest_in_group(self.bot, snapshot), None
        stockpile = await fetch_stockpile(self.bot, self.guild_id, self.stockpile_id)
        if stockpile is None:
            return "That stockpile no longer exists.", None, None
        if not await can_view_contents(self.bot, member, stockpile):
            return "You do not have access to this stockpile.", None, None
        return None, await latest_for_stockpile(self.bot, self.guild_id, self.stockpile_id), stockpile

    async def _render(self, interaction: discord.Interaction) -> tuple[str | None, discord.Embed | None]:
        error, latest, stockpile = await self._resolve(interaction)
        if error is not None:
            return error, None
        self.clear_items()
        self.image = None
        self.image_name = ""
        self.linked = stockpile is not None
        if latest is None:
            self.history_ids = []
            self._button("Import new contents", discord.ButtonStyle.success, self._import, row=2)
            return None, empty_contents_embed(stockpile)
        history = await snapshot_history(self.bot, latest)
        self.history_ids = [snapshot["id"] for snapshot in history]
        index = self.history_ids.index(self.selected_id) if self.selected_id in self.history_ids else 0
        snapshot = history[index]
        self.selected_id = snapshot["id"]
        previous = history[index + 1] if index + 1 < len(history) else None
        rows = await fetch_items(self.bot, snapshot["id"])
        previous_rows = await fetch_items(self.bot, previous["id"]) if previous is not None else None
        self.tab, pages, self.image, self.image_name = await tab_pages(
            self.bot,
            self.tab,
            snapshot=snapshot,
            stockpile=stockpile,
            rows=rows,
            previous_rows=previous_rows,
            previous=previous,
            map_mode=self.map_mode,
        )
        self.page = max(0, min(self.page, len(pages) - 1))
        head = contents_header(snapshot, stockpile)
        if index > 0:
            head.append("Viewing an older import. Press **Back** to go towards the newest one, or pick one from the list.")
        embed = discord.Embed(
            title=clip(f"Inventory: {display_name(snapshot, stockpile)}", 256),
            description=clip("\n".join(head) + "\n\n" + pages[self.page], 4096),
            colour=Colour.PRIVATE if stockpile is not None and stockpile["private"] else Colour.INFO,
        )
        part_text = f" | Part {self.page + 1}/{len(pages)}" if len(pages) > 1 else ""
        embed.set_footer(text=f"{TAB_LABELS[self.tab]}{part_text} | Snapshot {snapshot['id']}")
        if self.image is not None:
            embed.set_image(url=f"attachment://{self.image_name}")
        add_tab_buttons(self, self.tab, self._tab_switcher, row=0)
        await self._add_history_select(history, index)
        self._button("Back", discord.ButtonStyle.secondary, self._newer, row=2, disabled=index == 0)
        self._button(f"Page {index + 1}/{len(history)}", discord.ButtonStyle.secondary, None, row=2, disabled=True)
        self._button("Next", discord.ButtonStyle.secondary, self._older, row=2, disabled=index >= len(history) - 1)
        self._button("Import new contents", discord.ButtonStyle.success, self._import, row=2)
        if self.tab == TAB_MAP:
            add_map_switch(self, self.map_mode, self._map_switcher, row=3)
        else:
            add_page_buttons(self, self.page, len(pages), self._previous, self._next, row=3)
        self.add_import_actions(latest, row=4)
        return None, embed

    async def _add_history_select(self, history: list[dict], index: int) -> None:
        window = history_window(len(history), index)
        chosen = [history[position] for position in window]
        counts = await snapshot_counts(self.bot, [snapshot["id"] for snapshot in chosen])
        guild = self.bot.get_guild(self.guild_id)
        options = []
        for position in window:
            snapshot = history[position]
            types, crates, loose = counts.get(snapshot["id"], (0, 0, 0))
            note = "Newest | " if position == 0 else ""
            options.append(
                option(
                    f"{history_time(snapshot['imported_at'])} - {importer_name(guild, snapshot['imported_by'])}",
                    str(snapshot["id"]),
                    f"{note}{plural(types, 'item type')}, {crates:,} crates, {loose:,} loose",
                    default=position == index,
                )
            )
        select = discord.ui.Select(placeholder="Pick an import from the history...", options=options, row=1)
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        select = next(item for item in self.children if isinstance(item, discord.ui.Select))
        value = str(select.values[0]) if select.values else ""
        if value.isdigit():
            self.selected_id = int(value)
            self.page = 0
        await self.show(interaction)

    async def _older(self, interaction: discord.Interaction) -> None:
        self._step(1)
        await self.show(interaction)

    async def _newer(self, interaction: discord.Interaction) -> None:
        self._step(-1)
        await self.show(interaction)

    def _step(self, offset: int) -> None:
        if self.selected_id not in self.history_ids:
            return
        target = self.history_ids.index(self.selected_id) + offset
        if 0 <= target < len(self.history_ids):
            self.selected_id = self.history_ids[target]
            self.page = 0

    def _content(self, notice: str | None = None) -> str | None:
        parts = [notice] if notice else ([self.intro] if self.intro else [])
        if self.outcome is not None:
            parts.extend(import_notes(self.outcome))
            if not self.outcome.kept and not self.linked:
                parts.append("These contents are not linked to a tracked stockpile. Track them as a stockpile, link them to an existing one, or keep them unlinked.")
        text = "\n".join(part for part in parts if part)
        return clip(text, 1900) if text else None

    async def send(self, interaction: discord.Interaction) -> None:
        error, embed = await self._render(interaction)
        if error is not None:
            await deny(interaction, error)
            return
        await reply(interaction, self._content(), embed=embed, view=self, files=image_file(self.image, self.image_name) or None)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        error, embed = await self._render(interaction)
        if error is not None:
            await edit(interaction, content=error, embed=None, attachments=[], view=None)
            return
        await edit(
            interaction,
            content=self._content(notice),
            embed=embed,
            attachments=image_file(self.image, self.image_name),
            view=self,
        )

    def _tab_switcher(self, tab: str):
        async def switch(interaction: discord.Interaction) -> None:
            self.tab = tab
            self.page = 0
            await self.show(interaction)

        return switch

    def _map_switcher(self, mode: str):
        async def switch(interaction: discord.Interaction) -> None:
            self.map_mode = mode
            await self.show(interaction)

        return switch

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self.show(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        await self.show(interaction)

    async def _import(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.guild_id):
            return
        error, _, stockpile = await self._resolve(interaction)
        if error is not None:
            await deny(interaction, error)
            return
        await interaction.response.send_modal(PasteModal(self.bot, self.guild_id, stockpile=stockpile, origin=self))


async def show_for_stockpile(bot: FoxBot, interaction: discord.Interaction, stockpile: dict) -> None:
    member = await interaction_member(interaction, stockpile["guild_id"])
    if member is None or not await can_view_contents(bot, member, stockpile):
        await deny(interaction, "You do not have access to this stockpile's contents.")
        return
    view = ContentsView(bot, stockpile["guild_id"], interaction.user.id, stockpile_id=stockpile["id"])
    await view.send(interaction)


async def open_contents(bot: FoxBot, interaction: discord.Interaction, value: str) -> None:
    if value.startswith(STOCKPILE_PREFIX):
        view = ContentsView(bot, interaction.guild_id, interaction.user.id, stockpile_id=value[len(STOCKPILE_PREFIX) :])
    elif value.startswith(SNAPSHOT_PREFIX) and value[len(SNAPSHOT_PREFIX) :].isdigit():
        view = ContentsView(bot, interaction.guild_id, interaction.user.id, snapshot_id=int(value[len(SNAPSHOT_PREFIX) :]))
    else:
        await deny(interaction, "Pick a stockpile from the suggestions.")
        return
    await view.send(interaction)


def contents_value(snapshot: dict) -> str:
    if snapshot["stockpile_id"]:
        return STOCKPILE_PREFIX + snapshot["stockpile_id"]
    return f"{SNAPSHOT_PREFIX}{snapshot['id']}"


async def start_import(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    await interaction.response.send_modal(PasteModal(bot, interaction.guild_id))


class StockpileFromExportModal(BaseModal):
    def __init__(self, bot: FoxBot, guild_id: int):
        super().__init__(title="Add Stockpile from CSV")
        self.bot = bot
        self.guild_id = guild_id
        self.help = discord.ui.TextDisplay(CSV_STOCKPILE_HELP)
        self.contents = text_field(
            "Stockpile contents",
            placeholder="Terminus - Rising Loom - Seaport - KM Main - X: ... then one item per line",
            paragraph=True,
            max_length=PASTE_LIMIT,
        )
        self.code = text_field("Access code", placeholder="The stockpile's reserve code", max_length=20)
        self.priority = select_field("Priority", list(PRIORITIES), default="Medium")
        self.visibility = select_field(
            "Visibility",
            ["Public", "Private"],
            default="Public",
            description="Private stockpiles are only shown to you, admins and people you add.",
        )
        for item in (self.help, self.contents, self.code, self.priority, self.visibility):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.contents.component.value or "")
        if len(raw) >= PASTE_LIMIT - 10:
            await deny(
                interaction,
                "That text reached Discord's 4000 character limit for forms, so the end of it was probably cut off. "
                "Nothing was saved. Save it as a .txt file, use `/inventory upload`, then press **Track as stockpile**.",
            )
            return
        await create_stockpile_from_export(
            self.bot,
            interaction,
            self.guild_id,
            raw,
            code=text_value(self.code),
            priority=select_value(self.priority) or "Medium",
            private=select_value(self.visibility) == "Private",
        )


async def start_stockpile_from_export(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, STOCKPILE_GROUP):
        return
    if stockpiles_module(bot) is None:
        await deny(interaction, "Stockpile tracking is not available right now.")
        return
    await interaction.response.send_modal(StockpileFromExportModal(bot, interaction.guild_id))


async def create_stockpile_from_export(
    bot: FoxBot,
    interaction: discord.Interaction,
    guild_id: int,
    text: str,
    *,
    code: str,
    priority: str,
    private: bool,
) -> None:
    if not await check_group(interaction, STOCKPILE_GROUP, guild_id):
        return
    module = stockpiles_module(bot)
    if module is None:
        await deny(interaction, "Stockpile tracking is not available right now.")
        return
    code = code.strip()
    if not code:
        await deny(interaction, "Type the stockpile's access code.")
        return
    try:
        result = parse_inventory(text, bot.catalog, await guild_faction(bot, guild_id))
    except ParseError as error:
        await deny(interaction, str(error))
        return
    if len(result.stocks) != 1:
        await deny(interaction, "That text holds several stockpiles. Paste the contents of one stockpile here.")
        return
    stock = result.stocks[0]
    if not stock.has_header:
        await deny(
            interaction,
            "The first line with the hex, town and stockpile name is missing. Copy the whole text from the game.",
        )
        return
    member = await interaction_member(interaction, guild_id)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return
    location = bot.locations.resolve(stock.hex, stock.region)
    stock.hex, stock.region = location.hex, location.region
    types = await bot.settings.get(guild_id, keys.STORAGE_TYPES)
    stock.store_type = canonical_store_type(stock.store_type, types) or (types[0] if types else "Storage Depot")
    stock.name = clip(stock.name.strip() or stock.store_type, 60)
    item_types = len({item.code for item in stock.items})
    existing = await match_stockpile(bot, member, stock)
    if existing is not None:
        await save_snapshot(
            bot, guild_id, stock, source=result.source, faction=result.faction, stockpile_id=existing["id"], user_id=interaction.user.id
        )
        bot.boards.request_refresh(guild_id, KIND)
        panel = module.StockpilePanel(bot, guild_id, existing["id"], interaction.user.id)
        await panel.send(
            interaction,
            notice=f"**{md(existing['name'])}** is already tracked, so no new stockpile was made. Its contents were updated "
            f"({plural(item_types, 'item type')}). The code and priority were not changed.",
        )
        return
    row = await module.create(
        bot,
        guild_id,
        name=stock.name,
        hex_name=stock.hex,
        region=stock.region,
        store_type=stock.store_type,
        priority=priority if priority in PRIORITIES else "Medium",
        access_code=code,
        private=private,
        user_id=interaction.user.id,
    )
    await save_snapshot(
        bot, guild_id, stock, source=result.source, faction=result.faction, stockpile_id=row["id"], user_id=interaction.user.id
    )
    bot.boards.request_refresh(guild_id, KIND)
    notes = [
        f"Stockpile **{md(row['name'])}** created from the export with {plural(item_types, 'item type')}. "
        "Press **Inventory** to see the contents.",
        module.location_feedback(stock.hex, stock.region, location.known),
    ]
    if stock.unknown:
        notes.append(clip(f"Not recognised: {', '.join(md(name) for name in stock.unknown)}", 500))
    if private:
        notes.append("Only you and admins can see it until you add people with **Access**.")
    panel = module.StockpilePanel(bot, guild_id, row["id"], interaction.user.id)
    await panel.send(interaction, notice="\n".join(notes))


async def show_find(bot: FoxBot, interaction: discord.Interaction, query: str) -> None:
    if not await check_group(interaction, GROUP):
        return
    member = await interaction_member(interaction)
    query = (query or "").strip()
    if not query:
        await deny(interaction, "Type an item name.")
        return
    faction = await guild_faction(bot, interaction.guild_id)
    item = resolve_query(bot.catalog, query, faction)
    needle = normalise(query)
    rows = await visible_latest(bot, member)
    contents = await items_for(bot, [snapshot["id"] for snapshot, _ in rows])
    results = []
    for snapshot, stockpile in rows:
        if item is not None:
            matched = [row for row in contents[snapshot["id"]] if row["item_code"] == item.code]
        else:
            matched = [
                row
                for row in contents[snapshot["id"]]
                if needle in normalise(row["item_name"]) or row["item_code"] == query
            ]
        for holding in holdings(matched).values():
            results.append((holding, snapshot, stockpile))
    results.sort(key=lambda r: (-r[0].units, display_name(r[1], r[2]).casefold()))
    label = item.name if item is not None else query
    lines = []
    if results:
        grand = sum(holding.units for holding, _, _ in results)
        places = len({group_key(snapshot) for _, snapshot, _ in results})
        lines.append(f"**{grand}** units in {plural(places, 'stockpile')} (latest import of each).")
        lines.append("")
    for holding, snapshot, stockpile in results:
        named = f" {md(holding.name)}:" if item is None else ":"
        lines.append(
            f"{describe(snapshot, stockpile)}{named} {amount_text(holding.crates, holding.loose, holding.units)}, "
            f"data {timeutil.relative(snapshot['imported_at'])}"
        )
    embeds = paginate_embeds(
        clip(f"Find: {label}", 256),
        lines,
        colour=Colour.INFO,
        empty=f"No imported stockpile you can see holds **{md(label)}**.",
        footer="Only you can see this. Data is as old as the last import.",
    )
    attachments: list[tuple[str, bytes]] = []
    if item is not None and itemimages.available():
        icon = await asyncio.to_thread(itemimages.render_icon, item.code, item.name)
        if icon is not None:
            attachments.append((ICON_FILENAME, icon))
            for embed in embeds:
                embed.set_thumbnail(url=f"attachment://{ICON_FILENAME}")
    await EmbedPaginator(embeds, owner_id=interaction.user.id, attachments=attachments).start(interaction)


async def show_totals(bot: FoxBot, interaction: discord.Interaction, category: str | None = None) -> None:
    if not await check_group(interaction, GROUP):
        return
    member = await interaction_member(interaction)
    rows = await visible_latest(bot, member)
    contents = await items_for(bot, [snapshot["id"] for snapshot, _ in rows])
    totals: dict[str, Holding] = {}
    for snapshot, _ in rows:
        for code, holding in holdings(contents[snapshot["id"]], snapshot["id"]).items():
            total = totals.get(code)
            if total is None:
                totals[code] = holding
                continue
            total.crates += holding.crates
            total.loose += holding.loose
            total.units += holding.units
            total.stockpiles |= holding.stockpiles
    grouped: dict[str, list[Holding]] = {}
    for holding in totals.values():
        if category and holding.category != category:
            continue
        grouped.setdefault(holding.category, []).append(holding)
    lines = []
    for name in sorted(grouped, key=lambda c: (category_index(c), c)):
        lines.append(f"**{md(name)}**")
        for holding in sorted(grouped[name], key=lambda h: h.name.casefold()):
            lines.append(
                f"{md(holding.name)}: {amount_text(holding.crates, holding.loose, holding.units)} "
                f"in {plural(len(holding.stockpiles), 'stockpile')}"
            )
    title = f"Inventory Totals: {category}" if category else "Inventory Totals"
    embeds = paginate_embeds(
        title,
        lines,
        colour=Colour.INFO,
        empty="No imported contents match." if rows else "No stockpile contents have been imported yet.",
        footer=f"Latest import of {plural(len(rows), 'stockpile')} you can see",
    )
    image = await render_grid(totals_sections(grouped))
    attachments: list[tuple[str, bytes]] = []
    if image is not None:
        attachments.append((GRID_FILENAME, image))
        embeds[0].set_image(url=f"attachment://{GRID_FILENAME}")
    await EmbedPaginator(embeds, owner_id=interaction.user.id, attachments=attachments, attachment_pages={0}).start(interaction)


def safe_cell(value) -> str:
    text = str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def iso_time(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(int(timestamp), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def importer_name(guild: discord.Guild | None, user_id: int) -> str:
    member = guild.get_member(user_id) if guild is not None else None
    return member.display_name if member is not None else str(user_id)


async def export_csv(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    member = await interaction_member(interaction)
    rows = await visible_latest(bot, member)
    if not rows:
        await reply(interaction, "No stockpile contents have been imported yet.")
        return
    contents = await items_for(bot, [snapshot["id"] for snapshot, _ in rows])
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    count = 0
    ordered = sorted(rows, key=lambda r: (display_location(*r).casefold(), display_name(*r).casefold()))
    for snapshot, stockpile in ordered:
        by = importer_name(interaction.guild, snapshot["imported_by"])
        items = sorted(
            contents[snapshot["id"]],
            key=lambda row: (category_index(category_of(row)), row["item_name"].casefold(), row["crated"]),
        )
        for row in items:
            writer.writerow(
                [
                    safe_cell(display_name(snapshot, stockpile)),
                    safe_cell(display_hex(snapshot, stockpile)),
                    safe_cell(display_region(snapshot, stockpile)),
                    safe_cell(display_type(snapshot, stockpile)),
                    safe_cell(row["item_name"]),
                    safe_cell(row["item_code"]),
                    safe_cell(category_of(row)),
                    "yes" if row["crated"] else "no",
                    row["quantity"],
                    row["per_crate"] if row["crated"] else 1,
                    row_units(row),
                    iso_time(snapshot["imported_at"]),
                    safe_cell(by),
                ]
            )
            count += 1
    data = buffer.getvalue().encode("utf-8-sig")
    filename = f"inventory-{dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')}.csv"
    await reply(
        interaction,
        f"Latest contents of {plural(len(rows), 'stockpile')} ({plural(count, 'row')}). Only stockpiles you can see are included.",
        file=discord.File(io.BytesIO(data), filename=filename),
    )


class InventoryBoard:
    kind = KIND
    title = "Inventory"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def public_latest(self, guild_id: int) -> list[tuple[dict, dict | None]]:
        stockpiles = await guild_stockpiles(self.bot, guild_id)
        result = []
        for snapshot in await latest_snapshots(self.bot, guild_id):
            stockpile = None
            if snapshot["stockpile_id"]:
                stockpile = stockpiles.get(snapshot["stockpile_id"])
                if stockpile is None or stockpile["private"]:
                    continue
            result.append((snapshot, stockpile))
        return result

    async def render(self, guild: discord.Guild) -> BoardRender:
        latest = await self.public_latest(guild.id)
        counts: dict[int, int] = {}
        if latest:
            contents = await items_for(self.bot, [snapshot["id"] for snapshot, _ in latest])
            counts = {snapshot_id: len(holdings(rows)) for snapshot_id, rows in contents.items()}
        now = timeutil.now()
        listed = [
            {
                "hex": display_hex(snapshot, stockpile),
                "region": display_region(snapshot, stockpile),
                "snapshot": snapshot,
                "stockpile": stockpile,
            }
            for snapshot, stockpile in latest
        ]

        def line(entry: dict) -> str:
            snapshot, stockpile = entry["snapshot"], entry["stockpile"]
            kind = display_type(snapshot, stockpile)
            tracked = "" if stockpile is not None else " [not tracked]"
            return (
                f"**{md(display_name(snapshot, stockpile))}**{f' ({md(kind)})' if kind else ''}{tracked} - "
                f"{plural(counts.get(snapshot['id'], 0), 'item type')}, updated {timeutil.relative(snapshot['imported_at'])}"
            )

        lines = entries.grouped_lines(listed, line, sort_key=lambda e: display_name(e["snapshot"], e["stockpile"]).casefold())
        embeds = fit_board_embeds(
            "Stockpile Inventory",
            [BOARD_HELP, "", *lines] if lines else [],
            colour=Colour.INFO,
            empty=f"{BOARD_HELP}\n\nNo stockpile contents have been imported yet.",
            footer=f"{plural(len(latest), 'stockpile')} with imported contents | Updates automatically",
            overflow_hint="Too many to show here. Use /inventory view or /find.",
        )
        recent = sorted(latest, key=lambda r: (-r[0]["imported_at"], -r[0]["id"]))[:25]
        options = [
            option(
                plain_label(snapshot, stockpile),
                contents_value(snapshot),
                f"{plural(counts.get(snapshot['id'], 0), 'item type')} | updated "
                f"{timeutil.format_duration(now - snapshot['imported_at'])} ago",
            )
            for snapshot, stockpile in recent
        ]
        return BoardRender(
            embeds=embeds,
            options=options,
            placeholder="View a stockpile's contents (latest imports first)...",
            buttons=[
                BoardButton("import", "Import Contents", discord.ButtonStyle.success),
                BoardButton("find", "Find Item", discord.ButtonStyle.primary),
                BoardButton("totals", "Totals"),
                BoardButton("export", "Export CSV"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action == "import":
            await start_import(self.bot, interaction)
        elif action == "find":
            if not await check_group(interaction, GROUP):
                return
            await interaction.response.send_modal(FindModal(self.bot))
        elif action == "totals":
            await show_totals(self.bot, interaction)
        elif action == "export":
            await export_csv(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        if not await check_group(interaction, GROUP):
            return
        await open_contents(self.bot, interaction, value)


class Inventory(commands.Cog):
    group = app_commands.Group(name="inventory", description="Import, search and export stockpile contents.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot

    @group.command(name="import", description="Paste a stockpile's in-game Copy to Clipboard text.")
    async def import_command(self, interaction: discord.Interaction) -> None:
        await start_import(self.bot, interaction)

    @group.command(name="upload", description="Import a .csv, .tsv or .txt file (in-game text, FIR or Foxhole Stockpiles app).")
    @app_commands.describe(file="The exported file, up to 1 MB")
    async def upload(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        if not await check_group(interaction, GROUP):
            return
        if not (file.filename or "").lower().endswith(UPLOAD_EXTENSIONS):
            await deny(interaction, "Upload a .csv, .tsv or .txt file.")
            return
        if file.size > UPLOAD_LIMIT:
            await deny(interaction, "That file is too large. The limit is 1 MB.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await file.read()
        except discord.HTTPException:
            await deny(interaction, "Could not download that file from Discord. Try again.")
            return
        await run_import(self.bot, interaction, interaction.guild_id, decode_upload(data))

    @group.command(name="view", description="Show the latest imported contents of a stockpile.")
    @app_commands.describe(stockpile="Search by name or location")
    async def view(self, interaction: discord.Interaction, stockpile: str) -> None:
        if not await check_group(interaction, GROUP):
            return
        await open_contents(self.bot, interaction, stockpile)

    @view.autocomplete("stockpile")
    async def view_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        member = await interaction_member(interaction)
        if member is None or not await self.bot.perms.has(member, GROUP):
            return []
        needle = normalise(current)
        choices = []
        for snapshot, stockpile in await visible_latest(self.bot, member):
            label = plain_label(snapshot, stockpile)
            if needle and needle not in normalise(f"{label} {display_type(snapshot, stockpile)}"):
                continue
            prefix = "[Private] " if stockpile is not None and stockpile["private"] else ""
            choices.append(app_commands.Choice(name=clip(prefix + label, 100), value=contents_value(snapshot)))
        return choices[:25]

    @group.command(name="totals", description="Totals by category across the latest imports you can see.")
    @app_commands.describe(category="Only show one category")
    @app_commands.choices(category=[app_commands.Choice(name=name, value=name) for name in CATEGORIES])
    async def totals(self, interaction: discord.Interaction, category: app_commands.Choice[str] | None = None) -> None:
        await show_totals(self.bot, interaction, category.value if category else None)

    @group.command(name="export", description="Download the latest imported contents as a CSV file.")
    async def export(self, interaction: discord.Interaction) -> None:
        await export_csv(self.bot, interaction)

    @group.command(name="board", description="Admin: post the inventory board in this channel (moves it if it exists).")
    async def board(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.boards.post(interaction.guild, KIND, interaction.channel)
        except discord.HTTPException as error:
            await reply(interaction, f"Could not post the board here: {error.text or error}")
            return
        await reply(interaction, "Inventory board posted.")

    @app_commands.command(name="find", description="Find which stockpiles hold an item (from the latest imports).")
    @app_commands.guild_only()
    @app_commands.describe(item="Start typing an item name")
    async def find(self, interaction: discord.Interaction, item: app_commands.Range[str, 1, 100]) -> None:
        await show_find(self.bot, interaction, item)

    @find.autocomplete("item")
    async def find_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        faction = await guild_faction(self.bot, interaction.guild_id) if interaction.guild_id else None
        return self.bot.catalog.choices(current, faction)


async def setup(bot: FoxBot) -> None:
    bot.boards.register(InventoryBoard(bot))
    await bot.add_cog(Inventory(bot))
