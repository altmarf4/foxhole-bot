from __future__ import annotations

import math
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from foxbot.core.locations import Location
from foxbot.core.text import md
from foxbot.core.ui import BaseModal, BaseView, edit, option, reply, text_field, text_value

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

OnPicked = Callable[[discord.Interaction, Location], Awaitable[None]]

WHOLE_HEX = "__whole__"
MORE_PLACES = "__more__"
MAJOR_PLACES = "__major__"
PAGE_SIZE = 22

LOCATION_TABLES = ("stockpiles", "ships", "msupps_bases", "orders")


async def recent_hexes(bot: FoxBot, guild_id: int, limit: int = 25) -> list[str]:
    union = " UNION ALL ".join(f"SELECT hex FROM {table} WHERE guild_id = ?" for table in LOCATION_TABLES)
    rows = await bot.db.fetchall(
        f"SELECT hex, COUNT(*) AS uses FROM ({union}) WHERE hex != '' GROUP BY hex ORDER BY uses DESC, hex LIMIT ?",
        (*([guild_id] * len(LOCATION_TABLES)), limit),
    )
    known = set(bot.locations.hex_names())
    return [row["hex"] for row in rows if row["hex"] in known]


def split_evenly(names: list[str], max_size: int = 25) -> list[list[str]]:
    if not names:
        return []
    groups = math.ceil(len(names) / max_size)
    size = math.ceil(len(names) / groups)
    return [names[i : i + size] for i in range(0, len(names), size)]


class TypedLocationModal(BaseModal, title="Type a location"):
    def __init__(self, picker: LocationPicker):
        super().__init__()
        self.picker = picker
        self.location = text_field(
            "Location",
            placeholder="e.g. Syrinx Pass, or Syrinx Pass, Piper's Enclave",
            description="Town or region. Add the hex after a comma if it is ambiguous.",
            max_length=100,
        )
        self.add_item(self.location)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        resolved = self.picker.bot.locations.resolve_text(text_value(self.location))
        await self.picker.show_typed(interaction, resolved)


class LocationPicker(BaseView):
    def __init__(self, bot: FoxBot, owner_id: int, on_picked: OnPicked, *, prompt: str, recent: list[str]):
        super().__init__(owner_id=owner_id)
        self.bot = bot
        self.on_picked = on_picked
        self.prompt = prompt
        self.recent = recent
        self.stage = "hex"
        self.hex_name: str | None = None
        self.minor = False
        self.page = 0
        self.typed: Location | None = None
        self._build()

    @classmethod
    async def create(cls, bot: FoxBot, interaction: discord.Interaction, on_picked: OnPicked, *, prompt: str) -> LocationPicker:
        return cls(bot, interaction.user.id, on_picked, prompt=prompt, recent=await recent_hexes(bot, interaction.guild_id))

    async def start(self, interaction: discord.Interaction) -> None:
        await reply(interaction, self.content(), view=self)

    def content(self) -> str:
        if self.stage == "hex":
            return f"{self.prompt}\n**Step 1:** pick the hex."
        if self.stage == "typed" and self.typed is not None:
            if self.typed.known:
                return f"{self.prompt}\nLocation: **{md(self.typed.label())}**. Press **Continue**."
            label = self.typed.label() or "(empty)"
            return (
                f"{self.prompt}\n**{md(label)}** is not on the war map. Press **Continue** to use it exactly as typed, "
                "or **Back** to pick from the lists."
            )
        kind = "smaller places" if self.minor else "towns"
        return f"{self.prompt}\n**Step 2:** pick one of the {kind} in **{md(self.hex_name or '')}**."

    def _regions(self) -> list[str]:
        hex_ = self.bot.locations.find_hex(self.hex_name or "")
        if hex_ is None:
            return []
        return [r.name for r in hex_.regions if r.major != self.minor]

    def _build(self) -> None:
        self.clear_items()
        if self.stage == "hex":
            self._build_hex()
        elif self.stage == "town":
            self._build_town()
        else:
            self._add_button("Continue", discord.ButtonStyle.success, self._continue_typed, row=0)
            self._add_button("Back", discord.ButtonStyle.secondary, self._back, row=0)

    def _build_hex(self) -> None:
        row = 0
        if self.recent:
            self._add_select("Hexes your server uses...", self.recent, self._hex_picked, row)
            row += 1
        groups = split_evenly(self.bot.locations.hex_names())
        for group in groups[: 4 - row]:
            placeholder = f"Other hexes {group[0][0]}-{group[-1][0]}..."
            self._add_select(placeholder, group, self._hex_picked, row)
            row += 1
        self._add_button("Type it instead", discord.ButtonStyle.secondary, self._type_instead, row=4)

    def _build_town(self) -> None:
        regions = self._regions()
        pages = max(1, math.ceil(len(regions) / PAGE_SIZE))
        self.page = min(self.page, pages - 1)
        chunk = regions[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]
        options = [option(name, name) for name in chunk]
        options.append(option(f"Somewhere else in {self.hex_name}", WHOLE_HEX, "No specific town"))
        if self.minor:
            options.append(option("Back to the main towns", MAJOR_PLACES))
        elif self.bot.locations.find_hex(self.hex_name or "") and any(not r.major for r in self.bot.locations.find_hex(self.hex_name).regions):
            options.append(option("More places...", MORE_PLACES, "Smaller map locations in this hex"))
        select = discord.ui.Select(placeholder="Pick a town...", options=options, row=0)
        select.callback = self._town_picked
        self.add_item(select)
        if pages > 1:
            self._add_button("Previous", discord.ButtonStyle.secondary, self._previous, row=1, disabled=self.page == 0)
            self._add_button(f"Page {self.page + 1}/{pages}", discord.ButtonStyle.secondary, None, row=1, disabled=True)
            self._add_button("Next", discord.ButtonStyle.secondary, self._next, row=1, disabled=self.page >= pages - 1)
        self._add_button("Back", discord.ButtonStyle.secondary, self._back, row=2)

    def _add_select(self, placeholder: str, names: list[str], callback, row: int) -> None:
        select = discord.ui.Select(placeholder=placeholder, options=[option(n, n) for n in names[:25]], row=row)
        select.callback = lambda interaction, s=select: callback(interaction, s.values[0])
        self.add_item(select)

    def _add_button(self, label: str, style: discord.ButtonStyle, callback, *, row: int, disabled: bool = False) -> None:
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        if callback is not None:
            button.callback = callback
        self.add_item(button)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await edit(interaction, content=self.content(), view=self)

    async def _hex_picked(self, interaction: discord.Interaction, value: str) -> None:
        self.hex_name = value
        self.stage = "town"
        self.minor = False
        self.page = 0
        await self._redraw(interaction)

    async def _town_picked(self, interaction: discord.Interaction) -> None:
        select = next(item for item in self.children if isinstance(item, discord.ui.Select))
        value = select.values[0]
        if value == MORE_PLACES:
            self.minor, self.page = True, 0
            await self._redraw(interaction)
            return
        if value == MAJOR_PLACES:
            self.minor, self.page = False, 0
            await self._redraw(interaction)
            return
        region = "" if value == WHOLE_HEX else value
        await self.on_picked(interaction, Location(self.hex_name or "", region, True))

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._redraw(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        await self._redraw(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        self.stage = "hex"
        self.typed = None
        await self._redraw(interaction)

    async def _type_instead(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TypedLocationModal(self))

    async def show_typed(self, interaction: discord.Interaction, location: Location) -> None:
        self.typed = location
        self.stage = "typed"
        await self._redraw(interaction)

    async def _continue_typed(self, interaction: discord.Interaction) -> None:
        if self.typed is None or not (self.typed.hex or self.typed.region):
            self.stage = "hex"
            await self._redraw(interaction)
            return
        await self.on_picked(interaction, self.typed)
