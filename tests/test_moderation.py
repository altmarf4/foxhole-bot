import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from fakes import add_role, find_button, make_bot, make_channel, make_guild, make_interaction, make_member


def ranked_role(guild, name, position, *, managed=False):
    role = add_role(guild, name)
    role.position = position
    role.managed = managed
    role.__ge__ = lambda self, other: self.position >= other.position
    return role


def fake_message(*, author_id=1, pinned=False, age_days=0.0):
    message = MagicMock(spec=discord.Message)
    message.author = SimpleNamespace(id=author_id)
    message.pinned = pinned
    message.created_at = discord.utils.utcnow() - dt.timedelta(days=age_days)
    return message


def forbidden():
    return discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Permissions")


@pytest.fixture
async def world():
    bot = await make_bot(["foxbot.features.moderation"])
    moderation = bot.extensions["foxbot.features.moderation"]
    guild = make_guild(owner_id=1)
    guild.everyone_position = 0
    guild.default_role.position = 0
    guild.default_role.managed = False
    me = make_member(guild, name="FoxBot", bot=True)
    me.top_role = ranked_role(guild, "FoxBot", 50)
    me.guild_permissions = discord.Permissions(manage_roles=True)
    guild.me = me
    mod_role = ranked_role(guild, "Moderator", 40)
    mod = make_member(guild, name="Mod", roles=[mod_role])
    mod.top_role = mod_role
    owner = make_member(guild, user_id=1, name="Owner")
    owner.top_role = guild.default_role
    target_role = ranked_role(guild, "Logi", 10)
    channel = make_channel(guild, "general")
    yield SimpleNamespace(bot=bot, mod_module=moderation, guild=guild, me=me, mod=mod, owner=owner, role=target_role, channel=channel)
    await bot.db.close()


def make_members(guild, count, *, role=None, bots=0):
    members = []
    for index in range(count):
        member = make_member(guild, name=f"Member {index}", roles=[role] if role else [])
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        members.append(member)
    for index in range(bots):
        member = make_member(guild, name=f"Bot {index}", bot=True)
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        members.append(member)
    return members


def test_purge_check_counts_filters_and_keeps_pinned(world):
    check = world.mod_module.purge_check(2, None)
    pinned = fake_message(pinned=True)
    assert not check(pinned)
    assert check(fake_message()) and check(fake_message())
    assert not check(fake_message())
    by_user = world.mod_module.purge_check(5, 42)
    assert not by_user(fake_message(author_id=7))
    assert by_user(fake_message(author_id=42))
    assert not by_user(fake_message(author_id=42, pinned=True))


async def test_purge_deletes_and_reports(world):
    w = world
    deleted = [fake_message() for _ in range(3)] + [fake_message(age_days=20)]
    w.channel.purge = AsyncMock(return_value=deleted)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.purge.callback(cog, interaction, 10, None)
    assert interaction.recorder.calls[0] == ("defer", {"ephemeral": True, "thinking": True})
    kind, payload = interaction.recorder.last
    assert kind == "followup" and payload["ephemeral"]
    assert "Deleted 4 messages." in payload["content"]
    assert "1 message was older than 14 days" in payload["content"]
    kwargs = w.channel.purge.await_args.kwargs
    assert kwargs["limit"] == 10
    assert callable(kwargs["check"])


async def test_purge_by_user_scans_further(world):
    w = world
    user = SimpleNamespace(id=42, mention="<@42>")
    w.channel.purge = AsyncMock(return_value=[fake_message(author_id=42)])
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.purge.callback(cog, interaction, 5, user)
    kwargs = w.channel.purge.await_args.kwargs
    assert kwargs["limit"] == w.mod_module.USER_SCAN_LIMIT
    check = kwargs["check"]
    assert not check(fake_message(author_id=1))
    assert check(fake_message(author_id=42))
    payload = interaction.recorder.last[1]
    assert "Deleted 1 message from <@42>." in payload["content"]


async def test_purge_reports_forbidden(world):
    w = world
    w.channel.purge = AsyncMock(side_effect=forbidden())
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.purge.callback(cog, interaction, 3, None)
    assert "do not have permission" in interaction.recorder.last[1]["content"]


async def test_purge_permission_checks(world):
    w = world
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    interaction.permissions = discord.Permissions.none()
    interaction.app_permissions = discord.Permissions.all()
    with pytest.raises(app_commands.MissingPermissions):
        await cog.purge._check_can_run(interaction)
    interaction.permissions = discord.Permissions(manage_messages=True)
    interaction.app_permissions = discord.Permissions.none()
    with pytest.raises(app_commands.BotMissingPermissions):
        await cog.purge._check_can_run(interaction)
    interaction.app_permissions = discord.Permissions(manage_messages=True, read_message_history=True)
    assert await cog.purge._check_can_run(interaction)
    payload = cog.purge.to_dict(w.bot.tree)
    assert payload["default_member_permissions"] == discord.Permissions(manage_messages=True).value
    amount = next(option for option in payload["options"] if option["name"] == "amount")
    assert (amount["min_value"], amount["max_value"]) == (1, 500)


async def test_role_commands_are_guarded_by_manage_roles(world):
    w = world
    cog = w.bot.get_cog("Moderation")
    payload = cog.role_group.to_dict(w.bot.tree)
    assert payload["default_member_permissions"] == discord.Permissions(manage_roles=True).value
    assert payload["dm_permission"] is False
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    interaction.permissions = discord.Permissions.none()
    interaction.app_permissions = discord.Permissions.all()
    with pytest.raises(app_commands.MissingPermissions):
        await cog.role_add._check_can_run(interaction)


async def test_role_add_single_member(world):
    w = world
    (target,) = make_members(w.guild, 1)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, target)
    target.add_roles.assert_awaited_once()
    assert target.add_roles.await_args.args == (w.role,)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert payload["content"] == f"Added {w.role.mention} to {target.mention}."


async def test_role_single_member_already_has_or_lacks(world):
    w = world
    (holder,) = make_members(w.guild, 1, role=w.role)
    (other,) = make_members(w.guild, 1)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, holder)
    assert "already has" in interaction.recorder.last[1]["content"]
    holder.add_roles.assert_not_awaited()
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_remove.callback(cog, interaction, w.role, other)
    assert "does not have" in interaction.recorder.last[1]["content"]
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_remove.callback(cog, interaction, w.role, holder)
    holder.remove_roles.assert_awaited_once()
    assert interaction.recorder.last[1]["content"].startswith("Removed")


async def test_role_single_member_forbidden_is_reported(world):
    w = world
    (target,) = make_members(w.guild, 1)
    target.add_roles = AsyncMock(side_effect=forbidden())
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, target)
    assert "Discord refused" in interaction.recorder.last[1]["content"]


@pytest.mark.parametrize("case", ["everyone", "managed", "above_bot", "above_invoker", "equal_invoker", "no_bot_permission"])
async def test_role_refusals(world, case):
    w = world
    (target,) = make_members(w.guild, 1)
    role = w.role
    if case == "everyone":
        role = w.guild.default_role
    elif case == "managed":
        role = ranked_role(w.guild, "Booster", 5, managed=True)
    elif case == "above_bot":
        role = ranked_role(w.guild, "Leader", 60)
    elif case == "above_invoker":
        role = ranked_role(w.guild, "Officer", 45)
    elif case == "equal_invoker":
        role = w.mod.top_role
    elif case == "no_bot_permission":
        w.me.guild_permissions = discord.Permissions.none()
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, role, target)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    expected = {
        "everyone": "@everyone role cannot",
        "managed": "managed by an integration",
        "above_bot": "above my highest role",
        "above_invoker": "above your highest role",
        "equal_invoker": "above your highest role",
        "no_bot_permission": "I need the Manage Roles permission",
    }[case]
    assert expected in payload["content"]
    target.add_roles.assert_not_awaited()


async def test_owner_bypasses_invoker_hierarchy(world):
    w = world
    (target,) = make_members(w.guild, 1)
    role = ranked_role(w.guild, "Officer", 45)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.owner, channel=w.channel)
    await cog.role_add.callback(cog, interaction, role, target)
    target.add_roles.assert_awaited_once()


async def test_role_add_everyone_with_confirmation_and_progress(world):
    w = world
    fresh = make_members(w.guild, 30, bots=2)
    already = make_members(w.guild, 3, role=w.role)
    fresh[4].add_roles = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500, reason="Error"), "boom"))
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, None)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    targets = [m for m in w.guild.members if not m.bot and w.role not in m.roles]
    assert len(targets) == 32
    assert payload["content"].startswith(f"Add {w.role.mention} to 32 members?")
    for member in fresh + already:
        member.add_roles.assert_not_awaited()
    w.mod.add_roles = AsyncMock()
    w.owner.add_roles = AsyncMock()
    confirm = make_interaction(w.bot, w.mod, channel=w.channel)
    await find_button(payload["view"], "Add Role").callback(confirm)
    for member in [m for m in fresh if not m.bot] + [w.mod, w.owner]:
        member.add_roles.assert_awaited_once()
    for member in already + [m for m in fresh if m.bot]:
        member.add_roles.assert_not_awaited()
    kinds = confirm.recorder.kinds()
    assert kinds == ["edit", "edit_original", "edit_original"]
    assert confirm.recorder.calls[0][1]["view"] is None
    assert "0/32 done" in confirm.recorder.calls[0][1]["content"]
    assert "25/32 done" in confirm.recorder.calls[1][1]["content"]
    summary = confirm.recorder.last[1]["content"]
    skipped = len(w.guild.members) - 32
    assert skipped == 6
    assert "to **31 members**" in summary
    assert f"Skipped {skipped} (bots or already had it)" in summary
    assert "Failed for 1 member" in summary


async def test_role_remove_everyone(world):
    w = world
    holders = make_members(w.guild, 4, role=w.role)
    make_members(w.guild, 2)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_remove.callback(cog, interaction, w.role, None)
    payload = interaction.recorder.last[1]
    assert payload["content"].startswith(f"Remove {w.role.mention} from 4 members?")
    confirm = make_interaction(w.bot, w.mod, channel=w.channel)
    await find_button(payload["view"], "Remove Role").callback(confirm)
    for member in holders:
        member.remove_roles.assert_awaited_once()
    assert confirm.recorder.kinds() == ["edit", "edit_original"]
    assert "from **4 members**" in confirm.recorder.last[1]["content"]


async def test_role_everyone_nothing_to_do(world):
    w = world
    make_members(w.guild, 2, role=w.role)
    w.mod.roles.append(w.role)
    w.owner.roles.append(w.role)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, None)
    payload = interaction.recorder.last[1]
    assert "Nothing to do" in payload["content"]
    assert "view" not in payload


async def test_role_everyone_confirm_rechecks_permissions(world):
    w = world
    members = make_members(w.guild, 3)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, None)
    view = interaction.recorder.last[1]["view"]
    confirm = make_interaction(w.bot, w.mod, channel=w.channel)
    confirm.permissions = discord.Permissions.none()
    await find_button(view, "Add Role").callback(confirm)
    assert "no longer have the Manage Roles permission" in confirm.recorder.last[1]["content"]
    for member in members:
        member.add_roles.assert_not_awaited()


async def test_role_everyone_confirm_rechecks_hierarchy(world):
    w = world
    members = make_members(w.guild, 3)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, None)
    view = interaction.recorder.last[1]["view"]
    w.role.position = 70
    confirm = make_interaction(w.bot, w.mod, channel=w.channel)
    await find_button(view, "Add Role").callback(confirm)
    kind, payload = confirm.recorder.last
    assert kind == "edit" and "above my highest role" in payload["content"]
    for member in members:
        member.add_roles.assert_not_awaited()


async def test_role_everyone_cancel(world):
    w = world
    members = make_members(w.guild, 2)
    cog = w.bot.get_cog("Moderation")
    interaction = make_interaction(w.bot, w.mod, channel=w.channel)
    await cog.role_add.callback(cog, interaction, w.role, None)
    view = interaction.recorder.last[1]["view"]
    cancel = make_interaction(w.bot, w.mod, channel=w.channel)
    await find_button(view, "Cancel").callback(cancel)
    assert cancel.recorder.last[1]["content"] == "Cancelled."
    for member in members:
        member.add_roles.assert_not_awaited()
