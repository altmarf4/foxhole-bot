import ast
import asyncio
import io
import re
import tokenize
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

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
from foxbot.core import settings as keys
from foxbot.core import timeutil
from foxbot.core.locpicker import LocationPicker
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_FIELD_LIMIT, EMBED_TOTAL_LIMIT
from foxbot.core.ui import ConfirmView, EmbedPaginator, PickerView

logi = None

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


def not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Message")


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50007, "message": "Cannot send messages to this user"})


class MessageStore:
    def __init__(self):
        self.messages: dict[int, MagicMock] = {}
        self.missing: set[int] = set()
        self.deleted: list[int] = []

    def channel(self, channel_id, **kwargs):
        partial = MagicMock()
        partial.id = channel_id
        partial.get_partial_message.side_effect = self.message
        return partial

    def message(self, message_id: int) -> MagicMock:
        if message_id not in self.messages:
            message = MagicMock()
            message.id = message_id
            message.edits = []

            async def do_edit(**kwargs):
                if message_id in self.missing:
                    raise not_found()
                message.edits.append(kwargs)

            async def do_delete():
                if message_id in self.missing:
                    raise not_found()
                self.deleted.append(message_id)

            message.edit = AsyncMock(side_effect=do_edit)
            message.delete = AsyncMock(side_effect=do_delete)
            self.messages[message_id] = message
        return self.messages[message_id]


@pytest.fixture
async def world():
    global logi
    bot = await make_bot(["foxbot.features.logi"])
    logi = bot.extensions["foxbot.features.logi"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    drivers = add_role(guild, "Drivers")
    logistics = add_role(guild, "Logistics")
    await bot.perms.set_roles(guild.id, "logi", {drivers.id})
    store = MessageStore()
    bot.get_partial_messageable = MagicMock(side_effect=store.channel)
    w = SimpleNamespace(
        bot=bot,
        guild=guild,
        drivers=drivers,
        logistics=logistics,
        requester=make_member(guild, name="Requester", roles=[drivers]),
        driver=make_member(guild, name="Driver", roles=[drivers]),
        rival=make_member(guild, name="Rival", roles=[drivers]),
        outsider=make_member(guild, name="Outsider"),
        admin=make_member(guild, name="Admin", roles=[drivers], administrator=True),
        board_channel=make_channel(guild, "logi-board"),
        alerts_channel=make_channel(guild, "alerts"),
        store=store,
    )
    yield w
    await bot.db.close()


def interact(w, member, **kwargs):
    kwargs.setdefault("channel", w.board_channel)
    return make_interaction(w.bot, member, **kwargs)


def texts(interaction) -> str:
    return "\n".join(str(payload["content"]) for _, payload in interaction.recorder.calls if payload.get("content"))


async def use_board(w, channel=None):
    await w.bot.db.execute(
        "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (?, 'logi', ?, ?)",
        (w.guild.id, (channel or w.board_channel).id, 4242),
    )


async def make_run(w, *, user=None, ping=False, **overrides):
    values = dict(
        title="Bmats for the BB",
        cargo="60 Bmats\n20 Rifle crates",
        pickup_hex="Piper's Enclave",
        pickup_region="Syrinx Pass",
        dest_hex="Piper's Enclave",
        dest_region="Pan's Villa",
        priority="Medium",
        notes="",
        user_id=(user or w.requester).id,
    )
    values.update(overrides)
    run = await logi.create(w.bot, w.guild.id, **values)
    if ping:
        result = await logi.post_ping(w.bot, run)
        assert result.channel is not None
    return await logi.fetch(w.bot, w.guild.id, run["id"])


async def set_times(w, run, **columns):
    assignments = ", ".join(f"{column} = ?" for column in columns)
    await w.bot.db.execute(f"UPDATE logi_runs SET {assignments} WHERE id = ?", (*columns.values(), run["id"]))
    return await logi.fetch(w.bot, w.guild.id, run["id"])


async def open_panel(w, run, member):
    interaction = interact(w, member)
    await logi.open_panel(w.bot, interaction, run["id"])
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"], payload
    assert isinstance(payload["view"], logi.RunPanel)
    return payload["view"], payload


async def click(w, view, label, member):
    interaction = interact(w, member)
    await find_button(view, label).callback(interaction)
    return interaction


def labels(view) -> list[str]:
    return [item.label for item in view.children if isinstance(item, discord.ui.Button)]


def assert_modal_limits(modal):
    assert len(modal.children) <= 5
    assert len(modal.title) <= 45
    for label in modal.children:
        assert len(label.text) <= 45
        assert label.description is None or len(label.description) <= 100
        placeholder = getattr(label.component, "placeholder", None)
        assert placeholder is None or len(placeholder) <= 100


def assert_view_limits(view):
    for item in view.children:
        custom_id = getattr(item, "custom_id", None)
        if custom_id and not getattr(item, "url", None):
            assert len(custom_id) <= 100
        if isinstance(item, discord.ui.Select):
            assert len(item.options) <= 25
            assert all(len(o.label) <= 100 and (o.description is None or len(o.description) <= 100) for o in item.options)
        if isinstance(item, discord.ui.Button):
            assert item.label is None or len(item.label) <= 80
    rows: dict[int, int] = {}
    for item in view.children:
        rows[item.row or 0] = rows.get(item.row or 0, 0) + 1
    assert len(rows) <= 5


def assert_embed_limits(embed):
    assert len(embed) <= EMBED_TOTAL_LIMIT
    assert len(embed.description or "") <= EMBED_DESCRIPTION_LIMIT
    assert all(len(field.value) <= EMBED_FIELD_LIMIT for field in embed.fields)


async def test_slash_request_creates_run_pings_once_and_opens_panel(world):
    w = world
    await use_board(w)
    await w.bot.settings.set(w.guild.id, keys.LOGISTICS_ROLE, w.logistics.id)
    cog = w.bot.get_cog("Logi")
    interaction = interact(w, w.requester)
    await cog.request_command.callback(
        cog,
        interaction,
        "  Bmats for BB ",
        "60 Bmats, 20 Rifle crates",
        "pipers",
        "syrinx",
        "deadlands",
        app_commands.Choice(name="High", value="High"),
        " be *quick* ",
    )
    assert interaction.recorder.kinds() == ["defer", "followup"]
    assert interaction.recorder.calls[0][1]["ephemeral"]
    rows = await logi.list_active(w.bot, w.guild.id)
    assert len(rows) == 1
    run = rows[0]
    assert (run["title"], run["cargo"], run["priority"], run["notes"], run["status"]) == (
        "Bmats for BB",
        "60 Bmats, 20 Rifle crates",
        "High",
        "be *quick*",
        "open",
    )
    assert (run["dest_hex"], run["dest_region"]) == ("Piper's Enclave", "Syrinx Pass")
    assert (run["pickup_hex"], run["pickup_region"]) == ("Deadlands", "")
    assert run["created_by"] == w.requester.id and run["claimed_by"] is None

    assert len(w.board_channel.sent) == 1 and not w.alerts_channel.sent
    ping = w.board_channel.sent[0]
    assert ping["content"].startswith(w.logistics.mention)
    assert "Bmats for BB" in ping["content"] and "Deadlands -> Syrinx Pass, Piper's Enclave" in ping["content"]
    assert "[High]" in ping["content"]
    mentions = ping["allowed_mentions"]
    assert mentions.roles == [w.logistics] and mentions.users is False and mentions.everyone is False
    assert ping["embed"].title == "Bmats for BB"
    assert "\\*quick\\*" in next(f.value for f in ping["embed"].fields if f.name == "Notes")
    assert [item.custom_id for item in ping["view"].children] == [f"flr:claim:{run['id']}", f"flr:view:{run['id']}"]
    assert run["ping_channel_id"] == w.board_channel.id and run["ping_message_id"] is not None

    payload = interaction.recorder.last[1]
    assert payload["ephemeral"]
    panel = payload["view"]
    assert isinstance(panel, logi.RunPanel)
    assert labels(panel) == ["Claim", "Mark Delivered", "Edit", "Cancel Run", "Refresh"]
    assert_view_limits(panel)
    assert "Destination: **Syrinx Pass, Piper's Enclave**" in payload["content"]
    assert "Pickup: **Deadlands**" in payload["content"]
    assert f"The logistics role was pinged in {w.board_channel.mention}." in payload["content"]
    embed = payload["embed"]
    assert embed.title == "Bmats for BB"
    assert next(f.value for f in embed.fields if f.name == "Status") == "Open - waiting for a driver"
    assert_embed_limits(embed)
    w.bot.boards.request_refresh.assert_any_call(w.guild.id, "logi")


async def test_ping_falls_back_to_alert_channel_and_skips_without_any(world):
    w = world
    await w.bot.settings.set(w.guild.id, keys.ALERT_CHANNEL, w.alerts_channel.id)
    cog = w.bot.get_cog("Logi")
    first = interact(w, w.requester)
    await cog.request_command.callback(cog, first, "Shells", "40 Mortar shells", "Deadlands", None, None, None, None)
    assert len(w.alerts_channel.sent) == 1 and not w.board_channel.sent
    ping = w.alerts_channel.sent[0]
    assert not ping["content"].startswith("<@&")
    assert ping["allowed_mentions"].roles is False
    assert "No logistics role is set" in first.recorder.last[1]["content"]
    run = (await logi.list_active(w.bot, w.guild.id))[0]
    assert run["priority"] == "Medium" and run["pickup_hex"] == "" and run["notes"] == ""
    assert (run["dest_hex"], run["dest_region"]) == ("Deadlands", "")

    await w.bot.db.execute(
        "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (?, 'logi', ?, ?)",
        (w.guild.id, 987654321, 1),
    )
    second = interact(w, w.requester)
    await cog.request_command.callback(cog, second, "Gone board", "1 Bmats", "Deadlands", None, None, None, None)
    assert len(w.alerts_channel.sent) == 2

    await w.bot.settings.reset(w.guild.id, keys.ALERT_CHANNEL)
    third = interact(w, w.requester)
    await cog.request_command.callback(cog, third, "Nowhere", "1 Bmats", "Deadlands", None, None, None, None)
    assert "No logi board or alerts channel is set" in third.recorder.last[1]["content"]
    assert len(w.alerts_channel.sent) == 2
    nowhere = [row for row in await logi.list_active(w.bot, w.guild.id) if row["title"] == "Nowhere"][0]
    assert nowhere["ping_message_id"] is None


async def test_ping_send_failure_falls_through_and_reports(world):
    w = world
    await use_board(w)
    await w.bot.settings.set(w.guild.id, keys.ALERT_CHANNEL, w.alerts_channel.id)
    w.board_channel.send = AsyncMock(side_effect=forbidden())
    run = await make_run(w)
    result = await logi.post_ping(w.bot, run)
    assert result.channel is w.alerts_channel
    w.alerts_channel.send = AsyncMock(side_effect=forbidden())
    other = await make_run(w, title="Second")
    result = await logi.post_ping(w.bot, other)
    assert result.channel is None and result.tried
    assert "could not post" in logi.ping_feedback(result)


async def test_board_request_flow_uses_location_picker_and_modal(world):
    w = world
    await use_board(w)
    board = w.bot.boards.providers["logi"]
    start = interact(w, w.requester)
    await board.on_button(start, "request")
    kind, payload = start.recorder.last
    assert kind == "send" and payload["ephemeral"]
    picker = payload["view"]
    assert isinstance(picker, LocationPicker)
    assert "Request a logi run" in payload["content"]
    assert_view_limits(picker)
    hex_select = find_select(picker, 0)
    select_values(hex_select, ["Piper's Enclave"])
    await hex_select.callback(interact(w, w.requester))
    town_select = find_select(picker)
    select_values(town_select, ["Pan's Villa"])
    to_modal = interact(w, w.requester)
    await town_select.callback(to_modal)
    modal = to_modal.response.modal
    assert isinstance(modal, logi.RunModal)
    assert_modal_limits(modal)
    assert modal.destination.hex == "Piper's Enclave" and modal.destination.region == "Pan's Villa"
    assert modal.destination_field is None
    set_label_value(modal.title_field, "Rifles for the front")
    set_label_value(modal.cargo_field, "20 Rifle crates\n10 Ammo crates")
    set_label_value(modal.pickup_field, "synrix")
    set_label_value(modal.priority_field, ["Low"])
    set_label_value(modal.notes_field, "")
    submit = interact(w, w.requester)
    await modal.on_submit(submit)
    assert submit.recorder.kinds() == ["defer", "edit_original"]
    payload = submit.recorder.last[1]
    assert isinstance(payload["view"], logi.RunPanel)
    assert "Destination: **Pan's Villa, Piper's Enclave**" in payload["content"]
    assert "Pickup: **Syrinx Pass, Piper's Enclave**" in payload["content"]
    run = (await logi.list_active(w.bot, w.guild.id))[0]
    assert (run["title"], run["cargo"], run["priority"]) == ("Rifles for the front", "20 Rifle crates\n10 Ammo crates", "Low")
    assert (run["pickup_hex"], run["pickup_region"]) == ("Piper's Enclave", "Syrinx Pass")
    assert len(w.board_channel.sent) == 1
    assert run["ping_message_id"] is not None


async def test_request_flow_rechecks_permission_at_each_step(world):
    w = world
    board = w.bot.boards.providers["logi"]
    denied = interact(w, w.outsider)
    await board.on_button(denied, "request")
    assert "permission" in texts(denied)

    start = interact(w, w.requester)
    await board.on_button(start, "request")
    picker = start.recorder.last[1]["view"]
    hex_select = find_select(picker, 0)
    select_values(hex_select, ["Piper's Enclave"])
    await hex_select.callback(interact(w, w.requester))
    town_select = find_select(picker)
    select_values(town_select, ["Pan's Villa"])
    to_modal = interact(w, w.requester)
    await town_select.callback(to_modal)
    modal = to_modal.response.modal

    await w.bot.perms.set_roles(w.guild.id, "logi", set())
    blocked_pick = interact(w, w.requester)
    await town_select.callback(blocked_pick)
    assert blocked_pick.response.modal is None and "permission" in texts(blocked_pick)

    set_label_value(modal.title_field, "Late")
    set_label_value(modal.cargo_field, "1 Bmats")
    set_label_value(modal.pickup_field, "")
    set_label_value(modal.priority_field, ["High"])
    set_label_value(modal.notes_field, "")
    submit = interact(w, w.requester)
    await modal.on_submit(submit)
    assert "permission" in texts(submit)
    assert await logi.list_active(w.bot, w.guild.id) == []


async def test_modal_rejects_blank_title_or_cargo(world):
    w = world
    destination = w.bot.locations.resolve_text("Pan's Villa")
    modal = logi.RunModal(w.bot, w.guild.id, destination=destination)
    set_label_value(modal.title_field, "   ")
    set_label_value(modal.cargo_field, "5 Bmats")
    set_label_value(modal.pickup_field, "")
    set_label_value(modal.priority_field, [])
    set_label_value(modal.notes_field, "")
    submit = interact(w, w.requester)
    await modal.on_submit(submit)
    assert logi.NEEDS_DETAILS in texts(submit)
    set_label_value(modal.title_field, "Fine")
    ok = interact(w, w.requester)
    await modal.on_submit(ok)
    run = (await logi.list_active(w.bot, w.guild.id))[0]
    assert run["priority"] == "Medium" and run["pickup_hex"] == "" and run["pickup_region"] == ""
    assert "No logi board or alerts channel is set" in ok.recorder.last[1]["content"]


async def test_slash_request_validation(world):
    w = world
    cog = w.bot.get_cog("Logi")
    outsider = interact(w, w.outsider)
    await cog.request_command.callback(cog, outsider, "x", "y", "Deadlands", None, None, None, None)
    assert "permission" in texts(outsider)
    blank = interact(w, w.requester)
    await cog.request_command.callback(cog, blank, "   ", "5 Bmats", "Deadlands", None, None, None, None)
    assert logi.NEEDS_DETAILS in texts(blank)
    bad_priority = interact(w, w.requester)
    await cog.request_command.callback(
        cog, bad_priority, "x", "y", "Deadlands", None, None, app_commands.Choice(name="Urgent", value="Urgent"), None
    )
    assert "Unknown priority" in texts(bad_priority)
    assert await logi.list_active(w.bot, w.guild.id) == []

    town_in_hex = interact(w, w.requester)
    await cog.request_command.callback(cog, town_in_hex, "Town typed as hex", "5 Bmats", "Syrinx Pass", None, None, None, None)
    unknown = interact(w, w.requester)
    await cog.request_command.callback(cog, unknown, "Secret", "5 Bmats", "Our secret base", None, "nowhere special", None, None)
    rows = {row["title"]: row for row in await logi.list_active(w.bot, w.guild.id)}
    assert (rows["Town typed as hex"]["dest_hex"], rows["Town typed as hex"]["dest_region"]) == ("Piper's Enclave", "Syrinx Pass")
    assert rows["Secret"]["dest_region"] == "Our secret base" or rows["Secret"]["dest_hex"] == "Our secret base"
    assert rows["Secret"]["pickup_region"] == "nowhere special"
    content = unknown.recorder.last[1]["content"]
    assert "was not found on the war map" in content and "nowhere special" in content


async def test_autocomplete(world):
    w = world
    cog = w.bot.get_cog("Logi")
    hexes = await cog.destination_hex_autocomplete(interact(w, w.requester), "piper")
    assert [c.value for c in hexes] == ["Piper's Enclave"]
    towns = await cog.destination_town_autocomplete(
        interact(w, w.requester, namespace=SimpleNamespace(destination_hex="Piper's Enclave")), ""
    )
    names = {c.value for c in towns}
    assert "Syrinx Pass" in names and "Pan's Villa" in names and len(towns) <= 25
    assert all("(" not in c.name for c in towns)
    unfiltered = await cog.destination_town_autocomplete(interact(w, w.requester), "syrinx")
    assert any(c.name == "Syrinx Pass (Piper's Enclave)" for c in unfiltered)
    pickups = await cog.pickup_autocomplete(interact(w, w.requester), "syrinx")
    assert "Syrinx Pass, Piper's Enclave" in {c.value for c in pickups} and len(pickups) <= 25


async def test_claim_race_reports_who_got_it(world):
    w = world
    await use_board(w)
    run = await make_run(w, ping=True)
    ping_id = run["ping_message_id"]
    board = w.bot.boards.providers["logi"]
    pick = interact(w, w.driver)
    await board.on_pick(pick, run["id"])
    driver_panel = pick.recorder.last[1]["view"]
    rival_panel, _ = await open_panel(w, run, w.rival)
    assert labels(rival_panel) == ["Claim", "Refresh"]

    w.bot.boards.request_refresh.reset_mock()
    won = await click(w, driver_panel, "Claim", w.driver)
    w.bot.boards.request_refresh.assert_called_with(w.guild.id, "logi")
    kind, payload = won.recorder.last
    assert kind == "edit"
    assert "You claimed **Bmats for the BB**" in payload["content"]
    assert labels(driver_panel) == ["Unclaim", "Mark Delivered", "Refresh"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert current["status"] == "claimed" and current["claimed_by"] == w.driver.id and current["claimed_at"]
    assert ping_id in w.store.deleted and current["ping_message_id"] is None
    assert w.requester.send.await_count == 1
    dm = w.requester.send.await_args.kwargs["embed"]
    assert f"claimed by <@{w.driver.id}>" in dm.description and dm.author.name == w.guild.name

    w.bot.boards.request_refresh.reset_mock()
    lost = await click(w, rival_panel, "Claim", w.rival)
    w.bot.boards.request_refresh.assert_not_called()
    kind, payload = lost.recorder.last
    assert kind == "edit" and f"Too late: <@{w.driver.id}>" in payload["content"]
    assert labels(rival_panel) == ["Refresh"]
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["claimed_by"] == w.driver.id
    assert w.requester.send.await_count == 1

    again = await click(w, driver_panel, "Unclaim", w.driver)
    assert "Released your claim" in again.recorder.last[1]["content"]

    second = await make_run(w, title="Contested")
    results = await asyncio.gather(
        logi.claim(w.bot, second, w.driver.id),
        logi.claim(w.bot, second, w.rival.id),
        logi.claim(w.bot, second, w.requester.id),
    )
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    stored = await logi.fetch(w.bot, w.guild.id, second["id"])
    assert stored["claimed_by"] == winners[0]["claimed_by"]


async def test_own_claim_is_not_dmed_and_already_claimed_notice(world):
    w = world
    run = await make_run(w)
    panel, _ = await open_panel(w, run, w.requester)
    await click(w, panel, "Claim", w.requester)
    assert w.requester.send.await_count == 0
    stale, _ = await open_panel(w, run, w.requester)
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    outcome = await logi.attempt_claim(w.bot, w.requester, current)
    assert outcome.run is None and outcome.notice == "You have already claimed this run."
    assert labels(stale) == ["Unclaim", "Mark Delivered", "Edit", "Cancel Run", "Refresh"]


async def test_ping_buttons_are_persistent_and_recheck(world):
    w = world
    await use_board(w)
    registered = set(w.bot._connection._view_store._dynamic_items.values())
    assert logi.PingButtonItem in registered
    run = await make_run(w, ping=True)
    ping_view = w.board_channel.sent[-1]["view"]
    claim_item, view_item = ping_view.children
    template = logi.PingButtonItem.__discord_ui_compiled_template__
    assert not template.fullmatch("fb:logi:btn:request") and not template.fullmatch("fo:edit:ABCDEF")

    async def press(item, member, message=None):
        interaction = interact(w, member, message=message)
        match = template.fullmatch(item.custom_id)
        dynamic = await logi.PingButtonItem.from_custom_id(interaction, item, match)
        await dynamic.callback(interaction)
        return interaction

    denied = await press(claim_item, w.outsider)
    assert "permission to claim" in texts(denied)
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["status"] == "open"
    peek = await press(view_item, w.outsider)
    assert "permission" in texts(peek)

    claimed = await press(claim_item, w.driver)
    kind, payload = claimed.recorder.last
    assert kind == "send" and payload["ephemeral"] and isinstance(payload["view"], logi.RunPanel)
    assert "You claimed" in payload["content"]
    assert run["ping_message_id"] in w.store.deleted

    stale_message = MagicMock()
    stale_message.delete = AsyncMock()
    late = await press(claim_item, w.rival, message=stale_message)
    assert f"Too late: <@{w.driver.id}>" in texts(late)
    stale_message.delete.assert_awaited_once()

    details = await press(view_item, w.rival)
    kind, payload = details.recorder.last
    assert kind == "send" and isinstance(payload["view"], logi.RunPanel)
    assert labels(payload["view"]) == ["Refresh"]

    gone = logi.PingButtonItem("claim", "ZZZZZZ")
    missing = interact(w, w.driver)
    await gone.callback(missing)
    assert "no longer exists" in texts(missing)


async def test_unclaim_rules(world):
    w = world
    run = await make_run(w)
    run = await logi.claim(w.bot, run, w.rival.id)
    driver_panel, _ = await open_panel(w, run, w.driver)
    assert labels(driver_panel) == ["Refresh"]
    outcome = await logi.attempt_unclaim(w.bot, w.driver, run)
    assert outcome.denied and "Only the driver who claimed" in outcome.notice

    rival_panel, _ = await open_panel(w, run, w.rival)
    assert labels(rival_panel) == ["Unclaim", "Mark Delivered", "Refresh"]
    admin_panel, _ = await open_panel(w, run, w.admin)
    assert labels(admin_panel) == ["Unclaim", "Mark Delivered", "Edit", "Cancel Run", "Refresh"]
    w.admin.guild_permissions = discord.Permissions()
    refused = await click(w, admin_panel, "Unclaim", w.admin)
    assert refused.recorder.last[0] == "send" and "Only the driver who claimed" in texts(refused)
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["claimed_by"] == w.rival.id
    w.admin.guild_permissions = discord.Permissions(administrator=True)
    released = await click(w, admin_panel, "Unclaim", w.admin)
    assert f"Released the claim of <@{w.rival.id}>" in released.recorder.last[1]["content"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert (current["status"], current["claimed_by"], current["claimed_at"]) == ("open", None, None)
    assert labels(admin_panel)[0] == "Claim"

    await logi.claim(w.bot, current, w.driver.id)
    stale = await click(w, rival_panel, "Unclaim", w.rival)
    kind, payload = stale.recorder.last
    assert kind == "edit" and "changed after this panel was shown, so nothing was done" in payload["content"]
    assert f"Claimed by <@{w.driver.id}>" in payload["content"]
    assert labels(rival_panel) == ["Refresh"]
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["claimed_by"] == w.driver.id
    w.requester.send.assert_not_awaited()


async def test_unclaim_race_keeps_the_new_claim(world):
    w = world
    run = await make_run(w)
    claimed_by_driver = await logi.claim(w.bot, run, w.driver.id)
    await logi.unclaim(w.bot, claimed_by_driver)
    fresh = await logi.fetch(w.bot, w.guild.id, run["id"])
    await logi.claim(w.bot, fresh, w.rival.id)
    assert await logi.unclaim(w.bot, claimed_by_driver) is None
    assert await logi.deliver(w.bot, claimed_by_driver, w.driver.id) is None
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert (current["status"], current["claimed_by"]) == ("claimed", w.rival.id)


async def test_mark_delivered_rules_and_dms(world):
    w = world
    await use_board(w)
    run = await make_run(w, ping=True)
    outsider = interact(w, w.outsider)
    await logi.open_panel(w.bot, outsider, run["id"])
    assert "permission" in texts(outsider)

    rival_panel, _ = await open_panel(w, run, w.rival)
    assert "Mark Delivered" not in labels(rival_panel)
    assert (await logi.attempt_deliver(w.bot, w.rival, run)).denied

    driver_panel, _ = await open_panel(w, run, w.driver)
    await click(w, driver_panel, "Claim", w.driver)
    delivered = await click(w, driver_panel, "Mark Delivered", w.driver)
    assert "Marked **Bmats for the BB** as delivered" in delivered.recorder.last[1]["content"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert (current["status"], current["delivered_by"]) == ("delivered", w.driver.id) and current["delivered_at"]
    assert labels(driver_panel) == ["Refresh"]
    embed = delivered.recorder.last[1]["embed"]
    assert embed.title.startswith("[Delivered] ")
    assert w.requester.send.await_count == 2
    assert f"marked delivered by <@{w.driver.id}>" in w.requester.send.await_args.kwargs["embed"].description
    late = await click(w, rival_panel, "Claim", w.rival)
    assert "already delivered" in late.recorder.last[1]["content"]

    own = await make_run(w, title="Own run", ping=True)
    own_panel, _ = await open_panel(w, own, w.requester)
    await click(w, own_panel, "Mark Delivered", w.requester)
    assert (await logi.fetch(w.bot, w.guild.id, own["id"]))["status"] == "delivered"
    assert own["ping_message_id"] in w.store.deleted
    assert w.requester.send.await_count == 2

    by_admin = await make_run(w, title="Admin closes")
    admin_panel, _ = await open_panel(w, by_admin, w.admin)
    await click(w, admin_panel, "Mark Delivered", w.admin)
    assert w.requester.send.await_count == 3


async def test_dm_and_ping_delete_failures_are_ignored(world):
    w = world
    await use_board(w)
    run = await make_run(w, ping=True)
    w.store.missing.add(run["ping_message_id"])
    w.requester.send = AsyncMock(side_effect=forbidden())
    panel, _ = await open_panel(w, run, w.driver)
    claimed = await click(w, panel, "Claim", w.driver)
    assert "You claimed" in claimed.recorder.last[1]["content"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert current["status"] == "claimed" and current["ping_message_id"] is None
    w.requester.send.assert_awaited_once()

    gone = await make_run(w, title="Requester left", user=w.rival)
    del w.guild._members[w.rival.id]
    w.guild.fetch_member = AsyncMock(side_effect=not_found())
    driver_panel, _ = await open_panel(w, gone, w.driver)
    done = await click(w, driver_panel, "Claim", w.driver)
    assert "You claimed" in done.recorder.last[1]["content"]
    w.rival.send.assert_not_awaited()


async def test_post_ping_cleans_up_when_claimed_meanwhile(world):
    w = world
    await use_board(w)
    run = await make_run(w)
    await logi.claim(w.bot, run, w.driver.id)
    result = await logi.post_ping(w.bot, run)
    assert result.channel is w.board_channel
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert current["ping_message_id"] is None
    assert len(w.store.deleted) == 1


async def test_edit_via_panel(world):
    w = world
    await use_board(w)
    await w.bot.settings.set(w.guild.id, keys.LOGISTICS_ROLE, w.logistics.id)
    run = await make_run(w, ping=True, notes="Old notes")
    ping_id = run["ping_message_id"]
    rival_panel, _ = await open_panel(w, run, w.rival)
    assert "Edit" not in labels(rival_panel)

    panel, _ = await open_panel(w, run, w.requester)
    opened = await click(w, panel, "Edit", w.requester)
    modal = opened.response.modal
    assert isinstance(modal, logi.RunModal)
    assert_modal_limits(modal)
    assert modal.priority_field is None
    assert modal.title_field.component.default == "Bmats for the BB"
    assert modal.cargo_field.component.default == "60 Bmats\n20 Rifle crates"
    assert modal.pickup_field.component.default == "Syrinx Pass, Piper's Enclave"
    assert modal.destination_field.component.default == "Pan's Villa, Piper's Enclave"
    assert modal.notes_field.component.default == "Old notes"
    set_label_value(modal.title_field, "Bmats and shells")
    set_label_value(modal.cargo_field, "60 Bmats\n40 Mortar shells")
    set_label_value(modal.pickup_field, "")
    set_label_value(modal.destination_field, "Titancall, The Fingers")
    set_label_value(modal.notes_field, "")
    saved = interact(w, w.requester)
    await modal.on_submit(saved)
    kind, payload = saved.recorder.last
    assert kind == "edit" and payload["view"] is panel
    assert "Saved." in payload["content"]
    assert "Destination: **Titancall, The Fingers**" in payload["content"]
    assert "The pickup was removed." in payload["content"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert (current["title"], current["cargo"], current["notes"]) == ("Bmats and shells", "60 Bmats\n40 Mortar shells", "")
    assert (current["dest_hex"], current["dest_region"], current["pickup_hex"], current["pickup_region"]) == ("The Fingers", "Titancall", "", "")
    ping_edit = w.store.message(ping_id).edits[-1]
    assert ping_edit["embed"].title == "Bmats and shells"
    assert ping_edit["content"].startswith(w.logistics.mention)
    assert ping_edit["allowed_mentions"].roles == [w.logistics]

    unchanged = interact(w, w.requester)
    await modal.on_submit(unchanged)
    assert unchanged.recorder.last[1]["content"] == "Saved."

    set_label_value(modal.destination_field, "Our secret base")
    unknown = interact(w, w.requester)
    await modal.on_submit(unknown)
    assert "was not found on the war map" in unknown.recorder.last[1]["content"]

    set_label_value(modal.destination_field, "  ")
    empty = interact(w, w.requester)
    await modal.on_submit(empty)
    assert logi.NEEDS_DESTINATION in texts(empty)

    w.store.missing.add(ping_id)
    set_label_value(modal.destination_field, "Our secret base")
    await modal.on_submit(interact(w, w.requester))
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["ping_message_id"] is None

    admin_panel, _ = await open_panel(w, run, w.admin)
    w.admin.guild_permissions = discord.Permissions()
    refused = await click(w, admin_panel, "Edit", w.admin)
    assert refused.response.modal is None and "Only the requester or an admin" in texts(refused)
    w.admin.guild_permissions = discord.Permissions(administrator=True)
    admin_modal = (await click(w, admin_panel, "Edit", w.admin)).response.modal
    w.admin.guild_permissions = discord.Permissions()
    set_label_value(admin_modal.title_field, "Hijacked")
    sneaky = interact(w, w.admin)
    await admin_modal.on_submit(sneaky)
    assert "Only the requester or an admin" in texts(sneaky)

    await logi.deliver(w.bot, await logi.fetch(w.bot, w.guild.id, run["id"]), w.requester.id)
    late = interact(w, w.requester)
    await modal.on_submit(late)
    assert "can no longer be changed" in texts(late)
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["title"] == "Bmats and shells"


async def test_priority_select(world):
    w = world
    await use_board(w)
    run = await make_run(w, ping=True)
    rival_panel, _ = await open_panel(w, run, w.rival)
    assert not [item for item in rival_panel.children if isinstance(item, discord.ui.Select)]
    panel, _ = await open_panel(w, run, w.requester)
    select = find_select(panel)
    assert [o.value for o in select.options] == ["High", "Medium", "Low"]
    assert [o.default for o in select.options] == [False, True, False]
    select_values(select, ["High"])
    changed = interact(w, w.requester)
    await select.callback(changed)
    assert changed.recorder.last[1]["content"] == "Priority set to **High**."
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["priority"] == "High"
    assert "[High]" in w.store.message(run["ping_message_id"]).edits[-1]["content"]
    select = find_select(panel)
    select_values(select, ["High"])
    same = interact(w, w.requester)
    await select.callback(same)
    assert "already High" in same.recorder.last[1]["content"]
    select = find_select(panel)
    select_values(select, ["Bogus"])
    bogus = interact(w, w.requester)
    await select.callback(bogus)
    assert "Pick one of the priorities" in texts(bogus)
    await w.bot.perms.set_roles(w.guild.id, "logi", set())
    w.requester.roles = [w.guild.default_role]
    select_values(select, ["Low"])
    creator_without_role = interact(w, w.requester)
    await select.callback(creator_without_role)
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["priority"] == "Low"


async def test_cancel_with_confirm(world):
    w = world
    await use_board(w)
    run = await make_run(w, ping=True)
    panel, _ = await open_panel(w, run, w.requester)
    asked = await click(w, panel, "Cancel Run", w.requester)
    kind, payload = asked.recorder.last
    assert kind == "edit" and payload["embed"] is None
    confirm = payload["view"]
    assert isinstance(confirm, ConfirmView)
    assert labels(confirm) == ["Cancel Run", "Keep Run"]
    assert "Cancel the logi run **Bmats for the BB**" in payload["content"]
    kept = await click(w, confirm, "Keep Run", w.requester)
    assert kept.recorder.last[1]["view"] is panel
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["status"] == "open"

    asked = await click(w, panel, "Cancel Run", w.requester)
    confirm = asked.recorder.last[1]["view"]
    done = await click(w, confirm, "Cancel Run", w.requester)
    kind, payload = done.recorder.last
    assert kind == "edit" and payload["view"] is panel and "Cancelled **Bmats for the BB**" in payload["content"]
    assert labels(panel) == ["Refresh"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert (current["status"], current["cancelled_by"]) == ("cancelled", w.requester.id) and current["cancelled_at"]
    assert run["ping_message_id"] in w.store.deleted
    w.requester.send.assert_not_awaited()

    claimed = await make_run(w, title="Claimed then cancelled")
    claimed = await logi.claim(w.bot, claimed, w.driver.id)
    admin_panel, _ = await open_panel(w, claimed, w.admin)
    confirm = (await click(w, admin_panel, "Cancel Run", w.admin)).recorder.last[1]["view"]
    w.admin.guild_permissions = discord.Permissions()
    refused = await click(w, confirm, "Cancel Run", w.admin)
    assert "Only the requester or an admin" in refused.recorder.last[1]["content"]
    assert (await logi.fetch(w.bot, w.guild.id, claimed["id"]))["status"] == "claimed"
    assert labels(admin_panel) == ["Refresh"]
    w.admin.guild_permissions = discord.Permissions(administrator=True)
    await click(w, admin_panel, "Refresh", w.admin)
    confirm = (await click(w, admin_panel, "Cancel Run", w.admin)).recorder.last[1]["view"]
    await logi.deliver(w.bot, claimed, w.driver.id)
    raced = await click(w, confirm, "Cancel Run", w.admin)
    assert "already delivered" in raced.recorder.last[1]["content"]

    after = interact(w, w.requester)
    await find_button(panel, "Refresh").callback(after)
    assert after.recorder.last[1]["embed"].title.startswith("[Cancelled] ")


async def test_creator_without_role_keeps_own_run_but_cannot_claim(world):
    w = world
    run = await make_run(w)
    await w.bot.perms.set_roles(w.guild.id, "logi", set())
    panel, _ = await open_panel(w, run, w.requester)
    assert labels(panel) == ["Claim", "Mark Delivered", "Edit", "Cancel Run", "Refresh"]
    refused = await click(w, panel, "Claim", w.requester)
    assert refused.recorder.last[0] == "send" and "permission to claim" in texts(refused)
    rival = interact(w, w.rival)
    await logi.open_panel(w.bot, rival, run["id"])
    assert "permission" in texts(rival)


async def test_panel_rechecks_access_and_handles_missing_run(world):
    w = world
    run = await make_run(w)
    panel, _ = await open_panel(w, run, w.rival)
    await w.bot.perms.set_roles(w.guild.id, "logi", set())
    blocked = await click(w, panel, "Claim", w.rival)
    assert blocked.recorder.last[0] == "send" and "permission" in texts(blocked)
    refresh = await click(w, panel, "Refresh", w.rival)
    assert "permission" in texts(refresh)
    await w.bot.db.execute("DELETE FROM logi_runs WHERE id = ?", (run["id"],))
    gone = await click(w, panel, "Refresh", w.rival)
    assert gone.recorder.last == ("edit", {"content": "This logi run no longer exists.", "embed": None, "view": None})
    missing = interact(w, w.rival)
    await logi.open_panel(w.bot, missing, run["id"])
    assert "no longer exists" in texts(missing)


async def test_board_render_groups_by_priority_then_age(world):
    w = world
    now = timeutil.now()
    low = await set_times(w, await make_run(w, title="Low old", priority="Low"), created_at=now - 5000)
    high_new = await set_times(w, await make_run(w, title="High new", priority="High"), created_at=now - 100)
    high_old = await set_times(w, await make_run(w, title="High old", priority="High", pickup_hex="", pickup_region=""), created_at=now - 9000)
    medium = await set_times(w, await make_run(w, title="Medium", priority="Medium"), created_at=now - 200)
    claimed_medium = await logi.claim(w.bot, await make_run(w, title="Claimed medium"), w.driver.id)
    claimed_high = await logi.claim(w.bot, await make_run(w, title="Claimed high", priority="High"), w.rival.id)
    claimed_medium = await set_times(w, claimed_medium, claimed_at=now - 50)
    delivered = await make_run(w, title="Delivered one")
    await logi.deliver(w.bot, delivered, w.driver.id)
    cancelled = await make_run(w, title="Cancelled one")
    await logi.cancel(w.bot, cancelled, w.requester.id)

    render = await w.bot.boards.providers["logi"].render(w.guild)
    assert [(b.action, b.label) for b in render.buttons] == [("request", "Request Run"), ("history", "History")]
    assert render.buttons[0].style == discord.ButtonStyle.success
    description = "\n".join(e.description for e in render.embeds)
    lines = description.split("\n")
    titles = [line for line in lines if line.startswith("**")]
    order = [
        "**High priority** (2)",
        "**High old**",
        "**High new**",
        "**Medium priority** (1)",
        "**Medium**",
        "**Low priority** (1)",
        "**Low old**",
        "**Claimed** (2)",
        "**Claimed high**",
        "**Claimed medium**",
    ]
    assert len(titles) == len(order)
    positions = [description.index(prefix) for prefix in order]
    assert positions == sorted(positions)
    assert "Delivered one" not in description and "Cancelled one" not in description
    high_old_line = next(line for line in lines if line.startswith("**High old**"))
    assert "to Pan's Villa, Piper's Enclave" in high_old_line and f"<t:{now - 9000}:R>" in high_old_line
    medium_line = next(line for line in lines if line.startswith("**Medium**"))
    assert "Syrinx Pass, Piper's Enclave -> Pan's Villa, Piper's Enclave" in medium_line
    claimed_line = next(line for line in lines if line.startswith("**Claimed medium**"))
    assert f"<@{w.driver.id}> since <t:{now - 50}:R>" in claimed_line
    assert render.embeds[0].title == "Logi Runs"
    assert render.embeds[-1].footer.text.startswith("4 open, 2 claimed")
    assert [o.value for o in render.options] == [
        high_old["id"],
        high_new["id"],
        medium["id"],
        low["id"],
        claimed_high["id"],
        claimed_medium["id"],
    ]
    assert render.options[0].description.startswith("Open | High | to ")
    assert render.options[-1].description.startswith("Claimed | Medium")

    board = w.bot.boards.providers["logi"]
    denied = interact(w, w.outsider)
    await board.on_pick(denied, high_old["id"])
    assert "permission" in texts(denied)
    allowed = interact(w, w.driver)
    await board.on_pick(allowed, high_old["id"])
    assert isinstance(allowed.recorder.last[1]["view"], logi.RunPanel)
    unknown = interact(w, w.driver)
    await board.on_button(unknown, "nonsense")
    assert "no longer supported" in texts(unknown)


async def test_empty_board(world):
    w = world
    render = await w.bot.boards.providers["logi"].render(w.guild)
    assert "No open logi runs" in render.embeds[0].description
    assert render.options == []
    assert render.embeds[0].footer.text.startswith("0 open, 0 claimed")


async def test_worst_case_board_and_panel_fit_discord_limits(world):
    w = world
    for index in range(60):
        await make_run(
            w,
            title=f"Run {index:02d} *_" + "t" * 70,
            cargo="*_" * 500,
            pickup_hex="",
            pickup_region="p" * 100,
            dest_hex="h" * 60,
            dest_region="r" * 100,
            priority=["High", "Medium", "Low"][index % 3],
            notes="*_" * 250,
        )
    render = await w.bot.boards.providers["logi"].render(w.guild)
    assert len(render.options) == 25
    assert sum(len(e) for e in render.embeds) <= EMBED_TOTAL_LIMIT
    assert all(len(e.description or "") <= EMBED_DESCRIPTION_LIMIT for e in render.embeds)
    assert len(render.embeds) <= 10
    assert all(len(o.label) <= 100 and len(o.description or "") <= 100 for o in render.options)
    assert "not shown" in render.embeds[-1].footer.text
    run = (await logi.list_active(w.bot, w.guild.id))[0]
    assert_embed_limits(logi.card_embed(run))
    assert_embed_limits(logi.ping_embed(run))
    assert len(logi.ping_content(run, w.logistics)) <= 2000
    panel, payload = await open_panel(w, run, w.requester)
    assert_view_limits(panel)
    assert_embed_limits(payload["embed"])


async def test_history(world):
    w = world
    now = timeutil.now()
    recent = await logi.deliver(w.bot, await make_run(w, title="Recent delivery"), w.driver.id)
    await set_times(w, recent, delivered_at=now - 2 * 86400)
    cancelled = await logi.cancel(w.bot, await make_run(w, title="Recent cancel"), w.requester.id)
    await set_times(w, cancelled, cancelled_at=now - 86400)
    old = await logi.deliver(w.bot, await make_run(w, title="Ancient delivery"), w.driver.id)
    await set_times(w, old, delivered_at=now - 10 * 86400, updated_at=now - 10 * 86400)
    await make_run(w, title="Still open")

    board = w.bot.boards.providers["logi"]
    shown = interact(w, w.driver)
    await board.on_button(shown, "history")
    kind, payload = shown.recorder.last
    assert kind == "send" and payload["ephemeral"]
    embed = payload["embed"]
    assert embed.title == "Logi Run History"
    assert "Recent delivery" in embed.description and "Recent cancel" in embed.description
    assert "Ancient delivery" not in embed.description and "Still open" not in embed.description
    assert embed.description.index("Recent cancel") < embed.description.index("Recent delivery")
    assert f"Delivered <t:{now - 2 * 86400}:R> by <@{w.driver.id}>" in embed.description
    assert f"Cancelled <t:{now - 86400}:R> by <@{w.requester.id}>" in embed.description
    assert "1 delivered, 1 cancelled" in embed.footer.text

    denied = interact(w, w.outsider)
    await board.on_button(denied, "history")
    assert "permission" in texts(denied)

    cog = w.bot.get_cog("Logi")
    for index in range(40):
        assert await logi.deliver(w.bot, await make_run(w, title=f"Bulk {index} " + "b" * 70), w.driver.id) is not None
    paged = interact(w, w.driver)
    await cog.history_command.callback(cog, paged)
    payload = paged.recorder.last[1]
    paginator = payload["view"]
    assert isinstance(paginator, EmbedPaginator) and len(paginator.embeds) > 1
    assert all(len(e) <= EMBED_TOTAL_LIMIT for e in paginator.embeds)


async def test_history_empty(world):
    w = world
    cog = w.bot.get_cog("Logi")
    shown = interact(w, w.driver)
    await cog.history_command.callback(cog, shown)
    assert "No logi runs were delivered or cancelled in the last 7 days." in shown.recorder.last[1]["embed"].description


async def test_list_command(world):
    w = world
    cog = w.bot.get_cog("Logi")
    empty = interact(w, w.driver)
    await cog.list_command.callback(cog, empty)
    assert "no open or claimed logi runs" in texts(empty)
    denied = interact(w, w.outsider)
    await cog.list_command.callback(cog, denied)
    assert "permission" in texts(denied)

    low = await make_run(w, title="Low", priority="Low")
    claimed = await logi.claim(w.bot, await make_run(w, title="Taken", priority="High"), w.rival.id)
    high = await make_run(w, title="Urgent", priority="High")
    finished = await logi.deliver(w.bot, await make_run(w, title="Finished"), w.driver.id)
    listing = interact(w, w.driver)
    await cog.list_command.callback(cog, listing)
    kind, payload = listing.recorder.last
    assert kind == "send" and payload["ephemeral"]
    embed = payload["embed"]
    assert "Urgent" in embed.description and "Taken" in embed.description and "Finished" not in embed.description
    assert embed.footer.text.startswith("2 open, 1 claimed")
    view = payload["view"]
    assert isinstance(view, PickerView)
    assert [o.value for o in view.options] == [high["id"], low["id"], claimed["id"]]
    select = find_select(view)
    select_values(select, [claimed["id"]])
    pick = interact(w, w.driver)
    await select.callback(pick)
    assert isinstance(pick.recorder.last[1]["view"], logi.RunPanel)
    assert finished is not None


async def test_board_command_is_admin_only(world):
    w = world
    cog = w.bot.get_cog("Logi")
    denied = interact(w, w.driver)
    await cog.board.callback(cog, denied)
    assert "admins" in texts(denied)
    w.bot.boards.post = AsyncMock()
    allowed = interact(w, w.admin)
    await cog.board.callback(cog, allowed)
    w.bot.boards.post.assert_awaited_once_with(w.guild, "logi", w.board_channel)
    content = allowed.recorder.last[1]["content"]
    assert "Logi runs board posted" in content and "No logistics role is set" in content
    await w.bot.settings.set(w.guild.id, keys.LOGISTICS_ROLE, w.logistics.id)
    again = interact(w, w.admin)
    await cog.board.callback(cog, again)
    assert "No logistics role" not in again.recorder.last[1]["content"]
    w.bot.boards.post = AsyncMock(side_effect=forbidden())
    failed = interact(w, w.admin)
    await cog.board.callback(cog, failed)
    assert "Could not post the board here" in failed.recorder.last[1]["content"]


async def test_commands_registered_with_expected_options(world):
    w = world
    group = next(c for c in w.bot.tree.get_commands() if c.name == "logi")
    assert group.guild_only
    assert sorted(c.name for c in group.commands) == ["board", "history", "list", "request"]
    request = group.get_command("request")
    assert [(p.name, p.required) for p in request.parameters] == [
        ("title", True),
        ("cargo", True),
        ("destination_hex", True),
        ("destination_town", False),
        ("pickup", False),
        ("priority", False),
        ("notes", False),
    ]
    assert [c.value for c in request.get_parameter("priority").choices] == ["High", "Medium", "Low"]
    payload = group.to_dict(w.bot.tree)
    for option in payload["options"]:
        assert len(option["description"]) <= 100
        for sub in option.get("options", []):
            assert len(sub["description"]) <= 100


def test_source_follows_house_rules():
    root = Path(__file__).resolve().parent.parent
    for relative in ("foxbot/features/logi.py", "foxbot/features/mine.py", "tests/test_logi.py"):
        source = (root / relative).read_text(encoding="utf-8")
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        assert not [t for t in tokens if t.type == tokenize.COMMENT], relative
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                assert ast.get_docstring(node) is None, f"{relative}: {getattr(node, 'name', 'module')}"
        assert not EMOJI.search(source), relative
        assert re.search(r"\bRA\b", source) is None, relative



async def test_edit_keeps_long_unlisted_locations_when_untouched(world):
    w = world
    long_hex = "Forward base hex " + "h" * 43
    long_town = "Bunker base north of the river crossing " + "t" * 60
    long_pickup = "Seaport behind the second bridge " + "p" * 67
    run = await make_run(w, dest_hex=long_hex, dest_region=long_town, pickup_hex="Piper's Enclave", pickup_region=long_pickup)
    panel, _ = await open_panel(w, run, w.requester)
    modal = (await click(w, panel, "Edit", w.requester)).response.modal
    assert_modal_limits(modal)
    fields = (modal.title_field, modal.cargo_field, modal.pickup_field, modal.destination_field, modal.notes_field)
    assert all(len(field.component.default or "") <= field.component.max_length for field in fields)
    for field in fields:
        set_label_value(field, field.component.default or "")
    set_label_value(modal.title_field, "Renamed only")
    saved = interact(w, w.requester)
    await modal.on_submit(saved)
    assert saved.recorder.last[1]["content"] == "Saved."
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert current["title"] == "Renamed only"
    assert (current["dest_hex"], current["dest_region"]) == (long_hex, long_town)
    assert (current["pickup_hex"], current["pickup_region"]) == ("Piper's Enclave", long_pickup)

    set_label_value(modal.pickup_field, "")
    cleared = interact(w, w.requester)
    await modal.on_submit(cleared)
    assert "The pickup was removed." in cleared.recorder.last[1]["content"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert (current["pickup_hex"], current["pickup_region"]) == ("", "")
    assert (current["dest_hex"], current["dest_region"]) == (long_hex, long_town)


async def test_keep_run_rechecks_access(world):
    w = world
    boss = make_member(w.guild, name="Boss", administrator=True)
    run = await make_run(w)
    panel, _ = await open_panel(w, run, boss)
    confirm = (await click(w, panel, "Cancel Run", boss)).recorder.last[1]["view"]
    boss.guild_permissions = discord.Permissions()
    kept = await click(w, confirm, "Keep Run", boss)
    kind, payload = kept.recorder.last
    assert kind == "send" and payload.get("embed") is None and "permission" in payload["content"]
    assert (await logi.fetch(w.bot, w.guild.id, run["id"]))["status"] == "open"


async def test_stale_panel_does_not_undo_a_newer_claim(world):
    w = world
    run = await logi.claim(w.bot, await make_run(w), w.driver.id)
    admin_panel, _ = await open_panel(w, run, w.admin)
    released = await logi.unclaim(w.bot, run)
    await logi.claim(w.bot, released, w.rival.id)
    stale = await click(w, admin_panel, "Unclaim", w.admin)
    kind, payload = stale.recorder.last
    assert kind == "edit" and "nothing was done" in payload["content"] and f"Claimed by <@{w.rival.id}>" in payload["content"]
    current = await logi.fetch(w.bot, w.guild.id, run["id"])
    assert (current["status"], current["claimed_by"]) == ("claimed", w.rival.id)
    again = await click(w, admin_panel, "Unclaim", w.admin)
    assert f"Released the claim of <@{w.rival.id}>" in again.recorder.last[1]["content"]

    other = await make_run(w, title="Second")
    requester_panel, _ = await open_panel(w, other, w.requester)
    await logi.claim(w.bot, other, w.driver.id)
    early = await click(w, requester_panel, "Mark Delivered", w.requester)
    assert "nothing was done" in early.recorder.last[1]["content"]
    assert (await logi.fetch(w.bot, w.guild.id, other["id"]))["status"] == "claimed"
    confirmed = await click(w, requester_panel, "Mark Delivered", w.requester)
    assert "as delivered" in confirmed.recorder.last[1]["content"]
    assert (await logi.fetch(w.bot, w.guild.id, other["id"]))["status"] == "delivered"
