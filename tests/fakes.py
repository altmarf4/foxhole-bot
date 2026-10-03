from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from foxbot.bot import FoxBot
from foxbot.config import Config
from foxbot.core.locations import SEED_PATH
from foxbot.db import Database

_ids = itertools.count(1000)


def next_id() -> int:
    return next(_ids)


def make_role(guild: discord.Guild, name: str = "Role", role_id: int | None = None) -> discord.Role:
    role = MagicMock(spec=discord.Role)
    role.id = role_id or next_id()
    role.name = name
    role.guild = guild
    role.mention = f"<@&{role.id}>"
    role.members = []
    role.position = 1
    role.__ge__ = lambda self, other: False
    return role


def make_guild(guild_id: int | None = None, owner_id: int = 1) -> discord.Guild:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id or next_id()
    guild.name = "Test Regiment"
    guild.owner_id = owner_id
    guild._roles = {}
    guild._members = {}
    guild._channels = {}
    default_role = make_role(guild, "@everyone", guild.id)
    guild._roles[guild.id] = default_role
    guild.default_role = default_role
    guild.get_role.side_effect = lambda rid: guild._roles.get(rid)
    guild.get_member.side_effect = lambda uid: guild._members.get(uid)
    guild.get_channel.side_effect = lambda cid: guild._channels.get(cid)
    guild.get_channel_or_thread.side_effect = lambda cid: guild._channels.get(cid)
    type(guild).roles = property(lambda self: list(self._roles.values()))
    type(guild).members = property(lambda self: list(self._members.values()))
    guild.fetch_member = AsyncMock(side_effect=lambda uid: guild._members[uid])
    return guild


def add_role(guild: discord.Guild, name: str) -> discord.Role:
    role = make_role(guild, name)
    guild._roles[role.id] = role
    return role


def make_member(
    guild: discord.Guild,
    *,
    user_id: int | None = None,
    name: str = "Member",
    roles: list[discord.Role] | None = None,
    administrator: bool = False,
    bot: bool = False,
) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.id = user_id or next_id()
    member.guild = guild
    member.name = name
    member.display_name = name
    member.mention = f"<@{member.id}>"
    member.bot = bot
    member.roles = [guild.default_role, *(roles or [])]
    member.guild_permissions = discord.Permissions(administrator=administrator)
    member.send = AsyncMock(return_value=make_message())
    for role in roles or []:
        role.members.append(member)
    guild._members[member.id] = member
    return member


def make_message(channel_id: int | None = None) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.id = next_id()
    message.channel = SimpleNamespace(id=channel_id or next_id())
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    return message


def make_channel(guild: discord.Guild, name: str = "channel") -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = next_id()
    channel.name = name
    channel.guild = guild
    channel.mention = f"<#{channel.id}>"
    channel.sent = []

    async def send(*args, **kwargs):
        message = make_message(channel.id)
        message.kwargs = kwargs
        channel.sent.append(kwargs)
        return message

    channel.send = AsyncMock(side_effect=send)
    guild._channels[channel.id] = channel
    return channel


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def record(self, kind: str, kwargs: dict) -> None:
        self.calls.append((kind, kwargs))

    @property
    def last(self) -> tuple[str, dict]:
        return self.calls[-1]

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


class FakeResponse:
    def __init__(self, recorder: Recorder):
        self._done = False
        self.recorder = recorder
        self.modal = None

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._check()
        self.recorder.record("send", {"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self._check()
        self.recorder.record("edit", kwargs)

    async def send_modal(self, modal):
        self._check()
        self.modal = modal
        self.recorder.record("modal", {"modal": modal})

    async def defer(self, **kwargs):
        self._check()
        self.recorder.record("defer", kwargs)

    async def autocomplete(self, choices):
        self._check()
        self.recorder.record("autocomplete", {"choices": choices})

    def _check(self) -> None:
        if self._done:
            raise AssertionError("Interaction was already responded to")
        self._done = True


class FakeFollowup:
    def __init__(self, recorder: Recorder):
        self.recorder = recorder

    async def send(self, content=None, **kwargs):
        self.recorder.record("followup", {"content": content, **kwargs})
        return make_message()


def make_interaction(bot: FoxBot, member: discord.Member, *, channel=None, message=None, namespace=None) -> MagicMock:
    recorder = Recorder()
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.user = member
    interaction.guild = member.guild
    interaction.guild_id = member.guild.id
    interaction.channel = channel
    interaction.channel_id = channel.id if channel is not None else None
    interaction.message = message
    interaction.namespace = namespace or SimpleNamespace()
    interaction.response = FakeResponse(recorder)
    interaction.followup = FakeFollowup(recorder)
    interaction.recorder = recorder

    async def edit_original_response(**kwargs):
        recorder.record("edit_original", kwargs)

    interaction.edit_original_response = AsyncMock(side_effect=edit_original_response)
    return interaction


def config(tmp: Path | None = None) -> Config:
    return Config(
        token="test",
        data_dir=tmp or Path("/nonexistent"),
        dev_guild_id=None,
        log_level="INFO",
        war_api_url="http://127.0.0.1:9",
    )


async def make_bot(extensions: list[str], tmp: Path | None = None) -> FoxBot:
    bot = FoxBot(config(tmp), extensions=extensions, database=Database(":memory:"))
    await bot.db.connect()
    bot.locations._load_file(SEED_PATH)
    bot.boards.request_refresh = MagicMock()
    for name in extensions:
        await bot.load_extension(name)
    return bot


def set_label_value(label: discord.ui.Label, value) -> None:
    component = label.component
    if isinstance(component, discord.ui.TextInput):
        component._value = value
    elif isinstance(component, discord.ui.FileUpload):
        component._values = list(value)
    else:
        component._values = list(value) if isinstance(value, (list, tuple)) else [value]


def select_values(item, values: list) -> None:
    item._values = list(values)


def find_button(view: discord.ui.View, label: str) -> discord.ui.Button:
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label == label:
            return item
    raise AssertionError(f"No button labelled {label!r}; have {[getattr(i, 'label', None) for i in view.children]}")


def find_select(view: discord.ui.View, index: int = 0) -> discord.ui.Select:
    selects = [item for item in view.children if isinstance(item, discord.ui.Select)]
    return selects[index]
