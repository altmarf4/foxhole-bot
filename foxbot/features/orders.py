from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import Colour
from foxbot.core import entries, ids, timeutil
from foxbot.core import settings as keys
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.catalog import Catalog, Item, name_key, search_key
from foxbot.core.locations import Location
from foxbot.core.locpicker import LocationPicker, recent_hexes
from foxbot.core.permissions import check_admin, check_group, interaction_member
from foxbot.core.text import EMBED_FIELD_LIMIT, EMBED_TOTAL_LIMIT, chunk_lines, clip, fit_board_embeds, md, plural
from foxbot.core.ui import (
    BaseModal,
    BaseView,
    ConfirmView,
    PickerView,
    PickOption,
    deny,
    edit,
    option,
    reply,
    report_error,
    text_field,
    text_value,
)

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

KIND = "orders"
GROUP = "orders"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

ORDER_TYPES = {
    "Production": Colour.INFO,
    "Transport": Colour.ORANGE,
    "Scrap": 0x1ABC9C,
}
ORDER_TYPE_HINTS = {
    "Production": "Items to craft at a factory or MPF",
    "Transport": "Goods to haul to a destination",
    "Scrap": "Salvage and other resources to gather",
}
FACTION_ALIASES = {
    "warden": "warden",
    "wardens": "warden",
    "colonial": "colonial",
    "colonials": "colonial",
}

MAX_LINES = 20
MAX_TARGET = 1_000_000
DONE_CEILING = 100 * MAX_TARGET
TITLE_MAX = 100
ITEM_NAME_MAX = 80
NOTES_MAX = 1000
LINES_TEXT_MAX = 4000
DESTINATION_MAX = 100
BAR_BLOCKS = 10
FULL_BLOCK = "█"
EMPTY_BLOCK = "░"
TOP_CONTRIBUTORS = 5
QUICK_AMOUNTS = (1, 4, 9)
RECENT_CLOSED_DAYS = 7
CARD_LINE_LIMIT = 190
ERRORS_SHOWN = 10
MESSAGE_LIMIT = 2000

CARD_BUTTONS = {
    "contribute": ("Contribute", discord.ButtonStyle.success),
    "edit": ("Edit Lines", discord.ButtonStyle.secondary),
    "close": ("Close", discord.ButtonStyle.secondary),
    "reopen": ("Reopen", discord.ButtonStyle.success),
    "delete": ("Delete", discord.ButtonStyle.danger),
}

CARD_FAILED = (
    "The order was saved, but I could not post its card here or in the orders channel. "
    "Use `/order list` and **Repost Card** in a channel where I can send messages."
)

BULLET = re.compile(r"^[-*•]\s+")
NEGATIVE = re.compile(r"^-\s*\d")
AMOUNT_FIRST = re.compile(r"^(?P<amount>\d[\d,]*)(?:\s*[x×*]\s+|\s+)(?P<item>\S.*)$", re.IGNORECASE)
AMOUNT_LAST = re.compile(r"^(?P<item>.*?\S)(?:\s*[:=*×-]\s*|\s+x\s*|\s+)(?P<amount>\d[\d,]*)$", re.IGNORECASE)
WHOLE_NUMBER = re.compile(r"[+-]?\d+")


@dataclass
class ParsedLine:
    item_code: str | None
    item_name: str
    target: int
    typed: str


@dataclass
class ParseResult:
    lines: list[ParsedLine] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    guessed: list[tuple[str, str]] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)


def catalog_faction(value) -> str | None:
    if not value:
        return None
    return FACTION_ALIASES.get(str(value).strip().casefold())


async def guild_faction(bot: FoxBot, guild_id: int) -> str | None:
    return catalog_faction(await bot.settings.get(guild_id, keys.FACTION))


def match_item(catalog: Catalog, typed: str, faction: str | None) -> tuple[Item | None, bool]:
    item, _ = catalog.resolve_line(typed, faction)
    if item is not None:
        return item, True
    folded = typed.casefold()
    for code, candidate in catalog.items.items():
        if code.casefold() == folded:
            return candidate, True
    key = search_key(typed)
    if len(key) < 3:
        return None, False
    variants = [key]
    if key.endswith("s") and len(key) > 3:
        variants.append(key[:-1])
    for variant in variants:
        for hit in catalog.search(variant, faction, limit=10):
            hit_key = search_key(hit.name)
            if hit_key.startswith(variant) or (variant in hit_key and len(variant) * 2 >= len(hit_key)):
                return hit, False
    return None, False


def parse_lines(catalog: Catalog, text: str, faction: str | None) -> ParseResult:
    result = ParseResult()
    by_key: dict[str, ParsedLine] = {}
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = BULLET.sub("", raw.strip()).strip()
        if not line:
            continue
        shown = md(clip(line, 40))
        if NEGATIVE.match(line):
            result.errors.append(f"Line {number} ({shown}): amounts must be positive whole numbers.")
            continue
        match = AMOUNT_FIRST.match(line) or AMOUNT_LAST.match(line)
        if match is None:
            result.errors.append(f"Line {number} ({shown}): write an amount and an item, e.g. 50 Basic Materials.")
            continue
        amount = int(match["amount"].replace(",", ""))
        typed = " ".join(match["item"].split())
        if amount <= 0:
            result.errors.append(f"Line {number} ({shown}): the amount must be at least 1.")
            continue
        if amount > MAX_TARGET:
            result.errors.append(f"Line {number} ({shown}): the most one line can ask for is {MAX_TARGET:,}.")
            continue
        item, exact = match_item(catalog, typed, faction)
        name = item.name if item is not None else typed
        if len(name) > ITEM_NAME_MAX:
            result.errors.append(f"Line {number} ({shown}): item names can be at most {ITEM_NAME_MAX} characters.")
            continue
        key = item.code if item is not None else "text:" + name_key(name)
        existing = by_key.get(key)
        if existing is not None:
            existing.target += amount
            if name not in result.merged:
                result.merged.append(name)
            continue
        parsed = ParsedLine(item.code if item is not None else None, name, amount, typed)
        by_key[key] = parsed
        result.lines.append(parsed)
        if item is None:
            result.unmatched.append(name)
        elif not exact and search_key(typed) != search_key(item.name):
            result.guessed.append((typed, item.name))
    for parsed in result.lines:
        if parsed.target > MAX_TARGET:
            result.errors.append(f"{md(parsed.item_name)}: repeated lines add up to more than {MAX_TARGET:,}.")
    if len(result.lines) > MAX_LINES:
        result.errors.append(f"An order can have at most {MAX_LINES} lines; this one has {len(result.lines)}.")
    if not result.lines and not result.errors:
        result.errors.append("Add at least one line, e.g. 50 Basic Materials.")
    return result


def lines_to_text(lines: list[dict]) -> str:
    return "\n".join(f"{line['target']} {line['item_name']}" for line in lines)


def parse_report(result: ParseResult) -> list[str]:
    report = []
    if result.guessed:
        pairs = ", ".join(f"{md(typed)} -> {md(name)}" for typed, name in result.guessed)
        report.append(clip(f"Matched to the item catalog: {pairs}.", 600))
    if result.unmatched:
        report.append(clip(f"Kept as typed (not found in the item catalog): {', '.join(md(n) for n in result.unmatched)}.", 600))
    if result.merged:
        report.append(clip(f"Repeated items were added together: {', '.join(md(n) for n in result.merged)}.", 400))
    return report


def errors_text(result: ParseResult) -> str:
    shown = result.errors[:ERRORS_SHOWN]
    text = "The order was not saved:\n" + "\n".join(f"- {error}" for error in shown)
    if len(result.errors) > len(shown):
        text += f"\n- and {len(result.errors) - len(shown)} more problem(s)"
    text += "\nPress **Fix and Try Again** to correct it. One line per item, e.g. `50 Basic Materials` or `20 x Mortar Shell`."
    return clip(text, MESSAGE_LIMIT)


async def fetch(bot: FoxBot, guild_id: int, order_id: str) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM orders WHERE guild_id = ? AND id = ?", (guild_id, order_id))


async def fetch_lines(bot: FoxBot, order_id: str) -> list[dict]:
    return await bot.db.fetchall("SELECT * FROM order_lines WHERE order_id = ? ORDER BY position, id", (order_id,))


async def fetch_line(bot: FoxBot, order_id: str, line_id: int) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM order_lines WHERE order_id = ? AND id = ?", (order_id, line_id))


async def list_orders(bot: FoxBot, guild_id: int, *, status: str = STATUS_OPEN) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT * FROM orders WHERE guild_id = ? AND status = ? ORDER BY created_at, rowid",
        (guild_id, status),
    )


async def list_recent(bot: FoxBot, guild_id: int, since: int) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT * FROM orders WHERE guild_id = ? AND (status = ? OR closed_at >= ?) "
        "ORDER BY status = ?, created_at DESC, rowid DESC",
        (guild_id, STATUS_OPEN, since, STATUS_CLOSED),
    )


async def progress_by_order(bot: FoxBot, guild_id: int) -> dict[str, dict]:
    rows = await bot.db.fetchall(
        "SELECT l.order_id AS order_id, SUM(MIN(l.done, l.target)) AS done, SUM(l.target) AS target, "
        "COUNT(*) AS lines, SUM(l.done >= l.target) AS complete "
        "FROM order_lines l JOIN orders o ON o.id = l.order_id WHERE o.guild_id = ? GROUP BY l.order_id",
        (guild_id,),
    )
    return {row["order_id"]: row for row in rows}


async def top_contributors(bot: FoxBot, order_id: str) -> tuple[list[dict], int]:
    rows = await bot.db.fetchall(
        "SELECT user_id, SUM(amount) AS total FROM order_contributions WHERE order_id = ? "
        "GROUP BY user_id HAVING SUM(amount) > 0 ORDER BY total DESC, MIN(id)",
        (order_id,),
    )
    return rows[:TOP_CONTRIBUTORS], len(rows)


async def user_total(bot: FoxBot, line_id: int, user_id: int) -> int:
    return await bot.db.fetchval(
        "SELECT SUM(amount) FROM order_contributions WHERE line_id = ? AND user_id = ?",
        (line_id, user_id),
        0,
    )


async def create(
    bot: FoxBot,
    guild_id: int,
    *,
    order_type: str,
    title: str,
    hex_name: str,
    region: str,
    notes: str,
    lines: list[ParsedLine],
    user_id: int,
) -> dict:
    order_id = await ids.unique_id(bot.db, "orders")
    now = timeutil.now()
    async with bot.db.transaction() as tx:
        await tx.execute(
            "INSERT INTO orders (id, guild_id, order_type, title, hex, region, notes, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, guild_id, order_type, title, hex_name, region, notes, STATUS_OPEN, user_id, now, now),
        )
        await tx.executemany(
            "INSERT INTO order_lines (order_id, position, item_code, item_name, target, done) VALUES (?, ?, ?, ?, ?, 0)",
            [(order_id, position, line.item_code, line.item_name, line.target) for position, line in enumerate(lines)],
        )
    bot.boards.request_refresh(guild_id, KIND)
    return await fetch(bot, guild_id, order_id)


async def update(bot: FoxBot, order: dict, **fields) -> dict | None:
    fields["updated_at"] = timeutil.now()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    await bot.db.execute(
        f"UPDATE orders SET {assignments} WHERE guild_id = ? AND id = ?",
        (*fields.values(), order["guild_id"], order["id"]),
    )
    bot.boards.request_refresh(order["guild_id"], KIND)
    return await fetch(bot, order["guild_id"], order["id"])


async def replace_lines(bot: FoxBot, order_id: str, lines: list[ParsedLine]) -> None:
    async with bot.db.transaction() as tx:
        existing = await tx.fetchall("SELECT * FROM order_lines WHERE order_id = ? ORDER BY position, id", (order_id,))
        by_code = {row["item_code"]: row for row in existing if row["item_code"]}
        by_name = {name_key(row["item_name"]): row for row in reversed(existing)}
        used: set[int] = set()
        for position, line in enumerate(lines):
            match = by_code.get(line.item_code) if line.item_code else None
            if match is None or match["id"] in used:
                match = by_name.get(name_key(line.item_name))
            if match is not None and match["id"] not in used:
                used.add(match["id"])
                await tx.execute(
                    "UPDATE order_lines SET position = ?, item_code = ?, item_name = ?, target = ? WHERE id = ?",
                    (position, line.item_code, line.item_name, line.target, match["id"]),
                )
            else:
                await tx.execute(
                    "INSERT INTO order_lines (order_id, position, item_code, item_name, target, done) VALUES (?, ?, ?, ?, ?, 0)",
                    (order_id, position, line.item_code, line.item_name, line.target),
                )
        for row in existing:
            if row["id"] not in used:
                await tx.execute("DELETE FROM order_contributions WHERE line_id = ?", (row["id"],))
                await tx.execute("DELETE FROM order_lines WHERE id = ?", (row["id"],))


async def contribute(
    bot: FoxBot,
    order_id: str,
    line_id: int,
    user_id: int,
    amount: int,
    *,
    fill: bool = False,
) -> tuple[dict | None, int]:
    now = timeutil.now()
    async with bot.db.transaction() as tx:
        line = await tx.fetchone("SELECT * FROM order_lines WHERE id = ? AND order_id = ?", (line_id, order_id))
        if line is None:
            return None, 0
        if fill:
            amount = max(0, line["target"] - line["done"])
        new_done = min(max(0, line["done"] + amount), DONE_CEILING)
        applied = new_done - line["done"]
        if applied:
            await tx.execute("UPDATE order_lines SET done = ? WHERE id = ?", (new_done, line_id))
            await tx.execute(
                "INSERT INTO order_contributions (order_id, line_id, user_id, amount, created_at) VALUES (?, ?, ?, ?, ?)",
                (order_id, line_id, user_id, applied, now),
            )
            await tx.execute("UPDATE orders SET updated_at = ? WHERE id = ?", (now, order_id))
        line["done"] = new_done
    return line, applied


async def set_status(bot: FoxBot, order: dict, status: str, user_id: int) -> dict | None:
    now = timeutil.now()
    if status == STATUS_CLOSED:
        return await update(bot, order, status=status, closed_by=user_id, closed_at=now)
    return await update(bot, order, status=status, closed_by=None, closed_at=None)


async def set_message(bot: FoxBot, order_id: str, channel_id: int | None, message_id: int | None) -> None:
    await bot.db.execute(
        "UPDATE orders SET channel_id = ?, message_id = ? WHERE id = ?",
        (channel_id, message_id, order_id),
    )


async def delete(bot: FoxBot, order: dict) -> None:
    async with bot.db.transaction() as tx:
        await tx.execute("DELETE FROM order_contributions WHERE order_id = ?", (order["id"],))
        await tx.execute("DELETE FROM order_lines WHERE order_id = ?", (order["id"],))
        await tx.execute("DELETE FROM orders WHERE guild_id = ? AND id = ?", (order["guild_id"], order["id"]))
    if order["message_id"]:
        await delete_message(bot, order["channel_id"], order["message_id"])
    bot.boards.request_refresh(order["guild_id"], KIND)


async def can_manage(bot: FoxBot, member: discord.Member, order: dict) -> bool:
    return member.id == order["created_by"] or await bot.perms.is_admin(member)


def destination_of(order: dict) -> str:
    if order["hex"] or order["region"]:
        return entries.location_label(order["hex"], order["region"])
    return ""


def jump_link(order: dict) -> str | None:
    if not order.get("channel_id") or not order.get("message_id"):
        return None
    return f"https://discord.com/channels/{order['guild_id']}/{order['channel_id']}/{order['message_id']}"


def link_text(text: str) -> str:
    return md(text).replace("\\[", "(").replace("[", "(").replace("]", ")")


def progress_bar(done: int, target: int) -> str:
    filled = BAR_BLOCKS if target <= 0 else min(BAR_BLOCKS, max(0, done) * BAR_BLOCKS // target)
    return FULL_BLOCK * filled + EMPTY_BLOCK * (BAR_BLOCKS - filled)


def percent(done: int, target: int) -> int:
    if target <= 0:
        return 100
    return min(100, max(0, done) * 100 // target)


def is_line_complete(line: dict) -> bool:
    return line["done"] >= line["target"]


def is_complete(lines: list[dict]) -> bool:
    return bool(lines) and all(is_line_complete(line) for line in lines)


def overall(lines: list[dict]) -> tuple[int, int]:
    return sum(min(line["done"], line["target"]) for line in lines), sum(line["target"] for line in lines)


def line_progress_text(line: dict) -> str:
    text = f"{line['done']:,}/{line['target']:,} ({percent(line['done'], line['target'])}%)"
    return text + " - complete" if is_line_complete(line) else text


def card_line(line: dict) -> str:
    text = f"`{progress_bar(line['done'], line['target'])}` **{md(line['item_name'])}** {line_progress_text(line)}"
    return clip(text, CARD_LINE_LIMIT)


def status_text(order: dict, lines: list[dict]) -> str:
    if order["status"] == STATUS_CLOSED:
        who = f" by <@{order['closed_by']}>" if order.get("closed_by") else ""
        when = f" {timeutil.relative(order['closed_at'])}" if order.get("closed_at") else ""
        return f"Closed{who}{when}"
    if is_complete(lines):
        return "Completed - the creator or an admin can close it"
    return "Open"


def overall_text(lines: list[dict]) -> str:
    done, target = overall(lines)
    complete = sum(1 for line in lines if is_line_complete(line))
    return f"`{progress_bar(done, target)}` {percent(done, target)}% - {complete}/{len(lines)} lines complete"


def contributors_text(top: list[dict], count: int) -> str:
    if not top:
        return "No contributions yet."
    rows = [f"{index}. <@{row['user_id']}> - {row['total']:,}" for index, row in enumerate(top, start=1)]
    if count > len(top):
        rows.append(f"and {count - len(top)} more")
    return clip("\n".join(rows), EMBED_FIELD_LIMIT)


def card_colour(order: dict, lines: list[dict]) -> int:
    if order["status"] == STATUS_CLOSED:
        return Colour.MUTED
    if is_complete(lines):
        return Colour.SUCCESS
    return ORDER_TYPES.get(order["order_type"], Colour.INFO)


def card_embed(order: dict, lines: list[dict], top: list[dict], contributor_count: int) -> discord.Embed:
    closed = order["status"] == STATUS_CLOSED
    title = f"{'[Closed] ' if closed else ''}{order['order_type']}: {order['title']}"
    description = "\n".join(card_line(line) for line in lines) or "This order has no lines."
    embed = discord.Embed(title=clip(title, 256), description=description, colour=card_colour(order, lines))
    destination = destination_of(order)
    embed.add_field(name="Destination", value=clip(md(destination), EMBED_FIELD_LIMIT) if destination else "Not set", inline=True)
    embed.add_field(name="Status", value=status_text(order, lines), inline=True)
    embed.add_field(name="Overall", value=overall_text(lines), inline=False)
    if order["notes"]:
        embed.add_field(name="Notes", value=clip(md(order["notes"]), EMBED_FIELD_LIMIT), inline=False)
    embed.add_field(name="Top contributors", value=contributors_text(top, contributor_count), inline=False)
    embed.add_field(name="Created by", value=f"<@{order['created_by']}> {timeutil.relative(order['created_at'])}", inline=False)
    hint = "Closed" if closed else "Pick a line below to contribute"
    embed.set_footer(text=f"Order {order['id']} | {hint}")
    if len(embed) > EMBED_TOTAL_LIMIT and order["notes"]:
        index = next(i for i, f in enumerate(embed.fields) if f.name == "Notes")
        room = max(20, EMBED_FIELD_LIMIT - (len(embed) - EMBED_TOTAL_LIMIT) - 10)
        embed.set_field_at(index, name="Notes", value=clip(md(order["notes"]), room), inline=False)
    return embed


def line_option(line: dict, *, default: bool = False) -> discord.SelectOption:
    return option(line["item_name"], str(line["id"]), line_progress_text(line), default=default)


def card_view(order: dict, lines: list[dict]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if order["status"] == STATUS_OPEN:
        if lines:
            view.add_item(OrderLineSelectItem(order["id"], [line_option(line) for line in lines[:25]]).item)
        for action in ("contribute", "edit", "close", "delete"):
            view.add_item(OrderButtonItem(action, order["id"], row=1).item)
    else:
        for action in ("reopen", "delete"):
            view.add_item(OrderButtonItem(action, order["id"], row=0).item)
    return view


async def card_payload(bot: FoxBot, order: dict) -> tuple[discord.Embed, discord.ui.View]:
    lines = await fetch_lines(bot, order["id"])
    top, count = await top_contributors(bot, order["id"])
    return card_embed(order, lines, top, count), card_view(order, lines)


def can_send(guild: discord.Guild | None, channel) -> bool:
    me = getattr(guild, "me", None) if guild is not None else None
    permissions_for = getattr(channel, "permissions_for", None)
    if me is None or permissions_for is None:
        return True
    try:
        permissions = permissions_for(me)
    except (AttributeError, TypeError):
        return True
    return bool(permissions.view_channel and permissions.send_messages and permissions.embed_links)


async def post_card(bot: FoxBot, order: dict, fallback) -> discord.Message | None:
    embed, view = await card_payload(bot, order)
    targets = []
    configured = await bot.settings.get(order["guild_id"], keys.ORDERS_CHANNEL)
    guild = bot.get_guild(order["guild_id"])
    if configured and guild is not None:
        channel = guild.get_channel(configured)
        if channel is not None and can_send(guild, channel):
            targets.append(channel)
    if fallback is not None and all(getattr(t, "id", None) != getattr(fallback, "id", None) for t in targets):
        targets.append(fallback)
    for channel in targets:
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            log.warning("Could not post order card %s in channel %s", order["id"], getattr(channel, "id", "?"), exc_info=True)
            continue
        await set_message(bot, order["id"], message.channel.id, message.id)
        return message
    return None


async def delete_message(bot: FoxBot, channel_id: int | None, message_id: int | None) -> None:
    if not channel_id or not message_id:
        return
    try:
        await bot.get_partial_messageable(channel_id).get_partial_message(message_id).delete()
    except discord.HTTPException:
        pass


_card_busy: set[str] = set()
_card_dirty: set[str] = set()


async def _edit_card(bot: FoxBot, guild_id: int, order_id: str) -> None:
    order = await fetch(bot, guild_id, order_id)
    if order is None or not order["message_id"]:
        return
    embed, view = await card_payload(bot, order)
    message = bot.get_partial_messageable(order["channel_id"], guild_id=guild_id).get_partial_message(order["message_id"])
    try:
        await message.edit(embed=embed, view=view)
    except discord.NotFound:
        log.info("Card of order %s was deleted; keeping the order without a card", order_id)
        await bot.db.execute(
            "UPDATE orders SET channel_id = NULL, message_id = NULL WHERE id = ? AND message_id = ?",
            (order_id, order["message_id"]),
        )
    except discord.HTTPException:
        log.warning("Could not update the card of order %s", order_id, exc_info=True)


async def refresh_card(bot: FoxBot, guild_id: int, order_id: str) -> None:
    if order_id in _card_busy:
        _card_dirty.add(order_id)
        return
    _card_busy.add(order_id)
    try:
        while True:
            _card_dirty.discard(order_id)
            await _edit_card(bot, guild_id, order_id)
            if order_id not in _card_dirty:
                return
    finally:
        _card_busy.discard(order_id)


async def after_change(bot: FoxBot, guild_id: int, order_id: str) -> None:
    await refresh_card(bot, guild_id, order_id)
    bot.boards.request_refresh(guild_id, KIND)


def location_feedback(location: Location) -> str:
    label = entries.location_label(location.hex, location.region)
    if location.known:
        return f"Destination: **{md(label)}**"
    return f"Destination **{md(label)}** was not found on the war map, so it was saved exactly as typed."


async def load_managed(bot: FoxBot, interaction: discord.Interaction, guild_id: int, order_id: str) -> dict | None:
    order = await fetch(bot, guild_id, order_id)
    if order is None:
        await deny(interaction, "This order no longer exists.")
        return None
    if not await check_group(interaction, GROUP, guild_id):
        return None
    member = await interaction_member(interaction, guild_id)
    if member is None or not await can_manage(bot, member, order):
        await deny(interaction, "Only the order's creator or an admin can do that.")
        return None
    return order


class OrderModal(BaseModal):
    def __init__(
        self,
        bot: FoxBot,
        guild_id: int,
        *,
        order_type: str,
        location: Location | None = None,
        entry: dict | None = None,
        title_text: str | None = None,
        lines_text: str | None = None,
        notes_text: str | None = None,
        destination_text: str | None = None,
        replace_origin: bool = False,
    ):
        heading = f"Edit {order_type} Order" if entry is not None else f"New {order_type} Order"
        super().__init__(title=clip(heading, 45))
        self.bot = bot
        self.guild_id = guild_id
        self.order_type = order_type
        self.location = location
        self.entry = entry
        self.replace_origin = replace_origin
        self.title_field = text_field(
            "Title",
            default=clip(title_text, TITLE_MAX) if title_text else None,
            placeholder="e.g. Frontline ammo for Westgate",
            max_length=TITLE_MAX,
        )
        self.destination_field = None
        if entry is not None:
            self.destination_field = text_field(
                "Destination",
                default=clip(destination_text, DESTINATION_MAX) if destination_text else None,
                placeholder="e.g. Syrinx Pass. Leave empty for no destination.",
                description="Town or region. Add the hex after a comma if it is ambiguous.",
                required=False,
                max_length=DESTINATION_MAX,
            )
        self.lines_field = text_field(
            "Lines",
            default=lines_text or None,
            placeholder="50 Basic Materials\n20 x Mortar Shell\n10 Soldier Supplies",
            description=f"One item per line: amount, then item name. At most {MAX_LINES} lines.",
            paragraph=True,
            max_length=LINES_TEXT_MAX,
        )
        self.notes_field = text_field(
            "Notes",
            default=notes_text or None,
            placeholder="Optional: delivery details, priorities, who to contact",
            required=False,
            paragraph=True,
            max_length=NOTES_MAX,
        )
        for item in (self.title_field, self.destination_field, self.lines_field, self.notes_field):
            if item is not None:
                self.add_item(item)

    def again(self) -> OrderModal:
        return OrderModal(
            self.bot,
            self.guild_id,
            order_type=self.order_type,
            location=self.location,
            entry=self.entry,
            title_text=text_value(self.title_field) or None,
            lines_text=text_value(self.lines_field) or None,
            notes_text=text_value(self.notes_field) or None,
            destination_text=text_value(self.destination_field) if self.destination_field is not None else None,
            replace_origin=True,
        )

    async def _respond(self, interaction: discord.Interaction, notice: str) -> None:
        if self.replace_origin:
            await edit(interaction, content=clip(notice, MESSAGE_LIMIT), embed=None, view=None)
        else:
            await reply(interaction, clip(notice, MESSAGE_LIMIT))

    async def _defer(self, interaction: discord.Interaction) -> None:
        if self.replace_origin:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.guild_id):
            return
        current = None
        if self.entry is not None:
            current = await load_managed(self.bot, interaction, self.guild_id, self.entry["id"])
            if current is None:
                return
            if current["status"] != STATUS_OPEN:
                await deny(interaction, "This order is closed. Reopen it before editing it.")
                return
        title = text_value(self.title_field)
        if not title:
            await reply(interaction, "The order needs a title.", view=RetryView(self, interaction.user.id))
            return
        result = parse_lines(self.bot.catalog, text_value(self.lines_field), await guild_faction(self.bot, self.guild_id))
        if result.errors:
            await reply(interaction, errors_text(result), view=RetryView(self, interaction.user.id))
            return
        notes = text_value(self.notes_field)
        if current is None:
            await self._create(interaction, title, notes, result)
        else:
            await self._update(interaction, current, title, notes, result)

    async def _create(self, interaction: discord.Interaction, title: str, notes: str, result: ParseResult) -> None:
        await self._defer(interaction)
        location = self.location
        order = await create(
            self.bot,
            self.guild_id,
            order_type=self.order_type,
            title=title,
            hex_name=location.hex if location else "",
            region=location.region if location else "",
            notes=notes,
            lines=result.lines,
            user_id=interaction.user.id,
        )
        message = await post_card(self.bot, order, interaction.channel)
        parts = [f"{self.order_type} order **{md(title)}** created with {plural(len(result.lines), 'line')}."]
        if message is not None:
            order = await fetch(self.bot, self.guild_id, order["id"]) or order
            parts.append(f"[Open the card]({jump_link(order)})")
        else:
            parts.append(CARD_FAILED)
        if location is not None:
            parts.append(location_feedback(location))
        parts.extend(parse_report(result))
        await self._respond(interaction, "\n".join(parts))

    async def _update(self, interaction: discord.Interaction, current: dict, title: str, notes: str, result: ParseResult) -> None:
        destination = text_value(self.destination_field) if self.destination_field is not None else destination_of(current)
        location = None
        hex_name, region = current["hex"], current["region"]
        if destination != destination_of(current):
            location = self.bot.locations.resolve_text(destination) if destination else Location("", "", True)
            hex_name, region = location.hex, location.region
        await self._defer(interaction)
        await update(self.bot, current, title=title, notes=notes, hex=hex_name, region=region)
        await replace_lines(self.bot, current["id"], result.lines)
        await after_change(self.bot, self.guild_id, current["id"])
        parts = [f"Order **{md(title)}** updated. It now has {plural(len(result.lines), 'line')}; amounts already delivered were kept."]
        if location is not None and (location.hex or location.region):
            parts.append(location_feedback(location))
        elif location is not None:
            parts.append("The destination was removed.")
        parts.extend(parse_report(result))
        await self._respond(interaction, "\n".join(parts))


class RetryView(BaseView):
    def __init__(self, modal: OrderModal, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.modal = modal
        button = discord.ui.Button(label="Fix and Try Again", style=discord.ButtonStyle.primary)
        button.callback = self._retry
        self.add_item(button)

    async def _retry(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.modal.guild_id):
            return
        await interaction.response.send_modal(self.modal.again())


class CustomAmountModal(BaseModal, title="Custom Amount"):
    def __init__(self, panel: LinePanel, *, manager: bool):
        super().__init__()
        self.panel = panel
        self.amount_field = text_field(
            "Amount",
            placeholder="e.g. 25, or -5 to correct a mistake" if manager else "e.g. 25",
            description="A negative amount subtracts, to fix mistakes." if manager else "Whole number added to this line.",
            max_length=9,
        )
        self.add_item(self.amount_field)
        self.credit_field = None
        if manager:
            self.credit_field = discord.ui.Label(
                text="Credit to (optional)",
                description="Record it for someone else, e.g. to fix their entry. Leave empty for yourself.",
                component=discord.ui.UserSelect(min_values=0, max_values=1, required=False),
            )
            self.add_item(self.credit_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        loaded = await self.panel.load_checked(interaction)
        if loaded is None:
            return
        order, line = loaded
        raw = text_value(self.amount_field).replace(",", "").replace(" ", "")
        if not WHOLE_NUMBER.fullmatch(raw):
            await self.panel.show(interaction, notice="Enter a whole number, e.g. 25.")
            return
        amount = int(raw)
        if amount == 0 or abs(amount) > MAX_TARGET:
            await self.panel.show(interaction, notice=f"Enter an amount between 1 and {MAX_TARGET:,}.")
            return
        member = await interaction_member(interaction, order["guild_id"])
        manager = member is not None and await can_manage(self.panel.bot, member, order)
        if amount < 0 and not manager:
            await self.panel.show(interaction, notice="Only the order's creator or an admin can subtract amounts.")
            return
        credit_to = interaction.user.id
        if manager and self.credit_field is not None and self.credit_field.component.values:
            credit_to = self.credit_field.component.values[0].id
        await self.panel.apply(interaction, order, line, amount, credit_to=credit_to)


def panel_embed(order: dict, line: dict, lines: list[dict], mine: int) -> discord.Embed:
    remaining = line["target"] - line["done"]
    rows = [
        f"Order: **{md(order['title'])}** ({order['order_type']})",
        f"`{progress_bar(line['done'], line['target'])}` {line_progress_text(line)}",
    ]
    if remaining > 0:
        rows.append(f"Still needed: **{remaining:,}**")
    elif remaining < 0:
        rows.append(f"Complete, with {-remaining:,} extra.")
    else:
        rows.append("This line is complete.")
    rows.append(f"Your contributions to this line: **{mine:,}**")
    if is_complete(lines):
        rows.append("Every line of this order is complete.")
    embed = discord.Embed(title=clip(line["item_name"], 256), description="\n".join(rows), colour=card_colour(order, lines))
    embed.set_footer(text="Only you see this panel. Each button adds to the line right away.")
    return embed


class LinePanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, order_id: str, line_id: int, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.order_id = order_id
        self.line_id = line_id

    async def load_checked(self, interaction: discord.Interaction) -> tuple[dict, dict] | None:
        order = await fetch(self.bot, self.guild_id, self.order_id)
        if order is None:
            await edit(interaction, content="This order no longer exists.", embed=None, view=None)
            return None
        member = await interaction_member(interaction, self.guild_id)
        if member is None or not await self.bot.perms.has(member, GROUP):
            await deny(interaction, "You need the **Orders** permission to contribute.")
            return None
        if order["status"] != STATUS_OPEN:
            await edit(interaction, content="This order was closed, so it no longer takes contributions.", embed=None, view=None)
            return None
        line = await fetch_line(self.bot, self.order_id, self.line_id)
        if line is None:
            await edit(interaction, content="That line was removed from the order. Open the card again to pick another.", embed=None, view=None)
            return None
        return order, line

    def _button(self, label: str, style: discord.ButtonStyle, callback, *, row: int, disabled: bool = False) -> None:
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def _build(self, order: dict, lines: list[dict], line: dict) -> None:
        self.clear_items()
        for amount in QUICK_AMOUNTS:
            self._button(f"+{amount}", discord.ButtonStyle.primary, lambda interaction, value=amount: self._quick(interaction, value), row=0)
        self._button("Max", discord.ButtonStyle.success, self._max, row=0, disabled=line["done"] >= line["target"])
        self._button("Custom", discord.ButtonStyle.secondary, self._custom, row=0)
        if len(lines) > 1:
            select = discord.ui.Select(
                placeholder="Switch to another line...",
                options=[line_option(other, default=other["id"] == line["id"]) for other in lines[:25]],
                row=1,
            )
            select.callback = self._switch
            self.add_item(select)
        link = jump_link(order)
        if link:
            self.add_item(discord.ui.Button(label="Open Card", url=link, row=2))

    async def render(self, interaction: discord.Interaction) -> discord.Embed | None:
        order = await fetch(self.bot, self.guild_id, self.order_id)
        if order is None:
            return None
        lines = await fetch_lines(self.bot, self.order_id)
        line = next((row for row in lines if row["id"] == self.line_id), None)
        if line is None:
            return None
        self._build(order, lines, line)
        return panel_embed(order, line, lines, await user_total(self.bot, self.line_id, interaction.user.id))

    async def send(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        embed = await self.render(interaction)
        if embed is None:
            await deny(interaction, "That order line no longer exists.")
            return
        await reply(interaction, notice, embed=embed, view=self)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        embed = await self.render(interaction)
        if embed is None:
            await edit(interaction, content="That order line no longer exists.", embed=None, view=None)
            return
        await edit(interaction, content=notice, embed=embed, view=self)

    async def apply(
        self,
        interaction: discord.Interaction,
        order: dict,
        line: dict,
        amount: int,
        *,
        fill: bool = False,
        credit_to: int | None = None,
    ) -> None:
        credit_to = credit_to or interaction.user.id
        updated, applied = await contribute(self.bot, order["id"], line["id"], credit_to, amount, fill=fill)
        if updated is None:
            await edit(interaction, content="That line was removed from the order.", embed=None, view=None)
            return
        name = md(updated["item_name"])
        if applied > 0:
            notice = f"Added **{applied:,}** to **{name}**."
        elif applied < 0:
            notice = f"Removed **{-applied:,}** from **{name}**."
        elif fill:
            notice = f"**{name}** is already complete."
        else:
            notice = f"Nothing changed: **{name}** is already at 0."
        if applied and credit_to != interaction.user.id:
            notice += f" Recorded for <@{credit_to}>."
        await self.show(interaction, notice=notice)
        if applied:
            await after_change(self.bot, self.guild_id, self.order_id)

    async def _quick(self, interaction: discord.Interaction, amount: int) -> None:
        loaded = await self.load_checked(interaction)
        if loaded is None:
            return
        await self.apply(interaction, *loaded, amount)

    async def _max(self, interaction: discord.Interaction) -> None:
        loaded = await self.load_checked(interaction)
        if loaded is None:
            return
        await self.apply(interaction, *loaded, 0, fill=True)

    async def _custom(self, interaction: discord.Interaction) -> None:
        loaded = await self.load_checked(interaction)
        if loaded is None:
            return
        member = await interaction_member(interaction, self.guild_id)
        manager = member is not None and await can_manage(self.bot, member, loaded[0])
        await interaction.response.send_modal(CustomAmountModal(self, manager=manager))

    async def _switch(self, interaction: discord.Interaction) -> None:
        select = next(item for item in self.children if isinstance(item, discord.ui.Select))
        value = select.values[0] if select.values else ""
        if not value.isdigit():
            await deny(interaction, "Pick a line from the list.")
            return
        previous = self.line_id
        self.line_id = int(value)
        if await self.load_checked(interaction) is None:
            self.line_id = previous
            return
        await self.show(interaction)


async def start_contribution(bot: FoxBot, interaction: discord.Interaction, guild_id: int, order_id: str) -> None:
    order = await fetch(bot, guild_id, order_id)
    if order is None:
        await deny(interaction, "This order no longer exists.")
        return
    if not await check_group(interaction, GROUP, guild_id):
        return
    if order["status"] != STATUS_OPEN:
        await deny(interaction, "This order is closed, so it no longer takes contributions.")
        return
    lines = await fetch_lines(bot, order_id)
    if not lines:
        await deny(interaction, "This order has no lines yet.")
        return
    open_lines = [line for line in lines if not is_line_complete(line)]
    single = lines[0] if len(lines) == 1 else open_lines[0] if len(open_lines) == 1 else None
    if single is not None:
        await LinePanel(bot, guild_id, order_id, single["id"], interaction.user.id).send(interaction)
        return

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        if not value.isdigit():
            await deny(pick_interaction, "Pick a line from the list.")
            return
        panel = LinePanel(bot, guild_id, order_id, int(value), pick_interaction.user.id)
        if await panel.load_checked(pick_interaction) is None:
            return
        await panel.show(pick_interaction)

    ordered = sorted(lines, key=lambda line: (is_line_complete(line), line["position"]))
    options = [PickOption(str(line["id"]), line["item_name"], line_progress_text(line)) for line in ordered]
    note = "Pick the line you are contributing to." if open_lines else "Every line is complete. You can still add extra to any line."
    view = PickerView(options, picked, owner_id=interaction.user.id, placeholder="Pick a line...")
    await reply(interaction, f"**{md(order['title'])}**\n{note}", view=view)


async def choose_line(bot: FoxBot, interaction: discord.Interaction, guild_id: int, order_id: str, value: str) -> None:
    order = await fetch(bot, guild_id, order_id)
    if order is None:
        await deny(interaction, "This order no longer exists.")
        return
    if not await check_group(interaction, GROUP, guild_id):
        return
    if order["status"] != STATUS_OPEN:
        await deny(interaction, "This order is closed, so it no longer takes contributions.")
        return
    line = await fetch_line(bot, order_id, int(value)) if value.isdigit() else None
    if line is None:
        await deny(interaction, "That line is no longer part of the order. Pick another one.")
        return
    embed, view = await card_payload(bot, order)
    await interaction.response.edit_message(embed=embed, view=view)
    await LinePanel(bot, guild_id, order_id, line["id"], interaction.user.id).send(interaction)


async def start_edit(bot: FoxBot, interaction: discord.Interaction, guild_id: int, order_id: str) -> None:
    order = await load_managed(bot, interaction, guild_id, order_id)
    if order is None:
        return
    if order["status"] != STATUS_OPEN:
        await deny(interaction, "This order is closed. Reopen it before editing it.")
        return
    lines = await fetch_lines(bot, order_id)
    modal = OrderModal(
        bot,
        guild_id,
        order_type=order["order_type"],
        entry=order,
        title_text=order["title"],
        lines_text=lines_to_text(lines),
        notes_text=order["notes"] or None,
        destination_text=destination_of(order) or None,
    )
    await interaction.response.send_modal(modal)


async def change_status(bot: FoxBot, interaction: discord.Interaction, guild_id: int, order_id: str, *, close: bool) -> None:
    order = await load_managed(bot, interaction, guild_id, order_id)
    if order is None:
        return
    wanted = STATUS_CLOSED if close else STATUS_OPEN
    if order["status"] == wanted:
        await deny(interaction, "This order is already closed." if close else "This order is already open.")
        await refresh_card(bot, guild_id, order_id)
        return
    await set_status(bot, order, wanted, interaction.user.id)
    if close:
        notice = f"Closed **{md(order['title'])}**. It no longer takes contributions; press **Reopen** on the card to undo."
    else:
        notice = f"Reopened **{md(order['title'])}**. It takes contributions again."
    await reply(interaction, notice)
    await after_change(bot, guild_id, order_id)


async def start_delete(bot: FoxBot, interaction: discord.Interaction, guild_id: int, order_id: str) -> None:
    order = await load_managed(bot, interaction, guild_id, order_id)
    if order is None:
        return

    async def confirmed(confirm_interaction: discord.Interaction) -> None:
        current = await load_managed(bot, confirm_interaction, guild_id, order_id)
        if current is None:
            return
        await delete(bot, current)
        await edit(confirm_interaction, content=f"Deleted the order **{md(current['title'])}** and its card.", embed=None, view=None)

    await reply(
        interaction,
        f"Delete the order **{md(order['title'])}**, its card and every recorded contribution? This cannot be undone.",
        view=ConfirmView(owner_id=interaction.user.id, on_confirm=confirmed, confirm_label="Delete"),
    )


CARD_ACTIONS = {
    "contribute": start_contribution,
    "edit": start_edit,
    "delete": start_delete,
}


class OrderButtonItem(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fo:(?P<action>contribute|edit|close|reopen|delete):(?P<order>[A-Z0-9]+)",
):
    def __init__(self, action: str, order_id: str, *, row: int | None = None):
        label, style = CARD_BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"fo:{action}:{order_id}", row=row))
        self.action = action
        self.order_id = order_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], match["order"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await deny(interaction, "This only works inside the server.")
            return
        bot = interaction.client
        try:
            if self.action in ("close", "reopen"):
                await change_status(bot, interaction, interaction.guild_id, self.order_id, close=self.action == "close")
            else:
                await CARD_ACTIONS[self.action](bot, interaction, interaction.guild_id, self.order_id)
        except Exception as error:
            await report_error(interaction, error, f"order card {self.action}")


class OrderLineSelectItem(discord.ui.DynamicItem[discord.ui.Select], template=r"fo:line:(?P<order>[A-Z0-9]+)"):
    def __init__(self, order_id: str, options: list[discord.SelectOption] | None = None):
        super().__init__(
            discord.ui.Select(
                custom_id=f"fo:line:{order_id}",
                placeholder="Pick a line to contribute to...",
                options=options or [discord.SelectOption(label="-")],
                row=0,
            )
        )
        self.order_id = order_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match):
        return cls(match["order"])

    async def callback(self, interaction: discord.Interaction) -> None:
        values = self.item.values
        if interaction.guild_id is None or not values:
            await deny(interaction, "Pick a line from the list.")
            return
        try:
            await choose_line(interaction.client, interaction, interaction.guild_id, self.order_id, str(values[0]))
        except Exception as error:
            await report_error(interaction, error, "order card line select")


class DestinationPicker(LocationPicker):
    def __init__(self, bot: FoxBot, owner_id: int, on_picked, on_skip, *, prompt: str, recent: list[str]):
        self.on_skip = on_skip
        super().__init__(bot, owner_id, on_picked, prompt=prompt, recent=recent)

    def _build(self) -> None:
        super()._build()
        if self.stage == "hex":
            self._add_button("No Destination", discord.ButtonStyle.primary, self._skip, row=4)

    async def _skip(self, interaction: discord.Interaction) -> None:
        await self.on_skip(interaction)


class NewOrderView(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.type_select = discord.ui.Select(
            placeholder="Pick the order type...",
            options=[option(name, name, ORDER_TYPE_HINTS[name]) for name in ORDER_TYPES],
        )
        self.type_select.callback = self._picked
        self.add_item(self.type_select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.guild_id):
            return
        order_type = self.type_select.values[0] if self.type_select.values else ""
        if order_type not in ORDER_TYPES:
            await deny(interaction, "Pick one of the order types.")
            return
        bot = self.bot
        guild_id = self.guild_id

        async def chosen(pick_interaction: discord.Interaction, location: Location | None) -> None:
            if not await check_group(pick_interaction, GROUP, guild_id):
                return
            await pick_interaction.response.send_modal(
                OrderModal(bot, guild_id, order_type=order_type, location=location, replace_origin=True)
            )

        async def picked(pick_interaction: discord.Interaction, location: Location) -> None:
            await chosen(pick_interaction, location)

        async def skipped(pick_interaction: discord.Interaction) -> None:
            await chosen(pick_interaction, None)

        picker = DestinationPicker(
            bot,
            interaction.user.id,
            picked,
            skipped,
            prompt=f"**New {order_type} order**\nThe destination is optional. Press **No Destination** to skip it.",
            recent=await recent_hexes(bot, guild_id),
        )
        await edit(interaction, content=picker.content(), view=picker)


async def start_new(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    await reply(interaction, "**New order**\nWhat kind of order is it?", view=NewOrderView(bot, interaction.guild_id, interaction.user.id))


async def repost_card(bot: FoxBot, interaction: discord.Interaction, guild_id: int, order_id: str) -> None:
    order = await fetch(bot, guild_id, order_id)
    if order is None:
        await deny(interaction, "This order no longer exists.")
        return
    if not await check_group(interaction, GROUP, guild_id):
        return
    if order["message_id"]:
        await refresh_card(bot, guild_id, order_id)
        order = await fetch(bot, guild_id, order_id)
        if order is None:
            await deny(interaction, "This order no longer exists.")
            return
    member = await interaction_member(interaction, guild_id)
    if order["message_id"] and (member is None or not await can_manage(bot, member, order)):
        await deny(interaction, f"The card still exists: [open it]({jump_link(order)}). Only the creator or an admin can move it.")
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    old_channel, old_message = order["channel_id"], order["message_id"]
    message = await post_card(bot, order, interaction.channel)
    if message is None:
        await reply(interaction, "I could not post the card here or in the orders channel. Try a channel where I can send messages.")
        return
    if old_message and old_message != message.id:
        await delete_message(bot, old_channel, old_message)
    order = await fetch(bot, guild_id, order_id) or order
    await reply(interaction, f"Card posted. [Open the card]({jump_link(order)})")


class OrderSummaryView(BaseView):
    def __init__(self, bot: FoxBot, order: dict, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = order["guild_id"]
        self.order_id = order["id"]
        link = jump_link(order)
        if link:
            self.add_item(discord.ui.Button(label="Open Card", url=link))
        if order["status"] == STATUS_OPEN:
            contribute_button = discord.ui.Button(label="Contribute", style=discord.ButtonStyle.success)
            contribute_button.callback = self._contribute
            self.add_item(contribute_button)
        repost = discord.ui.Button(label="Repost Card", style=discord.ButtonStyle.secondary)
        repost.callback = self._repost
        self.add_item(repost)

    async def _contribute(self, interaction: discord.Interaction) -> None:
        await start_contribution(self.bot, interaction, self.guild_id, self.order_id)

    async def _repost(self, interaction: discord.Interaction) -> None:
        await repost_card(self.bot, interaction, self.guild_id, self.order_id)


async def show_summary(bot: FoxBot, interaction: discord.Interaction, order_id: str) -> None:
    order = await fetch(bot, interaction.guild_id, order_id)
    if order is None:
        await deny(interaction, "That order no longer exists.")
        return
    if not await check_group(interaction, GROUP):
        return
    embed, _ = await card_payload(bot, order)
    notice = None if order["message_id"] else "This order's card is missing. Press **Repost Card** to post it again."
    await reply(interaction, notice, embed=embed, view=OrderSummaryView(bot, order, interaction.user.id))


def summary_state(order: dict, progress: dict | None) -> str:
    if order["status"] == STATUS_CLOSED:
        return "Closed"
    if progress and progress["lines"] and progress["complete"] == progress["lines"]:
        return "Completed"
    return "Open"


def progress_numbers(progress: dict | None) -> tuple[int, int, int, int]:
    if not progress:
        return 0, 0, 0, 0
    return progress["done"] or 0, progress["target"] or 0, progress["lines"] or 0, progress["complete"] or 0


def order_title_link(order: dict) -> str:
    name = f"**{link_text(order['title'])}**"
    link = jump_link(order)
    return f"[{name}]({link})" if link else f"{name} (card missing)"


def board_line(order: dict, progress: dict | None) -> str:
    done, target, count, complete = progress_numbers(progress)
    parts = [f"`{progress_bar(done, target)}` {percent(done, target)}% {order_title_link(order)}", f"{complete}/{count} lines"]
    destination = destination_of(order)
    if destination:
        parts.append(md(destination))
    if count and complete == count:
        parts.append("Completed")
    return " - ".join(parts)


def list_line(order: dict, progress: dict | None) -> str:
    done, target, _, _ = progress_numbers(progress)
    return f"[{order['order_type']}] {order_title_link(order)} - {percent(done, target)}% - {summary_state(order, progress)}"


def option_description(order: dict, progress: dict | None) -> str:
    done, target, _, _ = progress_numbers(progress)
    destination = destination_of(order) or "No destination"
    return f"{order['order_type']} | {percent(done, target)}% | {summary_state(order, progress)} | {destination}"


def board_sort_key(order: dict) -> tuple:
    types = list(ORDER_TYPES)
    position = types.index(order["order_type"]) if order["order_type"] in types else len(types)
    return position, order["created_at"]


async def show_list(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    since = timeutil.now() - RECENT_CLOSED_DAYS * 86400
    rows = await list_recent(bot, interaction.guild_id, since)
    if not rows:
        await reply(interaction, f"There are no open orders and none were closed in the last {RECENT_CLOSED_DAYS} days.")
        return
    progress = await progress_by_order(bot, interaction.guild_id)
    open_rows = [row for row in rows if row["status"] == STATUS_OPEN]
    closed_rows = [row for row in rows if row["status"] != STATUS_OPEN]
    lines: list[str] = []
    if open_rows:
        lines.append("**Open**")
        lines.extend(list_line(row, progress.get(row["id"])) for row in sorted(open_rows, key=board_sort_key))
    if closed_rows:
        lines.append(f"**Closed in the last {RECENT_CLOSED_DAYS} days**")
        lines.extend(list_line(row, progress.get(row["id"])) for row in closed_rows)
    pages = chunk_lines(lines, 3800)
    embed = discord.Embed(title="Orders", description=pages[0], colour=Colour.INFO)
    footer = f"{len(open_rows)} open, {len(closed_rows)} recently closed | Pick an order below for details"
    if len(pages) > 1:
        footer = f"Not every order fits here. {footer}"
    embed.set_footer(text=footer)
    ordered = sorted(open_rows, key=board_sort_key) + closed_rows
    options = [PickOption(row["id"], row["title"], option_description(row, progress.get(row["id"]))) for row in ordered]

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await show_summary(bot, pick_interaction, value)

    view = PickerView(options, picked, owner_id=interaction.user.id, placeholder="Pick an order for details or to repost its card...")
    await reply(interaction, embed=embed, view=view)


class OrderBoard:
    kind = KIND
    title = "Orders"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        rows = sorted(await list_orders(self.bot, guild.id, status=STATUS_OPEN), key=board_sort_key)
        progress = await progress_by_order(self.bot, guild.id)
        lines: list[str] = []
        for order_type in ORDER_TYPES:
            group = [row for row in rows if row["order_type"] == order_type]
            if not group:
                continue
            lines.append(f"**{order_type}** ({len(group)})")
            lines.extend(board_line(row, progress.get(row["id"])) for row in group)
        others = [row for row in rows if row["order_type"] not in ORDER_TYPES]
        if others:
            lines.append(f"**Other** ({len(others)})")
            lines.extend(board_line(row, progress.get(row["id"])) for row in others)
        embeds = fit_board_embeds(
            "Open Orders",
            lines,
            colour=Colour.INFO,
            empty="No open orders. Press **New Order** to create one.",
            footer=f"{len(rows)} open order(s) | Updates automatically",
            overflow_hint="Too many to show here. Use All Orders or /order list.",
        )
        options = [option(row["title"], row["id"], option_description(row, progress.get(row["id"]))) for row in rows[:25]]
        return BoardRender(
            embeds=embeds,
            options=options,
            placeholder="View an order...",
            buttons=[
                BoardButton("new", "New Order", discord.ButtonStyle.success),
                BoardButton("list", "All Orders"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action == "new":
            await start_new(self.bot, interaction)
        elif action == "list":
            await show_list(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        if not await check_group(interaction, GROUP):
            return
        await show_summary(self.bot, interaction, value)


class Orders(commands.Cog):
    group = app_commands.Group(name="order", description="Production, transport and scrap orders with progress tracking.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot

    @group.command(name="create", description="Create an order. A form for its lines opens next.")
    @app_commands.describe(
        type="Production, Transport or Scrap",
        title="Short name shown on the card and the board",
        hex="Destination hex (optional, start typing to search)",
        town="Destination town in that hex (optional, start typing to search)",
    )
    @app_commands.choices(type=[app_commands.Choice(name=name, value=name) for name in ORDER_TYPES])
    async def create_command(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        title: app_commands.Range[str, 1, 100],
        hex: app_commands.Range[str, 1, 60] | None = None,
        town: app_commands.Range[str, 1, 100] | None = None,
    ) -> None:
        if not await check_group(interaction, GROUP):
            return
        if type.value not in ORDER_TYPES:
            await deny(interaction, f"Unknown order type. Choose one of: {', '.join(ORDER_TYPES)}.")
            return
        location = self.bot.locations.resolve(hex, town) if (hex or town) else None
        if location is not None and not (location.hex or location.region):
            location = None
        modal = OrderModal(self.bot, interaction.guild_id, order_type=type.value, location=location, title_text=title.strip())
        await interaction.response.send_modal(modal)

    @create_command.autocomplete("hex")
    async def create_hex_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.hex_choices(current)

    @create_command.autocomplete("town")
    async def create_town_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.region_choices(getattr(interaction.namespace, "hex", None), current)

    @group.command(name="list", description="List open orders and those closed in the last 7 days (only you see it).")
    async def list_command(self, interaction: discord.Interaction) -> None:
        await show_list(self.bot, interaction)

    @group.command(name="board", description="Admin: post the live orders board in this channel (moves it if it exists).")
    async def board(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.boards.post(interaction.guild, KIND, interaction.channel)
        except discord.HTTPException as error:
            await reply(interaction, f"Could not post the board here: {error.text or error}")
            return
        orders_channel = await self.bot.settings.get(interaction.guild_id, keys.ORDERS_CHANNEL)
        hint = "" if orders_channel else " Order cards are posted where each order is created until an orders channel is set in `/settings`."
        await reply(interaction, f"Orders board posted.{hint}")


async def setup(bot: FoxBot) -> None:
    bot.boards.register(OrderBoard(bot))
    bot.add_dynamic_items(OrderButtonItem, OrderLineSelectItem)
    await bot.add_cog(Orders(bot))
