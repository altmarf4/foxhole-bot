import asyncio
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
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_FIELD_LIMIT, EMBED_TOTAL_LIMIT

orders = None


def not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Message")


class CardStore:
    def __init__(self):
        self.messages: dict[int, MagicMock] = {}
        self.missing: set[int] = set()
        self.deleted: list[int] = []
        self.gate: asyncio.Event | None = None

    def channel(self, channel_id, **kwargs):
        partial = MagicMock()
        partial.id = channel_id
        partial.get_partial_message.side_effect = lambda message_id: self.message(message_id)
        return partial

    def message(self, message_id: int) -> MagicMock:
        if message_id not in self.messages:
            message = MagicMock()
            message.id = message_id
            message.edits = []

            async def do_edit(**kwargs):
                if self.gate is not None:
                    await self.gate.wait()
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

    def last_embed(self, message_id: int) -> discord.Embed:
        return self.message(message_id).edits[-1]["embed"]


@pytest.fixture
async def world():
    global orders
    bot = await make_bot(["foxbot.features.orders"])
    orders = bot.extensions["foxbot.features.orders"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    logi = add_role(guild, "Logi")
    await bot.perms.set_roles(guild.id, "orders", {logi.id})
    cards = CardStore()
    bot.get_partial_messageable = MagicMock(side_effect=cards.channel)
    w = SimpleNamespace(
        bot=bot,
        guild=guild,
        creator=make_member(guild, name="Creator", roles=[logi]),
        helper=make_member(guild, name="Helper", roles=[logi]),
        outsider=make_member(guild, name="Outsider"),
        admin=make_member(guild, name="Admin", administrator=True),
        channel=make_channel(guild, "logi"),
        cards=cards,
    )
    yield w
    await bot.db.close()


def interact(w, member, **kwargs):
    kwargs.setdefault("channel", w.channel)
    return make_interaction(w.bot, member, **kwargs)


def texts(interaction) -> str:
    parts = []
    for _, payload in interaction.recorder.calls:
        if payload.get("content"):
            parts.append(str(payload["content"]))
    return "\n".join(parts)


async def make_order(w, *, lines="50 Basic Materials\n20 Mortar Shell", title="Front ammo", order_type="Production", user=None, post=True, notes=""):
    parsed = orders.parse_lines(w.bot.catalog, lines, None)
    assert not parsed.errors
    order = await orders.create(
        w.bot,
        w.guild.id,
        order_type=order_type,
        title=title,
        hex_name="",
        region="",
        notes=notes,
        lines=parsed.lines,
        user_id=(user or w.creator).id,
    )
    if post:
        assert await orders.post_card(w.bot, order, w.channel) is not None
    return await orders.fetch(w.bot, w.guild.id, order["id"])


def card_view(w) -> discord.ui.View:
    return w.channel.sent[-1]["view"]


async def press(w, item, member, values=None, **kwargs):
    interaction = interact(w, member, **kwargs)
    for cls in (orders.OrderButtonItem, orders.OrderLineSelectItem):
        match = cls.__discord_ui_compiled_template__.fullmatch(item.custom_id)
        if match:
            dynamic = await cls.from_custom_id(interaction, item, match)
            if values is not None:
                select_values(dynamic.item, values)
            await dynamic.callback(interaction)
            return interaction
    raise AssertionError(f"No dynamic item handles {item.custom_id}")


async def open_panel(w, order, member, line_index=0):
    lines = await orders.fetch_lines(w.bot, order["id"])
    select = find_select(card_view(w))
    interaction = await press(w, select, member, values=[str(lines[line_index]["id"])])
    kind, payload = interaction.recorder.last
    assert kind == "followup" and payload["ephemeral"]
    return payload["view"], interaction


async def click(w, view, label, member):
    interaction = interact(w, member)
    await find_button(view, label).callback(interaction)
    return interaction


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


def test_parse_lines_formats_and_matching():
    from foxbot.core.catalog import Catalog

    module = __import__("foxbot.features.orders", fromlist=["parse_lines"])
    catalog = Catalog()
    result = module.parse_lines(
        catalog,
        "50 Basic Materials\n20 x Mortar Shell\n- 10 Soldier Supplies\nGas Mask: 30\n12.7mm 100\n"
        "1,000 Salvage\n5 cloth\n3 bmats\n7 loughcaster\n2 mortar shells\n\n",
        "warden",
    )
    assert not result.errors
    found = {line.item_name: (line.item_code, line.target) for line in result.lines}
    assert found["Basic Materials"] == ("Cloth", 55)
    assert found["Mortar Shell"] == ("MortarAmmo", 22)
    assert found["Soldier Supplies"] == ("SoldierSupplies", 10)
    assert found["Gas Mask"] == ("GasMask", 30)
    assert found["12.7mm"] == ("MGAmmo", 100)
    assert found["Salvage"] == ("Metal", 1000)
    assert found["bmats"] == (None, 3)
    assert found["No.2 Loughcaster"] == ("RifleW", 7)
    assert result.unmatched == ["bmats"]
    assert ("loughcaster", "No.2 Loughcaster") in result.guessed
    assert set(result.merged) == {"Basic Materials", "Mortar Shell"}
    assert module.catalog_faction("Colonials") == "colonial"
    assert module.catalog_faction(None) is None


def test_parse_lines_errors():
    from foxbot.core.catalog import Catalog

    module = __import__("foxbot.features.orders", fromlist=["parse_lines"])
    catalog = Catalog()
    errors = module.parse_lines(catalog, "-5 Basic Materials\n0 Salvage\n2000000 Salvage\nSalvage", None).errors
    assert len(errors) == 4
    assert "positive" in errors[0] and "at least 1" in errors[1] and "1,000,000" in errors[2] and "amount and an item" in errors[3]
    assert "at least one line" in module.parse_lines(catalog, " \n ", None).errors[0]
    too_many = "\n".join(f"{index + 1} thing number {index}" for index in range(21))
    assert "at most 20 lines" in module.parse_lines(catalog, too_many, None).errors[0]
    assert "more than 1,000,000" in module.parse_lines(catalog, "900000 Salvage\n900000 Salvage", None).errors[0]
    assert "80 characters" in module.parse_lines(catalog, "5 " + "x" * 81, None).errors[0]
    exactly_twenty = "\n".join(f"{index + 1} thing number {index}" for index in range(20))
    assert not module.parse_lines(catalog, exactly_twenty, None).errors


async def test_slash_create_opens_modal_and_posts_card(world):
    w = world
    cog = w.bot.get_cog("Orders")
    interaction = interact(w, w.creator)
    choice = app_commands.Choice(name="Transport", value="Transport")
    await cog.create_command.callback(cog, interaction, choice, "Front ammo", "pipers", "syrinx")
    modal = interaction.response.modal
    assert isinstance(modal, orders.OrderModal)
    assert_modal_limits(modal)
    assert modal.destination_field is None
    assert modal.title_field.component.default == "Front ammo"
    set_label_value(modal.lines_field, "50 Basic Materials\n20 x Mortar Shell\n3 bmats")
    set_label_value(modal.notes_field, "Deliver to the *north* depot")
    submit = interact(w, w.creator)
    await modal.on_submit(submit)
    assert submit.recorder.kinds() == ["defer", "followup"]
    assert submit.recorder.calls[0][1]["ephemeral"]
    rows = await orders.list_orders(w.bot, w.guild.id)
    assert len(rows) == 1
    order = rows[0]
    assert (order["order_type"], order["title"], order["hex"], order["region"]) == ("Transport", "Front ammo", "Piper's Enclave", "Syrinx Pass")
    assert order["notes"] == "Deliver to the *north* depot"
    lines = await orders.fetch_lines(w.bot, order["id"])
    assert [(l["item_code"], l["item_name"], l["target"], l["done"]) for l in lines] == [
        ("Cloth", "Basic Materials", 50, 0),
        ("MortarAmmo", "Mortar Shell", 20, 0),
        (None, "bmats", 3, 0),
    ]
    assert len(w.channel.sent) == 1
    sent = w.channel.sent[0]
    embed = sent["embed"]
    assert embed.title == "Transport: Front ammo"
    assert "Basic Materials" in embed.description and "0/50" in embed.description
    assert "\\*north\\*" in next(f.value for f in embed.fields if f.name == "Notes")
    view = sent["view"]
    assert_view_limits(view)
    custom_ids = [item.custom_id for item in view.children]
    assert all(cid.startswith("fo:") for cid in custom_ids)
    assert {item.label for item in view.children if isinstance(item, discord.ui.Button)} == {"Contribute", "Edit Lines", "Close", "Delete"}
    assert order["message_id"] is not None and order["channel_id"] == w.channel.id
    reply_text = submit.recorder.last[1]["content"]
    assert orders.jump_link(order) in reply_text
    assert "Piper's Enclave" in reply_text and "bmats" in reply_text
    w.bot.boards.request_refresh.assert_any_call(w.guild.id, "orders")


async def test_slash_create_without_destination(world):
    w = world
    cog = w.bot.get_cog("Orders")
    interaction = interact(w, w.creator)
    await cog.create_command.callback(cog, interaction, app_commands.Choice(name="Scrap", value="Scrap"), "Salvage run", None, None)
    modal = interaction.response.modal
    set_label_value(modal.lines_field, "500 Salvage")
    set_label_value(modal.notes_field, "")
    await modal.on_submit(interact(w, w.creator))
    order = (await orders.list_orders(w.bot, w.guild.id))[0]
    assert (order["hex"], order["region"], order["notes"]) == ("", "", "")
    embed = w.channel.sent[0]["embed"]
    assert next(f.value for f in embed.fields if f.name == "Destination") == "Not set"
    assert all(f.name != "Notes" for f in embed.fields)


async def test_create_uses_orders_channel_and_falls_back(world):
    w = world
    orders_channel = make_channel(w.guild, "orders")
    await w.bot.settings.set(w.guild.id, keys.ORDERS_CHANNEL, orders_channel.id)
    first = await make_order(w, post=False)
    message = await orders.post_card(w.bot, first, w.channel)
    assert message.channel.id == orders_channel.id
    assert len(orders_channel.sent) == 1 and not w.channel.sent

    orders_channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access")
    second = await make_order(w, post=False, title="Second")
    message = await orders.post_card(w.bot, second, w.channel)
    assert message.channel.id == w.channel.id

    blocked = make_channel(w.guild, "blocked")
    blocked.permissions_for = MagicMock(return_value=discord.Permissions.none())
    await w.bot.settings.set(w.guild.id, keys.ORDERS_CHANNEL, blocked.id)
    third = await make_order(w, post=False, title="Third")
    await orders.post_card(w.bot, third, w.channel)
    assert not blocked.sent
    assert len(w.channel.sent) == 2

    w.channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access")
    fourth = await make_order(w, post=False, title="Fourth")
    assert await orders.post_card(w.bot, fourth, w.channel) is None
    assert (await orders.fetch(w.bot, w.guild.id, fourth["id"]))["message_id"] is None


async def test_bad_lines_offer_retry_with_typed_text(world):
    w = world
    cog = w.bot.get_cog("Orders")
    interaction = interact(w, w.creator)
    await cog.create_command.callback(cog, interaction, app_commands.Choice(name="Production", value="Production"), "Shells", None, None)
    modal = interaction.response.modal
    typed = "20 Mortar Shell\nMortar Shell\n-4 Salvage"
    set_label_value(modal.lines_field, typed)
    set_label_value(modal.notes_field, "keep me")
    submit = interact(w, w.creator)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert "not saved" in payload["content"] and "Line 2" in payload["content"] and "Line 3" in payload["content"]
    assert await orders.list_orders(w.bot, w.guild.id) == []
    retry_view = payload["view"]
    retry = interact(w, w.creator)
    await find_button(retry_view, "Fix and Try Again").callback(retry)
    again = retry.response.modal
    assert isinstance(again, orders.OrderModal) and again.replace_origin
    assert again.lines_field.component.default == typed
    assert again.notes_field.component.default == "keep me"
    assert again.title_field.component.default == "Shells"
    set_label_value(again.title_field, "Shells")
    set_label_value(again.lines_field, "20 Mortar Shell\n4 Salvage")
    set_label_value(again.notes_field, "keep me")
    fixed = interact(w, w.creator)
    await again.on_submit(fixed)
    assert fixed.recorder.kinds() == ["defer", "edit_original"]
    assert "created" in fixed.recorder.last[1]["content"]
    assert len(await orders.list_orders(w.bot, w.guild.id)) == 1


async def test_outsider_is_denied_everywhere(world):
    w = world
    cog = w.bot.get_cog("Orders")
    interaction = interact(w, w.outsider)
    await cog.create_command.callback(cog, interaction, app_commands.Choice(name="Production", value="Production"), "Nope", None, None)
    assert interaction.recorder.last[0] == "send" and "permission" in interaction.recorder.last[1]["content"]
    assert interaction.response.modal is None

    order = await make_order(w)
    view = card_view(w)
    lines = await orders.fetch_lines(w.bot, order["id"])
    denied = await press(w, find_select(view), w.outsider, values=[str(lines[0]["id"])])
    assert "permission" in texts(denied)
    denied = await press(w, find_button(view, "Contribute"), w.outsider)
    assert "permission" in texts(denied)
    for label in ("Edit Lines", "Close", "Delete"):
        denied = await press(w, find_button(view, label), w.helper)
        assert "creator or an admin" in texts(denied)
        assert denied.response.modal is None
    assert await w.bot.db.fetchval("SELECT COUNT(*) FROM order_contributions") == 0
    board = interact(w, w.outsider)
    await w.bot.boards.providers["orders"].on_button(board, "new")
    assert "permission" in texts(board)


async def test_line_panel_quick_buttons_and_max(world):
    w = world
    order = await make_order(w)
    panel, opened = await open_panel(w, order, w.helper)
    assert opened.recorder.kinds() == ["edit", "followup"]
    assert isinstance(panel, orders.LinePanel)
    assert_view_limits(panel)
    labels = [item.label for item in panel.children if isinstance(item, discord.ui.Button)]
    assert labels[:5] == ["+1", "+4", "+9", "Max", "Custom"]
    assert "Open Card" in labels

    for label in ("+1", "+4", "+9"):
        result = await click(w, panel, label, w.helper)
        assert result.recorder.last[0] == "edit"
    lines = await orders.fetch_lines(w.bot, order["id"])
    assert lines[0]["done"] == 14
    assert "Your contributions to this line: **14**" in result.recorder.last[1]["embed"].description
    rows = await w.bot.db.fetchall("SELECT user_id, amount FROM order_contributions ORDER BY id")
    assert [(r["user_id"], r["amount"]) for r in rows] == [(w.helper.id, 1), (w.helper.id, 4), (w.helper.id, 9)]
    card = w.cards.last_embed(order["message_id"])
    assert "14/50" in card.description and "28%" in card.description
    assert f"<@{w.helper.id}> - 14" in next(f.value for f in card.fields if f.name == "Top contributors")
    w.bot.boards.request_refresh.assert_any_call(w.guild.id, "orders")

    result = await click(w, panel, "Max", w.helper)
    assert "Added **36**" in result.recorder.last[1]["content"]
    assert (await orders.fetch_lines(w.bot, order["id"]))[0]["done"] == 50
    assert find_button(panel, "Max").disabled
    count = await w.bot.db.fetchval("SELECT COUNT(*) FROM order_contributions")
    result = await click(w, panel, "Max", w.helper)
    assert "already complete" in result.recorder.last[1]["content"]
    assert await w.bot.db.fetchval("SELECT COUNT(*) FROM order_contributions") == count

    await click(w, panel, "+9", w.helper)
    assert (await orders.fetch_lines(w.bot, order["id"]))[0]["done"] == 59
    card = w.cards.last_embed(order["message_id"])
    assert "59/50 (100%)" in card.description and "complete" in card.description
    assert "Completed" not in next(f.value for f in card.fields if f.name == "Status")


async def test_contribute_button_single_and_multiple_lines(world):
    w = world
    single = await make_order(w, lines="10 Salvage", title="Single")
    interaction = await press(w, find_button(card_view(w), "Contribute"), w.helper)
    kind, payload = interaction.recorder.last
    assert kind == "send" and isinstance(payload["view"], orders.LinePanel) and payload["ephemeral"]

    multi = await make_order(w, title="Multi")
    interaction = await press(w, find_button(card_view(w), "Contribute"), w.helper)
    kind, payload = interaction.recorder.last
    picker = payload["view"]
    assert not isinstance(picker, orders.LinePanel)
    select = find_select(picker)
    assert [o.label for o in select.options] == ["Basic Materials", "Mortar Shell"]
    lines = await orders.fetch_lines(w.bot, multi["id"])
    select_values(select, [str(lines[1]["id"])])
    pick = interact(w, w.helper)
    await select.callback(pick)
    kind, payload = pick.recorder.last
    assert kind == "edit" and isinstance(payload["view"], orders.LinePanel)
    assert payload["view"].line_id == lines[1]["id"]

    await orders.contribute(w.bot, multi["id"], lines[0]["id"], w.helper.id, 50)
    interaction = await press(w, find_button(card_view(w), "Contribute"), w.helper)
    assert isinstance(interaction.recorder.last[1]["view"], orders.LinePanel)
    assert interaction.recorder.last[1]["view"].line_id == lines[1]["id"]
    assert single["id"] != multi["id"]


async def test_panel_switch_line(world):
    w = world
    order = await make_order(w)
    lines = await orders.fetch_lines(w.bot, order["id"])
    panel, _ = await open_panel(w, order, w.helper)
    switch = find_select(panel)
    assert len(switch.options) == 2
    select_values(switch, [str(lines[1]["id"])])
    interaction = interact(w, w.helper)
    await switch.callback(interaction)
    assert panel.line_id == lines[1]["id"]
    assert interaction.recorder.last[1]["embed"].title == "Mortar Shell"
    await click(w, panel, "+4", w.helper)
    assert [l["done"] for l in await orders.fetch_lines(w.bot, order["id"])] == [0, 4]


async def test_custom_amount_rules(world):
    w = world
    order = await make_order(w)
    lines = await orders.fetch_lines(w.bot, order["id"])
    panel, _ = await open_panel(w, order, w.helper)
    opener = await click(w, panel, "Custom", w.helper)
    modal = opener.response.modal
    assert isinstance(modal, orders.CustomAmountModal)
    assert_modal_limits(modal)
    assert len(modal.children) == 1 and modal.credit_field is None

    set_label_value(modal.amount_field, "-5")
    submit = interact(w, w.helper)
    await modal.on_submit(submit)
    assert "creator or an admin" in submit.recorder.last[1]["content"]
    set_label_value(modal.amount_field, "abc")
    submit = interact(w, w.helper)
    await modal.on_submit(submit)
    assert "whole number" in submit.recorder.last[1]["content"]
    set_label_value(modal.amount_field, "2,000,000")
    submit = interact(w, w.helper)
    await modal.on_submit(submit)
    assert "between 1 and 1,000,000" in submit.recorder.last[1]["content"]
    set_label_value(modal.amount_field, "25")
    submit = interact(w, w.helper)
    await modal.on_submit(submit)
    assert submit.recorder.last[0] == "edit" and "Added **25**" in submit.recorder.last[1]["content"]
    assert (await orders.fetch_line(w.bot, order["id"], lines[0]["id"]))["done"] == 25

    creator_panel, _ = await open_panel(w, order, w.creator)
    opener = await click(w, creator_panel, "Custom", w.creator)
    modal = opener.response.modal
    assert_modal_limits(modal)
    assert len(modal.children) == 2 and isinstance(modal.credit_field.component, discord.ui.UserSelect)
    set_label_value(modal.amount_field, "-10")
    set_label_value(modal.credit_field, [w.helper])
    submit = interact(w, w.creator)
    await modal.on_submit(submit)
    assert "Removed **10**" in submit.recorder.last[1]["content"] and f"<@{w.helper.id}>" in submit.recorder.last[1]["content"]
    assert await orders.user_total(w.bot, lines[0]["id"], w.helper.id) == 15

    set_label_value(modal.amount_field, "-100")
    set_label_value(modal.credit_field, [])
    submit = interact(w, w.creator)
    await modal.on_submit(submit)
    assert (await orders.fetch_line(w.bot, order["id"], lines[0]["id"]))["done"] == 0
    assert "Removed **15**" in submit.recorder.last[1]["content"]
    submit = interact(w, w.creator)
    await modal.on_submit(submit)
    assert "already at 0" in submit.recorder.last[1]["content"]
    top, count = await orders.top_contributors(w.bot, order["id"])
    assert [row["user_id"] for row in top] == [w.helper.id] and count == 1


async def test_completed_card_and_leaderboard(world):
    w = world
    order = await make_order(w, lines="10 Salvage\n5 Diesel")
    lines = await orders.fetch_lines(w.bot, order["id"])
    members = [make_member(w.guild, roles=[w.guild.get_role(r)]) for r in []]
    assert members == []
    await orders.contribute(w.bot, order["id"], lines[0]["id"], w.helper.id, 8)
    await orders.contribute(w.bot, order["id"], lines[0]["id"], w.creator.id, 2)
    await orders.contribute(w.bot, order["id"], lines[1]["id"], w.creator.id, 5)
    for index in range(6):
        await orders.contribute(w.bot, order["id"], lines[1]["id"], 5000 + index, 1)
    await orders.refresh_card(w.bot, w.guild.id, order["id"])
    card = w.cards.last_embed(order["message_id"])
    status = next(f.value for f in card.fields if f.name == "Status")
    assert status.startswith("Completed")
    assert card.colour.value == orders.Colour.SUCCESS
    top = next(f.value for f in card.fields if f.name == "Top contributors")
    assert top.splitlines()[0] == f"1. <@{w.helper.id}> - 8"
    assert top.splitlines()[1] == f"2. <@{w.creator.id}> - 7"
    assert top.splitlines()[-1] == "and 3 more"
    assert "100% - 2/2 lines complete" in next(f.value for f in card.fields if f.name == "Overall")


async def test_edit_lines_keeps_done_amounts(world):
    w = world
    order = await make_order(w, notes="old notes")
    lines = await orders.fetch_lines(w.bot, order["id"])
    await orders.contribute(w.bot, order["id"], lines[0]["id"], w.helper.id, 30)
    await orders.contribute(w.bot, order["id"], lines[1]["id"], w.helper.id, 5)

    opener = await press(w, find_button(card_view(w), "Edit Lines"), w.creator)
    modal = opener.response.modal
    assert isinstance(modal, orders.OrderModal) and modal.entry is not None
    assert_modal_limits(modal)
    assert len(modal.children) == 4
    assert modal.lines_field.component.default == "50 Basic Materials\n20 Mortar Shell"
    assert modal.notes_field.component.default == "old notes"
    set_label_value(modal.title_field, "Renamed")
    set_label_value(modal.destination_field, "Syrinx Pass")
    set_label_value(modal.lines_field, "60 basic materials\n10 Soldier Supplies")
    set_label_value(modal.notes_field, "")
    submit = interact(w, w.creator)
    await modal.on_submit(submit)
    assert submit.recorder.kinds() == ["defer", "followup"]
    assert "updated" in submit.recorder.last[1]["content"] and "Syrinx Pass" in submit.recorder.last[1]["content"]
    fresh = await orders.fetch(w.bot, w.guild.id, order["id"])
    assert (fresh["title"], fresh["hex"], fresh["region"], fresh["notes"]) == ("Renamed", "Piper's Enclave", "Syrinx Pass", "")
    new_lines = await orders.fetch_lines(w.bot, order["id"])
    assert [(l["item_name"], l["target"], l["done"]) for l in new_lines] == [("Basic Materials", 60, 30), ("Soldier Supplies", 10, 0)]
    assert new_lines[0]["id"] == lines[0]["id"]
    assert await w.bot.db.fetchval("SELECT COUNT(*) FROM order_contributions WHERE line_id = ?", (lines[1]["id"],)) == 0
    card = w.cards.last_embed(order["message_id"])
    assert card.title == "Production: Renamed" and "30/60" in card.description

    admin_open = await press(w, find_button(card_view(w), "Edit Lines"), w.admin)
    admin_modal = admin_open.response.modal
    assert admin_modal.destination_field.component.default == "Syrinx Pass, Piper's Enclave"
    set_label_value(admin_modal.title_field, "Renamed")
    set_label_value(admin_modal.destination_field, "")
    set_label_value(admin_modal.lines_field, "60 Basic Materials\n10 Soldier Supplies")
    set_label_value(admin_modal.notes_field, "")
    submit = interact(w, w.admin)
    await admin_modal.on_submit(submit)
    assert "destination was removed" in submit.recorder.last[1]["content"]
    fresh = await orders.fetch(w.bot, w.guild.id, order["id"])
    assert (fresh["hex"], fresh["region"]) == ("", "")

    set_label_value(admin_modal.lines_field, "nonsense")
    submit = interact(w, w.helper)
    await admin_modal.on_submit(submit)
    assert "creator or an admin" in texts(submit)


async def test_close_reopen_and_delete(world):
    w = world
    order = await make_order(w)
    view = card_view(w)
    closing = await press(w, find_button(view, "Close"), w.creator)
    assert "Closed" in closing.recorder.last[1]["content"] and closing.recorder.last[1]["ephemeral"]
    closed = await orders.fetch(w.bot, w.guild.id, order["id"])
    assert closed["status"] == "closed" and closed["closed_by"] == w.creator.id
    edit = w.cards.message(order["message_id"]).edits[-1]
    assert edit["embed"].title.startswith("[Closed]")
    closed_view = edit["view"]
    assert [item.label for item in closed_view.children] == ["Reopen", "Delete"]
    assert all(isinstance(item, discord.ui.Button) for item in closed_view.children)

    denied = await press(w, find_button(view, "Contribute"), w.helper)
    assert "closed" in texts(denied)
    lines = await orders.fetch_lines(w.bot, order["id"])
    denied = await press(w, find_select(view), w.helper, values=[str(lines[0]["id"])])
    assert "closed" in texts(denied)
    denied = await press(w, find_button(view, "Edit Lines"), w.creator)
    assert "Reopen" in texts(denied) and denied.response.modal is None

    reopening = await press(w, find_button(closed_view, "Reopen"), w.admin)
    assert "Reopened" in reopening.recorder.last[1]["content"]
    reopened = await orders.fetch(w.bot, w.guild.id, order["id"])
    assert reopened["status"] == "open" and reopened["closed_by"] is None

    panel, _ = await open_panel(w, order, w.helper)
    await orders.set_status(w.bot, reopened, "closed", w.creator.id)
    stale = await click(w, panel, "+1", w.helper)
    assert "closed" in stale.recorder.last[1]["content"] and stale.recorder.last[1]["view"] is None
    await orders.set_status(w.bot, reopened, "open", w.creator.id)

    await orders.contribute(w.bot, order["id"], lines[0]["id"], w.helper.id, 3)
    deleting = await press(w, find_button(view, "Delete"), w.creator)
    confirm_view = deleting.recorder.last[1]["view"]
    assert deleting.recorder.last[1]["ephemeral"]
    assert await orders.fetch(w.bot, w.guild.id, order["id"]) is not None
    confirm = interact(w, w.creator)
    await find_button(confirm_view, "Delete").callback(confirm)
    assert await orders.fetch(w.bot, w.guild.id, order["id"]) is None
    assert await w.bot.db.fetchval("SELECT COUNT(*) FROM order_lines") == 0
    assert await w.bot.db.fetchval("SELECT COUNT(*) FROM order_contributions") == 0
    assert order["message_id"] in w.cards.deleted
    assert "Deleted" in confirm.recorder.last[1]["content"]
    gone = await press(w, find_button(view, "Contribute"), w.helper)
    assert "no longer exists" in texts(gone)


async def test_deleted_card_is_forgotten_and_can_be_reposted(world):
    w = world
    order = await make_order(w)
    old_message = order["message_id"]
    w.cards.missing.add(old_message)
    lines = await orders.fetch_lines(w.bot, order["id"])
    await orders.contribute(w.bot, order["id"], lines[0]["id"], w.helper.id, 5)
    await orders.refresh_card(w.bot, w.guild.id, order["id"])
    forgotten = await orders.fetch(w.bot, w.guild.id, order["id"])
    assert forgotten["message_id"] is None and forgotten["channel_id"] is None
    assert (await orders.fetch_lines(w.bot, order["id"]))[0]["done"] == 5

    cog = w.bot.get_cog("Orders")
    listing = interact(w, w.helper)
    await cog.list_command.callback(cog, listing)
    kind, payload = listing.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert "card missing" in payload["embed"].description
    picker = payload["view"]
    select = find_select(picker)
    select_values(select, [order["id"]])
    pick = interact(w, w.helper)
    await select.callback(pick)
    summary = pick.recorder.last[1]
    assert "missing" in summary["content"]
    summary_view = summary["view"]
    assert all(item.label != "Open Card" for item in summary_view.children)
    repost = interact(w, w.helper)
    await find_button(summary_view, "Repost Card").callback(repost)
    assert repost.recorder.kinds() == ["defer", "followup"]
    reposted = await orders.fetch(w.bot, w.guild.id, order["id"])
    assert reposted["message_id"] is not None and reposted["message_id"] != old_message
    assert orders.jump_link(reposted) in repost.recorder.last[1]["content"]
    assert "5/50" in w.channel.sent[-1]["embed"].description


async def test_repost_existing_card_needs_manager(world):
    w = world
    order = await make_order(w)
    old_message = order["message_id"]
    view = orders.OrderSummaryView(w.bot, order, w.helper.id)
    assert find_button(view, "Open Card").url == orders.jump_link(order)
    denied = interact(w, w.helper)
    await find_button(view, "Repost Card").callback(denied)
    assert "Only the creator or an admin" in texts(denied)
    assert len(w.channel.sent) == 1

    other = make_channel(w.guild, "elsewhere")
    moved = interact(w, w.creator, channel=other)
    manager_view = orders.OrderSummaryView(w.bot, order, w.creator.id)
    await find_button(manager_view, "Repost Card").callback(moved)
    fresh = await orders.fetch(w.bot, w.guild.id, order["id"])
    assert fresh["channel_id"] == other.id and fresh["message_id"] != old_message
    assert old_message in w.cards.deleted


async def test_board_render_groups_and_links(world):
    w = world
    production = await make_order(w, title="Shells [urgent]")
    transport = await make_order(w, title="Haul", order_type="Transport", lines="100 Salvage")
    closed = await make_order(w, title="Old", order_type="Scrap", lines="5 Salvage")
    await orders.set_status(w.bot, closed, "closed", w.creator.id)
    missing = await make_order(w, title="No card", order_type="Transport", lines="1 Diesel", post=False)
    lines = await orders.fetch_lines(w.bot, transport["id"])
    await orders.contribute(w.bot, transport["id"], lines[0]["id"], w.helper.id, 40)
    render = await w.bot.boards.providers["orders"].render(w.guild)
    text = "\n".join(e.description for e in render.embeds)
    assert "**Production** (1)" in text and "**Transport** (2)" in text and "Scrap" not in text
    assert orders.jump_link(production) in text and orders.jump_link(transport) in text
    assert "Shells (urgent)" in text
    assert "40%" in text and "No card" in text and "(card missing)" in text
    assert "Old" not in text
    assert [o.value for o in render.options] == [production["id"], transport["id"], missing["id"]]
    assert {b.action for b in render.buttons} == {"new", "list"}
    assert sum(len(e) for e in render.embeds) <= EMBED_TOTAL_LIMIT

    pick = interact(w, w.helper)
    await w.bot.boards.providers["orders"].on_pick(pick, transport["id"])
    kind, payload = pick.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert payload["embed"].title == "Transport: Haul"
    assert find_button(payload["view"], "Open Card").url == orders.jump_link(transport)
    contribute = interact(w, w.helper)
    await find_button(payload["view"], "Contribute").callback(contribute)
    assert isinstance(contribute.recorder.last[1]["view"], orders.LinePanel)

    listing = interact(w, w.helper)
    await w.bot.boards.providers["orders"].on_button(listing, "list")
    assert "Haul" in listing.recorder.last[1]["embed"].description


async def test_board_new_order_without_destination(world):
    w = world
    board = w.bot.boards.providers["orders"]
    start = interact(w, w.helper)
    await board.on_button(start, "new")
    kind, payload = start.recorder.last
    assert kind == "send" and payload["ephemeral"]
    type_view = payload["view"]
    type_select = find_select(type_view)
    assert [o.value for o in type_select.options] == ["Production", "Transport", "Scrap"]
    select_values(type_select, ["Transport"])
    step = interact(w, w.helper)
    await type_select.callback(step)
    kind, payload = step.recorder.last
    assert kind == "edit"
    picker = payload["view"]
    assert isinstance(picker, orders.DestinationPicker)
    assert "New Transport order" in payload["content"]
    assert_view_limits(picker)
    skip = interact(w, w.helper)
    await find_button(picker, "No Destination").callback(skip)
    modal = skip.response.modal
    assert isinstance(modal, orders.OrderModal) and modal.replace_origin and modal.location is None
    assert_modal_limits(modal)
    set_label_value(modal.title_field, "Haul shells")
    set_label_value(modal.lines_field, "30 Mortar Shell")
    set_label_value(modal.notes_field, "")
    submit = interact(w, w.helper)
    await modal.on_submit(submit)
    assert submit.recorder.kinds() == ["defer", "edit_original"]
    assert submit.recorder.last[1]["view"] is None
    order = (await orders.list_orders(w.bot, w.guild.id))[0]
    assert (order["order_type"], order["title"], order["hex"], order["created_by"]) == ("Transport", "Haul shells", "", w.helper.id)
    assert w.channel.sent[-1]["embed"].title == "Transport: Haul shells"


async def test_board_new_order_with_destination(world):
    w = world
    board = w.bot.boards.providers["orders"]
    start = interact(w, w.creator)
    await board.on_button(start, "new")
    type_select = find_select(start.recorder.last[1]["view"])
    select_values(type_select, ["Production"])
    step = interact(w, w.creator)
    await type_select.callback(step)
    picker = step.recorder.last[1]["view"]
    hex_select = find_select(picker, 0)
    select_values(hex_select, ["Piper's Enclave"])
    await hex_select.callback(interact(w, w.creator))
    with pytest.raises(AssertionError):
        find_button(picker, "No Destination")
    town_select = find_select(picker)
    select_values(town_select, ["Syrinx Pass"])
    to_modal = interact(w, w.creator)
    await town_select.callback(to_modal)
    modal = to_modal.response.modal
    assert modal.location.hex == "Piper's Enclave" and modal.location.region == "Syrinx Pass"
    set_label_value(modal.title_field, "Rifles")
    set_label_value(modal.lines_field, "40 loughcaster")
    set_label_value(modal.notes_field, "")
    submit = interact(w, w.creator)
    await modal.on_submit(submit)
    content = submit.recorder.last[1]["content"]
    assert "Syrinx Pass, Piper's Enclave" in content and "No.2 Loughcaster" in content
    order = (await orders.list_orders(w.bot, w.guild.id))[0]
    assert (order["hex"], order["region"]) == ("Piper's Enclave", "Syrinx Pass")


async def test_list_includes_only_recently_closed(world):
    w = world
    open_order = await make_order(w, title="Still open")
    recent = await make_order(w, title="Closed lately")
    old = await make_order(w, title="Closed long ago")
    await w.bot.db.execute(
        "UPDATE orders SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
        (timeutil.now() - 3 * 86400, w.creator.id, recent["id"]),
    )
    await w.bot.db.execute(
        "UPDATE orders SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
        (timeutil.now() - 10 * 86400, w.creator.id, old["id"]),
    )
    cog = w.bot.get_cog("Orders")
    listing = interact(w, w.helper)
    await cog.list_command.callback(cog, listing)
    payload = listing.recorder.last[1]
    description = payload["embed"].description
    assert "Still open" in description and "Closed lately" in description and "Closed long ago" not in description
    assert description.index("Still open") < description.index("Closed in the last 7 days") < description.index("Closed lately")
    values = [o.value for o in find_select(payload["view"]).options]
    assert values == [open_order["id"], recent["id"]]

    outsider = interact(w, w.outsider)
    await cog.list_command.callback(cog, outsider)
    assert "permission" in texts(outsider)


async def test_list_when_empty(world):
    w = world
    cog = w.bot.get_cog("Orders")
    listing = interact(w, w.helper)
    await cog.list_command.callback(cog, listing)
    assert "no open orders" in listing.recorder.last[1]["content"]


async def test_board_command_is_admin_only(world):
    w = world
    cog = w.bot.get_cog("Orders")
    denied = interact(w, w.helper)
    await cog.board.callback(cog, denied)
    assert "admins" in texts(denied)
    w.bot.boards.post = AsyncMock()
    allowed = interact(w, w.admin)
    await cog.board.callback(cog, allowed)
    w.bot.boards.post.assert_awaited_once_with(w.guild, "orders", w.channel)
    assert "Orders board posted" in allowed.recorder.last[1]["content"]
    assert "orders channel" in allowed.recorder.last[1]["content"]


async def test_create_autocomplete(world):
    w = world
    cog = w.bot.get_cog("Orders")
    hexes = await cog.create_hex_autocomplete(interact(w, w.helper), "piper")
    assert [c.value for c in hexes] == ["Piper's Enclave"]
    towns = await cog.create_town_autocomplete(interact(w, w.helper, namespace=SimpleNamespace(hex="Piper's Enclave")), "")
    assert "Syrinx Pass" in {c.value for c in towns} and len(towns) <= 25


async def test_worst_case_card_and_board_fit_discord_limits(world):
    w = world
    names = [f"Item_{index:02d} *bold* " + "x" * 60 for index in range(20)]
    text = "\n".join(f"{1_000_000} {name}" for name in names)
    parsed = orders.parse_lines(w.bot.catalog, text, None)
    assert not parsed.errors and len(parsed.lines) == 20
    order = await orders.create(
        w.bot,
        w.guild.id,
        order_type="Production",
        title="T*" * 50,
        hex_name="Piper's Enclave",
        region="Syrinx Pass",
        notes="*_" * 500,
        lines=parsed.lines,
        user_id=w.creator.id,
    )
    lines = await orders.fetch_lines(w.bot, order["id"])
    for index, line in enumerate(lines):
        await orders.contribute(w.bot, order["id"], line["id"], 900_000_000_000 + index, 999_999)
    embed, view = await orders.card_payload(w.bot, order)
    assert_embed_limits(embed)
    assert_view_limits(view)
    assert len(find_select(view).options) == 20
    summary_line = orders.card_line(lines[0])
    assert len(summary_line) <= orders.CARD_LINE_LIMIT

    for index in range(60):
        await orders.create(
            w.bot,
            w.guild.id,
            order_type=list(orders.ORDER_TYPES)[index % 3],
            title=f"Order {index} " + "y" * 85,
            hex_name="Piper's Enclave",
            region="Syrinx Pass",
            notes="",
            lines=parsed.lines[:1],
            user_id=w.creator.id,
        )
    render = await w.bot.boards.providers["orders"].render(w.guild)
    assert len(render.options) == 25
    assert sum(len(e) for e in render.embeds) <= EMBED_TOTAL_LIMIT
    assert all(len(e.description or "") <= EMBED_DESCRIPTION_LIMIT for e in render.embeds)
    assert all(len(o.label) <= 100 and len(o.description or "") <= 100 for o in render.options)


async def test_dynamic_items_registered_and_round_trip(world):
    w = world
    store = w.bot._connection._view_store
    registered = set(store._dynamic_items.values())
    assert {orders.OrderButtonItem, orders.OrderLineSelectItem} <= {cls for cls in registered if cls.__module__ == orders.__name__}
    order = await make_order(w)
    for item in card_view(w).children:
        assert item.custom_id.startswith("fo:") and order["id"] in item.custom_id
        matched = [
            cls
            for cls in (orders.OrderButtonItem, orders.OrderLineSelectItem)
            if cls.__discord_ui_compiled_template__.fullmatch(item.custom_id)
        ]
        assert len(matched) == 1
    assert not orders.OrderButtonItem.__discord_ui_compiled_template__.fullmatch("fb:orders:btn:new")


async def test_refresh_card_coalesces_concurrent_updates(world):
    w = world
    order = await make_order(w)
    w.cards.gate = asyncio.Event()
    first = asyncio.create_task(orders.refresh_card(w.bot, w.guild.id, order["id"]))
    await asyncio.sleep(0.05)
    await orders.refresh_card(w.bot, w.guild.id, order["id"])
    await orders.refresh_card(w.bot, w.guild.id, order["id"])
    w.cards.gate.set()
    await first
    assert len(w.cards.message(order["message_id"]).edits) == 2


async def test_modal_on_submit_rechecks_permission(world):
    w = world
    cog = w.bot.get_cog("Orders")
    interaction = interact(w, w.helper)
    await cog.create_command.callback(cog, interaction, app_commands.Choice(name="Production", value="Production"), "Later", None, None)
    modal = interaction.response.modal
    await w.bot.perms.set_roles(w.guild.id, "orders", set())
    set_label_value(modal.lines_field, "5 Salvage")
    set_label_value(modal.notes_field, "")
    submit = interact(w, w.helper)
    await modal.on_submit(submit)
    assert "permission" in texts(submit)
    assert await orders.list_orders(w.bot, w.guild.id) == []
