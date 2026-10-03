from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.core.text import plural
from foxbot.core.ui import ConfirmView, deny, edit, reply

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

PURGE_MAX = 500
USER_SCAN_LIMIT = 2000
BULK_DELETE_AGE = dt.timedelta(days=14)
PROGRESS_EVERY = 25


def purge_check(amount: int, user_id: int | None) -> Callable[[discord.Message], bool]:
    matched = 0

    def check(message: discord.Message) -> bool:
        nonlocal matched
        if message.pinned:
            return False
        if user_id is not None and message.author.id != user_id:
            return False
        if matched >= amount:
            return False
        matched += 1
        return True

    return check


def older_than_bulk_limit(messages: list[discord.Message], now: dt.datetime | None = None) -> int:
    cutoff = (now or discord.utils.utcnow()) - BULK_DELETE_AGE
    return sum(1 for message in messages if message.created_at < cutoff)


def purge_summary(deleted: list[discord.Message], user: discord.abc.User | None) -> str:
    who = f" from {user.mention}" if user is not None else ""
    text = f"Deleted {plural(len(deleted), 'message')}{who}. Pinned messages were kept."
    old = older_than_bulk_limit(deleted)
    if old:
        text += (
            f"\n{plural(old, 'message was', 'messages were')} older than 14 days. Discord cannot bulk delete those, "
            "so they were deleted one by one, which is slow."
        )
    if user is not None:
        text += f"\nOnly the last {USER_SCAN_LIMIT} messages in this channel were searched."
    return text


def can_manage_roles(interaction: discord.Interaction) -> bool:
    permissions = interaction.permissions
    return bool(permissions.manage_roles or permissions.administrator)


def role_problem(role: discord.Role, guild: discord.Guild, invoker: discord.abc.User) -> str | None:
    if role.id == guild.id:
        return "The @everyone role cannot be added or removed."
    if role.managed:
        return f"{role.mention} is managed by an integration (a bot or a server subscription) and cannot be assigned by hand."
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return "I need the Manage Roles permission for that."
    if role >= me.top_role:
        return f"{role.mention} is at or above my highest role. Move my role above it in Server Settings > Roles."
    if guild.owner_id != invoker.id:
        top_role = getattr(invoker, "top_role", None)
        if top_role is None or role >= top_role:
            return f"{role.mention} is at or above your highest role, so you cannot manage it."
    return None


def needs_change(member: discord.Member, role: discord.Role, adding: bool) -> bool:
    if member.bot:
        return False
    has_role = role in member.roles
    return not has_role if adding else has_role


async def change_role(member: discord.Member, role: discord.Role, adding: bool, reason: str) -> None:
    if adding:
        await member.add_roles(role, reason=reason)
    else:
        await member.remove_roles(role, reason=reason)


def progress_text(role: discord.Role, adding: bool, done: int, total: int) -> str:
    verb = "Adding" if adding else "Removing"
    direction = "to" if adding else "from"
    return f"{verb} {role.mention} {direction} {plural(total, 'member')}: {done}/{total} done..."


def bulk_summary(role: discord.Role, adding: bool, changed: int, skipped: int, failed: int) -> str:
    verb = "Added" if adding else "Removed"
    direction = "to" if adding else "from"
    reason = "bots or already had it" if adding else "bots or did not have it"
    text = f"{verb} {role.mention} {direction} **{plural(changed, 'member')}**. Skipped {skipped} ({reason})."
    if failed:
        text += f" Failed for {plural(failed, 'member')}; Discord refused the change."
    return text


async def ensure_members(guild: discord.Guild) -> None:
    if guild.chunked:
        return
    try:
        await guild.chunk()
    except discord.HTTPException:
        log.warning("Could not load the member list of guild %s", guild.id, exc_info=True)


async def bulk_change(interaction: discord.Interaction, role: discord.Role, adding: bool) -> tuple[int, int, int]:
    guild = interaction.guild
    members = list(guild.members)
    targets = [member for member in members if needs_change(member, role, adding)]
    skipped = len(members) - len(targets)
    changed = failed = 0
    reason = f"Bulk role {'add' if adding else 'remove'} by {interaction.user} ({interaction.user.id})"
    await edit(interaction, content=progress_text(role, adding, 0, len(targets)), view=None)
    for index, member in enumerate(targets, start=1):
        if not needs_change(member, role, adding):
            skipped += 1
        else:
            try:
                await change_role(member, role, adding, reason)
            except discord.HTTPException:
                failed += 1
            else:
                changed += 1
        if index % PROGRESS_EVERY == 0 and index < len(targets):
            try:
                await edit(interaction, content=progress_text(role, adding, index, len(targets)))
            except discord.HTTPException:
                log.debug("Could not update role progress", exc_info=True)
    try:
        await edit(interaction, content=bulk_summary(role, adding, changed, skipped, failed), view=None)
    except discord.HTTPException:
        log.info("Could not report the bulk role result in guild %s", guild.id)
    return changed, skipped, failed


async def change_single(interaction: discord.Interaction, role: discord.Role, member: discord.Member, adding: bool) -> None:
    if adding and role in member.roles:
        await reply(interaction, f"{member.mention} already has {role.mention}.")
        return
    if not adding and role not in member.roles:
        await reply(interaction, f"{member.mention} does not have {role.mention}.")
        return
    reason = f"Role {'add' if adding else 'remove'} by {interaction.user} ({interaction.user.id})"
    try:
        await change_role(member, role, adding, reason)
    except discord.Forbidden:
        await reply(interaction, f"Discord refused to change {role.mention} for {member.mention}. Check my role position.")
        return
    except discord.HTTPException as error:
        await reply(interaction, f"Could not change {role.mention} for {member.mention}: {error.text or error}")
        return
    if adding:
        await reply(interaction, f"Added {role.mention} to {member.mention}.")
    else:
        await reply(interaction, f"Removed {role.mention} from {member.mention}.")


async def start_role_change(interaction: discord.Interaction, role: discord.Role, member: discord.Member | None, adding: bool) -> None:
    guild = interaction.guild
    problem = role_problem(role, guild, interaction.user)
    if problem:
        await deny(interaction, problem)
        return
    if member is not None:
        await change_single(interaction, role, member, adding)
        return
    await ensure_members(guild)
    count = sum(1 for candidate in guild.members if needs_change(candidate, role, adding))
    if count == 0:
        if adding:
            await reply(interaction, f"Nothing to do: every member already has {role.mention}. Bots are skipped.")
        else:
            await reply(interaction, f"Nothing to do: no member has {role.mention}. Bots are skipped.")
        return

    async def confirmed(confirm_interaction: discord.Interaction) -> None:
        if not can_manage_roles(confirm_interaction):
            await deny(confirm_interaction, "You no longer have the Manage Roles permission.")
            return
        current = guild.get_role(role.id) or role
        again = role_problem(current, guild, confirm_interaction.user)
        if again:
            await edit(confirm_interaction, content=again, view=None)
            return
        await bulk_change(confirm_interaction, current, adding)

    if adding:
        prompt = f"Add {role.mention} to {plural(count, 'member')}? Bots and members who already have it are skipped."
    else:
        prompt = f"Remove {role.mention} from {plural(count, 'member')}? Bots and members without it are skipped."
    view = ConfirmView(
        owner_id=interaction.user.id,
        on_confirm=confirmed,
        confirm_label="Add Role" if adding else "Remove Role",
        danger=not adding,
    )
    await reply(interaction, prompt, view=view)


class Moderation(commands.Cog):
    role_group = app_commands.Group(
        name="role",
        description="Add or remove a role for one member or for everyone.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    def __init__(self, bot: FoxBot):
        self.bot = bot

    @app_commands.command(name="purge", description="Delete recent messages in this channel (pinned messages are kept).")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @app_commands.describe(amount=f"How many messages to delete (1-{PURGE_MAX})", user="Only delete messages from this user")
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, PURGE_MAX],
        user: discord.User | None = None,
    ) -> None:
        channel = interaction.channel
        if channel is None or not hasattr(channel, "purge"):
            await deny(interaction, "Messages cannot be purged in this kind of channel.")
            return
        amount = max(1, min(PURGE_MAX, int(amount)))
        await interaction.response.defer(ephemeral=True, thinking=True)
        limit = USER_SCAN_LIMIT if user is not None else amount
        try:
            deleted = await channel.purge(
                limit=limit,
                check=purge_check(amount, user.id if user is not None else None),
                reason=f"Purge by {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await reply(interaction, "I do not have permission to delete messages here. I need Manage Messages and Read Message History.")
            return
        except discord.HTTPException as error:
            await reply(interaction, f"Deleting messages failed: {error.text or error}")
            return
        await reply(interaction, purge_summary(deleted, user))

    @role_group.command(name="add", description="Add a role to one member, or to everyone if no member is given.")
    @app_commands.describe(role="The role to add", member="Only this member (leave empty for everyone)")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def role_add(self, interaction: discord.Interaction, role: discord.Role, member: discord.Member | None = None) -> None:
        await start_role_change(interaction, role, member, adding=True)

    @role_group.command(name="remove", description="Remove a role from one member, or from everyone if no member is given.")
    @app_commands.describe(role="The role to remove", member="Only this member (leave empty for everyone)")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def role_remove(self, interaction: discord.Interaction, role: discord.Role, member: discord.Member | None = None) -> None:
        await start_role_change(interaction, role, member, adding=False)


async def setup(bot: FoxBot) -> None:
    await bot.add_cog(Moderation(bot))
