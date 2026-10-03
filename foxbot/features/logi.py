from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import PRIORITIES, PRIORITY_ORDER, Colour
from foxbot.core import entries, ids, timeutil
from foxbot.core import settings as keys
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.locations import Location
from foxbot.core.locpicker import LocationPicker
from foxbot.core.permissions import GROUPS, check_admin, check_group, interaction_member, resolve_member
from foxbot.core.text import EMBED_FIELD_LIMIT, chunk_lines, clip, fit_board_embeds, md, paginate_embeds
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
    report_error,
    select_field,
    select_value,
    text_field,
    text_value,
)

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

KIND = "logi"
GROUP = "logi"

STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_DELIVERED = "delivered"
STATUS_CANCELLED = "cancelled"
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_CLAIMED)
FINISHED_STATUSES = (STATUS_DELIVERED, STATUS_CANCELLED)
STATUS_LABELS = {
    STATUS_OPEN: "Open",
    STATUS_CLAIMED: "Claimed",
    STATUS_DELIVERED: "Delivered",
    STATUS_CANCELLED: "Cancelled",
}
STATUS_COLOURS = {
    STATUS_CLAIMED: Colour.INFO,
    STATUS_DELIVERED: Colour.SUCCESS,
    STATUS_CANCELLED: Colour.MUTED,
}

DEFAULT_PRIORITY = "Medium"
TITLE_MAX = 80
CARGO_MAX = 1000
NOTES_MAX = 500
LOCATION_MAX = 100
HEX_MAX = 60
HISTORY_DAYS = 7
ROUTE_PART_LIMIT = 60
MESSAGE_LIMIT = 2000
LIST_PAGE_CHARS = 3800

PING_BUTTONS = {
    "claim": ("Claim", discord.ButtonStyle.success),
    "view": ("Details", discord.ButtonStyle.secondary),
}

NO_ACCESS = f"You need the **{GROUPS[GROUP]}** permission for that. Ask an admin to grant it with `/permissions`."
NEEDS_DETAILS = "A logi run needs a title and some cargo."
NEEDS_DESTINATION = "A logi run needs a destination."


@dataclass
class Outcome:
    notice: str
    run: dict | None = None
    denied: bool = False


@dataclass
class PingResult:
    channel: discord.abc.Messageable | None
    role: discord.Role | None
    tried: bool


async def fetch(bot: FoxBot, guild_id: int, run_id: str) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM logi_runs WHERE guild_id = ? AND id = ?", (guild_id, run_id))


async def list_active(bot: FoxBot, guild_id: int) -> list[dict]:
    rows = await bot.db.fetchall(
        "SELECT * FROM logi_runs WHERE guild_id = ? AND status IN (?, ?)",
        (guild_id, *ACTIVE_STATUSES),
    )
    return sorted(rows, key=urgency_key)


async def list_history(bot: FoxBot, guild_id: int, since: int) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT * FROM logi_runs WHERE guild_id = ? AND status IN (?, ?) "
        "AND COALESCE(delivered_at, cancelled_at, updated_at) >= ? "
        "ORDER BY COALESCE(delivered_at, cancelled_at, updated_at) DESC, rowid DESC",
        (guild_id, *FINISHED_STATUSES, since),
    )


async def create(
    bot: FoxBot,
    guild_id: int,
    *,
    title: str,
    cargo: str,
    pickup_hex: str,
    pickup_region: str,
    dest_hex: str,
    dest_region: str,
    priority: str,
    notes: str,
    user_id: int,
) -> dict:
    run_id = await ids.unique_id(bot.db, "logi_runs")
    now = timeutil.now()
    await bot.db.execute(
        "INSERT INTO logi_runs (id, guild_id, title, cargo, pickup_hex, pickup_region, dest_hex, dest_region, priority, notes, "
        "status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, guild_id, title, cargo, pickup_hex, pickup_region, dest_hex, dest_region, priority, notes, STATUS_OPEN, user_id, now, now),
    )
    bot.boards.request_refresh(guild_id, KIND)
    return await fetch(bot, guild_id, run_id)


async def update(bot: FoxBot, run: dict, **fields) -> dict | None:
    fields["updated_at"] = timeutil.now()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    changed = await bot.db.execute(
        f"UPDATE logi_runs SET {assignments} WHERE guild_id = ? AND id = ? AND status IN (?, ?)",
        (*fields.values(), run["guild_id"], run["id"], *ACTIVE_STATUSES),
    )
    if not changed:
        return None
    bot.boards.request_refresh(run["guild_id"], KIND)
    return await fetch(bot, run["guild_id"], run["id"])


async def transition(bot: FoxBot, run: dict, status: str, **fields) -> dict | None:
    values = {"status": status, **fields, "updated_at": timeutil.now()}
    assignments = ", ".join(f"{column} = ?" for column in values)
    changed = await bot.db.execute(
        f"UPDATE logi_runs SET {assignments} WHERE guild_id = ? AND id = ? AND status = ? AND claimed_by IS ?",
        (*values.values(), run["guild_id"], run["id"], run["status"], run["claimed_by"]),
    )
    if not changed:
        return None
    bot.boards.request_refresh(run["guild_id"], KIND)
    return await fetch(bot, run["guild_id"], run["id"])


async def claim(bot: FoxBot, run: dict, user_id: int) -> dict | None:
    if run["status"] != STATUS_OPEN:
        return None
    return await transition(bot, run, STATUS_CLAIMED, claimed_by=user_id, claimed_at=timeutil.now())


async def unclaim(bot: FoxBot, run: dict) -> dict | None:
    if run["status"] != STATUS_CLAIMED:
        return None
    return await transition(bot, run, STATUS_OPEN, claimed_by=None, claimed_at=None)


async def deliver(bot: FoxBot, run: dict, user_id: int) -> dict | None:
    if run["status"] not in ACTIVE_STATUSES:
        return None
    return await transition(bot, run, STATUS_DELIVERED, delivered_by=user_id, delivered_at=timeutil.now())


async def cancel(bot: FoxBot, run: dict, user_id: int) -> dict | None:
    if run["status"] not in ACTIVE_STATUSES:
        return None
    return await transition(bot, run, STATUS_CANCELLED, cancelled_by=user_id, cancelled_at=timeutil.now())


async def can_access(bot: FoxBot, member: discord.Member, run: dict) -> bool:
    if member.id in (run["created_by"], run["claimed_by"]):
        return True
    return await bot.perms.has(member, GROUP)


async def can_manage(bot: FoxBot, member: discord.Member, run: dict) -> bool:
    return member.id == run["created_by"] or await bot.perms.is_admin(member)


async def can_unclaim(bot: FoxBot, member: discord.Member, run: dict) -> bool:
    return member.id == run["claimed_by"] or await bot.perms.is_admin(member)


async def can_deliver(bot: FoxBot, member: discord.Member, run: dict) -> bool:
    return member.id in (run["created_by"], run["claimed_by"]) or await bot.perms.is_admin(member)


def location_text(hex_name: str, region: str) -> str:
    if not (hex_name or region):
        return ""
    return entries.location_label(hex_name, region)


def pickup_of(run: dict) -> str:
    return location_text(run["pickup_hex"], run["pickup_region"])


def destination_of(run: dict) -> str:
    return location_text(run["dest_hex"], run["dest_region"]) or entries.UNKNOWN_HEX


def route_text(run: dict) -> str:
    destination = clip(destination_of(run), ROUTE_PART_LIMIT)
    pickup = pickup_of(run)
    if pickup:
        return f"{clip(pickup, ROUTE_PART_LIMIT)} -> {destination}"
    return f"to {destination}"


def finished_at(run: dict) -> int:
    if run["status"] == STATUS_DELIVERED and run.get("delivered_at"):
        return run["delivered_at"]
    if run["status"] == STATUS_CANCELLED and run.get("cancelled_at"):
        return run["cancelled_at"]
    return run["updated_at"]


def urgency_key(run: dict) -> tuple:
    claimed = run["status"] == STATUS_CLAIMED
    since = (run.get("claimed_at") or run["created_at"]) if claimed else run["created_at"]
    return (claimed, PRIORITY_ORDER.get(run["priority"], len(PRIORITY_ORDER)), since, run["id"])


def user_since(user_id: int | None, ts: int | None) -> str:
    who = f"<@{user_id}>" if user_id else "someone"
    return f"{who} {timeutil.relative(ts)}" if ts else who


def status_text(run: dict) -> str:
    status = run["status"]
    if status == STATUS_CLAIMED:
        return f"Claimed by {user_since(run['claimed_by'], run['claimed_at'])}"
    if status == STATUS_DELIVERED:
        return f"Delivered by {user_since(run['delivered_by'], run['delivered_at'])}"
    if status == STATUS_CANCELLED:
        return f"Cancelled by {user_since(run['cancelled_by'], run['cancelled_at'])}"
    return "Open - waiting for a driver"


def progress_text(run: dict) -> str:
    if run["status"] == STATUS_CLAIMED:
        return f"claimed by {user_since(run['claimed_by'], run['claimed_at'])}"
    if run["status"] == STATUS_OPEN:
        return f"open, requested {timeutil.relative(run['created_at'])}"
    return status_text(run)


def state_label(run: dict) -> str:
    return f"{STATUS_LABELS.get(run['status'], run['status'])} | {run['priority']}"


def state_notice(run: dict, user_id: int) -> str:
    status = run["status"]
    if status == STATUS_CLAIMED:
        if run["claimed_by"] == user_id:
            return "You have already claimed this run."
        when = f" {timeutil.relative(run['claimed_at'])}" if run["claimed_at"] else ""
        return f"Too late: <@{run['claimed_by']}> claimed this run{when}."
    if status == STATUS_DELIVERED:
        return f"This run was already delivered by {user_since(run['delivered_by'], run['delivered_at'])}."
    if status == STATUS_CANCELLED:
        return f"This run was cancelled by {user_since(run['cancelled_by'], run['cancelled_at'])}."
    return "This run is open: nobody has claimed it."


def title_text(run: dict) -> str:
    return md(clip(run["title"], TITLE_MAX))


def board_line(run: dict) -> str:
    head = f"**{title_text(run)}** - {md(route_text(run))}"
    if run["status"] == STATUS_CLAIMED:
        return f"{head} - <@{run['claimed_by']}> since {timeutil.relative(run['claimed_at'] or run['created_at'])}"
    return f"{head} - requested {timeutil.relative(run['created_at'])}"


def board_lines(rows: list[dict]) -> list[str]:
    open_rows = sorted((row for row in rows if row["status"] == STATUS_OPEN), key=urgency_key)
    claimed_rows = sorted((row for row in rows if row["status"] == STATUS_CLAIMED), key=urgency_key)
    lines: list[str] = []
    for priority in PRIORITIES:
        group = [row for row in open_rows if row["priority"] == priority]
        if group:
            lines.append(f"**{priority} priority** ({len(group)})")
            lines.extend(board_line(row) for row in group)
    others = [row for row in open_rows if row["priority"] not in PRIORITIES]
    if others:
        lines.append(f"**Other** ({len(others)})")
        lines.extend(board_line(row) for row in others)
    if claimed_rows:
        lines.append(f"**Claimed** ({len(claimed_rows)})")
        lines.extend(board_line(row) for row in claimed_rows)
    return lines


def history_line(run: dict) -> str:
    verb = "Delivered" if run["status"] == STATUS_DELIVERED else "Cancelled"
    actor = run["delivered_by"] if run["status"] == STATUS_DELIVERED else run["cancelled_by"]
    who = f" by <@{actor}>" if actor else ""
    return (
        f"{verb} {timeutil.relative(finished_at(run))}{who}: **{title_text(run)}** - {md(route_text(run))} "
        f"(requested by <@{run['created_by']}>)"
    )


def option_description(run: dict) -> str:
    return f"{state_label(run)} | {route_text(run)}"


def counts(rows: list[dict]) -> tuple[int, int]:
    open_count = sum(1 for row in rows if row["status"] == STATUS_OPEN)
    return open_count, len(rows) - open_count


def card_colour(run: dict) -> int:
    if run["status"] == STATUS_OPEN:
        return PRIORITIES.get(run["priority"], Colour.MUTED)
    return STATUS_COLOURS.get(run["status"], Colour.MUTED)


def card_embed(run: dict) -> discord.Embed:
    active = run["status"] in ACTIVE_STATUSES
    prefix = "" if active else f"[{STATUS_LABELS.get(run['status'], run['status'])}] "
    embed = discord.Embed(title=clip(prefix + run["title"], 256), description=md(route_text(run)), colour=card_colour(run))
    embed.add_field(name="Cargo", value=clip(md(run["cargo"]), EMBED_FIELD_LIMIT) or "Not set", inline=False)
    embed.add_field(name="Pickup", value=clip(md(pickup_of(run)), EMBED_FIELD_LIMIT) or "Not set", inline=True)
    embed.add_field(name="Destination", value=clip(md(destination_of(run)), EMBED_FIELD_LIMIT), inline=True)
    embed.add_field(name="Priority", value=run["priority"], inline=True)
    embed.add_field(name="Status", value=status_text(run), inline=False)
    if run["notes"]:
        embed.add_field(name="Notes", value=clip(md(run["notes"]), EMBED_FIELD_LIMIT), inline=False)
    embed.add_field(name="Requested by", value=user_since(run["created_by"], run["created_at"]), inline=False)
    embed.set_footer(text=f"Run {run['id']} | Only you see this panel")
    return embed


def ping_content(run: dict, role: discord.Role | None) -> str:
    text = f"New logi run: **{title_text(run)}** - {md(route_text(run))} [{run['priority']}]"
    if role is not None:
        text = f"{role.mention} {text}"
    return clip(text, MESSAGE_LIMIT)


def ping_embed(run: dict) -> discord.Embed:
    embed = discord.Embed(title=clip(run["title"], 256), description=clip(md(run["cargo"]), EMBED_FIELD_LIMIT) or None, colour=card_colour(run))
    embed.add_field(name="Pickup", value=clip(md(pickup_of(run)), EMBED_FIELD_LIMIT) or "Not set", inline=True)
    embed.add_field(name="Destination", value=clip(md(destination_of(run)), EMBED_FIELD_LIMIT), inline=True)
    embed.add_field(name="Priority", value=run["priority"], inline=True)
    if run["notes"]:
        embed.add_field(name="Notes", value=clip(md(run["notes"]), EMBED_FIELD_LIMIT), inline=False)
    embed.add_field(name="Requested by", value=f"<@{run['created_by']}>", inline=False)
    embed.set_footer(text=f"Run {run['id']} | This message is removed once a driver claims the run")
    return embed


def ping_view(run: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in PING_BUTTONS:
        view.add_item(PingButtonItem(action, run["id"]).item)
    return view


def ping_mentions(role: discord.Role | None) -> discord.AllowedMentions:
    return discord.AllowedMentions(everyone=False, users=False, roles=[role] if role is not None else False)


async def logistics_role(bot: FoxBot, guild: discord.Guild) -> discord.Role | None:
    role_id = await bot.settings.get(guild.id, keys.LOGISTICS_ROLE)
    return guild.get_role(int(role_id)) if role_id else None


async def ping_channels(bot: FoxBot, guild: discord.Guild) -> list:
    found = []
    for channel_id in (await bot.boards.channel_id_for(guild.id, KIND), await bot.settings.get(guild.id, keys.ALERT_CHANNEL)):
        if not channel_id:
            continue
        channel = guild.get_channel_or_thread(int(channel_id))
        if channel is not None and all(existing.id != channel.id for existing in found):
            found.append(channel)
    return found


async def clear_ping_ids(bot: FoxBot, run: dict) -> None:
    await bot.db.execute(
        "UPDATE logi_runs SET ping_channel_id = NULL, ping_message_id = NULL WHERE id = ? AND ping_message_id = ?",
        (run["id"], run["ping_message_id"]),
    )


async def post_ping(bot: FoxBot, run: dict) -> PingResult:
    guild = bot.get_guild(run["guild_id"])
    if guild is None:
        return PingResult(None, None, False)
    role = await logistics_role(bot, guild)
    channels = await ping_channels(bot, guild)
    for channel in channels:
        try:
            message = await channel.send(
                content=ping_content(run, role),
                embed=ping_embed(run),
                view=ping_view(run),
                allowed_mentions=ping_mentions(role),
            )
        except discord.HTTPException:
            log.warning("Could not post the ping of logi run %s in channel %s", run["id"], channel.id, exc_info=True)
            continue
        await bot.db.execute(
            "UPDATE logi_runs SET ping_channel_id = ?, ping_message_id = ? WHERE id = ?",
            (message.channel.id, message.id, run["id"]),
        )
        current = await fetch(bot, run["guild_id"], run["id"])
        if current is not None and current["status"] != STATUS_OPEN:
            await delete_ping(bot, current)
        return PingResult(channel, role, True)
    return PingResult(None, role, bool(channels))


async def delete_ping(bot: FoxBot, run: dict) -> None:
    if not run.get("ping_message_id"):
        return
    await clear_ping_ids(bot, run)
    try:
        await bot.get_partial_messageable(run["ping_channel_id"]).get_partial_message(run["ping_message_id"]).delete()
    except discord.HTTPException:
        pass


async def refresh_ping(bot: FoxBot, run: dict) -> None:
    if run["status"] != STATUS_OPEN or not run.get("ping_message_id"):
        return
    guild = bot.get_guild(run["guild_id"])
    role = await logistics_role(bot, guild) if guild is not None else None
    message = bot.get_partial_messageable(run["ping_channel_id"]).get_partial_message(run["ping_message_id"])
    try:
        await message.edit(content=ping_content(run, role), embed=ping_embed(run), view=ping_view(run), allowed_mentions=ping_mentions(role))
    except discord.NotFound:
        await clear_ping_ids(bot, run)
    except discord.HTTPException:
        log.warning("Could not update the ping of logi run %s", run["id"], exc_info=True)


async def notify_requester(bot: FoxBot, run: dict, text: str) -> None:
    member = await resolve_member(bot, run["guild_id"], run["created_by"])
    if member is None or member.bot:
        return
    embed = discord.Embed(title=clip(f"Logi run: {run['title']}", 256), description=text, colour=card_colour(run))
    guild = bot.get_guild(run["guild_id"])
    if guild is not None:
        embed.set_author(name=guild.name)
    embed.set_footer(text=f"Run {run['id']}")
    try:
        await member.send(embed=embed)
    except discord.HTTPException:
        log.info("Could not DM user %s about logi run %s", run["created_by"], run["id"])


async def after_change(bot: FoxBot, run: dict, actor_id: int) -> None:
    if run["status"] != STATUS_OPEN:
        await delete_ping(bot, run)
    if run["created_by"] == actor_id:
        return
    summary = f"**{title_text(run)}** ({md(route_text(run))})"
    if run["status"] == STATUS_CLAIMED:
        await notify_requester(bot, run, f"Your logi run {summary} was claimed by <@{run['claimed_by']}>.")
    elif run["status"] == STATUS_DELIVERED:
        await notify_requester(bot, run, f"Your logi run {summary} was marked delivered by <@{run['delivered_by']}>.")


def location_feedback(label: str, location: Location) -> str:
    text = entries.location_label(location.hex, location.region)
    if location.known:
        return f"{label}: **{md(text)}**"
    return f"{label} **{md(text)}** was not found on the war map, so it was saved exactly as typed. Use **Edit** to fix it."


def ping_feedback(result: PingResult) -> str:
    if result.channel is None:
        if result.tried:
            return "I could not post the request in the logi board or alerts channel, so nobody was pinged."
        return "No logi board or alerts channel is set, so nobody was pinged. An admin can post the board with `/logi board`."
    if result.role is None:
        return f"Posted in {result.channel.mention}. No logistics role is set in `/settings`, so nobody was pinged."
    return f"The logistics role was pinged in {result.channel.mention}."


async def announce(bot: FoxBot, run: dict, *, destination: Location, pickup: Location | None) -> str:
    parts = [f"Logi run **{title_text(run)}** requested.", location_feedback("Destination", destination)]
    if pickup is not None:
        parts.append(location_feedback("Pickup", pickup))
    parts.append(ping_feedback(await post_ping(bot, run)))
    return clip("\n".join(parts), MESSAGE_LIMIT)


def resolve_destination(bot: FoxBot, hex_text: str, town_text: str | None) -> Location:
    if town_text and town_text.strip():
        return bot.locations.resolve(hex_text, town_text)
    location = bot.locations.resolve(hex_text, None)
    if location.known:
        return location
    return bot.locations.resolve_text(hex_text)


def resolve_pickup(bot: FoxBot, text: str | None) -> Location | None:
    text = (text or "").strip()
    if not text:
        return None
    location = bot.locations.resolve_text(text)
    return location if (location.hex or location.region) else None


def resolve_changed(bot: FoxBot, text: str, shown: str, hex_name: str, region: str) -> tuple[Location, bool]:
    if text == shown or text == location_text(hex_name, region):
        return Location(hex_name, region, True), False
    if not text:
        return Location("", "", True), bool(hex_name or region)
    return bot.locations.resolve_text(text), True


def shown_location(hex_name: str, region: str) -> str:
    return clip(location_text(hex_name, region), LOCATION_MAX)


def changed_notice(run: dict) -> str:
    return f"This run changed after this panel was shown, so nothing was done. Now: {status_text(run)}."


async def attempt_claim(bot: FoxBot, member: discord.Member, run: dict) -> Outcome:
    if not await bot.perms.has(member, GROUP):
        return Outcome(f"You need the **{GROUPS[GROUP]}** permission to claim logi runs.", denied=True)
    if run["status"] != STATUS_OPEN:
        return Outcome(state_notice(run, member.id))
    claimed = await claim(bot, run, member.id)
    if claimed is None:
        current = await fetch(bot, run["guild_id"], run["id"]) or run
        return Outcome(state_notice(current, member.id))
    return Outcome(f"You claimed **{title_text(claimed)}**. Press **Mark Delivered** once the cargo is dropped off.", claimed)


async def attempt_unclaim(bot: FoxBot, member: discord.Member, run: dict) -> Outcome:
    if run["status"] != STATUS_CLAIMED:
        return Outcome(state_notice(run, member.id))
    if not await can_unclaim(bot, member, run):
        return Outcome("Only the driver who claimed this run or an admin can unclaim it.", denied=True)
    released = await unclaim(bot, run)
    if released is None:
        current = await fetch(bot, run["guild_id"], run["id"]) or run
        return Outcome(state_notice(current, member.id))
    whose = "your claim" if run["claimed_by"] == member.id else f"the claim of <@{run['claimed_by']}>"
    return Outcome(f"Released {whose}. **{title_text(released)}** is open again for any driver.", released)


async def attempt_deliver(bot: FoxBot, member: discord.Member, run: dict) -> Outcome:
    if run["status"] not in ACTIVE_STATUSES:
        return Outcome(state_notice(run, member.id))
    if not await can_deliver(bot, member, run):
        return Outcome("Only the driver who claimed this run, the requester or an admin can mark it delivered.", denied=True)
    delivered = await deliver(bot, run, member.id)
    if delivered is None:
        current = await fetch(bot, run["guild_id"], run["id"]) or run
        return Outcome(state_notice(current, member.id))
    return Outcome(f"Marked **{title_text(delivered)}** as delivered. Thanks for the run.", delivered)


async def manage_problem(bot: FoxBot, member: discord.Member, run: dict) -> str | None:
    if run["status"] not in ACTIVE_STATUSES:
        return f"{state_notice(run, member.id)} It can no longer be changed."
    if not await can_manage(bot, member, run):
        return "Only the requester or an admin can change or cancel this run."
    return None


def keep_or_cancel_view(owner_id: int, on_confirm, on_keep) -> ConfirmView:
    view = ConfirmView(owner_id=owner_id, on_confirm=on_confirm, on_cancel=on_keep, confirm_label="Cancel Run")
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label == "Cancel":
            item.label = "Keep Run"
    return view


class RunModal(BaseModal):
    def __init__(
        self,
        bot: FoxBot,
        guild_id: int,
        *,
        destination: Location | None = None,
        entry: dict | None = None,
        panel: RunPanel | None = None,
    ):
        super().__init__(title="Edit Logi Run" if entry is not None else "Request a Logi Run")
        self.bot = bot
        self.guild_id = guild_id
        self.destination = destination
        self.entry = entry
        self.panel = panel
        self.shown_pickup = shown_location(entry["pickup_hex"], entry["pickup_region"]) if entry else ""
        self.shown_destination = shown_location(entry["dest_hex"], entry["dest_region"]) if entry else ""
        self.title_field = text_field(
            "Title",
            default=clip(entry["title"], TITLE_MAX) if entry else None,
            placeholder="e.g. Bmats for the Pan's Villa bunker base",
            max_length=TITLE_MAX,
        )
        self.cargo_field = text_field(
            "Cargo",
            default=clip(entry["cargo"], CARGO_MAX) if entry else None,
            placeholder="60 Bmats\n20 Rifle crates",
            description="What to haul. One item per line is easiest to read.",
            paragraph=True,
            max_length=CARGO_MAX,
        )
        self.pickup_field = text_field(
            "Pickup (optional)",
            default=self.shown_pickup or None,
            placeholder="e.g. Syrinx Pass. Leave empty if any depot will do.",
            description="Town or region. Add the hex after a comma if it is ambiguous.",
            required=False,
            max_length=LOCATION_MAX,
        )
        self.destination_field = None
        self.priority_field = None
        if entry is not None:
            self.destination_field = text_field(
                "Destination",
                default=self.shown_destination or None,
                placeholder="e.g. Pan's Villa",
                description="Town or region. Add the hex after a comma if it is ambiguous.",
                max_length=LOCATION_MAX,
            )
        else:
            self.priority_field = select_field("Priority", list(PRIORITIES), default=DEFAULT_PRIORITY)
        self.notes_field = text_field(
            "Notes (optional)",
            default=clip(entry["notes"], NOTES_MAX) if entry and entry["notes"] else None,
            placeholder="e.g. Drop it in the seaport, ask for Fox on comms",
            required=False,
            paragraph=True,
            max_length=NOTES_MAX,
        )
        for item in (self.title_field, self.cargo_field, self.pickup_field, self.destination_field, self.priority_field, self.notes_field):
            if item is not None:
                self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.entry is None:
            await self._create(interaction)
        else:
            await self._update(interaction)

    async def _create(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP, self.guild_id):
            return
        title = text_value(self.title_field)
        cargo = text_value(self.cargo_field)
        if not title or not cargo:
            await deny(interaction, NEEDS_DETAILS)
            return
        destination = self.destination
        if destination is None or not (destination.hex or destination.region):
            await deny(interaction, NEEDS_DESTINATION)
            return
        priority = select_value(self.priority_field) if self.priority_field is not None else None
        if priority not in PRIORITIES:
            priority = DEFAULT_PRIORITY
        pickup = resolve_pickup(self.bot, text_value(self.pickup_field))
        await interaction.response.defer()
        run = await create(
            self.bot,
            self.guild_id,
            title=title,
            cargo=cargo,
            pickup_hex=pickup.hex if pickup else "",
            pickup_region=pickup.region if pickup else "",
            dest_hex=destination.hex,
            dest_region=destination.region,
            priority=priority,
            notes=text_value(self.notes_field),
            user_id=interaction.user.id,
        )
        notice = await announce(self.bot, run, destination=destination, pickup=pickup)
        await RunPanel(self.bot, self.guild_id, run["id"], interaction.user.id).show(interaction, notice=notice)

    async def _update(self, interaction: discord.Interaction) -> None:
        run = await fetch(self.bot, self.guild_id, self.entry["id"])
        if run is None:
            await deny(interaction, "This logi run no longer exists.")
            return
        member = await interaction_member(interaction, self.guild_id)
        if member is None:
            await deny(interaction, "This only works inside the server.")
            return
        problem = await manage_problem(self.bot, member, run)
        if problem:
            await deny(interaction, problem)
            return
        title = text_value(self.title_field)
        cargo = text_value(self.cargo_field)
        if not title or not cargo:
            await deny(interaction, NEEDS_DETAILS)
            return
        destination_text = text_value(self.destination_field)
        if not destination_text:
            await deny(interaction, NEEDS_DESTINATION)
            return
        destination, destination_changed = resolve_changed(
            self.bot, destination_text, self.shown_destination, run["dest_hex"], run["dest_region"]
        )
        if not (destination.hex or destination.region):
            await deny(interaction, NEEDS_DESTINATION)
            return
        pickup_text = text_value(self.pickup_field)
        pickup, pickup_changed = resolve_changed(self.bot, pickup_text, self.shown_pickup, run["pickup_hex"], run["pickup_region"])
        updated = await update(
            self.bot,
            run,
            title=title,
            cargo=cargo,
            notes=text_value(self.notes_field),
            pickup_hex=pickup.hex,
            pickup_region=pickup.region,
            dest_hex=destination.hex,
            dest_region=destination.region,
        )
        if updated is None:
            current = await fetch(self.bot, self.guild_id, run["id"]) or run
            await deny(interaction, f"{state_notice(current, interaction.user.id)} It can no longer be changed.")
            return
        parts = ["Saved."]
        if destination_changed:
            parts.append(location_feedback("Destination", destination))
        if pickup_changed:
            parts.append(location_feedback("Pickup", pickup) if pickup_text else "The pickup was removed.")
        notice = " ".join(parts)
        if self.panel is not None:
            await self.panel.show(interaction, notice=notice)
        else:
            await reply(interaction, notice)
        await refresh_ping(self.bot, updated)


class RunPanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, run_id: str, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.run_id = run_id
        self.seen: tuple[str, int | None] | None = None

    async def load(self, interaction: discord.Interaction) -> tuple[dict, discord.Member] | None:
        run = await fetch(self.bot, self.guild_id, self.run_id)
        if run is None:
            await edit(interaction, content="This logi run no longer exists.", embed=None, view=None)
            return None
        member = await interaction_member(interaction, self.guild_id)
        if member is None:
            await deny(interaction, "This only works inside the server.")
            return None
        if not await can_access(self.bot, member, run):
            await deny(interaction, NO_ACCESS)
            return None
        return run, member

    def _button(self, label: str, style: discord.ButtonStyle, callback, *, row: int) -> None:
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    async def still_as_shown(self, interaction: discord.Interaction, run: dict) -> bool:
        if self.seen is None or run["status"] not in ACTIVE_STATUSES or (run["status"], run["claimed_by"]) == self.seen:
            return True
        await self.show(interaction, notice=changed_notice(run))
        return False

    async def _build(self, run: dict, member: discord.Member | None) -> None:
        self.clear_items()
        self.seen = (run["status"], run["claimed_by"])
        if run["status"] in ACTIVE_STATUSES:
            if run["status"] == STATUS_OPEN:
                self._button("Claim", discord.ButtonStyle.success, self._claim, row=0)
            elif member is not None and await can_unclaim(self.bot, member, run):
                self._button("Unclaim", discord.ButtonStyle.secondary, self._unclaim, row=0)
            if member is not None and await can_deliver(self.bot, member, run):
                self._button("Mark Delivered", discord.ButtonStyle.primary, self._deliver, row=0)
            if member is not None and await can_manage(self.bot, member, run):
                self._button("Edit", discord.ButtonStyle.secondary, self._edit, row=1)
                self._button("Cancel Run", discord.ButtonStyle.danger, self._cancel, row=1)
                select = discord.ui.Select(
                    placeholder="Change the priority...",
                    options=[option(name, name, default=name == run["priority"]) for name in PRIORITIES],
                    row=2,
                )
                select.callback = lambda interaction, picked=select: self._priority(interaction, picked)
                self.add_item(select)
        self._button("Refresh", discord.ButtonStyle.secondary, self._refresh, row=1)

    async def render(self, interaction: discord.Interaction) -> discord.Embed | None:
        run = await fetch(self.bot, self.guild_id, self.run_id)
        if run is None:
            return None
        await self._build(run, await interaction_member(interaction, self.guild_id))
        return card_embed(run)

    async def send(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        embed = await self.render(interaction)
        if embed is None:
            await deny(interaction, "That logi run no longer exists.")
            return
        await reply(interaction, notice, embed=embed, view=self)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        embed = await self.render(interaction)
        if embed is None:
            await edit(interaction, content="This logi run no longer exists.", embed=None, view=None)
            return
        await edit(interaction, content=notice, embed=embed, view=self)

    async def _apply(self, interaction: discord.Interaction, outcome: Outcome) -> None:
        if outcome.denied:
            await deny(interaction, outcome.notice)
            return
        await self.show(interaction, notice=outcome.notice)
        if outcome.run is not None:
            await after_change(self.bot, outcome.run, interaction.user.id)

    async def _claim(self, interaction: discord.Interaction) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        run, member = loaded
        await self._apply(interaction, await attempt_claim(self.bot, member, run))

    async def _unclaim(self, interaction: discord.Interaction) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        run, member = loaded
        if not await self.still_as_shown(interaction, run):
            return
        await self._apply(interaction, await attempt_unclaim(self.bot, member, run))

    async def _deliver(self, interaction: discord.Interaction) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        run, member = loaded
        if not await self.still_as_shown(interaction, run):
            return
        await self._apply(interaction, await attempt_deliver(self.bot, member, run))

    async def _refresh(self, interaction: discord.Interaction) -> None:
        if await self.load(interaction) is None:
            return
        await self.show(interaction)

    async def _edit(self, interaction: discord.Interaction) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        run, member = loaded
        problem = await manage_problem(self.bot, member, run)
        if problem:
            await deny(interaction, problem)
            return
        await interaction.response.send_modal(RunModal(self.bot, self.guild_id, entry=run, panel=self))

    async def _priority(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        wanted = str(select.values[0]) if select.values else ""
        loaded = await self.load(interaction)
        if loaded is None:
            return
        run, member = loaded
        problem = await manage_problem(self.bot, member, run)
        if problem:
            await deny(interaction, problem)
            return
        if wanted not in PRIORITIES:
            await deny(interaction, "Pick one of the priorities.")
            return
        if wanted == run["priority"]:
            await self.show(interaction, notice=f"The priority is already {wanted}.")
            return
        updated = await update(self.bot, run, priority=wanted)
        if updated is None:
            current = await fetch(self.bot, self.guild_id, run["id"]) or run
            await self.show(interaction, notice=f"{state_notice(current, member.id)} It can no longer be changed.")
            return
        await self.show(interaction, notice=f"Priority set to **{wanted}**.")
        await refresh_ping(self.bot, updated)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        loaded = await self.load(interaction)
        if loaded is None:
            return
        run, member = loaded
        problem = await manage_problem(self.bot, member, run)
        if problem:
            await deny(interaction, problem)
            return

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            current = await self.load(confirm_interaction)
            if current is None:
                return
            fresh, actor = current
            blocked = await manage_problem(self.bot, actor, fresh)
            if blocked:
                await self.show(confirm_interaction, notice=blocked)
                return
            cancelled = await cancel(self.bot, fresh, actor.id)
            if cancelled is None:
                latest = await fetch(self.bot, self.guild_id, fresh["id"]) or fresh
                await self.show(confirm_interaction, notice=state_notice(latest, actor.id))
                return
            await self.show(confirm_interaction, notice=f"Cancelled **{title_text(cancelled)}**.")
            await after_change(self.bot, cancelled, actor.id)

        async def kept(keep_interaction: discord.Interaction) -> None:
            if await self.load(keep_interaction) is None:
                return
            await self.show(keep_interaction)

        await edit(
            interaction,
            content=f"Cancel the logi run **{title_text(run)}** ({md(route_text(run))})? Drivers will no longer see it.",
            embed=None,
            view=keep_or_cancel_view(interaction.user.id, confirmed, kept),
        )


async def open_panel(bot: FoxBot, interaction: discord.Interaction, run_id: str, *, replace: bool = False, notice: str | None = None) -> None:
    run = await fetch(bot, interaction.guild_id, run_id)
    if run is None:
        await deny(interaction, "That logi run no longer exists.")
        return
    member = await interaction_member(interaction)
    if member is None or not await can_access(bot, member, run):
        await deny(interaction, NO_ACCESS)
        return
    panel = RunPanel(bot, interaction.guild_id, run_id, interaction.user.id)
    if replace:
        await panel.show(interaction, notice=notice)
    else:
        await panel.send(interaction, notice=notice)


async def claim_from_ping(bot: FoxBot, interaction: discord.Interaction, run_id: str) -> None:
    run = await fetch(bot, interaction.guild_id, run_id)
    if run is None:
        await deny(interaction, "This logi run no longer exists.")
        return
    member = await interaction_member(interaction)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return
    outcome = await attempt_claim(bot, member, run)
    if outcome.run is None:
        await deny(interaction, outcome.notice)
        current = await fetch(bot, interaction.guild_id, run_id)
        if current is not None and current["status"] != STATUS_OPEN and interaction.message is not None:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass
        return
    await RunPanel(bot, interaction.guild_id, run_id, interaction.user.id).send(interaction, notice=outcome.notice)
    await after_change(bot, outcome.run, interaction.user.id)


class PingButtonItem(discord.ui.DynamicItem[discord.ui.Button], template=r"flr:(?P<action>claim|view):(?P<run>[A-Z0-9]+)"):
    def __init__(self, action: str, run_id: str):
        label, style = PING_BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"flr:{action}:{run_id}"))
        self.action = action
        self.run_id = run_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], match["run"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await deny(interaction, "This only works inside the server.")
            return
        try:
            if self.action == "claim":
                await claim_from_ping(interaction.client, interaction, self.run_id)
            else:
                await open_panel(interaction.client, interaction, self.run_id)
        except Exception as error:
            await report_error(interaction, error, f"logi ping {self.action}")


async def start_request(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    guild_id = interaction.guild_id

    async def picked(pick_interaction: discord.Interaction, location: Location) -> None:
        if not await check_group(pick_interaction, GROUP, guild_id):
            return
        await pick_interaction.response.send_modal(RunModal(bot, guild_id, destination=location))

    picker = await LocationPicker.create(bot, interaction, picked, prompt="**Request a logi run**\nWhere should the cargo go?")
    await picker.start(interaction)


async def show_list(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    rows = await list_active(bot, interaction.guild_id)
    if not rows:
        await reply(interaction, "There are no open or claimed logi runs. Use `/logi request` or the board's **Request Run** button to ask for one.")
        return
    open_count, claimed_count = counts(rows)
    pages = chunk_lines(board_lines(rows), LIST_PAGE_CHARS)
    embed = discord.Embed(title="Logi Runs", description=pages[0], colour=Colour.INFO)
    footer = f"{open_count} open, {claimed_count} claimed | Pick a run below to open its panel"
    if len(pages) > 1:
        footer = f"Not every run fits here. {footer}"
    embed.set_footer(text=footer)

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await open_panel(bot, pick_interaction, value)

    options = [PickOption(row["id"], row["title"], option_description(row)) for row in rows]
    view = PickerView(options, picked, owner_id=interaction.user.id, placeholder="Open a logi run...")
    await reply(interaction, embed=embed, view=view)


async def show_history(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    rows = await list_history(bot, interaction.guild_id, timeutil.now() - HISTORY_DAYS * 86400)
    delivered = sum(1 for row in rows if row["status"] == STATUS_DELIVERED)
    embeds = paginate_embeds(
        "Logi Run History",
        [history_line(row) for row in rows],
        colour=Colour.INFO,
        empty=f"No logi runs were delivered or cancelled in the last {HISTORY_DAYS} days.",
        per_page_chars=3000,
        footer=f"Last {HISTORY_DAYS} days: {delivered} delivered, {len(rows) - delivered} cancelled",
    )
    await EmbedPaginator(embeds, owner_id=interaction.user.id).start(interaction)


class LogiBoard:
    kind = KIND
    title = "Logi Runs"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        rows = await list_active(self.bot, guild.id)
        open_count, claimed_count = counts(rows)
        embeds = fit_board_embeds(
            "Logi Runs",
            board_lines(rows),
            colour=Colour.INFO,
            empty="No open logi runs. Press **Request Run** to ask for a delivery.",
            footer=f"{open_count} open, {claimed_count} claimed | Updates automatically",
            overflow_hint="Too many to show here. Use /logi list.",
        )
        options = [option(row["title"], row["id"], option_description(row)) for row in rows[:25]]
        return BoardRender(
            embeds=embeds,
            options=options,
            placeholder="Open a logi run (most urgent first)...",
            buttons=[
                BoardButton("request", "Request Run", discord.ButtonStyle.success),
                BoardButton("history", "History"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action == "request":
            await start_request(self.bot, interaction)
        elif action == "history":
            await show_history(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        await open_panel(self.bot, interaction, value)


class Logi(commands.Cog):
    group = app_commands.Group(name="logi", description="Logi run requests: ask for a delivery, claim it, mark it delivered.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot

    @group.command(name="request", description="Request a logi run and ping the logistics role once.")
    @app_commands.describe(
        title="Short name shown on the board, e.g. Bmats for the bunker base",
        cargo="What to haul, e.g. 60 Bmats, 20 Rifle crates",
        destination_hex="Destination hex (start typing to search)",
        destination_town="Destination town in that hex (start typing to search)",
        pickup="Where to pick the cargo up (optional, start typing to search)",
        priority="How urgent the run is (default Medium)",
        notes="Anything else the driver should know",
    )
    @app_commands.choices(priority=[app_commands.Choice(name=name, value=name) for name in PRIORITIES])
    async def request_command(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, TITLE_MAX],
        cargo: app_commands.Range[str, 1, CARGO_MAX],
        destination_hex: app_commands.Range[str, 1, HEX_MAX],
        destination_town: app_commands.Range[str, 1, LOCATION_MAX] | None = None,
        pickup: app_commands.Range[str, 1, LOCATION_MAX] | None = None,
        priority: app_commands.Choice[str] | None = None,
        notes: app_commands.Range[str, 1, NOTES_MAX] | None = None,
    ) -> None:
        if not await check_group(interaction, GROUP):
            return
        title = title.strip()
        cargo = cargo.strip()
        if not title or not cargo:
            await deny(interaction, NEEDS_DETAILS)
            return
        destination = resolve_destination(self.bot, destination_hex, destination_town)
        if not (destination.hex or destination.region):
            await deny(interaction, NEEDS_DESTINATION)
            return
        wanted = priority.value if priority is not None else DEFAULT_PRIORITY
        if wanted not in PRIORITIES:
            await deny(interaction, f"Unknown priority. Choose one of: {', '.join(PRIORITIES)}.")
            return
        pickup_location = resolve_pickup(self.bot, pickup)
        await interaction.response.defer(ephemeral=True, thinking=True)
        run = await create(
            self.bot,
            interaction.guild_id,
            title=title,
            cargo=cargo,
            pickup_hex=pickup_location.hex if pickup_location else "",
            pickup_region=pickup_location.region if pickup_location else "",
            dest_hex=destination.hex,
            dest_region=destination.region,
            priority=wanted,
            notes=(notes or "").strip(),
            user_id=interaction.user.id,
        )
        notice = await announce(self.bot, run, destination=destination, pickup=pickup_location)
        await RunPanel(self.bot, interaction.guild_id, run["id"], interaction.user.id).send(interaction, notice=notice)

    @request_command.autocomplete("destination_hex")
    async def destination_hex_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.hex_choices(current)

    @request_command.autocomplete("destination_town")
    async def destination_town_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.region_choices(getattr(interaction.namespace, "destination_hex", None), current)

    @request_command.autocomplete("pickup")
    async def pickup_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.location_choices(current)

    @group.command(name="list", description="List open and claimed logi runs (only you see it).")
    async def list_command(self, interaction: discord.Interaction) -> None:
        await show_list(self.bot, interaction)

    @group.command(name="history", description="Logi runs delivered or cancelled in the last 7 days (only you see it).")
    async def history_command(self, interaction: discord.Interaction) -> None:
        await show_history(self.bot, interaction)

    @group.command(name="board", description="Admin: post the live logi runs board in this channel (moves it if it exists).")
    async def board(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.boards.post(interaction.guild, KIND, interaction.channel)
        except discord.HTTPException as error:
            await reply(interaction, f"Could not post the board here: {error.text or error}")
            return
        role_id = await self.bot.settings.get(interaction.guild_id, keys.LOGISTICS_ROLE)
        hint = "" if role_id else " No logistics role is set in `/settings`, so new requests are posted here without a ping."
        await reply(interaction, f"Logi runs board posted. New requests are announced in this channel.{hint}")


async def setup(bot: FoxBot) -> None:
    bot.boards.register(LogiBoard(bot))
    bot.add_dynamic_items(PingButtonItem)
    await bot.add_cog(Logi(bot))
