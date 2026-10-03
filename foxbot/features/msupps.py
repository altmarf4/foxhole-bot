from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import Colour
from foxbot.core import entries, ids, timeutil
from foxbot.core import settings as keys
from foxbot.core.alerts import TimedEntry
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.locations import Location
from foxbot.core.locpicker import LocationPicker
from foxbot.core.permissions import check_admin, check_group, interaction_member
from foxbot.core.text import clip, fit_board_embeds, md, paginate_embeds
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

KIND = "msupps"
GROUP = "msupps"
MAX_MSUPPS = 1_000_000
MAX_RATE = 100_000
NAME_MAX = 60
LOCATION_MAX = 100
LOW_HOURS = 6

SavedCallback = Callable[[discord.Interaction, dict, str], Awaitable[None]]
AmountCallback = Callable[[discord.Interaction, int], Awaitable[None]]


def parse_amount(text: str) -> int | None:
    cleaned = re.sub(r"[\s,_']", "", text or "").lower()
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)k", cleaned)
    if match:
        return int(round(float(match.group(1)) * 1000))
    return None


def current_msupps(entry: dict, now: int | None = None) -> int:
    now = now if now is not None else timeutil.now()
    if entry["rate"] <= 0:
        return max(0, int(entry["msupps"]))
    elapsed = max(0, now - entry["measured_at"])
    return max(0, math.floor(entry["msupps"] - entry["rate"] * elapsed / 3600))


def run_out_at(entry: dict) -> int | None:
    if entry["rate"] <= 0:
        return None
    return int(entry["measured_at"] + max(0, entry["msupps"]) * 3600 // entry["rate"])


def is_out(entry: dict, now: int) -> bool:
    deadline = run_out_at(entry)
    return deadline is not None and deadline <= now


async def fetch(bot: FoxBot, guild_id: int, entry_id: str) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM msupps_bases WHERE guild_id = ? AND id = ?", (guild_id, entry_id))


async def list_entries(bot: FoxBot, guild_id: int) -> list[dict]:
    rows = await bot.db.fetchall("SELECT * FROM msupps_bases WHERE guild_id = ?", (guild_id,))
    return sorted(rows, key=sort_key)


async def sync_alerts(bot: FoxBot, entry: dict, now: int | None = None) -> None:
    deadline = run_out_at(entry)
    if deadline is None:
        await bot.alerts.forget(KIND, entry["id"])
    else:
        await bot.alerts.reset(KIND, entry["id"], entry["guild_id"], deadline, now)


async def create(
    bot: FoxBot,
    guild_id: int,
    *,
    name: str,
    hex_name: str,
    region: str,
    msupps: int,
    rate: int,
    user_id: int,
) -> dict:
    entry_id = await ids.unique_id(bot.db, "msupps_bases")
    now = timeutil.now()
    await bot.db.execute(
        "INSERT INTO msupps_bases (id, guild_id, name, hex, region, msupps, rate, measured_at, measured_by, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, guild_id, name, hex_name, region, int(msupps), int(rate), now, user_id, user_id, now, now),
    )
    row = await fetch(bot, guild_id, entry_id)
    await sync_alerts(bot, row, now)
    bot.boards.request_refresh(guild_id, KIND)
    return row


async def update(bot: FoxBot, entry: dict, **fields) -> dict | None:
    fields["updated_at"] = timeutil.now()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    await bot.db.execute(
        f"UPDATE msupps_bases SET {assignments} WHERE guild_id = ? AND id = ?",
        (*fields.values(), entry["guild_id"], entry["id"]),
    )
    bot.boards.request_refresh(entry["guild_id"], KIND)
    return await fetch(bot, entry["guild_id"], entry["id"])


async def remeasure(
    bot: FoxBot,
    entry: dict,
    user_id: int,
    *,
    observed: int | None = None,
    added: int = 0,
    rate: int | None = None,
    sync: bool = True,
) -> dict | None:
    now = timeutil.now()
    base = current_msupps(entry, now) if observed is None else observed
    amount = min(MAX_MSUPPS, max(0, int(base) + int(added)))
    new_rate = entry["rate"] if rate is None else int(rate)
    updated = await update(bot, entry, msupps=amount, rate=new_rate, measured_at=now, measured_by=user_id)
    if sync and updated is not None:
        await sync_alerts(bot, updated, now)
    return updated


async def add_msupps(bot: FoxBot, entry: dict, amount: int, user_id: int, *, sync: bool = True) -> dict | None:
    return await remeasure(bot, entry, user_id, added=amount, sync=sync)


async def set_msupps(bot: FoxBot, entry: dict, observed: int, user_id: int) -> dict | None:
    return await remeasure(bot, entry, user_id, observed=observed)


async def change_rate(bot: FoxBot, entry: dict, rate: int, user_id: int) -> dict | None:
    return await remeasure(bot, entry, user_id, rate=rate)


async def delete(bot: FoxBot, entry: dict) -> None:
    async with bot.db.transaction() as tx:
        await tx.execute("DELETE FROM msupps_bases WHERE guild_id = ? AND id = ?", (entry["guild_id"], entry["id"]))
        await entries.delete_children(tx, KIND, entry["id"])
    await bot.alerts.forget(KIND, entry["id"])
    bot.boards.request_refresh(entry["guild_id"], KIND)


async def can_access(bot: FoxBot, member: discord.Member, entry: dict) -> bool:
    return await bot.perms.has(member, GROUP)


async def visible_entries(bot: FoxBot, member: discord.Member) -> list[dict]:
    if not await bot.perms.has(member, GROUP):
        return []
    return await list_entries(bot, member.guild.id)


def location_of(entry: dict) -> str:
    return entries.location_label(entry["hex"], entry["region"])


def location_text(entry: dict) -> str:
    return Location(entry["hex"], entry["region"], True).label()


def measured_by(entry: dict) -> int:
    return entry.get("measured_by") or entry["created_by"]


def sort_key(entry: dict) -> tuple:
    deadline = run_out_at(entry)
    return (deadline is None, deadline or 0, entry["name"].lower())


def status_text(entry: dict, now: int | None = None) -> str:
    now = now if now is not None else timeutil.now()
    deadline = run_out_at(entry)
    if deadline is None:
        return f"{current_msupps(entry, now):,} msupps, rate not set"
    if deadline <= now:
        return f"**OUT OF MSUPPS** ({entry['rate']:,}/h)"
    return f"~{current_msupps(entry, now):,} msupps, {entry['rate']:,}/h, runs out {timeutil.relative(deadline)}"


def board_line(entry: dict, now: int | None = None) -> str:
    return f"**{md(entry['name'])}** {status_text(entry, now)} - <@{measured_by(entry)}>"


def option_description(entry: dict, now: int) -> str:
    deadline = run_out_at(entry)
    if deadline is None:
        return f"{current_msupps(entry, now):,} msupps | rate not set"
    if deadline <= now:
        return f"OUT OF MSUPPS | {entry['rate']:,}/h"
    return f"~{current_msupps(entry, now):,} msupps | {entry['rate']:,}/h | {timeutil.format_duration(deadline - now)} left"


def state_text(entry: dict, now: int) -> str:
    deadline = run_out_at(entry)
    if deadline is None:
        return "rate not set"
    if deadline <= now:
        return "OUT OF MSUPPS"
    return f"{timeutil.format_duration(deadline - now)} left"


def colour_for(entry: dict, now: int) -> int:
    deadline = run_out_at(entry)
    if deadline is None:
        return Colour.MUTED
    if deadline <= now:
        return Colour.DANGER
    if deadline - now <= LOW_HOURS * 3600:
        return Colour.ORANGE
    return Colour.INFO


def card_embed(entry: dict, *, alert_role_ids: list[int], now: int | None = None) -> discord.Embed:
    now = now if now is not None else timeutil.now()
    deadline = run_out_at(entry)
    embed = discord.Embed(title=clip(entry["name"], 256), description=md(location_of(entry)), colour=colour_for(entry, now))
    amount = current_msupps(entry, now)
    embed.add_field(name="Msupps now", value=f"~{amount:,}" if deadline is not None else f"{amount:,}", inline=True)
    embed.add_field(name="Consumption", value=f"{entry['rate']:,} per hour" if entry["rate"] > 0 else "Not set", inline=True)
    if deadline is None:
        runs_out = "Set a consumption rate to get reminders."
    elif deadline <= now:
        runs_out = f"**OUT OF MSUPPS** since {timeutil.relative(deadline)}"
    else:
        runs_out = f"{timeutil.relative(deadline)} ({timeutil.full(deadline)})"
    embed.add_field(name="Runs out", value=runs_out, inline=False)
    embed.add_field(
        name="Last measured",
        value=f"{entry['msupps']:,} msupps by <@{measured_by(entry)}> {timeutil.relative(entry['measured_at'])}",
        inline=False,
    )
    embed.add_field(
        name="Reminder pings",
        value=", ".join(f"<@&{rid}>" for rid in alert_role_ids) if alert_role_ids else "Logistics role only",
        inline=False,
    )
    embed.add_field(name="Added by", value=f"<@{entry['created_by']}> {timeutil.relative(entry['created_at'])}", inline=False)
    embed.set_footer(text=f"ID {entry['id']}")
    return embed


async def card(bot: FoxBot, entry: dict) -> discord.Embed:
    return card_embed(entry, alert_role_ids=await entries.alert_roles(bot.db, KIND, entry["id"]))


def location_feedback(hex_name: str, region: str, known: bool) -> str:
    label = entries.location_label(hex_name, region)
    if known:
        return f"Location: **{md(label)}**"
    return f"Location **{md(label)}** was not found on the war map, so it was saved exactly as typed. Use Edit to fix it."


def summary(entry: dict, now: int | None = None) -> str:
    return f"**{md(entry['name'])}**: {status_text(entry, now)}."


def amount_error(minimum: int, maximum: int) -> str:
    return f"Enter a whole number from {minimum:,} to {maximum:,} (for example 1500 or 1.5k)."


class AmountModal(BaseModal):
    def __init__(
        self,
        *,
        title: str,
        label: str,
        on_amount: AmountCallback,
        minimum: int,
        maximum: int,
        description: str | None = None,
        placeholder: str | None = None,
        default: int | None = None,
    ):
        super().__init__(title=title)
        self.on_amount = on_amount
        self.minimum = minimum
        self.maximum = maximum
        self.amount_field = text_field(
            label,
            default=str(default) if default is not None else None,
            placeholder=placeholder,
            description=description,
            max_length=12,
        )
        self.add_item(self.amount_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = parse_amount(text_value(self.amount_field))
        if value is None or not self.minimum <= value <= self.maximum:
            await deny(interaction, amount_error(self.minimum, self.maximum))
            return
        await self.on_amount(interaction, value)


def add_amount_modal(on_amount: AmountCallback) -> AmountModal:
    return AmountModal(
        title="Add Msupps",
        label="Msupps added",
        description="How many maintenance supplies you just put into the base.",
        placeholder="e.g. 1500",
        on_amount=on_amount,
        minimum=1,
        maximum=MAX_MSUPPS,
    )


class AddBaseModal(BaseModal, title="Add Base"):
    def __init__(self, bot: FoxBot, guild_id: int, location: Location, on_saved: SavedCallback):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.location = location
        self.on_saved = on_saved
        self.name_field = text_field("Base name", placeholder="e.g. Pan's Villa bunker base", max_length=NAME_MAX)
        self.msupps_field = text_field(
            "Current msupps",
            placeholder="e.g. 4000",
            description="What the base shows right now.",
            max_length=12,
        )
        self.rate_field = text_field(
            "Consumption per hour",
            placeholder="e.g. 50",
            description="Msupps used per hour. Use 0 if you do not know yet.",
            max_length=12,
        )
        for item in (self.name_field, self.msupps_field, self.rate_field):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = text_value(self.name_field)
        amount = parse_amount(text_value(self.msupps_field))
        rate = parse_amount(text_value(self.rate_field))
        if not name:
            await deny(interaction, "A base name is required.")
            return
        if amount is None or amount > MAX_MSUPPS:
            await deny(interaction, f"Current msupps: {amount_error(0, MAX_MSUPPS)}")
            return
        if rate is None or rate > MAX_RATE:
            await deny(interaction, f"Consumption per hour: {amount_error(0, MAX_RATE)}")
            return
        member = await interaction_member(interaction, self.guild_id)
        if member is None or not await self.bot.perms.has(member, GROUP):
            await deny(interaction, "You no longer have the Msupps permission.")
            return
        row = await create(
            self.bot,
            self.guild_id,
            name=name,
            hex_name=self.location.hex,
            region=self.location.region,
            msupps=amount,
            rate=rate,
            user_id=interaction.user.id,
        )
        await self.on_saved(interaction, row, location_feedback(self.location.hex, self.location.region, self.location.known))


class EditBaseModal(BaseModal, title="Edit Base"):
    def __init__(self, panel: MsuppsPanel, entry: dict):
        super().__init__()
        self.panel = panel
        self.name_field = text_field("Base name", default=entry["name"][:NAME_MAX], max_length=NAME_MAX)
        self.location_field = text_field(
            "Location",
            default=location_text(entry)[:LOCATION_MAX] or None,
            placeholder="e.g. Syrinx Pass",
            description="Town or region name. Add the hex after a comma if it is ambiguous.",
            max_length=LOCATION_MAX,
        )
        self.add_item(self.name_field)
        self.add_item(self.location_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        entry = await self.panel.load_checked(interaction)
        if entry is None:
            return
        name = text_value(self.name_field)
        location = self.panel.bot.locations.resolve_text(text_value(self.location_field))
        if not name or not (location.hex or location.region):
            await deny(interaction, "A base name and a location are required.")
            return
        await update(self.panel.bot, entry, name=name, hex=location.hex, region=location.region)
        await self.panel.show(interaction, notice=f"Saved. {location_feedback(location.hex, location.region, location.known)}")


class AlertRolesModal(BaseModal, title="Reminder Pings"):
    def __init__(self, panel: MsuppsPanel, current: list[int]):
        super().__init__()
        self.panel = panel
        self.roles = discord.ui.Label(
            text="Extra roles to ping",
            description="Pinged together with the logistics role when this base runs low on msupps.",
            component=discord.ui.RoleSelect(
                min_values=0,
                max_values=10,
                required=False,
                default_values=[discord.SelectDefaultValue(id=rid, type=discord.SelectDefaultValueType.role) for rid in current[:10]],
            ),
        )
        self.add_item(self.roles)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        entry = await self.panel.load_checked(interaction)
        if entry is None:
            return
        role_ids = [role.id for role in self.roles.component.values]
        await entries.set_alert_roles(self.panel.bot.db, KIND, entry["id"], role_ids)
        await self.panel.show(interaction, notice="Reminder pings updated.")


class MsuppsPanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, entry_id: str, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.entry_id = entry_id

    async def load_checked(self, interaction: discord.Interaction) -> dict | None:
        entry = await fetch(self.bot, self.guild_id, self.entry_id)
        if entry is None:
            await edit(interaction, content="This base no longer exists.", embed=None, view=None)
            return None
        member = await interaction_member(interaction, self.guild_id)
        if member is None or not await can_access(self.bot, member, entry):
            await deny(interaction, "You need the **Msupps** permission for that.")
            return None
        return entry

    async def _build(self, entry: dict, member: discord.Member | None) -> None:
        self.clear_items()
        self._button("Add Msupps", discord.ButtonStyle.success, self._add, row=0)
        self._button("Set Msupps", discord.ButtonStyle.primary, self._set, row=0)
        self._button("Change Rate", discord.ButtonStyle.secondary, self._rate, row=0)
        self._button("Edit", discord.ButtonStyle.secondary, self._edit, row=0)
        self._button("Reminder Pings", discord.ButtonStyle.secondary, self._alert_roles, row=1)
        subscribed = member is not None and await entries.is_subscribed(self.bot.db, KIND, entry["id"], member.id)
        self._button("Stop Notifying Me" if subscribed else "Notify Me", discord.ButtonStyle.secondary, self._notify, row=1)
        self._button("Delete", discord.ButtonStyle.danger, self._delete, row=1)

    def _button(self, label: str, style: discord.ButtonStyle, callback, *, row: int) -> None:
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    async def render(self, interaction: discord.Interaction) -> tuple[dict | None, discord.Embed | None]:
        entry = await fetch(self.bot, self.guild_id, self.entry_id)
        if entry is None:
            return None, None
        member = await interaction_member(interaction, self.guild_id)
        await self._build(entry, member)
        return entry, await card(self.bot, entry)

    async def send(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        entry, embed = await self.render(interaction)
        if entry is None:
            await deny(interaction, "That base no longer exists.")
            return
        await reply(interaction, notice, embed=embed, view=self)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        entry, embed = await self.render(interaction)
        if entry is None:
            await edit(interaction, content="This base no longer exists.", embed=None, view=None)
            return
        await edit(interaction, content=notice, embed=embed, view=self)

    async def _add(self, interaction: discord.Interaction) -> None:
        if await self.load_checked(interaction) is None:
            return

        async def entered(modal_interaction: discord.Interaction, amount: int) -> None:
            entry = await self.load_checked(modal_interaction)
            if entry is None:
                return
            updated = await add_msupps(self.bot, entry, amount, modal_interaction.user.id)
            await self.show(modal_interaction, notice=f"Added {amount:,} msupps. {summary(updated)}")

        await interaction.response.send_modal(add_amount_modal(entered))

    async def _set(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return

        async def entered(modal_interaction: discord.Interaction, amount: int) -> None:
            current = await self.load_checked(modal_interaction)
            if current is None:
                return
            updated = await set_msupps(self.bot, current, amount, modal_interaction.user.id)
            await self.show(modal_interaction, notice=f"Msupps set to {amount:,}. {summary(updated)}")

        await interaction.response.send_modal(
            AmountModal(
                title="Set Msupps",
                label="Msupps in the base now",
                description="The amount you see in game right now.",
                placeholder="e.g. 3200",
                on_amount=entered,
                minimum=0,
                maximum=MAX_MSUPPS,
                default=current_msupps(entry),
            )
        )

    async def _rate(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return

        async def entered(modal_interaction: discord.Interaction, rate: int) -> None:
            current = await self.load_checked(modal_interaction)
            if current is None:
                return
            updated = await change_rate(self.bot, current, rate, modal_interaction.user.id)
            label = f"{rate:,} per hour" if rate > 0 else "not set"
            await self.show(modal_interaction, notice=f"Consumption is now {label}. {summary(updated)}")

        await interaction.response.send_modal(
            AmountModal(
                title="Change Rate",
                label="Consumption per hour",
                description="Msupps the base uses per hour. Use 0 to turn reminders off.",
                placeholder="e.g. 50",
                on_amount=entered,
                minimum=0,
                maximum=MAX_RATE,
                default=entry["rate"],
            )
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        await interaction.response.send_modal(EditBaseModal(self, entry))

    async def _alert_roles(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        current = await entries.alert_roles(self.bot.db, KIND, entry["id"])
        await interaction.response.send_modal(AlertRolesModal(self, current))

    async def _notify(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        subscribed = await entries.toggle_subscription(self.bot.db, KIND, entry["id"], interaction.user.id)
        notice = "You will get a DM for this base's reminders." if subscribed else "You will no longer get DMs for this base."
        await self.show(interaction, notice=notice)

    async def _delete(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            current = await self.load_checked(confirm_interaction)
            if current is None:
                return
            await delete(self.bot, current)
            await edit(confirm_interaction, content=f"Deleted **{md(current['name'])}**.", embed=None, view=None)

        async def cancelled(cancel_interaction: discord.Interaction) -> None:
            if await self.load_checked(cancel_interaction) is None:
                return
            await self.show(cancel_interaction)

        await edit(
            interaction,
            content=f"Delete **{md(entry['name'])}** at {md(location_of(entry))}? This cannot be undone.",
            embed=None,
            view=ConfirmView(owner_id=interaction.user.id, on_confirm=confirmed, on_cancel=cancelled, confirm_label="Delete"),
        )


async def open_panel(bot: FoxBot, interaction: discord.Interaction, entry_id: str, *, replace: bool = False, notice: str | None = None) -> None:
    entry = await fetch(bot, interaction.guild_id, entry_id)
    if entry is None:
        await deny(interaction, "That base no longer exists.")
        return
    member = await interaction_member(interaction)
    if member is None or not await can_access(bot, member, entry):
        await deny(interaction, "You need the **Msupps** permission for that.")
        return
    panel = MsuppsPanel(bot, interaction.guild_id, entry_id, interaction.user.id)
    if replace:
        await panel.show(interaction, notice=notice)
    else:
        await panel.send(interaction, notice=notice)


async def start_add(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return

    async def saved(modal_interaction: discord.Interaction, row: dict, feedback: str) -> None:
        panel = MsuppsPanel(bot, row["guild_id"], row["id"], modal_interaction.user.id)
        await panel.show(modal_interaction, notice=f"Base added. {feedback}")

    async def picked(pick_interaction: discord.Interaction, location: Location) -> None:
        if not await check_group(pick_interaction, GROUP):
            return
        await pick_interaction.response.send_modal(AddBaseModal(bot, pick_interaction.guild_id, location, saved))

    picker = await LocationPicker.create(bot, interaction, picked, prompt="**Add a base**")
    await picker.start(interaction)


def picker_options(rows: list[dict], now: int) -> list[PickOption]:
    return [
        PickOption(row["id"], f"{row['name']} - {location_of(row)}", option_description(row, now))
        for row in sorted(rows, key=lambda r: (location_of(r).lower(), r["name"].lower()))
    ]


async def show_all(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    rows = await list_entries(bot, interaction.guild_id)
    if not rows:
        await reply(interaction, "No bases are tracked yet.")
        return

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await open_panel(bot, pick_interaction, value)

    view = PickerView(picker_options(rows, timeutil.now()), picked, owner_id=interaction.user.id, placeholder="Pick a base to manage...")
    await reply(interaction, f"{len(rows)} base(s), sorted by location.", view=view)


class MsuppsBoard:
    kind = KIND
    title = "Msupps"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        rows = await list_entries(self.bot, guild.id)
        now = timeutil.now()
        lines = entries.grouped_lines(rows, lambda e: board_line(e, now), sort_key=sort_key)
        embeds = fit_board_embeds(
            "Base Maintenance Supplies",
            lines,
            colour=Colour.INFO,
            empty="No bases are tracked yet. Press **Add Base** to add one.",
            footer=f"{len(rows)} base(s) | Estimates update automatically",
            overflow_hint="Too many to show here. Use All Bases or /msupps list.",
        )
        options = [option(f"{row['name']} - {location_of(row)}", row["id"], option_description(row, now)) for row in rows[:25]]
        return BoardRender(
            embeds=embeds,
            options=options,
            placeholder="Manage a base (runs out first at the top)...",
            buttons=[
                BoardButton("add", "Add Base", discord.ButtonStyle.success),
                BoardButton("all", "All Bases"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action == "add":
            await start_add(self.bot, interaction)
        elif action == "all":
            await show_all(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        if not await check_group(interaction, GROUP):
            return
        await open_panel(self.bot, interaction, value)


class MsuppsAlerts:
    kind = KIND
    noun = "Base maintenance supplies"
    action_label = "Add Msupps"
    permission = GROUP

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def timed_entries(self) -> list[TimedEntry]:
        rows = await self.bot.db.fetchall(
            "SELECT id, guild_id, name, hex, region, msupps, rate, measured_at FROM msupps_bases WHERE rate > 0"
        )
        return [TimedEntry(KIND, row["guild_id"], row["id"], run_out_at(row), row["name"], location_of(row)) for row in rows]

    async def alert_roles(self, entry: TimedEntry) -> list[int]:
        return await entries.alert_roles(self.bot.db, KIND, entry.entry_id)

    async def recipients(self, entry: TimedEntry) -> set[int]:
        return set()

    async def can_notify(self, entry: TimedEntry, member: discord.Member) -> bool:
        return await self.bot.perms.has(member, GROUP)

    def describe(self, entry: TimedEntry, expired: bool) -> str:
        name = f"**{md(entry.title)}**"
        if expired:
            return f"{name} has run out of maintenance supplies. Fill it up in game, then press **Add Msupps**."
        return (
            f"{name} runs out of maintenance supplies {timeutil.relative(entry.deadline)}. "
            "Fill it up in game, then press **Add Msupps**."
        )

    async def _check(self, interaction: discord.Interaction, guild_id: int, entry_id: str) -> dict | None:
        entry = await fetch(self.bot, guild_id, entry_id)
        if entry is None:
            await deny(interaction, "That base is no longer tracked.")
            return None
        member = await interaction_member(interaction, guild_id)
        if member is None or not await can_access(self.bot, member, entry):
            await deny(interaction, "You need the **Msupps** permission for that.")
            return None
        return entry

    async def on_alert_action(self, interaction: discord.Interaction, guild_id: int, entry_id: str) -> None:
        if await self._check(interaction, guild_id, entry_id) is None:
            return

        async def entered(modal_interaction: discord.Interaction, amount: int) -> None:
            entry = await self._check(modal_interaction, guild_id, entry_id)
            if entry is None:
                return
            updated = await add_msupps(self.bot, entry, amount, modal_interaction.user.id, sync=False)
            await reply(modal_interaction, f"Added {amount:,} msupps. {summary(updated)}")
            await sync_alerts(self.bot, updated)

        await interaction.response.send_modal(add_amount_modal(entered))


class Msupps(commands.Cog):
    group = app_commands.Group(name="msupps", description="Track base maintenance supplies and when they run out.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def _entry_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        member = await interaction_member(interaction)
        if member is None:
            return []
        needle = current.lower().strip()
        rows = await visible_entries(self.bot, member)
        now = timeutil.now()
        matches = [row for row in rows if not needle or needle in f"{row['name']} {location_of(row)}".lower()]
        return [
            app_commands.Choice(name=clip(f"{row['name']} - {location_of(row)} ({state_text(row, now)})", 100), value=row["id"])
            for row in matches[:25]
        ]

    @group.command(name="add", description="Add a base and track its maintenance supplies.")
    @app_commands.describe(
        hex="The hex (start typing to search)",
        town="Town in that hex (start typing to search)",
        name="Base name shown on the board",
        msupps="Maintenance supplies in the base right now",
        rate="Msupps used per hour (0 if unknown)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        hex: app_commands.Range[str, 1, 60],
        town: app_commands.Range[str, 1, 100],
        name: app_commands.Range[str, 1, NAME_MAX],
        msupps: app_commands.Range[int, 0, MAX_MSUPPS],
        rate: app_commands.Range[int, 0, MAX_RATE],
    ) -> None:
        if not await check_group(interaction, GROUP):
            return
        resolved = self.bot.locations.resolve(hex, town)
        row = await create(
            self.bot,
            interaction.guild_id,
            name=name.strip(),
            hex_name=resolved.hex,
            region=resolved.region,
            msupps=msupps,
            rate=rate,
            user_id=interaction.user.id,
        )
        notice = f"Base added. {location_feedback(resolved.hex, resolved.region, resolved.known)}"
        await MsuppsPanel(self.bot, interaction.guild_id, row["id"], interaction.user.id).send(interaction, notice=notice)

    @add.autocomplete("hex")
    async def add_hex_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.hex_choices(current)

    @add.autocomplete("town")
    async def add_town_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.region_choices(getattr(interaction.namespace, "hex", None), current)

    @group.command(name="open", description="Open a base's panel to add msupps, change the rate or delete it.")
    @app_commands.describe(base="Search by name or location")
    async def open(self, interaction: discord.Interaction, base: str) -> None:
        await open_panel(self.bot, interaction, base)

    @open.autocomplete("base")
    async def open_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._entry_choices(interaction, current)

    @group.command(name="resupply", description="Record maintenance supplies you just put into a base.")
    @app_commands.describe(base="Search by name or location", amount="Msupps you added")
    async def resupply(self, interaction: discord.Interaction, base: str, amount: app_commands.Range[int, 1, MAX_MSUPPS]) -> None:
        if not await check_group(interaction, GROUP):
            return
        entry = await fetch(self.bot, interaction.guild_id, base)
        if entry is None:
            await deny(interaction, "Base not found. Pick one from the suggestions.")
            return
        updated = await add_msupps(self.bot, entry, amount, interaction.user.id)
        await reply(interaction, f"Added {amount:,} msupps. {summary(updated)}")

    @resupply.autocomplete("base")
    async def resupply_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._entry_choices(interaction, current)

    @group.command(name="list", description="List every tracked base (only you see the list).")
    async def list_command(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP):
            return
        rows = await list_entries(self.bot, interaction.guild_id)
        now = timeutil.now()
        embeds = paginate_embeds(
            "Base Maintenance Supplies",
            entries.grouped_lines(rows, lambda e: board_line(e, now), sort_key=sort_key),
            colour=Colour.INFO,
            empty="No bases are tracked yet.",
            footer=f"{len(rows)} base(s)",
        )
        await EmbedPaginator(embeds, owner_id=interaction.user.id).start(interaction)

    @group.command(name="board", description="Admin: post the live msupps board in this channel (moves it if it exists).")
    async def board(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.boards.post(interaction.guild, KIND, interaction.channel)
        except discord.HTTPException as error:
            await reply(interaction, f"Could not post the board here: {error.text or error}")
            return
        alert_channel = await self.bot.settings.get(interaction.guild_id, keys.ALERT_CHANNEL)
        hint = "" if alert_channel else " Reminders will be posted in this channel until an alerts channel is set in `/settings`."
        await reply(interaction, f"Msupps board posted.{hint}")


async def setup(bot: FoxBot) -> None:
    bot.boards.register(MsuppsBoard(bot))
    bot.alerts.register(MsuppsAlerts(bot))
    await bot.add_cog(Msupps(bot))
