from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from foxbot.core import settings as keys
from foxbot.core.settings import SettingsStore
from foxbot.db import Database

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

GROUPS: dict[str, str] = {
    "stockpile": "Stockpiles",
    "ship": "Ships",
    "msupps": "Msupps",
    "orders": "Orders",
    "inventory": "Inventory",
    "rares": "Regiment rares",
    "tickets": "Ticket staff",
}


class PermissionService:
    def __init__(self, db: Database, settings: SettingsStore):
        self.db = db
        self.settings = settings

    async def roles_for(self, guild_id: int, group: str) -> set[int]:
        rows = await self.db.fetchall(
            "SELECT role_id FROM permission_roles WHERE guild_id = ? AND grp = ?",
            (guild_id, group),
        )
        return {row["role_id"] for row in rows}

    async def set_roles(self, guild_id: int, group: str, role_ids: set[int]) -> None:
        if group not in GROUPS:
            raise ValueError(f"Unknown permission group {group!r}")
        async with self.db.transaction() as tx:
            await tx.execute(
                "DELETE FROM permission_roles WHERE guild_id = ? AND grp = ?",
                (guild_id, group),
            )
            await tx.executemany(
                "INSERT INTO permission_roles (guild_id, grp, role_id) VALUES (?, ?, ?)",
                [(guild_id, group, role_id) for role_id in sorted(role_ids)],
            )

    async def is_admin(self, member: discord.Member) -> bool:
        if member.guild.owner_id == member.id:
            return True
        if member.guild_permissions.administrator:
            return True
        admin_role_id = await self.settings.get(member.guild.id, keys.ADMIN_ROLE)
        return admin_role_id is not None and any(role.id == admin_role_id for role in member.roles)

    async def has(self, member: discord.Member, group: str) -> bool:
        if await self.is_admin(member):
            return True
        allowed = await self.roles_for(member.guild.id, group)
        if member.guild.id in allowed:
            return True
        return any(role.id in allowed for role in member.roles)


async def resolve_member(bot: FoxBot, guild_id: int, user_id: int) -> discord.Member | None:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def interaction_member(interaction: discord.Interaction, guild_id: int | None = None) -> discord.Member | None:
    if isinstance(interaction.user, discord.Member) and (guild_id is None or interaction.user.guild.id == guild_id):
        return interaction.user
    target_guild = guild_id or interaction.guild_id
    if target_guild is None:
        return None
    return await resolve_member(interaction.client, target_guild, interaction.user.id)


async def check_group(interaction: discord.Interaction, group: str, guild_id: int | None = None) -> bool:
    from foxbot.core.ui import deny

    member = await interaction_member(interaction, guild_id)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return False
    if await interaction.client.perms.has(member, group):
        return True
    await deny(interaction, f"You need the **{GROUPS[group]}** permission for that. Ask an admin to grant it with `/permissions`.")
    return False


async def check_admin(interaction: discord.Interaction) -> bool:
    from foxbot.core.ui import deny

    member = await interaction_member(interaction)
    if member is None:
        await deny(interaction, "This only works inside the server.")
        return False
    if await interaction.client.perms.is_admin(member):
        return True
    await deny(interaction, "Only bot admins can do that (server owner, Administrator permission, or the admin role).")
    return False
