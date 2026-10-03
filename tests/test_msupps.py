from unittest.mock import AsyncMock, MagicMock

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
from foxbot.core.text import EMBED_TOTAL_LIMIT
from foxbot.core.ui import EmbedPaginator, PickerView

msupps = None


@pytest.fixture
async def world():
    global msupps
    bot = await make_bot(["foxbot.features.msupps"])
    msupps = bot.extensions["foxbot.features.msupps"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    partial = MagicMock()
    partial.get_partial_message.return_value.delete = AsyncMock()
    bot.get_partial_messageable = MagicMock(return_value=partial)
    engineers = add_role(guild, "Engineers")
    await bot.perms.set_roles(guild.id, "msupps", {engineers.id})
    member = make_member(guild, name="Engineer", roles=[engineers])
    outsider = make_member(guild, name="Outsider")
    channel = make_channel(guild, "bases")
    yield bot, guild, member, outsider, channel
    await bot.db.close()


async def create(bot, guild, member, **overrides):
    values = dict(
        name="Villa Bunker",
        hex_name="Piper's Enclave",
        region="Pan's Villa",
        msupps=1000,
        rate=100,
        user_id=member.id,
    )
    values.update(overrides)
    return await msupps.create(bot, guild.id, **values)


async def age(bot, row, seconds):
    await bot.db.execute("UPDATE msupps_bases SET measured_at = measured_at - ? WHERE id = ?", (seconds, row["id"]))
    return await msupps.fetch(bot, row["guild_id"], row["id"])


async def open_panel(bot, guild, member, channel, row):
    panel = msupps.MsuppsPanel(bot, guild.id, row["id"], member.id)
    await panel.send(make_interaction(bot, member, channel=channel))
    return panel


async def alert_thresholds(bot, entry_id):
    rows = await bot.db.fetchall("SELECT threshold FROM alert_state WHERE kind = 'msupps' AND entry_id = ?", (entry_id,))
    return sorted(row["threshold"] for row in rows)


def test_parse_amount():
    import foxbot.features.msupps as module

    parse = module.parse_amount
    assert parse("1500") == 1500
    assert parse("1,500") == 1500
    assert parse(" 2 000 ") == 2000
    assert parse("1.5k") == 1500
    assert parse("3K") == 3000
    assert parse("") is None
    assert parse("-5") is None
    assert parse("abc") is None
    assert parse("1.5") is None


def test_decay_math():
    import foxbot.features.msupps as module

    row = {"msupps": 1000, "rate": 100, "measured_at": 10_000}
    assert module.current_msupps(row, 10_000) == 1000
    assert module.current_msupps(row, 10_000 + 7200) == 800
    assert module.current_msupps(row, 10_000 + 1800) == 950
    assert module.current_msupps(row, 10_000 + 20 * 3600) == 0
    assert module.run_out_at(row) == 10_000 + 10 * 3600
    flat = {"msupps": 500, "rate": 0, "measured_at": 10_000}
    assert module.current_msupps(flat, 10_000 + 99 * 3600) == 500
    assert module.run_out_at(flat) is None
    empty = {"msupps": 0, "rate": 30, "measured_at": 10_000}
    assert module.run_out_at(empty) == 10_000
    assert module.is_out(empty, 10_000)


async def test_board_add_flow_uses_picker_then_modal(world):
    bot, guild, member, _, channel = world
    interaction = make_interaction(bot, member, channel=channel)
    await bot.boards.providers["msupps"].on_button(interaction, "add")
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    picker = payload["view"]
    assert isinstance(picker, LocationPicker)
    hex_select = find_select(picker, 0)
    select_values(hex_select, ["Piper's Enclave"])
    await hex_select.callback(make_interaction(bot, member, channel=channel))
    town_select = find_select(picker)
    select_values(town_select, ["Syrinx Pass"])
    step3 = make_interaction(bot, member, channel=channel)
    await town_select.callback(step3)
    modal = step3.response.modal
    assert isinstance(modal, msupps.AddBaseModal)
    assert len(modal.children) == 3

    set_label_value(modal.name_field, "Syrinx BB")
    set_label_value(modal.msupps_field, "abc")
    set_label_value(modal.rate_field, "50")
    bad = make_interaction(bot, member, channel=channel)
    await modal.on_submit(bad)
    assert "whole number" in bad.recorder.last[1]["content"]
    assert await msupps.list_entries(bot, guild.id) == []

    set_label_value(modal.msupps_field, "4,000")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "edit" and "Base added" in payload["content"]
    assert isinstance(payload["view"], msupps.MsuppsPanel)
    rows = await msupps.list_entries(bot, guild.id)
    row = rows[0]
    assert (row["name"], row["hex"], row["region"], row["msupps"], row["rate"], row["measured_by"]) == (
        "Syrinx BB",
        "Piper's Enclave",
        "Syrinx Pass",
        4000,
        50,
        member.id,
    )
    assert msupps.run_out_at(row) == row["measured_at"] + 80 * 3600
    bot.boards.request_refresh.assert_called_with(guild.id, "msupps")


async def test_add_modal_rechecks_permission(world):
    bot, guild, _, outsider, channel = world
    modal = msupps.AddBaseModal(bot, guild.id, bot.locations.resolve_text("Syrinx Pass"), AsyncMock())
    set_label_value(modal.name_field, "Sneaky")
    set_label_value(modal.msupps_field, "100")
    set_label_value(modal.rate_field, "5")
    interaction = make_interaction(bot, outsider, channel=channel)
    await modal.on_submit(interaction)
    assert "permission" in interaction.recorder.last[1]["content"]
    assert await msupps.list_entries(bot, guild.id) == []


async def test_outsider_cannot_use_board_buttons(world):
    bot, guild, member, outsider, channel = world
    row = await create(bot, guild, member)
    for action in ("add", "all"):
        interaction = make_interaction(bot, outsider, channel=channel)
        await bot.boards.providers["msupps"].on_button(interaction, action)
        assert "permission" in interaction.recorder.last[1]["content"]
    pick = make_interaction(bot, outsider, channel=channel)
    await bot.boards.providers["msupps"].on_pick(pick, row["id"])
    assert "permission" in pick.recorder.last[1]["content"]


async def test_board_render_states_and_order(world):
    bot, guild, member, _, _ = world
    await create(bot, guild, member, name="No Rate", rate=0, msupps=700)
    await create(bot, guild, member, name="Dry", msupps=0, rate=20)
    await create(bot, guild, member, name="Soon", msupps=500, rate=100)
    await create(bot, guild, member, name="Later", msupps=5000, rate=100)
    render = await bot.boards.providers["msupps"].render(guild)
    text = "\n".join(e.description for e in render.embeds)
    assert "**No Rate** 700 msupps, rate not set" in text
    assert "**Dry** **OUT OF MSUPPS** (20/h)" in text
    assert "**Soon** ~500 msupps, 100/h, runs out <t:" in text
    assert "**Later** ~5,000 msupps, 100/h, runs out <t:" in text
    assert f"<@{member.id}>" in text
    assert [o.label.split(" - ")[0] for o in render.options] == ["Dry", "Soon", "Later", "No Rate"]
    assert render.options[0].description == "OUT OF MSUPPS | 20/h"
    assert render.options[3].description == "700 msupps | rate not set"
    assert [b.label for b in render.buttons] == ["Add Base", "All Bases"]
    assert {b.action for b in render.buttons} == {"add", "all"}

    for index in range(40):
        await create(bot, guild, member, name=f"Base {index:02d} " + "z" * 45, msupps=index * 10, rate=5)
    render = await bot.boards.providers["msupps"].render(guild)
    assert len(render.options) == 25
    assert sum(len(e) for e in render.embeds) <= EMBED_TOTAL_LIMIT


async def test_panel_add_msupps_rebaselines(world):
    bot, guild, member, _, channel = world
    row = await age(bot, await create(bot, guild, member), 7200)
    assert msupps.current_msupps(row) == 800
    panel = await open_panel(bot, guild, member, channel, row)
    assert [b.label for b in panel.children] == [
        "Add Msupps",
        "Set Msupps",
        "Change Rate",
        "Edit",
        "Reminder Pings",
        "Notify Me",
        "Delete",
    ]
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Add Msupps").callback(click)
    modal = click.response.modal
    assert isinstance(modal, msupps.AmountModal) and len(modal.children) == 1
    set_label_value(modal.amount_field, "0")
    zero = make_interaction(bot, member, channel=channel)
    await modal.on_submit(zero)
    assert "whole number" in zero.recorder.last[1]["content"]
    set_label_value(modal.amount_field, "1.5k")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "edit" and "Added 1,500 msupps" in payload["content"]
    fresh = await msupps.fetch(bot, guild.id, row["id"])
    assert fresh["msupps"] == 2300
    assert fresh["measured_by"] == member.id
    assert abs(fresh["measured_at"] - timeutil.now()) <= 2
    assert await alert_thresholds(bot, row["id"]) == []


async def test_set_msupps_and_change_rate(world):
    bot, guild, member, _, channel = world
    row = await age(bot, await create(bot, guild, member, msupps=2000, rate=100), 3600)
    panel = await open_panel(bot, guild, member, channel, row)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Set Msupps").callback(click)
    modal = click.response.modal
    assert modal.amount_field.component.default == "1900"
    set_label_value(modal.amount_field, "150")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    assert "Msupps set to 150" in submit.recorder.last[1]["content"]
    fresh = await msupps.fetch(bot, guild.id, row["id"])
    assert fresh["msupps"] == 150 and fresh["rate"] == 100
    assert await alert_thresholds(bot, row["id"]) == [2.0, 6.0, 12.0]

    await age(bot, fresh, 1800)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Change Rate").callback(click)
    rate_modal = click.response.modal
    assert rate_modal.amount_field.component.default == "100"
    set_label_value(rate_modal.amount_field, "0")
    submit = make_interaction(bot, member, channel=channel)
    await rate_modal.on_submit(submit)
    assert "Consumption is now not set" in submit.recorder.last[1]["content"]
    fresh = await msupps.fetch(bot, guild.id, row["id"])
    assert fresh["rate"] == 0 and fresh["msupps"] == 100
    assert await alert_thresholds(bot, row["id"]) == []
    provider = bot.alerts.providers["msupps"]
    assert [e.entry_id for e in await provider.timed_entries()] == []

    set_label_value(rate_modal.amount_field, "25")
    again = make_interaction(bot, member, channel=channel)
    await rate_modal.on_submit(again)
    fresh = await msupps.fetch(bot, guild.id, row["id"])
    assert fresh["rate"] == 25 and msupps.run_out_at(fresh) == fresh["measured_at"] + 4 * 3600
    timed = await provider.timed_entries()
    assert [(e.entry_id, e.deadline) for e in timed] == [(row["id"], fresh["measured_at"] + 4 * 3600)]

    set_label_value(rate_modal.amount_field, "999999")
    too_big = make_interaction(bot, member, channel=channel)
    await rate_modal.on_submit(too_big)
    assert "whole number" in too_big.recorder.last[1]["content"]


async def test_edit_modal_name_and_location(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Edit").callback(click)
    modal = click.response.modal
    assert isinstance(modal, msupps.EditBaseModal) and len(modal.children) <= 5
    assert modal.location_field.component.default == "Pan's Villa, Piper's Enclave"
    set_label_value(modal.name_field, "Syrinx Bunker")
    set_label_value(modal.location_field, "synrix")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    assert submit.recorder.last[1]["content"].startswith("Saved.")
    fresh = await msupps.fetch(bot, guild.id, row["id"])
    assert (fresh["name"], fresh["hex"], fresh["region"], fresh["msupps"], fresh["measured_at"]) == (
        "Syrinx Bunker",
        "Piper's Enclave",
        "Syrinx Pass",
        row["msupps"],
        row["measured_at"],
    )
    set_label_value(modal.location_field, "")
    empty = make_interaction(bot, member, channel=channel)
    await modal.on_submit(empty)
    assert "required" in empty.recorder.last[1]["content"]


async def test_panel_buttons_recheck_permission(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Add Msupps").callback(click)
    pending = click.response.modal
    await bot.perms.set_roles(guild.id, "msupps", set())
    for label in ("Add Msupps", "Set Msupps", "Change Rate", "Edit", "Reminder Pings", "Notify Me", "Delete"):
        denied = make_interaction(bot, member, channel=channel)
        await find_button(panel, label).callback(denied)
        kind, payload = denied.recorder.last
        assert kind == "send" and payload["ephemeral"] and "permission" in payload["content"], label
    set_label_value(pending.amount_field, "100")
    late = make_interaction(bot, member, channel=channel)
    await pending.on_submit(late)
    assert "permission" in late.recorder.last[1]["content"]
    assert (await msupps.fetch(bot, guild.id, row["id"]))["msupps"] == 1000


async def test_notify_roles_and_delete(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    await find_button(panel, "Notify Me").callback(make_interaction(bot, member, channel=channel))
    assert await entries.is_subscribed(bot.db, "msupps", row["id"], member.id)
    assert find_button(panel, "Stop Notifying Me")

    pings = add_role(guild, "Base Crew")
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Reminder Pings").callback(click)
    roles_modal = click.response.modal
    set_label_value(roles_modal.roles, [pings])
    await roles_modal.on_submit(make_interaction(bot, member, channel=channel))
    assert await entries.alert_roles(bot.db, "msupps", row["id"]) == [pings.id]

    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Delete").callback(click)
    confirm_view = click.recorder.last[1]["view"]
    assert await msupps.fetch(bot, guild.id, row["id"]) is not None
    done = make_interaction(bot, member, channel=channel)
    await find_button(confirm_view, "Delete").callback(done)
    assert "Deleted" in done.recorder.last[1]["content"]
    assert await msupps.fetch(bot, guild.id, row["id"]) is None
    assert await entries.alert_roles(bot.db, "msupps", row["id"]) == []
    assert await entries.subscribers(bot.db, "msupps", row["id"]) == set()


async def test_alerts_post_reminder_and_expiry(world):
    bot, guild, member, outsider, channel = world
    bot.boards.channel_id_for = AsyncMock(return_value=channel.id)
    bot.get_user = MagicMock(side_effect=lambda uid: guild._members.get(uid))
    row = await create(bot, guild, member, msupps=2000, rate=100)
    await create(bot, guild, member, name="Idle", msupps=50, rate=0)
    await entries.toggle_subscription(bot.db, "msupps", row["id"], member.id)
    await entries.toggle_subscription(bot.db, "msupps", row["id"], outsider.id)
    start = row["measured_at"]
    await bot.alerts.check(now=start + 9 * 3600)
    assert len(channel.sent) == 1
    embed = channel.sent[0]["embed"]
    assert embed.title == "Base maintenance supplies reminder"
    assert "runs out of maintenance supplies" in embed.description
    assert member.send.await_count == 1 and outsider.send.await_count == 0
    view = channel.sent[0]["view"]
    assert any(getattr(item, "label", None) == "Add Msupps" for item in view.children)
    await bot.alerts.check(now=start + 9 * 3600 + 60)
    assert len(channel.sent) == 1
    await bot.alerts.check(now=start + 21 * 3600)
    assert len(channel.sent) == 2
    assert channel.sent[1]["embed"].title == "Base maintenance supplies expired"
    assert "has run out" in channel.sent[1]["embed"].description


async def test_alert_action_opens_modal_and_resets(world):
    bot, guild, member, outsider, channel = world
    bot.boards.channel_id_for = AsyncMock(return_value=channel.id)
    row = await create(bot, guild, member, msupps=500, rate=100)
    await bot.alerts.check(now=row["measured_at"] + 4 * 3600 + 1800)
    assert len(channel.sent) == 1
    assert await bot.db.fetchval("SELECT COUNT(*) FROM alert_messages WHERE entry_id = ?", (row["id"],)) == 1
    provider = bot.alerts.providers["msupps"]
    assert provider.action_label == "Add Msupps"

    denied = make_interaction(bot, outsider, channel=channel)
    await provider.on_alert_action(denied, guild.id, row["id"])
    assert denied.recorder.last[0] == "send" and "permission" in denied.recorder.last[1]["content"]

    click = make_interaction(bot, member, channel=channel)
    await provider.on_alert_action(click, guild.id, row["id"])
    assert click.recorder.last[0] == "modal"
    modal = click.response.modal
    assert isinstance(modal, msupps.AmountModal) and len(modal.children) <= 5
    set_label_value(modal.amount_field, "1000")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "send" and payload["ephemeral"] and "Added 1,000 msupps" in payload["content"]
    fresh = await msupps.fetch(bot, guild.id, row["id"])
    assert fresh["msupps"] == 1500 and fresh["measured_by"] == member.id
    assert await alert_thresholds(bot, row["id"]) == []
    assert await bot.db.fetchval("SELECT COUNT(*) FROM alert_messages WHERE entry_id = ?", (row["id"],)) == 0

    gone = make_interaction(bot, member, channel=channel)
    await provider.on_alert_action(gone, guild.id, "NOPE00")
    assert "no longer tracked" in gone.recorder.last[1]["content"]


async def test_slash_commands(world):
    bot, guild, member, outsider, channel = world
    cog = bot.get_cog("Msupps")
    interaction = make_interaction(bot, member, channel=channel)
    await cog.add.callback(cog, interaction, "pipers", "syrinx", "Syrinx BB", 3000, 60)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"] and "Base added" in payload["content"]
    row = (await msupps.list_entries(bot, guild.id))[0]
    assert (row["hex"], row["region"], row["msupps"], row["rate"]) == ("Piper's Enclave", "Syrinx Pass", 3000, 60)

    denied = make_interaction(bot, outsider, channel=channel)
    await cog.add.callback(cog, denied, "pipers", "syrinx", "Nope", 1, 1)
    assert "permission" in denied.recorder.last[1]["content"]

    choices = await cog.open_autocomplete(make_interaction(bot, member, channel=channel), "syrinx")
    assert [c.value for c in choices] == [row["id"]]
    assert await cog.open_autocomplete(make_interaction(bot, outsider, channel=channel), "") == []

    resupply = make_interaction(bot, member, channel=channel)
    await cog.resupply.callback(cog, resupply, row["id"], 500)
    assert "Added 500 msupps" in resupply.recorder.last[1]["content"]
    assert (await msupps.fetch(bot, guild.id, row["id"]))["msupps"] == 3500
    missing = make_interaction(bot, member, channel=channel)
    await cog.resupply.callback(cog, missing, "NOPE00", 5)
    assert "not found" in missing.recorder.last[1]["content"]
    blocked = make_interaction(bot, outsider, channel=channel)
    await cog.resupply.callback(cog, blocked, row["id"], 5)
    assert "permission" in blocked.recorder.last[1]["content"]

    opened = make_interaction(bot, member, channel=channel)
    await cog.open.callback(cog, opened, row["id"])
    assert isinstance(opened.recorder.last[1]["view"], msupps.MsuppsPanel)

    for index in range(70):
        await create(bot, guild, member, name=f"Base {index:02d} " + "q" * 50)
    listing = make_interaction(bot, member, channel=channel)
    await cog.list_command.callback(cog, listing)
    kind, payload = listing.recorder.last
    assert kind == "send" and payload["ephemeral"] and isinstance(payload["view"], EmbedPaginator)
    assert all(len(e) <= EMBED_TOTAL_LIMIT for e in payload["view"].embeds)

    board = make_interaction(bot, member, channel=channel)
    await cog.board.callback(cog, board)
    assert "Only bot admins" in board.recorder.last[1]["content"]
    admin = make_member(guild, name="Admin", administrator=True)
    posted = make_interaction(bot, admin, channel=channel)
    await cog.board.callback(cog, posted)
    assert "Msupps board posted" in posted.recorder.last[1]["content"]
    custom_ids = {item.custom_id for item in channel.sent[-1]["view"].children}
    assert {"fb:msupps:pick", "fb:msupps:btn:add", "fb:msupps:btn:all"} <= custom_ids


async def test_all_bases_picker_pages(world):
    bot, guild, member, _, channel = world
    for index in range(27):
        await create(bot, guild, member, name=f"Base {index:02d}")
    interaction = make_interaction(bot, member, channel=channel)
    await bot.boards.providers["msupps"].on_button(interaction, "all")
    view = interaction.recorder.last[1]["view"]
    assert isinstance(view, PickerView) and view.page_count == 2
    select = find_select(view)
    assert len(select.options) == 25
    select_values(select, [select.options[0].value])
    pick = make_interaction(bot, member, channel=channel)
    await select.callback(pick)
    assert isinstance(pick.recorder.last[1]["view"], msupps.MsuppsPanel)
