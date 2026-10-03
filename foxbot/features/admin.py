from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot import constants
from foxbot.constants import Colour
from foxbot.core import settings as keys
from foxbot.core import timeutil
from foxbot.core.backups import KEEP as BACKUPS_KEPT
from foxbot.core.permissions import GROUPS, check_admin, interaction_member
from foxbot.core.settings import Setting
from foxbot.core.text import EMBED_FIELD_LIMIT, clip, md, plural
from foxbot.core.ui import (
    BaseModal,
    BaseView,
    edit,
    option,
    reply,
    select_field,
    select_value,
    text_field,
    text_value,
)

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

MAX_THRESHOLDS = 10
MAX_THRESHOLD_HOURS = 720
MAX_LIST_VALUES = 25
LIST_VALUE_MAX_LENGTH = 40
MAX_SERVICES = 25
SERVICE_NAME_MAX_LENGTH = 60
MAX_SERVICE_COST = 1_000_000

FACTIONS = {"Warden": "warden", "Colonial": "colonial"}
NO_FACTION = "Not set"

TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
VOICE_CHANNEL_TYPES = [discord.ChannelType.voice]

SEND_PERMISSIONS = {"view_channel": "View Channel", "send_messages": "Send Messages", "embed_links": "Embed Links"}
RENAME_PERMISSIONS = {"view_channel": "View Channel", "manage_channels": "Manage Channels"}

ACCESS_GROUPS = ["stockpile", "ship", "msupps", "orders", "inventory", "rares"]

SECTIONS = {
    "general": ("General", "Roles, alerts channel, reminder thresholds, faction, backups"),
    "stockpiles": ("Stockpiles", "Storage types"),
    "ships": ("Ships", "Ship types"),
    "tickets": ("Tickets", "Services, prices and ticket channels"),
    "rares": ("Rares", "Rare Alloys log and voice channels"),
    "orders": ("Orders", "Orders channel"),
}

SETUP_STEPS = [
    ("admin", "Admin role"),
    ("logistics", "Logistics ping role"),
    ("alerts", "Alerts channel"),
    ("faction", "Faction"),
    ("tickets", "Ticket staff"),
    ("access", "Who can use the trackers"),
    ("boards", "Post boards"),
]

SelectHandler = Callable[[discord.Interaction, Any], Awaitable[None]]


@dataclass(frozen=True)
class ListSection:
    key: str
    setting: Setting
    title: str
    noun: str
    defaults: tuple[str, ...]
    usage: str


LIST_SECTIONS = {
    "stockpiles": ListSection(
        "stockpiles",
        keys.STORAGE_TYPES,
        "Stockpiles",
        "storage type",
        tuple(constants.DEFAULT_STORAGE_TYPES),
        "Storage types offered when adding or editing a stockpile.",
    ),
    "ships": ListSection(
        "ships",
        keys.SHIP_TYPES,
        "Ships",
        "ship type",
        tuple(constants.DEFAULT_SHIP_TYPES),
        "Ship types offered when adding or editing a ship.",
    ),
}


def parse_thresholds(text: str) -> list[float]:
    parts = [part for part in re.split(r"[\s,;]+", text.strip()) if part]
    if not parts:
        raise ValueError("enter at least one number of hours.")
    values: set[float] = set()
    for part in parts:
        cleaned = part.lower().removesuffix("h")
        try:
            value = float(cleaned)
        except ValueError:
            raise ValueError(f"**{md(clip(part, 20))}** is not a number of hours.") from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("every threshold must be a positive number of hours.")
        if value > MAX_THRESHOLD_HOURS:
            raise ValueError(f"thresholds can be at most {MAX_THRESHOLD_HOURS} hours.")
        values.add(round(value, 2))
    if len(values) > MAX_THRESHOLDS:
        raise ValueError(f"use at most {MAX_THRESHOLDS} thresholds.")
    return sorted(values, reverse=True)


def parse_cost(text: str) -> int:
    cleaned = re.sub(r"[\s,_']", "", text)
    if not re.fullmatch(r"[0-9]+", cleaned):
        raise ValueError("The cost must be a whole number of Rare Alloys, 0 or more.")
    value = int(cleaned)
    if value > MAX_SERVICE_COST:
        raise ValueError(f"The cost can be at most {MAX_SERVICE_COST:,} Rare Alloys.")
    return value


def clean_name(text: str) -> str:
    return " ".join(str(text).split())


async def store_optional(bot: FoxBot, guild_id: int, setting: Setting, value: Any) -> None:
    if value is None:
        await bot.settings.reset(guild_id, setting)
    else:
        await bot.settings.set(guild_id, setting, value)


async def add_list_value(bot: FoxBot, guild_id: int, section: ListSection, value: str) -> str | None:
    value = clean_name(value)
    if not value:
        return f"The {section.noun} name cannot be empty."
    current = list(await bot.settings.get(guild_id, section.setting))
    if any(existing.casefold() == value.casefold() for existing in current):
        return f"**{md(value)}** is already in the list."
    if len(current) >= MAX_LIST_VALUES:
        return f"There can be at most {MAX_LIST_VALUES} {section.noun}s. Remove one first."
    await bot.settings.set(guild_id, section.setting, [*current, value])
    return None


async def remove_list_value(bot: FoxBot, guild_id: int, section: ListSection, value: str) -> str | None:
    current = list(await bot.settings.get(guild_id, section.setting))
    remaining = [existing for existing in current if existing != value]
    if len(remaining) == len(current):
        return f"**{md(value)}** is no longer in the list."
    if not remaining:
        return f"Keep at least one {section.noun}. Add another one before removing **{md(value)}**."
    await bot.settings.set(guild_id, section.setting, remaining)
    return None


async def list_services(bot: FoxBot, guild_id: int) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT name, cost FROM ticket_services WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
        (guild_id,),
    )


async def save_service(bot: FoxBot, guild_id: int, name: str, cost: int) -> bool:
    name = clean_name(name)
    if not name:
        raise ValueError("The service name cannot be empty.")
    services = await list_services(bot, guild_id)
    existing = next((row for row in services if row["name"].casefold() == name.casefold()), None)
    if existing is None and len(services) >= MAX_SERVICES:
        raise ValueError(f"There can be at most {MAX_SERVICES} services. Remove one first.")
    async with bot.db.transaction() as tx:
        if existing is not None:
            await tx.execute("DELETE FROM ticket_services WHERE guild_id = ? AND name = ?", (guild_id, existing["name"]))
        await tx.execute("INSERT INTO ticket_services (guild_id, name, cost) VALUES (?, ?, ?)", (guild_id, name, cost))
    return existing is not None


async def remove_service(bot: FoxBot, guild_id: int, name: str) -> bool:
    return bool(await bot.db.execute("DELETE FROM ticket_services WHERE guild_id = ? AND name = ?", (guild_id, name)))


async def group_roles(bot: FoxBot, guild_id: int, groups: list[str] | None = None) -> dict[str, set[int]]:
    return {group: await bot.perms.roles_for(guild_id, group) for group in (groups or list(GROUPS))}


async def premark_new_thresholds(bot: FoxBot, guild_id: int, added: list[float], now: int | None = None) -> int:
    now = now if now is not None else timeutil.now()
    rows: list[tuple[str, str, float, int]] = []
    for provider in list(bot.alerts.providers.values()):
        try:
            timed = await provider.timed_entries()
        except Exception:
            log.exception("Could not load timed entries for %s", getattr(provider, "kind", provider))
            continue
        for entry in timed:
            if entry.guild_id != guild_id:
                continue
            remaining = entry.deadline - now
            if remaining <= 0:
                continue
            rows.extend((entry.kind, entry.entry_id, float(t), now) for t in added if remaining <= t * 3600)
    if rows:
        async with bot.db.transaction() as tx:
            await tx.executemany(
                "INSERT OR IGNORE INTO alert_state (kind, entry_id, threshold, sent_at) VALUES (?, ?, ?, ?)",
                rows,
            )
    return len(rows)


async def refresh_rares_voice(bot: FoxBot, guild_id: int) -> None:
    module = bot.extensions.get("foxbot.features.rares")
    sync = getattr(module, "sync_voice", None) if module is not None else None
    if sync is None:
        return
    try:
        await sync(bot, guild_id)
    except Exception:
        log.exception("Could not update the Rare Alloys voice channel in guild %s", guild_id)


async def update_thresholds(bot: FoxBot, guild_id: int, raw: str) -> str | None:
    old = await bot.settings.alert_thresholds(guild_id)
    if not raw.strip():
        await bot.settings.reset(guild_id, keys.ALERT_THRESHOLDS)
    else:
        try:
            values = parse_thresholds(raw)
        except ValueError as error:
            return f"Reminder thresholds were not changed: {error}"
        await bot.settings.set(guild_id, keys.ALERT_THRESHOLDS, values)
    new = await bot.settings.alert_thresholds(guild_id)
    added = [t for t in new if t not in old]
    if added:
        await premark_new_thresholds(bot, guild_id, added)
    return None


def faction_label(value: Any) -> str:
    lookup = {stored: label for label, stored in FACTIONS.items()}
    return lookup.get(str(value).lower(), NO_FACTION) if value else NO_FACTION


def role_text(guild: discord.Guild, role_id: int | None, empty: str = "Not set") -> str:
    if not role_id:
        return empty
    if role_id == guild.id:
        return "Everyone"
    return f"<@&{role_id}>"


def channel_text(channel_id: int | None, empty: str = "Not set") -> str:
    return f"<#{channel_id}>" if channel_id else empty


def roles_text(guild: discord.Guild, role_ids: set[int], empty: str = "Admins only") -> str:
    if not role_ids:
        return empty
    parts = ["Everyone"] if guild.id in role_ids else []
    parts += [f"<@&{rid}>" for rid in sorted(role_ids) if rid != guild.id]
    return clip(", ".join(parts), EMBED_FIELD_LIMIT)


def roles_plain(guild: discord.Guild, role_ids: set[int], empty: str = "Admins only") -> str:
    if not role_ids:
        return empty
    names = ["Everyone"] if guild.id in role_ids else []
    for rid in sorted(role_ids):
        if rid == guild.id:
            continue
        role = guild.get_role(rid)
        names.append(role.name if role is not None else "Deleted role")
    return ", ".join(names)


def thresholds_text(values: list[float]) -> str:
    return ", ".join(timeutil.format_hours(float(v)) for v in sorted(values, reverse=True)) or "None"


def thresholds_input(values: list[float]) -> str:
    return ", ".join(f"{float(v):g}" for v in sorted(values, reverse=True))


def role_defaults(guild: discord.Guild, role_ids) -> list[discord.SelectDefaultValue]:
    return [
        discord.SelectDefaultValue(id=rid, type=discord.SelectDefaultValueType.role)
        for rid in sorted(set(role_ids))
        if rid and rid != guild.id and guild.get_role(rid) is not None
    ][:25]


def channel_defaults(channel_id: int | None) -> list[discord.SelectDefaultValue]:
    if not channel_id:
        return []
    return [discord.SelectDefaultValue(id=channel_id, type=discord.SelectDefaultValueType.channel)]


def missing_permissions(channel: Any, me: discord.Member | None, needed: dict[str, str] = SEND_PERMISSIONS) -> list[str]:
    if me is None or channel is None:
        return []
    permissions = channel.permissions_for(me)
    return [label for attribute, label in needed.items() if not getattr(permissions, attribute, True)]


def channel_warning(guild: discord.Guild, channel_id: int | None, needed: dict[str, str], purpose: str) -> str | None:
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        return f"I cannot see <#{channel_id}>. Give me the View Channel permission there so I can {purpose}."
    missing = missing_permissions(channel, guild.me, needed)
    if missing:
        return f"I cannot {purpose} in {channel.mention} yet: I am missing {', '.join(missing)}."
    return None


def services_lines(services: list[dict]) -> list[str]:
    return [f"- **{md(row['name'])}**: {row['cost']:,} Rare Alloys" for row in services]


async def general_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    admin_role = await bot.settings.get(guild.id, keys.ADMIN_ROLE)
    logistics_role = await bot.settings.get(guild.id, keys.LOGISTICS_ROLE)
    alert_channel = await bot.settings.get(guild.id, keys.ALERT_CHANNEL)
    thresholds = await bot.settings.alert_thresholds(guild.id)
    faction = await bot.settings.get(guild.id, keys.FACTION)
    embed = discord.Embed(
        title="Settings - General",
        description="Press **Edit General Settings** to change these. Pick another section below for more settings.",
        colour=Colour.INFO,
    )
    embed.add_field(
        name="Admin role",
        value=role_text(guild, admin_role, "Not set. Only the server owner and members with the Administrator permission are admins."),
        inline=False,
    )
    embed.add_field(name="Logistics ping role", value=role_text(guild, logistics_role, "Not set. Reminders ping nobody by default."), inline=False)
    embed.add_field(
        name="Alerts channel",
        value=channel_text(alert_channel, "Not set. Each reminder is posted in its board's channel."),
        inline=False,
    )
    embed.add_field(
        name="Reminder thresholds",
        value=f"{thresholds_text(thresholds)} before a timer runs out, plus one notice when it expires.",
        inline=False,
    )
    embed.add_field(name="Faction", value=faction_label(faction), inline=False)
    embed.add_field(
        name="Backups",
        value=(
            f"The database is backed up every day and the last {BACKUPS_KEPT} copies are kept on the bot's host. "
            "**Back Up Now** makes one immediately."
        ),
        inline=False,
    )
    embed.set_footer(text="Only bot admins can change settings.")
    return embed


async def list_embed(bot: FoxBot, guild: discord.Guild, section: ListSection) -> discord.Embed:
    values = list(await bot.settings.get(guild.id, section.setting))
    lines = "\n".join(f"- {md(value)}" for value in values) or "None"
    embed = discord.Embed(
        title=f"Settings - {section.title}",
        description=f"{section.usage}\n\n{lines}",
        colour=Colour.INFO,
    )
    embed.set_footer(text=f"{len(values)} of at most {MAX_LIST_VALUES} {section.noun}s. At least one must remain.")
    return embed


async def tickets_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    services = await list_services(bot, guild.id)
    category_id = await bot.settings.get(guild.id, keys.TICKET_CATEGORY)
    log_channel = await bot.settings.get(guild.id, keys.TICKET_LOG_CHANNEL)
    lines = services_lines(services) or ["No services yet. Customers cannot order anything until you add one."]
    embed = discord.Embed(
        title="Settings - Tickets",
        description="Services customers can order in a ticket, with their price in Rare Alloys.\n\n" + "\n".join(lines),
        colour=Colour.INFO,
    )
    category = guild.get_channel(category_id) if category_id else None
    if category is not None:
        category_value = f"**{md(category.name)}**"
    elif category_id:
        category_value = "Missing. It is created again with the next ticket."
    else:
        category_value = "Not created yet. It is created automatically with the first ticket."
    embed.add_field(name="Ticket category", value=category_value, inline=False)
    embed.add_field(
        name="Ticket log channel",
        value=channel_text(log_channel, "Not created yet. The ticket system creates it automatically."),
        inline=False,
    )
    embed.set_footer(text=f"{len(services)} of at most {MAX_SERVICES} services. Prices are saved on each ticket when ordered.")
    return embed


async def rares_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    log_channel = await bot.settings.get(guild.id, keys.RARES_LOG_CHANNEL)
    voice_channel = await bot.settings.get(guild.id, keys.RARES_VOICE_CHANNEL)
    embed = discord.Embed(
        title="Settings - Rares",
        description="Optional channels for the regiment's Rare Alloys stock.",
        colour=Colour.INFO,
    )
    embed.add_field(name="Log channel", value=channel_text(log_channel, "Not set. Changes are only kept in the log."), inline=False)
    embed.add_field(
        name="Voice channel",
        value=channel_text(voice_channel, "Not set. No channel shows the live count."),
        inline=False,
    )
    embed.set_footer(text="Discord allows only 2 channel renames per 10 minutes, so the voice channel name can lag behind.")
    return embed


async def orders_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    channel_id = await bot.settings.get(guild.id, keys.ORDERS_CHANNEL)
    embed = discord.Embed(
        title="Settings - Orders",
        description="The channel where new order cards are posted.",
        colour=Colour.INFO,
    )
    embed.add_field(
        name="Orders channel",
        value=channel_text(channel_id, "Not set. Order cards are posted in the channel where each order is created."),
        inline=False,
    )
    return embed


def permissions_embed(guild: discord.Guild, current: dict[str, set[int]]) -> discord.Embed:
    embed = discord.Embed(
        title="Permissions",
        description=(
            "Choose which roles may use each part of the bot. Pick a group below to change it.\n\n"
            "Admins always pass every check: the server owner, members with the Administrator permission "
            "and members with the admin role from `/settings`.\n"
            "Adding @everyone (or setting **Open to everyone** to Yes) opens a group to all members.\n"
            "A group without roles can only be used by admins."
        ),
        colour=Colour.INFO,
    )
    for group, label in GROUPS.items():
        embed.add_field(name=label, value=roles_text(guild, current.get(group, set())), inline=False)
    embed.set_footer(text="Moderation commands use Discord's own Manage Messages and Manage Roles permissions.")
    return embed


async def boards_lines(bot: FoxBot, guild: discord.Guild) -> list[str]:
    lines = []
    for kind, provider in sorted(bot.boards.providers.items(), key=lambda item: item[1].title.lower()):
        record = await bot.boards.record(guild.id, kind)
        where = f"posted in <#{record['channel_id']}>" if record else "not posted yet"
        lines.append(f"**{md(provider.title)}**: {where}")
    return lines


async def summary_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Server Setup - Summary",
        description="Setup is done. Change anything later with `/settings` and `/permissions`, or run `/setup` again.",
        colour=Colour.SUCCESS,
    )
    embed.add_field(name="Admin role", value=role_text(guild, await bot.settings.get(guild.id, keys.ADMIN_ROLE)), inline=True)
    embed.add_field(
        name="Logistics ping role",
        value=role_text(guild, await bot.settings.get(guild.id, keys.LOGISTICS_ROLE)),
        inline=True,
    )
    embed.add_field(name="Alerts channel", value=channel_text(await bot.settings.get(guild.id, keys.ALERT_CHANNEL)), inline=True)
    embed.add_field(name="Faction", value=faction_label(await bot.settings.get(guild.id, keys.FACTION)), inline=True)
    embed.add_field(name="Reminder thresholds", value=thresholds_text(await bot.settings.alert_thresholds(guild.id)), inline=True)
    current = await group_roles(bot, guild.id)
    permission_lines = [f"**{label}**: {roles_text(guild, current[group])}" for group, label in GROUPS.items()]
    embed.add_field(name="Permissions", value=clip("\n".join(permission_lines), EMBED_FIELD_LIMIT), inline=False)
    board_lines = await boards_lines(bot, guild)
    embed.add_field(name="Boards", value=clip("\n".join(board_lines) or "No boards are available.", EMBED_FIELD_LIMIT), inline=False)
    return embed


def role_select_label(text: str, description: str, guild: discord.Guild, role_ids, *, max_values: int = 1) -> discord.ui.Label:
    return discord.ui.Label(
        text=text,
        description=description,
        component=discord.ui.RoleSelect(
            min_values=0,
            max_values=max_values,
            required=False,
            default_values=role_defaults(guild, role_ids)[:max_values],
        ),
    )


def channel_select_label(text: str, description: str, channel_id: int | None, channel_types: list[discord.ChannelType]) -> discord.ui.Label:
    return discord.ui.Label(
        text=text,
        description=description,
        component=discord.ui.ChannelSelect(
            channel_types=channel_types,
            min_values=0,
            max_values=1,
            required=False,
            default_values=channel_defaults(channel_id),
        ),
    )


def first_id(label: discord.ui.Label) -> int | None:
    values = label.component.values
    return values[0].id if values else None


async def lost_admin_warning(bot: FoxBot, interaction: discord.Interaction) -> str | None:
    member = await interaction_member(interaction)
    if member is not None and not await bot.perms.is_admin(member):
        return "You are no longer a bot admin, so further changes will be refused."
    return None


class GeneralModal(BaseModal, title="General Settings"):
    def __init__(
        self,
        panel: SettingsPanel,
        guild: discord.Guild,
        *,
        admin_role: int | None,
        logistics_role: int | None,
        alert_channel: int | None,
        thresholds: list[float],
        faction: str | None,
    ):
        super().__init__()
        self.panel = panel
        self.admin_field = role_select_label(
            "Admin role",
            "Members with this role can use every admin panel and command.",
            guild,
            [admin_role] if admin_role else [],
        )
        self.logistics_field = role_select_label(
            "Logistics ping role",
            "Pinged in the alerts channel when timers run low.",
            guild,
            [logistics_role] if logistics_role else [],
        )
        self.channel_field = channel_select_label(
            "Alerts channel",
            "Where reminders are posted. Leave empty to use each board's channel.",
            alert_channel,
            TEXT_CHANNEL_TYPES,
        )
        self.thresholds_field = text_field(
            "Reminder thresholds (hours)",
            default=thresholds_input(thresholds),
            placeholder="12, 6, 2, 1",
            description=f"Up to {MAX_THRESHOLDS} positive numbers, separated by commas. Empty restores 12, 6, 2, 1.",
            required=False,
            max_length=100,
        )
        self.faction_field = select_field(
            "Faction",
            [*FACTIONS, NO_FACTION],
            default=faction_label(faction),
            description="Used to tell apart items that share a name. Switch it every war.",
        )
        for item in (self.admin_field, self.logistics_field, self.channel_field, self.thresholds_field, self.faction_field):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        bot = self.panel.bot
        problems: list[str] = []
        admin_role = first_id(self.admin_field)
        if admin_role == guild.id:
            problems.append("@everyone cannot be the admin role, so the admin role was not changed.")
        else:
            await store_optional(bot, guild.id, keys.ADMIN_ROLE, admin_role)
        logistics_role = first_id(self.logistics_field)
        if logistics_role == guild.id:
            problems.append("@everyone cannot be the logistics ping role, so it was not changed.")
        else:
            await store_optional(bot, guild.id, keys.LOGISTICS_ROLE, logistics_role)
        alert_channel = first_id(self.channel_field)
        await store_optional(bot, guild.id, keys.ALERT_CHANNEL, alert_channel)
        warning = channel_warning(guild, alert_channel, SEND_PERMISSIONS, "post reminders")
        if warning:
            problems.append(warning)
        threshold_problem = await update_thresholds(bot, guild.id, text_value(self.thresholds_field))
        if threshold_problem:
            problems.append(threshold_problem)
        await store_optional(bot, guild.id, keys.FACTION, FACTIONS.get(select_value(self.faction_field) or NO_FACTION))
        lost = await lost_admin_warning(bot, interaction)
        if lost:
            problems.append(lost)
        await self.panel.show(interaction, "\n".join(["General settings saved.", *problems]))


class ListValueModal(BaseModal):
    def __init__(self, panel: SettingsPanel, section: ListSection):
        super().__init__(title=f"Add {section.noun.title()}")
        self.panel = panel
        self.section = section
        self.value_field = text_field(
            "Name",
            placeholder=f"e.g. {section.defaults[0]}" if section.defaults else None,
            description=f"Shown exactly like this when picking a {section.noun}.",
            max_length=LIST_VALUE_MAX_LENGTH,
        )
        self.add_item(self.value_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        value = clean_name(text_value(self.value_field))
        error = await add_list_value(self.panel.bot, guild.id, self.section, value)
        await self.panel.show(interaction, error or f"Added **{md(value)}**.")


class ServiceModal(BaseModal, title="Add or Update Service"):
    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel
        self.name_field = text_field(
            "Service name",
            placeholder="e.g. Tank production",
            description="Using the name of an existing service updates its cost.",
            max_length=SERVICE_NAME_MAX_LENGTH,
        )
        self.cost_field = text_field(
            "Cost in Rare Alloys",
            placeholder="e.g. 50",
            description="A whole number, 0 or more.",
            max_length=9,
        )
        self.add_item(self.name_field)
        self.add_item(self.cost_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        name = clean_name(text_value(self.name_field))
        try:
            cost = parse_cost(text_value(self.cost_field))
            updated = await save_service(self.panel.bot, guild.id, name, cost)
        except ValueError as error:
            await self.panel.show(interaction, f"Nothing was saved. {error}")
            return
        verb = "Updated" if updated else "Added"
        await self.panel.show(interaction, f"{verb} **{md(name)}**: {cost:,} Rare Alloys.")


class RaresChannelsModal(BaseModal, title="Rare Alloys Channels"):
    def __init__(self, panel: SettingsPanel, log_channel: int | None, voice_channel: int | None):
        super().__init__()
        self.panel = panel
        self.log_field = channel_select_label(
            "Log channel",
            "Every change to the Rare Alloys count is posted here. Leave empty for none.",
            log_channel,
            TEXT_CHANNEL_TYPES,
        )
        self.voice_field = channel_select_label(
            "Voice channel",
            "Renamed to show the live Rare Alloys count. Leave empty for none.",
            voice_channel,
            VOICE_CHANNEL_TYPES,
        )
        self.add_item(self.log_field)
        self.add_item(self.voice_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        bot = self.panel.bot
        log_channel = first_id(self.log_field)
        voice_channel = first_id(self.voice_field)
        await store_optional(bot, guild.id, keys.RARES_LOG_CHANNEL, log_channel)
        await store_optional(bot, guild.id, keys.RARES_VOICE_CHANNEL, voice_channel)
        if voice_channel:
            await refresh_rares_voice(bot, guild.id)
        problems = [
            warning
            for warning in (
                channel_warning(guild, log_channel, SEND_PERMISSIONS, "post the Rare Alloys log"),
                channel_warning(guild, voice_channel, RENAME_PERMISSIONS, "rename the voice channel"),
            )
            if warning
        ]
        await self.panel.show(interaction, "\n".join(["Rare Alloys channels saved.", *problems]))


class OrdersChannelModal(BaseModal, title="Orders Channel"):
    def __init__(self, panel: SettingsPanel, channel_id: int | None):
        super().__init__()
        self.panel = panel
        self.channel_field = channel_select_label(
            "Orders channel",
            "Where new order cards are posted. Leave empty for none.",
            channel_id,
            TEXT_CHANNEL_TYPES,
        )
        self.add_item(self.channel_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        channel_id = first_id(self.channel_field)
        await store_optional(self.panel.bot, guild.id, keys.ORDERS_CHANNEL, channel_id)
        warning = channel_warning(guild, channel_id, SEND_PERMISSIONS, "post order cards")
        await self.panel.show(interaction, "\n".join(filter(None, ["Orders channel saved.", warning])))


class AdminPanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id

    async def guard(self, interaction: discord.Interaction) -> discord.Guild | None:
        if not await check_admin(interaction):
            return None
        return interaction.guild or self.bot.get_guild(self.guild_id)

    def _button(self, label: str, style: discord.ButtonStyle, callback, *, row: int, disabled: bool = False) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)
        return button

    def _watch(self, component: discord.ui.Item, handler: SelectHandler) -> None:
        component.callback = lambda interaction, c=component: handler(interaction, c)
        self.add_item(component)

    async def render(self, guild: discord.Guild) -> discord.Embed:
        raise NotImplementedError

    async def send(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        guild = interaction.guild or self.bot.get_guild(self.guild_id)
        embed = await self.render(guild)
        await reply(interaction, notice, embed=embed, view=self)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        guild = interaction.guild or self.bot.get_guild(self.guild_id)
        embed = await self.render(guild)
        await edit(interaction, content=notice, embed=embed, view=self)


class SettingsPanel(AdminPanel):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int, section: str = "general"):
        super().__init__(bot, guild_id, owner_id)
        self.section = section if section in SECTIONS else "general"

    async def render(self, guild: discord.Guild) -> discord.Embed:
        self.clear_items()
        sections = discord.ui.Select(
            placeholder="Choose a settings section...",
            options=[option(label, key, description, default=key == self.section) for key, (label, description) in SECTIONS.items()],
            row=0,
        )
        self._watch(sections, self._section_picked)
        if self.section in LIST_SECTIONS:
            return await self._render_list(guild, LIST_SECTIONS[self.section])
        if self.section == "tickets":
            return await self._render_tickets(guild)
        if self.section == "rares":
            self._button("Set Channels", discord.ButtonStyle.primary, self._edit_rares, row=1)
            self._button("Clear Log Channel", discord.ButtonStyle.secondary, self._clear_rares_log, row=1)
            self._button("Clear Voice Channel", discord.ButtonStyle.secondary, self._clear_rares_voice, row=1)
            return await rares_embed(self.bot, guild)
        if self.section == "orders":
            self._button("Set Orders Channel", discord.ButtonStyle.primary, self._edit_orders, row=1)
            self._button("Clear Orders Channel", discord.ButtonStyle.secondary, self._clear_orders, row=1)
            return await orders_embed(self.bot, guild)
        self._button("Edit General Settings", discord.ButtonStyle.primary, self._edit_general, row=1)
        self._button("Back Up Now", discord.ButtonStyle.secondary, self._backup, row=1)
        return await general_embed(self.bot, guild)

    async def _render_list(self, guild: discord.Guild, section: ListSection) -> discord.Embed:
        values = list(await self.bot.settings.get(guild.id, section.setting))
        if values:
            remove = discord.ui.Select(
                placeholder=f"Remove a {section.noun}...",
                options=[option(value, value[:100]) for value in values[:25]],
                row=1,
            )
            self._watch(remove, self._remove_list_value)
        self._button(f"Add {section.noun.title()}", discord.ButtonStyle.success, self._add_list_value, row=2)
        self._button("Reset to Defaults", discord.ButtonStyle.secondary, self._reset_list, row=2)
        return await list_embed(self.bot, guild, section)

    async def _render_tickets(self, guild: discord.Guild) -> discord.Embed:
        services = await list_services(self.bot, guild.id)
        if services:
            remove = discord.ui.Select(
                placeholder="Remove a service...",
                options=[option(row["name"], row["name"][:100], f"{row['cost']:,} Rare Alloys") for row in services[:25]],
                row=1,
            )
            self._watch(remove, self._remove_service)
        self._button("Add or Update Service", discord.ButtonStyle.success, self._add_service, row=2)
        self._button("Reset Ticket Category", discord.ButtonStyle.secondary, self._reset_ticket_category, row=2)
        self._button("Reset Log Channel", discord.ButtonStyle.secondary, self._reset_ticket_log, row=2)
        return await tickets_embed(self.bot, guild)

    async def _section_picked(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if await self.guard(interaction) is None:
            return
        value = select.values[0] if select.values else "general"
        self.section = value if value in SECTIONS else "general"
        await self.show(interaction)

    async def _edit_general(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        modal = GeneralModal(
            self,
            guild,
            admin_role=await self.bot.settings.get(guild.id, keys.ADMIN_ROLE),
            logistics_role=await self.bot.settings.get(guild.id, keys.LOGISTICS_ROLE),
            alert_channel=await self.bot.settings.get(guild.id, keys.ALERT_CHANNEL),
            thresholds=await self.bot.settings.alert_thresholds(guild.id),
            faction=await self.bot.settings.get(guild.id, keys.FACTION),
        )
        await interaction.response.send_modal(modal)

    async def _backup(self, interaction: discord.Interaction) -> None:
        if await self.guard(interaction) is None:
            return
        await interaction.response.defer()
        try:
            path = await self.bot.backups.backup_now()
        except Exception:
            log.exception("Manual backup failed")
            notice = "The backup failed. The error was logged for the bot host."
        else:
            notice = f"Backup saved as `{path.name}` on the bot's host."
        await self.show(interaction, notice)

    async def _add_list_value(self, interaction: discord.Interaction) -> None:
        if await self.guard(interaction) is None:
            return
        section = LIST_SECTIONS.get(self.section)
        if section is None:
            await self.show(interaction)
            return
        await interaction.response.send_modal(ListValueModal(self, section))

    async def _remove_list_value(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        section = LIST_SECTIONS.get(self.section)
        if section is None or not select.values:
            await self.show(interaction)
            return
        value = select.values[0]
        error = await remove_list_value(self.bot, guild.id, section, value)
        await self.show(interaction, error or f"Removed **{md(value)}**. Entries that already use it keep it.")

    async def _reset_list(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        section = LIST_SECTIONS.get(self.section)
        if section is None:
            await self.show(interaction)
            return
        await self.bot.settings.reset(guild.id, section.setting)
        await self.show(interaction, f"The {section.noun}s are back to the defaults.")

    async def _add_service(self, interaction: discord.Interaction) -> None:
        if await self.guard(interaction) is None:
            return
        await interaction.response.send_modal(ServiceModal(self))

    async def _remove_service(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        name = select.values[0] if select.values else ""
        removed = await remove_service(self.bot, guild.id, name)
        notice = f"Removed **{md(name)}**. Tickets that already ordered it keep their price." if removed else "That service was already removed."
        await self.show(interaction, notice)

    async def _reset_ticket_category(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        await self.bot.settings.reset(guild.id, keys.TICKET_CATEGORY)
        await self.show(interaction, "Ticket category reset. A new one is created with the next ticket; the old category was not deleted.")

    async def _reset_ticket_log(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        await self.bot.settings.reset(guild.id, keys.TICKET_LOG_CHANNEL)
        await self.show(interaction, "Ticket log channel reset. The ticket system creates a new one when it next needs it; the old channel was not deleted.")

    async def _edit_rares(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        modal = RaresChannelsModal(
            self,
            await self.bot.settings.get(guild.id, keys.RARES_LOG_CHANNEL),
            await self.bot.settings.get(guild.id, keys.RARES_VOICE_CHANNEL),
        )
        await interaction.response.send_modal(modal)

    async def _clear(self, interaction: discord.Interaction, setting: Setting, notice: str) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        await self.bot.settings.reset(guild.id, setting)
        await self.show(interaction, notice)

    async def _clear_rares_log(self, interaction: discord.Interaction) -> None:
        await self._clear(interaction, keys.RARES_LOG_CHANNEL, "Rare Alloys log channel cleared.")

    async def _clear_rares_voice(self, interaction: discord.Interaction) -> None:
        await self._clear(interaction, keys.RARES_VOICE_CHANNEL, "Rare Alloys voice channel cleared. Its current name is left as it is.")

    async def _edit_orders(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        await interaction.response.send_modal(OrdersChannelModal(self, await self.bot.settings.get(guild.id, keys.ORDERS_CHANNEL)))

    async def _clear_orders(self, interaction: discord.Interaction) -> None:
        await self._clear(interaction, keys.ORDERS_CHANNEL, "Orders channel cleared.")


class PermissionModal(BaseModal):
    def __init__(self, panel: PermissionsPanel, guild: discord.Guild, group: str, current: set[int]):
        super().__init__(title=clip(f"Permissions: {GROUPS[group]}", 45))
        self.panel = panel
        self.group = group
        self.roles_field = role_select_label(
            "Roles allowed",
            "Members with any of these roles can use it. Admins always can.",
            guild,
            current,
            max_values=25,
        )
        self.everyone_field = select_field(
            "Open to everyone",
            ["No", "Yes"],
            default="Yes" if guild.id in current else "No",
            description="Yes lets every member use it, the same as granting @everyone.",
        )
        self.add_item(self.roles_field)
        self.add_item(self.everyone_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        role_ids = {role.id for role in self.roles_field.component.values}
        if select_value(self.everyone_field) == "Yes":
            role_ids.add(guild.id)
        await self.panel.bot.perms.set_roles(guild.id, self.group, role_ids)
        await self.panel.show(interaction, f"**{GROUPS[self.group]}** can now be used by: {roles_text(guild, role_ids)}.")


class PermissionsPanel(AdminPanel):
    async def render(self, guild: discord.Guild) -> discord.Embed:
        self.clear_items()
        current = await group_roles(self.bot, guild.id)
        select = discord.ui.Select(
            placeholder="Change who can use a group...",
            options=[option(label, group, roles_plain(guild, current[group])) for group, label in GROUPS.items()],
            row=0,
        )
        self._watch(select, self._group_picked)
        return permissions_embed(guild, current)

    async def _group_picked(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        group = select.values[0] if select.values else ""
        if group not in GROUPS:
            await self.show(interaction, "That group no longer exists.")
            return
        current = await self.bot.perms.roles_for(guild.id, group)
        await interaction.response.send_modal(PermissionModal(self, guild, group, current))


class SetupWizard(AdminPanel):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int):
        super().__init__(bot, guild_id, owner_id)
        self.step = 0
        self.pending_access: list[int] | None = None
        self.board_kind: str | None = None
        self.board_channel_id: int | None = None

    @property
    def finished(self) -> bool:
        return self.step >= len(SETUP_STEPS)

    async def render(self, guild: discord.Guild) -> discord.Embed:
        self.clear_items()
        if self.finished:
            self._button("Back", discord.ButtonStyle.secondary, self._back, row=4)
            self._button("Done", discord.ButtonStyle.success, self._done, row=4)
            return await summary_embed(self.bot, guild)
        key, title = SETUP_STEPS[self.step]
        builders = {
            "admin": self._step_admin,
            "logistics": self._step_logistics,
            "alerts": self._step_alerts,
            "faction": self._step_faction,
            "tickets": self._step_tickets,
            "access": self._step_access,
            "boards": self._step_boards,
        }
        description = await builders[key](guild)
        embed = discord.Embed(
            title=f"Server Setup - Step {self.step + 1} of {len(SETUP_STEPS)}: {title}",
            description=description,
            colour=Colour.INFO,
        )
        embed.set_footer(text="Changes are saved as soon as you make them. Use Back and Next to move between steps.")
        last = self.step == len(SETUP_STEPS) - 1
        self._button("Back", discord.ButtonStyle.secondary, self._back, row=4, disabled=self.step == 0)
        self._button("Finish" if last else "Next", discord.ButtonStyle.primary, self._next, row=4)
        return embed

    async def _step_admin(self, guild: discord.Guild) -> str:
        current = await self.bot.settings.get(guild.id, keys.ADMIN_ROLE)
        select = discord.ui.RoleSelect(
            placeholder="Pick the admin role...",
            min_values=0,
            max_values=1,
            default_values=role_defaults(guild, [current] if current else []),
            row=0,
        )
        self._watch(select, self._admin_picked)
        return (
            "Members with this role can use `/settings`, `/permissions`, `/setup` and every admin button.\n"
            "The server owner and members with the Administrator permission are always admins.\n\n"
            f"Current: {role_text(guild, current)}"
        )

    async def _step_logistics(self, guild: discord.Guild) -> str:
        current = await self.bot.settings.get(guild.id, keys.LOGISTICS_ROLE)
        select = discord.ui.RoleSelect(
            placeholder="Pick the logistics ping role...",
            min_values=0,
            max_values=1,
            default_values=role_defaults(guild, [current] if current else []),
            row=0,
        )
        self._watch(select, self._logistics_picked)
        return (
            "This role is pinged in the alerts channel when a stockpile, ship or base timer runs low.\n\n"
            f"Current: {role_text(guild, current)}"
        )

    async def _step_alerts(self, guild: discord.Guild) -> str:
        current = await self.bot.settings.get(guild.id, keys.ALERT_CHANNEL)
        select = discord.ui.ChannelSelect(
            placeholder="Pick the alerts channel...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=0,
            max_values=1,
            default_values=channel_defaults(current),
            row=0,
        )
        self._watch(select, self._alerts_picked)
        return (
            "Timer reminders are posted here. Without one, each reminder goes to the channel of its board.\n\n"
            f"Current: {channel_text(current)}"
        )

    async def _step_faction(self, guild: discord.Guild) -> str:
        current = faction_label(await self.bot.settings.get(guild.id, keys.FACTION))
        select = discord.ui.Select(
            placeholder="Pick your faction...",
            options=[option(label, label, default=label == current) for label in [*FACTIONS, NO_FACTION]],
            row=0,
        )
        self._watch(select, self._faction_picked)
        return (
            "Used to tell apart items that share a name in inventory imports and orders. Switch it every war in `/settings`.\n\n"
            f"Current: {current}"
        )

    async def _step_tickets(self, guild: discord.Guild) -> str:
        current = await self.bot.perms.roles_for(guild.id, "tickets")
        select = discord.ui.RoleSelect(
            placeholder="Pick the ticket staff roles...",
            min_values=0,
            max_values=25,
            default_values=role_defaults(guild, current),
            row=0,
        )
        self._watch(select, self._tickets_picked)
        return (
            "Ticket staff see every ticket channel and can set status, assign tickets and log payments.\n\n"
            f"Current: {roles_text(guild, current)}"
        )

    async def _step_access(self, guild: discord.Guild) -> str:
        current = await group_roles(self.bot, guild.id, ACCESS_GROUPS)
        sets = list(current.values())
        same = all(role_ids == sets[0] for role_ids in sets)
        if self.pending_access is not None:
            shown = self.pending_access
        else:
            shown = sorted(sets[0]) if same else []
        select = discord.ui.RoleSelect(
            placeholder="Pick the roles for all six groups...",
            min_values=0,
            max_values=25,
            default_values=role_defaults(guild, shown),
            row=0,
        )
        self._watch(select, self._access_picked)
        self._button("Apply to All", discord.ButtonStyle.success, self._apply_access, row=1)
        self._button("Allow Everyone", discord.ButtonStyle.secondary, self._allow_everyone, row=1)
        names = ", ".join(GROUPS[group] for group in ACCESS_GROUPS)
        lines = [f"**{GROUPS[group]}**: {roles_text(guild, current[group])}" for group in ACCESS_GROUPS]
        pending = ""
        if self.pending_access is not None:
            pending = f"\n\nSelected, not saved yet: {roles_text(guild, set(self.pending_access), 'no roles (admins only)')}"
        return clip(
            f"Who may use {names}?\n"
            "Pick roles and press **Apply to All**. This replaces the roles of all six groups. "
            "**Allow Everyone** opens them to every member. Press **Next** to keep the current access. "
            "Fine-tune single groups later with `/permissions`.\n\n" + "\n".join(lines) + pending,
            4000,
        )

    async def _step_boards(self, guild: discord.Guild) -> str:
        providers = self.bot.boards.providers
        if not providers:
            return "No boards are available."
        if self.board_kind not in providers:
            self.board_kind = None
        options = []
        for kind, provider in sorted(providers.items(), key=lambda item: item[1].title.lower())[:25]:
            record = await self.bot.boards.record(guild.id, kind)
            channel = guild.get_channel(record["channel_id"]) if record else None
            if record and channel is not None:
                where = f"Posted in #{channel.name}"
            else:
                where = "Posted" if record else "Not posted yet"
            options.append(option(provider.title, kind, where, default=kind == self.board_kind))
        board_select = discord.ui.Select(placeholder="1. Pick a board...", options=options, row=0)
        self._watch(board_select, self._board_picked)
        channel_select = discord.ui.ChannelSelect(
            placeholder="2. Pick the channel for it...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            default_values=channel_defaults(self.board_channel_id),
            row=1,
        )
        self._watch(channel_select, self._board_channel_picked)
        self._button("Post Board", discord.ButtonStyle.success, self._post_board, row=2)
        lines = await boards_lines(self.bot, guild)
        return clip(
            "Boards are live messages that update by themselves and carry the buttons people use. "
            "Pick a board and a channel, then press **Post Board**. Posting a board again moves it.\n\n" + "\n".join(lines),
            4000,
        )

    async def _admin_picked(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        role = select.values[0] if select.values else None
        if role is not None and role.id == guild.id:
            await self.show(interaction, "@everyone cannot be the admin role.")
            return
        await store_optional(self.bot, guild.id, keys.ADMIN_ROLE, role.id if role else None)
        notice = f"Admin role set to {role.mention}." if role else "Admin role cleared."
        lost = await lost_admin_warning(self.bot, interaction)
        await self.show(interaction, f"{notice}\n{lost}" if lost else notice)

    async def _logistics_picked(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        role = select.values[0] if select.values else None
        if role is not None and role.id == guild.id:
            await self.show(interaction, "@everyone cannot be the logistics ping role.")
            return
        await store_optional(self.bot, guild.id, keys.LOGISTICS_ROLE, role.id if role else None)
        await self.show(interaction, f"Logistics ping role set to {role.mention}." if role else "Logistics ping role cleared.")

    async def _alerts_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        channel_id = select.values[0].id if select.values else None
        await store_optional(self.bot, guild.id, keys.ALERT_CHANNEL, channel_id)
        notice = f"Alerts channel set to <#{channel_id}>." if channel_id else "Alerts channel cleared."
        warning = channel_warning(guild, channel_id, SEND_PERMISSIONS, "post reminders")
        await self.show(interaction, f"{notice}\n{warning}" if warning else notice)

    async def _faction_picked(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        label = select.values[0] if select.values else NO_FACTION
        await store_optional(self.bot, guild.id, keys.FACTION, FACTIONS.get(label))
        await self.show(interaction, f"Faction set to {faction_label(FACTIONS.get(label))}.")

    async def _tickets_picked(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        current = await self.bot.perms.roles_for(guild.id, "tickets")
        role_ids = {role.id for role in select.values}
        if guild.id in current:
            role_ids.add(guild.id)
        await self.bot.perms.set_roles(guild.id, "tickets", role_ids)
        await self.show(interaction, f"Ticket staff: {roles_text(guild, role_ids)}.")

    async def _access_picked(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        if await self.guard(interaction) is None:
            return
        self.pending_access = sorted({role.id for role in select.values})
        await self.show(interaction, "Press **Apply to All** to save this selection.")

    async def _apply_access(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        if self.pending_access is None:
            await self.show(interaction, "Pick roles first, or press **Next** to keep the current access.")
            return
        role_ids = set(self.pending_access)
        for group in ACCESS_GROUPS:
            await self.bot.perms.set_roles(guild.id, group, role_ids)
        self.pending_access = None
        await self.show(interaction, f"Saved for all six groups: {roles_text(guild, role_ids, 'admins only')}.")

    async def _allow_everyone(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        for group in ACCESS_GROUPS:
            await self.bot.perms.set_roles(guild.id, group, {guild.id})
        self.pending_access = None
        await self.show(interaction, "Every member can now use all six groups.")

    async def _board_picked(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if await self.guard(interaction) is None:
            return
        self.board_kind = select.values[0] if select.values else None
        await self.show(interaction)

    async def _board_channel_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        if await self.guard(interaction) is None:
            return
        self.board_channel_id = select.values[0].id if select.values else None
        await self.show(interaction)

    async def _post_board(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        provider = self.bot.boards.providers.get(self.board_kind or "")
        if provider is None:
            await self.show(interaction, "Pick a board first.")
            return
        if not self.board_channel_id:
            await self.show(interaction, "Pick a channel first.")
            return
        channel = guild.get_channel(self.board_channel_id)
        if channel is None:
            await self.show(interaction, f"I cannot see <#{self.board_channel_id}>. Give me the View Channel permission there.")
            return
        missing = missing_permissions(channel, guild.me, SEND_PERMISSIONS)
        if missing:
            await self.show(interaction, f"I cannot post in {channel.mention}: I am missing {', '.join(missing)} there.")
            return
        await interaction.response.defer()
        try:
            await self.bot.boards.post(guild, provider.kind, channel)
        except discord.Forbidden:
            notice = f"Discord refused to let me post in {channel.mention}. Check my permissions there (View Channel, Send Messages, Embed Links)."
        except discord.HTTPException as error:
            notice = f"Could not post the {md(provider.title)} board: {error.text or error}"
        else:
            notice = f"**{md(provider.title)}** board posted in {channel.mention}."
        await self.show(interaction, notice)

    async def _back(self, interaction: discord.Interaction) -> None:
        if await self.guard(interaction) is None:
            return
        self.step = max(0, self.step - 1)
        await self.show(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        if await self.guard(interaction) is None:
            return
        self.step = min(len(SETUP_STEPS), self.step + 1)
        await self.show(interaction)

    async def _done(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        self.stop()
        await edit(
            interaction,
            content="Setup finished. Run `/setup` again at any time.",
            embed=await summary_embed(self.bot, guild),
            view=None,
        )


class Admin(commands.Cog):
    def __init__(self, bot: FoxBot):
        self.bot = bot

    @app_commands.command(name="settings", description="Admin: view and change this server's bot settings.")
    @app_commands.guild_only()
    async def settings_command(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await SettingsPanel(self.bot, interaction.guild_id, interaction.user.id).send(interaction)

    @app_commands.command(name="permissions", description="Admin: choose which roles can use each part of the bot.")
    @app_commands.guild_only()
    async def permissions_command(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await PermissionsPanel(self.bot, interaction.guild_id, interaction.user.id).send(interaction)

    @app_commands.command(name="setup", description="Admin: guided setup of roles, alerts, faction, access and boards.")
    @app_commands.guild_only()
    async def setup_command(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await SetupWizard(self.bot, interaction.guild_id, interaction.user.id).send(interaction)


async def setup(bot: FoxBot) -> None:
    await bot.add_cog(Admin(bot))
