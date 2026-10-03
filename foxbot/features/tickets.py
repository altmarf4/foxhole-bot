from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import TICKET_QUANTITY_MAX, TICKET_STATUSES, Colour
from foxbot.core import timeutil
from foxbot.core import settings as keys
from foxbot.core.permissions import check_group, interaction_member, resolve_member
from foxbot.core.text import EMBED_FIELD_LIMIT, clip, md, paginate_embeds
from foxbot.core.ui import (
    BaseModal,
    BaseView,
    ConfirmView,
    EmbedPaginator,
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

KIND = "tickets"
GROUP = "tickets"
RARES_EXTENSION = "foxbot.features.rares"
CATEGORY_NAME = "Tickets"
LOG_CHANNEL_NAME = "ticket-log"
DEFAULT_STATUS = "Open"
REGIMENT_MAX = 50
NOTES_MAX = 1000
AMOUNT_MAX = 1_000_000_000
INTEGER = re.compile(r"[+-]?[0-9]+")
SELECT_MAX = 25
TRANSCRIPT_MAX_BYTES = 8 * 1024 * 1024
QUANTITY_CHOICES = list(range(1, min(TICKET_QUANTITY_MAX, SELECT_MAX) + 1))

ORDER_INSTRUCTIONS = (
    "After this form closes your ticket channel opens. Press the Order Service button, pick a service and a quantity, "
    "then press Confirm. Repeat for each service you need."
)
NO_SERVICES = "No services are configured yet, so tickets cannot be opened. An admin can add services in `/settings`."
PERMISSION_HINT = (
    "I need the **Manage Channels** permission, plus View Channels, Send Messages, Embed Links, Attach Files and "
    "Read Message History. Ask an admin to check my role."
)
NOT_CUSTOMER = "Only the person who opened this ticket or ticket staff can do that."

Handler = Callable[["FoxBot", discord.Interaction, int, int], Awaitable[None]]


class SetupError(Exception):
    pass


@dataclass
class Summary:
    ticket: dict
    lines: list[dict]
    paid: int

    @property
    def cost(self) -> int:
        return sum(line["unit_cost"] * line["quantity"] for line in self.lines)

    @property
    def owed(self) -> int:
        return max(0, self.cost - self.paid)

    @property
    def ordered(self) -> bool:
        return bool(self.lines)


def ticket_label(number: int) -> str:
    return f"{int(number):04d}"


def regiment_key(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def parse_int(raw: str) -> int | None:
    cleaned = raw.replace(",", "").replace(" ", "").replace("_", "")
    if not INTEGER.fullmatch(cleaned):
        return None
    value = int(cleaned)
    if abs(value) > AMOUNT_MAX:
        return None
    return value


def slug(text: str, limit: int) -> str:
    cleaned = re.sub(r"[\W_]+", "-", str(text).casefold()).strip("-")
    return cleaned[:limit].strip("-")


def channel_name(regiment: str, number: int, service: str | None = None) -> str:
    parts = ["ticket"]
    regiment_slug = slug(regiment, 24)
    if regiment_slug:
        parts.append(regiment_slug)
    if service:
        service_slug = slug(service, 40)
        if service_slug:
            parts.append(service_slug)
    parts.append(ticket_label(number))
    return "-".join(parts)[:100]


async def list_services(bot: FoxBot, guild_id: int) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT name, cost FROM ticket_services WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
        (guild_id,),
    )


async def get_service(bot: FoxBot, guild_id: int, name: str) -> dict | None:
    return await bot.db.fetchone(
        "SELECT name, cost FROM ticket_services WHERE guild_id = ? AND name = ?",
        (guild_id, name),
    )


async def set_service(bot: FoxBot, guild_id: int, name: str, cost: int) -> None:
    await bot.db.execute(
        "INSERT INTO ticket_services (guild_id, name, cost) VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id, name) DO UPDATE SET cost = excluded.cost",
        (guild_id, name.strip(), int(cost)),
    )


async def remove_service(bot: FoxBot, guild_id: int, name: str) -> bool:
    removed = await bot.db.execute("DELETE FROM ticket_services WHERE guild_id = ? AND name = ?", (guild_id, name))
    return removed > 0


async def fetch(bot: FoxBot, guild_id: int, number: int) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM tickets WHERE guild_id = ? AND number = ?", (guild_id, number))


async def fetch_by_channel(bot: FoxBot, channel_id: int) -> dict | None:
    return await bot.db.fetchone(
        "SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL",
        (channel_id,),
    )


async def items(bot: FoxBot, guild_id: int, number: int) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT service, unit_cost, quantity FROM ticket_items WHERE guild_id = ? AND number = ? ORDER BY rowid",
        (guild_id, number),
    )


async def paid_total(bot: FoxBot, guild_id: int, number: int) -> int:
    return int(
        await bot.db.fetchval(
            "SELECT SUM(amount) FROM ticket_payments WHERE guild_id = ? AND number = ?",
            (guild_id, number),
            0,
        )
    )


async def summarize(bot: FoxBot, ticket: dict) -> Summary:
    return Summary(
        ticket=ticket,
        lines=await items(bot, ticket["guild_id"], ticket["number"]),
        paid=await paid_total(bot, ticket["guild_id"], ticket["number"]),
    )


async def create(bot: FoxBot, guild_id: int, *, regiment: str, notes: str, user_id: int) -> dict:
    regiment = " ".join(regiment.split())[:REGIMENT_MAX]
    now = timeutil.now()
    async with bot.db.transaction() as tx:
        number = int(await tx.fetchval("SELECT MAX(number) FROM tickets WHERE guild_id = ?", (guild_id,), 0)) + 1
        await tx.execute(
            "INSERT INTO tickets (guild_id, number, regiment, notes, status, opened_by, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, regiment, notes, DEFAULT_STATUS, user_id, now),
        )
    return await fetch(bot, guild_id, number)


async def discard(bot: FoxBot, guild_id: int, number: int) -> None:
    await bot.db.execute("DELETE FROM tickets WHERE guild_id = ? AND number = ?", (guild_id, number))


async def update(bot: FoxBot, ticket: dict, **fields) -> dict | None:
    assignments = ", ".join(f"{column} = ?" for column in fields)
    await bot.db.execute(
        f"UPDATE tickets SET {assignments} WHERE guild_id = ? AND number = ?",
        (*fields.values(), ticket["guild_id"], ticket["number"]),
    )
    return await fetch(bot, ticket["guild_id"], ticket["number"])


async def add_item(bot: FoxBot, guild_id: int, number: int, service: str, unit_cost: int, quantity: int) -> None:
    await bot.db.execute(
        "INSERT INTO ticket_items (guild_id, number, service, unit_cost, quantity) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (guild_id, number, service, unit_cost) DO UPDATE SET quantity = ticket_items.quantity + excluded.quantity",
        (guild_id, number, service, unit_cost, quantity),
    )


async def remove_item(bot: FoxBot, guild_id: int, number: int, service: str, unit_cost: int) -> dict | None:
    async with bot.db.transaction() as tx:
        row = await tx.fetchone(
            "SELECT service, unit_cost, quantity FROM ticket_items WHERE guild_id = ? AND number = ? AND service = ? AND unit_cost = ?",
            (guild_id, number, service, unit_cost),
        )
        if row is None:
            return None
        await tx.execute(
            "DELETE FROM ticket_items WHERE guild_id = ? AND number = ? AND service = ? AND unit_cost = ?",
            (guild_id, number, service, unit_cost),
        )
    return row


async def record_payment(bot: FoxBot, guild_id: int, number: int, amount: int, user_id: int) -> None:
    await bot.db.execute(
        "INSERT INTO ticket_payments (guild_id, number, amount, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, number, amount, user_id, timeutil.now()),
    )


async def claim_rename(bot: FoxBot, guild_id: int, number: int) -> bool:
    changed = await bot.db.execute(
        "UPDATE tickets SET renamed = 1 WHERE guild_id = ? AND number = ? AND renamed = 0",
        (guild_id, number),
    )
    return changed == 1


async def mark_closed(bot: FoxBot, ticket: dict, user_id: int | None, closed_at: int) -> bool:
    changed = await bot.db.execute(
        "UPDATE tickets SET closed_at = ?, closed_by = ? WHERE guild_id = ? AND number = ? AND closed_at IS NULL",
        (closed_at, user_id, ticket["guild_id"], ticket["number"]),
    )
    return changed == 1


async def ticket_rows(bot: FoxBot, guild_id: int, *, open_only: bool = False, opened_by: int | None = None) -> list[dict]:
    conditions = ["t.guild_id = ?"]
    params: list = [guild_id]
    if open_only:
        conditions.append("t.closed_at IS NULL")
    if opened_by is not None:
        conditions.append("t.opened_by = ?")
        params.append(opened_by)
    return await bot.db.fetchall(
        "SELECT t.*, "
        "COALESCE((SELECT SUM(i.unit_cost * i.quantity) FROM ticket_items i WHERE i.guild_id = t.guild_id AND i.number = t.number), 0) AS cost, "
        "COALESCE((SELECT SUM(p.amount) FROM ticket_payments p WHERE p.guild_id = t.guild_id AND p.number = t.number), 0) AS paid, "
        "(SELECT COUNT(*) FROM ticket_items c WHERE c.guild_id = t.guild_id AND c.number = t.number) AS line_count "
        f"FROM tickets t WHERE {' AND '.join(conditions)} ORDER BY t.number",
        params,
    )


async def closed_count(bot: FoxBot, guild_id: int) -> int:
    return int(await bot.db.fetchval("SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND closed_at IS NOT NULL", (guild_id,), 0))


async def is_staff(bot: FoxBot, member: discord.Member) -> bool:
    return await bot.perms.has(member, GROUP)


async def staff_roles(bot: FoxBot, guild: discord.Guild) -> list[discord.Role]:
    role_ids = await bot.perms.roles_for(guild.id, GROUP)
    roles = [guild.get_role(role_id) for role_id in sorted(role_ids) if role_id != guild.id]
    return [role for role in roles if role is not None]


async def access_roles(bot: FoxBot, guild: discord.Guild) -> list[discord.Role]:
    roles = await staff_roles(bot, guild)
    admin_role_id = await bot.settings.get(guild.id, keys.ADMIN_ROLE)
    if admin_role_id and int(admin_role_id) != guild.id:
        admin_role = guild.get_role(int(admin_role_id))
        if admin_role is not None and admin_role not in roles:
            roles.append(admin_role)
    return roles


def owed_text(cost: int, paid: int, ordered: bool) -> str:
    if not ordered:
        return "Nothing ordered yet"
    if paid >= cost:
        return "Paid in full"
    return f"{cost - paid} Rare Alloys"


def owed_phrase(cost: int, paid: int, ordered: bool) -> str:
    if not ordered:
        return "nothing ordered yet"
    if paid >= cost:
        return "paid in full"
    return f"{cost - paid} Rare Alloys still owed"


def item_line(row: dict) -> str:
    return f"{row['quantity']}x {md(row['service'])} - {row['quantity'] * row['unit_cost']} Rare Alloys"


def fit_lines(lines: list[str], *, empty: str, limit: int = EMBED_FIELD_LIMIT) -> str:
    if not lines:
        return empty
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        line = clip(line, 200)
        remaining = len(lines) - index - 1
        tail = len(f"\n... and {remaining} more line(s)") if remaining else 0
        if used + len(line) + 1 + tail > limit:
            kept.append(f"... and {len(lines) - index} more line(s)")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def services_text(lines: list[dict]) -> str:
    return fit_lines([item_line(row) for row in lines], empty="Nothing ordered yet")


def card_embed(summary: Summary) -> discord.Embed:
    ticket = summary.ticket
    embed = discord.Embed(
        title=clip(f"Ticket {ticket_label(ticket['number'])} - {ticket['regiment']}", 256),
        colour=TICKET_STATUSES.get(ticket["status"], Colour.MUTED),
    )
    embed.add_field(name="Regiment", value=clip(md(ticket["regiment"]), EMBED_FIELD_LIMIT), inline=True)
    embed.add_field(name="Status", value=ticket["status"], inline=True)
    embed.add_field(
        name="Assigned To",
        value=f"<@{ticket['assigned_to']}>" if ticket["assigned_to"] else "Unassigned",
        inline=True,
    )
    embed.add_field(name="Services", value=services_text(summary.lines), inline=False)
    embed.add_field(name="Total Cost", value=f"{summary.cost} Rare Alloys", inline=True)
    embed.add_field(name="Rares Paid", value=f"{summary.paid} Rare Alloys", inline=True)
    embed.add_field(name="Rares Owed", value=owed_text(summary.cost, summary.paid, summary.ordered), inline=True)
    if ticket["notes"]:
        embed.add_field(name="Notes", value=clip(md(ticket["notes"]), EMBED_FIELD_LIMIT), inline=False)
    embed.add_field(name="Opened By", value=f"<@{ticket['opened_by']}>", inline=True)
    embed.add_field(name="Opened", value=timeutil.full(ticket["opened_at"]), inline=True)
    embed.set_footer(text=f"Ticket {ticket_label(ticket['number'])}")
    return embed


def closed_embed(summary: Summary, closed_by: int, closed_at: int) -> discord.Embed:
    ticket = summary.ticket
    embed = discord.Embed(
        title=clip(f"Ticket {ticket_label(ticket['number'])} closed - {ticket['regiment']}", 256),
        colour=Colour.DARK,
    )
    embed.add_field(name="Regiment", value=clip(md(ticket["regiment"]), EMBED_FIELD_LIMIT), inline=True)
    embed.add_field(name="Final Status", value=ticket["status"], inline=True)
    embed.add_field(name="Closed By", value=f"<@{closed_by}>", inline=True)
    embed.add_field(name="Services", value=services_text(summary.lines), inline=False)
    embed.add_field(name="Total Cost", value=f"{summary.cost} Rare Alloys", inline=True)
    embed.add_field(name="Rares Paid", value=f"{summary.paid} Rare Alloys", inline=True)
    embed.add_field(name="Rares Owed", value=owed_text(summary.cost, summary.paid, summary.ordered), inline=True)
    if ticket["notes"]:
        embed.add_field(name="Notes", value=clip(md(ticket["notes"]), EMBED_FIELD_LIMIT), inline=False)
    embed.add_field(name="Opened By", value=f"<@{ticket['opened_by']}>", inline=True)
    embed.add_field(name="Opened", value=timeutil.full(ticket["opened_at"]), inline=True)
    embed.add_field(name="Closed", value=timeutil.full(closed_at), inline=True)
    embed.add_field(
        name="Assigned To",
        value=f"<@{ticket['assigned_to']}>" if ticket["assigned_to"] else "Unassigned",
        inline=True,
    )
    embed.set_footer(text=f"Ticket {ticket_label(ticket['number'])} | Transcript attached")
    return embed


def utc_text(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def utc_ts(ts: int) -> str:
    return utc_text(datetime.fromtimestamp(int(ts), tz=timezone.utc))


def person(guild: discord.Guild | None, user_id: int | None) -> str:
    if not user_id:
        return "nobody"
    member = guild.get_member(user_id) if guild is not None else None
    if member is not None:
        return f"{member.display_name} ({user_id})"
    return f"user {user_id}"


def transcript_header(summary: Summary, guild: discord.Guild | None, closed_by: int, closed_at: int) -> list[str]:
    ticket = summary.ticket
    lines = [
        f"Ticket {ticket_label(ticket['number'])} - {ticket['regiment']}",
        f"Opened by {person(guild, ticket['opened_by'])} at {utc_ts(ticket['opened_at'])}",
        f"Closed by {person(guild, closed_by)} at {utc_ts(closed_at)}",
        f"Final status: {ticket['status']}",
        f"Assigned to: {person(guild, ticket['assigned_to']) if ticket['assigned_to'] else 'Unassigned'}",
        "Services:",
    ]
    if summary.lines:
        lines += [
            f"  {row['quantity']}x {row['service']} - {row['quantity'] * row['unit_cost']} Rare Alloys ({row['unit_cost']} each)"
            for row in summary.lines
        ]
    else:
        lines.append("  Nothing ordered")
    lines += [
        f"Total cost: {summary.cost} Rare Alloys",
        f"Rares paid: {summary.paid} Rare Alloys",
        f"Rares owed: {owed_text(summary.cost, summary.paid, summary.ordered)}",
    ]
    if ticket["notes"]:
        lines.append("Notes: " + ticket["notes"].replace("\n", "\n       "))
    lines += ["", "Messages", "-" * 60]
    return lines


async def build_transcript(channel, summary: Summary, guild: discord.Guild | None, closed_by: int, closed_at: int) -> str:
    lines = transcript_header(summary, guild, closed_by, closed_at)
    count = 0
    if channel is not None:
        async for message in channel.history(limit=None, oldest_first=True):
            count += 1
            author = message.author
            name = getattr(author, "display_name", None) or getattr(author, "name", "unknown")
            content = (message.clean_content or "").replace("\n", "\n    ")
            lines.append(f"[{utc_text(message.created_at)}] {name} ({author.id}): {content}")
            for attachment in message.attachments:
                lines.append(f"    Attachment: {attachment.url}")
            for embed in message.embeds:
                if embed.title:
                    lines.append(f"    Embed: {embed.title}")
    if count == 0:
        lines.append("No messages.")
    return "\n".join(lines) + "\n"


def transcript_file(text: str, number: int) -> discord.File:
    data = text.encode("utf-8")
    if len(data) > TRANSCRIPT_MAX_BYTES:
        notice = b"\n[Transcript truncated: the rest was too large to upload]\n"
        data = data[: TRANSCRIPT_MAX_BYTES - len(notice)] + notice
    return discord.File(io.BytesIO(data), filename=f"ticket-{ticket_label(number)}-transcript.txt")


BUTTONS: dict[str, tuple[str, discord.ButtonStyle, int]] = {
    "status": ("Set Status", discord.ButtonStyle.primary, 0),
    "assign": ("Assign", discord.ButtonStyle.primary, 0),
    "payment": ("Log Payment", discord.ButtonStyle.secondary, 0),
    "order": ("Order Service", discord.ButtonStyle.success, 1),
    "remove": ("Remove Service", discord.ButtonStyle.secondary, 1),
    "close": ("Close Ticket", discord.ButtonStyle.danger, 2),
}


class TicketButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ft:(?P<action>[a-z]+):(?P<guild>\d+):(?P<number>\d+)",
):
    def __init__(self, action: str, guild_id: int, number: int):
        label, style, row = BUTTONS.get(action, (action, discord.ButtonStyle.secondary, 0))
        super().__init__(
            discord.ui.Button(label=label, style=style, row=row, custom_id=f"ft:{action}:{guild_id}:{number}")
        )
        self.action = action
        self.guild_id = guild_id
        self.number = number

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], int(match["guild"]), int(match["number"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = HANDLERS.get(self.action)
        if handler is None:
            await deny(interaction, "That button is no longer supported.")
            return
        try:
            await handler(interaction.client, interaction, self.guild_id, self.number)
        except Exception as error:
            await report_error(interaction, error, f"ticket button {self.action}")


def ticket_view(guild_id: int, number: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in BUTTONS:
        view.add_item(TicketButton(action, guild_id, number).item)
    return view


async def refresh_card(bot: FoxBot, guild_id: int, number: int) -> None:
    ticket = await fetch(bot, guild_id, number)
    if ticket is None or not ticket["channel_id"] or not ticket["card_message_id"]:
        return
    summary = await summarize(bot, ticket)
    try:
        message = bot.get_partial_messageable(ticket["channel_id"]).get_partial_message(ticket["card_message_id"])
        await message.edit(embed=card_embed(summary), view=ticket_view(guild_id, number))
    except discord.HTTPException:
        log.warning("Could not update the card of ticket %s in guild %s", number, guild_id, exc_info=True)


async def load_open(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> dict | None:
    ticket = await fetch(bot, guild_id, number)
    if ticket is None:
        await deny(interaction, "This ticket no longer exists.")
        return None
    if ticket["closed_at"] is not None:
        await deny(interaction, "This ticket is closed.")
        return None
    return ticket


async def staff_gate(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> dict | None:
    if not await check_group(interaction, GROUP, guild_id):
        return None
    return await load_open(bot, interaction, guild_id, number)


async def customer_gate(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> dict | None:
    ticket = await load_open(bot, interaction, guild_id, number)
    if ticket is None:
        return None
    member = await interaction_member(interaction, guild_id)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return None
    if member.id != ticket["opened_by"] and not await is_staff(bot, member):
        await deny(interaction, NOT_CUSTOMER)
        return None
    return ticket


def member_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        embed_links=True,
    )


def hidden_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(view_channel=False)


async def ensure_category(bot: FoxBot, guild: discord.Guild) -> discord.CategoryChannel:
    category_id = await bot.settings.get(guild.id, keys.TICKET_CATEGORY)
    existing = guild.get_channel(int(category_id)) if category_id else None
    if isinstance(existing, discord.CategoryChannel):
        return existing
    overwrites = {guild.default_role: hidden_overwrite()}
    if guild.me is not None:
        overwrites[guild.me] = member_overwrite()
    category = await guild.create_category(CATEGORY_NAME, overwrites=overwrites, reason="Ticket system setup")
    await bot.settings.set(guild.id, keys.TICKET_CATEGORY, category.id)
    return category


async def ensure_log_channel(bot: FoxBot, guild: discord.Guild, category: discord.CategoryChannel) -> discord.TextChannel:
    channel_id = await bot.settings.get(guild.id, keys.TICKET_LOG_CHANNEL)
    existing = guild.get_channel(int(channel_id)) if channel_id else None
    if isinstance(existing, discord.TextChannel):
        return existing
    overwrites = {guild.default_role: hidden_overwrite()}
    for role in await access_roles(bot, guild):
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
    if guild.me is not None:
        overwrites[guild.me] = member_overwrite()
    channel = await guild.create_text_channel(
        LOG_CHANNEL_NAME,
        category=category,
        overwrites=overwrites,
        reason="Ticket system setup",
    )
    await bot.settings.set(guild.id, keys.TICKET_LOG_CHANNEL, channel.id)
    return channel


async def ensure_ticket_channels(bot: FoxBot, guild: discord.Guild) -> tuple[discord.CategoryChannel, discord.TextChannel]:
    try:
        category = await ensure_category(bot, guild)
        log_channel = await ensure_log_channel(bot, guild, category)
    except discord.Forbidden:
        raise SetupError(f"I could not create the Tickets category or the ticket-log channel. {PERMISSION_HINT}") from None
    except discord.HTTPException as error:
        raise SetupError(f"Discord refused to create the ticket channels: {error.text or error}") from None
    return category, log_channel


async def ticket_overwrites(bot: FoxBot, guild: discord.Guild, opener: discord.abc.Snowflake) -> dict:
    overwrites = {guild.default_role: hidden_overwrite(), opener: member_overwrite()}
    for role in await access_roles(bot, guild):
        overwrites[role] = member_overwrite()
    if guild.me is not None:
        overwrites[guild.me] = member_overwrite()
    return overwrites


async def open_ticket(bot: FoxBot, interaction: discord.Interaction, *, regiment: str, notes: str) -> None:
    guild = interaction.guild
    member = await interaction_member(interaction)
    if guild is None or member is None:
        await deny(interaction, "Tickets can only be opened inside a server.")
        return
    regiment = " ".join(regiment.split())[:REGIMENT_MAX]
    if not regiment:
        await deny(interaction, "A regiment name is required.")
        return
    if not await list_services(bot, guild.id):
        await deny(interaction, NO_SERVICES)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        category, _ = await ensure_ticket_channels(bot, guild)
    except SetupError as error:
        await reply(interaction, str(error))
        return
    ticket = await create(bot, guild.id, regiment=regiment, notes=notes.strip()[:NOTES_MAX], user_id=member.id)
    number = ticket["number"]
    try:
        channel = await guild.create_text_channel(
            channel_name(regiment, number),
            category=category,
            overwrites=await ticket_overwrites(bot, guild, member),
            reason=f"Ticket {ticket_label(number)} opened by {member}",
        )
    except discord.Forbidden:
        await discard(bot, guild.id, number)
        await reply(interaction, f"I could not create your ticket channel. {PERMISSION_HINT}")
        return
    except discord.HTTPException as error:
        await discard(bot, guild.id, number)
        await reply(interaction, f"Discord refused to create your ticket channel: {error.text or error}")
        return
    pings = await staff_roles(bot, guild)
    summary = await summarize(bot, ticket)
    try:
        message = await channel.send(
            content=" ".join(role.mention for role in pings) or None,
            embed=card_embed(summary),
            view=ticket_view(guild.id, number),
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=pings),
        )
    except discord.HTTPException:
        log.warning("Could not post the card of ticket %s in guild %s", number, guild.id, exc_info=True)
        try:
            await channel.delete(reason="The ticket card could not be posted")
        except discord.HTTPException:
            pass
        await discard(bot, guild.id, number)
        await reply(interaction, f"I created your ticket channel but could not post in it, so it was removed. {PERMISSION_HINT}")
        return
    await update(bot, ticket, channel_id=channel.id, card_message_id=message.id)
    await reply(
        interaction,
        f"Your ticket is open: {channel.mention}\nPress **Order Service** there, pick a service and a quantity, then press **Confirm**.",
    )


class OpenTicketModal(BaseModal, title="Open a Ticket"):
    def __init__(self, bot: FoxBot):
        super().__init__()
        self.bot = bot
        self.instructions = discord.ui.TextDisplay(ORDER_INSTRUCTIONS)
        self.regiment_field = text_field("Regiment", placeholder="e.g. KM", max_length=REGIMENT_MAX)
        self.notes_field = text_field(
            "Notes",
            placeholder="Optional: anything else we should know",
            required=False,
            max_length=NOTES_MAX,
            paragraph=True,
        )
        self.add_item(self.instructions)
        self.add_item(self.regiment_field)
        self.add_item(self.notes_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await open_ticket(
            self.bot,
            interaction,
            regiment=text_value(self.regiment_field),
            notes=text_value(self.notes_field),
        )


class StatusPicker(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, number: int, current: str, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.number = number
        self.select = discord.ui.Select(
            placeholder="Pick the new status...",
            options=[option(name, name, default=name == current) for name in TICKET_STATUSES],
        )
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        ticket = await staff_gate(self.bot, interaction, self.guild_id, self.number)
        if ticket is None:
            return
        new = self.select.values[0]
        if new not in TICKET_STATUSES:
            await deny(interaction, "That status is not available.")
            return
        old = ticket["status"]
        if old != new:
            await update(self.bot, ticket, status=new)
        await edit(
            interaction,
            content=f"Ticket **{ticket_label(self.number)}** status changed from **{old}** to **{new}**.",
            view=None,
        )
        await refresh_card(self.bot, self.guild_id, self.number)


class AssignPicker(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, number: int, current: int | None, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.number = number
        defaults = [discord.SelectDefaultValue(id=current, type=discord.SelectDefaultValueType.user)] if current else []
        self.select = discord.ui.UserSelect(
            placeholder="Pick a ticket staff member...",
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=0,
        )
        self.select.callback = self._picked
        self.add_item(self.select)
        self.unassign_button = discord.ui.Button(
            label="Unassign",
            style=discord.ButtonStyle.secondary,
            disabled=current is None,
            row=1,
        )
        self.unassign_button.callback = self._unassign
        self.add_item(self.unassign_button)

    async def _picked(self, interaction: discord.Interaction) -> None:
        ticket = await staff_gate(self.bot, interaction, self.guild_id, self.number)
        if ticket is None:
            return
        chosen = self.select.values[0]
        member = chosen if isinstance(chosen, discord.Member) else await resolve_member(self.bot, self.guild_id, chosen.id)
        if member is None or member.bot or not await is_staff(self.bot, member):
            await edit(
                interaction,
                content=f"{chosen.mention} is not ticket staff. Pick someone with the **Ticket staff** permission or an admin.",
                view=self,
            )
            return
        await update(self.bot, ticket, assigned_to=member.id)
        await edit(interaction, content=f"Ticket **{ticket_label(self.number)}** assigned to {member.mention}.", view=None)
        await refresh_card(self.bot, self.guild_id, self.number)

    async def _unassign(self, interaction: discord.Interaction) -> None:
        ticket = await staff_gate(self.bot, interaction, self.guild_id, self.number)
        if ticket is None:
            return
        await update(self.bot, ticket, assigned_to=None)
        await edit(interaction, content=f"Ticket **{ticket_label(self.number)}** is now unassigned.", view=None)
        await refresh_card(self.bot, self.guild_id, self.number)


class PaymentModal(BaseModal, title="Log Rare Alloys Payment"):
    def __init__(self, bot: FoxBot, guild_id: int, number: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.number = number
        self.amount_field = text_field(
            "Rare Alloys received",
            placeholder="e.g. 200 (a negative number corrects a mistake)",
            max_length=12,
        )
        self.add_item(self.amount_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ticket = await staff_gate(self.bot, interaction, self.guild_id, self.number)
        if ticket is None:
            return
        amount = parse_int(text_value(self.amount_field))
        if amount is None:
            await deny(interaction, "Enter a whole number, for example 200, or -50 to correct a mistake.")
            return
        if amount == 0:
            await deny(interaction, "The amount cannot be zero.")
            return
        paid = await paid_total(self.bot, self.guild_id, self.number)
        if paid + amount < 0:
            await deny(
                interaction,
                f"That correction would take the total paid below zero. Only **{paid}** Rare Alloys are recorded as paid on this ticket.",
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await record_payment(self.bot, self.guild_id, self.number, amount, interaction.user.id)
        stock = ""
        rares = self.bot.extensions.get(RARES_EXTENSION)
        if rares is not None:
            try:
                old, new = await rares.apply_change(
                    self.bot,
                    self.guild_id,
                    action="payment",
                    amount=amount,
                    user_id=interaction.user.id,
                    note=f"Ticket {ticket_label(self.number)} - {ticket['regiment']}",
                    ticket_number=self.number,
                )
                stock = f"\nRegiment stock: {old} -> {new} Rare Alloys."
            except Exception:
                log.exception("Could not add the payment of ticket %s to the regiment stock", self.number)
                stock = "\nThe regiment Rare Alloys stock could not be updated. Check the Rare Alloys board."
        summary = await summarize(self.bot, ticket)
        await reply(
            interaction,
            f"Logged **{amount:+d}** Rare Alloys on ticket **{ticket_label(self.number)}**. "
            f"Total paid: **{summary.paid}** Rare Alloys - {owed_phrase(summary.cost, summary.paid, summary.ordered)}.{stock}",
        )
        await refresh_card(self.bot, self.guild_id, self.number)


async def rename_after_first_order(bot: FoxBot, ticket: dict, service: str) -> None:
    if ticket["renamed"] or not ticket["channel_id"]:
        return
    if not await claim_rename(bot, ticket["guild_id"], ticket["number"]):
        return
    guild = bot.get_guild(ticket["guild_id"])
    channel = guild.get_channel(ticket["channel_id"]) if guild is not None else None
    if channel is None:
        return
    try:
        await channel.edit(
            name=channel_name(ticket["regiment"], ticket["number"], service),
            reason=f"Ticket {ticket_label(ticket['number'])} first order",
        )
    except discord.HTTPException:
        log.warning("Could not rename the channel of ticket %s", ticket["number"], exc_info=True)


class OrderServiceView(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, number: int, services: list[dict], owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.number = number
        self.services = [dict(service) for service in services[:SELECT_MAX]]
        self.truncated = len(services) > SELECT_MAX
        self.service_index: int | None = None
        self.quantity: int | None = None
        self.notice: str | None = None
        self._build()

    @property
    def chosen(self) -> dict | None:
        if self.service_index is None or self.service_index >= len(self.services):
            return None
        return self.services[self.service_index]

    def _build(self) -> None:
        self.clear_items()
        self.service_select = discord.ui.Select(
            placeholder="Pick a service...",
            options=[
                option(service["name"], str(index), f"{service['cost']} Rare Alloys each", default=index == self.service_index)
                for index, service in enumerate(self.services)
            ],
            row=0,
        )
        self.service_select.callback = self._service_picked
        self.quantity_select = discord.ui.Select(
            placeholder="Pick a quantity...",
            options=[option(str(n), str(n), default=n == self.quantity) for n in QUANTITY_CHOICES],
            row=1,
        )
        self.quantity_select.callback = self._quantity_picked
        self.confirm_button = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.success,
            disabled=self.chosen is None or self.quantity is None,
            row=2,
        )
        self.confirm_button.callback = self._confirm
        self.add_item(self.service_select)
        self.add_item(self.quantity_select)
        self.add_item(self.confirm_button)

    def content(self) -> str:
        lines = [f"**Order a service for ticket {ticket_label(self.number)}**"]
        if self.notice:
            lines.append(self.notice)
        service = self.chosen
        if service is None and self.quantity is None:
            lines.append("Pick a service, then a quantity.")
        elif service is None:
            lines.append(f"Quantity: **{self.quantity}**. Now pick a service.")
        elif self.quantity is None:
            lines.append(f"**{md(service['name'])}** - {service['cost']} Rare Alloys each.\nNow pick a quantity.")
        else:
            lines.append(
                f"**{self.quantity}x {md(service['name'])}**\n"
                f"{service['cost']} Rare Alloys each - **{service['cost'] * self.quantity} Rare Alloys** total.\n"
                "Press **Confirm** to add this to the order."
            )
        if self.truncated:
            lines.append(f"Only the first {SELECT_MAX} services are listed.")
        return "\n".join(lines)

    async def start(self, interaction: discord.Interaction) -> None:
        await reply(interaction, self.content(), view=self)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await edit(interaction, content=self.content(), view=self)

    async def _service_picked(self, interaction: discord.Interaction) -> None:
        if await customer_gate(self.bot, interaction, self.guild_id, self.number) is None:
            return
        self.service_index = int(self.service_select.values[0])
        self.notice = None
        await self._redraw(interaction)

    async def _quantity_picked(self, interaction: discord.Interaction) -> None:
        if await customer_gate(self.bot, interaction, self.guild_id, self.number) is None:
            return
        self.quantity = int(self.quantity_select.values[0])
        self.notice = None
        await self._redraw(interaction)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        ticket = await customer_gate(self.bot, interaction, self.guild_id, self.number)
        if ticket is None:
            return
        service = self.chosen
        quantity = self.quantity
        if service is None or quantity is None:
            await self._redraw(interaction)
            return
        current = await get_service(self.bot, self.guild_id, service["name"])
        if current is None:
            self.services.pop(self.service_index)
            self.service_index = None
            if not self.services:
                await edit(interaction, content="No services are offered any more. Ask ticket staff for help.", view=None)
                return
            self.notice = f"**{md(service['name'])}** is no longer offered. Pick another service."
            await self._redraw(interaction)
            return
        if int(current["cost"]) != int(service["cost"]):
            service["cost"] = int(current["cost"])
            self.notice = (
                f"The price of **{md(service['name'])}** changed to **{service['cost']}** Rare Alloys each. "
                "Check the total and press **Confirm** again."
            )
            await self._redraw(interaction)
            return
        await add_item(self.bot, self.guild_id, self.number, service["name"], int(service["cost"]), quantity)
        summary = await summarize(self.bot, ticket)
        self.service_index = None
        self.quantity = None
        self.notice = (
            f"Added **{quantity}x {md(service['name'])}** ({quantity * service['cost']} Rare Alloys). "
            f"Order total is now **{summary.cost} Rare Alloys**.\nPick another service to keep ordering, or dismiss this message."
        )
        await self._redraw(interaction)
        await refresh_card(self.bot, self.guild_id, self.number)
        await rename_after_first_order(self.bot, ticket, service["name"])


class RemoveServiceView(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, number: int, lines: list[dict], owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.number = number
        self.lines = lines[:SELECT_MAX]
        self.select = discord.ui.Select(
            placeholder="Pick a line to remove...",
            options=[
                option(
                    f"{line['quantity']}x {line['service']}",
                    str(index),
                    f"{line['quantity'] * line['unit_cost']} Rare Alloys ({line['unit_cost']} each)",
                )
                for index, line in enumerate(self.lines)
            ],
        )
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        ticket = await customer_gate(self.bot, interaction, self.guild_id, self.number)
        if ticket is None:
            return
        line = self.lines[int(self.select.values[0])]
        removed = await remove_item(self.bot, self.guild_id, self.number, line["service"], line["unit_cost"])
        if removed is None:
            await edit(interaction, content="That line was already removed from the order.", view=None)
            return
        summary = await summarize(self.bot, ticket)
        await edit(
            interaction,
            content=(
                f"Removed **{removed['quantity']}x {md(removed['service'])}** from the order.\n"
                f"Order total is now **{summary.cost} Rare Alloys**."
            ),
            view=None,
        )
        await refresh_card(self.bot, self.guild_id, self.number)


async def close_ticket(bot: FoxBot, ticket: dict, closer: discord.abc.User) -> str | None:
    guild = bot.get_guild(ticket["guild_id"])
    if guild is None:
        return "I cannot see this server right now. Try again in a moment."
    channel = guild.get_channel(ticket["channel_id"]) if ticket["channel_id"] else None
    summary = await summarize(bot, ticket)
    closed_at = timeutil.now()
    try:
        text = await build_transcript(channel, summary, guild, closer.id, closed_at)
    except discord.HTTPException:
        log.warning("Could not read the history of ticket %s", ticket["number"], exc_info=True)
        return "I could not read this channel's history, so the ticket was not closed. I need the **Read Message History** permission here."
    try:
        _, log_channel = await ensure_ticket_channels(bot, guild)
    except SetupError as error:
        return f"{error} The ticket was not closed."
    try:
        await log_channel.send(
            embed=closed_embed(summary, closer.id, closed_at),
            file=transcript_file(text, ticket["number"]),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.warning("Could not post the transcript of ticket %s", ticket["number"], exc_info=True)
        return (
            f"I could not post the transcript in {log_channel.mention}, so the ticket was not closed. "
            "Check that I can send messages and attach files there."
        )
    await mark_closed(bot, ticket, closer.id, closed_at)
    if channel is None:
        return None
    try:
        await channel.delete(reason=f"Ticket {ticket_label(ticket['number'])} closed by {closer}")
    except discord.HTTPException as error:
        reason = "missing the Manage Channels permission" if isinstance(error, discord.Forbidden) else (error.text or str(error))
        return (
            f"Ticket closed and the transcript saved in {log_channel.mention}, but I could not delete this channel "
            f"({reason}). Please delete it manually."
        )
    return None


async def handle_status(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> None:
    ticket = await staff_gate(bot, interaction, guild_id, number)
    if ticket is None:
        return
    picker = StatusPicker(bot, guild_id, number, ticket["status"], interaction.user.id)
    await reply(interaction, f"Pick the new status for ticket **{ticket_label(number)}**:", view=picker)


async def handle_assign(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> None:
    ticket = await staff_gate(bot, interaction, guild_id, number)
    if ticket is None:
        return
    picker = AssignPicker(bot, guild_id, number, ticket["assigned_to"], interaction.user.id)
    await reply(interaction, f"Assign ticket **{ticket_label(number)}** to a ticket staff member:", view=picker)


async def handle_payment(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> None:
    if await staff_gate(bot, interaction, guild_id, number) is None:
        return
    await interaction.response.send_modal(PaymentModal(bot, guild_id, number))


async def handle_order(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> None:
    if await customer_gate(bot, interaction, guild_id, number) is None:
        return
    services = await list_services(bot, guild_id)
    if not services:
        await deny(interaction, "No services are configured yet. Ask an admin to add some in `/settings`.")
        return
    await OrderServiceView(bot, guild_id, number, services, interaction.user.id).start(interaction)


async def handle_remove(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> None:
    if await customer_gate(bot, interaction, guild_id, number) is None:
        return
    lines = await items(bot, guild_id, number)
    if not lines:
        await deny(interaction, "Nothing has been ordered on this ticket yet.")
        return
    note = f"\nOnly the first {SELECT_MAX} lines are listed." if len(lines) > SELECT_MAX else ""
    view = RemoveServiceView(bot, guild_id, number, lines, interaction.user.id)
    await reply(interaction, f"Pick the line to remove from the order:{note}", view=view)


async def handle_close(bot: FoxBot, interaction: discord.Interaction, guild_id: int, number: int) -> None:
    ticket = await customer_gate(bot, interaction, guild_id, number)
    if ticket is None:
        return

    async def confirmed(confirm_interaction: discord.Interaction) -> None:
        current = await customer_gate(bot, confirm_interaction, guild_id, number)
        if current is None:
            return
        await edit(confirm_interaction, content="Closing the ticket and saving the transcript...", view=None)
        problem = await close_ticket(bot, current, confirm_interaction.user)
        if problem:
            await reply(confirm_interaction, problem)

    view = ConfirmView(owner_id=interaction.user.id, on_confirm=confirmed, confirm_label="Close Ticket")
    await reply(
        interaction,
        f"Close ticket **{ticket_label(number)}** for **{md(ticket['regiment'])}**? "
        "The conversation is saved as a transcript in the ticket log and this channel is deleted.",
        view=view,
    )


HANDLERS: dict[str, Handler] = {
    "status": handle_status,
    "assign": handle_assign,
    "payment": handle_payment,
    "order": handle_order,
    "remove": handle_remove,
    "close": handle_close,
}


def assigned_text(row: dict) -> str:
    return f"<@{row['assigned_to']}>" if row["assigned_to"] else "unassigned"


def overview_lines(rows: list[dict]) -> list[str]:
    statuses = list(TICKET_STATUSES) + sorted({row["status"] for row in rows} - set(TICKET_STATUSES))
    lines: list[str] = []
    for status in statuses:
        group = [row for row in rows if row["status"] == status]
        if not group:
            continue
        if lines:
            lines.append("")
        lines.append(f"**{status}** ({len(group)})")
        for row in group:
            channel = f" - <#{row['channel_id']}>" if row["channel_id"] else ""
            lines.append(
                f"`{ticket_label(row['number'])}` **{md(row['regiment'])}** - {row['cost']} Rare Alloys - {assigned_text(row)}{channel}"
            )
    return lines


def overview_embeds(rows: list[dict]) -> list[discord.Embed]:
    total = sum(row["cost"] for row in rows)
    return paginate_embeds(
        "Ticket Overview",
        overview_lines(rows),
        colour=Colour.INFO,
        empty="No open tickets.",
        footer=f"{len(rows)} open ticket(s) - {total} Rare Alloys total",
    )


def rares_line(row: dict) -> str:
    cost, paid = row["cost"], row["paid"]
    if not row["line_count"]:
        state = "nothing ordered yet"
    elif paid >= cost:
        state = "paid in full"
    else:
        state = f"{cost - paid} owed"
    return f"`{ticket_label(row['number'])}` **{md(row['regiment'])}** - paid {paid}/{cost} - {state}"


def rares_embeds(rows: list[dict], closed: int) -> list[discord.Embed]:
    billed = sum(row["cost"] for row in rows)
    paid = sum(row["paid"] for row in rows)
    owed = sum(max(0, row["cost"] - row["paid"]) for row in rows)
    embeds = paginate_embeds(
        "Rare Alloys - Paid and Owed",
        [rares_line(row) for row in rows],
        colour=Colour.PRIVATE,
        empty="No open tickets.",
        footer=f"{len(rows)} open ticket(s) - {closed} closed",
    )
    for embed in embeds:
        embed.add_field(name="Total Billed", value=f"{billed} Rare Alloys", inline=True)
        embed.add_field(name="Total Paid", value=f"{paid} Rare Alloys", inline=True)
        embed.add_field(name="Total Owed", value=f"{owed} Rare Alloys" if owed else "Nothing outstanding", inline=True)
    return embeds


def leaderboard_ranking(rows: list[dict]) -> list[dict]:
    stats: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r["number"]):
        key = regiment_key(row["regiment"])
        entry = stats.setdefault(key, {"name": " ".join(row["regiment"].split()), "paid": 0})
        entry["paid"] += row["paid"]
    return sorted(stats.values(), key=lambda entry: (-entry["paid"], entry["name"].casefold()))


def leaderboard_embeds(rows: list[dict]) -> list[discord.Embed]:
    ranking = leaderboard_ranking(rows)
    lines = [f"{position}. {md(entry['name'])} - {entry['paid']} Rare Alloys" for position, entry in enumerate(ranking, start=1)]
    embeds = paginate_embeds(
        "Regiment Leaderboard - Rare Alloys Spent",
        lines,
        colour=Colour.GOLD,
        empty="No tickets on record yet.",
    )
    total = sum(entry["paid"] for entry in ranking)
    for embed in embeds:
        embed.add_field(name="Regiments", value=str(len(ranking)), inline=True)
        embed.add_field(name="Total Spent", value=f"{total} Rare Alloys", inline=True)
    return embeds


def mine_lines(rows: list[dict]) -> list[str]:
    open_rows = [row for row in rows if row["closed_at"] is None]
    closed_rows = [row for row in rows if row["closed_at"] is not None]
    lines: list[str] = []
    if open_rows:
        lines.append("**Open tickets**")
        for row in open_rows:
            channel = f" - <#{row['channel_id']}>" if row["channel_id"] else ""
            lines.append(
                f"`{ticket_label(row['number'])}` **{md(row['regiment'])}** - {row['status']} - cost {row['cost']}, "
                f"paid {row['paid']}, owed {max(0, row['cost'] - row['paid'])} Rare Alloys{channel}"
            )
    if closed_rows:
        if lines:
            lines.append("")
        lines.append("**Closed tickets**")
        for row in reversed(closed_rows):
            lines.append(
                f"`{ticket_label(row['number'])}` **{md(row['regiment'])}** - closed {timeutil.short_date(row['closed_at'])} - "
                f"cost {row['cost']}, paid {row['paid']}, owed {max(0, row['cost'] - row['paid'])} Rare Alloys"
            )
    return lines


class Tickets(commands.Cog):
    group = app_commands.Group(name="ticket", description="Order services from the regiment through private tickets.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(TicketButton)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        ticket = await fetch_by_channel(self.bot, channel.id)
        if ticket is None:
            return
        if not await mark_closed(self.bot, ticket, None, timeutil.now()):
            return
        log.info("Ticket %s in guild %s was closed because its channel was deleted", ticket["number"], ticket["guild_id"])
        log_channel_id = await self.bot.settings.get(ticket["guild_id"], keys.TICKET_LOG_CHANNEL)
        log_channel = channel.guild.get_channel(int(log_channel_id)) if log_channel_id else None
        if log_channel is None:
            return
        embed = discord.Embed(
            title=clip(f"Ticket {ticket_label(ticket['number'])} closed - {ticket['regiment']}", 256),
            description="Its channel was deleted without the Close Ticket button, so no transcript was saved.",
            colour=Colour.DARK,
        )
        try:
            await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @group.command(name="open", description="Open a private ticket to order services from the regiment.")
    async def open(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await deny(interaction, "Tickets can only be opened inside a server.")
            return
        if not await list_services(self.bot, interaction.guild_id):
            await deny(interaction, NO_SERVICES)
            return
        await interaction.response.send_modal(OpenTicketModal(self.bot))

    @group.command(name="overview", description="Staff: every open ticket grouped by status (only you see it).")
    async def overview(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP):
            return
        rows = await ticket_rows(self.bot, interaction.guild_id, open_only=True)
        await EmbedPaginator(overview_embeds(rows), owner_id=interaction.user.id).start(interaction)

    @group.command(name="rares", description="Staff: Rare Alloys paid and owed on open tickets (only you see it).")
    async def rares(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP):
            return
        rows = await ticket_rows(self.bot, interaction.guild_id, open_only=True)
        closed = await closed_count(self.bot, interaction.guild_id)
        await EmbedPaginator(rares_embeds(rows, closed), owner_id=interaction.user.id).start(interaction)

    @group.command(name="leaderboard", description="Staff: regiments ranked by Rare Alloys spent (only you see it).")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP):
            return
        rows = await ticket_rows(self.bot, interaction.guild_id)
        await EmbedPaginator(leaderboard_embeds(rows), owner_id=interaction.user.id).start(interaction)

    @group.command(name="mine", description="Your own tickets with costs and payments (only you see it).")
    async def mine(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await deny(interaction, "This only works inside a server.")
            return
        rows = await ticket_rows(self.bot, interaction.guild_id, opened_by=interaction.user.id)
        embeds = paginate_embeds(
            "Your Tickets",
            mine_lines(rows),
            colour=Colour.INFO,
            empty="You have not opened any tickets yet. Use `/ticket open` to order services.",
            footer=f"{len(rows)} ticket(s)",
        )
        await EmbedPaginator(embeds, owner_id=interaction.user.id).start(interaction)


async def setup(bot: FoxBot) -> None:
    bot.add_dynamic_items(TicketButton)
    await bot.add_cog(Tickets(bot))
