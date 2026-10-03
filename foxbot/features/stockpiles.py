from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import PRIORITIES, PRIORITY_ORDER, STOCKPILE_LIFETIME_HOURS, Colour
from foxbot.core import entries, ids, timeutil
from foxbot.core import settings as keys
from foxbot.core.alerts import TimedEntry
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.locations import Location
from foxbot.core.locpicker import LocationPicker
from foxbot.core.permissions import check_admin, check_group, interaction_member
from foxbot.core.text import chunk_lines, clip, code, fit_board_embeds, md, paginate_embeds
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

KIND = "stockpile"
GROUP = "stockpile"
LIFETIME_SECONDS = timeutil.hours_to_seconds(STOCKPILE_LIFETIME_HOURS)

SavedCallback = Callable[[discord.Interaction, dict, str], Awaitable[None]]


async def fetch(bot: FoxBot, guild_id: int, entry_id: str) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM stockpiles WHERE guild_id = ? AND id = ?", (guild_id, entry_id))


async def list_entries(bot: FoxBot, guild_id: int, *, private: bool) -> list[dict]:
    return await bot.db.fetchall(
        "SELECT * FROM stockpiles WHERE guild_id = ? AND private = ? ORDER BY expires_at",
        (guild_id, int(private)),
    )


async def create(
    bot: FoxBot,
    guild_id: int,
    *,
    name: str,
    hex_name: str,
    region: str,
    store_type: str,
    priority: str,
    access_code: str,
    private: bool,
    user_id: int,
) -> dict:
    entry_id = await ids.unique_id(bot.db, "stockpiles")
    now = timeutil.now()
    expires_at = now + LIFETIME_SECONDS
    await bot.db.execute(
        "INSERT INTO stockpiles (id, guild_id, name, hex, region, store_type, priority, code, expires_at, private, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, guild_id, name, hex_name, region, store_type, priority, access_code, expires_at, int(private), user_id, now, now),
    )
    await bot.alerts.reset(KIND, entry_id, guild_id, expires_at, now)
    if not private:
        bot.boards.request_refresh(guild_id, KIND)
    return await fetch(bot, guild_id, entry_id)


async def update(bot: FoxBot, entry: dict, **fields) -> dict | None:
    fields["updated_at"] = timeutil.now()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    await bot.db.execute(
        f"UPDATE stockpiles SET {assignments} WHERE guild_id = ? AND id = ?",
        (*fields.values(), entry["guild_id"], entry["id"]),
    )
    if not entry["private"]:
        bot.boards.request_refresh(entry["guild_id"], KIND)
    return await fetch(bot, entry["guild_id"], entry["id"])


async def refresh_timer(bot: FoxBot, entry: dict, user_id: int) -> dict | None:
    now = timeutil.now()
    expires_at = now + LIFETIME_SECONDS
    updated = await update(bot, entry, expires_at=expires_at, refreshed_by=user_id, refreshed_at=now)
    await bot.alerts.reset(KIND, entry["id"], entry["guild_id"], expires_at, now)
    return updated


async def delete(bot: FoxBot, entry: dict) -> None:
    async with bot.db.transaction() as tx:
        await tx.execute("DELETE FROM stockpiles WHERE guild_id = ? AND id = ?", (entry["guild_id"], entry["id"]))
        await entries.delete_children(tx, KIND, entry["id"])
    await bot.alerts.forget(KIND, entry["id"])
    if not entry["private"]:
        bot.boards.request_refresh(entry["guild_id"], KIND)


async def can_access(bot: FoxBot, member: discord.Member, entry: dict) -> bool:
    if not entry["private"]:
        return await bot.perms.has(member, GROUP)
    if await bot.perms.is_admin(member) or member.id == entry["created_by"]:
        return True
    users, roles = await entries.access_for(bot.db, KIND, entry["id"])
    return member.id in users or any(role.id in roles for role in member.roles)


async def can_manage_access(bot: FoxBot, member: discord.Member, entry: dict) -> bool:
    return member.id == entry["created_by"] or await bot.perms.is_admin(member)


async def visible_entries(bot: FoxBot, member: discord.Member) -> list[dict]:
    result = []
    if await bot.perms.has(member, GROUP):
        result.extend(await list_entries(bot, member.guild.id, private=False))
    for entry in await list_entries(bot, member.guild.id, private=True):
        if await can_access(bot, member, entry):
            result.append(entry)
    return result


def location_of(entry: dict) -> str:
    return entries.location_label(entry["hex"], entry["region"])


def board_line(entry: dict, now: int | None = None) -> str:
    who = entry.get("refreshed_by") or entry["created_by"]
    return (
        f"{code(entry['code'])} **{md(entry['name'])}** [{entry['priority']}] "
        f"{entries.timer_text(entry['expires_at'], now)} - <@{who}>"
    )


def option_description(entry: dict, now: int) -> str:
    state = "EXPIRED" if entry["expires_at"] <= now else f"{timeutil.format_duration(entry['expires_at'] - now)} left"
    return f"Code {entry['code']} | {entry['priority']} | {state}"


def sort_key(entry: dict) -> tuple:
    return (entry["expires_at"], PRIORITY_ORDER.get(entry["priority"], 99), entry["name"].lower())


def card_embed(
    entry: dict,
    *,
    alert_role_ids: list[int],
    access: tuple[set[int], set[int]] | None,
    now: int | None = None,
) -> discord.Embed:
    now = now if now is not None else timeutil.now()
    colour = Colour.PRIVATE if entry["private"] else PRIORITIES.get(entry["priority"], Colour.MUTED)
    title = f"{'[Private] ' if entry['private'] else ''}{entry['name']}"
    embed = discord.Embed(title=clip(title, 256), description=md(location_of(entry)), colour=colour)
    embed.add_field(name="Storage", value=md(entry["store_type"]), inline=True)
    embed.add_field(name="Priority", value=entry["priority"], inline=True)
    embed.add_field(name="Code", value=f"```{entry['code']}```", inline=False)
    if entry["expires_at"] <= now:
        timer = "**EXPIRED** - the stockpile is no longer reserved"
    else:
        timer = f"{timeutil.relative(entry['expires_at'])} ({timeutil.full(entry['expires_at'])})"
    embed.add_field(name="Timer", value=timer, inline=False)
    embed.add_field(name="Last refreshed", value=entries.refreshed_text(entry), inline=False)
    embed.add_field(
        name="Reminder pings",
        value=", ".join(f"<@&{rid}>" for rid in alert_role_ids) if alert_role_ids else "Logistics role only",
        inline=False,
    )
    if access is not None:
        users, roles = access
        holders = [f"<@{entry['created_by']}> (creator)"]
        holders += [f"<@{uid}>" for uid in sorted(users) if uid != entry["created_by"]]
        holders += [f"<@&{rid}>" for rid in sorted(roles)]
        embed.add_field(name="Access", value=clip(", ".join(holders), 1024), inline=False)
    embed.add_field(name="Added by", value=f"<@{entry['created_by']}> {timeutil.relative(entry['created_at'])}", inline=False)
    embed.set_footer(text=f"ID {entry['id']}")
    return embed


async def card(bot: FoxBot, entry: dict) -> tuple[discord.Embed, list[discord.File]]:
    access = await entries.access_for(bot.db, KIND, entry["id"]) if entry["private"] else None
    embed = card_embed(entry, alert_role_ids=await entries.alert_roles(bot.db, KIND, entry["id"]), access=access)
    files = await entries.attach_screenshot(bot.db, KIND, entry["id"], embed)
    return embed, files


def location_feedback(hex_name: str, region: str, known: bool) -> str:
    label = entries.location_label(hex_name, region)
    if known:
        return f"Location: **{md(label)}**"
    return f"Location **{md(label)}** was not found on the war map, so it was saved exactly as typed. Use Edit to fix it."


class StockpileModal(BaseModal):
    def __init__(
        self,
        bot: FoxBot,
        guild_id: int,
        storage_types: list[str],
        on_saved: SavedCallback,
        *,
        entry: dict | None = None,
        private: bool = False,
        location: Location | None = None,
        prefill: dict | None = None,
    ):
        if entry is not None:
            title = "Edit Stockpile"
        else:
            title = "Add Private Stockpile" if private else "Add Stockpile"
        super().__init__(title=title)
        self.bot = bot
        self.guild_id = guild_id
        self.entry = entry
        self.private = private
        self.on_saved = on_saved
        self.location = location
        prefill = prefill or {}
        prefill_name = clip(str(prefill["name"]), 60) if prefill.get("name") else None
        self.name_field = text_field(
            "Name", default=entry["name"] if entry else prefill_name, placeholder="e.g. KM Main", max_length=60
        )
        self.location_field = None
        if location is None:
            self.location_field = text_field(
                "Location",
                default=location_of(entry) if entry else None,
                placeholder="e.g. Syrinx Pass",
                description="Town or region name. Add the hex after a comma if it is ambiguous.",
                max_length=100,
            )
        self.code_field = text_field(
            "Access code",
            default=entry["code"] if entry else None,
            placeholder="The stockpile's reserve code",
            max_length=20,
        )
        types = list(storage_types)
        preset_type = prefill.get("store_type") if prefill.get("store_type") in types else (types[0] if types else None)
        self.type_field = select_field("Storage type", types, default=entry["store_type"] if entry else preset_type)
        self.priority_field = select_field("Priority", list(PRIORITIES), default=entry["priority"] if entry else "Medium")
        for item in (self.name_field, self.location_field, self.code_field, self.type_field, self.priority_field):
            if item is not None:
                self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        location = self.location or self.bot.locations.resolve_text(text_value(self.location_field))
        if not (location.hex or location.region):
            await deny(interaction, "A location is required.")
            return
        values = {
            "name": text_value(self.name_field),
            "hex_name": location.hex,
            "region": location.region,
            "store_type": select_value(self.type_field) or "Storage Depot",
            "priority": select_value(self.priority_field) or "Medium",
            "access_code": text_value(self.code_field),
        }
        member = await interaction_member(interaction, self.guild_id)
        if member is None:
            await deny(interaction, "This only works inside the server.")
            return
        if self.entry is None:
            if not await self.bot.perms.has(member, GROUP):
                await deny(interaction, "You no longer have the Stockpiles permission.")
                return
            saved = await create(self.bot, self.guild_id, private=self.private, user_id=interaction.user.id, **values)
        else:
            current = await fetch(self.bot, self.guild_id, self.entry["id"])
            if current is None:
                await deny(interaction, "That stockpile no longer exists.")
                return
            if not await can_access(self.bot, member, current):
                await deny(interaction, "You no longer have access to this stockpile.")
                return
            saved = await update(
                self.bot,
                current,
                name=values["name"],
                hex=values["hex_name"],
                region=values["region"],
                store_type=values["store_type"],
                priority=values["priority"],
                code=values["access_code"],
            )
        await self.on_saved(interaction, saved, location_feedback(location.hex, location.region, location.known))


class ScreenshotModal(BaseModal, title="Stockpile Screenshot"):
    def __init__(self, panel: StockpilePanel):
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
    def __init__(self, panel: StockpilePanel, current: list[int]):
        super().__init__()
        self.panel = panel
        self.roles = discord.ui.Label(
            text="Extra roles to ping",
            description="Pinged together with the logistics role when this stockpile's timer runs low.",
            component=discord.ui.RoleSelect(
                min_values=0,
                max_values=10,
                required=False,
                default_values=[discord.SelectDefaultValue(id=rid, type=discord.SelectDefaultValueType.role) for rid in current],
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


class AccessModal(BaseModal, title="Private Stockpile Access"):
    def __init__(self, panel: StockpilePanel, users: set[int], roles: set[int]):
        super().__init__()
        self.panel = panel
        defaults = [discord.SelectDefaultValue(id=uid, type=discord.SelectDefaultValueType.user) for uid in sorted(users)]
        defaults += [discord.SelectDefaultValue(id=rid, type=discord.SelectDefaultValueType.role) for rid in sorted(roles)]
        self.targets = discord.ui.Label(
            text="People and roles with access",
            description="They can see the code, refresh the timer and get reminder DMs. Admins always have access.",
            component=discord.ui.MentionableSelect(min_values=0, max_values=25, required=False, default_values=defaults[:25]),
        )
        self.add_item(self.targets)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        entry = await self.panel.load_checked(interaction)
        if entry is None:
            return
        member = await interaction_member(interaction, entry["guild_id"])
        if member is None or not await can_manage_access(self.panel.bot, member, entry):
            await deny(interaction, "Only the creator or an admin can change who has access.")
            return
        users = {v.id for v in self.targets.component.values if not isinstance(v, discord.Role)}
        roles = {v.id for v in self.targets.component.values if isinstance(v, discord.Role)}
        await entries.set_access(self.panel.bot.db, KIND, entry["id"], users, roles)
        await self.panel.show(interaction, notice="Access updated.")


class StockpilePanel(BaseView):
    def __init__(self, bot: FoxBot, guild_id: int, entry_id: str, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.entry_id = entry_id

    async def load_checked(self, interaction: discord.Interaction) -> dict | None:
        entry = await fetch(self.bot, self.guild_id, self.entry_id)
        if entry is None:
            await edit(interaction, content="This stockpile no longer exists.", embed=None, attachments=[], view=None)
            return None
        member = await interaction_member(interaction, self.guild_id)
        if member is None or not await can_access(self.bot, member, entry):
            await deny(interaction, "You do not have access to this stockpile.")
            return None
        return entry

    async def _build(self, entry: dict, member: discord.Member | None) -> None:
        self.clear_items()
        self._button("Refresh Timer", discord.ButtonStyle.success, self._refresh, row=0)
        self._button("Edit", discord.ButtonStyle.primary, self._edit, row=0)
        self._button("Screenshot", discord.ButtonStyle.secondary, self._screenshot, row=0)
        self._button("Reminder Pings", discord.ButtonStyle.secondary, self._alert_roles, row=0)
        self._button("Inventory", discord.ButtonStyle.secondary, self._inventory, row=0)
        subscribed = member is not None and await entries.is_subscribed(self.bot.db, KIND, entry["id"], member.id)
        self._button("Stop Notifying Me" if subscribed else "Notify Me", discord.ButtonStyle.secondary, self._notify, row=1)
        if entry["private"] and member is not None and await can_manage_access(self.bot, member, entry):
            self._button("Access", discord.ButtonStyle.primary, self._access, row=1)
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
            await deny(interaction, "That stockpile no longer exists.")
            return
        await reply(interaction, notice, embed=embed, view=self, files=files or None)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        entry, embed, files = await self.render(interaction)
        if entry is None:
            await edit(interaction, content="This stockpile no longer exists.", embed=None, attachments=[], view=None)
            return
        await edit(interaction, content=notice, embed=embed, attachments=files, view=self)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        updated = await refresh_timer(self.bot, entry, interaction.user.id)
        await self.show(interaction, notice=f"Timer refreshed. **{md(updated['name'])}** now expires {timeutil.relative(updated['expires_at'])}.")

    async def _notify(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        subscribed = await entries.toggle_subscription(self.bot.db, KIND, entry["id"], interaction.user.id)
        notice = "You will get a DM for this stockpile's reminders." if subscribed else "You will no longer get DMs for this stockpile."
        await self.show(interaction, notice=notice)

    async def _inventory(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        from foxbot.features.inventory import show_for_stockpile

        await show_for_stockpile(self.bot, interaction, entry)

    async def _edit(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return

        async def saved(modal_interaction: discord.Interaction, row: dict, feedback: str) -> None:
            await self.show(modal_interaction, notice=f"Saved. {feedback}")

        types = await self.bot.settings.get(self.guild_id, keys.STORAGE_TYPES)
        await interaction.response.send_modal(StockpileModal(self.bot, self.guild_id, types, saved, entry=entry))

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

    async def _access(self, interaction: discord.Interaction) -> None:
        entry = await self.load_checked(interaction)
        if entry is None:
            return
        member = await interaction_member(interaction, self.guild_id)
        if member is None or not await can_manage_access(self.bot, member, entry):
            await deny(interaction, "Only the creator or an admin can change who has access.")
            return
        users, roles = await entries.access_for(self.bot.db, KIND, entry["id"])
        await interaction.response.send_modal(AccessModal(self, users, roles))

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
        await deny(interaction, "That stockpile no longer exists.")
        return
    member = await interaction_member(interaction)
    if member is None or not await can_access(bot, member, entry):
        await deny(interaction, "You do not have access to that stockpile.")
        return
    panel = StockpilePanel(bot, interaction.guild_id, entry_id, interaction.user.id)
    if replace:
        await panel.show(interaction, notice=notice)
    else:
        await panel.send(interaction, notice=notice)


async def start_add(bot: FoxBot, interaction: discord.Interaction, *, private: bool) -> None:
    if not await check_group(interaction, GROUP):
        return

    async def saved(modal_interaction: discord.Interaction, row: dict, feedback: str) -> None:
        notice = f"Stockpile added. {feedback}"
        if private:
            notice += "\nOnly you and admins can see it until you add people with **Access**."
        panel = StockpilePanel(bot, row["guild_id"], row["id"], modal_interaction.user.id)
        await panel.show(modal_interaction, notice=notice)

    async def picked(pick_interaction: discord.Interaction, location: Location) -> None:
        types = await bot.settings.get(pick_interaction.guild_id, keys.STORAGE_TYPES)
        await pick_interaction.response.send_modal(
            StockpileModal(bot, pick_interaction.guild_id, types, saved, private=private, location=location)
        )

    prompt = "**Add a private stockpile**" if private else "**Add a stockpile**"
    picker = await LocationPicker.create(bot, interaction, picked, prompt=prompt)
    await picker.start(interaction)


def picker_options(rows: list[dict], now: int) -> list[PickOption]:
    return [
        PickOption(row["id"], f"{row['name']} - {location_of(row)}", option_description(row, now))
        for row in sorted(rows, key=lambda r: (location_of(r).lower(), r["name"].lower()))
    ]


async def show_all(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await check_group(interaction, GROUP):
        return
    rows = await list_entries(bot, interaction.guild_id, private=False)
    if not rows:
        await reply(interaction, "No stockpiles are tracked yet.")
        return

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await open_panel(bot, pick_interaction, value)

    view = PickerView(picker_options(rows, timeutil.now()), picked, owner_id=interaction.user.id, placeholder="Pick a stockpile to manage...")
    await reply(interaction, f"{len(rows)} stockpile(s), sorted by location.", view=view)


async def show_private(bot: FoxBot, interaction: discord.Interaction) -> None:
    member = await interaction_member(interaction)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return
    rows = [row for row in await list_entries(bot, member.guild.id, private=True) if await can_access(bot, member, row)]
    now = timeutil.now()
    lines = entries.grouped_lines(rows, lambda e: board_line(e, now), sort_key=sort_key)
    description = chunk_lines(lines, 3800)[0] if lines else "You do not have access to any private stockpiles."
    embed = discord.Embed(title="Private Stockpiles", description=description, colour=Colour.PRIVATE)
    embed.set_footer(text="Only you can see this list.")
    add_button = discord.ui.Button(label="Add Private Stockpile", style=discord.ButtonStyle.success)

    async def add_clicked(button_interaction: discord.Interaction) -> None:
        await start_add(bot, button_interaction, private=True)

    add_button.callback = add_clicked

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        await open_panel(bot, pick_interaction, value)

    view = PickerView(
        picker_options(rows, now),
        picked,
        owner_id=interaction.user.id,
        placeholder="Pick a private stockpile...",
        extra_buttons=[add_button],
    )
    await reply(interaction, embed=embed, view=view)


class StockpileBoard:
    kind = KIND
    title = "Stockpiles"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        rows = await list_entries(self.bot, guild.id, private=False)
        now = timeutil.now()
        lines = entries.grouped_lines(rows, lambda e: board_line(e, now), sort_key=sort_key)
        embeds = fit_board_embeds(
            "Tracked Stockpiles",
            lines,
            colour=Colour.INFO,
            empty="No stockpiles are tracked yet. Press **Add Stockpile** to add one.",
            footer=f"{len(rows)} stockpile(s) | Updates automatically",
            overflow_hint="Too many to show here. Use All Stockpiles or /stockpile list.",
        )
        urgent = sorted(rows, key=sort_key)[:25]
        options = [
            option(f"{row['name']} - {location_of(row)}", row["id"], option_description(row, now))
            for row in urgent
        ]
        return BoardRender(
            embeds=embeds,
            options=options,
            placeholder="Manage a stockpile (most urgent first)...",
            buttons=[
                BoardButton("add", "Add Stockpile", discord.ButtonStyle.success),
                BoardButton("all", "All Stockpiles"),
                BoardButton("private", "Private Stockpiles"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action == "add":
            await start_add(self.bot, interaction, private=False)
        elif action == "all":
            await show_all(self.bot, interaction)
        elif action == "private":
            await show_private(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        if not await check_group(interaction, GROUP):
            return
        await open_panel(self.bot, interaction, value)


class StockpileAlerts:
    kind = KIND
    noun = "Stockpile"
    action_label = "Refresh Timer"
    permission = GROUP

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def timed_entries(self) -> list[TimedEntry]:
        rows = await self.bot.db.fetchall("SELECT id, guild_id, name, hex, region, expires_at, private FROM stockpiles")
        return [
            TimedEntry(KIND, row["guild_id"], row["id"], row["expires_at"], row["name"], location_of(row), bool(row["private"]))
            for row in rows
        ]

    async def alert_roles(self, entry: TimedEntry) -> list[int]:
        return await entries.alert_roles(self.bot.db, KIND, entry.entry_id)

    async def recipients(self, entry: TimedEntry) -> set[int]:
        row = await fetch(self.bot, entry.guild_id, entry.entry_id)
        if row is None:
            return set()
        users, roles = await entries.access_for(self.bot.db, KIND, entry.entry_id)
        result = {row["created_by"], *users}
        guild = self.bot.get_guild(entry.guild_id)
        if guild is not None:
            for role_id in roles:
                role = guild.get_role(role_id)
                if role is not None:
                    result.update(member.id for member in role.members if not member.bot)
        return result

    async def can_notify(self, entry: TimedEntry, member: discord.Member) -> bool:
        row = await fetch(self.bot, entry.guild_id, entry.entry_id)
        return row is not None and await can_access(self.bot, member, row)

    def describe(self, entry: TimedEntry, expired: bool) -> str:
        name = f"**{md(entry.title)}**"
        if expired:
            return f"{name} has expired and is no longer reserved. If it was refreshed in game in time, press **Refresh Timer**."
        return f"{name} expires {timeutil.relative(entry.deadline)}. Refresh it in game, then press **Refresh Timer**."

    async def on_alert_action(self, interaction: discord.Interaction, guild_id: int, entry_id: str) -> None:
        entry = await fetch(self.bot, guild_id, entry_id)
        if entry is None:
            await deny(interaction, "That stockpile is no longer tracked.")
            return
        member = await interaction_member(interaction, guild_id)
        if member is None or not await can_access(self.bot, member, entry):
            await deny(interaction, "You do not have access to that stockpile.")
            return
        now = timeutil.now()
        expires_at = now + LIFETIME_SECONDS
        await self.bot.db.execute(
            "UPDATE stockpiles SET expires_at = ?, updated_at = ?, refreshed_by = ?, refreshed_at = ? WHERE guild_id = ? AND id = ?",
            (expires_at, now, interaction.user.id, now, guild_id, entry_id),
        )
        await reply(interaction, f"Timer refreshed. **{md(entry['name'])}** now expires {timeutil.relative(expires_at)}.")
        await self.bot.alerts.reset(KIND, entry_id, guild_id, expires_at, now)
        if not entry["private"]:
            self.bot.boards.request_refresh(guild_id, KIND)


class Stockpiles(commands.Cog):
    group = app_commands.Group(name="stockpile", description="Track reserved stockpiles and their timers.", guild_only=True)

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
            if not needle or needle in f"{row['name']} {row['code']} {location_of(row)}".lower()
        ]
        matches.sort(key=sort_key)
        return [
            app_commands.Choice(
                name=clip(f"{'[Private] ' if row['private'] else ''}{row['name']} - {location_of(row)} ({row['code']})", 100),
                value=row["id"],
            )
            for row in matches[:25]
        ]

    @group.command(name="add", description="Add a stockpile and start its reserve timer.")
    @app_commands.describe(
        hex="The hex (start typing to search)",
        town="Town in that hex (start typing to search)",
        name="Name shown on the board",
        code="The stockpile's reserve code",
        storage_type="Storage Depot, Seaport, ...",
        priority="How urgent refreshing it is",
        private="Only you, admins and people you add can see it",
    )
    @app_commands.choices(priority=[app_commands.Choice(name=p, value=p) for p in PRIORITIES])
    async def add(
        self,
        interaction: discord.Interaction,
        hex: app_commands.Range[str, 1, 60],
        town: app_commands.Range[str, 1, 100],
        name: app_commands.Range[str, 1, 60],
        code: app_commands.Range[str, 1, 20],
        storage_type: str,
        priority: app_commands.Choice[str] | None = None,
        private: bool = False,
    ) -> None:
        if not await check_group(interaction, GROUP):
            return
        types = await self.bot.settings.get(interaction.guild_id, keys.STORAGE_TYPES)
        if storage_type not in types:
            await deny(interaction, f"Unknown storage type. Choose one of: {', '.join(types)}.")
            return
        resolved = self.bot.locations.resolve(hex, town)
        row = await create(
            self.bot,
            interaction.guild_id,
            name=name.strip(),
            hex_name=resolved.hex,
            region=resolved.region,
            store_type=storage_type,
            priority=priority.value if priority else "Medium",
            access_code=code.strip(),
            private=private,
            user_id=interaction.user.id,
        )
        notice = f"Stockpile added. {location_feedback(resolved.hex, resolved.region, resolved.known)}"
        if private:
            notice += "\nOnly you and admins can see it until you add people with **Access**."
        await StockpilePanel(self.bot, interaction.guild_id, row["id"], interaction.user.id).send(interaction, notice=notice)

    @add.autocomplete("hex")
    async def add_hex_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.hex_choices(current)

    @add.autocomplete("town")
    async def add_town_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.region_choices(getattr(interaction.namespace, "hex", None), current)

    @add.autocomplete("storage_type")
    async def add_type_autocomplete(self, interaction: discord.Interaction, current: str):
        types = await self.bot.settings.get(interaction.guild_id, keys.STORAGE_TYPES)
        return [app_commands.Choice(name=t, value=t) for t in types if current.lower() in t.lower()][:25]

    @group.command(name="open", description="Open a stockpile's panel to refresh, edit or delete it.")
    @app_commands.describe(stockpile="Search by name, code or location")
    async def open(self, interaction: discord.Interaction, stockpile: str) -> None:
        await open_panel(self.bot, interaction, stockpile)

    @open.autocomplete("stockpile")
    async def open_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._entry_choices(interaction, current)

    @group.command(name="refresh", description="Reset a stockpile's timer after refreshing it in game.")
    @app_commands.describe(stockpile="Search by name, code or location")
    async def refresh(self, interaction: discord.Interaction, stockpile: str) -> None:
        entry = await fetch(self.bot, interaction.guild_id, stockpile)
        member = await interaction_member(interaction)
        if entry is None or member is None or not await can_access(self.bot, member, entry):
            await deny(interaction, "Stockpile not found. Pick one from the suggestions.")
            return
        updated = await refresh_timer(self.bot, entry, interaction.user.id)
        await reply(interaction, f"Timer refreshed. **{md(updated['name'])}** now expires {timeutil.relative(updated['expires_at'])}.")

    @refresh.autocomplete("stockpile")
    async def refresh_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._entry_choices(interaction, current)

    @group.command(name="list", description="List every stockpile you can see (only you see the list).")
    async def list_command(self, interaction: discord.Interaction) -> None:
        member = await interaction_member(interaction)
        if member is None:
            return
        rows = await visible_entries(self.bot, member)
        if not rows and not await self.bot.perms.has(member, GROUP):
            await deny(interaction, "You need the **Stockpiles** permission for that.")
            return
        now = timeutil.now()

        def line(entry: dict) -> str:
            return f"{'[Private] ' if entry['private'] else ''}{board_line(entry, now)}"

        embeds = paginate_embeds(
            "Stockpiles",
            entries.grouped_lines(rows, line, sort_key=sort_key),
            colour=Colour.INFO,
            empty="No stockpiles are tracked yet.",
            footer=f"{len(rows)} stockpile(s)",
        )
        await EmbedPaginator(embeds, owner_id=interaction.user.id).start(interaction)

    @group.command(name="private", description="Show the private stockpiles you have access to.")
    async def private(self, interaction: discord.Interaction) -> None:
        await show_private(self.bot, interaction)

    @group.command(name="board", description="Admin: post the live stockpile board in this channel (moves it if it exists).")
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
        await reply(interaction, f"Stockpile board posted.{hint}")


async def setup(bot: FoxBot) -> None:
    bot.boards.register(StockpileBoard(bot))
    bot.alerts.register(StockpileAlerts(bot))
    await bot.add_cog(Stockpiles(bot))
