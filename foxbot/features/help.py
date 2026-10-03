from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import Colour
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, chunk_lines, clip
from foxbot.core.ui import EmbedPaginator

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

PAGE_CHARS = 3500
TITLE = "Foxhole Logistics Bot - Help"

INTROS = {
    "stockpile": (
        "Track reserved stockpiles, their codes and their 50 hour reserve timers. The live board lists every public "
        "stockpile with its code; private stockpiles are only visible to the people with access."
    ),
    "ship": "Track large ships: type, reserved squad, where they are parked and their 48 hour squad lock timers.",
    "msupps": (
        "Track maintenance supplies at bases: how many are stored, how fast they are used and when they run out."
    ),
    "inventory": (
        "Import stockpile contents from the in-game Copy to Clipboard export or a CSV file, then see totals, "
        "changes since the last import and export everything as a CSV file."
    ),
    "find": "Find which stockpiles hold an item, crated or loose, based on the latest imported inventories.",
    "order": (
        "Production, transport and scrap orders. Each order is a public card with progress bars that anyone can "
        "contribute to, plus a leaderboard of contributors."
    ),
    "rare": (
        "The regiment's Rare Alloys stock: the current count, every change with who made it, an optional log "
        "channel and an optional voice channel that shows the live count."
    ),
    "ticket": (
        "Customer tickets for ordering services from the regiment. Each ticket gets a private channel; prices are "
        "in Rare Alloys and payments are added to the regiment's stock."
    ),
    "war": (
        "The current war from the official War API: war number and day, victory towns held by each side, "
        "casualties and recent town captures. Admins can post a live war board and archive the previous war's data."
    ),
    "logi": (
        "Logi run requests: ask for a delivery from one place to another, let a driver claim it and mark it "
        "delivered. The logistics role is pinged once when a request is made."
    ),
    "facility": (
        "A shared facility queue: what the regiment wants produced at its facilities, in order, who is working on "
        "each entry and what is done."
    ),
    "mine": (
        "Everything you created or have access to across stockpiles, ships and bases, with their timers, plus the "
        "open logi runs you requested or claimed."
    ),
    "settings": (
        "Admin panel: admin and logistics roles, alerts channel, reminder thresholds, faction, storage and ship "
        "types, ticket services, Rare Alloys channels, orders channel, war alerts and backups."
    ),
    "permissions": "Admin panel to choose which roles may use each part of the bot. Admins always pass.",
    "setup": "Guided first-time setup: roles, alerts channel, faction, ticket staff, who can use the trackers and the boards.",
    "purge": (
        "Delete recent messages in the current channel, optionally only from one user. Needs Discord's Manage "
        "Messages permission. Pinned messages are kept."
    ),
    "role": "Add or remove a role for one member or for everyone. Needs Discord's Manage Roles permission.",
    "opsec": "Post a public guide on turning off Discord Activity Sharing, which can reveal where you are in game.",
    "help": "This guide. It is built from the live command list, so it always matches what the bot offers.",
}

ALIASES = {
    "stockpiles": "stockpile",
    "ships": "ship",
    "orders": "order",
    "rares": "rare",
    "tickets": "ticket",
}

ORDER = [
    "setup",
    "stockpile",
    "ship",
    "msupps",
    "inventory",
    "find",
    "order",
    "rare",
    "ticket",
    "logi",
    "facility",
    "war",
    "mine",
    "settings",
    "permissions",
    "purge",
    "role",
    "opsec",
    "help",
]

Entry = app_commands.Command | app_commands.Group


def base_name(name: str) -> str:
    return ALIASES.get(name, name)


def intro_for(command: Entry) -> str:
    return INTROS.get(base_name(command.name)) or command.description or "No description."


def sort_key(command: Entry) -> tuple[int, str]:
    name = base_name(command.name)
    return (ORDER.index(name) if name in ORDER else len(ORDER), command.name)


def top_level_commands(bot: FoxBot) -> list[Entry]:
    found = bot.tree.get_commands(type=discord.AppCommandType.chat_input)
    return sorted(found, key=sort_key)


def leaf_commands(command: Entry) -> list[app_commands.Command]:
    if isinstance(command, app_commands.Group):
        result: list[app_commands.Command] = []
        for child in command.commands:
            result.extend(leaf_commands(child))
        return result
    return [command]


def parameter_text(command: app_commands.Command) -> str:
    parts = []
    for parameter in command.parameters:
        name = parameter.display_name
        parts.append(f"`{name}`" if parameter.required else f"`[{name}]`")
    return " ".join(parts)


def command_line(command: app_commands.Command) -> str:
    parameters = parameter_text(command)
    head = f"**/{command.qualified_name}**" + (f" {parameters}" if parameters else "")
    return f"{head}\n{command.description or 'No description.'}"


def permission_note(command: Entry) -> str | None:
    permissions = command.default_permissions
    if permissions is None or permissions.value == 0:
        return None
    names = [name.replace("_", " ").title() for name, enabled in permissions if enabled]
    return f"Only shown to members with: {', '.join(names)}."


def overview_lines(commands_: list[Entry]) -> list[str]:
    return [f"**/{command.name}** - {command.description or 'No description.'}" for command in commands_]


def command_lines(command: Entry) -> list[str]:
    lines = [intro_for(command)]
    note = permission_note(command)
    if note:
        lines.append(f"*{note}*")
    lines.append("")
    leaves = leaf_commands(command)
    for index, leaf in enumerate(leaves):
        lines.append(command_line(leaf))
        if index < len(leaves) - 1:
            lines.append("")
    return lines


def page_embeds(title: str, lines: list[str]) -> list[discord.Embed]:
    chunks = chunk_lines(lines, PAGE_CHARS) or ["No commands."]
    embeds = []
    for index, chunk in enumerate(chunks, start=1):
        page_title = title if len(chunks) == 1 else f"{title} ({index}/{len(chunks)})"
        embeds.append(discord.Embed(title=clip(page_title, 256), description=clip(chunk.strip("\n"), EMBED_DESCRIPTION_LIMIT), colour=Colour.INFO))
    return embeds


def build_pages(bot: FoxBot) -> list[discord.Embed]:
    commands_ = top_level_commands(bot)
    intro = [
        "Every command this bot offers, one page per command. Use the buttons below to flip through the pages.",
        "Panels, pickers, confirmations and errors are only visible to you. Options in `[brackets]` are optional.",
        "",
    ]
    overview = overview_lines(commands_) or ["No commands are registered."]
    pages = page_embeds(TITLE, intro + overview)
    for command in commands_:
        pages.extend(page_embeds(f"/{command.name}", command_lines(command)))
    total = len(pages)
    for number, embed in enumerate(pages, start=1):
        embed.set_footer(text=f"Page {number}/{total}")
    return pages


class Help(commands.Cog):
    def __init__(self, bot: FoxBot):
        self.bot = bot

    @app_commands.command(name="help", description="Browse every command of this bot (only you see it).")
    async def help_command(self, interaction: discord.Interaction) -> None:
        await EmbedPaginator(build_pages(self.bot), owner_id=interaction.user.id).start(interaction)


async def setup(bot: FoxBot) -> None:
    await bot.add_cog(Help(bot))
