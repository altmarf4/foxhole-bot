from types import SimpleNamespace
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
from foxbot.core import settings as keys
from foxbot.core.locpicker import LocationPicker
from foxbot.core.text import EMBED_TOTAL_LIMIT
from foxbot.core.ui import EmbedPaginator, PickerView

ships = None


@pytest.fixture
async def world():
    global ships
    bot = await make_bot(["foxbot.features.ships"])
    ships = bot.extensions["foxbot.features.ships"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    partial = MagicMock()
    partial.get_partial_message.return_value.delete = AsyncMock()
    bot.get_partial_messageable = MagicMock(return_value=partial)
    navy = add_role(guild, "Navy")
    await bot.perms.set_roles(guild.id, "ship", {navy.id})
    member = make_member(guild, name="Sailor", roles=[navy])
    outsider = make_member(guild, name="Outsider")
    channel = make_channel(guild, "navy")
    yield bot, guild, member, outsider, channel
    await bot.db.close()


async def create(bot, guild, member, **overrides):
    values = dict(
        name="HMS Fox",
        ship_type="Destroyer",
        squad="Alpha",
        hex_name="Piper's Enclave",
        region="Pan's Villa",
        parked_at="Dock 2",
        notes="",
        user_id=member.id,
    )
    values.update(overrides)
    return await ships.create(bot, guild.id, **values)


async def open_panel(bot, guild, member, channel, row):
    panel = ships.ShipPanel(bot, guild.id, row["id"], member.id)
    await panel.send(make_interaction(bot, member, channel=channel))
    return panel


async def test_board_add_flow_uses_picker_then_modal(world):
    bot, guild, member, _, channel = world
    interaction = make_interaction(bot, member, channel=channel)
    await bot.boards.providers["ship"].on_button(interaction, "add")
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    picker = payload["view"]
    assert isinstance(picker, LocationPicker)

    hex_select = find_select(picker, 0)
    select_values(hex_select, ["Piper's Enclave"])
    await hex_select.callback(make_interaction(bot, member, channel=channel))
    town_select = find_select(picker)
    assert len(town_select.options) <= 25
    select_values(town_select, ["Syrinx Pass"])
    step3 = make_interaction(bot, member, channel=channel)
    await town_select.callback(step3)
    modal = step3.response.modal
    assert isinstance(modal, ships.ShipModal)
    assert len(modal.children) <= 5
    assert modal.location_field is None and modal.notes_field is not None
    assert [o.value for o in modal.type_field.component.options][:2] == ["Destroyer", "Submarine"]

    set_label_value(modal.name_field, "Big Blue")
    set_label_value(modal.type_field, "Battleship")
    set_label_value(modal.squad_field, "Navy QRF")
    set_label_value(modal.parked_field, "Seaport dock 1")
    set_label_value(modal.notes_field, "Needs shells")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "edit"
    assert "Ship added" in payload["content"]
    assert isinstance(payload["view"], ships.ShipPanel)
    rows = await ships.list_entries(bot, guild.id)
    assert len(rows) == 1
    row = rows[0]
    assert (row["name"], row["ship_type"], row["squad"], row["hex"], row["region"], row["location"], row["notes"]) == (
        "Big Blue",
        "Battleship",
        "Navy QRF",
        "Piper's Enclave",
        "Syrinx Pass",
        "Seaport dock 1",
        "Needs shells",
    )
    assert row["expires_at"] >= timeutil.now() + 47 * 3600
    bot.boards.request_refresh.assert_called_with(guild.id, "ship")


async def test_add_modal_rechecks_permission(world):
    bot, guild, member, outsider, channel = world
    location = bot.locations.resolve_text("Syrinx Pass")
    saved = AsyncMock()
    modal = ships.ShipModal(bot, guild.id, ["Destroyer"], saved, location=location)
    set_label_value(modal.name_field, "Sneaky")
    set_label_value(modal.type_field, "Destroyer")
    set_label_value(modal.squad_field, "Bravo")
    interaction = make_interaction(bot, outsider, channel=channel)
    await modal.on_submit(interaction)
    assert "permission" in interaction.recorder.last[1]["content"]
    assert saved.await_count == 0
    assert await ships.list_entries(bot, guild.id) == []


async def test_outsider_cannot_open_add_or_list(world):
    bot, guild, _, outsider, channel = world
    for action in ("add", "all"):
        interaction = make_interaction(bot, outsider, channel=channel)
        await bot.boards.providers["ship"].on_button(interaction, action)
        kind, payload = interaction.recorder.last
        assert kind == "send" and payload["ephemeral"] and "permission" in payload["content"]


async def test_board_render_lines_and_limits(world):
    bot, guild, member, _, _ = world
    await create(bot, guild, member)
    await create(bot, guild, member, name="Ghost", ship_type="Submarine", squad="", parked_at="")
    render = await bot.boards.providers["ship"].render(guild)
    text = "\n".join(e.description for e in render.embeds)
    assert "**HMS Fox** [Destroyer] (Alpha) expires <t:" in text
    assert "parked at Dock 2" in text
    assert "**Ghost** [Submarine] expires" in text
    assert f"<@{member.id}>" in text
    assert {b.action for b in render.buttons} == {"add", "all"}
    assert [b.label for b in render.buttons] == ["Add Ship", "All Ships"]
    fox = next(o for o in render.options if o.label == "HMS Fox - Pan's Villa, Piper's Enclave")
    assert fox.description.startswith("Destroyer | Squad Alpha | ")

    for index in range(40):
        await create(bot, guild, member, name=f"Ship {index:02d} " + "x" * 40, notes="n" * 400)
    render = await bot.boards.providers["ship"].render(guild)
    assert len(render.options) == 25
    assert sum(len(e) for e in render.embeds) <= EMBED_TOTAL_LIMIT
    assert all(len(e.description or "") <= 4096 for e in render.embeds)


async def test_board_select_opens_panel_and_checks_permission(world):
    bot, guild, member, outsider, channel = world
    row = await create(bot, guild, member)
    interaction = make_interaction(bot, member, channel=channel)
    await bot.boards.providers["ship"].on_pick(interaction, row["id"])
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"] and isinstance(payload["view"], ships.ShipPanel)
    assert payload["embed"].title == "HMS Fox"
    denied = make_interaction(bot, outsider, channel=channel)
    await bot.boards.providers["ship"].on_pick(denied, row["id"])
    assert "permission" in denied.recorder.last[1]["content"]


async def test_panel_refresh_records_who_and_resets_alerts(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    await bot.db.execute("UPDATE ships SET expires_at = ? WHERE id = ?", (timeutil.now() + 3600, row["id"]))
    await bot.db.execute(
        "INSERT INTO alert_state (kind, entry_id, threshold, sent_at) VALUES ('ship', ?, 12.0, 1)",
        (row["id"],),
    )
    panel = await open_panel(bot, guild, member, channel, row)
    assert [b.label for b in panel.children][:5] == ["Refresh Timer", "Edit", "Notes", "Screenshot", "Reminder Pings"]
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Refresh Timer").callback(click)
    fresh = await ships.fetch(bot, guild.id, row["id"])
    assert fresh["refreshed_by"] == member.id
    assert fresh["expires_at"] > timeutil.now() + 47 * 3600
    assert "Timer refreshed" in click.recorder.last[1]["content"]
    state = await bot.db.fetchall("SELECT * FROM alert_state WHERE entry_id = ?", (row["id"],))
    assert state == []


async def test_panel_buttons_recheck_permission(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    await bot.perms.set_roles(guild.id, "ship", set())
    for label in ("Refresh Timer", "Edit", "Notes", "Screenshot", "Reminder Pings", "Notify Me", "Delete"):
        click = make_interaction(bot, member, channel=channel)
        await find_button(panel, label).callback(click)
        kind, payload = click.recorder.last
        assert kind == "send" and payload["ephemeral"] and "permission" in payload["content"], label
    assert (await ships.fetch(bot, guild.id, row["id"]))["refreshed_by"] is None


async def test_edit_modal_resolves_location_and_stays_within_limits(world):
    bot, guild, member, _, channel = world
    await bot.settings.set(guild.id, keys.SHIP_TYPES, ["Gunboat", "Frigate"])
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Edit").callback(click)
    modal = click.response.modal
    assert isinstance(modal, ships.ShipModal)
    assert len(modal.children) <= 5
    assert modal.notes_field is None and modal.location_field is not None
    assert modal.location_field.component.default == "Pan's Villa, Piper's Enclave"
    assert [o.value for o in modal.type_field.component.options] == ["Destroyer", "Gunboat", "Frigate"]
    set_label_value(modal.name_field, "HMS Vixen")
    set_label_value(modal.type_field, "Frigate")
    set_label_value(modal.squad_field, "Bravo")
    set_label_value(modal.location_field, "synrix")
    set_label_value(modal.parked_field, "")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "edit" and payload["content"].startswith("Saved.")
    fresh = await ships.fetch(bot, guild.id, row["id"])
    assert (fresh["name"], fresh["ship_type"], fresh["squad"], fresh["hex"], fresh["region"], fresh["location"]) == (
        "HMS Vixen",
        "Frigate",
        "Bravo",
        "Piper's Enclave",
        "Syrinx Pass",
        "",
    )


async def test_edit_modal_unknown_location_kept_and_empty_rejected(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = ships.ShipPanel(bot, guild.id, row["id"], member.id)
    modal = ships.ShipModal(bot, guild.id, ["Destroyer"], AsyncMock(), entry=row)
    set_label_value(modal.name_field, "HMS Fox")
    set_label_value(modal.type_field, "Destroyer")
    set_label_value(modal.squad_field, "Alpha")
    set_label_value(modal.location_field, "")
    set_label_value(modal.parked_field, "")
    empty = make_interaction(bot, member, channel=channel)
    await modal.on_submit(empty)
    assert "location is required" in empty.recorder.last[1]["content"]

    async def saved(interaction, saved_row, feedback):
        await panel.show(interaction, notice=feedback)

    modal = ships.ShipModal(bot, guild.id, ["Destroyer"], saved, entry=row)
    set_label_value(modal.name_field, "HMS Fox")
    set_label_value(modal.type_field, "Destroyer")
    set_label_value(modal.squad_field, "Alpha")
    set_label_value(modal.location_field, "Hidden Cove")
    set_label_value(modal.parked_field, "")
    unknown = make_interaction(bot, member, channel=channel)
    await modal.on_submit(unknown)
    assert "not found on the war map" in unknown.recorder.last[1]["content"]
    assert (await ships.fetch(bot, guild.id, row["id"]))["region"] == "Hidden Cove"


async def test_notes_modal_sets_and_clears(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Notes").callback(click)
    modal = click.response.modal
    assert isinstance(modal, ships.NotesModal) and len(modal.children) == 1
    set_label_value(modal.notes_field, "Low on fuel")
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    assert submit.recorder.last[1]["content"] == "Notes saved."
    assert any(f.name == "Notes" and "Low on fuel" in f.value for f in submit.recorder.last[1]["embed"].fields)
    set_label_value(modal.notes_field, "")
    clear = make_interaction(bot, member, channel=channel)
    await modal.on_submit(clear)
    assert clear.recorder.last[1]["content"] == "Notes cleared."
    assert (await ships.fetch(bot, guild.id, row["id"]))["notes"] == ""


async def test_screenshot_and_reminder_roles(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Screenshot").callback(click)
    modal = click.response.modal
    attachment = MagicMock(spec=discord.Attachment)
    attachment.content_type = "image/png"
    attachment.size = 1000
    attachment.read = AsyncMock(return_value=b"png-bytes")
    set_label_value(modal.upload, [attachment])
    submit = make_interaction(bot, member, channel=channel)
    await modal.on_submit(submit)
    assert submit.recorder.last[1]["content"] == "Screenshot saved."
    assert len(submit.recorder.last[1]["attachments"]) == 1
    assert await entries.has_screenshot(bot.db, "ship", row["id"])

    pings = add_role(guild, "Sailors")
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Reminder Pings").callback(click)
    roles_modal = click.response.modal
    set_label_value(roles_modal.roles, [pings])
    submit = make_interaction(bot, member, channel=channel)
    await roles_modal.on_submit(submit)
    assert await entries.alert_roles(bot.db, "ship", row["id"]) == [pings.id]


async def test_notify_toggle(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    panel = await open_panel(bot, guild, member, channel, row)
    await find_button(panel, "Notify Me").callback(make_interaction(bot, member, channel=channel))
    assert await entries.is_subscribed(bot.db, "ship", row["id"], member.id)
    await find_button(panel, "Stop Notifying Me").callback(make_interaction(bot, member, channel=channel))
    assert not await entries.is_subscribed(bot.db, "ship", row["id"], member.id)


async def test_delete_requires_confirmation_and_cancel_returns(world):
    bot, guild, member, _, channel = world
    row = await create(bot, guild, member)
    await entries.toggle_subscription(bot.db, "ship", row["id"], member.id)
    panel = await open_panel(bot, guild, member, channel, row)
    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Delete").callback(click)
    confirm_view = click.recorder.last[1]["view"]
    cancel = make_interaction(bot, member, channel=channel)
    await find_button(confirm_view, "Cancel").callback(cancel)
    assert isinstance(cancel.recorder.last[1]["view"], ships.ShipPanel)
    assert await ships.fetch(bot, guild.id, row["id"]) is not None

    click = make_interaction(bot, member, channel=channel)
    await find_button(panel, "Delete").callback(click)
    confirm_view = click.recorder.last[1]["view"]
    done = make_interaction(bot, member, channel=channel)
    await find_button(confirm_view, "Delete").callback(done)
    assert "Deleted" in done.recorder.last[1]["content"]
    assert await ships.fetch(bot, guild.id, row["id"]) is None
    assert await entries.subscribers(bot.db, "ship", row["id"]) == set()


async def test_open_panel_blocks_outsider(world):
    bot, guild, member, outsider, channel = world
    row = await create(bot, guild, member)
    interaction = make_interaction(bot, outsider, channel=channel)
    await ships.open_panel(bot, interaction, row["id"])
    assert "permission" in interaction.recorder.last[1]["content"]
    missing = make_interaction(bot, member, channel=channel)
    await ships.open_panel(bot, missing, "NOPE00")
    assert "no longer exists" in missing.recorder.last[1]["content"]


async def test_alerts_post_and_dm_only_permitted_subscribers(world):
    bot, guild, member, outsider, channel = world
    bot.boards.channel_id_for = AsyncMock(return_value=channel.id)
    bot.get_user = MagicMock(side_effect=lambda uid: guild._members.get(uid))
    row = await create(bot, guild, member)
    pings = add_role(guild, "Sailors")
    await entries.set_alert_roles(bot.db, "ship", row["id"], [pings.id])
    await entries.toggle_subscription(bot.db, "ship", row["id"], member.id)
    await entries.toggle_subscription(bot.db, "ship", row["id"], outsider.id)
    later = timeutil.now() + 47 * 3600
    await bot.alerts.check(now=later)
    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert sent["embed"].title == "Ship squad lock reminder"
    assert "HMS Fox (Destroyer)" in sent["embed"].description
    assert pings.mention in sent["content"]
    assert member.send.await_count == 1
    assert outsider.send.await_count == 0
    await bot.alerts.check(now=later + 60)
    assert len(channel.sent) == 1


async def test_alert_action_refreshes_timer(world):
    bot, guild, member, outsider, channel = world
    row = await create(bot, guild, member)
    provider = bot.alerts.providers["ship"]
    assert provider.action_label == "Refresh Timer"
    denied = make_interaction(bot, outsider, channel=channel)
    await provider.on_alert_action(denied, guild.id, row["id"])
    assert "permission" in denied.recorder.last[1]["content"]
    interaction = make_interaction(bot, member, channel=channel)
    await provider.on_alert_action(interaction, guild.id, row["id"])
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"] and "Timer refreshed" in payload["content"]
    assert (await ships.fetch(bot, guild.id, row["id"]))["refreshed_by"] == member.id
    gone = make_interaction(bot, member, channel=channel)
    await provider.on_alert_action(gone, guild.id, "NOPE00")
    assert "no longer tracked" in gone.recorder.last[1]["content"]


async def test_slash_add_resolves_location_and_type(world):
    bot, guild, member, outsider, channel = world
    cog = bot.get_cog("Ships")
    interaction = make_interaction(bot, member, channel=channel)
    await cog.add.callback(cog, interaction, "pipers", "syrinx", "Seawolf", "submarine", "Charlie", None, None)
    rows = await ships.list_entries(bot, guild.id)
    assert rows[0]["hex"] == "Piper's Enclave" and rows[0]["region"] == "Syrinx Pass"
    assert rows[0]["ship_type"] == "Submarine" and rows[0]["location"] == "" and rows[0]["notes"] == ""
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"] and "Ship added" in payload["content"]

    bad = make_interaction(bot, member, channel=channel)
    await cog.add.callback(cog, bad, "pipers", "syrinx", "Rowboat", "Canoe", "Charlie", None, None)
    assert "Unknown ship type" in bad.recorder.last[1]["content"]
    denied = make_interaction(bot, outsider, channel=channel)
    await cog.add.callback(cog, denied, "pipers", "syrinx", "Rowboat", "Destroyer", "Charlie", None, None)
    assert "permission" in denied.recorder.last[1]["content"]
    assert len(await ships.list_entries(bot, guild.id)) == 1


async def test_slash_autocomplete_refresh_list_and_board(world):
    bot, guild, member, outsider, channel = world
    cog = bot.get_cog("Ships")
    row = await create(bot, guild, member)
    auto = make_interaction(bot, member, channel=channel, namespace=SimpleNamespace(hex="Piper's Enclave"))
    types = await cog.add_type_autocomplete(auto, "sub")
    assert [c.value for c in types] == ["Submarine"]
    towns = await cog.add_town_autocomplete(auto, "")
    assert "Syrinx Pass" in {c.value for c in towns} and len(towns) <= 25
    choices = await cog.open_autocomplete(auto, "alpha")
    assert [c.value for c in choices] == [row["id"]]
    assert await cog.open_autocomplete(make_interaction(bot, outsider, channel=channel), "") == []

    refresh = make_interaction(bot, member, channel=channel)
    await cog.refresh.callback(cog, refresh, row["id"])
    assert "Timer refreshed" in refresh.recorder.last[1]["content"]
    missing = make_interaction(bot, member, channel=channel)
    await cog.refresh.callback(cog, missing, "NOPE00")
    assert "not found" in missing.recorder.last[1]["content"]

    for index in range(60):
        await create(bot, guild, member, name=f"Ship {index:02d} " + "y" * 50)
    listing = make_interaction(bot, member, channel=channel)
    await cog.list_command.callback(cog, listing)
    kind, payload = listing.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert isinstance(payload["view"], EmbedPaginator)
    assert all(len(e) <= EMBED_TOTAL_LIMIT for e in payload["view"].embeds)
    denied = make_interaction(bot, outsider, channel=channel)
    await cog.list_command.callback(cog, denied)
    assert "permission" in denied.recorder.last[1]["content"]

    board = make_interaction(bot, member, channel=channel)
    await cog.board.callback(cog, board)
    assert "Only bot admins" in board.recorder.last[1]["content"]
    assert channel.sent == []


async def test_all_ships_picker_pages(world):
    bot, guild, member, _, channel = world
    for index in range(30):
        await create(bot, guild, member, name=f"Ship {index:02d}")
    interaction = make_interaction(bot, member, channel=channel)
    await bot.boards.providers["ship"].on_button(interaction, "all")
    view = interaction.recorder.last[1]["view"]
    assert isinstance(view, PickerView) and view.page_count == 2
    select = find_select(view)
    assert len(select.options) == 25
    select_values(select, [select.options[0].value])
    pick = make_interaction(bot, member, channel=channel)
    await select.callback(pick)
    assert isinstance(pick.recorder.last[1]["view"], ships.ShipPanel)


async def test_admin_posts_board(world):
    bot, guild, member, _, channel = world
    admin = make_member(guild, name="Admin", administrator=True)
    cog = bot.get_cog("Ships")
    await create(bot, guild, member)
    interaction = make_interaction(bot, admin, channel=channel)
    await cog.board.callback(cog, interaction)
    assert len(channel.sent) == 1
    view = channel.sent[0]["view"]
    custom_ids = {item.custom_id for item in view.children}
    assert {"fb:ship:pick", "fb:ship:btn:add", "fb:ship:btn:all"} <= custom_ids
    assert "Ship board posted" in interaction.recorder.last[1]["content"]
    assert await bot.boards.channel_id_for(guild.id, "ship") == channel.id
