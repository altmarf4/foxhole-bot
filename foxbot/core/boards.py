from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import discord
from discord.ext import tasks

from foxbot.core.ui import deny, report_error

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0
PERIODIC_REFRESH_MINUTES = 10


@dataclass
class BoardButton:
    action: str
    label: str
    style: discord.ButtonStyle = discord.ButtonStyle.secondary


@dataclass
class BoardRender:
    embeds: list[discord.Embed]
    options: list[discord.SelectOption] = field(default_factory=list)
    placeholder: str = "Select an entry..."
    buttons: list[BoardButton] = field(default_factory=list)


class BoardProvider(Protocol):
    kind: str
    title: str

    async def render(self, guild: discord.Guild) -> BoardRender: ...

    async def on_button(self, interaction: discord.Interaction, action: str) -> None: ...

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None: ...


def build_board_view(kind: str, render: BoardRender) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if render.options:
        view.add_item(
            discord.ui.Select(
                custom_id=f"fb:{kind}:pick",
                placeholder=render.placeholder[:150],
                options=render.options[:25],
                row=0,
            )
        )
    for index, button in enumerate(render.buttons[:10]):
        view.add_item(
            discord.ui.Button(
                label=button.label,
                style=button.style,
                custom_id=f"fb:{kind}:btn:{button.action}",
                row=1 + index // 5,
            )
        )
    return view


class BoardButtonItem(discord.ui.DynamicItem[discord.ui.Button], template=r"fb:(?P<kind>[a-z_]+):btn:(?P<action>[a-z_]+)"):
    def __init__(self, kind: str, action: str):
        super().__init__(discord.ui.Button(label=action, custom_id=f"fb:{kind}:btn:{action}"))
        self.kind = kind
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["kind"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        provider = interaction.client.boards.providers.get(self.kind)
        if provider is None or interaction.guild is None:
            await deny(interaction, "This board is no longer active.")
            return
        try:
            await provider.on_button(interaction, self.action)
        except Exception as error:
            await report_error(interaction, error, f"board {self.kind} button {self.action}")


class BoardSelectItem(discord.ui.DynamicItem[discord.ui.Select], template=r"fb:(?P<kind>[a-z_]+):pick"):
    def __init__(self, kind: str):
        super().__init__(discord.ui.Select(custom_id=f"fb:{kind}:pick", options=[discord.SelectOption(label="-")]))
        self.kind = kind

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match):
        return cls(match["kind"])

    async def callback(self, interaction: discord.Interaction) -> None:
        provider = interaction.client.boards.providers.get(self.kind)
        values = self.item.values
        if provider is None or interaction.guild is None or not values:
            await deny(interaction, "This board is no longer active.")
            return
        try:
            await provider.on_pick(interaction, str(values[0]))
        except Exception as error:
            await report_error(interaction, error, f"board {self.kind} pick")


class BoardService:
    def __init__(self, bot: FoxBot):
        self.bot = bot
        self.providers: dict[str, BoardProvider] = {}
        self._scheduled: set[tuple[int, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        self._locks: defaultdict[tuple[int, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    def register(self, provider: BoardProvider) -> None:
        self.providers[provider.kind] = provider

    def start(self) -> None:
        if not self._periodic.is_running():
            self._periodic.start()

    def stop(self) -> None:
        self._periodic.cancel()
        for task in list(self._tasks):
            task.cancel()

    async def record(self, guild_id: int, kind: str) -> dict | None:
        return await self.bot.db.fetchone(
            "SELECT channel_id, message_id FROM boards WHERE guild_id = ? AND kind = ?",
            (guild_id, kind),
        )

    async def channel_id_for(self, guild_id: int, kind: str) -> int | None:
        record = await self.record(guild_id, kind)
        return record["channel_id"] if record else None

    async def post(self, guild: discord.Guild, kind: str, channel: discord.abc.Messageable) -> discord.Message:
        provider = self.providers[kind]
        async with self._locks[(guild.id, kind)]:
            render = await provider.render(guild)
            message = await channel.send(embeds=render.embeds, view=build_board_view(kind, render))
            old = await self.record(guild.id, kind)
            await self.bot.db.execute(
                "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, kind) DO UPDATE SET channel_id = excluded.channel_id, message_id = excluded.message_id",
                (guild.id, kind, message.channel.id, message.id),
            )
        if old and old["message_id"] != message.id:
            await self._delete_message(old["channel_id"], old["message_id"])
        return message

    async def remove(self, guild_id: int, kind: str) -> bool:
        old = await self.record(guild_id, kind)
        if not old:
            return False
        await self.bot.db.execute("DELETE FROM boards WHERE guild_id = ? AND kind = ?", (guild_id, kind))
        await self._delete_message(old["channel_id"], old["message_id"])
        return True

    async def _delete_message(self, channel_id: int, message_id: int) -> None:
        try:
            await self.bot.get_partial_messageable(channel_id).get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass

    def request_refresh(self, guild_id: int | None, kind: str) -> None:
        if guild_id is None:
            return
        key = (guild_id, kind)
        if key in self._scheduled:
            return
        self._scheduled.add(key)
        task = asyncio.get_running_loop().create_task(self._delayed_refresh(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _delayed_refresh(self, key: tuple[int, str]) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        self._scheduled.discard(key)
        await self.refresh(*key)

    async def refresh(self, guild_id: int, kind: str) -> None:
        provider = self.providers.get(kind)
        guild = self.bot.get_guild(guild_id)
        if provider is None or guild is None:
            return
        async with self._locks[(guild_id, kind)]:
            record = await self.record(guild_id, kind)
            if not record:
                return
            try:
                render = await provider.render(guild)
                message = self.bot.get_partial_messageable(record["channel_id"]).get_partial_message(record["message_id"])
                await message.edit(embeds=render.embeds, view=build_board_view(kind, render))
            except discord.NotFound:
                log.info("Board %s in guild %s was deleted; forgetting it", kind, guild_id)
                await self.bot.db.execute(
                    "DELETE FROM boards WHERE guild_id = ? AND kind = ? AND message_id = ?",
                    (guild_id, kind, record["message_id"]),
                )
            except discord.HTTPException:
                log.warning("Could not refresh board %s in guild %s", kind, guild_id, exc_info=True)
            except Exception:
                log.exception("Board %s render failed in guild %s", kind, guild_id)

    @tasks.loop(minutes=PERIODIC_REFRESH_MINUTES)
    async def _periodic(self) -> None:
        rows = await self.bot.db.fetchall("SELECT guild_id, kind FROM boards")
        for row in rows:
            await self.refresh(row["guild_id"], row["kind"])
            await asyncio.sleep(1)

    @_periodic.before_loop
    async def _before_periodic(self) -> None:
        await self.bot.wait_until_ready()
