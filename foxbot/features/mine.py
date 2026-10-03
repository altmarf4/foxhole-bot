from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import Colour
from foxbot.core import entries, timeutil
from foxbot.core.permissions import interaction_member
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_TOTAL_LIMIT, clip, code, md, plural
from foxbot.core.ui import PickerView, PickOption, deny, reply

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

TOTAL_BUDGET = EMBED_TOTAL_LIMIT - 600
SECTION_LIMIT = EMBED_DESCRIPTION_LIMIT - 96
LINE_LIMIT = 300
OVERFLOW_RESERVE = 60
FOOTER = "Only you can see this. Pick an entry below to open its panel."
EMPTY = (
    "Nothing to show yet. Stockpiles, ships and bases you add, refresh, follow with **Notify Me** "
    "or (for private stockpiles) have access to show up here."
)


@dataclass
class MineEntry:
    kind: str
    entry_id: str
    name: str
    location: str
    deadline: int | None
    line: str
    state: str
    reasons: list[str] = field(default_factory=list)

    def sort_key(self) -> tuple:
        return (self.deadline is None, self.deadline or 0, self.name.lower())

    def text(self) -> str:
        return f"{self.line} ({', '.join(self.reasons)})"


Collector = Callable[["FoxBot", discord.Member, ModuleType, int], Awaitable[list[MineEntry]]]


async def subscribed_ids(bot: FoxBot, kind: str, user_id: int) -> set[str]:
    rows = await bot.db.fetchall(
        "SELECT entry_id FROM entry_subscribers WHERE kind = ? AND user_id = ?",
        (kind, user_id),
    )
    return {row["entry_id"] for row in rows}


async def shared_ids(bot: FoxBot, kind: str, member: discord.Member) -> set[str]:
    rows = await bot.db.fetchall(
        "SELECT entry_id, target_type, target_id FROM entry_access WHERE kind = ?",
        (kind,),
    )
    role_ids = {role.id for role in member.roles}
    return {
        row["entry_id"]
        for row in rows
        if (row["target_type"] == "user" and row["target_id"] == member.id)
        or (row["target_type"] == "role" and row["target_id"] in role_ids)
    }


async def guild_rows(bot: FoxBot, table: str, guild_id: int) -> list[dict]:
    return await bot.db.fetchall(f"SELECT * FROM {table} WHERE guild_id = ?", (guild_id,))


def common_reasons(row: dict, member: discord.Member, subscribed: set[str], refreshed_column: str, refreshed_word: str) -> list[str]:
    reasons = []
    if row["created_by"] == member.id:
        reasons.append("added")
    if row.get(refreshed_column) == member.id:
        reasons.append(refreshed_word)
    if row["id"] in subscribed:
        reasons.append("notified")
    return reasons


def timer_state(expires_at: int, now: int) -> str:
    if expires_at <= now:
        return "EXPIRED"
    return f"{timeutil.format_duration(expires_at - now)} left"


async def stockpile_entries(bot: FoxBot, member: discord.Member, module: ModuleType, now: int) -> list[MineEntry]:
    subscribed = await subscribed_ids(bot, "stockpile", member.id)
    shared = await shared_ids(bot, "stockpile", member)
    result = []
    for row in await guild_rows(bot, "stockpiles", member.guild.id):
        reasons = common_reasons(row, member, subscribed, "refreshed_by", "refreshed")
        if row["private"] and row["id"] in shared:
            reasons.append("access")
        if not reasons or not await module.can_access(bot, member, row):
            continue
        location = entries.location_label(row["hex"], row["region"])
        prefix = "[Private] " if row["private"] else ""
        line = (
            f"{prefix}**{md(row['name'])}** {code(row['code'])} - {md(location)} - "
            f"{entries.timer_text(row['expires_at'], now)}"
        )
        result.append(
            MineEntry("stockpile", row["id"], row["name"], location, row["expires_at"], line, timer_state(row["expires_at"], now), reasons)
        )
    return result


async def ship_entries(bot: FoxBot, member: discord.Member, module: ModuleType, now: int) -> list[MineEntry]:
    subscribed = await subscribed_ids(bot, "ship", member.id)
    result = []
    for row in await guild_rows(bot, "ships", member.guild.id):
        reasons = common_reasons(row, member, subscribed, "refreshed_by", "refreshed")
        if not reasons or not await module.can_access(bot, member, row):
            continue
        location = entries.location_label(row["hex"], row["region"])
        squad = f" ({md(row['squad'])})" if row["squad"] else ""
        line = (
            f"**{md(row['name'])}** [{md(row['ship_type'])}]{squad} - {md(location)} - "
            f"{entries.timer_text(row['expires_at'], now)}"
        )
        result.append(
            MineEntry("ship", row["id"], row["name"], location, row["expires_at"], line, timer_state(row["expires_at"], now), reasons)
        )
    return result


async def msupps_entries(bot: FoxBot, member: discord.Member, module: ModuleType, now: int) -> list[MineEntry]:
    subscribed = await subscribed_ids(bot, "msupps", member.id)
    result = []
    for row in await guild_rows(bot, "msupps_bases", member.guild.id):
        reasons = common_reasons(row, member, subscribed, "measured_by", "measured")
        if not reasons or not await module.can_access(bot, member, row):
            continue
        location = entries.location_label(row["hex"], row["region"])
        line = f"**{md(row['name'])}** - {md(location)} - {module.status_text(row, now)}"
        result.append(
            MineEntry("msupps", row["id"], row["name"], location, module.run_out_at(row), line, module.state_text(row, now), reasons)
        )
    return result


@dataclass(frozen=True)
class Feature:
    kind: str
    title: str
    noun: str
    module: str
    collect: Collector


FEATURES = [
    Feature("stockpile", "Stockpiles", "Stockpile", "foxbot.features.stockpiles", stockpile_entries),
    Feature("ship", "Ships", "Ship", "foxbot.features.ships", ship_entries),
    Feature("msupps", "Msupps", "Base", "foxbot.features.msupps", msupps_entries),
]
FEATURES_BY_KIND = {feature.kind: feature for feature in FEATURES}


async def collect(bot: FoxBot, member: discord.Member, now: int | None = None) -> list[tuple[Feature, list[MineEntry]]]:
    now = now if now is not None else timeutil.now()
    sections = []
    for feature in FEATURES:
        module = bot.extensions.get(feature.module)
        if module is None:
            continue
        found = await feature.collect(bot, member, module, now)
        if found:
            sections.append((feature, sorted(found, key=MineEntry.sort_key)))
    return sections


def fit_sections(sections: list[tuple[Feature, list[MineEntry]]]) -> list[discord.Embed]:
    texts = [[clip(item.text(), LINE_LIMIT) for item in items] for _, items in sections]
    order = sorted(range(len(texts)), key=lambda index: sum(len(line) + 1 for line in texts[index]))
    remaining = TOTAL_BUDGET - sum(len(feature.title) + 10 for feature, _ in sections) - len(FOOTER)
    kept: dict[int, list[str]] = {}
    for position, index in enumerate(order):
        share = min(remaining // (len(order) - position), SECTION_LIMIT)
        lines = texts[index]
        total = sum(len(line) + 1 for line in lines)
        budget = share if total <= share else share - OVERFLOW_RESERVE
        chosen: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) + 1 > budget:
                break
            chosen.append(line)
            used += len(line) + 1
        hidden = len(lines) - len(chosen)
        if hidden:
            note = f"...and {hidden} more. Use the picker below."
            chosen.append(note)
            used += len(note) + 1
        kept[index] = chosen
        remaining -= used
    embeds = []
    for index, (feature, items) in enumerate(sections):
        embeds.append(
            discord.Embed(
                title=f"{feature.title} ({len(items)})",
                description="\n".join(kept[index]),
                colour=Colour.INFO,
            )
        )
    if embeds:
        embeds[-1].set_footer(text=FOOTER)
    return embeds


def pick_options(sections: list[tuple[Feature, list[MineEntry]]]) -> list[PickOption]:
    options = []
    for feature, items in sections:
        for item in items:
            options.append(
                PickOption(
                    f"{item.kind}:{item.entry_id}",
                    f"{feature.noun}: {item.name} - {item.location}",
                    f"{item.state} | {', '.join(item.reasons)}",
                )
            )
    return options


async def open_entry(bot: FoxBot, interaction: discord.Interaction, value: str) -> None:
    kind, _, entry_id = value.partition(":")
    feature = FEATURES_BY_KIND.get(kind)
    module = bot.extensions.get(feature.module) if feature is not None else None
    if module is None or not entry_id:
        await deny(interaction, "That feature is not available right now.")
        return
    await module.open_panel(bot, interaction, entry_id)


async def show_mine(bot: FoxBot, interaction: discord.Interaction) -> None:
    member = await interaction_member(interaction)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return
    sections = await collect(bot, member)
    if not sections:
        await reply(interaction, EMPTY)
        return
    total = sum(len(items) for _, items in sections)

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await open_entry(bot, pick_interaction, value)

    view = PickerView(pick_options(sections), picked, owner_id=interaction.user.id, placeholder="Open an entry...")
    await reply(interaction, f"You are involved in {plural(total, 'tracked entry', 'tracked entries')}.", embeds=fit_sections(sections), view=view)


class Mine(commands.Cog):
    def __init__(self, bot: FoxBot):
        self.bot = bot

    @app_commands.command(name="mine", description="Everything you added, refreshed, follow or can access, with timers (only you see it).")
    @app_commands.guild_only()
    async def mine(self, interaction: discord.Interaction) -> None:
        await show_mine(self.bot, interaction)


async def setup(bot: FoxBot) -> None:
    await bot.add_cog(Mine(bot))
