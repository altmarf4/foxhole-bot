from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from fakes import (
    add_role,
    find_button,
    find_select,
    make_bot,
    make_channel,
    make_guild,
    make_interaction,
    make_member,
    select_values,
    set_label_value,
)
from foxbot.core import entries, timeutil
from foxbot.core.locpicker import LocationPicker

stockpiles = None
StockpileModal = None
StockpilePanel = None


@pytest.fixture
async def world():
    global stockpiles, StockpileModal, StockpilePanel
    bot = await make_bot(["foxbot.features.stockpiles"])
    stockpiles = bot.extensions["foxbot.features.stockpiles"]
    StockpileModal = stockpiles.StockpileModal
    StockpilePanel = stockpiles.StockpilePanel
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    logi = add_role(guild, "Logi")
    await bot.perms.set_roles(guild.id, "stockpile", {logi.id})
    member = make_member(guild, name="Logi Guy", roles=[logi])
    outsider = make_member(guild, name="Outsider")
    channel = make_channel(guild, "logi")
    yield bot, guild, member, outsider, channel
    await bot.db.close()


async def create(bot, guild, member, **overrides):
    values = dict(
        name="KM1",
        hex_name="Piper's Enclave",
        region="Pan's Villa",
        store_type="Storage Depot",
        priority="Medium",
        access_code="861781",
        private=False,
        user_id=member.id,
    )
    values.update(overrides)
    return await stockpiles.create(bot, guild.id, **values)


async def test_board_add_flow_uses_picker_then_modal(world):
    bot, guild, member, _, channel = world
    board = bot.boards.providers["stockpile"]
    interaction = make_interaction(bot, member, channel=channel)
    await board.on_button(interaction, "add")
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    picker = payload["view"]
    assert isinstance(picker, LocationPicker)
    assert "Step 1" in payload["content"]

    hex_select = find_select(picker, 0)
    select_values(hex_select, ["Piper's Enclave"])
    step2 = make_interaction(bot, member, channel=channel)
    await hex_select.callback(step2)
    assert step2.recorder.last[0] == "edit"
    town_select = find_select(picker)
    assert any(o.value == "Syrinx Pass" for o in town_select.options)
    assert len(town_select.options) <= 25

    select_values(town_select, ["Syrinx Pass"])
    step3 = make_interaction(bot, member, channel=channel)
    await town_select.callback(step3)
    modal = step3.response.modal
    assert isinstance(modal, StockpileModal)
    assert len(modal.children) <= 5
    assert modal.location_field is None

    set_label_value(modal.name_field, "Navy QRF")
    set_label_value(modal.code_field, "750541")
    set_label_value(modal.type_field, "Seaport")
    set_label_value(modal.priority_field, "Low")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "edit"
    assert "Stockpile added" in payload["content"]
    rows = await stockpiles.list_entries(bot, guild.id, private=False)
    assert len(rows) == 1
    row = rows[0]
    assert (row["name"], row["hex"], row["region"], row["store_type"], row["priority"], row["code"]) == (
        "Navy QRF",
        "Piper's Enclave",
        "Syrinx Pass",
        "Seaport",
        "Low",
        "750541",
    )


async def test_outsider_cannot_open_add(world):
    bot, guild, _, outsider, channel = world
    interaction = make_interaction(bot, outsider, channel=channel)
    await bot.boards.providers["stockpile"].on_button(interaction, "add")
    kind, payload = interaction.recorder.last
    assert kind == "send" and "permission" in payload["content"]


async def test_board_render(world):
    bot, guild, member, _, _ = world
    await create(bot, guild, member)
    await create(bot, guild, member, name="Secret", private=True)
    render = await bot.boards.providers["stockpile"].render(guild)
    text = "\n".join(e.description for e in render.embeds)
    assert "861781" in text and "KM1" in text
    assert "Secret" not in text
    assert [o.label for o in render.options] == ["KM1 - Pan's Villa, Piper's Enclave"]
    assert {b.action for b in render.buttons} == {"add", "all", "private"}


async def test_panel_refresh_records_who(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    await bot.db.execute("UPDATE stockpiles SET expires_at = 5 WHERE id = ?", (row["id"],))
    panel = StockpilePanel(bot, guild.id, row["id"], member.id)
    opener = make_interaction(bot, member, channel=channel)
    await panel.send(opener)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Refresh Timer").callback(click)
    fresh = await stockpiles.fetch(bot, guild.id, row["id"])
    assert fresh["refreshed_by"] == member.id
    assert fresh["expires_at"] > timeutil.now() + 49 * 3600
    assert "Timer refreshed" in click.recorder.last[1]["content"]


async def test_notify_toggle(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = StockpilePanel(bot, guild.id, row["id"], member.id)
    await panel.send(make_interaction(bot, member, channel=channel))
    await find_button(panel, "Notify Me").callback(make_interaction(bot, member, channel=channel))
    assert await entries.is_subscribed(bot.db, "stockpile", row["id"], member.id)
    await find_button(panel, "Stop Notifying Me").callback(make_interaction(bot, member, channel=channel))
    assert not await entries.is_subscribed(bot.db, "stockpile", row["id"], member.id)


async def test_delete_requires_confirmation(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = StockpilePanel(bot, guild.id, row["id"], member.id)
    await panel.send(make_interaction(bot, member, channel=channel))
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Delete").callback(click)
    confirm_view = click.recorder.last[1]["view"]
    assert await stockpiles.fetch(bot, guild.id, row["id"]) is not None
    await find_button(confirm_view, "Delete").callback(make_interaction(bot, member, channel=channel))
    assert await stockpiles.fetch(bot, guild.id, row["id"]) is None


async def test_private_visibility(world):
    bot, guild, member, outsider, channel = world
    row = await create(bot, guild, member, private=True)
    assert not await stockpiles.can_access(bot, outsider, row)
    await entries.set_access(bot.db, "stockpile", row["id"], {outsider.id}, set())
    assert await stockpiles.can_access(bot, outsider, row)
    assert not await stockpiles.can_manage_access(bot, outsider, row)
    interaction = make_interaction(bot, outsider, channel=channel)
    await stockpiles.show_private(bot, interaction)
    payload = interaction.recorder.last[1]
    assert "KM1" in payload["embed"].description


async def test_panel_blocks_outsider(world):
    bot, guild, member, outsider, channel = world
    row = await create(bot, guild, member)
    interaction = make_interaction(bot, outsider, channel=channel)
    await stockpiles.open_panel(bot, interaction, row["id"])
    assert "access" in interaction.recorder.last[1]["content"]


async def test_alerts_post_to_board_channel_and_dm_private(world):
    bot, guild, member, outsider, channel = world
    bot.boards.channel_id_for = AsyncMock(return_value=channel.id)
    public = await create(bot, guild, member)
    private = await create(bot, guild, member, name="Hidden", private=True)
    await entries.set_access(bot.db, "stockpile", private["id"], {outsider.id}, set())
    await entries.toggle_subscription(bot.db, "stockpile", public["id"], outsider.id)
    await entries.toggle_subscription(bot.db, "stockpile", public["id"], member.id)
    bot.get_user = MagicMock(side_effect=lambda uid: guild._members.get(uid))
    later = timeutil.now() + 49 * 3600
    await bot.alerts.check(now=later)
    assert len(channel.sent) == 1
    assert "KM1" in channel.sent[0]["embed"].description
    assert outsider.send.await_count == 1
    assert member.send.await_count == 2
    await bot.alerts.check(now=later + 60)
    assert len(channel.sent) == 1


async def test_slash_add_resolves_location(world):
    bot, guild, member, _, channel = world
    cog = bot.get_cog("Stockpiles")
    interaction = make_interaction(bot, member, channel=channel)
    await cog.add.callback(cog, interaction, "pipers", "syrinx", "KM2", "338908", "Storage Depot", None, False)
    rows = await stockpiles.list_entries(bot, guild.id, private=False)
    assert rows[0]["hex"] == "Piper's Enclave" and rows[0]["region"] == "Syrinx Pass"
    assert interaction.recorder.last[0] == "send"
