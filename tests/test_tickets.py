from datetime import datetime, timezone
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
    next_id,
    select_values,
    set_label_value,
)
from foxbot.constants import TICKET_STATUSES
from foxbot.core import settings as keys
from foxbot.core.text import EMBED_FIELD_LIMIT, EMBED_TOTAL_LIMIT

INSTRUCTIONS = (
    "After this form closes your ticket channel opens. Press the Order Service button, pick a service and a quantity, "
    "then press Confirm. Repeat for each service you need."
)


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"})


def history_message(author, content, minute, attachments=()):
    return SimpleNamespace(
        author=author,
        clean_content=content,
        created_at=datetime(2026, 10, 3, 12, minute, tzinfo=timezone.utc),
        attachments=[SimpleNamespace(url=url) for url in attachments],
        embeds=[],
    )


@pytest.fixture
async def world():
    bot = await make_bot(["foxbot.features.rares", "foxbot.features.tickets"])
    tickets = bot.extensions["foxbot.features.tickets"]
    rares = bot.extensions["foxbot.features.rares"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    guild.me = make_member(guild, name="FoxBot", bot=True)
    staff_role = add_role(guild, "Ticket Staff")
    await bot.perms.set_roles(guild.id, "tickets", {staff_role.id})
    staff = make_member(guild, name="Staffer", roles=[staff_role])
    customer = make_member(guild, name="Customer")
    other = make_member(guild, name="Other")
    admin = make_member(guild, name="Admin", administrator=True)
    lobby = make_channel(guild, "lobby")
    await tickets.set_service(bot, guild.id, "Ship Build", 200)
    await tickets.set_service(bot, guild.id, "Repairs", 50)
    state = SimpleNamespace(
        bot=bot,
        tickets=tickets,
        rares=rares,
        guild=guild,
        staff_role=staff_role,
        staff=staff,
        customer=customer,
        other=other,
        admin=admin,
        lobby=lobby,
        created=[],
        categories=[],
        card_edits=[],
    )

    async def create_category(name, **kwargs):
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = next_id()
        category.name = name
        category.kwargs = kwargs
        guild._channels[category.id] = category
        state.categories.append(category)
        return category

    async def create_text_channel(name, **kwargs):
        channel = make_channel(guild, name)
        channel.kwargs = kwargs
        channel.messages = []

        async def history(**history_kwargs):
            for message in channel.messages:
                yield message

        async def rename(**edit_kwargs):
            channel.name = edit_kwargs.get("name", channel.name)

        channel.history = history
        channel.edit = AsyncMock(side_effect=rename)
        channel.delete = AsyncMock()
        state.created.append(channel)
        return channel

    guild.create_category = AsyncMock(side_effect=create_category)
    guild.create_text_channel = AsyncMock(side_effect=create_text_channel)

    def partial_messageable(channel_id, **kwargs):
        partial = MagicMock()

        def get_partial_message(message_id):
            message = MagicMock()

            async def edit_card(**edit_kwargs):
                state.card_edits.append((channel_id, message_id, edit_kwargs))

            message.edit = AsyncMock(side_effect=edit_card)
            return message

        partial.get_partial_message.side_effect = get_partial_message
        return partial

    bot.get_partial_messageable = MagicMock(side_effect=partial_messageable)
    yield state
    await bot.get_cog("Rares").cog_unload()
    await bot.db.close()


def channel_named(world, name):
    return next(channel for channel in world.created if channel.name == name)


async def open_ticket(world, user, regiment="KM", notes=""):
    cog = world.bot.get_cog("Tickets")
    command = make_interaction(world.bot, user, channel=world.lobby)
    await cog.open.callback(cog, command)
    modal = command.response.modal
    assert modal is not None, command.recorder.calls
    set_label_value(modal.regiment_field, regiment)
    set_label_value(modal.notes_field, notes)
    submit = make_interaction(world.bot, user, channel=world.lobby)
    await modal.on_submit(submit)
    return submit


async def opened(world, user=None, regiment="KM", notes=""):
    await open_ticket(world, user or world.customer, regiment, notes)
    channel = world.created[-1]
    card = channel.sent[0]
    ticket = await world.tickets.fetch_by_channel(world.bot, channel.id)
    return channel, card, ticket


async def press(world, view, label, user, channel):
    button = find_button(view, label)
    cls = world.tickets.TicketButton
    match = cls.__discord_ui_compiled_template__.fullmatch(button.custom_id)
    assert match is not None
    interaction = make_interaction(world.bot, user, channel=channel)
    item = await cls.from_custom_id(interaction, button, match)
    await item.callback(interaction)
    return interaction


def last_card(world):
    return world.card_edits[-1][2]["embed"]


def fields(embed):
    return {field.name: field.value for field in embed.fields}


async def order(world, view, channel, user, service, quantity):
    click = await press(world, view, "Order Service", user, channel)
    panel = click.recorder.last[1]["view"]
    service_select = find_select(panel, 0)
    index = next(o.value for o in service_select.options if o.label == service)
    select_values(service_select, [index])
    await service_select.callback(make_interaction(world.bot, user, channel=channel))
    quantity_select = find_select(panel, 1)
    select_values(quantity_select, [str(quantity)])
    await quantity_select.callback(make_interaction(world.bot, user, channel=channel))
    confirm = make_interaction(world.bot, user, channel=channel)
    await find_button(panel, "Confirm").callback(confirm)
    return panel, confirm


async def test_open_modal_layout(world):
    cog = world.bot.get_cog("Tickets")
    command = make_interaction(world.bot, world.customer, channel=world.lobby)
    await cog.open.callback(cog, command)
    modal = command.response.modal
    assert isinstance(modal, world.tickets.OpenTicketModal)
    assert len(modal.children) <= 5
    assert isinstance(modal.children[0], discord.ui.TextDisplay)
    assert modal.children[0].content == INSTRUCTIONS
    assert modal.regiment_field.text == "Regiment"
    assert modal.regiment_field.component.required and modal.regiment_field.component.max_length == 50
    assert modal.notes_field.text == "Notes"
    assert not modal.notes_field.component.required
    assert modal.notes_field.component.style == discord.TextStyle.paragraph
    modal.to_dict()


async def test_open_creates_channels_and_exactly_one_card(world):
    submit = await open_ticket(world, world.customer, regiment="  KM  Navy ", notes="Need it by Friday")
    assert world.guild.create_category.await_count == 1
    category = world.categories[0]
    assert category.name == "Tickets"
    assert await world.bot.settings.get(world.guild.id, keys.TICKET_CATEGORY) == category.id
    log_channel = channel_named(world, "ticket-log")
    assert await world.bot.settings.get(world.guild.id, keys.TICKET_LOG_CHANNEL) == log_channel.id
    log_overwrites = log_channel.kwargs["overwrites"]
    assert log_overwrites[world.guild.default_role].view_channel is False
    assert log_overwrites[world.staff_role].view_channel is True
    assert world.customer not in log_overwrites

    channel = channel_named(world, "ticket-km-navy-0001")
    assert channel.kwargs["category"] is category
    overwrites = channel.kwargs["overwrites"]
    assert overwrites[world.guild.default_role].view_channel is False
    for target in (world.customer, world.staff_role, world.guild.me):
        assert overwrites[target].view_channel is True and overwrites[target].send_messages is True
    assert world.other not in overwrites

    assert len(channel.sent) == 1
    card = channel.sent[0]
    assert card["content"] == world.staff_role.mention
    assert card["allowed_mentions"].roles == [world.staff_role]
    assert card["allowed_mentions"].users is False and card["allowed_mentions"].everyone is False
    embed = card["embed"]
    assert embed.title == "Ticket 0001 - KM Navy"
    values = fields(embed)
    assert values["Status"] == "Open"
    assert values["Services"] == "Nothing ordered yet"
    assert values["Rares Owed"] == "Nothing ordered yet"
    assert values["Notes"] == "Need it by Friday"
    assert values["Assigned To"] == "Unassigned"
    assert embed.colour.value == TICKET_STATUSES["Open"]
    view = card["view"]
    assert view.timeout is None
    labels = [item.label for item in view.children]
    assert labels == ["Set Status", "Assign", "Log Payment", "Order Service", "Remove Service", "Close Ticket"]
    assert all(item.custom_id.startswith("ft:") and len(item.custom_id) <= 100 for item in view.children)
    view.to_components()

    assert submit.recorder.kinds() == ["defer", "followup"]
    assert channel.mention in submit.recorder.last[1]["content"]
    assert submit.recorder.last[1]["ephemeral"]
    ticket = await world.tickets.fetch(world.bot, world.guild.id, 1)
    assert (ticket["channel_id"], ticket["card_message_id"], ticket["regiment"], ticket["opened_by"]) == (
        channel.id,
        ticket["card_message_id"],
        "KM Navy",
        world.customer.id,
    )
    assert ticket["card_message_id"] is not None


async def test_numbers_increase_and_setup_channels_are_reused(world):
    await open_ticket(world, world.customer)
    await open_ticket(world, world.other, regiment="Navy")
    assert world.guild.create_category.await_count == 1
    assert [channel.name for channel in world.created] == ["ticket-log", "ticket-km-0001", "ticket-navy-0002"]
    second = await world.tickets.fetch(world.bot, world.guild.id, 2)
    assert second["regiment"] == "Navy" and second["opened_by"] == world.other.id


async def test_deleted_category_and_log_channel_are_recreated(world):
    await open_ticket(world, world.customer)
    for channel in [world.categories[0], channel_named(world, "ticket-log")]:
        del world.guild._channels[channel.id]
    await open_ticket(world, world.customer)
    assert world.guild.create_category.await_count == 2
    assert [channel.name for channel in world.created].count("ticket-log") == 2


async def test_numbering_is_per_guild(world):
    await open_ticket(world, world.customer)
    other_guild = await world.tickets.create(world.bot, 77, regiment="Elsewhere", notes="", user_id=5)
    assert other_guild["number"] == 1
    third = await world.tickets.create(world.bot, world.guild.id, regiment="KM", notes="", user_id=5)
    assert third["number"] == 2


async def test_open_refused_without_services(world):
    await world.tickets.remove_service(world.bot, world.guild.id, "Ship Build")
    await world.tickets.remove_service(world.bot, world.guild.id, "Repairs")
    cog = world.bot.get_cog("Tickets")
    command = make_interaction(world.bot, world.customer, channel=world.lobby)
    await cog.open.callback(cog, command)
    assert command.response.modal is None
    assert "No services" in command.recorder.last[1]["content"]
    modal = world.tickets.OpenTicketModal(world.bot)
    set_label_value(modal.regiment_field, "KM")
    set_label_value(modal.notes_field, "")
    submit = make_interaction(world.bot, world.customer, channel=world.lobby)
    await modal.on_submit(submit)
    assert "No services" in submit.recorder.last[1]["content"]
    assert world.created == []


async def test_no_staff_roles_means_no_ping_and_everyone_never_pinged(world):
    await world.bot.perms.set_roles(world.guild.id, "tickets", set())
    channel, card, _ = await opened(world)
    assert card["content"] is None
    await world.bot.perms.set_roles(world.guild.id, "tickets", {world.guild.id, world.staff_role.id})
    channel, card, _ = await opened(world)
    assert card["content"] == world.staff_role.mention
    assert channel.kwargs["overwrites"][world.guild.default_role].view_channel is False


async def test_admin_role_gets_channel_access(world):
    officer = add_role(world.guild, "Officer")
    await world.bot.settings.set(world.guild.id, keys.ADMIN_ROLE, officer.id)
    channel, card, _ = await opened(world)
    assert channel.kwargs["overwrites"][officer].view_channel is True
    assert card["content"] == world.staff_role.mention


async def test_missing_permissions_give_clear_errors(world):
    world.guild.create_category.side_effect = forbidden()
    submit = await open_ticket(world, world.customer)
    assert "Manage Channels" in submit.recorder.last[1]["content"]
    assert await world.tickets.ticket_rows(world.bot, world.guild.id) == []

    world.guild.create_category.side_effect = None
    category = MagicMock(spec=discord.CategoryChannel)
    category.id = next_id()
    world.guild._channels[category.id] = category
    log_channel = make_channel(world.guild, "ticket-log")
    await world.bot.settings.set(world.guild.id, keys.TICKET_CATEGORY, category.id)
    await world.bot.settings.set(world.guild.id, keys.TICKET_LOG_CHANNEL, log_channel.id)
    world.guild.create_text_channel.side_effect = forbidden()
    submit = await open_ticket(world, world.customer)
    assert "Manage Channels" in submit.recorder.last[1]["content"]
    assert await world.tickets.ticket_rows(world.bot, world.guild.id) == []


async def test_card_post_failure_removes_channel(world):
    channel_holder = {}
    original = world.guild.create_text_channel.side_effect

    async def create(name, **kwargs):
        channel = await original(name, **kwargs)
        if name.startswith("ticket-km"):
            channel.send = AsyncMock(side_effect=forbidden())
            channel_holder["channel"] = channel
        return channel

    world.guild.create_text_channel.side_effect = create
    submit = await open_ticket(world, world.customer)
    assert "could not post" in submit.recorder.last[1]["content"]
    channel_holder["channel"].delete.assert_awaited()
    assert await world.tickets.ticket_rows(world.bot, world.guild.id) == []


async def test_order_service_flow_with_live_cost_stacking_and_rename(world):
    channel, card, ticket = await opened(world)
    click = await press(world, card["view"], "Order Service", world.customer, channel)
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"]
    panel = payload["view"]
    assert "Pick a service, then a quantity." in payload["content"]
    confirm_button = find_button(panel, "Confirm")
    assert confirm_button.disabled
    service_select = find_select(panel, 0)
    quantity_select = find_select(panel, 1)
    assert len(service_select.options) <= 25 and len(quantity_select.options) == 25
    assert [o.label for o in quantity_select.options][:3] == ["1", "2", "3"]
    assert [o.label for o in service_select.options] == ["Repairs", "Ship Build"]
    assert service_select.options[1].description == "200 Rare Alloys each"

    select_values(service_select, ["1"])
    step = make_interaction(world.bot, world.customer, channel=channel)
    await service_select.callback(step)
    assert "**Ship Build** - 200 Rare Alloys each." in step.recorder.last[1]["content"]
    assert find_button(panel, "Confirm").disabled

    quantity_select = find_select(panel, 1)
    select_values(quantity_select, ["3"])
    step = make_interaction(world.bot, world.customer, channel=channel)
    await quantity_select.callback(step)
    content = step.recorder.last[1]["content"]
    assert "**3x Ship Build**" in content and "**600 Rare Alloys** total" in content
    assert not find_button(panel, "Confirm").disabled
    assert any(o.default for o in find_select(panel, 0).options)

    confirm = make_interaction(world.bot, world.customer, channel=channel)
    await find_button(panel, "Confirm").callback(confirm)
    kind, payload = confirm.recorder.last
    assert kind == "edit"
    assert "Added **3x Ship Build** (600 Rare Alloys)" in payload["content"]
    assert "Order total is now **600 Rare Alloys**" in payload["content"]
    assert find_button(panel, "Confirm").disabled
    assert await world.tickets.items(world.bot, world.guild.id, 1) == [{"service": "Ship Build", "unit_cost": 200, "quantity": 3}]
    values = fields(last_card(world))
    assert values["Services"] == "3x Ship Build - 600 Rare Alloys"
    assert values["Total Cost"] == "600 Rare Alloys"
    assert values["Rares Owed"] == "600 Rare Alloys"
    assert channel.name == "ticket-km-ship-build-0001"
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["renamed"] == 1

    await order(world, card["view"], channel, world.customer, "Ship Build", 2)
    await order(world, card["view"], channel, world.customer, "Repairs", 1)
    assert await world.tickets.items(world.bot, world.guild.id, 1) == [
        {"service": "Ship Build", "unit_cost": 200, "quantity": 5},
        {"service": "Repairs", "unit_cost": 50, "quantity": 1},
    ]
    assert channel.edit.await_count == 1
    assert fields(last_card(world))["Total Cost"] == "1050 Rare Alloys"
    assert len(channel.sent) == 1


async def test_prices_are_snapshotted_per_line(world):
    channel, card, _ = await opened(world)
    await order(world, card["view"], channel, world.customer, "Ship Build", 2)
    await world.tickets.set_service(world.bot, world.guild.id, "Ship Build", 300)
    await order(world, card["view"], channel, world.customer, "Ship Build", 1)
    assert await world.tickets.items(world.bot, world.guild.id, 1) == [
        {"service": "Ship Build", "unit_cost": 200, "quantity": 2},
        {"service": "Ship Build", "unit_cost": 300, "quantity": 1},
    ]
    values = fields(last_card(world))
    assert values["Services"] == "2x Ship Build - 400 Rare Alloys\n1x Ship Build - 300 Rare Alloys"
    assert values["Total Cost"] == "700 Rare Alloys"


async def test_price_change_while_panel_open_needs_reconfirm(world):
    channel, card, _ = await opened(world)
    click = await press(world, card["view"], "Order Service", world.customer, channel)
    panel = click.recorder.last[1]["view"]
    select_values(find_select(panel, 0), ["1"])
    await find_select(panel, 0).callback(make_interaction(world.bot, world.customer, channel=channel))
    select_values(find_select(panel, 1), ["2"])
    await find_select(panel, 1).callback(make_interaction(world.bot, world.customer, channel=channel))
    await world.tickets.set_service(world.bot, world.guild.id, "Ship Build", 250)
    confirm = make_interaction(world.bot, world.customer, channel=channel)
    await find_button(panel, "Confirm").callback(confirm)
    content = confirm.recorder.last[1]["content"]
    assert "changed to **250**" in content and "**500 Rare Alloys** total" in content
    assert await world.tickets.items(world.bot, world.guild.id, 1) == []
    confirm = make_interaction(world.bot, world.customer, channel=channel)
    await find_button(panel, "Confirm").callback(confirm)
    assert await world.tickets.items(world.bot, world.guild.id, 1) == [{"service": "Ship Build", "unit_cost": 250, "quantity": 2}]


async def test_removed_service_cannot_be_confirmed(world):
    channel, card, _ = await opened(world)
    click = await press(world, card["view"], "Order Service", world.customer, channel)
    panel = click.recorder.last[1]["view"]
    select_values(find_select(panel, 0), ["0"])
    await find_select(panel, 0).callback(make_interaction(world.bot, world.customer, channel=channel))
    select_values(find_select(panel, 1), ["1"])
    await find_select(panel, 1).callback(make_interaction(world.bot, world.customer, channel=channel))
    await world.tickets.remove_service(world.bot, world.guild.id, "Repairs")
    confirm = make_interaction(world.bot, world.customer, channel=channel)
    await find_button(panel, "Confirm").callback(confirm)
    assert "no longer offered" in confirm.recorder.last[1]["content"]
    assert [o.label for o in find_select(panel, 0).options] == ["Ship Build"]
    assert await world.tickets.items(world.bot, world.guild.id, 1) == []


async def test_order_and_remove_limited_to_opener_or_staff(world):
    channel, card, _ = await opened(world)
    for label in ("Order Service", "Remove Service", "Close Ticket"):
        click = await press(world, card["view"], label, world.other, channel)
        kind, payload = click.recorder.last
        assert kind == "send" and payload["ephemeral"]
        assert payload["content"] == world.tickets.NOT_CUSTOMER
    await order(world, card["view"], channel, world.staff, "Repairs", 4)
    await order(world, card["view"], channel, world.admin, "Repairs", 1)
    assert await world.tickets.items(world.bot, world.guild.id, 1) == [{"service": "Repairs", "unit_cost": 50, "quantity": 5}]
    click = await press(world, card["view"], "Order Service", world.customer, channel)
    panel = click.recorder.last[1]["view"]
    other_click = make_interaction(world.bot, world.other, channel=channel)
    allowed = await panel.interaction_check(other_click)
    assert not allowed


async def test_remove_service(world):
    channel, card, _ = await opened(world)
    click = await press(world, card["view"], "Remove Service", world.customer, channel)
    assert "Nothing has been ordered" in click.recorder.last[1]["content"]
    await order(world, card["view"], channel, world.customer, "Ship Build", 2)
    await order(world, card["view"], channel, world.customer, "Repairs", 3)
    click = await press(world, card["view"], "Remove Service", world.customer, channel)
    panel = click.recorder.last[1]["view"]
    select = find_select(panel)
    assert [o.label for o in select.options] == ["2x Ship Build", "3x Repairs"]
    assert select.options[0].description == "400 Rare Alloys (200 each)"
    select_values(select, ["0"])
    pick = make_interaction(world.bot, world.customer, channel=channel)
    await select.callback(pick)
    content = pick.recorder.last[1]["content"]
    assert "Removed **2x Ship Build**" in content and "**150 Rare Alloys**" in content
    assert await world.tickets.items(world.bot, world.guild.id, 1) == [{"service": "Repairs", "unit_cost": 50, "quantity": 3}]
    assert fields(last_card(world))["Total Cost"] == "150 Rare Alloys"
    again = make_interaction(world.bot, world.customer, channel=channel)
    await select.callback(again)
    assert "already removed" in again.recorder.last[1]["content"]


async def test_set_status_is_staff_only_and_silent(world):
    channel, card, _ = await opened(world)
    click = await press(world, card["view"], "Set Status", world.customer, channel)
    assert "Ticket staff" in click.recorder.last[1]["content"]
    click = await press(world, card["view"], "Set Status", world.staff, channel)
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"]
    picker = payload["view"]
    select = find_select(picker)
    assert [o.label for o in select.options] == list(TICKET_STATUSES)
    assert [o.default for o in select.options][0] is True
    select_values(select, ["Payment Pending"])
    pick = make_interaction(world.bot, world.staff, channel=channel)
    await select.callback(pick)
    kind, payload = pick.recorder.last
    assert kind == "edit" and "from **Open** to **Payment Pending**" in payload["content"]
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["status"] == "Payment Pending"
    embed = last_card(world)
    assert fields(embed)["Status"] == "Payment Pending"
    assert embed.colour.value == TICKET_STATUSES["Payment Pending"]
    assert len(channel.sent) == 1

    await world.bot.perms.set_roles(world.guild.id, "tickets", set())
    world.staff.roles = [world.guild.default_role]
    select_values(select, ["Paid"])
    late = make_interaction(world.bot, world.staff, channel=channel)
    await select.callback(late)
    assert "Ticket staff" in late.recorder.last[1]["content"]
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["status"] == "Payment Pending"


async def test_assign_validates_staff(world):
    channel, card, _ = await opened(world)
    click = await press(world, card["view"], "Assign", world.customer, channel)
    assert "Ticket staff" in click.recorder.last[1]["content"]
    click = await press(world, card["view"], "Assign", world.staff, channel)
    picker = click.recorder.last[1]["view"]
    select = next(item for item in picker.children if isinstance(item, discord.ui.UserSelect))
    assert find_button(picker, "Unassign").disabled
    select_values(select, [world.customer])
    pick = make_interaction(world.bot, world.staff, channel=channel)
    await select.callback(pick)
    assert "is not ticket staff" in pick.recorder.last[1]["content"]
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["assigned_to"] is None
    select_values(select, [world.admin])
    pick = make_interaction(world.bot, world.staff, channel=channel)
    await select.callback(pick)
    assert world.admin.mention in pick.recorder.last[1]["content"]
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["assigned_to"] == world.admin.id
    assert fields(last_card(world))["Assigned To"] == f"<@{world.admin.id}>"

    click = await press(world, card["view"], "Assign", world.staff, channel)
    picker = click.recorder.last[1]["view"]
    assert not find_button(picker, "Unassign").disabled
    unassign = make_interaction(world.bot, world.staff, channel=channel)
    await find_button(picker, "Unassign").callback(unassign)
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["assigned_to"] is None
    assert fields(last_card(world))["Assigned To"] == "Unassigned"


async def submit_payment(world, card, channel, user, raw):
    click = await press(world, card["view"], "Log Payment", user, channel)
    modal = click.response.modal
    if modal is None:
        return click
    assert len(modal.children) <= 5
    set_label_value(modal.amount_field, raw)
    submit = make_interaction(world.bot, user, channel=channel)
    await modal.on_submit(submit)
    return submit


async def test_log_payment_updates_card_and_regiment_stock(world):
    log_channel = make_channel(world.guild, "rares-log")
    await world.bot.settings.set(world.guild.id, keys.RARES_LOG_CHANNEL, log_channel.id)
    channel, card, _ = await opened(world)
    await order(world, card["view"], channel, world.customer, "Ship Build", 3)
    denied = await submit_payment(world, card, channel, world.customer, "100")
    assert "Ticket staff" in denied.recorder.last[1]["content"]

    submit = await submit_payment(world, card, channel, world.staff, "250")
    assert submit.recorder.kinds() == ["defer", "followup"]
    content = submit.recorder.last[1]["content"]
    assert "Logged **+250** Rare Alloys" in content and "350 Rare Alloys still owed" in content
    assert "Regiment stock: 0 -> 250" in content
    values = fields(last_card(world))
    assert values["Rares Paid"] == "250 Rare Alloys" and values["Rares Owed"] == "350 Rare Alloys"
    assert await world.rares.get_count(world.bot, world.guild.id) == 250
    log_row = (await world.rares.log_rows(world.bot, world.guild.id))[0]
    assert (log_row["action"], log_row["amount"], log_row["ticket_number"]) == ("payment", 250, 1)
    assert "Ticket 0001 - KM" == log_row["note"]
    assert fields(log_channel.sent[-1]["embed"])["Ticket"] == "0001"

    submit = await submit_payment(world, card, channel, world.staff, "-50")
    assert "Regiment stock: 250 -> 200" in submit.recorder.last[1]["content"]
    assert fields(last_card(world))["Rares Paid"] == "200 Rare Alloys"

    submit = await submit_payment(world, card, channel, world.staff, "400")
    assert "paid in full" in submit.recorder.last[1]["content"]
    assert fields(last_card(world))["Rares Owed"] == "Paid in full"

    for raw, message in (("-1000", "below zero"), ("0", "cannot be zero"), ("lots", "whole number")):
        submit = await submit_payment(world, card, channel, world.staff, raw)
        assert message in submit.recorder.last[1]["content"]
    assert await world.tickets.paid_total(world.bot, world.guild.id, 1) == 600
    assert len(channel.sent) == 1


async def test_payment_without_rares_extension(world):
    await world.bot.unload_extension("foxbot.features.rares")
    channel, card, _ = await opened(world)
    submit = await submit_payment(world, card, channel, world.staff, "100")
    content = submit.recorder.last[1]["content"]
    assert "Logged **+100**" in content and "Regiment stock" not in content
    await world.bot.load_extension("foxbot.features.rares")


async def test_close_ticket_posts_transcript_and_deletes_channel(world):
    channel, card, ticket = await opened(world, notes="Urgent")
    await order(world, card["view"], channel, world.customer, "Repairs", 2)
    await submit_payment(world, card, channel, world.staff, "60")
    channel.messages = [
        history_message(world.guild.me, "@Ticket Staff", 0),
        history_message(world.customer, "Hello, we need two repairs\nat the depot", 1, ["https://cdn.example/depot.png"]),
        history_message(world.staff, "On it", 2),
    ]
    click = await press(world, card["view"], "Close Ticket", world.customer, channel)
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"] and "Close ticket **0001**" in payload["content"]
    confirm_view = payload["view"]
    confirm = make_interaction(world.bot, world.customer, channel=channel)
    await find_button(confirm_view, "Close Ticket").callback(confirm)
    assert confirm.recorder.kinds() == ["edit"]
    log_channel = channel_named(world, "ticket-log")
    assert len(log_channel.sent) == 1
    post = log_channel.sent[0]
    summary = fields(post["embed"])
    assert summary["Closed By"] == f"<@{world.customer.id}>"
    assert summary["Services"] == "2x Repairs - 100 Rare Alloys"
    assert summary["Rares Paid"] == "60 Rare Alloys" and summary["Rares Owed"] == "40 Rare Alloys"
    assert summary["Notes"] == "Urgent"
    attachment = post["file"]
    assert attachment.filename == "ticket-0001-transcript.txt"
    text = attachment.fp.read().decode("utf-8")
    assert "Ticket 0001 - KM" in text
    assert "[2026-10-03 12:01:00 UTC] Customer" in text
    assert "Hello, we need two repairs\n    at the depot" in text
    assert "Attachment: https://cdn.example/depot.png" in text
    assert "Staffer" in text and "On it" in text
    closed = await world.tickets.fetch(world.bot, world.guild.id, 1)
    assert closed["closed_by"] == world.customer.id and closed["closed_at"] is not None
    channel.delete.assert_awaited_once()

    after = await press(world, card["view"], "Order Service", world.customer, channel)
    assert "closed" in after.recorder.last[1]["content"]
    assert await world.tickets.fetch_by_channel(world.bot, channel.id) is None


async def test_close_reports_failed_channel_delete(world):
    channel, card, _ = await opened(world)
    channel.delete = AsyncMock(side_effect=forbidden())
    click = await press(world, card["view"], "Close Ticket", world.staff, channel)
    confirm = make_interaction(world.bot, world.staff, channel=channel)
    await find_button(click.recorder.last[1]["view"], "Close Ticket").callback(confirm)
    kind, payload = confirm.recorder.last
    assert kind == "followup" and payload["ephemeral"]
    assert "could not delete this channel" in payload["content"] and "Manage Channels" in payload["content"]
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["closed_at"] is not None


async def test_close_aborts_when_transcript_cannot_be_posted(world):
    channel, card, _ = await opened(world)
    log_channel = channel_named(world, "ticket-log")
    log_channel.send = AsyncMock(side_effect=forbidden())
    click = await press(world, card["view"], "Close Ticket", world.customer, channel)
    confirm = make_interaction(world.bot, world.customer, channel=channel)
    await find_button(click.recorder.last[1]["view"], "Close Ticket").callback(confirm)
    assert "was not closed" in confirm.recorder.last[1]["content"]
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["closed_at"] is None
    channel.delete.assert_not_awaited()


async def test_close_cancel_keeps_ticket(world):
    channel, card, _ = await opened(world)
    click = await press(world, card["view"], "Close Ticket", world.customer, channel)
    cancel = make_interaction(world.bot, world.customer, channel=channel)
    await find_button(click.recorder.last[1]["view"], "Cancel").callback(cancel)
    assert cancel.recorder.last[1]["content"] == "Cancelled."
    assert (await world.tickets.fetch(world.bot, world.guild.id, 1))["closed_at"] is None


async def test_deleted_channel_closes_ticket(world):
    channel, card, _ = await opened(world)
    cog = world.bot.get_cog("Tickets")
    await cog.on_guild_channel_delete(channel)
    ticket = await world.tickets.fetch(world.bot, world.guild.id, 1)
    assert ticket["closed_at"] is not None and ticket["closed_by"] is None
    log_channel = channel_named(world, "ticket-log")
    assert "no transcript" in log_channel.sent[-1]["embed"].description
    await cog.on_guild_channel_delete(world.lobby)


async def make_history(world):
    tickets, bot, guild_id = world.tickets, world.bot, world.guild.id
    first = await tickets.create(bot, guild_id, regiment="KM", notes="", user_id=world.customer.id)
    await tickets.update(bot, first, channel_id=111, status="In Production", assigned_to=world.staff.id)
    await tickets.add_item(bot, guild_id, first["number"], "Ship Build", 200, 5)
    await tickets.record_payment(bot, guild_id, first["number"], 1000, world.staff.id)
    second = await tickets.create(bot, guild_id, regiment=" km ", notes="", user_id=world.other.id)
    await tickets.update(bot, second, channel_id=222)
    await tickets.add_item(bot, guild_id, second["number"], "Repairs", 50, 2)
    await tickets.record_payment(bot, guild_id, second["number"], 50, world.staff.id)
    third = await tickets.create(bot, guild_id, regiment="Navy", notes="", user_id=world.customer.id)
    await tickets.add_item(bot, guild_id, third["number"], "Repairs", 50, 1)
    await tickets.record_payment(bot, guild_id, third["number"], 150, world.staff.id)
    await tickets.mark_closed(bot, third, world.staff.id, 1_700_000_000)
    fourth = await tickets.create(bot, guild_id, regiment="KM", notes="", user_id=world.customer.id)
    await tickets.record_payment(bot, guild_id, fourth["number"], 100, world.staff.id)
    await tickets.mark_closed(bot, fourth, world.staff.id, 1_700_000_100)


async def test_overview(world):
    await make_history(world)
    cog = world.bot.get_cog("Tickets")
    denied = make_interaction(world.bot, world.customer, channel=world.lobby)
    await cog.overview.callback(cog, denied)
    assert "Ticket staff" in denied.recorder.last[1]["content"]
    interaction = make_interaction(world.bot, world.staff, channel=world.lobby)
    await cog.overview.callback(cog, interaction)
    kind, payload = interaction.recorder.last
    assert payload["ephemeral"]
    embed = payload["embed"]
    text = embed.description
    assert text.index("**Open** (1)") < text.index("**In Production** (1)")
    assert "`0001` **KM** - 1000 Rare Alloys" in text and "<#111>" in text and f"<@{world.staff.id}>" in text
    assert "`0002` **km** - 100 Rare Alloys - unassigned - <#222>" in text
    assert "Navy" not in text
    assert embed.footer.text == "2 open ticket(s) - 1100 Rare Alloys total"


async def test_rares_summary(world):
    await make_history(world)
    cog = world.bot.get_cog("Tickets")
    interaction = make_interaction(world.bot, world.admin, channel=world.lobby)
    await cog.rares.callback(cog, interaction)
    embed = interaction.recorder.last[1]["embed"]
    assert "`0001` **KM** - paid 1000/1000 - paid in full" in embed.description
    assert "`0002` **km** - paid 50/100 - 50 owed" in embed.description
    values = fields(embed)
    assert values == {"Total Billed": "1100 Rare Alloys", "Total Paid": "1050 Rare Alloys", "Total Owed": "50 Rare Alloys"}
    assert embed.footer.text == "2 open ticket(s) - 2 closed"
    denied = make_interaction(world.bot, world.customer, channel=world.lobby)
    await cog.rares.callback(cog, denied)
    assert "Ticket staff" in denied.recorder.last[1]["content"]


async def test_leaderboard(world):
    await make_history(world)
    cog = world.bot.get_cog("Tickets")
    interaction = make_interaction(world.bot, world.staff, channel=world.lobby)
    await cog.leaderboard.callback(cog, interaction)
    embed = interaction.recorder.last[1]["embed"]
    assert embed.description.splitlines() == ["1. KM - 1150 Rare Alloys", "2. Navy - 150 Rare Alloys"]
    assert fields(embed) == {"Regiments": "2", "Total Spent": "1300 Rare Alloys"}
    assert "owed" not in embed.description.lower()
    denied = make_interaction(world.bot, world.customer, channel=world.lobby)
    await cog.leaderboard.callback(cog, denied)
    assert "Ticket staff" in denied.recorder.last[1]["content"]


async def test_mine(world):
    await make_history(world)
    cog = world.bot.get_cog("Tickets")
    interaction = make_interaction(world.bot, world.customer, channel=world.lobby)
    await cog.mine.callback(cog, interaction)
    kind, payload = interaction.recorder.last
    assert payload["ephemeral"]
    text = payload["embed"].description
    assert "**Open tickets**" in text and "**Closed tickets**" in text
    assert "`0001` **KM** - In Production - cost 1000, paid 1000, owed 0 Rare Alloys - <#111>" in text
    assert "`0003` **Navy** - closed" in text and "`0004`" in text
    assert "`0002`" not in text
    nobody = make_interaction(world.bot, world.admin, channel=world.lobby)
    await cog.mine.callback(cog, nobody)
    assert "have not opened any tickets" in nobody.recorder.last[1]["embed"].description


async def test_many_tickets_paginate_within_limits(world):
    tickets, bot, guild_id = world.tickets, world.bot, world.guild.id
    for index in range(120):
        row = await tickets.create(bot, guild_id, regiment=f"Regiment {index} " + "x" * 40, notes="", user_id=world.customer.id)
        await tickets.update(bot, row, channel_id=10_000 + index)
        await tickets.add_item(bot, guild_id, row["number"], "Repairs", 50, 1 + index % 5)
        await tickets.record_payment(bot, guild_id, row["number"], index, world.staff.id)
    cog = world.bot.get_cog("Tickets")
    for command in (cog.overview, cog.rares, cog.leaderboard, cog.mine):
        interaction = make_interaction(world.bot, world.customer if command is cog.mine else world.staff, channel=world.lobby)
        await command.callback(cog, interaction)
        payload = interaction.recorder.last[1]
        paginator = payload["view"]
        assert len(paginator.embeds) > 1
        assert all(len(embed) <= EMBED_TOTAL_LIMIT and len(embed.description) <= 4096 for embed in paginator.embeds)


async def test_card_stays_within_limits_with_many_lines(world):
    tickets, bot, guild_id = world.tickets, world.bot, world.guild.id
    row = await tickets.create(bot, guild_id, regiment="R" * 50, notes="n" * 1000, user_id=world.customer.id)
    for index in range(60):
        await tickets.add_item(bot, guild_id, row["number"], f"Very long service name number {index} " + "s" * 40, 100 + index, 25)
    summary = await tickets.summarize(bot, row)
    embed = tickets.card_embed(summary)
    assert all(len(field.value) <= EMBED_FIELD_LIMIT for field in embed.fields)
    assert len(embed) <= EMBED_TOTAL_LIMIT
    assert "more line(s)" in fields(embed)["Services"]
    closed = tickets.closed_embed(summary, world.staff.id, 1_700_000_000)
    assert len(closed) <= EMBED_TOTAL_LIMIT
    lines = await tickets.items(bot, guild_id, row["number"])
    view = tickets.RemoveServiceView(bot, guild_id, row["number"], lines, world.customer.id)
    assert len(find_select(view).options) == 25
    many = [{"name": f"Service {index}", "cost": index} for index in range(40)]
    panel = tickets.OrderServiceView(bot, guild_id, row["number"], many, world.customer.id)
    assert len(find_select(panel, 0).options) == 25
    assert "Only the first 25 services" in panel.content()


async def test_dynamic_ids_and_slugs(world):
    tickets = world.tickets
    view = tickets.ticket_view(10**19 - 1, 99999)
    assert all(len(item.custom_id) <= 100 for item in view.children)
    rows = {}
    for item in view.children:
        rows.setdefault(item.row, []).append(item)
    assert len(rows) <= 5 and all(len(items) <= 5 for items in rows.values())
    assert tickets.channel_name("[KM] Navy!!", 7) == "ticket-km-navy-0007"
    assert tickets.channel_name("!!!", 12, "Ship Build") == "ticket-ship-build-0012"
    assert tickets.channel_name("さみしい", 3) == "ticket-さみしい-0003"
    assert len(tickets.channel_name("x" * 200, 1, "y" * 200)) <= 100
    assert tickets.regiment_key("  K  M ") == tickets.regiment_key("k m")


async def test_service_helpers(world):
    tickets, bot, guild_id = world.tickets, world.bot, world.guild.id
    await tickets.set_service(bot, guild_id, "Ship Build", 275)
    assert await tickets.get_service(bot, guild_id, "Ship Build") == {"name": "Ship Build", "cost": 275}
    assert [row["name"] for row in await tickets.list_services(bot, guild_id)] == ["Repairs", "Ship Build"]
    assert await tickets.remove_service(bot, guild_id, "Repairs")
    assert not await tickets.remove_service(bot, guild_id, "Repairs")


async def test_unknown_or_missing_ticket_buttons(world):
    cls = world.tickets.TicketButton
    button = discord.ui.Button(label="x", custom_id=f"ft:order:{world.guild.id}:42")
    match = cls.__discord_ui_compiled_template__.fullmatch(button.custom_id)
    interaction = make_interaction(world.bot, world.customer, channel=world.lobby)
    item = await cls.from_custom_id(interaction, button, match)
    await item.callback(interaction)
    assert "no longer exists" in interaction.recorder.last[1]["content"]
    odd = cls("explode", world.guild.id, 1)
    interaction = make_interaction(world.bot, world.customer, channel=world.lobby)
    await odd.callback(interaction)
    assert "no longer supported" in interaction.recorder.last[1]["content"]


async def test_free_service_counts_as_ordered(world):
    await world.tickets.set_service(world.bot, world.guild.id, "Advice", 0)
    channel, card, _ = await opened(world)
    await order(world, card["view"], channel, world.customer, "Advice", 1)
    values = fields(last_card(world))
    assert values["Services"] == "1x Advice - 0 Rare Alloys"
    assert values["Rares Owed"] == "Paid in full"
    cog = world.bot.get_cog("Tickets")
    interaction = make_interaction(world.bot, world.staff, channel=world.lobby)
    await cog.rares.callback(cog, interaction)
    assert "paid 0/0 - paid in full" in interaction.recorder.last[1]["embed"].description
