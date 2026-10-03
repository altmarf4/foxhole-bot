from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import RARES_STATUS, RARES_STOCKED, Colour
from foxbot.core import timeutil
from foxbot.core import settings as keys
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.permissions import check_admin, check_group, interaction_member
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, clip, md, paginate_embeds
from foxbot.core.ui import BaseModal, EmbedPaginator, deny, reply, text_field, text_value

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

KIND = "rares"
GROUP = "rares"
COG_NAME = "Rares"
VOICE_RENAME_INTERVAL = 300.0
BOARD_LOG_LINES = 10
NOTE_MAX = 200
AMOUNT_MAX = 1_000_000_000
INTEGER = re.compile(r"[+-]?[0-9]+")

ACTIONS = ("add", "remove", "set", "reset", "payment")
ACTION_COLOURS = {
    "add": Colour.SUCCESS,
    "remove": Colour.DANGER,
    "set": Colour.WARNING,
    "reset": Colour.MUTED,
    "payment": Colour.INFO,
}
MODAL_TITLES = {
    "add": "Add Rare Alloys",
    "remove": "Remove Rare Alloys",
    "set": "Set Rare Alloys Count",
}


async def get_count(bot: FoxBot, guild_id: int) -> int:
    return int(await bot.db.fetchval("SELECT count FROM rares_stock WHERE guild_id = ?", (guild_id,), 0))


async def log_rows(bot: FoxBot, guild_id: int, limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM rares_log WHERE guild_id = ? ORDER BY id DESC"
    params: tuple = (guild_id,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (guild_id, limit)
    return await bot.db.fetchall(sql, params)


def new_count(action: str, old: int, amount: int) -> int:
    if action == "add":
        return old + amount
    if action == "remove":
        return max(0, old - amount)
    if action == "set":
        return max(0, amount)
    if action == "reset":
        return 0
    if action == "payment":
        return max(0, old + amount)
    raise ValueError(f"Unknown rares action {action!r}")


async def apply_change(
    bot: FoxBot,
    guild_id: int,
    *,
    action: str,
    amount: int,
    user_id: int,
    note: str = "",
    ticket_number: int | None = None,
) -> tuple[int, int]:
    if action not in ACTIONS:
        raise ValueError(f"Unknown rares action {action!r}")
    amount = int(amount)
    if action in ("add", "remove", "set") and amount < 0:
        raise ValueError("Amount must not be negative.")
    if action == "reset":
        amount = 0
    note = (note or "").strip()[:NOTE_MAX]
    now = timeutil.now()
    async with bot.db.transaction() as tx:
        old = int(await tx.fetchval("SELECT count FROM rares_stock WHERE guild_id = ?", (guild_id,), 0))
        new = new_count(action, old, amount)
        await tx.execute(
            "INSERT INTO rares_stock (guild_id, count) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET count = excluded.count",
            (guild_id, new),
        )
        await tx.execute(
            "INSERT INTO rares_log (guild_id, action, amount, old_count, new_count, note, user_id, ticket_number, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, action, amount, old, new, note, user_id, ticket_number, now),
        )
    row = {
        "action": action,
        "amount": amount,
        "old_count": old,
        "new_count": new,
        "note": note,
        "user_id": user_id,
        "ticket_number": ticket_number,
        "created_at": now,
    }
    bot.boards.request_refresh(guild_id, KIND)
    await post_log(bot, guild_id, row)
    await schedule_voice_update(bot, guild_id)
    return old, new


def status_for(count: int) -> tuple[str, int]:
    for threshold, label, colour in RARES_STATUS:
        if count <= threshold:
            return label, colour
    return RARES_STOCKED


def ticket_label(number: int) -> str:
    return f"{int(number):04d}"


def change_text(action: str, amount: int) -> str:
    if action == "add":
        return f"added **{amount}** Rare Alloys"
    if action == "remove":
        return f"removed **{amount}** Rare Alloys"
    if action == "set":
        return f"set the count to **{amount}** Rare Alloys"
    if action == "reset":
        return "reset the count to **0**"
    if action == "payment":
        if amount < 0:
            return f"corrected a ticket payment by **{amount}** Rare Alloys"
        return f"logged a ticket payment of **{amount}** Rare Alloys"
    return f"changed the count ({md(action)} {amount})"


def log_line(row: dict, *, note_limit: int = 120) -> str:
    parts = [
        f"{timeutil.relative(row['created_at'])} <@{row['user_id']}> {change_text(row['action'], row['amount'])} "
        f"({row['old_count']} -> {row['new_count']})"
    ]
    if row.get("ticket_number") is not None:
        parts.append(f"ticket {ticket_label(row['ticket_number'])}")
    if row.get("note"):
        parts.append(f"*{clip(md(row['note']), note_limit)}*")
    return " - ".join(parts)


def change_embed(row: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Rare Alloys",
        description=f"<@{row['user_id']}> {change_text(row['action'], row['amount'])}\n**{row['old_count']} -> {row['new_count']}**",
        colour=ACTION_COLOURS.get(row["action"], Colour.INFO),
        timestamp=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
    )
    if row.get("ticket_number") is not None:
        embed.add_field(name="Ticket", value=ticket_label(row["ticket_number"]), inline=True)
    if row.get("note"):
        embed.add_field(name="Note", value=clip(md(row["note"]), 1024), inline=False)
    return embed


def count_embed(count: int, *, title: str = "Regiment Rare Alloys") -> discord.Embed:
    label, colour = status_for(count)
    embed = discord.Embed(title=title, colour=colour)
    embed.add_field(name="Count", value=f"**{count}** Rare Alloys", inline=True)
    embed.add_field(name="Status", value=label, inline=True)
    return embed


def board_embed(count: int, rows: list[dict]) -> discord.Embed:
    label, colour = status_for(count)
    header = f"## {count} Rare Alloys\nStatus: **{label}**\n\n**Recent changes**\n"
    body_limit = EMBED_DESCRIPTION_LIMIT - len(header) - 50
    lines: list[str] = []
    used = 0
    for row in rows[:BOARD_LOG_LINES]:
        line = clip(log_line(row, note_limit=80), 300)
        if used + len(line) + 1 > body_limit:
            break
        lines.append(line)
        used += len(line) + 1
    body = "\n".join(lines) if lines else "No changes recorded yet."
    embed = discord.Embed(title="Regiment Rare Alloys", description=header + body, colour=colour)
    embed.set_footer(text="Updates automatically | Log shows the full history")
    return embed


def log_embeds(rows: list[dict]) -> list[discord.Embed]:
    return paginate_embeds(
        "Rare Alloys Log",
        [log_line(row, note_limit=200) for row in rows],
        colour=Colour.INFO,
        empty="No changes recorded yet.",
        per_page_chars=3000,
        footer=f"{len(rows)} change(s), newest first",
    )


async def post_log(bot: FoxBot, guild_id: int, row: dict) -> None:
    channel_id = await bot.settings.get(guild_id, keys.RARES_LOG_CHANNEL)
    if not channel_id:
        return
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel_or_thread(int(channel_id)) if guild is not None else None
    if channel is None:
        log.info("Rare Alloys log channel %s not found in guild %s", channel_id, guild_id)
        return
    try:
        await channel.send(embed=change_embed(row), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.warning("Could not post to the Rare Alloys log channel %s", channel_id, exc_info=True)


def voice_name(count: int) -> str:
    return f"Rare Alloys: {count}"


async def schedule_voice_update(bot: FoxBot, guild_id: int) -> None:
    if not await bot.settings.get(guild_id, keys.RARES_VOICE_CHANNEL):
        return
    cog = bot.get_cog(COG_NAME)
    if cog is not None:
        cog.schedule_voice(guild_id)


async def sync_voice(bot: FoxBot, guild_id: int) -> None:
    await schedule_voice_update(bot, guild_id)


def parse_amount(raw: str) -> int | None:
    cleaned = raw.replace(",", "").replace(" ", "").replace("_", "")
    if not INTEGER.fullmatch(cleaned):
        return None
    value = int(cleaned)
    if abs(value) > AMOUNT_MAX:
        return None
    return value


class ChangeModal(BaseModal):
    def __init__(self, bot: FoxBot, guild_id: int, action: str):
        super().__init__(title=MODAL_TITLES[action])
        self.bot = bot
        self.guild_id = guild_id
        self.action = action
        label = "New count" if action == "set" else "Amount"
        self.amount_field = text_field(label, placeholder="e.g. 250", max_length=12)
        self.note_field = text_field(
            "Note",
            placeholder="Optional, e.g. what it was for",
            required=False,
            max_length=NOTE_MAX,
        )
        self.add_item(self.amount_field)
        self.add_item(self.note_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.guild_id):
            return
        amount = parse_amount(text_value(self.amount_field))
        if amount is None:
            await deny(interaction, "That is not a whole number. Enter digits only, for example 250.")
            return
        if amount < 0:
            await deny(interaction, "The amount cannot be negative.")
            return
        if amount == 0 and self.action in ("add", "remove"):
            await deny(interaction, "The amount must be more than zero.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        old, new = await apply_change(
            self.bot,
            self.guild_id,
            action=self.action,
            amount=amount,
            user_id=interaction.user.id,
            note=text_value(self.note_field),
        )
        message = f"Rare Alloys updated: **{old} -> {new}**."
        if self.action == "remove" and amount > old:
            message += f" Only {old} were in stock, so the count stopped at zero."
        await reply(interaction, message)


async def start_change(bot: FoxBot, interaction: discord.Interaction, action: str) -> None:
    if not await check_group(interaction, GROUP):
        return
    await interaction.response.send_modal(ChangeModal(bot, interaction.guild_id, action))


async def show_log(bot: FoxBot, interaction: discord.Interaction) -> None:
    rows = await log_rows(bot, interaction.guild_id)
    await EmbedPaginator(log_embeds(rows), owner_id=interaction.user.id).start(interaction)


class RaresBoard:
    kind = KIND
    title = "Rare Alloys"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        count = await get_count(self.bot, guild.id)
        rows = await log_rows(self.bot, guild.id, BOARD_LOG_LINES)
        return BoardRender(
            embeds=[board_embed(count, rows)],
            buttons=[
                BoardButton("add", "Add", discord.ButtonStyle.success),
                BoardButton("remove", "Remove", discord.ButtonStyle.danger),
                BoardButton("set", "Set", discord.ButtonStyle.primary),
                BoardButton("log", "Log"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action in MODAL_TITLES:
            await start_change(self.bot, interaction, action)
        elif action == "log":
            await show_log(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        await deny(interaction, "This board has no selectable entries.")


class Rares(commands.Cog, name=COG_NAME):
    group = app_commands.Group(name="rare", description="The regiment's Rare Alloys stock.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot
        self.voice_interval = VOICE_RENAME_INTERVAL
        self.clock = time.monotonic
        self._voice_last: dict[int, float] = {}
        self._voice_dirty: dict[int, bool] = {}
        self._voice_tasks: dict[int, asyncio.Task] = {}

    async def cog_unload(self) -> None:
        for task in list(self._voice_tasks.values()):
            task.cancel()
        self._voice_tasks.clear()

    def schedule_voice(self, guild_id: int) -> None:
        self._voice_dirty[guild_id] = True
        if guild_id in self._voice_tasks:
            return
        task = asyncio.get_running_loop().create_task(self._voice_worker(guild_id))
        self._voice_tasks[guild_id] = task

    async def wait_voice(self) -> None:
        while self._voice_tasks:
            await asyncio.gather(*list(self._voice_tasks.values()), return_exceptions=True)

    async def _voice_worker(self, guild_id: int) -> None:
        try:
            while self._voice_dirty.pop(guild_id, False):
                wait = self._voice_last.get(guild_id, -math.inf) + self.voice_interval - self.clock()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._voice_dirty.pop(guild_id, None)
                if await self._rename_voice(guild_id):
                    self._voice_last[guild_id] = self.clock()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Rare Alloys voice channel update failed in guild %s", guild_id)
        finally:
            self._voice_tasks.pop(guild_id, None)

    async def _rename_voice(self, guild_id: int) -> bool:
        channel_id = await self.bot.settings.get(guild_id, keys.RARES_VOICE_CHANNEL)
        guild = self.bot.get_guild(guild_id)
        if not channel_id or guild is None:
            return False
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            log.info("Rare Alloys voice channel %s not found in guild %s", channel_id, guild_id)
            return False
        name = voice_name(await get_count(self.bot, guild_id))
        if channel.name == name:
            return False
        try:
            await channel.edit(name=name, reason="Rare Alloys count changed")
        except discord.HTTPException:
            log.warning("Could not rename the Rare Alloys voice channel in guild %s", guild_id, exc_info=True)
            return False
        return True

    @group.command(name="count", description="Show the regiment's Rare Alloys count in this channel.")
    async def count(self, interaction: discord.Interaction) -> None:
        if await interaction_member(interaction) is None:
            await deny(interaction, "This only works inside the server.")
            return
        value = await get_count(self.bot, interaction.guild_id)
        await reply(interaction, embed=count_embed(value), ephemeral=False)

    @group.command(name="log", description="Show the full Rare Alloys change log (only you see it).")
    async def log_command(self, interaction: discord.Interaction) -> None:
        await show_log(self.bot, interaction)

    @group.command(name="board", description="Admin: post the live Rare Alloys board in this channel (moves it if it exists).")
    async def board(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.boards.post(interaction.guild, KIND, interaction.channel)
        except discord.HTTPException as error:
            await reply(interaction, f"Could not post the board here: {error.text or error}")
            return
        await reply(interaction, "Rare Alloys board posted.")


async def setup(bot: FoxBot) -> None:
    bot.boards.register(RaresBoard(bot))
    await bot.add_cog(Rares(bot))
