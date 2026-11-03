from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
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
TICKET_BOARD = "tickets"
RARES_BOARD = "rares"
MIN_TIMER_HOURS = 1
MAX_TIMER_HOURS = 500
SERVICE_NAME_MAX_LENGTH = 60
MAX_SERVICE_COST = 1_000_000

FACTIONS = {"Warden": "warden", "Colonial": "colonial"}
NO_FACTION = "Not set"

TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
CATEGORY_TYPES = [discord.ChannelType.category]
VOICE_CHANNEL_TYPES = [discord.ChannelType.voice]

SEND_PERMISSIONS = {"view_channel": "View Channel", "send_messages": "Send Messages", "embed_links": "Embed Links"}
RENAME_PERMISSIONS = {"view_channel": "View Channel", "manage_channels": "Manage Channels"}

ACCESS_GROUPS = ["stockpile", "ship", "msupps", "orders", "inventory", "rares"]
ALL_ACCESS_GROUPS = f"all {len(ACCESS_GROUPS)} groups"

SECTIONS = {
    "overview": ("Overview", "Everything at a glance and what is still to set up"),
    "roles": ("Roles", "Admin role, logistics ping role, ticket staff"),
    "channels": ("Channels", "Alerts, orders and war alert channels"),
    "permissions": ("Permissions", "Which roles can use each part of the bot"),
    "boards": ("Boards", "Post, move, remove or refresh the live boards"),
    "war": ("Faction & War", "Faction and town change alerts"),
    "timers": ("Timers", "Stockpile and ship timers, reminder thresholds"),
    "lists": ("Stockpiles & Ships", "Storage types and ship types"),
    "tickets": ("Tickets", "Services and prices, ticket category and log channel"),
    "rares": ("Rares", "Log and voice channels, status levels"),
    "data": ("Data & Backups", "Back up now, archive the previous war"),
}

BOARDS_HELP = (
    "Boards are live messages that update by themselves and carry the buttons people use. "
    "Pick a board and a channel, then press **Post Board**. Posting a board again moves it."
)

WAR_ALERT_MODES = {"Off": "off", "Our faction only": "ours", "All changes": "all"}
FACTION_TEAM_NAMES = {"warden": "Wardens", "colonial": "Colonials"}

SETUP_STEPS = [
    ("admin", "Admin role"),
    ("logistics", "Logistics ping role"),
    ("alerts", "Alerts channel"),
    ("faction", "Faction"),
    ("tickets", "Ticket staff and pings"),
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
        raise ValueError("The cost must be a whole number of Rares, 0 or more.")
    value = int(cleaned)
    if value > MAX_SERVICE_COST:
        raise ValueError(f"The cost can be at most {MAX_SERVICE_COST:,} Rares.")
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
    bot.boards.request_refresh(guild_id, TICKET_BOARD)
    return existing is not None


async def remove_service(bot: FoxBot, guild_id: int, name: str) -> bool:
    removed = bool(await bot.db.execute("DELETE FROM ticket_services WHERE guild_id = ? AND name = ?", (guild_id, name)))
    bot.boards.request_refresh(guild_id, TICKET_BOARD)
    return removed


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
        log.exception("Could not update the Rares voice channel in guild %s", guild_id)


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


def ping_defaults(guild: discord.Guild, stored: dict) -> list[discord.SelectDefaultValue]:
    values = [
        discord.SelectDefaultValue(id=int(rid), type=discord.SelectDefaultValueType.role)
        for rid in stored.get("roles", [])
        if guild.get_role(int(rid)) is not None
    ]
    values += [discord.SelectDefaultValue(id=int(uid), type=discord.SelectDefaultValueType.user) for uid in stored.get("users", [])]
    return values[:25]


def pings_text(stored: dict, empty: str = "Nobody") -> str:
    mentions = [f"<@&{rid}>" for rid in stored.get("roles", [])] + [f"<@{uid}>" for uid in stored.get("users", [])]
    return clip(", ".join(mentions), EMBED_FIELD_LIMIT) if mentions else empty


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
    return [f"- **{md(row['name'])}**: {row['cost']:,} Rares" for row in services]


def war_mode_label(value: Any) -> str:
    lookup = {stored: label for label, stored in WAR_ALERT_MODES.items()}
    return lookup.get(str(value).lower(), "Off") if value else "Off"


def war_mode_text(mode_label: str, faction: Any) -> str:
    team = FACTION_TEAM_NAMES.get(str(faction).lower()) if faction else None
    if mode_label == "All changes":
        return "**All changes**: every town that changes hands, on any side."
    if mode_label == "Our faction only":
        if team is None:
            return "**Our faction only**, but the faction is not set, so these alerts are off. Press your faction's button above."
        return f"**Our faction only**: towns the {team} lost or took."
    return "**Off**: no town change alerts are posted."


def permissions_embed(guild: discord.Guild, current: dict[str, set[int]]) -> discord.Embed:
    embed = discord.Embed(
        title="Settings - Permissions",
        description=(
            "Choose which roles may use each part of the bot. Pick a group below to change it.\n\n"
            "Admins always pass every check: the server owner, members with the Administrator permission "
            "and members with the admin role from General.\n"
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


async def boards_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    lines = await boards_lines(bot, guild)
    return discord.Embed(
        title="Settings - Boards",
        description=clip(BOARDS_HELP + "\n\n" + ("\n".join(lines) or "No boards are available."), 4000),
        colour=Colour.INFO,
    )


async def summary_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Server Setup - Summary",
        description="Setup is done. Change anything later with `/settings`, or run `/setup` again.",
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


async def lost_admin_warning(bot: FoxBot, interaction: discord.Interaction) -> str | None:
    member = await interaction_member(interaction)
    if member is not None and not await bot.perms.is_admin(member):
        return "You are no longer a bot admin, so further changes will be refused."
    return None


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
            "Cost in Rares",
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
        await self.panel.show(interaction, f"{verb} **{md(name)}**: {cost:,} Rares.")


class AdminPanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.board_kind: str | None = None
        self.board_channel_id: int | None = None

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

    async def _add_board_controls(self, guild: discord.Guild, row: int) -> None:
        providers = self.bot.boards.providers
        if not providers:
            return
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
        board_select = discord.ui.Select(placeholder="1. Pick a board...", options=options, row=row)
        self._watch(board_select, self._board_picked)
        channel_select = discord.ui.ChannelSelect(
            placeholder="2. Pick the channel for it...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            default_values=channel_defaults(self.board_channel_id),
            row=row + 1,
        )
        self._watch(channel_select, self._board_channel_picked)
        self._button("Post Board", discord.ButtonStyle.success, self._post_board, row=row + 2)

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

    def _add_permission_select(self, guild: discord.Guild, current: dict[str, set[int]], row: int) -> None:
        select = discord.ui.Select(
            placeholder="Change who can use a group...",
            options=[option(label, group, roles_plain(guild, current[group])) for group, label in GROUPS.items()],
            row=row,
        )
        self._watch(select, self._group_picked)

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
        await self._store_faction(interaction, select.values[0] if select.values else NO_FACTION)

    async def _store_faction(self, interaction: discord.Interaction, label: str) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
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

    def _add_ping_select(self, guild: discord.Guild, stored: dict, row: int) -> None:
        select = discord.ui.MentionableSelect(
            placeholder="Who gets pinged for new tickets...",
            min_values=0,
            max_values=25,
            default_values=ping_defaults(guild, stored),
            row=row,
        )
        self._watch(select, self._ticket_pings_picked)

    async def _ticket_pings_picked(self, interaction: discord.Interaction, select: discord.ui.MentionableSelect) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        roles = [value.id for value in select.values if isinstance(value, discord.Role) and value.id != guild.id]
        users = [value.id for value in select.values if not isinstance(value, discord.Role)]
        stored = {"roles": roles, "users": users}
        if roles or users:
            await self.bot.settings.set(guild.id, keys.TICKET_PINGS, stored)
        else:
            await self.bot.settings.reset(guild.id, keys.TICKET_PINGS)
        notice = f"New tickets ping: {pings_text(stored)}."
        if any(isinstance(value, discord.Role) and value.id == guild.id for value in select.values):
            notice += " @everyone is never pinged, so it was left out."
        if roles or users:
            notice += " They can also see every new ticket channel."
        await self.show(interaction, notice)

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


def parse_hours(text: str, label: str) -> float:
    cleaned = text.strip().lower().removesuffix("h").strip()
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"{label} must be a number of hours.") from None
    if not math.isfinite(value) or value < MIN_TIMER_HOURS or value > MAX_TIMER_HOURS:
        raise ValueError(f"{label} must be between {MIN_TIMER_HOURS} and {MAX_TIMER_HOURS} hours.")
    return round(value, 2)


def parse_levels(low_text: str, moderate_text: str) -> list[int]:
    values = []
    for text, label in ((low_text, "Low"), (moderate_text, "Moderate")):
        cleaned = re.sub(r"[\s,_']", "", text)
        if not re.fullmatch(r"[0-9]+", cleaned):
            raise ValueError(f"{label} must be a whole number of Rares.")
        values.append(int(cleaned))
    low, moderate = values
    if low < 1:
        raise ValueError("Low must be at least 1.")
    if moderate <= low:
        raise ValueError("Moderate must be higher than Low.")
    if moderate > MAX_SERVICE_COST:
        raise ValueError(f"Moderate can be at most {MAX_SERVICE_COST:,}.")
    return [low, moderate]


def hours_text(value: Any) -> str:
    return timeutil.format_hours(float(value))


def section_footer(key: str) -> str:
    keys_in_order = list(SECTIONS)
    return f"Section {keys_in_order.index(key) + 1} of {len(keys_in_order)}. Pick another one in the menu at the top."


def backup_files(bot: FoxBot, limit: int = 5) -> list[Path]:
    directory = bot.backups.directory
    if not directory.exists():
        return []
    files = [path for path in directory.glob("*.db") if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def backup_line(path: Path) -> str:
    stat = path.stat()
    size = stat.st_size / (1024 * 1024)
    return f"`{path.name}` - {size:.1f} MB - {timeutil.relative(int(stat.st_mtime))}"


async def setup_todo(bot: FoxBot, guild: discord.Guild) -> list[str]:
    todo = []
    if not await bot.settings.get(guild.id, keys.ADMIN_ROLE):
        todo.append("No admin role (Roles). Only the owner and Administrators can change settings.")
    if not await bot.settings.get(guild.id, keys.FACTION):
        todo.append("Faction not set (Faction & War).")
    if not await bot.settings.get(guild.id, keys.ALERT_CHANNEL):
        todo.append("No alerts channel (Channels). Reminders nobody can get by DM go to the board's channel.")
    closed = [GROUPS[group] for group in ACCESS_GROUPS if not await bot.perms.roles_for(guild.id, group)]
    if closed:
        todo.append(f"Only admins can use: {', '.join(closed)} (Permissions).")
    if not await list_services(bot, guild.id):
        todo.append("No ticket services, so customers cannot order anything (Tickets).")
    unposted = [provider.title for kind, provider in bot.boards.providers.items() if not await bot.boards.record(guild.id, kind)]
    if unposted:
        todo.append(f"Boards not posted: {', '.join(sorted(unposted))} (Boards).")
    return todo


async def overview_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    s = bot.settings
    embed = discord.Embed(
        title="Settings - Overview",
        description="Every setting of this bot, one section at a time. Pick a section in the menu at the top to change it.",
        colour=Colour.INFO,
    )
    embed.add_field(
        name="Roles",
        value=(
            f"Admin: {role_text(guild, await s.get(guild.id, keys.ADMIN_ROLE))}\n"
            f"Logistics ping: {role_text(guild, await s.get(guild.id, keys.LOGISTICS_ROLE))}\n"
            f"Ticket staff: {roles_text(guild, await bot.perms.roles_for(guild.id, 'tickets'), 'Admins only')}\n"
            f"Pinged for new tickets: {pings_text(await s.get(guild.id, keys.TICKET_PINGS))}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Channels",
        value=(
            f"Alerts: {channel_text(await s.get(guild.id, keys.ALERT_CHANNEL))}\n"
            f"Orders: {channel_text(await s.get(guild.id, keys.ORDERS_CHANNEL))}\n"
            f"War alerts: {channel_text(await s.get(guild.id, keys.WAR_ALERT_CHANNEL))}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Faction & War",
        value=(
            f"Faction: {faction_label(await s.get(guild.id, keys.FACTION))}\n"
            f"Town change alerts: {war_mode_label(await s.get(guild.id, keys.WAR_ALERT_MODE))}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Timers",
        value=(
            f"Stockpiles: {hours_text(await s.get(guild.id, keys.STOCKPILE_HOURS))}\n"
            f"Ships: {hours_text(await s.get(guild.id, keys.SHIP_HOURS))}\n"
            f"Reminders: {thresholds_text(await s.alert_thresholds(guild.id))}"
        ),
        inline=True,
    )
    todo = await setup_todo(bot, guild)
    embed.add_field(
        name="Still to do" if todo else "Setup",
        value=clip("\n".join(f"- {line}" for line in todo), EMBED_FIELD_LIMIT) if todo else "Everything important is set up.",
        inline=False,
    )
    return embed


async def roles_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Settings - Roles",
        description="Pick a role in a menu to save it straight away. Remove every role from a menu to clear it.",
        colour=Colour.INFO,
    )
    embed.add_field(
        name="Admin role",
        value=(
            role_text(guild, await bot.settings.get(guild.id, keys.ADMIN_ROLE), "Not set")
            + "\nCan use `/settings`, `/setup` and every admin button. The server owner and members with the "
            "Administrator permission are always admins."
        ),
        inline=False,
    )
    embed.add_field(
        name="Logistics ping role",
        value=(
            role_text(guild, await bot.settings.get(guild.id, keys.LOGISTICS_ROLE), "Not set")
            + "\nPinged when a timer reminder could not be sent to anyone by DM."
        ),
        inline=False,
    )
    embed.add_field(
        name="Ticket staff",
        value=(
            roles_text(guild, await bot.perms.roles_for(guild.id, "tickets"))
            + "\nSee every ticket channel and can set status, assign tickets and log payments."
        ),
        inline=False,
    )
    embed.add_field(
        name="Pinged for new tickets",
        value=(
            pings_text(await bot.settings.get(guild.id, keys.TICKET_PINGS), "Nobody")
            + "\nRoles or members mentioned in every new ticket channel. They can see those channels too. "
            "Nobody is pinged until you pick someone."
        ),
        inline=False,
    )
    return embed


async def channels_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    alerts = await bot.settings.get(guild.id, keys.ALERT_CHANNEL)
    orders_channel = await bot.settings.get(guild.id, keys.ORDERS_CHANNEL)
    war_channel = await bot.settings.get(guild.id, keys.WAR_ALERT_CHANNEL)
    embed = discord.Embed(
        title="Settings - Channels",
        description=(
            "Pick a channel in a menu to save it straight away. Remove it from a menu to clear it. "
            "Ticket, Rares and board channels are in their own sections."
        ),
        colour=Colour.INFO,
    )
    rows = [
        ("Alerts channel", alerts, "Not set. A reminder nobody can get by DM goes to its board's channel.", "post reminders"),
        ("Orders channel", orders_channel, "Not set. Order cards are posted where each order is created.", "post order cards"),
        ("War alert channel", war_channel, "Not set. War posts go to the alerts channel, or the war board's channel.", "post war alerts"),
    ]
    for name, channel_id, empty, purpose in rows:
        value = channel_text(channel_id, empty)
        warning = channel_warning(guild, channel_id, SEND_PERMISSIONS, purpose)
        embed.add_field(name=name, value=f"{value}\n{warning}" if warning else value, inline=False)
    return embed


async def war_section_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    faction = await bot.settings.get(guild.id, keys.FACTION)
    mode = war_mode_label(await bot.settings.get(guild.id, keys.WAR_ALERT_MODE))
    embed = discord.Embed(
        title="Settings - Faction & War",
        description=(
            "Press your faction's button at the start of every war. Town change alerts come from the official "
            "War API, are checked every 5 minutes and never ping anyone."
        ),
        colour=Colour.INFO,
    )
    embed.add_field(
        name="Faction",
        value=f"{faction_label(faction)}\nUsed for items that share a name and for our-faction war alerts.",
        inline=False,
    )
    embed.add_field(name="Town change alerts", value=war_mode_text(mode, faction), inline=False)
    embed.add_field(
        name="War alert channel",
        value=channel_text(await bot.settings.get(guild.id, keys.WAR_ALERT_CHANNEL), "Not set (change it under Channels)."),
        inline=False,
    )
    embed.add_field(name="New war", value="Archive the previous war's data under Data & Backups.", inline=False)
    return embed


async def timers_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Settings - Timers",
        description="Press **Edit Timers** to change these. New lengths apply the next time a timer is started or refreshed.",
        colour=Colour.INFO,
    )
    embed.add_field(
        name="Stockpile reserve timer",
        value=f"{hours_text(await bot.settings.get(guild.id, keys.STOCKPILE_HOURS))} (default {constants.STOCKPILE_LIFETIME_HOURS} hours)",
        inline=False,
    )
    embed.add_field(
        name="Ship squad-lock timer",
        value=f"{hours_text(await bot.settings.get(guild.id, keys.SHIP_HOURS))} (default {constants.SHIP_LIFETIME_HOURS} hours)",
        inline=False,
    )
    embed.add_field(
        name="Reminders",
        value=f"{thresholds_text(await bot.settings.alert_thresholds(guild.id))} before a timer runs out, plus one notice when it expires.",
        inline=False,
    )
    return embed


async def lists_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Settings - Stockpiles & Ships",
        description="The choices offered when adding or editing a stockpile or a ship. Pick one in a menu to remove it.",
        colour=Colour.INFO,
    )
    for section in LIST_SECTIONS.values():
        values = list(await bot.settings.get(guild.id, section.setting))
        lines = "\n".join(f"- {md(value)}" for value in values) or "None"
        embed.add_field(
            name=f"{section.title} ({len(values)} of {MAX_LIST_VALUES})",
            value=clip(lines, EMBED_FIELD_LIMIT),
            inline=True,
        )
    embed.set_footer(text="At least one of each must remain. Entries that use a removed value keep it.")
    return embed


async def tickets_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    services = await list_services(bot, guild.id)
    category_id = await bot.settings.get(guild.id, keys.TICKET_CATEGORY)
    log_channel = await bot.settings.get(guild.id, keys.TICKET_LOG_CHANNEL)
    lines = services_lines(services) or ["No services yet. Customers cannot order anything until you add one."]
    embed = discord.Embed(
        title="Settings - Tickets",
        description=clip(
            f"Services customers can order, with their price in Rares ({len(services)} of {MAX_SERVICES}). "
            "Pick one in the first menu to remove it. Prices are saved on each ticket when ordered.\n\n" + "\n".join(lines),
            4000,
        ),
        colour=Colour.INFO,
    )
    category = guild.get_channel(category_id) if category_id else None
    if category is not None:
        category_value = f"**{md(category.name)}**"
    elif category_id:
        category_value = "Missing. A new one is created with the next ticket."
    else:
        category_value = "Not set. One is created automatically with the first ticket."
    embed.add_field(
        name="Pinged for new tickets",
        value=pings_text(await bot.settings.get(guild.id, keys.TICKET_PINGS), "Nobody") + " (change it under Roles)",
        inline=False,
    )
    embed.add_field(name="Ticket category", value=category_value, inline=False)
    embed.add_field(
        name="Ticket log channel",
        value=channel_text(log_channel, "Not set. One is created automatically when it is first needed."),
        inline=False,
    )
    return embed


async def rares_section_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    log_channel = await bot.settings.get(guild.id, keys.RARES_LOG_CHANNEL)
    voice_channel = await bot.settings.get(guild.id, keys.RARES_VOICE_CHANNEL)
    low, moderate = await bot.settings.get(guild.id, keys.RARES_LEVELS)
    embed = discord.Embed(title="Settings - Rares", description="The regiment's Rares stock.", colour=Colour.INFO)
    log_value = channel_text(log_channel, "Not set. Changes are only kept in the log.")
    warning = channel_warning(guild, log_channel, SEND_PERMISSIONS, "post the Rares log")
    embed.add_field(name="Log channel", value=f"{log_value}\n{warning}" if warning else log_value, inline=False)
    voice_value = channel_text(voice_channel, "Not set. No channel shows the live count.")
    warning = channel_warning(guild, voice_channel, RENAME_PERMISSIONS, "rename the voice channel")
    embed.add_field(name="Voice channel", value=f"{voice_value}\n{warning}" if warning else voice_value, inline=False)
    embed.add_field(
        name="Status levels",
        value=f"Empty at 0, Low up to {low:,}, Moderate up to {moderate:,}, Stocked above that.",
        inline=False,
    )
    embed.set_footer(text="Discord allows only 2 channel renames per 10 minutes, so the voice channel name can lag behind.")
    return embed


async def data_embed(bot: FoxBot, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Settings - Data & Backups",
        description=(
            f"The database is backed up every day and the last {BACKUPS_KEPT} daily copies are kept on the bot's host. "
            "**Back Up Now** makes one immediately.\n\n"
            "**Archive Previous War** backs up the database, then deletes this server's stockpiles, ships, msupps "
            "bases, inventories and orders. Settings, permissions, boards, Rares and tickets are kept. "
            "It asks for confirmation first."
        ),
        colour=Colour.INFO,
    )
    files = backup_files(bot)
    embed.add_field(
        name="Latest backups",
        value=clip("\n".join(backup_line(path) for path in files), EMBED_FIELD_LIMIT) if files else "None yet.",
        inline=False,
    )
    archive = await bot.db.fetchone(
        "SELECT war_number, archived_by, archived_at FROM war_archives WHERE guild_id = ? ORDER BY archived_at DESC LIMIT 1",
        (guild.id,),
    )
    if archive is not None:
        war = f"War {archive['war_number']}" if archive["war_number"] else "A war"
        value = f"{war}, by <@{archive['archived_by']}> {timeutil.relative(archive['archived_at'])}"
    else:
        value = "Never."
    embed.add_field(name="Last archive", value=value, inline=False)
    return embed


class TimersModal(BaseModal, title="Timers"):
    def __init__(self, panel: SettingsPanel, *, stockpile_hours: Any, ship_hours: Any, thresholds: list[float]):
        super().__init__()
        self.panel = panel
        self.stockpile_field = text_field(
            "Stockpile reserve timer (hours)",
            default=f"{float(stockpile_hours):g}",
            placeholder=str(constants.STOCKPILE_LIFETIME_HOURS),
            description=f"Between {MIN_TIMER_HOURS} and {MAX_TIMER_HOURS}. The game's default is {constants.STOCKPILE_LIFETIME_HOURS}.",
            max_length=6,
        )
        self.ship_field = text_field(
            "Ship squad-lock timer (hours)",
            default=f"{float(ship_hours):g}",
            placeholder=str(constants.SHIP_LIFETIME_HOURS),
            description=f"Between {MIN_TIMER_HOURS} and {MAX_TIMER_HOURS}. The game's default is {constants.SHIP_LIFETIME_HOURS}.",
            max_length=6,
        )
        self.thresholds_field = text_field(
            "Reminder thresholds (hours)",
            default=thresholds_input(thresholds),
            placeholder="12, 6, 2, 1",
            description=f"Up to {MAX_THRESHOLDS} positive numbers, separated by commas. Empty restores 12, 6, 2, 1.",
            required=False,
            max_length=100,
        )
        for item in (self.stockpile_field, self.ship_field, self.thresholds_field):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        bot = self.panel.bot
        problems = []
        for field, setting, label in (
            (self.stockpile_field, keys.STOCKPILE_HOURS, "The stockpile timer"),
            (self.ship_field, keys.SHIP_HOURS, "The ship timer"),
        ):
            try:
                await bot.settings.set(guild.id, setting, parse_hours(text_value(field), label))
            except ValueError as error:
                problems.append(f"{error} It was not changed.")
        threshold_problem = await update_thresholds(bot, guild.id, text_value(self.thresholds_field))
        if threshold_problem:
            problems.append(threshold_problem)
        await self.panel.show(interaction, "\n".join(["Timers saved.", *problems]))


class RaresLevelsModal(BaseModal, title="Rares Status Levels"):
    def __init__(self, panel: SettingsPanel, levels: list[int]):
        super().__init__()
        self.panel = panel
        self.low_field = text_field(
            "Low up to",
            default=str(levels[0]),
            description="The status shows Low from 1 up to this count.",
            max_length=9,
        )
        self.moderate_field = text_field(
            "Moderate up to",
            default=str(levels[1]),
            description="Moderate above Low up to this count, Stocked above it.",
            max_length=9,
        )
        self.add_item(self.low_field)
        self.add_item(self.moderate_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = await self.panel.guard(interaction)
        if guild is None:
            return
        try:
            levels = parse_levels(text_value(self.low_field), text_value(self.moderate_field))
        except ValueError as error:
            await self.panel.show(interaction, f"Nothing was saved. {error}")
            return
        await self.panel.bot.settings.set(guild.id, keys.RARES_LEVELS, levels)
        self.panel.bot.boards.request_refresh(guild.id, RARES_BOARD)
        await self.panel.show(interaction, f"Status levels saved: Low up to {levels[0]:,}, Moderate up to {levels[1]:,}.")


class AllTrackersModal(BaseModal, title="Same Roles for All Trackers"):
    def __init__(self, panel: AdminPanel, guild: discord.Guild):
        super().__init__()
        self.panel = panel
        self.roles_field = role_select_label(
            "Roles allowed",
            f"Replaces the roles of {ALL_ACCESS_GROUPS}. Admins always can.",
            guild,
            [],
            max_values=25,
        )
        self.everyone_field = select_field(
            "Open to everyone",
            ["No", "Yes"],
            default="No",
            description="Yes lets every member use all of them.",
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
        for group in ACCESS_GROUPS:
            await self.panel.bot.perms.set_roles(guild.id, group, role_ids)
        await self.panel.show(interaction, f"Saved for {ALL_ACCESS_GROUPS}: {roles_text(guild, role_ids, 'admins only')}.")


class SettingsPanel(AdminPanel):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int, section: str = "overview"):
        super().__init__(bot, guild_id, owner_id)
        self.section = section if section in SECTIONS else "overview"

    async def render(self, guild: discord.Guild) -> discord.Embed:
        self.clear_items()
        sections = discord.ui.Select(
            placeholder="Choose a settings section...",
            options=[option(label, key, description, default=key == self.section) for key, (label, description) in SECTIONS.items()],
            row=0,
        )
        self._watch(sections, self._section_picked)
        builders = {
            "overview": self._render_overview,
            "roles": self._render_roles,
            "channels": self._render_channels,
            "permissions": self._render_permissions,
            "boards": self._render_boards,
            "war": self._render_war,
            "timers": self._render_timers,
            "lists": self._render_lists,
            "tickets": self._render_tickets,
            "rares": self._render_rares,
            "data": self._render_data,
        }
        embed = await builders[self.section](guild)
        footer = section_footer(self.section)
        if embed.footer and embed.footer.text:
            footer = f"{embed.footer.text}\n{footer}"
        embed.set_footer(text=footer)
        return embed

    def _role_select(self, guild: discord.Guild, placeholder: str, role_ids, handler: SelectHandler, row: int, *, max_values: int = 1) -> None:
        select = discord.ui.RoleSelect(
            placeholder=placeholder,
            min_values=0,
            max_values=max_values,
            default_values=role_defaults(guild, role_ids)[:max_values],
            row=row,
        )
        self._watch(select, handler)

    def _channel_select(self, placeholder: str, channel_id: int | None, channel_types, handler: SelectHandler, row: int) -> None:
        select = discord.ui.ChannelSelect(
            placeholder=placeholder,
            channel_types=channel_types,
            min_values=0,
            max_values=1,
            default_values=channel_defaults(channel_id),
            row=row,
        )
        self._watch(select, handler)

    async def _render_overview(self, guild: discord.Guild) -> discord.Embed:
        return await overview_embed(self.bot, guild)

    async def _render_roles(self, guild: discord.Guild) -> discord.Embed:
        admin_role = await self.bot.settings.get(guild.id, keys.ADMIN_ROLE)
        logistics_role = await self.bot.settings.get(guild.id, keys.LOGISTICS_ROLE)
        staff = await self.bot.perms.roles_for(guild.id, "tickets")
        self._role_select(guild, "Admin role...", [admin_role] if admin_role else [], self._admin_picked, 1)
        self._role_select(guild, "Logistics ping role...", [logistics_role] if logistics_role else [], self._logistics_picked, 2)
        self._role_select(guild, "Ticket staff roles...", staff, self._tickets_picked, 3, max_values=25)
        self._add_ping_select(guild, await self.bot.settings.get(guild.id, keys.TICKET_PINGS), 4)
        return await roles_embed(self.bot, guild)

    async def _render_channels(self, guild: discord.Guild) -> discord.Embed:
        s = self.bot.settings
        self._channel_select("Alerts channel...", await s.get(guild.id, keys.ALERT_CHANNEL), TEXT_CHANNEL_TYPES, self._alerts_picked, 1)
        self._channel_select("Orders channel...", await s.get(guild.id, keys.ORDERS_CHANNEL), TEXT_CHANNEL_TYPES, self._orders_channel_picked, 2)
        self._channel_select("War alert channel...", await s.get(guild.id, keys.WAR_ALERT_CHANNEL), TEXT_CHANNEL_TYPES, self._war_channel_picked, 3)
        return await channels_embed(self.bot, guild)

    async def _render_permissions(self, guild: discord.Guild) -> discord.Embed:
        current = await group_roles(self.bot, guild.id)
        self._add_permission_select(guild, current, 1)
        self._button("Same Roles for All Trackers", discord.ButtonStyle.primary, self._all_trackers, row=2)
        return permissions_embed(guild, current)

    async def _render_boards(self, guild: discord.Guild) -> discord.Embed:
        await self._add_board_controls(guild, 1)
        if self.bot.boards.providers:
            self._button("Remove Board", discord.ButtonStyle.danger, self._remove_board, row=3)
            self._button("Refresh All Boards", discord.ButtonStyle.secondary, self._refresh_boards, row=3)
        return await boards_embed(self.bot, guild)

    async def _render_war(self, guild: discord.Guild) -> discord.Embed:
        current = faction_label(await self.bot.settings.get(guild.id, keys.FACTION))
        for label in [*FACTIONS, NO_FACTION]:
            style = discord.ButtonStyle.success if label == current else discord.ButtonStyle.secondary
            self._button(label, style, lambda interaction, value=label: self._store_faction(interaction, value), row=1)
        mode = war_mode_label(await self.bot.settings.get(guild.id, keys.WAR_ALERT_MODE))
        select = discord.ui.Select(
            placeholder="Town change alerts...",
            options=[option(f"Town change alerts: {label}", label, default=label == mode) for label in WAR_ALERT_MODES],
            row=2,
        )
        self._watch(select, self._war_mode_picked)
        return await war_section_embed(self.bot, guild)

    async def _render_timers(self, guild: discord.Guild) -> discord.Embed:
        self._button("Edit Timers", discord.ButtonStyle.primary, self._edit_timers, row=1)
        self._button("Restore Defaults", discord.ButtonStyle.secondary, self._reset_timers, row=1)
        return await timers_embed(self.bot, guild)

    async def _render_lists(self, guild: discord.Guild) -> discord.Embed:
        for row, section in enumerate(LIST_SECTIONS.values(), start=1):
            values = list(await self.bot.settings.get(guild.id, section.setting))
            if values:
                remove = discord.ui.Select(
                    placeholder=f"Remove a {section.noun}...",
                    options=[option(value, value[:100]) for value in values[:25]],
                    row=row,
                )
                self._watch(remove, lambda interaction, select, key=section.key: self._remove_list_value(interaction, select, key))
        for section in LIST_SECTIONS.values():
            self._button(
                f"Add {section.noun.title()}",
                discord.ButtonStyle.success,
                lambda interaction, key=section.key: self._add_list_value(interaction, key),
                row=3,
            )
        for section in LIST_SECTIONS.values():
            self._button(
                f"Reset {section.title}",
                discord.ButtonStyle.secondary,
                lambda interaction, key=section.key: self._reset_list(interaction, key),
                row=4,
            )
        return await lists_embed(self.bot, guild)

    async def _render_tickets(self, guild: discord.Guild) -> discord.Embed:
        services = await list_services(self.bot, guild.id)
        if services:
            remove = discord.ui.Select(
                placeholder="Remove a service...",
                options=[option(row["name"], row["name"][:100], f"{row['cost']:,} Rares") for row in services[:25]],
                row=1,
            )
            self._watch(remove, self._remove_service)
        s = self.bot.settings
        self._channel_select("Ticket category...", await s.get(guild.id, keys.TICKET_CATEGORY), CATEGORY_TYPES, self._ticket_category_picked, 2)
        self._channel_select("Ticket log channel...", await s.get(guild.id, keys.TICKET_LOG_CHANNEL), TEXT_CHANNEL_TYPES, self._ticket_log_picked, 3)
        self._button("Add or Update Service", discord.ButtonStyle.success, self._add_service, row=4)
        return await tickets_embed(self.bot, guild)

    async def _render_rares(self, guild: discord.Guild) -> discord.Embed:
        s = self.bot.settings
        self._channel_select("Rares log channel...", await s.get(guild.id, keys.RARES_LOG_CHANNEL), TEXT_CHANNEL_TYPES, self._rares_log_picked, 1)
        self._channel_select("Rares voice channel...", await s.get(guild.id, keys.RARES_VOICE_CHANNEL), VOICE_CHANNEL_TYPES, self._rares_voice_picked, 2)
        self._button("Status Levels", discord.ButtonStyle.primary, self._edit_levels, row=3)
        return await rares_section_embed(self.bot, guild)

    async def _render_data(self, guild: discord.Guild) -> discord.Embed:
        self._button("Back Up Now", discord.ButtonStyle.primary, self._backup, row=1)
        war_loaded = self.bot.extensions.get("foxbot.features.war") is not None
        self._button("Archive Previous War", discord.ButtonStyle.danger, self._archive, row=1, disabled=not war_loaded)
        return await data_embed(self.bot, guild)

    async def _section_picked(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if await self.guard(interaction) is None:
            return
        value = select.values[0] if select.values else "overview"
        self.section = value if value in SECTIONS else "overview"
        await self.show(interaction)

    async def _store_channel(
        self,
        interaction: discord.Interaction,
        select: discord.ui.ChannelSelect,
        setting: Setting,
        name: str,
        needed: dict[str, str] | None = None,
        purpose: str | None = None,
    ) -> int | None:
        guild = await self.guard(interaction)
        if guild is None:
            return None
        channel_id = select.values[0].id if select.values else None
        await store_optional(self.bot, guild.id, setting, channel_id)
        notice = f"{name} set to <#{channel_id}>." if channel_id else f"{name} cleared."
        warning = channel_warning(guild, channel_id, needed, purpose) if needed and purpose else None
        await self.show(interaction, f"{notice}\n{warning}" if warning else notice)
        return channel_id

    async def _orders_channel_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await self._store_channel(interaction, select, keys.ORDERS_CHANNEL, "Orders channel", SEND_PERMISSIONS, "post order cards")

    async def _war_channel_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await self._store_channel(interaction, select, keys.WAR_ALERT_CHANNEL, "War alert channel", SEND_PERMISSIONS, "post war alerts")

    async def _ticket_category_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await self._store_channel(interaction, select, keys.TICKET_CATEGORY, "Ticket category")

    async def _ticket_log_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await self._store_channel(interaction, select, keys.TICKET_LOG_CHANNEL, "Ticket log channel", SEND_PERMISSIONS, "post ticket transcripts")

    async def _rares_log_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await self._store_channel(interaction, select, keys.RARES_LOG_CHANNEL, "Rares log channel", SEND_PERMISSIONS, "post the Rares log")

    async def _rares_voice_picked(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        channel_id = await self._store_channel(
            interaction, select, keys.RARES_VOICE_CHANNEL, "Rares voice channel", RENAME_PERMISSIONS, "rename the voice channel"
        )
        if channel_id:
            await refresh_rares_voice(self.bot, self.guild_id)

    async def _war_mode_picked(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        label = select.values[0] if select.values else "Off"
        mode = WAR_ALERT_MODES.get(label, "off")
        await store_optional(self.bot, guild.id, keys.WAR_ALERT_MODE, None if mode == "off" else mode)
        notice = f"Town change alerts: {label}."
        if mode == "ours" and not await self.bot.settings.get(guild.id, keys.FACTION):
            notice += " The faction is not set, so these alerts stay off until you press your faction's button."
        await self.show(interaction, notice)

    async def _all_trackers(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        await interaction.response.send_modal(AllTrackersModal(self, guild))

    async def _remove_board(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        provider = self.bot.boards.providers.get(self.board_kind or "")
        if provider is None:
            await self.show(interaction, "Pick a board first.")
            return
        removed = await self.bot.boards.remove(guild.id, provider.kind)
        notice = f"**{md(provider.title)}** board removed. Post it again at any time." if removed else f"**{md(provider.title)}** is not posted."
        await self.show(interaction, notice)

    async def _refresh_boards(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        posted = [kind for kind in self.bot.boards.providers if await self.bot.boards.record(guild.id, kind)]
        for kind in posted:
            self.bot.boards.request_refresh(guild.id, kind)
        await self.show(interaction, f"Refreshing {plural(len(posted), 'posted board')}." if posted else "No boards are posted yet.")

    async def _edit_timers(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        modal = TimersModal(
            self,
            stockpile_hours=await self.bot.settings.get(guild.id, keys.STOCKPILE_HOURS),
            ship_hours=await self.bot.settings.get(guild.id, keys.SHIP_HOURS),
            thresholds=await self.bot.settings.alert_thresholds(guild.id),
        )
        await interaction.response.send_modal(modal)

    async def _reset_timers(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        await self.bot.settings.reset(guild.id, keys.STOCKPILE_HOURS)
        await self.bot.settings.reset(guild.id, keys.SHIP_HOURS)
        await update_thresholds(self.bot, guild.id, "")
        await self.show(interaction, "Timers are back to the defaults.")

    async def _add_list_value(self, interaction: discord.Interaction, key: str) -> None:
        if await self.guard(interaction) is None:
            return
        await interaction.response.send_modal(ListValueModal(self, LIST_SECTIONS[key]))

    async def _remove_list_value(self, interaction: discord.Interaction, select: discord.ui.Select, key: str) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        if not select.values:
            await self.show(interaction)
            return
        value = select.values[0]
        error = await remove_list_value(self.bot, guild.id, LIST_SECTIONS[key], value)
        await self.show(interaction, error or f"Removed **{md(value)}**. Entries that already use it keep it.")

    async def _reset_list(self, interaction: discord.Interaction, key: str) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        section = LIST_SECTIONS[key]
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

    async def _edit_levels(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        await interaction.response.send_modal(RaresLevelsModal(self, list(await self.bot.settings.get(guild.id, keys.RARES_LEVELS))))

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

    async def _archive(self, interaction: discord.Interaction) -> None:
        if await self.guard(interaction) is None:
            return
        war = self.bot.extensions.get("foxbot.features.war")
        if war is None:
            await self.show(interaction, "The war feature is not loaded on this bot.")
            return
        await war.start_archive(self.bot, interaction, war.archive_label(await war.load_state(self.bot)))


class PermissionModal(BaseModal):
    def __init__(self, panel: AdminPanel, guild: discord.Guild, group: str, current: set[int]):
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


class SetupWizard(AdminPanel):
    def __init__(self, bot: FoxBot, guild_id: int, owner_id: int):
        super().__init__(bot, guild_id, owner_id)
        self.step = 0
        self.pending_access: list[int] | None = None

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
            "Members with this role can use `/settings`, `/setup` and every admin button.\n"
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
            "Timer reminders go by DM to whoever added or last refreshed the entry, everyone who pressed Notify Me and the entry's reminder roles. "
            "Only a reminder nobody can get by DM is posted here. Without a channel it goes to the channel of its board.\n\n"
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
        pings = await self.bot.settings.get(guild.id, keys.TICKET_PINGS)
        self._add_ping_select(guild, pings, 1)
        return (
            "**1. Ticket staff** (first menu) see every ticket channel and can set status, assign tickets and log payments.\n"
            "**2. Who gets pinged** (second menu) is mentioned in every new ticket channel and can see those channels. "
            "Pick roles or members; nobody is pinged until you pick someone.\n\n"
            f"Ticket staff: {roles_text(guild, current)}\n"
            f"Pinged for new tickets: {pings_text(pings)}"
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
            placeholder=f"Pick the roles for {ALL_ACCESS_GROUPS}...",
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
            f"Pick roles and press **Apply to All**. This replaces the roles of {ALL_ACCESS_GROUPS}. "
            "**Allow Everyone** opens them to every member. Press **Next** to keep the current access. "
            "Fine-tune single groups later in `/settings` under Permissions.\n\n" + "\n".join(lines) + pending,
            4000,
        )

    async def _step_boards(self, guild: discord.Guild) -> str:
        if not self.bot.boards.providers:
            return "No boards are available."
        await self._add_board_controls(guild, 0)
        lines = await boards_lines(self.bot, guild)
        return clip(BOARDS_HELP + "\n\n" + "\n".join(lines), 4000)

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
        await self.show(interaction, f"Saved for {ALL_ACCESS_GROUPS}: {roles_text(guild, role_ids, 'admins only')}.")

    async def _allow_everyone(self, interaction: discord.Interaction) -> None:
        guild = await self.guard(interaction)
        if guild is None:
            return
        for group in ACCESS_GROUPS:
            await self.bot.perms.set_roles(guild.id, group, {guild.id})
        self.pending_access = None
        await self.show(interaction, f"Every member can now use {ALL_ACCESS_GROUPS}.")

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

    @app_commands.command(name="settings", description="Admin: every bot setting in one menu: roles, permissions, boards, channels and more.")
    @app_commands.guild_only()
    async def settings_command(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await SettingsPanel(self.bot, interaction.guild_id, interaction.user.id).send(interaction)

    @app_commands.command(name="setup", description="Admin: guided setup of roles, alerts, faction, access and boards.")
    @app_commands.guild_only()
    async def setup_command(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await SetupWizard(self.bot, interaction.guild_id, interaction.user.id).send(interaction)


async def setup(bot: FoxBot) -> None:
    await bot.add_cog(Admin(bot))
