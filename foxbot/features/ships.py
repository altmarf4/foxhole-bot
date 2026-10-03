from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import DEFAULT_SHIP_TYPES, SHIP_LIFETIME_HOURS, Colour
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
    select_field,
    select_value,
    text_field,
    text_value,
)

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

KIND = "ship"
GROUP = "ship"
LIFETIME_SECONDS = timeutil.hours_to_seconds(SHIP_LIFETIME_HOURS)
NAME_MAX = 60
SQUAD_MAX = 40
PARKED_MAX = 80
NOTES_MAX = 500
LOCATION_MAX = 100

SavedCallback = Callable[[discord.Interaction, dict, str], Awaitable[None]]


async def fetch(bot: FoxBot, guild_id: int, entry_id: str) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM ships WHERE guild_id = ? AND id = ?", (guild_id, entry_id))


async def list_entries(bot: FoxBot, guild_id: int) -> list[dict]:
    return await bot.db.fetchall("SELECT * FROM ships WHERE guild_id = ? ORDER BY expires_at", (guild_id,))


async def ship_types(bot: FoxBot, guild_id: int) -> list[str]:
    configured = await bot.settings.get(guild_id, keys.SHIP_TYPES) or []
    types = [str(value).strip() for value in configured if str(value).strip()]
    return list(dict.fromkeys(types)) or list(DEFAULT_SHIP_TYPES)


def match_ship_type(types: list[str], text: str) -> str | None:
    needle = (text or "").strip().casefold()
    for ship_type in types:
        if ship_type.casefold() == needle:
            return ship_type
    return None


async def create(
    bot: FoxBot,
    guild_id: int,
    *,
    name: str,
    ship_type: str,
    squad: str,
    hex_name: str,
    region: str,
    parked_at: str,
    notes: str,
    user_id: int,
) -> dict:
    entry_id = await ids.unique_id(bot.db, "ships")
    now = timeutil.now()
    expires_at = now + LIFETIME_SECONDS
    await bot.db.execute(
        "INSERT INTO ships (id, guild_id, name, ship_type, squad, hex, region, location, notes, expires_at, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, guild_id, name, ship_type, squad, hex_name, region, parked_at, notes, expires_at, user_id, now, now),
    )
    await bot.alerts.reset(KIND, entry_id, guild_id, expires_at, now)
    bot.boards.request_refresh(guild_id, KIND)
    return await fetch(bot, guild_id, entry_id)


async def update(bot: FoxBot, entry: dict, **fields) -> dict | None:
    fields["updated_at"] = timeutil.now()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    await bot.db.execute(
        f"UPDATE ships SET {assignments} WHERE guild_id = ? AND id = ?",
        (*fields.values(), entry["guild_id"], entry["id"]),
    )
    bot.boards.request_refresh(entry["guild_id"], KIND)
    return await fetch(bot, entry["guild_id"], entry["id"])


async def refresh_timer(bot: FoxBot, entry: dict, user_id: int, *, reset_alerts: bool = True) -> dict | None:
    now = timeutil.now()
    expires_at = now + LIFETIME_SECONDS
    updated = await update(bot, entry, expires_at=expires_at, refreshed_by=user_id, refreshed_at=now)
    if reset_alerts:
        await bot.alerts.reset(KIND, entry["id"], entry["guild_id"], expires_at, now)
    return updated


async def delete(bot: FoxBot, entry: dict) -> None:
    async with bot.db.transaction() as tx:
        await tx.execute("DELETE FROM ships WHERE guild_id = ? AND id = ?", (entry["guild_id"], entry["id"]))
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


def heading(entry: dict) -> str:
    text = f"**{md(entry['name'])}** [{md(entry['ship_type'])}]"
    if entry["squad"]:
        text += f" ({md(entry['squad'])})"
    return text


def board_line(entry: dict, now: int | None = None) -> str:
    who = entry.get("refreshed_by") or entry["created_by"]
    line = f"{heading(entry)} {entries.timer_text(entry['expires_at'], now)} - <@{who}>"
    if entry["location"]:
        line += f" - parked at {md(entry['location'])}"
    return line


def state_text(entry: dict, now: int) -> str:
    if entry["expires_at"] <= now:
        return "EXPIRED"
    return f"{timeutil.format_duration(entry['expires_at'] - now)} left"


def option_description(entry: dict, now: int) -> str:
    parts = [entry["ship_type"]]
    if entry["squad"]:
        parts.append(f"Squad {entry['squad']}")
    parts.append(state_text(entry, now))
    return " | ".join(parts)


def sort_key(entry: dict) -> tuple:
    return (entry["expires_at"], entry["name"].lower())


def card_embed(entry: dict, *, alert_role_ids: list[int], now: int | None = None) -> discord.Embed:
    now = now if now is not None else timeutil.now()
    expired = entry["expires_at"] <= now
    embed = discord.Embed(
        title=clip(entry["name"], 256),
        description=md(location_of(entry)),
        colour=Colour.DARK if expired else Colour.INFO,
    )
    embed.add_field(name="Ship type", value=md(entry["ship_type"]), inline=True)
    embed.add_field(name="Reserved squad", value=md(entry["squad"]) if entry["squad"] else "Not set", inline=True)
    if entry["location"]:
        embed.add_field(name="Parked at", value=clip(md(entry["location"]), 1024), inline=True)
    if expired:
        timer = "**EXPIRED** - the squad lock has run out"
    else:
        timer = f"{timeutil.relative(entry['expires_at'])} ({timeutil.full(entry['expires_at'])})"
    embed.add_field(name="Squad lock", value=timer, inline=False)
    embed.add_field(name="Last refreshed", value=entries.refreshed_text(entry), inline=False)
    embed.add_field(
        name="Reminder pings",
        value=", ".join(f"<@&{rid}>" for rid in alert_role_ids) if alert_role_ids else "Logistics role only",
        inline=False,
    )
    if entry["notes"]:
        embed.add_field(name="Notes", value=clip(md(entry["notes"]), 1024), inline=False)
    embed.add_field(name="Added by", value=f"<@{entry['created_by']}> {timeutil.relative(entry['created_at'])}", inline=False)
    embed.set_footer(text=f"ID {entry['id']}")
    return embed


async def card(bot: FoxBot, entry: dict) -> tuple[discord.Embed, list[discord.File]]:
    embed = card_embed(entry, alert_role_ids=await entries.alert_roles(bot.db, KIND, entry["id"]))
    files = await entries.attach_screenshot(bot.db, KIND, entry["id"], embed)
    return embed, files


def location_feedback(hex_name: str, region: str, known: bool) -> str:
    label = entries.location_label(hex_name, region)
    if known:
        return f"Location: **{md(label)}**"
    return f"Location **{md(label)}** was not found on the war map, so it was saved exactly as typed. Use Edit to fix it."


def refreshed_notice(entry: dict) -> str:
    return f"Timer refreshed. The squad lock on **{md(entry['name'])}** now expires {timeutil.relative(entry['expires_at'])}."


class ShipModal(BaseModal):
    def __init__(
        self,
        bot: FoxBot,
        guild_id: int,
        types: list[str],
        on_saved: SavedCallback,
        *,
        entry: dict | None = None,
        location: Location | None = None,
    ):
        super().__init__(title="Edit Ship" if entry is not None else "Add Ship")
        self.bot = bot
        self.guild_id = guild_id
        self.entry = entry
        self.on_saved = on_saved
        self.location = location
        self.types = list(types) or list(DEFAULT_SHIP_TYPES)
        self.name_field = text_field(
            "Name",
            default=entry["name"][:NAME_MAX] if entry else None,
            placeholder="e.g. HMS Foxhound",
            max_length=NAME_MAX,
        )
        self.type_field = select_field("Ship type", self.types, default=entry["ship_type"] if entry else self.types[0])
        self.squad_field = text_field(
            "Reserved squad",
            default=entry["squad"][:SQUAD_MAX] if entry and entry["squad"] else None,
            placeholder="The squad holding the lock",
            max_length=SQUAD_MAX,
        )
        self.location_field = None
        if location is None:
            self.location_field = text_field(
                "Location",
                default=location_text(entry)[:LOCATION_MAX] or None if entry else None,
                placeholder="e.g. Syrinx Pass",
                description="Town or region name. Add the hex after a comma if it is ambiguous.",
                max_length=LOCATION_MAX,
            )
        self.parked_field = text_field(
            "Parked at",
            default=entry["location"][:PARKED_MAX] if entry and entry["location"] else None,
            placeholder="e.g. Seaport dock 2",
            required=False,
            max_length=PARKED_MAX,
        )
        self.notes_field = None
        if entry is None and location is not None:
            self.notes_field = text_field(
                "Notes",
                placeholder="Anything the crew should know",
                required=False,
                max_length=NOTES_MAX,
                paragraph=True,
            )
        for item in (self.name_field, self.type_field, self.squad_field, self.location_field, self.parked_field, self.notes_field):
            if item is not None:
                self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        location = self.location or self.bot.locations.resolve_text(text_value(self.location_field))
        if not (location.hex or location.region):
            await deny(interaction, "A location is required.")
            return
        name = text_value(self.name_field)
        squad = text_value(self.squad_field)
        if not name or not squad:
            await deny(interaction, "A name and a reserved squad are required.")
            return
        ship_type = select_value(self.type_field) or self.types[0]
        parked_at = text_value(self.parked_field)
        member = await interaction_member(interaction, self.guild_id)
        if member is None:
            await deny(interaction, "This only works inside the server.")
            return
        if self.entry is None:
            if not await self.bot.perms.has(member, GROUP):
                await deny(interaction, "You no longer have the Ships permission.")
                return
            saved = await create(
                self.bot,
                self.guild_id,
                name=name,
                ship_type=ship_type,
                squad=squad,
                hex_name=location.hex,
                region=location.region,
                parked_at=parked_at,
                notes=text_value(self.notes_field) if self.notes_field is not None else "",
                user_id=interaction.user.id,
            )
        else:
            current = await fetch(self.bot, self.guild_id, self.entry["id"])
            if current is None:
                await deny(interaction, "That ship no longer exists.")
                return
            if not await can_access(self.bot, member, current):
                await deny(interaction, "You no longer have the Ships permission.")
                return
            saved = await update(
                self.bot,
                current,
                name=name,
                ship_type=ship_type,
                squad=squad,
                hex=location.hex,
                region=location.region,
                location=parked_at,
            )
        await self.on_saved(interaction, saved, location_feedback(location.hex, location.region, location.known))


class NotesModal(BaseModal, title="Ship Notes"):
    def __init__(self, panel: ShipPanel, current: str):
        super().__init__()
        self.panel = panel
        self.notes_field = text_field(
            "Notes",
            default=current[:NOTES_MAX] or None,
            placeholder="Anything the crew should know",
            description="Leave empty to clear the notes.",
            required=False,
            max_length=NOTES_MAX,
            paragraph=True,
        )
        self.add_item(self.notes_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        entry = await self.panel.load_checked(interaction)
        if entry is None:
            return
        notes = text_value(self.notes_field)
        await update(self.panel.bot, entry, notes=notes)
        await self.panel.show(interaction, notice="Notes saved." if notes else "Notes cleared.")


class ScreenshotModal(BaseModal, title="Ship Screenshot"):
    def __init__(self, panel: ShipPanel):
        super().__init__()
        self.panel = panel
        self.upload = discord.ui.Label(
            text="Screenshot",
            description="Upload an image. Submit without a file to remove the current screenshot.",
            component=discord.ui.FileUpload(required=False, max_values=1),
        )
        self.add_item(self.upload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        entry = await self.panel.load_checked(interaction)
        if entry is None:
            return
        files = self.upload.component.values
        bot = self.panel.bot
        if not files:
            await entries.delete_screenshot(bot.db, KIND, entry["id"])
            await self.panel.show(interaction, notice="Screenshot removed.")
            return
        error = await entries.save_screenshot(bot.db, KIND, entry["id"], files[0], interaction.user.id)
        if error:
            await self.panel.show(interaction, notice=error)
            return
        await self.panel.show(interaction, notice="Screenshot saved.")


class AlertRolesModal(BaseModal, title="Reminder Pings"):
    def __init__(self, panel: ShipPanel, current: list[int]):
        super().__init__()
        self.panel = panel
        self.roles = discord.ui.Label(
            text="Extra roles to ping",
            description="Pinged together with the logistics role when this ship's squad lock runs low.",
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


class ShipPanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, entry_id: str, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.entry_id = entry_id

    async def load_checked(self, interaction: discord.Interaction) -> dict | None:
        entry = await fetch(self.bot, self.guild_id, self.entry_id)
        if entry is None:
            await edit(interaction, content="This ship no longer exists.", embed=None, attachments=[], view=None)
            return None
        member = await interaction_member(interaction, self.guild_id)
        if member is None or not await can_access(self.bot, member, entry):
            await deny(interaction, "You need the **Ships** permission for that.")
            return None
        return entry

    async def _build(self, entry: dict, member: discord.Member | None) -> None:
        self.clear_items()
        self._button("Refresh Timer", discord.ButtonStyle.success, self._refresh, row=0)
        self._button("Edit", discord.ButtonStyle.primary, self._edit, row=0)
        self._button("Notes", discord.ButtonStyle.secondary, self._notes, row=0)
        self._button("Screenshot", discord.ButtonStyle.secondary, self._screenshot, row=0)
        self._button("Reminder Pings", discord.ButtonStyle.secondary, self._alert_roles, row=0)
        subscribed = member is not None and await entries.is_subscribed(self.bot.db, KIND, entry["id"], member.id)
        self._button("Stop Notifying Me" if subscribed else "Notify Me", discord.ButtonStyle.secondary, self._notify, row=1)
        self._button("Delete", discord.ButtonStyle.danger, self._delete, row=1)

    def _button(self, label: str, style: discord.ButtonStyle, callback, *, row: int) -> None:
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    async def render(self, interaction: discord.Interaction) -> tuple[dict | None, discord.Embed | None, list[discord.File]]:
        entry = await fetch(self.bot, self.guild_id, self.entry_id)
        if entry is None:
            return None, None, []
        member = await interaction_member(interaction, self.guild_id)
        await self._build(entry, member)
        embed, files = await card(self.bot, entry)
        return entry, embed, files

    async def send(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        entry, embed, files = await self.render(interaction)
        if entry is None:
            await deny(interaction, "That ship no longer exists.")
            return
        await reply(interaction, notice, embed=embed, view=self, files=files or None)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        entry, embed, files = await self.render(interaction)
        if entry is None:
            await edit(interaction, content="This ship no longer exists.", embed=None, attachments=[], view=None)
            return
        await edit(interaction, content=notice, embed=embed, attachments=files, view=self)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        updated = await refresh_timer(self.bot, entry, interaction.user.id)
        await self.show(interaction, notice=refreshed_notice(updated))

    async def _notify(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        subscribed = await entries.toggle_subscription(self.bot.db, KIND, entry["id"], interaction.user.id)
        notice = "You will get a DM for this ship's reminders." if subscribed else "You will no longer get DMs for this ship."
        await self.show(interaction, notice=notice)

    async def _edit(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return

        async def saved(modal_interaction: discord.Interaction, row: dict, feedback: str) -> None:
            await self.show(modal_interaction, notice=f"Saved. {feedback}")

        types = await ship_types(self.bot, self.guild_id)
        await interaction.response.send_modal(ShipModal(self.bot, self.guild_id, types, saved, entry=entry))

    async def _notes(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        await interaction.response.send_modal(NotesModal(self, entry["notes"]))

    async def _screenshot(self, interaction: discord.Interaction) -> None:
        if await self.load_checked(interaction) is None:
            return
        await interaction.response.send_modal(ScreenshotModal(self))

    async def _alert_roles(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        current = await entries.alert_roles(self.bot.db, KIND, entry["id"])
        await interaction.response.send_modal(AlertRolesModal(self, current))

    async def _delete(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            current = await self.load_checked(confirm_interaction)
            if current is None:
                return
            await delete(self.bot, current)
            await edit(
                confirm_interaction,
                content=f"Deleted **{md(current['name'])}**.",
                embed=None,
                attachments=[],
                view=None,
            )

        async def cancelled(cancel_interaction: discord.Interaction) -> None:
            if await self.load_checked(cancel_interaction) is None:
                return
            await self.show(cancel_interaction)

        await edit(
            interaction,
            content=f"Delete **{md(entry['name'])}** at {md(location_of(entry))}? This cannot be undone.",
            embed=None,
            attachments=[],
            view=ConfirmView(owner_id=interaction.user.id, on_confirm=confirmed, on_cancel=cancelled, confirm_label="Delete"),
        )


async def open_panel(bot: FoxBot, interaction: discord.Interaction, entry_id: str, *, replace: bool = False, notice: str | None = None) -> None:
    entry = await fetch(bot, interaction.guild_id, entry_id)
    if entry is None:
        await deny(interaction, "That ship no longer exists.")
        return
    member = await interaction_member(interaction)
    if member is None or not await can_access(bot, member, entry):
        await deny(interaction, "You need the **Ships** permission for that.")
        return
    panel = ShipPanel(bot, interaction.guild_id, entry_id, interaction.user.id)
    if replace:
        await panel.show(interaction, notice=notice)
    else:
        await panel.send(interaction, notice=notice)


async def start_add(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return

    async def saved(modal_interaction: discord.Interaction, row: dict, feedback: str) -> None:
        panel = ShipPanel(bot, row["guild_id"], row["id"], modal_interaction.user.id)
        await panel.show(modal_interaction, notice=f"Ship added. {feedback}")

    async def picked(pick_interaction: discord.Interaction, location: Location) -> None:
        if not await check_group(pick_interaction, GROUP):
            return
        types = await ship_types(bot, pick_interaction.guild_id)
        await pick_interaction.response.send_modal(ShipModal(bot, pick_interaction.guild_id, types, saved, location=location))

    picker = await LocationPicker.create(bot, interaction, picked, prompt="**Add a ship**")
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
        await reply(interaction, "No ships are tracked yet.")
        return

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await open_panel(bot, pick_interaction, value)

    view = PickerView(picker_options(rows, timeutil.now()), picked, owner_id=interaction.user.id, placeholder="Pick a ship to manage...")
    await reply(interaction, f"{len(rows)} ship(s), sorted by location.", view=view)


class ShipBoard:
    kind = KIND
    title = "Ships"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        rows = await list_entries(self.bot, guild.id)
        now = timeutil.now()
        lines = entries.grouped_lines(rows, lambda e: board_line(e, now), sort_key=sort_key)
        embeds = fit_board_embeds(
            "Tracked Ships",
            lines,
            colour=Colour.INFO,
            empty="No ships are tracked yet. Press **Add Ship** to add one.",
            footer=f"{len(rows)} ship(s) | Updates automatically",
            overflow_hint="Too many to show here. Use All Ships or /ship list.",
        )
        urgent = sorted(rows, key=sort_key)[:25]
        options = [option(f"{row['name']} - {location_of(row)}", row["id"], option_description(row, now)) for row in urgent]
        return BoardRender(
            embeds=embeds,
            options=options,
            placeholder="Manage a ship (most urgent first)...",
            buttons=[
                BoardButton("add", "Add Ship", discord.ButtonStyle.success),
                BoardButton("all", "All Ships"),
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


class ShipAlerts:
    kind = KIND
    noun = "Ship squad lock"
    action_label = "Refresh Timer"
    permission = GROUP

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def timed_entries(self) -> list[TimedEntry]:
        rows = await self.bot.db.fetchall("SELECT id, guild_id, name, ship_type, hex, region, expires_at FROM ships")
        return [
            TimedEntry(KIND, row["guild_id"], row["id"], row["expires_at"], f"{row['name']} ({row['ship_type']})", location_of(row))
            for row in rows
        ]

    async def alert_roles(self, entry: TimedEntry) -> list[int]:
        return await entries.alert_roles(self.bot.db, KIND, entry.entry_id)

    async def recipients(self, entry: TimedEntry) -> set[int]:
        return set()

    async def can_notify(self, entry: TimedEntry, member: discord.Member) -> bool:
        return await self.bot.perms.has(member, GROUP)

    def describe(self, entry: TimedEntry, expired: bool) -> str:
        name = f"**{md(entry.title)}**"
        if expired:
            return f"The squad lock on {name} has expired. If it was renewed in game in time, press **Refresh Timer**."
        return f"The squad lock on {name} expires {timeutil.relative(entry.deadline)}. Renew it in game, then press **Refresh Timer**."

    async def on_alert_action(self, interaction: discord.Interaction, guild_id: int, entry_id: str) -> None:
        entry = await fetch(self.bot, guild_id, entry_id)
        if entry is None:
            await deny(interaction, "That ship is no longer tracked.")
            return
        member = await interaction_member(interaction, guild_id)
        if member is None or not await can_access(self.bot, member, entry):
            await deny(interaction, "You need the **Ships** permission for that.")
            return
        updated = await refresh_timer(self.bot, entry, interaction.user.id, reset_alerts=False)
        await reply(interaction, refreshed_notice(updated))
        await self.bot.alerts.reset(KIND, entry_id, guild_id, updated["expires_at"])


class Ships(commands.Cog):
    group = app_commands.Group(name="ship", description="Track ships and their squad-lock timers.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def _entry_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        member = await interaction_member(interaction)
        if member is None:
            return []
        needle = current.lower().strip()
        rows = await visible_entries(self.bot, member)
        matches = [
            row
            for row in rows
            if not needle
            or needle in f"{row['name']} {row['ship_type']} {row['squad']} {row['location']} {location_of(row)}".lower()
        ]
        matches.sort(key=sort_key)
        return [
            app_commands.Choice(name=clip(f"{row['name']} [{row['ship_type']}] - {location_of(row)}", 100), value=row["id"])
            for row in matches[:25]
        ]

    @group.command(name="add", description=f"Add a ship and start its {SHIP_LIFETIME_HOURS} hour squad-lock timer.")
    @app_commands.describe(
        hex="The hex (start typing to search)",
        town="Town in that hex (start typing to search)",
        name="Name shown on the board",
        ship_type="Destroyer, Submarine, ...",
        squad="The squad holding the lock",
        parked_at="Where exactly it is parked, e.g. Seaport dock 2",
        notes="Anything the crew should know",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        hex: app_commands.Range[str, 1, 60],
        town: app_commands.Range[str, 1, 100],
        name: app_commands.Range[str, 1, NAME_MAX],
        ship_type: str,
        squad: app_commands.Range[str, 1, SQUAD_MAX],
        parked_at: app_commands.Range[str, 1, PARKED_MAX] | None = None,
        notes: app_commands.Range[str, 1, NOTES_MAX] | None = None,
    ) -> None:
        if not await check_group(interaction, GROUP):
            return
        types = await ship_types(self.bot, interaction.guild_id)
        matched = match_ship_type(types, ship_type)
        if matched is None:
            await deny(interaction, f"Unknown ship type. Choose one of: {', '.join(types)}.")
            return
        resolved = self.bot.locations.resolve(hex, town)
        row = await create(
            self.bot,
            interaction.guild_id,
            name=name.strip(),
            ship_type=matched,
            squad=squad.strip(),
            hex_name=resolved.hex,
            region=resolved.region,
            parked_at=(parked_at or "").strip(),
            notes=(notes or "").strip(),
            user_id=interaction.user.id,
        )
        notice = f"Ship added. {location_feedback(resolved.hex, resolved.region, resolved.known)}"
        await ShipPanel(self.bot, interaction.guild_id, row["id"], interaction.user.id).send(interaction, notice=notice)

    @add.autocomplete("hex")
    async def add_hex_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.hex_choices(current)

    @add.autocomplete("town")
    async def add_town_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.region_choices(getattr(interaction.namespace, "hex", None), current)

    @add.autocomplete("ship_type")
    async def add_type_autocomplete(self, interaction: discord.Interaction, current: str):
        types = await ship_types(self.bot, interaction.guild_id)
        return [app_commands.Choice(name=t[:100], value=t[:100]) for t in types if current.lower() in t.lower()][:25]

    @group.command(name="open", description="Open a ship's panel to refresh, edit or delete it.")
    @app_commands.describe(ship="Search by name, type, squad or location")
    async def open(self, interaction: discord.Interaction, ship: str) -> None:
        await open_panel(self.bot, interaction, ship)

    @open.autocomplete("ship")
    async def open_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._entry_choices(interaction, current)

    @group.command(name="refresh", description="Reset a ship's squad-lock timer after renewing it in game.")
    @app_commands.describe(ship="Search by name, type, squad or location")
    async def refresh(self, interaction: discord.Interaction, ship: str) -> None:
        if not await check_group(interaction, GROUP):
            return
        entry = await fetch(self.bot, interaction.guild_id, ship)
        if entry is None:
            await deny(interaction, "Ship not found. Pick one from the suggestions.")
            return
        updated = await refresh_timer(self.bot, entry, interaction.user.id)
        await reply(interaction, refreshed_notice(updated))

    @refresh.autocomplete("ship")
    async def refresh_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._entry_choices(interaction, current)

    @group.command(name="list", description="List every tracked ship (only you see the list).")
    async def list_command(self, interaction: discord.Interaction) -> None:
        if not await check_group(interaction, GROUP):
            return
        rows = await list_entries(self.bot, interaction.guild_id)
        now = timeutil.now()
        embeds = paginate_embeds(
            "Ships",
            entries.grouped_lines(rows, lambda e: board_line(e, now), sort_key=sort_key),
            colour=Colour.INFO,
            empty="No ships are tracked yet.",
            footer=f"{len(rows)} ship(s)",
        )
        await EmbedPaginator(embeds, owner_id=interaction.user.id).start(interaction)

    @group.command(name="board", description="Admin: post the live ship board in this channel (moves it if it exists).")
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
        await reply(interaction, f"Ship board posted.{hint}")


async def setup(bot: FoxBot) -> None:
    bot.boards.register(ShipBoard(bot))
    bot.alerts.register(ShipAlerts(bot))
    await bot.add_cog(Ships(bot))
