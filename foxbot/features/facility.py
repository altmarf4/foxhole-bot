from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import Colour
from foxbot.core import ids, timeutil
from foxbot.core import settings as keys
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.catalog import Catalog, Item, search_key
from foxbot.core.permissions import GROUPS, check_admin, check_group, interaction_member
from foxbot.core.text import EMBED_FIELD_LIMIT, chunk_lines, clip, fit_board_embeds, md, paginate_embeds, plural
from foxbot.core.ui import (
    BaseModal,
    BaseView,
    ConfirmView,
    EmbedPaginator,
    PickerView,
    PickOption,
    deny,
    edit,
    option,
    reply,
    text_field,
    text_value,
)

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

KIND = "facility"
GROUP = "facility"
BOARD_TITLE = "Facility Queue"

STATUS_QUEUED = "queued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_IN_PROGRESS)
STATUS_COLOURS = {
    STATUS_QUEUED: Colour.INFO,
    STATUS_IN_PROGRESS: Colour.ORANGE,
    STATUS_DONE: Colour.SUCCESS,
}
FACTIONS = ("warden", "colonial")

MOVE_UP = "up"
MOVE_DOWN = "down"
MOVE_TOP = "top"

ITEM_MAX = 80
QUANTITY_MAX = 100_000
QUANTITY_TEXT_MAX = 9
FACILITY_MAX = 100
NOTES_MAX = 500
DONE_WINDOW_SECONDS = 86400
DONE_SUMMARY_COUNT = 5
BOARD_FACILITY_LIMIT = 60
BOARD_NOTE_LIMIT = 60
LINE_LIMIT = 600
LIST_PAGE_CHARS = 3800
MESSAGE_LIMIT = 2000
RECENT_FACILITIES = 50
RECENT_FACILITY_CHOICES = 15
WHOLE_NUMBER = re.compile(r"\d+")

ACTIVE_SQL = "SELECT * FROM facility_queue WHERE guild_id = ? AND status IN (?, ?) ORDER BY position, created_at, id"

NO_ACCESS = f"You need the **{GROUPS[GROUP]}** permission for that. Ask an admin to grant it with `/permissions`."
GONE = "This entry is no longer in the facility queue."
MANAGE_ONLY = "Only the requester, whoever worked on this entry or an admin can change or remove it."
FINISH_ONLY = "Only the person working on this entry, the requester or an admin can mark it done."
STOP_ONLY = "Only the person working on this entry or an admin can stop the work on it."
NOTHING_QUEUED = "Nothing is queued right now."
CHANGED_MEANWHILE = "The entry changed in the meantime, so nothing was saved. Check it and try again."
PANEL_OUTDATED = "This entry changed since this panel was shown, so nothing was done."
REMOVE_OUTDATED = "This entry changed since you were asked to confirm, so it was not removed."


@dataclass
class Outcome:
    notice: str
    denied: bool = False


Attempt = Callable[["FoxBot", discord.Member, dict], Awaitable[Outcome]]


async def guild_faction(bot: FoxBot, guild_id: int) -> str | None:
    value = await bot.settings.get(guild_id, keys.FACTION)
    value = str(value).strip().casefold() if value else None
    return value if value in FACTIONS else None


def one_line(text: str | None) -> str:
    return " ".join(str(text or "").split())


def starts_with_words(key: str, name: str) -> bool:
    run = ""
    for word in re.findall(r"[a-z0-9]+", name.casefold()):
        run += word
        if run == key:
            return True
        if len(run) >= len(key):
            return False
    return False


def match_item(catalog: Catalog, typed: str, faction: str | None) -> tuple[Item | None, bool]:
    text = one_line(typed)
    if not text:
        return None, False
    item = catalog.get(text)
    if item is not None:
        return item, True
    item, _ = catalog.resolve_line(text, faction)
    if item is not None:
        return item, True
    folded = text.casefold()
    for code, candidate in catalog.items.items():
        if code.casefold() == folded:
            return candidate, True
    key = search_key(text)
    if len(key) < 3:
        return None, False
    variants = [key]
    if key.endswith("s") and len(key) > 3:
        variants.append(key[:-1])
    for variant in variants:
        for hit in catalog.search(variant, faction, limit=10):
            hit_key = search_key(hit.name)
            if starts_with_words(variant, hit.name) or (variant in hit_key and len(variant) * 2 >= len(hit_key)):
                return hit, False
    return None, False


async def resolve_item(bot: FoxBot, guild_id: int, typed: str) -> tuple[str, str | None, str | None]:
    text = one_line(typed)
    item, exact = match_item(bot.catalog, text, await guild_faction(bot, guild_id))
    if item is None:
        name = clip(text, ITEM_MAX)
        return name, None, f"**{md(name)}** is not in the item catalog, so it was saved as typed."
    if exact:
        return item.name, item.code, None
    return item.name, item.code, f"Matched **{md(clip(text, ITEM_MAX))}** to the catalog item **{md(item.name)}**."


def parse_quantity(raw: str) -> int | None:
    cleaned = (raw or "").replace(",", "").replace(" ", "").replace("_", "")
    if not WHOLE_NUMBER.fullmatch(cleaned):
        return None
    value = int(cleaned)
    if not 1 <= value <= QUANTITY_MAX:
        return None
    return value


async def fetch(bot: FoxBot, guild_id: int, entry_id: str) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM facility_queue WHERE guild_id = ? AND id = ?", (guild_id, entry_id))


async def active_rows(bot: FoxBot, guild_id: int) -> list[dict]:
    return await bot.db.fetchall(ACTIVE_SQL, (guild_id, *ACTIVE_STATUSES))


async def done_rows(bot: FoxBot, guild_id: int, since: int) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT * FROM facility_queue WHERE guild_id = ? AND status = ? AND done_at >= ? ORDER BY done_at DESC, id",
        (guild_id, STATUS_DONE, since),
    )


async def done_today(bot: FoxBot, guild_id: int) -> list[dict]:
    return await done_rows(bot, guild_id, timeutil.now() - DONE_WINDOW_SECONDS)


async def count_done(bot: FoxBot, guild_id: int, cutoff: int) -> int:
    return int(
        await bot.db.fetchval(
            "SELECT COUNT(*) FROM facility_queue WHERE guild_id = ? AND status = ? AND done_at <= ?",
            (guild_id, STATUS_DONE, cutoff),
            0,
        )
    )


async def recent_facilities(bot: FoxBot, guild_id: int) -> list[str]:
    rows = await bot.db.fetchall(
        "SELECT facility, MAX(updated_at) AS used FROM facility_queue WHERE guild_id = ? AND facility != '' "
        "GROUP BY facility ORDER BY used DESC, facility LIMIT ?",
        (guild_id, RECENT_FACILITIES),
    )
    return [row["facility"] for row in rows]


async def renumber(tx, guild_id: int) -> list[dict]:
    rows = await tx.fetchall(ACTIVE_SQL, (guild_id, *ACTIVE_STATUSES))
    changes = [(index, row["id"]) for index, row in enumerate(rows, start=1) if row["position"] != index]
    if changes:
        await tx.executemany("UPDATE facility_queue SET position = ? WHERE id = ?", changes)
    for index, row in enumerate(rows, start=1):
        row["position"] = index
    return rows


async def create(
    bot: FoxBot,
    guild_id: int,
    *,
    item: str,
    item_code: str | None,
    quantity: int,
    facility: str,
    notes: str,
    user_id: int,
) -> dict:
    entry_id = await ids.unique_id(bot.db, "facility_queue")
    now = timeutil.now()
    async with bot.db.transaction() as tx:
        last = await tx.fetchval(
            "SELECT MAX(position) FROM facility_queue WHERE guild_id = ? AND status IN (?, ?)",
            (guild_id, *ACTIVE_STATUSES),
            0,
        )
        await tx.execute(
            "INSERT INTO facility_queue (id, guild_id, item, item_code, quantity, facility, notes, position, status, "
            "requested_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, guild_id, item, item_code, quantity, facility, notes, int(last) + 1, STATUS_QUEUED, user_id, now, now),
        )
        await renumber(tx, guild_id)
    bot.boards.request_refresh(guild_id, KIND)
    return await fetch(bot, guild_id, entry_id)


async def guarded_update(bot: FoxBot, entry: dict, **fields) -> dict | None:
    fields["updated_at"] = timeutil.now()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    async with bot.db.transaction() as tx:
        cursor = await tx.execute(
            f"UPDATE facility_queue SET {assignments} WHERE guild_id = ? AND id = ? AND status = ? AND worker_id IS ?",
            (*fields.values(), entry["guild_id"], entry["id"], entry["status"], entry["worker_id"]),
        )
        changed = cursor.rowcount
        await cursor.close()
        if changed:
            await renumber(tx, entry["guild_id"])
    if not changed:
        return None
    bot.boards.request_refresh(entry["guild_id"], KIND)
    return await fetch(bot, entry["guild_id"], entry["id"])


async def start_work(bot: FoxBot, entry: dict, user_id: int) -> dict | None:
    return await guarded_update(bot, entry, status=STATUS_IN_PROGRESS, worker_id=user_id, started_at=timeutil.now())


async def stop_work(bot: FoxBot, entry: dict) -> dict | None:
    return await guarded_update(bot, entry, status=STATUS_QUEUED, worker_id=None, started_at=None)


async def mark_done(bot: FoxBot, entry: dict, user_id: int) -> dict | None:
    return await guarded_update(bot, entry, status=STATUS_DONE, done_by=user_id, done_at=timeutil.now(), position=0)


async def requeue(bot: FoxBot, entry: dict) -> dict | None:
    return await guarded_update(
        bot,
        entry,
        status=STATUS_QUEUED,
        worker_id=None,
        started_at=None,
        done_by=None,
        done_at=None,
        position=0,
    )


async def remove(bot: FoxBot, entry: dict) -> bool:
    async with bot.db.transaction() as tx:
        cursor = await tx.execute(
            "DELETE FROM facility_queue WHERE guild_id = ? AND id = ? AND status = ? AND worker_id IS ?",
            (entry["guild_id"], entry["id"], entry["status"], entry["worker_id"]),
        )
        changed = cursor.rowcount
        await cursor.close()
        if changed:
            await renumber(tx, entry["guild_id"])
    if changed:
        bot.boards.request_refresh(entry["guild_id"], KIND)
    return bool(changed)


def reorder(waiting: list[dict], index: int, direction: str) -> list[dict]:
    result = list(waiting)
    if direction == MOVE_UP and index > 0:
        result[index - 1], result[index] = result[index], result[index - 1]
    elif direction == MOVE_DOWN and index < len(result) - 1:
        result[index + 1], result[index] = result[index], result[index + 1]
    elif direction == MOVE_TOP and index > 0:
        result.insert(0, result.pop(index))
    return result


async def move(bot: FoxBot, guild_id: int, entry_id: str, direction: str) -> str:
    async with bot.db.transaction() as tx:
        rows = await renumber(tx, guild_id)
        waiting = [row for row in rows if row["status"] == STATUS_QUEUED]
        index = next((i for i, row in enumerate(waiting) if row["id"] == entry_id), None)
        if index is None:
            return "not_queued"
        reordered = reorder(waiting, index, direction)
        if [row["id"] for row in reordered] == [row["id"] for row in waiting]:
            return "unchanged"
        replacements = iter(reordered)
        ordered = [next(replacements) if row["status"] == STATUS_QUEUED else row for row in rows]
        changes = [(position, row["id"]) for position, row in enumerate(ordered, start=1) if row["position"] != position]
        await tx.executemany("UPDATE facility_queue SET position = ? WHERE id = ?", changes)
        await tx.execute("UPDATE facility_queue SET updated_at = ? WHERE id = ?", (timeutil.now(), entry_id))
    bot.boards.request_refresh(guild_id, KIND)
    return "moved"


async def clear_done(bot: FoxBot, guild_id: int, cutoff: int) -> int:
    async with bot.db.transaction() as tx:
        cursor = await tx.execute(
            "DELETE FROM facility_queue WHERE guild_id = ? AND status = ? AND done_at <= ?",
            (guild_id, STATUS_DONE, cutoff),
        )
        removed = cursor.rowcount
        await cursor.close()
    if removed:
        bot.boards.request_refresh(guild_id, KIND)
    return removed


def display_order(rows: list[dict]) -> list[dict]:
    working = [row for row in rows if row["status"] == STATUS_IN_PROGRESS]
    waiting = [row for row in rows if row["status"] == STATUS_QUEUED]
    return working + waiting


async def number_of(bot: FoxBot, guild_id: int, entry_id: str) -> int | None:
    ordered = display_order(await active_rows(bot, guild_id))
    return next((index for index, row in enumerate(ordered, start=1) if row["id"] == entry_id), None)


async def can_manage(bot: FoxBot, member: discord.Member, entry: dict) -> bool:
    if member.id in (entry["requested_by"], entry["worker_id"], entry["done_by"]):
        return True
    return await bot.perms.is_admin(member)


async def can_stop(bot: FoxBot, member: discord.Member, entry: dict) -> bool:
    return member.id == entry["worker_id"] or await bot.perms.is_admin(member)


async def can_finish(bot: FoxBot, member: discord.Member, entry: dict) -> bool:
    if entry["status"] == STATUS_QUEUED:
        return True
    if member.id in (entry["worker_id"], entry["requested_by"]):
        return True
    return await bot.perms.is_admin(member)


def item_label(entry: dict) -> str:
    return f"{entry['item']} x {entry['quantity']:,}"


def bold_item(entry: dict) -> str:
    return f"**{md(entry['item'])}** x {entry['quantity']:,}"


def member_name(guild: discord.Guild | None, user_id: int | None) -> str:
    member = guild.get_member(user_id) if guild is not None and user_id else None
    return member.display_name if member is not None else "an unknown member"


def state_of(entry: dict) -> tuple[str, int | None]:
    return entry["status"], entry["worker_id"]


def state_notice(entry: dict | None, user_id: int) -> str:
    if entry is None:
        return GONE
    if entry["status"] == STATUS_QUEUED:
        return "This entry is waiting in the queue."
    if entry["status"] == STATUS_IN_PROGRESS:
        if entry["worker_id"] == user_id:
            return "You are working on this entry."
        return f"<@{entry['worker_id']}> started working on this entry {timeutil.relative(entry['started_at'])}."
    return f"This entry was marked done by <@{entry['done_by']}> {timeutil.relative(entry['done_at'])}."


def board_line(number: int, entry: dict) -> str:
    head = f"`#{number}` {bold_item(entry)}"
    if entry["facility"]:
        head += f" at {md(clip(entry['facility'], BOARD_FACILITY_LIMIT))}"
    parts = [head]
    if entry["status"] == STATUS_IN_PROGRESS:
        parts.append(f"<@{entry['worker_id']}> since {timeutil.relative(entry['started_at'])}")
    else:
        parts.append(f"requested by <@{entry['requested_by']}>")
    if entry["notes"]:
        parts.append(f"*{md(clip(one_line(entry['notes']), BOARD_NOTE_LIMIT))}*")
    return clip(" - ".join(parts), LINE_LIMIT)


def done_summary(done: list[dict]) -> str:
    shown = [f"{md(item_label(row))} (<@{row['done_by']}>)" for row in done[:DONE_SUMMARY_COUNT]]
    text = ", ".join(shown)
    if len(done) > DONE_SUMMARY_COUNT:
        text += f", and {len(done) - DONE_SUMMARY_COUNT} more"
    return text


def done_line(entry: dict) -> str:
    text = f"{timeutil.relative(entry['done_at'])} {bold_item(entry)}"
    if entry["facility"]:
        text += f" at {md(clip(entry['facility'], BOARD_FACILITY_LIMIT))}"
    return clip(f"{text} - done by <@{entry['done_by']}> - requested by <@{entry['requested_by']}>", LINE_LIMIT)


def queue_lines(ordered: list[dict], done: list[dict]) -> list[str]:
    working = [row for row in ordered if row["status"] == STATUS_IN_PROGRESS]
    waiting = [row for row in ordered if row["status"] == STATUS_QUEUED]
    lines: list[str] = []
    if working:
        lines.append(f"**In progress** ({len(working)})")
        lines.extend(board_line(number, row) for number, row in enumerate(working, start=1))
    if waiting:
        lines.append(f"**Queued** ({len(waiting)})")
        lines.extend(board_line(number, row) for number, row in enumerate(waiting, start=len(working) + 1))
    if done:
        if not ordered:
            lines.append(NOTHING_QUEUED)
        lines.append(f"**Done in the last 24 hours** ({len(done)})")
        lines.append(done_summary(done))
    return lines


def counts_text(ordered: list[dict], done: list[dict]) -> str:
    working = sum(1 for row in ordered if row["status"] == STATUS_IN_PROGRESS)
    return f"{working} in progress, {len(ordered) - working} queued, {len(done)} done in the last 24 hours"


def option_label(number: int, entry: dict) -> str:
    return f"#{number} {item_label(entry)}"


def option_description(entry: dict, guild: discord.Guild | None) -> str:
    if entry["status"] == STATUS_IN_PROGRESS:
        text = f"In progress: {member_name(guild, entry['worker_id'])}"
    elif entry["status"] == STATUS_QUEUED:
        text = f"Queued, requested by {member_name(guild, entry['requested_by'])}"
    else:
        text = f"Done by {member_name(guild, entry['done_by'])}"
    if entry["facility"]:
        text += f" | {one_line(entry['facility'])}"
    return text


def status_text(entry: dict, number: int | None, next_up: bool) -> str:
    if entry["status"] == STATUS_QUEUED:
        text = f"**Queued** at **#{number}**" if number else "**Queued**"
        return text + " - next up" if next_up else text
    if entry["status"] == STATUS_IN_PROGRESS:
        return f"**In progress** - <@{entry['worker_id']}> since {timeutil.relative(entry['started_at'])}"
    return f"**Done** by <@{entry['done_by']}> {timeutil.relative(entry['done_at'])}"


def card_embed(entry: dict, number: int | None, next_up: bool) -> discord.Embed:
    embed = discord.Embed(
        title=clip(item_label(entry), 256),
        description=status_text(entry, number, next_up),
        colour=STATUS_COLOURS.get(entry["status"], Colour.MUTED),
    )
    embed.add_field(name="Quantity", value=f"{entry['quantity']:,}", inline=True)
    embed.add_field(name="Facility", value=clip(md(entry["facility"]), EMBED_FIELD_LIMIT) if entry["facility"] else "Not set", inline=True)
    if entry["notes"]:
        embed.add_field(name="Notes", value=clip(md(entry["notes"]), EMBED_FIELD_LIMIT), inline=False)
    embed.add_field(name="Requested by", value=f"<@{entry['requested_by']}> {timeutil.relative(entry['created_at'])}", inline=False)
    embed.set_footer(text=f"Entry {entry['id']} | Only you see this panel")
    return embed


async def attempt_start(bot: FoxBot, member: discord.Member, entry: dict) -> Outcome:
    if entry["status"] != STATUS_QUEUED:
        return Outcome(state_notice(entry, member.id))
    started = await start_work(bot, entry, member.id)
    if started is None:
        return Outcome(state_notice(await fetch(bot, entry["guild_id"], entry["id"]), member.id))
    return Outcome(
        f"You are working on **{md(item_label(started))}** now. Press **Mark Done** when it is finished, "
        "or **Stop Working** to put it back in the queue.",
    )


async def attempt_stop(bot: FoxBot, member: discord.Member, entry: dict) -> Outcome:
    if entry["status"] != STATUS_IN_PROGRESS:
        return Outcome(state_notice(entry, member.id))
    if not await can_stop(bot, member, entry):
        return Outcome(STOP_ONLY, denied=True)
    stopped = await stop_work(bot, entry)
    if stopped is None:
        return Outcome(state_notice(await fetch(bot, entry["guild_id"], entry["id"]), member.id))
    whose = "your work" if entry["worker_id"] == member.id else f"the work of <@{entry['worker_id']}>"
    return Outcome(f"Stopped {whose} on **{md(item_label(stopped))}**. It is back in the queue at its old place.")


async def attempt_finish(bot: FoxBot, member: discord.Member, entry: dict) -> Outcome:
    if entry["status"] not in ACTIVE_STATUSES:
        return Outcome(state_notice(entry, member.id))
    if not await can_finish(bot, member, entry):
        return Outcome(FINISH_ONLY, denied=True)
    finished = await mark_done(bot, entry, member.id)
    if finished is None:
        return Outcome(state_notice(await fetch(bot, entry["guild_id"], entry["id"]), member.id))
    return Outcome(
        f"Marked **{md(item_label(finished))}** as done. It is listed under Done Today for 24 hours. "
        "Press **Back to Queue** if that was a mistake.",
    )


async def attempt_requeue(bot: FoxBot, member: discord.Member, entry: dict) -> Outcome:
    if entry["status"] != STATUS_DONE:
        return Outcome(state_notice(entry, member.id))
    if not await can_manage(bot, member, entry):
        return Outcome(MANAGE_ONLY, denied=True)
    queued = await requeue(bot, entry)
    if queued is None:
        return Outcome(state_notice(await fetch(bot, entry["guild_id"], entry["id"]), member.id))
    return Outcome(f"**{md(item_label(queued))}** is back in the queue, at the top. Use **Move Down** to change its place.")


async def attempt_move(bot: FoxBot, member: discord.Member, entry: dict, direction: str) -> Outcome:
    if entry["status"] != STATUS_QUEUED:
        return Outcome(f"{state_notice(entry, member.id)} Only queued entries can be moved.")
    result = await move(bot, entry["guild_id"], entry["id"], direction)
    label = md(item_label(entry))
    if result == "not_queued":
        current = await fetch(bot, entry["guild_id"], entry["id"])
        return Outcome(f"{state_notice(current, member.id)} Only queued entries can be moved.")
    if result == "unchanged":
        where = "last" if direction == MOVE_DOWN else "first"
        return Outcome(f"**{label}** is already {where} in the queue.")
    number = await number_of(bot, entry["guild_id"], entry["id"])
    if direction == MOVE_TOP:
        return Outcome(f"Moved **{label}** to the top of the queue (**#{number}**).")
    return Outcome(f"Moved **{label}** {direction} to **#{number}**.")


async def added_notice(bot: FoxBot, entry: dict, report: str | None) -> str:
    number = await number_of(bot, entry["guild_id"], entry["id"])
    parts = [f"Added **{md(item_label(entry))}** to the facility queue at **#{number}**."]
    if report:
        parts.append(report)
    return clip("\n".join(parts), MESSAGE_LIMIT)


class EntryModal(BaseModal):
    def __init__(
        self,
        bot: FoxBot,
        guild_id: int,
        *,
        entry: dict | None = None,
        values: dict | None = None,
        replace_origin: bool = False,
    ):
        super().__init__(title="Edit Facility Queue Entry" if entry is not None else "Add to Facility Queue")
        self.bot = bot
        self.guild_id = guild_id
        self.entry = entry
        self.replace_origin = replace_origin
        if values is None:
            values = {}
            if entry is not None:
                values = {
                    "item": entry["item"],
                    "quantity": str(entry["quantity"]),
                    "facility": entry["facility"],
                    "notes": entry["notes"],
                }
        self.item_field = text_field(
            "Item",
            default=clip(values["item"], ITEM_MAX) if values.get("item") else None,
            placeholder="e.g. Basic Materials, 12.7mm or Mortar Shell",
            description="A name from the item catalog, or any text.",
            max_length=ITEM_MAX,
        )
        self.quantity_field = text_field(
            "Quantity",
            default=values.get("quantity") or None,
            placeholder="e.g. 60",
            description=f"A whole number from 1 to {QUANTITY_MAX:,}.",
            max_length=QUANTITY_TEXT_MAX,
        )
        self.facility_field = text_field(
            "Facility",
            default=clip(values["facility"], FACILITY_MAX) if values.get("facility") else None,
            placeholder="Optional, e.g. Factory - Westgate",
            required=False,
            max_length=FACILITY_MAX,
        )
        self.notes_field = text_field(
            "Notes",
            default=clip(values["notes"], NOTES_MAX) if values.get("notes") else None,
            placeholder="Optional: crates or loose, deadline, who it is for",
            required=False,
            paragraph=True,
            max_length=NOTES_MAX,
        )
        for item in (self.item_field, self.quantity_field, self.facility_field, self.notes_field):
            self.add_item(item)

    def typed_values(self) -> dict:
        return {
            "item": text_value(self.item_field),
            "quantity": text_value(self.quantity_field),
            "facility": text_value(self.facility_field),
            "notes": text_value(self.notes_field),
        }

    def again(self) -> EntryModal:
        return EntryModal(self.bot, self.guild_id, entry=self.entry, values=self.typed_values(), replace_origin=True)

    async def _load_editable(self, interaction: discord.Interaction) -> dict | None:
        current = await fetch(self.bot, self.guild_id, self.entry["id"])
        if current is None:
            await deny(interaction, GONE)
            return None
        member = await interaction_member(interaction, self.guild_id)
        if member is None or not await can_manage(self.bot, member, current):
            await deny(interaction, MANAGE_ONLY)
            return None
        if current["status"] == STATUS_DONE:
            await deny(interaction, "This entry is already done, so it can no longer be edited.")
            return None
        return current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.guild_id):
            return
        current = None
        if self.entry is not None:
            current = await self._load_editable(interaction)
            if current is None:
                return
        typed = one_line(text_value(self.item_field))
        quantity = parse_quantity(text_value(self.quantity_field))
        problems = []
        if not typed:
            problems.append("Name the item to produce.")
        if quantity is None:
            problems.append(f"The quantity must be a whole number from 1 to {QUANTITY_MAX:,}, for example 60.")
        if problems:
            text = "Nothing was saved:\n" + "\n".join(f"- {problem}" for problem in problems)
            text += "\nPress **Fix and Try Again** to correct it."
            await reply(interaction, text, view=RetryView(self, interaction.user.id))
            return
        name, item_code, report = await resolve_item(self.bot, self.guild_id, typed)
        facility = one_line(text_value(self.facility_field))
        notes = text_value(self.notes_field)
        if current is None:
            entry = await create(
                self.bot,
                self.guild_id,
                item=name,
                item_code=item_code,
                quantity=quantity,
                facility=facility,
                notes=notes,
                user_id=interaction.user.id,
            )
            notice = await added_notice(self.bot, entry, report)
            entry_id = entry["id"]
        else:
            entry_id = current["id"]
            saved = await guarded_update(
                self.bot,
                current,
                item=name,
                item_code=item_code,
                quantity=quantity,
                facility=facility,
                notes=notes,
            )
            notice = clip("\n".join(filter(None, ["Saved.", report])), MESSAGE_LIMIT) if saved is not None else CHANGED_MEANWHILE
        panel = FacilityPanel(self.bot, self.guild_id, entry_id, interaction.user.id)
        if self.replace_origin:
            await panel.show(interaction, notice=notice)
        else:
            await panel.send(interaction, notice=notice)


class RetryView(BaseView):
    def __init__(self, modal: EntryModal, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.modal = modal
        button = discord.ui.Button(label="Fix and Try Again", style=discord.ButtonStyle.primary)
        button.callback = self._retry
        self.add_item(button)

    async def _retry(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.modal.guild_id):
            return
        await interaction.response.send_modal(self.modal.again())


class FacilityPanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, entry_id: str, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.entry_id = entry_id
        self.seen: tuple[str, int | None] | None = None

    async def load(self, interaction: discord.Interaction) -> tuple[dict, discord.Member] | None:
        entry = await fetch(self.bot, self.guild_id, self.entry_id)
        if entry is None:
            await edit(interaction, content=GONE, embed=None, view=None)
            return None
        member = await interaction_member(interaction, self.guild_id)
        if member is None:
            await deny(interaction, "This only works inside the server.")
            return None
        if not await self.bot.perms.has(member, GROUP):
            await deny(interaction, NO_ACCESS)
            return None
        return entry, member

    def _button(self, label: str, style: discord.ButtonStyle, callback, *, row: int, disabled: bool = False) -> None:
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    async def _build(self, entry: dict, member: discord.Member | None, waiting_index: int, waiting_count: int) -> None:
        self.clear_items()
        status = entry["status"]
        if status == STATUS_QUEUED:
            self._button("Start Working", discord.ButtonStyle.success, self._start, row=0)
            self._button("Mark Done", discord.ButtonStyle.primary, self._finish, row=0)
            first = waiting_index <= 0
            last = waiting_index >= waiting_count - 1
            self._button("Move Up", discord.ButtonStyle.secondary, self._move_up, row=1, disabled=first)
            self._button("Move Down", discord.ButtonStyle.secondary, self._move_down, row=1, disabled=last)
            self._button("Move to Top", discord.ButtonStyle.secondary, self._move_top, row=1, disabled=first)
        elif status == STATUS_IN_PROGRESS:
            if member is not None and await can_stop(self.bot, member, entry):
                self._button("Stop Working", discord.ButtonStyle.secondary, self._stop, row=0)
            if member is not None and await can_finish(self.bot, member, entry):
                self._button("Mark Done", discord.ButtonStyle.primary, self._finish, row=0)
        manager = member is not None and await can_manage(self.bot, member, entry)
        if status == STATUS_DONE and manager:
            self._button("Back to Queue", discord.ButtonStyle.primary, self._requeue, row=0)
        if manager and status != STATUS_DONE:
            self._button("Edit", discord.ButtonStyle.secondary, self._edit, row=2)
        if manager:
            self._button("Remove", discord.ButtonStyle.danger, self._remove, row=2)
        self._button("Refresh", discord.ButtonStyle.secondary, self._refresh, row=2)

    async def render(self, interaction: discord.Interaction) -> discord.Embed | None:
        entry = await fetch(self.bot, self.guild_id, self.entry_id)
        if entry is None:
            return None
        self.seen = state_of(entry)
        ordered = display_order(await active_rows(self.bot, self.guild_id))
        waiting = [row["id"] for row in ordered if row["status"] == STATUS_QUEUED]
        number = next((index for index, row in enumerate(ordered, start=1) if row["id"] == entry["id"]), None)
        waiting_index = waiting.index(entry["id"]) if entry["id"] in waiting else -1
        await self._build(entry, await interaction_member(interaction, self.guild_id), waiting_index, len(waiting))
        return card_embed(entry, number, waiting_index == 0)

    async def send(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        embed = await self.render(interaction)
        if embed is None:
            await deny(interaction, GONE)
            return
        await reply(interaction, notice, embed=embed, view=self)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        embed = await self.render(interaction)
        if embed is None:
            await edit(interaction, content=GONE, embed=None, view=None)
            return
        await edit(interaction, content=notice, embed=embed, view=self)

    async def _act(self, interaction: discord.Interaction, attempt: Attempt) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        entry, member = loaded
        if self.seen is not None and state_of(entry) != self.seen:
            await self.show(interaction, notice=f"{PANEL_OUTDATED} {state_notice(entry, member.id)}")
            return
        outcome = await attempt(self.bot, member, entry)
        if outcome.denied:
            await deny(interaction, outcome.notice)
            return
        await self.show(interaction, notice=outcome.notice)

    async def _start(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, attempt_start)

    async def _stop(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, attempt_stop)

    async def _finish(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, attempt_finish)

    async def _requeue(self, interaction: discord.Interaction) -> None:
        await self._act(interaction, attempt_requeue)

    async def _move(self, interaction: discord.Interaction, direction: str) -> None:
        async def attempt(bot: FoxBot, member: discord.Member, entry: dict) -> Outcome:
            return await attempt_move(bot, member, entry, direction)

        await self._act(interaction, attempt)

    async def _move_up(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, MOVE_UP)

    async def _move_down(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, MOVE_DOWN)

    async def _move_top(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, MOVE_TOP)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        if await self.load(interaction) is None:
            return
        await self.show(interaction)

    async def _edit(self, interaction: discord.Interaction) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        entry, member = loaded
        if not await can_manage(self.bot, member, entry):
            await deny(interaction, MANAGE_ONLY)
            return
        if entry["status"] == STATUS_DONE:
            await self.show(interaction, notice="This entry is already done, so it can no longer be edited.")
            return
        await interaction.response.send_modal(EntryModal(self.bot, self.guild_id, entry=entry, replace_origin=True))

    async def _remove(self, interaction: discord.Interaction) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        entry, member = loaded
        if not await can_manage(self.bot, member, entry):
            await deny(interaction, MANAGE_ONLY)
            return

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            current = await self.load(confirm_interaction)
            if current is None:
                return
            fresh, actor = current
            if not await can_manage(self.bot, actor, fresh):
                await deny(confirm_interaction, MANAGE_ONLY)
                return
            if state_of(fresh) != state_of(entry) or not await remove(self.bot, fresh):
                latest = await fetch(self.bot, self.guild_id, self.entry_id)
                if latest is None:
                    await edit(confirm_interaction, content=GONE, embed=None, view=None)
                    return
                await self.show(confirm_interaction, notice=f"{REMOVE_OUTDATED} {state_notice(latest, actor.id)}")
                return
            await edit(
                confirm_interaction,
                content=f"Removed **{md(item_label(fresh))}** from the facility queue.",
                embed=None,
                view=None,
            )

        async def cancelled(cancel_interaction: discord.Interaction) -> None:
            await self._refresh(cancel_interaction)

        working = ""
        if entry["status"] == STATUS_IN_PROGRESS and entry["worker_id"] != member.id:
            working = f" <@{entry['worker_id']}> is working on it."
        await edit(
            interaction,
            content=f"Remove **{md(item_label(entry))}** from the facility queue?{working} This cannot be undone.",
            embed=None,
            view=ConfirmView(owner_id=interaction.user.id, on_confirm=confirmed, on_cancel=cancelled, confirm_label="Remove"),
        )


async def open_panel(bot: FoxBot, interaction: discord.Interaction, entry_id: str) -> None:
    if not await check_group(interaction, GROUP):
        return
    if await fetch(bot, interaction.guild_id, entry_id) is None:
        await deny(interaction, GONE)
        return
    await FacilityPanel(bot, interaction.guild_id, entry_id, interaction.user.id).send(interaction)


async def start_add(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    await interaction.response.send_modal(EntryModal(bot, interaction.guild_id))


async def show_done_today(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    rows = await done_today(bot, interaction.guild_id)
    embeds = paginate_embeds(
        "Done in the Last 24 Hours",
        [done_line(row) for row in rows],
        colour=Colour.SUCCESS,
        empty="Nothing was finished in the last 24 hours.",
        per_page_chars=3000,
        footer=f"{plural(len(rows), 'entry', 'entries')} finished, newest first",
    )
    await EmbedPaginator(embeds, owner_id=interaction.user.id).start(interaction)


async def start_clear_done(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_admin(interaction):
        return
    guild_id = interaction.guild_id
    cutoff = timeutil.now()
    count = await count_done(bot, guild_id, cutoff)
    if not count:
        await reply(interaction, "There are no done entries to clear.")
        return

    async def confirmed(confirm_interaction: discord.Interaction) -> None:
        if not await check_admin(confirm_interaction):
            return
        removed = await clear_done(bot, guild_id, cutoff)
        if removed:
            text = f"Cleared {plural(removed, 'done entry', 'done entries')} from the facility queue."
        else:
            text = "There were no done entries left to clear."
        await edit(confirm_interaction, content=text, embed=None, view=None)

    await reply(
        interaction,
        f"Clear {plural(count, 'done entry', 'done entries')} from the facility queue? They disappear from the board "
        "and from Done Today. Queued and in-progress entries are kept. This cannot be undone.",
        view=ConfirmView(owner_id=interaction.user.id, on_confirm=confirmed, confirm_label="Clear Done"),
    )


async def show_list(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    ordered = display_order(await active_rows(bot, interaction.guild_id))
    done = await done_today(bot, interaction.guild_id)
    if not ordered and not done:
        await reply(interaction, "The facility queue is empty. Use `/facility add` or the board's **Add** button to queue something.")
        return
    pages = chunk_lines(queue_lines(ordered, done), LIST_PAGE_CHARS)
    embed = discord.Embed(title=BOARD_TITLE, description=pages[0], colour=Colour.INFO)
    footer = f"{counts_text(ordered, done)} | Pick an entry below to open its panel"
    if len(pages) > 1:
        footer = f"Not every entry fits here. {footer}"
    embed.set_footer(text=footer)
    guild = interaction.guild
    options = [PickOption(row["id"], option_label(number, row), option_description(row, guild)) for number, row in enumerate(ordered, start=1)]
    options += [PickOption(row["id"], f"Done: {item_label(row)}", option_description(row, guild)) for row in done]

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await open_panel(bot, pick_interaction, value)

    view = PickerView(options, picked, owner_id=interaction.user.id, placeholder="Open an entry...")
    await reply(interaction, embed=embed, view=view)


class FacilityBoard:
    kind = KIND
    title = BOARD_TITLE

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        ordered = display_order(await active_rows(self.bot, guild.id))
        done = await done_today(self.bot, guild.id)
        embeds = fit_board_embeds(
            BOARD_TITLE,
            queue_lines(ordered, done),
            colour=Colour.INFO,
            empty=f"{NOTHING_QUEUED} Press **Add** to queue something for the facility.",
            footer=f"{counts_text(ordered, done)} | Updates automatically",
            overflow_hint="Too many to show here. Use /facility list.",
        )
        options = [
            option(option_label(number, row), row["id"], option_description(row, guild))
            for number, row in enumerate(ordered[:25], start=1)
        ]
        return BoardRender(
            embeds=embeds,
            options=options,
            placeholder="Open an entry (in progress first, then the queue)...",
            buttons=[
                BoardButton("add", "Add", discord.ButtonStyle.success),
                BoardButton("done", "Done Today"),
                BoardButton("clear", "Clear Done"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action == "add":
            await start_add(self.bot, interaction)
        elif action == "done":
            await show_done_today(self.bot, interaction)
        elif action == "clear":
            await start_clear_done(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        await open_panel(self.bot, interaction, value)


class Facility(commands.Cog):
    group = app_commands.Group(
        name="facility",
        description="Shared facility queue: what to produce next, who is working on it, what is done.",
        guild_only=True,
    )

    def __init__(self, bot: FoxBot):
        self.bot = bot

    @group.command(name="add", description="Add an item to the end of the shared facility queue.")
    @app_commands.describe(
        item="Item to produce (start typing to search the catalog, or type any text)",
        quantity=f"How many, from 1 to {QUANTITY_MAX:,}",
        facility="Where to produce it, e.g. Factory - Westgate (optional)",
        notes="Anything the producer should know (optional)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        item: app_commands.Range[str, 1, ITEM_MAX],
        quantity: app_commands.Range[int, 1, QUANTITY_MAX],
        facility: app_commands.Range[str, 1, FACILITY_MAX] | None = None,
        notes: app_commands.Range[str, 1, NOTES_MAX] | None = None,
    ) -> None:
        if not await check_group(interaction, GROUP):
            return
        typed = one_line(item)
        if not typed:
            await deny(interaction, "Name the item to produce.")
            return
        if not 1 <= quantity <= QUANTITY_MAX:
            await deny(interaction, f"The quantity must be a whole number from 1 to {QUANTITY_MAX:,}.")
            return
        name, item_code, report = await resolve_item(self.bot, interaction.guild_id, typed)
        entry = await create(
            self.bot,
            interaction.guild_id,
            item=name,
            item_code=item_code,
            quantity=quantity,
            facility=one_line(facility)[:FACILITY_MAX],
            notes=(notes or "").strip()[:NOTES_MAX],
            user_id=interaction.user.id,
        )
        notice = await added_notice(self.bot, entry, report)
        await FacilityPanel(self.bot, interaction.guild_id, entry["id"], interaction.user.id).send(interaction, notice=notice)

    @add.autocomplete("item")
    async def add_item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        faction = await guild_faction(self.bot, interaction.guild_id) if interaction.guild_id else None
        return self.bot.catalog.choices(current, faction)

    @add.autocomplete("facility")
    async def add_facility_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        key = one_line(current).casefold()
        used = await recent_facilities(self.bot, interaction.guild_id) if interaction.guild_id else []
        choices = [
            app_commands.Choice(name=clip(name, 100), value=name[:100])
            for name in used
            if not key or key in name.casefold()
        ][:RECENT_FACILITY_CHOICES]
        seen = {choice.value for choice in choices}
        for choice in self.bot.locations.location_choices(current):
            if len(choices) >= 25:
                break
            if choice.value not in seen:
                seen.add(choice.value)
                choices.append(choice)
        return choices[:25]

    @group.command(name="list", description="Show the facility queue and open an entry's panel (only you see it).")
    async def list_command(self, interaction: discord.Interaction) -> None:
        await show_list(self.bot, interaction)

    @group.command(name="board", description="Admin: post the live facility queue board in this channel (moves it if it exists).")
    async def board(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.boards.post(interaction.guild, KIND, interaction.channel)
        except discord.HTTPException as error:
            await reply(interaction, f"Could not post the board here: {error.text or error}")
            return
        await reply(interaction, "Facility queue board posted.")


async def setup(bot: FoxBot) -> None:
    bot.boards.register(FacilityBoard(bot))
    await bot.add_cog(Facility(bot))
