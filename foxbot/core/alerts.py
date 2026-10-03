from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import discord
from discord.ext import tasks

from foxbot.constants import Colour
from foxbot.core import entries
from foxbot.core import settings as keys
from foxbot.core import timeutil
from foxbot.core.permissions import GROUPS, interaction_member
from foxbot.core.text import md
from foxbot.core.ui import deny, report_error

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

EXPIRED = 0.0


@dataclass
class TimedEntry:
    kind: str
    guild_id: int
    entry_id: str
    deadline: int
    title: str
    location: str
    private: bool = False


class AlertProvider(Protocol):
    kind: str
    noun: str
    action_label: str
    permission: str

    async def timed_entries(self) -> list[TimedEntry]: ...

    async def alert_roles(self, entry: TimedEntry) -> list[int]: ...

    async def recipients(self, entry: TimedEntry) -> set[int]: ...

    async def can_notify(self, entry: TimedEntry, member: discord.Member) -> bool: ...

    def describe(self, entry: TimedEntry, expired: bool) -> str: ...

    async def on_alert_action(self, interaction: discord.Interaction, guild_id: int, entry_id: str) -> None: ...


def crossed_thresholds(thresholds: list[float], remaining_seconds: int, sent: set[float]) -> list[float]:
    return [t for t in thresholds if remaining_seconds <= t * 3600 and t not in sent]


class AlertActionItem(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fa:act:(?P<kind>[a-z_]+):(?P<guild>\d+):(?P<entry>[A-Z0-9]+)",
):
    def __init__(self, kind: str, guild_id: int, entry_id: str, label: str = "Act"):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                custom_id=f"fa:act:{kind}:{guild_id}:{entry_id}",
            )
        )
        self.kind = kind
        self.guild_id = guild_id
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["kind"], int(match["guild"]), match["entry"])

    async def callback(self, interaction: discord.Interaction) -> None:
        provider = interaction.client.alerts.providers.get(self.kind)
        if provider is None:
            await deny(interaction, "This reminder is no longer active.")
            return
        try:
            await provider.on_alert_action(interaction, self.guild_id, self.entry_id)
        except Exception as error:
            await report_error(interaction, error, f"alert action {self.kind}")


class AlertDismissItem(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fa:dismiss:(?P<kind>[a-z_]+):(?P<guild>\d+)",
):
    def __init__(self, kind: str, guild_id: int):
        super().__init__(
            discord.ui.Button(
                label="Dismiss",
                style=discord.ButtonStyle.secondary,
                custom_id=f"fa:dismiss:{kind}:{guild_id}",
            )
        )
        self.kind = kind
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["kind"], int(match["guild"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        provider = bot.alerts.providers.get(self.kind)
        if interaction.guild_id is not None and provider is not None:
            member = await interaction_member(interaction, self.guild_id)
            if member is None or not await bot.perms.has(member, provider.permission):
                await deny(interaction, f"You need the **{GROUPS[provider.permission]}** permission to dismiss reminders.")
                return
        await interaction.response.defer()
        if interaction.message is not None:
            await bot.db.execute(
                "DELETE FROM alert_messages WHERE channel_id = ? AND message_id = ?",
                (interaction.message.channel.id, interaction.message.id),
            )
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass


def alert_view(kind: str, guild_id: int, entry_id: str, action_label: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(AlertActionItem(kind, guild_id, entry_id, action_label).item)
    view.add_item(AlertDismissItem(kind, guild_id).item)
    return view


class AlertService:
    def __init__(self, bot: FoxBot):
        self.bot = bot
        self.providers: dict[str, AlertProvider] = {}

    def register(self, provider: AlertProvider) -> None:
        self.providers[provider.kind] = provider

    def start(self) -> None:
        if not self._loop.is_running():
            self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    @tasks.loop(seconds=60)
    async def _loop(self) -> None:
        await self.check()

    @_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def check(self, now: int | None = None) -> None:
        now = now if now is not None else timeutil.now()
        for provider in list(self.providers.values()):
            try:
                timed = await provider.timed_entries()
            except Exception:
                log.exception("Could not load timed entries for %s", provider.kind)
                continue
            for entry in timed:
                try:
                    await self._process(provider, entry, now)
                except Exception:
                    log.exception("Alert processing failed for %s %s", entry.kind, entry.entry_id)

    async def _sent(self, kind: str, entry_id: str) -> set[float]:
        rows = await self.bot.db.fetchall(
            "SELECT threshold FROM alert_state WHERE kind = ? AND entry_id = ?",
            (kind, entry_id),
        )
        return {float(row["threshold"]) for row in rows}

    async def _mark(self, kind: str, entry_id: str, thresholds: list[float], now: int) -> None:
        async with self.bot.db.transaction() as tx:
            await tx.executemany(
                "INSERT OR IGNORE INTO alert_state (kind, entry_id, threshold, sent_at) VALUES (?, ?, ?, ?)",
                [(kind, entry_id, float(t), now) for t in thresholds],
            )

    async def _process(self, provider: AlertProvider, entry: TimedEntry, now: int) -> None:
        thresholds = await self.bot.settings.alert_thresholds(entry.guild_id)
        sent = await self._sent(entry.kind, entry.entry_id)
        remaining = entry.deadline - now
        if remaining <= 0:
            if EXPIRED in sent:
                return
            await self._mark(entry.kind, entry.entry_id, [*thresholds, EXPIRED], now)
            await self._deliver(provider, entry, expired=True)
            self.bot.boards.request_refresh(entry.guild_id, entry.kind)
            return
        crossed = crossed_thresholds(thresholds, remaining, sent)
        if not crossed:
            return
        await self._mark(entry.kind, entry.entry_id, crossed, now)
        await self._deliver(provider, entry, expired=False)

    async def reset(self, kind: str, entry_id: str, guild_id: int, deadline: int, now: int | None = None) -> None:
        now = now if now is not None else timeutil.now()
        thresholds = await self.bot.settings.alert_thresholds(guild_id)
        remaining = deadline - now
        already = [t for t in thresholds if remaining <= t * 3600]
        async with self.bot.db.transaction() as tx:
            await tx.execute("DELETE FROM alert_state WHERE kind = ? AND entry_id = ?", (kind, entry_id))
            await tx.executemany(
                "INSERT INTO alert_state (kind, entry_id, threshold, sent_at) VALUES (?, ?, ?, ?)",
                [(kind, entry_id, float(t), now) for t in already],
            )
        await self.clear_messages(kind, entry_id)

    async def forget(self, kind: str, entry_id: str) -> None:
        await self.bot.db.execute("DELETE FROM alert_state WHERE kind = ? AND entry_id = ?", (kind, entry_id))
        await self.clear_messages(kind, entry_id)

    async def clear_messages(self, kind: str, entry_id: str) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT channel_id, message_id FROM alert_messages WHERE kind = ? AND entry_id = ?",
            (kind, entry_id),
        )
        if not rows:
            return
        await self.bot.db.execute("DELETE FROM alert_messages WHERE kind = ? AND entry_id = ?", (kind, entry_id))
        for row in rows:
            try:
                await self.bot.get_partial_messageable(row["channel_id"]).get_partial_message(row["message_id"]).delete()
            except discord.HTTPException:
                pass

    async def _remember(self, entry: TimedEntry, message: discord.Message) -> None:
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO alert_messages (kind, entry_id, channel_id, message_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (entry.kind, entry.entry_id, message.channel.id, message.id, timeutil.now()),
        )

    def _embed(self, provider: AlertProvider, entry: TimedEntry, expired: bool) -> discord.Embed:
        title = f"{provider.noun} expired" if expired else f"{provider.noun} reminder"
        embed = discord.Embed(
            title=title,
            description=provider.describe(entry, expired),
            colour=Colour.DARK if expired else self._urgency_colour(entry.deadline - timeutil.now()),
        )
        if entry.location:
            embed.add_field(name="Location", value=md(entry.location), inline=True)
        embed.set_footer(text=f"ID {entry.entry_id}")
        return embed

    @staticmethod
    def _urgency_colour(remaining: int) -> int:
        hours = remaining / 3600
        if hours <= 1:
            return Colour.CRITICAL
        if hours <= 2:
            return Colour.DANGER
        if hours <= 6:
            return Colour.ORANGE
        return Colour.WARNING

    async def _deliver(self, provider: AlertProvider, entry: TimedEntry, *, expired: bool) -> None:
        await self.clear_messages(entry.kind, entry.entry_id)
        embed = self._embed(provider, entry, expired)
        view = alert_view(entry.kind, entry.guild_id, entry.entry_id, provider.action_label)
        direct = await self._dm_targets(provider, entry)
        await self._send_dms(entry, embed, view, direct)
        if entry.private:
            return
        guild = self.bot.get_guild(entry.guild_id)
        if guild is None:
            return
        channel_id = await self.bot.settings.get(entry.guild_id, keys.ALERT_CHANNEL)
        if channel_id is None:
            channel_id = await self.bot.boards.channel_id_for(entry.guild_id, entry.kind)
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        if channel is None:
            log.info("No alert channel for %s in guild %s; skipping reminder", entry.kind, entry.guild_id)
            return
        role_ids: list[int] = []
        logistics = await self.bot.settings.get(entry.guild_id, keys.LOGISTICS_ROLE)
        if logistics:
            role_ids.append(int(logistics))
        for role_id in await provider.alert_roles(entry):
            if role_id not in role_ids:
                role_ids.append(role_id)
        roles = [role for role in (guild.get_role(rid) for rid in role_ids) if role is not None]
        content = " ".join(role.mention for role in roles) or None
        try:
            message = await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=roles),
            )
        except discord.HTTPException:
            log.warning("Could not post %s reminder in channel %s", entry.kind, channel_id, exc_info=True)
            return
        await self._remember(entry, message)

    async def _dm_targets(self, provider: AlertProvider, entry: TimedEntry) -> set[int]:
        targets = set(await provider.recipients(entry)) if entry.private else set()
        guild = self.bot.get_guild(entry.guild_id)
        if guild is None:
            return targets
        for user_id in await entries.subscribers(self.bot.db, entry.kind, entry.entry_id):
            if user_id in targets:
                continue
            member = guild.get_member(user_id)
            if member is not None and await provider.can_notify(entry, member):
                targets.add(user_id)
        return targets

    async def _send_dms(self, entry: TimedEntry, embed: discord.Embed, view: discord.ui.View, user_ids: set[int]) -> None:
        if not user_ids:
            return
        guild = self.bot.get_guild(entry.guild_id)
        dm_embed = embed.copy()
        if guild is not None:
            dm_embed.set_author(name=guild.name)
        for user_id in sorted(user_ids):
            user = self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    continue
            if user.bot:
                continue
            try:
                message = await user.send(embed=dm_embed, view=view)
            except discord.HTTPException:
                log.info("Could not DM user %s a %s reminder", user_id, entry.kind)
                continue
            await self._remember(entry, message)
