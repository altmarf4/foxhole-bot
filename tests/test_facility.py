import ast
import asyncio
import io
import random
import re
import tokenize
from pathlib import Path
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
from foxbot.core import settings as keys
from foxbot.core import timeutil
from foxbot.core.boards import BoardButtonItem, BoardSelectItem, build_board_view
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_FIELD_LIMIT, EMBED_TOTAL_LIMIT
from foxbot.core.ui import ConfirmView, EmbedPaginator

facility = None

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


@pytest.fixture
async def world():
    global facility
    bot = await make_bot(["foxbot.features.facility"])
    facility = bot.extensions["foxbot.features.facility"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    crew = add_role(guild, "Factory Crew")
    await bot.perms.set_roles(guild.id, "facility", {crew.id})
    w = SimpleNamespace(
        bot=bot,
        guild=guild,
        crew=crew,
        requester=make_member(guild, name="Requester", roles=[crew]),
        worker=make_member(guild, name="Worker", roles=[crew]),
        helper=make_member(guild, name="Helper", roles=[crew]),
        outsider=make_member(guild, name="Outsider"),
        admin=make_member(guild, name="Admin", administrator=True),
        channel=make_channel(guild, "factory"),
        cog=bot.get_cog("Facility"),
        board=bot.boards.providers["facility"],
    )
    yield w
    await bot.db.close()


def interact(w, member, **kwargs):
    kwargs.setdefault("channel", w.channel)
    return make_interaction(w.bot, member, **kwargs)


def texts(interaction) -> str:
    return "\n".join(str(payload["content"]) for _, payload in interaction.recorder.calls if payload.get("content"))


def labels(view) -> list[str]:
    return [item.label for item in view.children if isinstance(item, discord.ui.Button)]


def buttons(view) -> dict[str, discord.ui.Button]:
    return {item.label: item for item in view.children if isinstance(item, discord.ui.Button)}


async def add(w, item="Basic Materials", quantity=100, *, user=None, facility_name="", notes=""):
    name, code, _ = await facility.resolve_item(w.bot, w.guild.id, item)
    return await facility.create(
        w.bot,
        w.guild.id,
        item=name,
        item_code=code,
        quantity=quantity,
        facility=facility_name,
        notes=notes,
        user_id=(user or w.requester).id,
    )


async def fresh(w, entry):
    return await facility.fetch(w.bot, w.guild.id, entry["id"])


async def order(w) -> list[str]:
    return [row["id"] for row in await facility.active_rows(w.bot, w.guild.id)]


async def assert_dense(w):
    rows = await facility.active_rows(w.bot, w.guild.id)
    assert [row["position"] for row in rows] == list(range(1, len(rows) + 1))
    return rows


async def open_panel(w, entry, member):
    interaction = interact(w, member)
    await w.board.on_pick(interaction, entry["id"])
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"], interaction.recorder.calls
    assert isinstance(payload["view"], facility.FacilityPanel)
    return payload["view"], payload


async def click(w, view, label, member):
    interaction = interact(w, member)
    await find_button(view, label).callback(interaction)
    return interaction


async def press(w, button, member):
    interaction = interact(w, member)
    await button.callback(interaction)
    return interaction


async def slash_add(w, member, item, quantity, place=None, notes=None):
    interaction = interact(w, member)
    await w.cog.add.callback(w.cog, interaction, item, quantity, place, notes)
    return interaction


async def set_done_at(w, entry, done_at, user=None):
    await w.bot.db.execute(
        "UPDATE facility_queue SET status = 'done', done_by = ?, done_at = ?, position = 0 WHERE id = ?",
        ((user or w.worker).id, done_at, entry["id"]),
    )


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
    assert len(rows) <= 5 and all(count <= 5 for count in rows.values())


def assert_embed_limits(embed):
    assert len(embed) <= EMBED_TOTAL_LIMIT
    assert len(embed.description or "") <= EMBED_DESCRIPTION_LIMIT
    assert all(len(field.value) <= EMBED_FIELD_LIMIT for field in embed.fields)


def test_parse_quantity_and_match_item():
    from foxbot.core.catalog import Catalog
    from foxbot.features import facility as module

    assert [module.parse_quantity(raw) for raw in ("1,500", " 60 ", "100 000", "100000")] == [1500, 60, 100000, 100000]
    assert all(module.parse_quantity(raw) is None for raw in ("0", "100001", "-5", "abc", "", "1.5"))
    catalog = Catalog()
    assert module.match_item(catalog, "Cloth", None)[0].name == "Basic Materials"
    item, exact = module.match_item(catalog, "12.7mm", None)
    assert item.code == "MGAmmo" and exact
    item, exact = module.match_item(catalog, "basic   materials (crate)", None)
    assert item.code == "Cloth" and exact
    item, exact = module.match_item(catalog, "mortar shells", None)
    assert item.code == "MortarAmmo" and not exact
    item, exact = module.match_item(catalog, "loughcaster", "warden")
    assert item.code == "RifleW" and not exact
    assert module.match_item(catalog, "loughcaster", "colonial") == (None, False)
    assert module.match_item(catalog, "bmats", None) == (None, False)
    for partial_word in ("tank", "Tanks", "Bas"):
        assert module.match_item(catalog, partial_word, None) == (None, False), partial_word
    item, exact = module.match_item(catalog, "Basic", None)
    assert item.code == "Cloth" and not exact
    item, exact = module.match_item(catalog, "Bandage", None)
    assert item.name == "Bandages" and not exact
    assert module.starts_with_words("no2", "No.2 Loughcaster")
    assert not module.starts_with_words("tank", "Tankman\u2019s Coveralls")
    assert module.match_item(catalog, "ab", None) == (None, False)
    assert module.match_item(catalog, "   ", None) == (None, False)


async def test_slash_add_with_catalog_choice_opens_panel(world):
    w = world
    interaction = await slash_add(w, w.requester, "Cloth", 120, "Factory - Westgate", "Crates please")
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert "Added **Basic Materials x 120** to the facility queue at **#1**." == payload["content"]
    rows = await facility.active_rows(w.bot, w.guild.id)
    assert len(rows) == 1
    row = rows[0]
    assert (row["item"], row["item_code"], row["quantity"], row["facility"], row["notes"]) == (
        "Basic Materials",
        "Cloth",
        120,
        "Factory - Westgate",
        "Crates please",
    )
    assert (row["status"], row["position"], row["requested_by"], row["worker_id"]) == ("queued", 1, w.requester.id, None)
    assert len(row["id"]) == 6
    panel = payload["view"]
    assert isinstance(panel, facility.FacilityPanel)
    assert_view_limits(panel)
    assert labels(panel) == ["Start Working", "Mark Done", "Move Up", "Move Down", "Move to Top", "Edit", "Remove", "Refresh"]
    assert all(buttons(panel)[label].disabled for label in ("Move Up", "Move Down", "Move to Top"))
    embed = payload["embed"]
    assert_embed_limits(embed)
    assert embed.title == "Basic Materials x 120"
    assert embed.description == "**Queued** at **#1** - next up"
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Quantity"] == "120" and fields["Facility"] == "Factory - Westgate" and fields["Notes"] == "Crates please"
    assert fields["Requested by"].startswith(f"<@{w.requester.id}> <t:")
    assert embed.footer.text == f"Entry {row['id']} | Only you see this panel"
    w.bot.boards.request_refresh.assert_any_call(w.guild.id, "facility")


async def test_slash_add_free_text_and_guessed_items(world):
    w = world
    free = await slash_add(w, w.requester, "  Concrete   pour  ", 5)
    assert "**Concrete pour** is not in the item catalog, so it was saved as typed." in free.recorder.last[1]["content"]
    guessed = await slash_add(w, w.requester, "loughcaster", 40)
    assert "Matched **loughcaster** to the catalog item **No.2 Loughcaster**." in guessed.recorder.last[1]["content"]
    exact = await slash_add(w, w.requester, "12.7mm", 1)
    assert exact.recorder.last[1]["content"] == "Added **12.7mm x 1** to the facility queue at **#3**."
    rows = await facility.active_rows(w.bot, w.guild.id)
    assert [(r["item"], r["item_code"], r["quantity"], r["facility"], r["notes"]) for r in rows] == [
        ("Concrete pour", None, 5, "", ""),
        ("No.2 Loughcaster", "RifleW", 40, "", ""),
        ("12.7mm", "MGAmmo", 1, "", ""),
    ]
    blank = await slash_add(w, w.requester, "   ", 5)
    assert "Name the item" in texts(blank)
    too_many = await slash_add(w, w.requester, "Salvage", 100_001)
    assert "1 to 100,000" in texts(too_many)
    assert len(await facility.active_rows(w.bot, w.guild.id)) == 3


async def test_add_autocomplete_uses_faction_and_recent_facilities(world):
    w = world
    await w.bot.settings.set(w.guild.id, keys.FACTION, "warden")
    choices = await w.cog.add_item_autocomplete(interact(w, w.requester), "lough")
    assert "RifleW" in {c.value for c in choices} and len(choices) <= 25
    await w.bot.settings.set(w.guild.id, keys.FACTION, "colonial")
    choices = await w.cog.add_item_autocomplete(interact(w, w.requester), "lough")
    assert "RifleW" not in {c.value for c in choices}
    assert all(len(c.name) <= 100 for c in await w.cog.add_item_autocomplete(interact(w, w.requester), ""))

    await add(w, facility_name="Factory - Westgate")
    await add(w, facility_name="MPF - Kings Cage")
    typed = await w.cog.add_facility_autocomplete(interact(w, w.requester), "fact")
    assert typed[0].value == "Factory - Westgate"
    assert "MPF - Kings Cage" not in {c.value for c in typed}
    assert typed[-1].value == "fact" and typed[-1].name == "Use as typed: fact"
    everything = await w.cog.add_facility_autocomplete(interact(w, w.requester), "")
    assert {everything[0].value, everything[1].value} == {"Factory - Westgate", "MPF - Kings Cage"}
    assert len(everything) == 25
    assert len({c.value for c in everything}) == len(everything)


async def test_board_add_modal_success_and_retry(world):
    w = world
    start = interact(w, w.requester)
    await w.board.on_button(start, "add")
    modal = start.response.modal
    assert isinstance(modal, facility.EntryModal) and not modal.replace_origin and modal.entry is None
    assert modal.title == "Add to Facility Queue"
    assert_modal_limits(modal)
    assert [label.text for label in modal.children] == ["Item", "Quantity", "Facility", "Notes"]
    assert [label.component.required for label in modal.children] == [True, True, False, False]
    set_label_value(modal.item_field, "Mortar Shell")
    set_label_value(modal.quantity_field, "lots")
    set_label_value(modal.facility_field, "")
    set_label_value(modal.notes_field, "")
    bad = interact(w, w.requester)
    await modal.on_submit(bad)
    kind, payload = bad.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert payload["content"].startswith("Nothing was saved:") and "1 to 100,000" in payload["content"]
    assert await facility.active_rows(w.bot, w.guild.id) == []

    retry = interact(w, w.requester)
    await find_button(payload["view"], "Fix and Try Again").callback(retry)
    again = retry.response.modal
    assert isinstance(again, facility.EntryModal) and again.replace_origin
    assert again.item_field.component.default == "Mortar Shell"
    assert again.quantity_field.component.default == "lots"
    set_label_value(again.item_field, "Mortar Shell")
    set_label_value(again.quantity_field, "1,500")
    set_label_value(again.facility_field, "  MPF -   Kings Cage ")
    set_label_value(again.notes_field, "Line one\nLine two")
    fixed = interact(w, w.requester)
    await again.on_submit(fixed)
    kind, payload = fixed.recorder.last
    assert kind == "edit"
    assert isinstance(payload["view"], facility.FacilityPanel)
    assert payload["content"] == "Added **Mortar Shell x 1,500** to the facility queue at **#1**."
    row = (await facility.active_rows(w.bot, w.guild.id))[0]
    assert (row["item"], row["item_code"], row["quantity"], row["facility"], row["notes"]) == (
        "Mortar Shell",
        "MortarAmmo",
        1500,
        "MPF - Kings Cage",
        "Line one\nLine two",
    )

    second = interact(w, w.helper)
    await w.board.on_button(second, "add")
    direct = second.response.modal
    set_label_value(direct.item_field, "   ")
    set_label_value(direct.quantity_field, "0")
    set_label_value(direct.facility_field, "")
    set_label_value(direct.notes_field, "")
    rejected = interact(w, w.helper)
    await direct.on_submit(rejected)
    assert "Name the item" in texts(rejected) and "whole number" in texts(rejected)
    set_label_value(direct.item_field, "bmats")
    set_label_value(direct.quantity_field, "100000")
    accepted = interact(w, w.helper)
    await direct.on_submit(accepted)
    kind, payload = accepted.recorder.last
    assert kind == "send" and payload["ephemeral"] and isinstance(payload["view"], facility.FacilityPanel)
    assert "#2" in payload["content"] and "not in the item catalog" in payload["content"]


async def test_outsider_is_denied_everywhere(world):
    w = world
    denied = await slash_add(w, w.outsider, "Salvage", 10)
    assert "permission" in texts(denied)
    assert await facility.active_rows(w.bot, w.guild.id) == []
    entry = await add(w)
    for action in ("add", "done"):
        interaction = interact(w, w.outsider)
        await w.board.on_button(interaction, action)
        assert "permission" in texts(interaction) and interaction.response.modal is None
    cleared = interact(w, w.outsider)
    await w.board.on_button(cleared, "clear")
    assert "admins" in texts(cleared)
    picked = interact(w, w.outsider)
    await w.board.on_pick(picked, entry["id"])
    assert "permission" in texts(picked)
    listing = interact(w, w.outsider)
    await w.cog.list_command.callback(w.cog, listing)
    assert "permission" in texts(listing)

    outsider_panel = facility.FacilityPanel(w.bot, w.guild.id, entry["id"], w.outsider.id)
    await outsider_panel.send(interact(w, w.outsider))
    for label in ("Start Working", "Mark Done", "Move to Top", "Refresh"):
        attempt = await click(w, outsider_panel, label, w.outsider)
        assert "permission" in texts(attempt)
    assert (await fresh(w, entry))["status"] == "queued"
    unknown = interact(w, w.helper)
    await w.board.on_button(unknown, "explode")
    assert "no longer supported" in texts(unknown)


async def test_permission_rechecked_on_every_use(world):
    w = world
    entry = await add(w)
    panel, _ = await open_panel(w, entry, w.helper)
    start = interact(w, w.helper)
    await w.board.on_button(start, "add")
    modal = start.response.modal
    set_label_value(modal.item_field, "Salvage")
    set_label_value(modal.quantity_field, "5")
    set_label_value(modal.facility_field, "")
    set_label_value(modal.notes_field, "")
    retry_source = interact(w, w.helper)
    set_label_value(modal.quantity_field, "nope")
    await modal.on_submit(retry_source)
    retry_view = retry_source.recorder.last[1]["view"]
    set_label_value(modal.quantity_field, "5")

    await w.bot.perms.set_roles(w.guild.id, "facility", set())
    for label in ("Start Working", "Mark Done", "Move Down", "Refresh"):
        attempt = await click(w, panel, label, w.helper)
        assert "permission" in texts(attempt)
    submit = interact(w, w.helper)
    await modal.on_submit(submit)
    assert "permission" in texts(submit)
    retry = interact(w, w.helper)
    await find_button(retry_view, "Fix and Try Again").callback(retry)
    assert "permission" in texts(retry) and retry.response.modal is None
    assert [row["id"] for row in await facility.active_rows(w.bot, w.guild.id)] == [entry["id"]]
    assert (await fresh(w, entry))["status"] == "queued"

    admin_panel, _ = await open_panel(w, entry, w.admin)
    started = await click(w, admin_panel, "Start Working", w.admin)
    assert "You are working on" in started.recorder.last[1]["content"]

    await w.bot.perms.set_roles(w.guild.id, "facility", {w.guild.id})
    everyone = await slash_add(w, w.outsider, "Salvage", 3)
    assert "Added" in everyone.recorder.last[1]["content"]


async def test_start_stop_and_finish_rules(world):
    w = world
    entry = await add(w, user=w.requester)
    helper_panel, _ = await open_panel(w, entry, w.helper)
    stale = buttons(helper_panel)
    worker_panel, _ = await open_panel(w, entry, w.worker)
    started = await click(w, worker_panel, "Start Working", w.worker)
    kind, payload = started.recorder.last
    assert kind == "edit" and payload["content"].startswith("You are working on **Basic Materials x 100** now.")
    row = await fresh(w, entry)
    assert (row["status"], row["worker_id"], row["position"]) == ("in_progress", w.worker.id, 1)
    assert row["started_at"] is not None
    assert labels(worker_panel) == ["Stop Working", "Mark Done", "Edit", "Remove", "Refresh"]
    assert payload["embed"].description.startswith(f"**In progress** - <@{w.worker.id}> since <t:")

    late = await press(w, stale["Start Working"], w.helper)
    kind, payload = late.recorder.last
    assert kind == "edit"
    assert payload["content"] == (
        f"{facility.PANEL_OUTDATED} <@{w.worker.id}> started working on this entry <t:{row['started_at']}:R>."
    )
    assert labels(helper_panel) == ["Refresh"]
    assert (await fresh(w, entry))["worker_id"] == w.worker.id
    finish = await press(w, stale["Mark Done"], w.helper)
    assert "Only the person working on this entry, the requester or an admin" in texts(finish)
    assert finish.recorder.last[0] == "send"
    assert (await fresh(w, entry))["status"] == "in_progress"

    stopper = facility.FacilityPanel(w.bot, w.guild.id, entry["id"], w.helper.id)
    blocked = interact(w, w.helper)
    await stopper._stop(blocked)
    assert "Only the person working on this entry or an admin" in texts(blocked)

    requester_panel, _ = await open_panel(w, entry, w.requester)
    assert labels(requester_panel) == ["Mark Done", "Edit", "Remove", "Refresh"]

    admin_panel, _ = await open_panel(w, entry, w.admin)
    assert labels(admin_panel) == ["Stop Working", "Mark Done", "Edit", "Remove", "Refresh"]
    stopped = await click(w, admin_panel, "Stop Working", w.admin)
    assert f"Stopped the work of <@{w.worker.id}>" in stopped.recorder.last[1]["content"]
    row = await fresh(w, entry)
    assert (row["status"], row["worker_id"], row["started_at"], row["position"]) == ("queued", None, None, 1)

    again = await click(w, worker_panel, "Refresh", w.worker)
    assert again.recorder.last[0] == "edit"
    assert labels(worker_panel)[0] == "Start Working"
    stale_stop = await press(w, buttons(admin_panel)["Start Working"], w.worker)
    assert "You are working on" in stale_stop.recorder.last[1]["content"]
    own_stop = await click(w, worker_panel, "Refresh", w.worker)
    assert labels(worker_panel)[0] == "Stop Working"
    own_stop = await click(w, worker_panel, "Stop Working", w.worker)
    assert "Stopped your work on **Basic Materials x 100**" in own_stop.recorder.last[1]["content"]

    done = await click(w, helper_panel, "Refresh", w.helper)
    assert labels(helper_panel)[:2] == ["Start Working", "Mark Done"]
    done = await click(w, helper_panel, "Mark Done", w.helper)
    assert "Marked **Basic Materials x 100** as done." in done.recorder.last[1]["content"]
    row = await fresh(w, entry)
    assert (row["status"], row["done_by"], row["position"]) == ("done", w.helper.id, 0)
    assert done.recorder.last[1]["embed"].description.startswith(f"**Done** by <@{w.helper.id}>")
    assert labels(helper_panel) == ["Back to Queue", "Remove", "Refresh"]
    assert await facility.active_rows(w.bot, w.guild.id) == []

    late_start = await press(w, buttons(worker_panel)["Mark Done"], w.worker)
    assert f"marked done by <@{w.helper.id}>" in late_start.recorder.last[1]["content"]


async def test_stop_keeps_position_and_requeue_goes_to_top(world):
    w = world
    a = await add(w, "Salvage", 10)
    b = await add(w, "Diesel", 20)
    c = await add(w, "Petrol", 30)
    assert await facility.start_work(w.bot, b, w.worker.id) is not None
    ordered = facility.display_order(await facility.active_rows(w.bot, w.guild.id))
    assert [row["id"] for row in ordered] == [b["id"], a["id"], c["id"]]
    assert await facility.number_of(w.bot, w.guild.id, b["id"]) == 1
    assert await facility.number_of(w.bot, w.guild.id, a["id"]) == 2
    assert await facility.stop_work(w.bot, await fresh(w, b)) is not None
    assert await order(w) == [a["id"], b["id"], c["id"]]
    await assert_dense(w)

    assert await facility.mark_done(w.bot, await fresh(w, a), w.worker.id) is not None
    assert await order(w) == [b["id"], c["id"]]
    await assert_dense(w)
    assert await facility.start_work(w.bot, await fresh(w, c), w.worker.id) is not None

    panel, _ = await open_panel(w, a, w.requester)
    assert labels(panel) == ["Back to Queue", "Remove", "Refresh"]
    helper_panel, _ = await open_panel(w, a, w.helper)
    assert labels(helper_panel) == ["Refresh"]
    blocked = interact(w, w.helper)
    await helper_panel._requeue(blocked)
    assert "Only the requester" in texts(blocked)
    back = await click(w, panel, "Back to Queue", w.requester)
    assert "is back in the queue, at the top" in back.recorder.last[1]["content"]
    row = await fresh(w, a)
    assert (row["status"], row["done_by"], row["done_at"], row["worker_id"]) == ("queued", None, None, None)
    rows = await assert_dense(w)
    ordered = facility.display_order(rows)
    assert [r["id"] for r in ordered] == [c["id"], a["id"], b["id"]]
    assert back.recorder.last[1]["embed"].description == "**Queued** at **#2** - next up"
    twice = await press(w, buttons(helper_panel)["Refresh"], w.helper)
    assert twice.recorder.last[0] == "edit"
    stale = facility.FacilityPanel(w.bot, w.guild.id, a["id"], w.requester.id)
    repeat = interact(w, w.requester)
    await stale._requeue(repeat)
    assert "waiting in the queue" in repeat.recorder.last[1]["content"]


async def test_moves_reorder_only_the_waiting_queue(world):
    w = world
    a, b, c, d = [await add(w, name, 10) for name in ("Salvage", "Diesel", "Petrol", "Components")]
    panel, payload = await open_panel(w, d, w.helper)
    state = buttons(panel)
    assert (state["Move Up"].disabled, state["Move Down"].disabled, state["Move to Top"].disabled) == (False, True, False)
    assert payload["embed"].description == "**Queued** at **#4**"

    up = await click(w, panel, "Move Up", w.helper)
    assert up.recorder.last[1]["content"] == "Moved **Components x 10** up to **#3**."
    assert await order(w) == [a["id"], b["id"], d["id"], c["id"]]
    top = await click(w, panel, "Move to Top", w.helper)
    assert top.recorder.last[1]["content"] == "Moved **Components x 10** to the top of the queue (**#1**)."
    assert await order(w) == [d["id"], a["id"], b["id"], c["id"]]
    state = buttons(panel)
    assert (state["Move Up"].disabled, state["Move Down"].disabled, state["Move to Top"].disabled) == (True, False, True)
    first = await press(w, state["Move Up"], w.helper)
    assert first.recorder.last[1]["content"] == "**Components x 10** is already first in the queue."
    down = await click(w, panel, "Move Down", w.helper)
    assert down.recorder.last[1]["content"] == "Moved **Components x 10** down to **#2**."
    assert await order(w) == [a["id"], d["id"], b["id"], c["id"]]
    await assert_dense(w)

    assert await facility.start_work(w.bot, await fresh(w, b), w.worker.id) is not None
    c_panel, payload = await open_panel(w, c, w.helper)
    assert payload["embed"].description == "**Queued** at **#4**"
    moved = await click(w, c_panel, "Move Up", w.helper)
    assert moved.recorder.last[1]["content"] == "Moved **Petrol x 10** up to **#3**."
    rows = await assert_dense(w)
    assert [(r["id"], r["position"]) for r in rows] == [(a["id"], 1), (c["id"], 2), (b["id"], 3), (d["id"], 4)]
    d_panel, _ = await open_panel(w, d, w.helper)
    await click(w, d_panel, "Move to Top", w.helper)
    rows = await assert_dense(w)
    assert [(r["id"], r["position"]) for r in rows] == [(d["id"], 1), (a["id"], 2), (b["id"], 3), (c["id"], 4)]
    last = await press(w, buttons(c_panel)["Move Down"], w.helper)
    assert last.recorder.last[1]["content"] == "**Petrol x 10** is already last in the queue."

    b_panel = facility.FacilityPanel(w.bot, w.guild.id, b["id"], w.helper.id)
    working = interact(w, w.helper)
    await b_panel._move_up(working)
    assert "Only queued entries can be moved" in working.recorder.last[1]["content"]
    assert (await fresh(w, b))["position"] == 3
    assert await facility.move(w.bot, w.guild.id, "NOPE00", facility.MOVE_UP) == "not_queued"


async def test_interleaved_operations_keep_positions_dense(world):
    w = world
    rng = random.Random(7)
    entries = [await add(w, f"Thing {index}", index + 1) for index in range(10)]
    members = [w.requester, w.worker, w.helper, w.admin]

    async def random_operation(index: int):
        entry = rng.choice(entries)
        snapshot = await fresh(w, entry)
        choice = index % 7
        if choice in (0, 1, 2):
            return await facility.move(w.bot, w.guild.id, entry["id"], rng.choice([facility.MOVE_UP, facility.MOVE_DOWN, facility.MOVE_TOP]))
        if choice == 3:
            return await add(w, f"New {index}", 5)
        if snapshot is None:
            return None
        if choice == 4:
            return await facility.remove(w.bot, snapshot)
        if choice == 5:
            return await facility.start_work(w.bot, snapshot, rng.choice(members).id)
        return await facility.mark_done(w.bot, snapshot, rng.choice(members).id)

    for _ in range(3):
        await asyncio.gather(*(random_operation(index) for index in range(40)))
        rows = await assert_dense(w)
        assert len({row["id"] for row in rows}) == len(rows)
        assert all(row["status"] in ("queued", "in_progress") for row in rows)
    done = await w.bot.db.fetchall("SELECT position FROM facility_queue WHERE status = 'done'")
    assert all(row["position"] == 0 for row in done)

    panel_entries = (await facility.active_rows(w.bot, w.guild.id))[:5]
    panels = [facility.FacilityPanel(w.bot, w.guild.id, row["id"], w.helper.id) for row in panel_entries]
    clicks = []
    for index, panel in enumerate(panels * 3):
        method = (panel._move_up, panel._move_down, panel._move_top)[index % 3]
        clicks.append(method(interact(w, w.helper)))
    await asyncio.gather(*clicks)
    await assert_dense(w)


async def test_stale_panels_after_removal(world):
    w = world
    entry = await add(w)
    panel, _ = await open_panel(w, entry, w.helper)
    assert await facility.remove(w.bot, entry)
    for label in ("Start Working", "Mark Done", "Move Up", "Refresh"):
        interaction = await press(w, buttons(panel)[label], w.helper)
        kind, payload = interaction.recorder.last
        assert kind == "edit" and payload["content"] == facility.GONE and payload["view"] is None
    gone = interact(w, w.helper)
    await w.board.on_pick(gone, entry["id"])
    assert texts(gone) == facility.GONE
    assert not await facility.remove(w.bot, entry)
    assert await facility.guarded_update(w.bot, entry, quantity=5) is None


async def test_guarded_update_rejects_stale_state(world):
    w = world
    entry = await add(w)
    assert await facility.start_work(w.bot, entry, w.worker.id) is not None
    assert await facility.start_work(w.bot, entry, w.helper.id) is None
    assert await facility.guarded_update(w.bot, entry, quantity=5) is None
    current = await fresh(w, entry)
    assert (current["worker_id"], current["quantity"]) == (w.worker.id, 100)
    assert not await facility.remove(w.bot, entry)
    assert await facility.remove(w.bot, current)


async def test_edit_modal_rules(world):
    w = world
    entry = await add(w, user=w.requester, facility_name="Factory - Westgate", notes="Old notes")
    helper_panel, _ = await open_panel(w, entry, w.helper)
    assert "Edit" not in labels(helper_panel) and "Remove" not in labels(helper_panel)
    blocked = interact(w, w.helper)
    await helper_panel._edit(blocked)
    assert "Only the requester" in texts(blocked) and blocked.response.modal is None

    panel, _ = await open_panel(w, entry, w.requester)
    opener = await click(w, panel, "Edit", w.requester)
    modal = opener.response.modal
    assert isinstance(modal, facility.EntryModal) and modal.replace_origin and modal.entry is not None
    assert modal.title == "Edit Facility Queue Entry"
    assert_modal_limits(modal)
    assert modal.item_field.component.default == "Basic Materials"
    assert modal.quantity_field.component.default == "100"
    assert modal.facility_field.component.default == "Factory - Westgate"
    assert modal.notes_field.component.default == "Old notes"
    set_label_value(modal.item_field, "12.7mm")
    set_label_value(modal.quantity_field, "40")
    set_label_value(modal.facility_field, "")
    set_label_value(modal.notes_field, "")
    saved = interact(w, w.requester)
    await modal.on_submit(saved)
    kind, payload = saved.recorder.last
    assert kind == "edit" and payload["content"] == "Saved." and isinstance(payload["view"], facility.FacilityPanel)
    row = await fresh(w, entry)
    assert (row["item"], row["item_code"], row["quantity"], row["facility"], row["notes"]) == ("12.7mm", "MGAmmo", 40, "", "")
    assert payload["embed"].title == "12.7mm x 40"

    set_label_value(modal.item_field, "bmats")
    renamed = interact(w, w.requester)
    await modal.on_submit(renamed)
    assert "Saved.\n**bmats** is not in the item catalog" in renamed.recorder.last[1]["content"]
    assert (await fresh(w, entry))["item_code"] is None

    intruder = interact(w, w.helper)
    await modal.on_submit(intruder)
    assert "Only the requester" in texts(intruder)

    await facility.start_work(w.bot, await fresh(w, entry), w.worker.id)
    worker_panel, _ = await open_panel(w, entry, w.worker)
    worker_open = await click(w, worker_panel, "Edit", w.worker)
    worker_modal = worker_open.response.modal
    set_label_value(worker_modal.quantity_field, "abc")
    bad = interact(w, w.worker)
    await worker_modal.on_submit(bad)
    assert bad.recorder.last[0] == "send" and "Fix and Try Again" in bad.recorder.last[1]["content"]
    set_label_value(worker_modal.quantity_field, "45")
    good = interact(w, w.worker)
    await worker_modal.on_submit(good)
    row = await fresh(w, entry)
    assert (row["quantity"], row["status"], row["worker_id"]) == (45, "in_progress", w.worker.id)

    await facility.mark_done(w.bot, row, w.worker.id)
    finished = interact(w, w.worker)
    await worker_modal.on_submit(finished)
    assert "already done" in texts(finished)
    done_panel = facility.FacilityPanel(w.bot, w.guild.id, entry["id"], w.requester.id)
    done_edit = interact(w, w.requester)
    await done_panel._edit(done_edit)
    assert "already done" in done_edit.recorder.last[1]["content"] and done_edit.response.modal is None

    await facility.remove(w.bot, await fresh(w, entry))
    missing = interact(w, w.requester)
    await modal.on_submit(missing)
    assert texts(missing) == facility.GONE


async def test_remove_with_confirm(world):
    w = world
    a = await add(w, "Salvage", 10, user=w.requester)
    b = await add(w, "Diesel", 20, user=w.requester)
    c = await add(w, "Petrol", 30, user=w.helper)
    helper_panel, _ = await open_panel(w, a, w.helper)
    blocked = interact(w, w.helper)
    await helper_panel._remove(blocked)
    assert "Only the requester" in texts(blocked)

    panel, _ = await open_panel(w, a, w.requester)
    asking = await click(w, panel, "Remove", w.requester)
    kind, payload = asking.recorder.last
    assert kind == "edit" and payload["embed"] is None
    assert payload["content"] == "Remove **Salvage x 10** from the facility queue? This cannot be undone."
    confirm = payload["view"]
    assert isinstance(confirm, ConfirmView)
    cancel = await click(w, confirm, "Cancel", w.requester)
    assert cancel.recorder.last[0] == "edit" and cancel.recorder.last[1]["view"] is panel
    assert await fresh(w, a) is not None

    asking = await click(w, panel, "Remove", w.requester)
    confirm = asking.recorder.last[1]["view"]
    done = await click(w, confirm, "Remove", w.requester)
    assert done.recorder.last[1]["content"] == "Removed **Salvage x 10** from the facility queue."
    assert done.recorder.last[1]["view"] is None
    assert await fresh(w, a) is None
    rows = await assert_dense(w)
    assert [r["id"] for r in rows] == [b["id"], c["id"]]

    await facility.start_work(w.bot, b, w.worker.id)
    requester_panel, _ = await open_panel(w, b, w.requester)
    asking = await click(w, requester_panel, "Remove", w.requester)
    assert f"<@{w.worker.id}> is working on it." in asking.recorder.last[1]["content"]
    confirm = asking.recorder.last[1]["view"]
    await facility.stop_work(w.bot, await fresh(w, b))
    changed = await click(w, confirm, "Remove", w.requester)
    kind, payload = changed.recorder.last
    assert kind == "edit" and payload["view"] is requester_panel
    assert payload["content"] == f"{facility.REMOVE_OUTDATED} This entry is waiting in the queue."
    assert await fresh(w, b) is not None
    asking = await click(w, requester_panel, "Remove", w.requester)
    assert asking.recorder.last[1]["content"] == "Remove **Diesel x 20** from the facility queue? This cannot be undone."
    removed = await click(w, asking.recorder.last[1]["view"], "Remove", w.requester)
    assert removed.recorder.last[1]["content"] == "Removed **Diesel x 20** from the facility queue."

    admin_panel, _ = await open_panel(w, c, w.admin)
    asking = await click(w, admin_panel, "Remove", w.admin)
    confirm = asking.recorder.last[1]["view"]
    raced = interact(w, w.admin)
    original_remove = facility.remove
    facility.remove = AsyncMock(return_value=False)
    try:
        await find_button(confirm, "Remove").callback(raced)
    finally:
        facility.remove = original_remove
    assert raced.recorder.last[1]["content"] == f"{facility.REMOVE_OUTDATED} This entry is waiting in the queue."
    assert await fresh(w, c) is not None


async def test_remove_confirm_refuses_when_someone_started_meanwhile(world):
    w = world
    entry = await add(w, user=w.requester)
    panel, _ = await open_panel(w, entry, w.requester)
    asking = await click(w, panel, "Remove", w.requester)
    assert "is working on it" not in asking.recorder.last[1]["content"]
    confirm = asking.recorder.last[1]["view"]
    started = await facility.start_work(w.bot, await fresh(w, entry), w.worker.id)
    late = await click(w, confirm, "Remove", w.requester)
    kind, payload = late.recorder.last
    assert kind == "edit" and payload["view"] is panel
    assert payload["content"] == (
        f"{facility.REMOVE_OUTDATED} <@{w.worker.id}> started working on this entry <t:{started['started_at']}:R>."
    )
    row = await fresh(w, entry)
    assert (row["status"], row["worker_id"]) == ("in_progress", w.worker.id)
    asking = await click(w, panel, "Remove", w.requester)
    assert f"<@{w.worker.id}> is working on it." in asking.recorder.last[1]["content"]
    await facility.remove(w.bot, await fresh(w, entry))
    gone = await click(w, asking.recorder.last[1]["view"], "Remove", w.requester)
    assert gone.recorder.last[1]["content"] == facility.GONE and gone.recorder.last[1]["view"] is None


async def test_stale_panel_does_not_act_on_a_changed_entry(world):
    w = world
    entry = await add(w, user=w.requester)
    await facility.start_work(w.bot, entry, w.worker.id)
    admin_panel, _ = await open_panel(w, entry, w.admin)
    requester_panel, _ = await open_panel(w, entry, w.requester)
    assert "Stop Working" in labels(admin_panel) and "Mark Done" in labels(requester_panel)
    await facility.stop_work(w.bot, await fresh(w, entry))
    restarted = await facility.start_work(w.bot, await fresh(w, entry), w.helper.id)

    stop = await click(w, admin_panel, "Stop Working", w.admin)
    kind, payload = stop.recorder.last
    assert kind == "edit" and payload["view"] is admin_panel
    assert payload["content"] == (
        f"{facility.PANEL_OUTDATED} <@{w.helper.id}> started working on this entry <t:{restarted['started_at']}:R>."
    )
    finish = await click(w, requester_panel, "Mark Done", w.requester)
    assert finish.recorder.last[1]["content"].startswith(facility.PANEL_OUTDATED)
    row = await fresh(w, entry)
    assert (row["status"], row["worker_id"]) == ("in_progress", w.helper.id)

    stopped = await click(w, admin_panel, "Stop Working", w.admin)
    assert stopped.recorder.last[1]["content"].startswith(f"Stopped the work of <@{w.helper.id}>")
    assert (await fresh(w, entry))["worker_id"] is None

    queued_panel, _ = await open_panel(w, entry, w.helper)
    await facility.mark_done(w.bot, await fresh(w, entry), w.requester.id)
    await facility.requeue(w.bot, await fresh(w, entry))
    moved = await click(w, queued_panel, "Start Working", w.helper)
    assert moved.recorder.last[1]["content"].startswith("You are working on")


async def test_board_render_sections_and_select(world):
    w = world
    a = await add(w, "Salvage", 1000, user=w.requester)
    b = await add(w, "Diesel", 20, user=w.requester, facility_name="Refinery - Westgate")
    c = await add(w, "Mortar Shell", 30, user=w.helper, facility_name="Factory - Westgate", notes="*urgent* for the\nfront")
    await facility.start_work(w.bot, b, w.worker.id)
    now = timeutil.now()
    recent = [await add(w, f"Done {index}", index + 1) for index in range(7)]
    for index, entry in enumerate(recent):
        await set_done_at(w, entry, now - 60 * (index + 1))
    old = await add(w, "Ancient", 1)
    await set_done_at(w, old, now - 2 * 86400)

    render = await w.board.render(w.guild)
    text = "\n".join(e.description for e in render.embeds)
    for embed in render.embeds:
        assert_embed_limits(embed)
    marks = [
        "**In progress** (1)",
        "`#1` **Diesel** x 20 at Refinery - Westgate",
        "**Queued** (2)",
        "`#2` **Salvage** x 1,000",
        "`#3` **Mortar Shell** x 30 at Factory - Westgate",
        "**Done in the last 24 hours** (7)",
    ]
    positions = [text.index(mark) for mark in marks]
    assert positions == sorted(positions)
    assert f"<@{w.worker.id}> since <t:" in text
    assert f"requested by <@{w.requester.id}>" in text and f"requested by <@{w.helper.id}>" in text
    assert "*\\*urgent\\* for the front*" in text
    assert f"Done 0 x 1 (<@{w.worker.id}>)" in text and "Done 4 x 5" in text and "Done 5 x 6" not in text
    assert ", and 2 more" in text and "Ancient" not in text
    assert render.embeds[-1].footer.text == "1 in progress, 2 queued, 7 done in the last 24 hours | Updates automatically"
    assert render.embeds[0].title == "Facility Queue"
    assert [o.value for o in render.options] == [b["id"], a["id"], c["id"]]
    assert [o.label for o in render.options] == ["#1 Diesel x 20", "#2 Salvage x 1,000", "#3 Mortar Shell x 30"]
    assert render.options[0].description == "In progress: Worker | Refinery - Westgate"
    assert render.options[1].description == "Queued, requested by Requester"
    assert [(button.action, button.label) for button in render.buttons] == [("add", "Add"), ("done", "Done Today"), ("clear", "Clear Done")]

    view = build_board_view("facility", render)
    assert_view_limits(view)
    assert {item.custom_id for item in view.children} == {
        "fb:facility:pick",
        "fb:facility:btn:add",
        "fb:facility:btn:done",
        "fb:facility:btn:clear",
    }


async def test_board_empty_and_done_only(world):
    w = world
    render = await w.board.render(w.guild)
    assert render.embeds[0].description == "Nothing is queued right now. Press **Add** to queue something for the facility."
    assert render.options == []
    assert render.embeds[0].footer.text.startswith("0 in progress, 0 queued, 0 done")
    entry = await add(w)
    await facility.mark_done(w.bot, entry, w.worker.id)
    render = await w.board.render(w.guild)
    lines = render.embeds[0].description.splitlines()
    assert lines == ["Nothing is queued right now.", "**Done in the last 24 hours** (1)", f"Basic Materials x 100 (<@{w.worker.id}>)"]
    assert render.options == []


async def test_board_items_route_through_the_framework(world):
    w = world
    entry = await add(w)
    interaction = interact(w, w.helper)
    button = BoardButtonItem("facility", "add")
    await button.callback(interaction)
    assert isinstance(interaction.response.modal, facility.EntryModal)
    select = BoardSelectItem("facility")
    select_values(select.item, [entry["id"]])
    picked = interact(w, w.helper)
    await select.callback(picked)
    kind, payload = picked.recorder.last
    assert kind == "send" and payload["ephemeral"] and isinstance(payload["view"], facility.FacilityPanel)


async def test_done_today_and_clear_done(world):
    w = world
    now = timeutil.now()
    active = await add(w, "Salvage", 10)
    recent = await add(w, "Diesel", 20)
    newer = await add(w, "Petrol", 30)
    old = await add(w, "Ancient", 1)
    await set_done_at(w, recent, now - 3600)
    await set_done_at(w, newer, now - 60, user=w.helper)
    await set_done_at(w, old, now - 3 * 86400)

    listing = interact(w, w.helper)
    await w.board.on_button(listing, "done")
    kind, payload = listing.recorder.last
    assert kind == "send" and payload["ephemeral"]
    embed = payload["embed"]
    assert embed.title == "Done in the Last 24 Hours"
    lines = embed.description.splitlines()
    assert len(lines) == 2 and "Petrol" in lines[0] and "Diesel" in lines[1]
    assert f"done by <@{w.helper.id}>" in lines[0] and f"requested by <@{w.requester.id}>" in lines[0]
    assert embed.footer.text == "2 entries finished, newest first"

    denied = interact(w, w.helper)
    await w.board.on_button(denied, "clear")
    assert "admins" in texts(denied)
    asking = interact(w, w.admin)
    await w.board.on_button(asking, "clear")
    kind, payload = asking.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert payload["content"].startswith("Clear 3 done entries from the facility queue?")
    confirm = payload["view"]
    late = await add(w, "Late", 2)
    await set_done_at(w, late, now + 3600)
    cleared = await click(w, confirm, "Clear Done", w.admin)
    assert cleared.recorder.last[1]["content"] == "Cleared 3 done entries from the facility queue."
    remaining = await w.bot.db.fetchall("SELECT id FROM facility_queue ORDER BY id")
    assert {row["id"] for row in remaining} == {active["id"], late["id"]}
    w.bot.boards.request_refresh.assert_any_call(w.guild.id, "facility")

    await w.bot.db.execute("DELETE FROM facility_queue WHERE id = ?", (late["id"],))
    nothing = interact(w, w.admin)
    await w.board.on_button(nothing, "clear")
    assert texts(nothing) == "There are no done entries to clear."
    empty = interact(w, w.helper)
    await w.board.on_button(empty, "done")
    assert empty.recorder.last[1]["embed"].description == "Nothing was finished in the last 24 hours."


async def test_clear_done_rechecks_admin_and_cancel(world):
    w = world
    officer_role = add_role(w.guild, "Officer")
    officer = make_member(w.guild, name="Officer", roles=[officer_role])
    await w.bot.settings.set(w.guild.id, keys.ADMIN_ROLE, officer_role.id)
    entry = await add(w)
    await facility.mark_done(w.bot, entry, w.worker.id)
    asking = interact(w, officer)
    await w.board.on_button(asking, "clear")
    confirm = asking.recorder.last[1]["view"]
    cancel = await click(w, confirm, "Cancel", officer)
    assert cancel.recorder.last[1]["content"] == "Cancelled."
    asking = interact(w, officer)
    await w.board.on_button(asking, "clear")
    confirm = asking.recorder.last[1]["view"]
    await w.bot.settings.reset(w.guild.id, keys.ADMIN_ROLE)
    denied = await click(w, confirm, "Clear Done", officer)
    assert "Only bot admins" in texts(denied)
    assert await fresh(w, entry) is not None
    await w.bot.db.execute("DELETE FROM facility_queue")
    gone = await click(w, asking.recorder.last[1]["view"], "Clear Done", w.admin)
    assert gone.recorder.last[1]["content"] == "There were no done entries left to clear."


async def test_list_command_and_picker(world):
    w = world
    empty = interact(w, w.helper)
    await w.cog.list_command.callback(w.cog, empty)
    assert "The facility queue is empty" in texts(empty)

    a = await add(w, "Salvage", 10, user=w.requester)
    b = await add(w, "Diesel", 20, user=w.requester)
    c = await add(w, "Petrol", 30, user=w.requester)
    await facility.start_work(w.bot, b, w.worker.id)
    await facility.mark_done(w.bot, await fresh(w, c), w.worker.id)
    listing = interact(w, w.helper)
    await w.cog.list_command.callback(w.cog, listing)
    kind, payload = listing.recorder.last
    assert kind == "send" and payload["ephemeral"]
    embed = payload["embed"]
    assert embed.title == "Facility Queue"
    assert embed.description.index("Diesel") < embed.description.index("Salvage") < embed.description.index("Petrol")
    assert embed.footer.text == "1 in progress, 1 queued, 1 done in the last 24 hours | Pick an entry below to open its panel"
    picker = payload["view"]
    assert_view_limits(picker)
    select = find_select(picker)
    assert [o.value for o in select.options] == [b["id"], a["id"], c["id"]]
    assert [o.label for o in select.options] == ["#1 Diesel x 20", "#2 Salvage x 10", "Done: Petrol x 30"]
    assert select.options[2].description == "Done by Worker"
    select_values(select, [c["id"]])
    pick = interact(w, w.requester)
    await select.callback(pick)
    kind, payload = pick.recorder.last
    assert kind == "send" and isinstance(payload["view"], facility.FacilityPanel)
    assert labels(payload["view"]) == ["Back to Queue", "Remove", "Refresh"]


async def test_board_command_is_admin_only(world):
    w = world
    denied = interact(w, w.helper)
    await w.cog.board.callback(w.cog, denied)
    assert "admins" in texts(denied)
    w.bot.boards.post = AsyncMock()
    allowed = interact(w, w.admin)
    await w.cog.board.callback(w.cog, allowed)
    w.bot.boards.post.assert_awaited_once_with(w.guild, "facility", w.channel)
    assert allowed.recorder.last[1]["content"] == "Facility queue board posted."
    w.bot.boards.post = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access"))
    failed = interact(w, w.admin)
    await w.cog.board.callback(w.cog, failed)
    assert "Could not post the board here" in failed.recorder.last[1]["content"]


async def test_worst_case_texts_fit_discord_limits(world):
    w = world
    for index in range(60):
        entry = await facility.create(
            w.bot,
            w.guild.id,
            item=f"I{index:02d} " + "*_" * 38,
            item_code=None,
            quantity=100_000,
            facility="F*" * 50,
            notes="N_" * 250,
            user_id=900_000_000_000_000_000 + index,
        )
        if index % 4 == 0:
            await facility.start_work(w.bot, entry, 800_000_000_000_000_000 + index)
        if index % 9 == 0:
            await facility.mark_done(w.bot, await fresh(w, entry), 700_000_000_000_000_000 + index)
    render = await w.board.render(w.guild)
    assert sum(len(e) for e in render.embeds) <= EMBED_TOTAL_LIMIT
    for embed in render.embeds:
        assert_embed_limits(embed)
    assert "not shown" in render.embeds[-1].footer.text
    assert len(render.options) == 25
    assert_view_limits(build_board_view("facility", render))

    entry = (await facility.active_rows(w.bot, w.guild.id))[0]
    panel, payload = await open_panel(w, entry, w.admin)
    assert_embed_limits(payload["embed"])
    assert_view_limits(panel)
    listing = interact(w, w.admin)
    await w.cog.list_command.callback(w.cog, listing)
    assert_embed_limits(listing.recorder.last[1]["embed"])
    assert_view_limits(listing.recorder.last[1]["view"])
    done = interact(w, w.admin)
    await w.board.on_button(done, "done")
    payload = done.recorder.last[1]
    pages = payload["view"].embeds if isinstance(payload.get("view"), EmbedPaginator) else [payload["embed"]]
    for embed in pages:
        assert_embed_limits(embed)


async def test_command_tree_shape(world):
    w = world
    payload = {command["name"]: command for command in w.bot.command_payload()}
    group = payload["facility"]
    assert group["contexts"] == [0] or group.get("dm_permission") is False
    subcommands = {option["name"]: option for option in group["options"]}
    assert set(subcommands) == {"add", "list", "board"}
    add_options = {option["name"]: option for option in subcommands["add"]["options"]}
    assert list(add_options) == ["item", "quantity", "facility", "notes"]
    assert add_options["item"]["autocomplete"] and add_options["item"]["required"]
    assert add_options["item"]["max_length"] == facility.EntryModal(w.bot, w.guild.id).item_field.component.max_length == 80
    assert add_options["quantity"]["min_value"] == 1 and add_options["quantity"]["max_value"] == 100_000
    assert add_options["facility"]["autocomplete"] and not add_options["facility"].get("required", False)
    assert not add_options["notes"].get("required", False) and add_options["notes"]["max_length"] == 500
    for option in [group, *subcommands.values(), *add_options.values()]:
        assert 1 <= len(option["description"]) <= 100
    assert "facility" in w.bot.boards.providers and w.bot.boards.providers["facility"].title == "Facility Queue"


def test_source_follows_house_rules():
    path = Path(__file__).resolve().parent.parent / "foxbot" / "features" / "facility.py"
    source = path.read_text(encoding="utf-8")
    assert not EMOJI.search(source)
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    assert not [token for token in tokens if token.type == tokenize.COMMENT]
    tree = ast.parse(source)
    for node in [tree, *ast.walk(tree)]:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            assert ast.get_docstring(node) is None, getattr(node, "name", "module")
    assert re.search(r"\bRA\b", source) is None


async def test_remove_cancel_rechecks_permission(world):
    w = world
    entry = await add(w, user=w.requester)
    panel, _ = await open_panel(w, entry, w.requester)
    asking = await click(w, panel, "Remove", w.requester)
    confirm = asking.recorder.last[1]["view"]
    await w.bot.perms.set_roles(w.guild.id, "facility", set())
    cancel = await click(w, confirm, "Cancel", w.requester)
    assert "permission" in texts(cancel) and cancel.recorder.last[0] == "send"
    blocked = await click(w, confirm, "Remove", w.requester)
    assert "permission" in texts(blocked)
    assert await fresh(w, entry) is not None
