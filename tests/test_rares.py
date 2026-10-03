import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from fakes import add_role, find_button, make_bot, make_channel, make_guild, make_interaction, make_member, next_id, set_label_value
from foxbot.core import settings as keys
from foxbot.core.text import EMBED_TOTAL_LIMIT


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"})


def make_voice(guild, name="Rares"):
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = next_id()
    channel.name = name
    channel.guild = guild
    channel.renames = []

    async def edit(**kwargs):
        channel.renames.append((time.monotonic(), kwargs["name"]))
        channel.name = kwargs["name"]

    channel.edit = AsyncMock(side_effect=edit)
    guild._channels[channel.id] = channel
    return channel


@pytest.fixture
async def world():
    bot = await make_bot(["foxbot.features.rares"])
    rares = bot.extensions["foxbot.features.rares"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    role = add_role(guild, "Quartermaster")
    await bot.perms.set_roles(guild.id, "rares", {role.id})
    keeper = make_member(guild, name="Keeper", roles=[role])
    outsider = make_member(guild, name="Outsider")
    admin = make_member(guild, name="Admin", administrator=True)
    channel = make_channel(guild, "rares")
    cog = bot.get_cog("Rares")
    yield SimpleNamespace(bot=bot, rares=rares, guild=guild, keeper=keeper, outsider=outsider, admin=admin, channel=channel, cog=cog)
    await cog.cog_unload()
    await bot.db.close()


async def change(world, action, amount, **kwargs):
    return await world.rares.apply_change(world.bot, world.guild.id, action=action, amount=amount, user_id=world.keeper.id, **kwargs)


async def test_apply_change_actions_and_log(world):
    assert await change(world, "add", 100) == (0, 100)
    assert await change(world, "remove", 30) == (100, 70)
    assert await change(world, "remove", 500) == (70, 0)
    assert await change(world, "set", 250) == (0, 250)
    assert await change(world, "payment", -50, ticket_number=4) == (250, 200)
    assert await change(world, "payment", 120, ticket_number=4) == (200, 320)
    assert await change(world, "payment", -1000) == (320, 0)
    assert await change(world, "add", 5) == (0, 5)
    assert await change(world, "reset", 999) == (5, 0)
    assert await world.rares.get_count(world.bot, world.guild.id) == 0
    rows = await world.rares.log_rows(world.bot, world.guild.id)
    assert len(rows) == 9
    assert rows[0]["action"] == "reset" and rows[0]["amount"] == 0
    payment = [row for row in rows if row["action"] == "payment" and row["ticket_number"] == 4]
    assert len(payment) == 2
    world.bot.boards.request_refresh.assert_called_with(world.guild.id, "rares")


async def test_apply_change_rejects_bad_input(world):
    with pytest.raises(ValueError):
        await change(world, "steal", 5)
    with pytest.raises(ValueError):
        await change(world, "add", -5)
    with pytest.raises(ValueError):
        await change(world, "set", -1)
    assert await world.rares.log_rows(world.bot, world.guild.id) == []


async def test_status_labels(world):
    rares = world.rares
    assert rares.status_for(0)[0] == "Empty"
    assert rares.status_for(1)[0] == "Low"
    assert rares.status_for(50)[0] == "Low"
    assert rares.status_for(51)[0] == "Moderate"
    assert rares.status_for(200)[0] == "Moderate"
    assert rares.status_for(201)[0] == "Stocked"


async def test_board_render_shows_count_status_and_last_ten(world):
    for index in range(15):
        await change(world, "add", index + 1, note=f"note {index}")
    render = await world.bot.boards.providers["rares"].render(world.guild)
    assert {button.action for button in render.buttons} == {"add", "remove", "set", "log"}
    assert [button.label for button in render.buttons] == ["Add", "Remove", "Set", "Log"]
    assert not render.options
    embed = render.embeds[0]
    assert "120 Rare Alloys" in embed.description
    assert "Status: **Moderate**" in embed.description
    assert embed.description.count("added **") == 10
    assert "note 14" in embed.description and "note 4" not in embed.description
    assert len(embed) <= EMBED_TOTAL_LIMIT
    assert "RA " not in embed.description


async def test_board_render_with_huge_notes_stays_in_limits(world):
    for _ in range(12):
        await change(world, "add", 1, note="x" * 200)
    render = await world.bot.boards.providers["rares"].render(world.guild)
    assert len(render.embeds[0].description) <= 4096
    assert len(render.embeds[0]) <= EMBED_TOTAL_LIMIT


async def test_board_add_flow(world):
    board = world.bot.boards.providers["rares"]
    click = make_interaction(world.bot, world.keeper, channel=world.channel)
    await board.on_button(click, "add")
    modal = click.response.modal
    assert isinstance(modal, world.rares.ChangeModal)
    assert len(modal.children) <= 5
    assert modal.title == "Add Rare Alloys"
    set_label_value(modal.amount_field, "1,250")
    set_label_value(modal.note_field, "From KM")
    submit = make_interaction(world.bot, world.keeper, channel=world.channel)
    await modal.on_submit(submit)
    assert submit.recorder.kinds() == ["defer", "followup"]
    payload = submit.recorder.last[1]
    assert payload["ephemeral"] and "0 -> 1250" in payload["content"]
    assert await world.rares.get_count(world.bot, world.guild.id) == 1250
    row = (await world.rares.log_rows(world.bot, world.guild.id))[0]
    assert (row["action"], row["amount"], row["note"], row["user_id"]) == ("add", 1250, "From KM", world.keeper.id)


async def test_board_remove_and_set_flows(world):
    await change(world, "add", 40)
    board = world.bot.boards.providers["rares"]
    click = make_interaction(world.bot, world.keeper, channel=world.channel)
    await board.on_button(click, "remove")
    modal = click.response.modal
    assert modal.title == "Remove Rare Alloys"
    set_label_value(modal.amount_field, "100")
    set_label_value(modal.note_field, "")
    submit = make_interaction(world.bot, world.keeper, channel=world.channel)
    await modal.on_submit(submit)
    content = submit.recorder.last[1]["content"]
    assert "40 -> 0" in content and "Only 40 were in stock" in content

    click = make_interaction(world.bot, world.keeper, channel=world.channel)
    await board.on_button(click, "set")
    modal = click.response.modal
    assert modal.title == "Set Rare Alloys Count"
    set_label_value(modal.amount_field, "0")
    set_label_value(modal.note_field, "War reset")
    submit = make_interaction(world.bot, world.keeper, channel=world.channel)
    await modal.on_submit(submit)
    assert "0 -> 0" in submit.recorder.last[1]["content"]
    set_label_value(modal.amount_field, "+75")
    submit = make_interaction(world.bot, world.keeper, channel=world.channel)
    await modal.on_submit(submit)
    assert await world.rares.get_count(world.bot, world.guild.id) == 75


@pytest.mark.parametrize("raw, message", [("abc", "whole number"), ("-5", "negative"), ("0", "more than zero"), ("12.5", "whole number")])
async def test_invalid_amounts_are_rejected(world, raw, message):
    modal = world.rares.ChangeModal(world.bot, world.guild.id, "add")
    set_label_value(modal.amount_field, raw)
    set_label_value(modal.note_field, "")
    submit = make_interaction(world.bot, world.keeper, channel=world.channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "send" and payload["ephemeral"] and message in payload["content"]
    assert await world.rares.get_count(world.bot, world.guild.id) == 0


async def test_permissions_checked_on_button_and_submit(world):
    board = world.bot.boards.providers["rares"]
    for action in ("add", "remove", "set"):
        click = make_interaction(world.bot, world.outsider, channel=world.channel)
        await board.on_button(click, action)
        kind, payload = click.recorder.last
        assert kind == "send" and payload["ephemeral"] and "permission" in payload["content"]
        assert click.response.modal is None
    modal = world.rares.ChangeModal(world.bot, world.guild.id, "add")
    set_label_value(modal.amount_field, "10")
    set_label_value(modal.note_field, "")
    submit = make_interaction(world.bot, world.outsider, channel=world.channel)
    await modal.on_submit(submit)
    assert "permission" in submit.recorder.last[1]["content"]
    assert await world.rares.get_count(world.bot, world.guild.id) == 0
    admin_click = make_interaction(world.bot, world.admin, channel=world.channel)
    await board.on_button(admin_click, "add")
    assert isinstance(admin_click.response.modal, world.rares.ChangeModal)


async def test_log_button_and_command_are_paginated(world):
    for index in range(80):
        await change(world, "add", 1, note=f"batch {index} " + "y" * 60)
    click = make_interaction(world.bot, world.outsider, channel=world.channel)
    await world.bot.boards.providers["rares"].on_button(click, "log")
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"]
    paginator = payload["view"]
    assert len(paginator.embeds) > 1
    assert all(len(embed) <= EMBED_TOTAL_LIMIT for embed in paginator.embeds)
    assert "batch 79" in paginator.embeds[0].description
    next_click = make_interaction(world.bot, world.outsider, channel=world.channel)
    await find_button(paginator, "Next").callback(next_click)
    assert next_click.recorder.last[0] == "edit"

    cog = world.cog
    command = make_interaction(world.bot, world.outsider, channel=world.channel)
    await cog.log_command.callback(cog, command)
    assert command.recorder.last[1]["ephemeral"]


async def test_empty_log(world):
    click = make_interaction(world.bot, world.keeper, channel=world.channel)
    await world.bot.boards.providers["rares"].on_button(click, "log")
    assert "No changes recorded yet." in click.recorder.last[1]["embed"].description


async def test_log_channel_post(world):
    log_channel = make_channel(world.guild, "rares-log")
    await world.bot.settings.set(world.guild.id, keys.RARES_LOG_CHANNEL, log_channel.id)
    await change(world, "add", 100)
    await change(world, "payment", 200, note="Ticket 0007 - KM", ticket_number=7)
    assert len(log_channel.sent) == 2
    embed = log_channel.sent[1]["embed"]
    assert f"<@{world.keeper.id}>" in embed.description
    assert "100 -> 300" in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Ticket"] == "0007"
    assert fields["Note"] == "Ticket 0007 - KM"
    assert log_channel.sent[1]["allowed_mentions"].users is False


async def test_log_channel_failures_do_not_break_changes(world):
    log_channel = make_channel(world.guild, "rares-log")
    log_channel.send = AsyncMock(side_effect=forbidden())
    await world.bot.settings.set(world.guild.id, keys.RARES_LOG_CHANNEL, log_channel.id)
    assert await change(world, "add", 10) == (0, 10)
    await world.bot.settings.set(world.guild.id, keys.RARES_LOG_CHANNEL, 999999)
    assert await change(world, "add", 10) == (10, 20)


async def test_voice_rename_is_debounced_and_ends_on_latest(world):
    voice = make_voice(world.guild)
    await world.bot.settings.set(world.guild.id, keys.RARES_VOICE_CHANNEL, voice.id)
    world.cog.voice_interval = 0.3
    await change(world, "add", 1)
    await asyncio.sleep(0.05)
    assert [name for _, name in voice.renames] == ["Rare Alloys: 1"]
    await change(world, "add", 2)
    await change(world, "add", 3)
    await asyncio.sleep(0.05)
    assert len(voice.renames) == 1
    await change(world, "remove", 1)
    await world.cog.wait_voice()
    assert [name for _, name in voice.renames] == ["Rare Alloys: 1", "Rare Alloys: 5"]
    assert voice.renames[1][0] - voice.renames[0][0] >= 0.25


async def test_voice_skips_unchanged_name_and_survives_errors(world):
    voice = make_voice(world.guild, name="Rare Alloys: 10")
    await world.bot.settings.set(world.guild.id, keys.RARES_VOICE_CHANNEL, voice.id)
    world.cog.voice_interval = 0.01
    await change(world, "add", 10)
    await world.cog.wait_voice()
    assert voice.edit.await_count == 0
    voice.edit = AsyncMock(side_effect=forbidden())
    await change(world, "add", 1)
    await world.cog.wait_voice()
    assert voice.edit.await_count == 1
    assert not world.cog._voice_tasks


async def test_no_voice_task_without_setting(world):
    await change(world, "add", 1)
    assert not world.cog._voice_tasks


async def test_sync_voice_helper(world):
    voice = make_voice(world.guild)
    await change(world, "add", 42)
    await world.bot.settings.set(world.guild.id, keys.RARES_VOICE_CHANNEL, voice.id)
    await world.rares.sync_voice(world.bot, world.guild.id)
    await world.cog.wait_voice()
    assert voice.name == "Rare Alloys: 42"


async def test_count_command_is_public(world):
    await change(world, "add", 30)
    interaction = make_interaction(world.bot, world.outsider, channel=world.channel)
    await world.cog.count.callback(world.cog, interaction)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"] is False
    fields = {field.name: field.value for field in payload["embed"].fields}
    assert fields == {"Count": "**30** Rare Alloys", "Status": "Low"}


async def test_board_command_requires_admin(world):
    interaction = make_interaction(world.bot, world.keeper, channel=world.channel)
    await world.cog.board.callback(world.cog, interaction)
    assert "admins" in interaction.recorder.last[1]["content"]
    world.bot.boards.post = AsyncMock()
    interaction = make_interaction(world.bot, world.admin, channel=world.channel)
    await world.cog.board.callback(world.cog, interaction)
    world.bot.boards.post.assert_awaited_once_with(world.guild, "rares", world.channel)
    assert "posted" in interaction.recorder.last[1]["content"]


async def test_board_post_uses_framework_view(world):
    from foxbot.core.boards import build_board_view

    render = await world.bot.boards.providers["rares"].render(world.guild)
    view = build_board_view("rares", render)
    ids = {item.custom_id for item in view.children}
    assert ids == {"fb:rares:btn:add", "fb:rares:btn:remove", "fb:rares:btn:set", "fb:rares:btn:log"}


async def test_unknown_board_button_and_pick(world):
    board = world.bot.boards.providers["rares"]
    click = make_interaction(world.bot, world.keeper, channel=world.channel)
    await board.on_button(click, "explode")
    assert "no longer supported" in click.recorder.last[1]["content"]
    pick = make_interaction(world.bot, world.keeper, channel=world.channel)
    await board.on_pick(pick, "x")
    assert pick.recorder.last[1]["ephemeral"]
