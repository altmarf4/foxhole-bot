import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui.select import BaseSelect

from fakes import (
    add_role,
    find_button,
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
from foxbot.core.permissions import GROUPS


@pytest.fixture
async def world(tmp_path):
    bot = await make_bot(["foxbot.features.admin", "foxbot.features.stockpiles"], tmp_path)
    admin = bot.extensions["foxbot.features.admin"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    owner = make_member(guild, user_id=1, name="Owner")
    officer = add_role(guild, "Officer")
    logi = add_role(guild, "Logi")
    plain = make_member(guild, name="Plain")
    channel = make_channel(guild, "logi")
    voice = make_channel(guild, "rares-voice")
    yield SimpleNamespace(
        bot=bot,
        admin=admin,
        guild=guild,
        owner=owner,
        plain=plain,
        officer=officer,
        logi=logi,
        channel=channel,
        voice=voice,
        tmp=tmp_path,
    )
    await bot.db.close()


def selects(view):
    return [item for item in view.children if isinstance(item, BaseSelect)]


def select_of(view, kind, index=0):
    found = [item for item in view.children if isinstance(item, kind)]
    return found[index]


def last(interaction):
    return interaction.recorder.last


async def open_settings(w, member=None):
    member = member or w.owner
    cog = w.bot.get_cog("Admin")
    interaction = make_interaction(w.bot, member, channel=w.channel)
    await cog.settings_command.callback(cog, interaction)
    return interaction


async def pick(w, view, component, values, member=None):
    select_values(component, values)
    interaction = make_interaction(w.bot, member or w.owner, channel=w.channel)
    await component.callback(interaction)
    return interaction


async def click(w, view, label, member=None):
    interaction = make_interaction(w.bot, member or w.owner, channel=w.channel)
    await find_button(view, label).callback(interaction)
    return interaction


async def submit(w, modal, member=None):
    interaction = make_interaction(w.bot, member or w.owner, channel=w.channel)
    await modal.on_submit(interaction)
    return interaction


async def go_to_section(w, panel, key):
    section_select = select_of(panel, discord.ui.Select, 0)
    interaction = await pick(w, panel, section_select, [key])
    assert last(interaction)[0] == "edit"
    return last(interaction)[1]


def assert_modal_limits(modal):
    assert len(modal.children) <= 5
    assert len(modal.title) <= 45
    for child in modal.children:
        if isinstance(child, discord.ui.Label):
            assert len(child.text) <= 45
            assert child.description is None or len(child.description) <= 100
            component = child.component
            if isinstance(component, discord.ui.Select):
                assert len(component.options) <= 25


def assert_view_limits(view):
    rows = {}
    for item in view.children:
        rows.setdefault(item.row, []).append(item)
        if isinstance(item, discord.ui.Button):
            assert len(item.label) <= 80
        if isinstance(item, discord.ui.Select):
            assert len(item.options) <= 25
            assert all(len(o.label) <= 100 and (o.description is None or len(o.description) <= 100) for o in item.options)
    assert all(row is None or 0 <= row <= 4 for row in rows)
    for row, items in rows.items():
        if any(isinstance(item, BaseSelect) for item in items):
            assert len(items) == 1
        else:
            assert len(items) <= 5


async def test_settings_requires_admin(world):
    w = world
    interaction = await open_settings(w, w.plain)
    kind, payload = last(interaction)
    assert kind == "send" and payload["ephemeral"]
    assert "Only bot admins" in payload["content"]
    assert "view" not in payload


async def test_settings_panel_opens_ephemeral_with_sections(world):
    w = world
    interaction = await open_settings(w)
    kind, payload = last(interaction)
    assert kind == "send" and payload["ephemeral"]
    assert payload["embed"].title == "Settings - General"
    panel = payload["view"]
    assert_view_limits(panel)
    section_select = select_of(panel, discord.ui.Select, 0)
    assert [o.value for o in section_select.options] == ["general", "stockpiles", "ships", "tickets", "rares", "orders", "war"]
    for key, title in [("stockpiles", "Stockpiles"), ("ships", "Ships"), ("tickets", "Tickets"), ("rares", "Rares"), ("orders", "Orders"), ("war", "War")]:
        payload = await go_to_section(w, panel, key)
        assert payload["embed"].title == f"Settings - {title}"
        assert len(payload["embed"]) <= 6000
        assert_view_limits(panel)
    general = await go_to_section(w, panel, "general")
    names = [field.name for field in general["embed"].fields]
    assert names == ["Admin role", "Logistics ping role", "Alerts channel", "Reminder thresholds", "Faction", "Backups"]
    assert "12h, 6h, 2h, 1h" in general["embed"].fields[3].value


async def test_general_modal_saves_and_prefills(world):
    w = world
    panel = last(await open_settings(w))[1]["view"]
    opened = await click(w, panel, "Edit General Settings")
    modal = opened.response.modal
    assert isinstance(modal, w.admin.GeneralModal)
    assert_modal_limits(modal)
    assert len(modal.children) == 5
    assert modal.thresholds_field.component.default == "12, 6, 2, 1"
    set_label_value(modal.admin_field, w.officer)
    set_label_value(modal.logistics_field, w.logi)
    set_label_value(modal.channel_field, w.channel)
    set_label_value(modal.thresholds_field, "24, 6h; 2 0.5")
    set_label_value(modal.faction_field, "Colonial")
    result = await submit(w, modal)
    kind, payload = last(result)
    assert kind == "edit"
    assert payload["content"].startswith("General settings saved.")
    assert payload["embed"].title == "Settings - General"
    bot, gid = w.bot, w.guild.id
    assert await bot.settings.get(gid, keys.ADMIN_ROLE) == w.officer.id
    assert await bot.settings.get(gid, keys.LOGISTICS_ROLE) == w.logi.id
    assert await bot.settings.get(gid, keys.ALERT_CHANNEL) == w.channel.id
    assert await bot.settings.alert_thresholds(gid) == [24.0, 6.0, 2.0, 0.5]
    assert await bot.settings.get(gid, keys.FACTION) == "colonial"
    reopened = (await click(w, panel, "Edit General Settings")).response.modal
    assert [d.id for d in reopened.admin_field.component.default_values] == [w.officer.id]
    assert [d.id for d in reopened.logistics_field.component.default_values] == [w.logi.id]
    assert [d.id for d in reopened.channel_field.component.default_values] == [w.channel.id]
    assert reopened.thresholds_field.component.default == "24, 6, 2, 0.5"
    assert [o.label for o in reopened.faction_field.component.options if o.default] == ["Colonial"]


async def test_general_modal_reports_problems_and_keeps_old_values(world):
    w = world
    bot, gid = w.bot, w.guild.id
    await bot.settings.set(gid, keys.ADMIN_ROLE, w.officer.id)
    panel = last(await open_settings(w))[1]["view"]
    modal = (await click(w, panel, "Edit General Settings")).response.modal
    set_label_value(modal.admin_field, w.guild.default_role)
    set_label_value(modal.logistics_field, w.guild.default_role)
    set_label_value(modal.channel_field, [])
    set_label_value(modal.thresholds_field, "12, soon")
    set_label_value(modal.faction_field, "Not set")
    payload = last(await submit(w, modal))[1]
    assert "@everyone cannot be the admin role" in payload["content"]
    assert "@everyone cannot be the logistics ping role" in payload["content"]
    assert "Reminder thresholds were not changed" in payload["content"]
    assert await bot.settings.get(gid, keys.ADMIN_ROLE) == w.officer.id
    assert await bot.settings.get(gid, keys.LOGISTICS_ROLE) is None
    assert await bot.settings.alert_thresholds(gid) == [12.0, 6.0, 2.0, 1.0]
    assert await bot.settings.get(gid, keys.FACTION) is None


async def test_empty_thresholds_restore_defaults_and_cleared_roles(world):
    w = world
    bot, gid = w.bot, w.guild.id
    await bot.settings.set(gid, keys.ALERT_THRESHOLDS, [3.0])
    await bot.settings.set(gid, keys.LOGISTICS_ROLE, w.logi.id)
    panel = last(await open_settings(w))[1]["view"]
    modal = (await click(w, panel, "Edit General Settings")).response.modal
    set_label_value(modal.admin_field, [])
    set_label_value(modal.logistics_field, [])
    set_label_value(modal.channel_field, [])
    set_label_value(modal.thresholds_field, "")
    set_label_value(modal.faction_field, "Warden")
    await submit(w, modal)
    assert await bot.settings.alert_thresholds(gid) == [12.0, 6.0, 2.0, 1.0]
    assert await bot.settings.get(gid, keys.LOGISTICS_ROLE) is None
    assert await bot.settings.get(gid, keys.FACTION) == "warden"


async def test_alert_channel_warning_when_bot_cannot_post(world):
    w = world
    w.channel.permissions_for = MagicMock(return_value=discord.Permissions(view_channel=True))
    panel = last(await open_settings(w))[1]["view"]
    modal = (await click(w, panel, "Edit General Settings")).response.modal
    set_label_value(modal.channel_field, w.channel)
    set_label_value(modal.faction_field, "Not set")
    payload = last(await submit(w, modal))[1]
    assert "Send Messages" in payload["content"] and "Embed Links" in payload["content"]
    assert await w.bot.settings.get(w.guild.id, keys.ALERT_CHANNEL) == w.channel.id


async def test_admin_role_change_warns_when_editor_loses_admin(world):
    w = world
    bot, gid = w.bot, w.guild.id
    officer_member = make_member(w.guild, name="Officer", roles=[w.officer])
    await bot.settings.set(gid, keys.ADMIN_ROLE, w.officer.id)
    panel = last(await open_settings(w, officer_member))[1]["view"]
    modal = (await click(w, panel, "Edit General Settings", officer_member)).response.modal
    set_label_value(modal.admin_field, w.logi)
    set_label_value(modal.faction_field, "Not set")
    payload = last(await submit(w, modal, officer_member))[1]
    assert "no longer a bot admin" in payload["content"]
    denied = await click(w, panel, "Back Up Now", officer_member)
    assert "Only bot admins" in last(denied)[1]["content"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("12, 6, 2, 1", [12.0, 6.0, 2.0, 1.0]),
        ("1 1 2", [2.0, 1.0]),
        ("0.5; 48h", [48.0, 0.5]),
        ("  3  ", [3.0]),
    ],
)
def test_parse_thresholds_valid(world, text, expected):
    assert world.admin.parse_thresholds(text) == expected


@pytest.mark.parametrize("text", ["", "-1", "0", "abc", "nan", "inf", "1000", ", ".join(str(n) for n in range(1, 12))])
def test_parse_thresholds_invalid(world, text):
    with pytest.raises(ValueError):
        world.admin.parse_thresholds(text)


async def test_new_thresholds_do_not_fire_old_reminders(world):
    w = world
    bot, gid = w.bot, w.guild.id
    stockpiles = bot.extensions["foxbot.features.stockpiles"]
    bot.boards.channel_id_for = AsyncMock(return_value=w.channel.id)
    entry = await stockpiles.create(
        bot,
        gid,
        name="KM1",
        hex_name="Piper's Enclave",
        region="Pan's Villa",
        store_type="Storage Depot",
        priority="Medium",
        access_code="111111",
        private=False,
        user_id=w.owner.id,
    )
    panel = last(await open_settings(w))[1]["view"]
    modal = (await click(w, panel, "Edit General Settings")).response.modal
    set_label_value(modal.thresholds_field, "60, 12, 6, 2, 1")
    set_label_value(modal.faction_field, "Not set")
    await submit(w, modal)
    rows = await bot.db.fetchall("SELECT threshold FROM alert_state WHERE entry_id = ?", (entry["id"],))
    assert [row["threshold"] for row in rows] == [60.0]
    await bot.alerts.check(now=timeutil.now() + 60)
    assert w.channel.sent == []
    await bot.alerts.check(now=timeutil.now() + 39 * 3600)
    assert len(w.channel.sent) == 1


async def test_backup_now_reports_file_name(world):
    w = world
    panel = last(await open_settings(w))[1]["view"]
    interaction = await click(w, panel, "Back Up Now")
    assert interaction.recorder.kinds() == ["defer", "edit_original"]
    payload = last(interaction)[1]
    name = f"foxbot-{dt.date.today().isoformat()}.db"
    assert f"`{name}`" in payload["content"]
    assert (w.tmp / "backups" / name).exists()
    assert "file" not in payload and "files" not in payload and "attachments" not in payload


async def test_backup_failure_is_reported(world):
    w = world
    w.bot.backups.backup_now = AsyncMock(side_effect=OSError("disk full"))
    panel = last(await open_settings(w))[1]["view"]
    payload = last(await click(w, panel, "Back Up Now"))[1]
    assert "backup failed" in payload["content"]


async def test_storage_types_add_remove_reset(world):
    w = world
    bot, gid = w.bot, w.guild.id
    panel = last(await open_settings(w))[1]["view"]
    payload = await go_to_section(w, panel, "stockpiles")
    assert "Seaport" in payload["embed"].description
    opened = await click(w, panel, "Add Storage Type")
    modal = opened.response.modal
    assert_modal_limits(modal)
    set_label_value(modal.value_field, "  Bunker   Base ")
    payload = last(await submit(w, modal))[1]
    assert "Added **Bunker Base**" in payload["content"]
    assert await bot.settings.get(gid, keys.STORAGE_TYPES) == ["Storage Depot", "Seaport", "Aircraft Depot", "Bunker Base"]
    duplicate = (await click(w, panel, "Add Storage Type")).response.modal
    set_label_value(duplicate.value_field, "bunker base")
    payload = last(await submit(w, duplicate))[1]
    assert "already in the list" in payload["content"]
    remove = select_of(panel, discord.ui.Select, 1)
    assert [o.value for o in remove.options] == ["Storage Depot", "Seaport", "Aircraft Depot", "Bunker Base"]
    payload = last(await pick(w, panel, remove, ["Seaport"]))[1]
    assert "Removed **Seaport**" in payload["content"]
    for value in ["Storage Depot", "Aircraft Depot"]:
        await pick(w, panel, select_of(panel, discord.ui.Select, 1), [value])
    assert await bot.settings.get(gid, keys.STORAGE_TYPES) == ["Bunker Base"]
    payload = last(await pick(w, panel, select_of(panel, discord.ui.Select, 1), ["Bunker Base"]))[1]
    assert "Keep at least one" in payload["content"]
    assert await bot.settings.get(gid, keys.STORAGE_TYPES) == ["Bunker Base"]
    await click(w, panel, "Reset to Defaults")
    assert await bot.settings.get(gid, keys.STORAGE_TYPES) == ["Storage Depot", "Seaport", "Aircraft Depot"]


async def test_list_values_capped_at_25(world):
    w = world
    bot, gid = w.bot, w.guild.id
    await bot.settings.set(gid, keys.SHIP_TYPES, [f"Ship {n}" for n in range(25)])
    panel = last(await open_settings(w))[1]["view"]
    await go_to_section(w, panel, "ships")
    assert len(select_of(panel, discord.ui.Select, 1).options) == 25
    assert_view_limits(panel)
    modal = (await click(w, panel, "Add Ship Type")).response.modal
    set_label_value(modal.value_field, "Frigate")
    payload = last(await submit(w, modal))[1]
    assert "at most 25" in payload["content"]
    assert len(await bot.settings.get(gid, keys.SHIP_TYPES)) == 25


async def test_ticket_services_add_update_remove(world):
    w = world
    bot, gid = w.bot, w.guild.id
    panel = last(await open_settings(w))[1]["view"]
    payload = await go_to_section(w, panel, "tickets")
    assert "No services yet" in payload["embed"].description
    assert len(selects(panel)) == 1
    modal = (await click(w, panel, "Add or Update Service")).response.modal
    assert_modal_limits(modal)
    set_label_value(modal.name_field, "Tank Production")
    set_label_value(modal.cost_field, "1,500")
    payload = last(await submit(w, modal))[1]
    assert "Added **Tank Production**: 1,500 Rare Alloys" in payload["content"]
    assert "Tank Production" in payload["embed"].description
    update = (await click(w, panel, "Add or Update Service")).response.modal
    set_label_value(update.name_field, "tank production")
    set_label_value(update.cost_field, "75")
    payload = last(await submit(w, update))[1]
    assert payload["content"].startswith("Updated")
    rows = await bot.db.fetchall("SELECT name, cost FROM ticket_services WHERE guild_id = ?", (gid,))
    assert rows == [{"name": "tank production", "cost": 75}]
    for bad in ["-5", "abc", "1.5", "2000000"]:
        broken = (await click(w, panel, "Add or Update Service")).response.modal
        set_label_value(broken.name_field, "Free")
        set_label_value(broken.cost_field, bad)
        payload = last(await submit(w, broken))[1]
        assert payload["content"].startswith("Nothing was saved")
    free = (await click(w, panel, "Add or Update Service")).response.modal
    set_label_value(free.name_field, "Advice")
    set_label_value(free.cost_field, "0")
    await submit(w, free)
    remove = select_of(panel, discord.ui.Select, 1)
    assert [o.value for o in remove.options] == ["Advice", "tank production"]
    payload = last(await pick(w, panel, remove, ["tank production"]))[1]
    assert "Removed **tank production**" in payload["content"]
    rows = await bot.db.fetchall("SELECT name, cost FROM ticket_services WHERE guild_id = ?", (gid,))
    assert rows == [{"name": "Advice", "cost": 0}]


async def test_ticket_services_capped_at_25(world):
    w = world
    bot, gid = w.bot, w.guild.id
    for n in range(25):
        await bot.db.execute("INSERT INTO ticket_services (guild_id, name, cost) VALUES (?, ?, ?)", (gid, f"Service {n:02d}", n))
    panel = last(await open_settings(w))[1]["view"]
    payload = await go_to_section(w, panel, "tickets")
    assert len(payload["embed"]) <= 6000
    assert len(select_of(panel, discord.ui.Select, 1).options) == 25
    modal = (await click(w, panel, "Add or Update Service")).response.modal
    set_label_value(modal.name_field, "One Too Many")
    set_label_value(modal.cost_field, "5")
    payload = last(await submit(w, modal))[1]
    assert "at most 25" in payload["content"]
    update = (await click(w, panel, "Add or Update Service")).response.modal
    set_label_value(update.name_field, "Service 03")
    set_label_value(update.cost_field, "99")
    payload = last(await submit(w, update))[1]
    assert payload["content"].startswith("Updated")


async def test_ticket_category_and_log_reset(world):
    w = world
    bot, gid = w.bot, w.guild.id
    category = make_channel(w.guild, "Tickets")
    await bot.settings.set(gid, keys.TICKET_CATEGORY, category.id)
    await bot.settings.set(gid, keys.TICKET_LOG_CHANNEL, w.channel.id)
    panel = last(await open_settings(w))[1]["view"]
    payload = await go_to_section(w, panel, "tickets")
    assert payload["embed"].fields[0].value == "**Tickets**"
    payload = last(await click(w, panel, "Reset Ticket Category"))[1]
    assert "Ticket category reset" in payload["content"]
    assert await bot.settings.get(gid, keys.TICKET_CATEGORY) is None
    payload = last(await click(w, panel, "Reset Log Channel"))[1]
    assert "Ticket log channel reset" in payload["content"]
    assert await bot.settings.get(gid, keys.TICKET_LOG_CHANNEL) is None


async def test_rares_channels_set_and_clear(world):
    w = world
    bot, gid = w.bot, w.guild.id
    panel = last(await open_settings(w))[1]["view"]
    await go_to_section(w, panel, "rares")
    modal = (await click(w, panel, "Set Channels")).response.modal
    assert_modal_limits(modal)
    assert modal.log_field.component.channel_types == [discord.ChannelType.text, discord.ChannelType.news]
    assert modal.voice_field.component.channel_types == [discord.ChannelType.voice]
    set_label_value(modal.log_field, w.channel)
    set_label_value(modal.voice_field, w.voice)
    payload = last(await submit(w, modal))[1]
    assert payload["content"].startswith("Rare Alloys channels saved.")
    assert await bot.settings.get(gid, keys.RARES_LOG_CHANNEL) == w.channel.id
    assert await bot.settings.get(gid, keys.RARES_VOICE_CHANNEL) == w.voice.id
    reopened = (await click(w, panel, "Set Channels")).response.modal
    assert [d.id for d in reopened.voice_field.component.default_values] == [w.voice.id]
    await click(w, panel, "Clear Voice Channel")
    assert await bot.settings.get(gid, keys.RARES_VOICE_CHANNEL) is None
    await click(w, panel, "Clear Log Channel")
    assert await bot.settings.get(gid, keys.RARES_LOG_CHANNEL) is None


async def test_rares_voice_warns_without_manage_channels(world):
    w = world
    w.voice.permissions_for = MagicMock(return_value=discord.Permissions(view_channel=True, connect=True))
    panel = last(await open_settings(w))[1]["view"]
    await go_to_section(w, panel, "rares")
    modal = (await click(w, panel, "Set Channels")).response.modal
    set_label_value(modal.log_field, [])
    set_label_value(modal.voice_field, w.voice)
    payload = last(await submit(w, modal))[1]
    assert "Manage Channels" in payload["content"]


async def test_orders_channel_set_and_clear(world):
    w = world
    bot, gid = w.bot, w.guild.id
    panel = last(await open_settings(w))[1]["view"]
    await go_to_section(w, panel, "orders")
    modal = (await click(w, panel, "Set Orders Channel")).response.modal
    assert_modal_limits(modal)
    set_label_value(modal.channel_field, w.channel)
    payload = last(await submit(w, modal))[1]
    assert payload["content"] == "Orders channel saved."
    assert await bot.settings.get(gid, keys.ORDERS_CHANNEL) == w.channel.id
    payload = last(await click(w, panel, "Clear Orders Channel"))[1]
    assert payload["content"] == "Orders channel cleared."
    assert await bot.settings.get(gid, keys.ORDERS_CHANNEL) is None


async def test_war_alerts_set_and_clear(world):
    w = world
    bot, gid = w.bot, w.guild.id
    panel = last(await open_settings(w))[1]["view"]
    payload = await go_to_section(w, panel, "war")
    fields = {field.name: field.value for field in payload["embed"].fields}
    assert fields["Town change alerts"].startswith("**Off**")
    assert fields["War alert channel"].startswith("Not set.")
    assert_view_limits(panel)
    modal = (await click(w, panel, "Set War Alerts")).response.modal
    assert isinstance(modal, w.admin.WarAlertsModal)
    assert_modal_limits(modal)
    assert [o.label for o in modal.mode_field.component.options] == ["Off", "Our faction only", "All changes"]
    assert [o.label for o in modal.mode_field.component.options if o.default] == ["Off"]
    set_label_value(modal.mode_field, "Our faction only")
    set_label_value(modal.channel_field, w.channel)
    payload = last(await submit(w, modal))[1]
    assert payload["content"].startswith("War alerts saved.")
    assert "faction is not set" in payload["content"]
    assert await bot.settings.get(gid, keys.WAR_ALERT_MODE) == "ours"
    assert await bot.settings.get(gid, keys.WAR_ALERT_CHANNEL) == w.channel.id
    fields = {field.name: field.value for field in payload["embed"].fields}
    assert "faction is not set, so these alerts are off" in fields["Town change alerts"]
    assert fields["War alert channel"] == f"<#{w.channel.id}>"
    await bot.settings.set(gid, keys.FACTION, "colonial")
    reopened = (await click(w, panel, "Set War Alerts")).response.modal
    assert [o.label for o in reopened.mode_field.component.options if o.default] == ["Our faction only"]
    assert [d.id for d in reopened.channel_field.component.default_values] == [w.channel.id]
    set_label_value(reopened.mode_field, "All changes")
    set_label_value(reopened.channel_field, w.channel)
    payload = last(await submit(w, reopened))[1]
    assert payload["content"] == "War alerts saved."
    assert await bot.settings.get(gid, keys.WAR_ALERT_MODE) == "all"
    await bot.settings.set(gid, keys.WAR_ALERT_MODE, "ours")
    payload = last(await click(w, panel, "Clear War Alert Channel"))[1]
    assert payload["content"].startswith("War alert channel cleared.")
    assert await bot.settings.get(gid, keys.WAR_ALERT_CHANNEL) is None
    fields = {field.name: field.value for field in payload["embed"].fields}
    assert fields["Town change alerts"] == "**Our faction only**: towns the Colonials lost or took."
    off = (await click(w, panel, "Set War Alerts")).response.modal
    set_label_value(off.mode_field, "Off")
    set_label_value(off.channel_field, [])
    await submit(w, off)
    assert await bot.settings.get(gid, keys.WAR_ALERT_MODE) == "off"
    assert await bot.db.fetchval("SELECT COUNT(*) FROM guild_settings WHERE key = 'war_alert_mode'") == 0
    sneaky = w.admin.WarAlertsModal(panel, "off", None)
    set_label_value(sneaky.mode_field, "All changes")
    set_label_value(sneaky.channel_field, [])
    interaction = await submit(w, sneaky, w.plain)
    assert "Only bot admins" in last(interaction)[1]["content"]
    assert await bot.settings.get(gid, keys.WAR_ALERT_MODE) == "off"
    for label in ["Set War Alerts", "Clear War Alert Channel"]:
        interaction = await click(w, panel, label, w.plain)
        assert "Only bot admins" in last(interaction)[1]["content"]
        assert interaction.response.modal is None


async def test_settings_callbacks_recheck_admin(world):
    w = world
    bot, gid = w.bot, w.guild.id
    panel = w.admin.SettingsPanel(bot, gid, w.plain.id)
    await panel.send(make_interaction(bot, w.plain, channel=w.channel))
    for label in ["Edit General Settings", "Back Up Now"]:
        interaction = await click(w, panel, label, w.plain)
        assert "Only bot admins" in last(interaction)[1]["content"]
        assert interaction.response.modal is None
    modal = w.admin.ServiceModal(panel)
    set_label_value(modal.name_field, "Sneaky")
    set_label_value(modal.cost_field, "1")
    interaction = await submit(w, modal, w.plain)
    assert "Only bot admins" in last(interaction)[1]["content"]
    assert await bot.db.fetchall("SELECT * FROM ticket_services") == []
    section_select = select_of(panel, discord.ui.Select, 0)
    interaction = await pick(w, panel, section_select, ["tickets"], w.plain)
    assert "Only bot admins" in last(interaction)[1]["content"]
    assert panel.section == "general"


async def test_other_member_cannot_use_someone_elses_panel(world):
    w = world
    panel = last(await open_settings(w))[1]["view"]
    intruder = make_member(w.guild, name="Intruder", administrator=True)
    interaction = make_interaction(w.bot, intruder, channel=w.channel)
    assert not await panel.interaction_check(interaction)
    assert "belongs to someone else" in last(interaction)[1]["content"]


async def test_permissions_panel_lists_groups_and_updates(world):
    w = world
    bot, gid = w.bot, w.guild.id
    cog = bot.get_cog("Admin")
    interaction = make_interaction(bot, w.owner, channel=w.channel)
    await cog.permissions_command.callback(cog, interaction)
    kind, payload = last(interaction)
    assert kind == "send" and payload["ephemeral"]
    embed = payload["embed"]
    assert [field.name for field in embed.fields] == list(GROUPS.values())
    assert all(field.value == "Admins only" for field in embed.fields)
    assert "@everyone" in embed.description and "always pass" in embed.description
    panel = payload["view"]
    assert_view_limits(panel)
    group_select = select_of(panel, discord.ui.Select, 0)
    assert [o.value for o in group_select.options] == list(GROUPS)
    opened = await pick(w, panel, group_select, ["stockpile"])
    modal = opened.response.modal
    assert isinstance(modal, w.admin.PermissionModal)
    assert_modal_limits(modal)
    assert modal.roles_field.component.max_values == 25 and modal.roles_field.component.min_values == 0
    set_label_value(modal.roles_field, [w.logi, w.officer])
    set_label_value(modal.everyone_field, "No")
    payload = last(await submit(w, modal))[1]
    assert await bot.perms.roles_for(gid, "stockpile") == {w.logi.id, w.officer.id}
    assert "**Stockpiles** can now be used by" in payload["content"]
    stockpile_field = payload["embed"].fields[0]
    assert f"<@&{w.logi.id}>" in stockpile_field.value
    option = next(o for o in select_of(panel, discord.ui.Select, 0).options if o.value == "stockpile")
    assert option.description == "Officer, Logi" or option.description == "Logi, Officer"
    modal = (await pick(w, panel, select_of(panel, discord.ui.Select, 0), ["stockpile"])).response.modal
    assert {d.id for d in modal.roles_field.component.default_values} == {w.logi.id, w.officer.id}
    set_label_value(modal.roles_field, [])
    set_label_value(modal.everyone_field, "Yes")
    payload = last(await submit(w, modal))[1]
    assert await bot.perms.roles_for(gid, "stockpile") == {gid}
    assert payload["embed"].fields[0].value == "Everyone"
    assert await bot.perms.has(w.plain, "stockpile")
    modal = (await pick(w, panel, select_of(panel, discord.ui.Select, 0), ["stockpile"])).response.modal
    assert [o.label for o in modal.everyone_field.component.options if o.default] == ["Yes"]
    assert modal.roles_field.component.default_values == []


async def test_permissions_everyone_role_from_select_counts(world):
    w = world
    panel = w.admin.PermissionsPanel(w.bot, w.guild.id, w.owner.id)
    await panel.send(make_interaction(w.bot, w.owner, channel=w.channel))
    modal = (await pick(w, panel, select_of(panel, discord.ui.Select, 0), ["orders"])).response.modal
    set_label_value(modal.roles_field, [w.guild.default_role])
    set_label_value(modal.everyone_field, "No")
    await submit(w, modal)
    assert await w.bot.perms.roles_for(w.guild.id, "orders") == {w.guild.id}


async def test_permissions_denied_for_non_admin(world):
    w = world
    cog = w.bot.get_cog("Admin")
    interaction = make_interaction(w.bot, w.plain, channel=w.channel)
    await cog.permissions_command.callback(cog, interaction)
    assert "Only bot admins" in last(interaction)[1]["content"]
    panel = w.admin.PermissionsPanel(w.bot, w.guild.id, w.plain.id)
    await panel.send(make_interaction(w.bot, w.plain, channel=w.channel))
    denied = await pick(w, panel, select_of(panel, discord.ui.Select, 0), ["stockpile"], w.plain)
    assert denied.response.modal is None
    assert "Only bot admins" in last(denied)[1]["content"]
    modal = w.admin.PermissionModal(panel, w.guild, "stockpile", set())
    set_label_value(modal.roles_field, [w.logi])
    set_label_value(modal.everyone_field, "No")
    await submit(w, modal, w.plain)
    assert await w.bot.perms.roles_for(w.guild.id, "stockpile") == set()


async def open_wizard(w, member=None):
    cog = w.bot.get_cog("Admin")
    interaction = make_interaction(w.bot, member or w.owner, channel=w.channel)
    await cog.setup_command.callback(cog, interaction)
    return interaction


async def test_setup_wizard_full_flow(world):
    w = world
    bot, gid = w.bot, w.guild.id
    kind, payload = last(await open_wizard(w))
    assert kind == "send" and payload["ephemeral"]
    wizard = payload["view"]
    assert payload["embed"].title == "Server Setup - Step 1 of 7: Admin role"
    assert find_button(wizard, "Back").disabled
    assert_view_limits(wizard)

    await pick(w, wizard, select_of(wizard, discord.ui.RoleSelect), [w.officer])
    assert await bot.settings.get(gid, keys.ADMIN_ROLE) == w.officer.id
    payload = last(await click(w, wizard, "Next"))[1]
    assert payload["embed"].title.startswith("Server Setup - Step 2 of 7")

    payload = last(await pick(w, wizard, select_of(wizard, discord.ui.RoleSelect), [w.logi]))[1]
    assert "Logistics ping role set" in payload["content"]
    assert await bot.settings.get(gid, keys.LOGISTICS_ROLE) == w.logi.id
    assert [d.id for d in select_of(wizard, discord.ui.RoleSelect).default_values] == [w.logi.id]
    await click(w, wizard, "Next")

    channel_select = select_of(wizard, discord.ui.ChannelSelect)
    assert channel_select.channel_types == [discord.ChannelType.text, discord.ChannelType.news]
    await pick(w, wizard, channel_select, [w.channel])
    assert await bot.settings.get(gid, keys.ALERT_CHANNEL) == w.channel.id
    await click(w, wizard, "Next")

    faction_select = select_of(wizard, discord.ui.Select)
    assert [o.label for o in faction_select.options] == ["Warden", "Colonial", "Not set"]
    await pick(w, wizard, faction_select, ["Warden"])
    assert await bot.settings.get(gid, keys.FACTION) == "warden"
    await click(w, wizard, "Next")

    payload = last(await pick(w, wizard, select_of(wizard, discord.ui.RoleSelect), [w.officer]))[1]
    assert await bot.perms.roles_for(gid, "tickets") == {w.officer.id}
    assert "Ticket staff" in payload["content"]
    payload = last(await click(w, wizard, "Next"))[1]
    assert payload["embed"].title == "Server Setup - Step 6 of 7: Who can use the trackers"
    assert_view_limits(wizard)

    payload = last(await click(w, wizard, "Apply to All"))[1]
    assert "Pick roles first" in payload["content"]
    await pick(w, wizard, select_of(wizard, discord.ui.RoleSelect), [w.logi])
    assert await bot.perms.roles_for(gid, "stockpile") == set()
    payload = last(await click(w, wizard, "Apply to All"))[1]
    assert "Saved for all 8 groups" in payload["content"]
    for group in ["stockpile", "ship", "msupps", "orders", "inventory", "rares", "logi", "facility"]:
        assert await bot.perms.roles_for(gid, group) == {w.logi.id}
    assert await bot.perms.roles_for(gid, "tickets") == {w.officer.id}
    assert [d.id for d in select_of(wizard, discord.ui.RoleSelect).default_values] == [w.logi.id]
    payload = last(await click(w, wizard, "Next"))[1]
    assert payload["embed"].title == "Server Setup - Step 7 of 7: Post boards"
    assert find_button(wizard, "Finish")
    assert_view_limits(wizard)

    payload = last(await click(w, wizard, "Post Board"))[1]
    assert payload["content"] == "Pick a board first."
    board_select = select_of(wizard, discord.ui.Select)
    assert [(o.value, o.label, o.description) for o in board_select.options] == [("stockpile", "Stockpiles", "Not posted yet")]
    await pick(w, wizard, board_select, ["stockpile"])
    payload = last(await click(w, wizard, "Post Board"))[1]
    assert payload["content"] == "Pick a channel first."
    await pick(w, wizard, select_of(wizard, discord.ui.ChannelSelect), [w.channel])
    posted = await click(w, wizard, "Post Board")
    assert posted.recorder.kinds() == ["defer", "edit_original"]
    assert "**Stockpiles** board posted in" in last(posted)[1]["content"]
    assert len(w.channel.sent) == 1 and w.channel.sent[0]["embeds"]
    record = await bot.boards.record(gid, "stockpile")
    assert record["channel_id"] == w.channel.id
    assert select_of(wizard, discord.ui.Select).options[0].description == "Posted in #logi"

    payload = last(await click(w, wizard, "Finish"))[1]
    summary = payload["embed"]
    assert summary.title == "Server Setup - Summary"
    values = {field.name: field.value for field in summary.fields}
    assert values["Admin role"] == f"<@&{w.officer.id}>"
    assert values["Alerts channel"] == f"<#{w.channel.id}>"
    assert values["Faction"] == "Warden"
    assert f"posted in <#{w.channel.id}>" in values["Boards"]
    assert len(summary) <= 6000
    payload = last(await click(w, wizard, "Done"))[1]
    assert payload["view"] is None
    assert payload["embed"].title == "Server Setup - Summary"


async def test_setup_back_and_everyone(world):
    w = world
    bot, gid = w.bot, w.guild.id
    wizard = last(await open_wizard(w))[1]["view"]
    payload = last(await pick(w, wizard, select_of(wizard, discord.ui.RoleSelect), [w.guild.default_role]))[1]
    assert "@everyone cannot be the admin role" in payload["content"]
    assert await bot.settings.get(gid, keys.ADMIN_ROLE) is None
    for _ in range(5):
        await click(w, wizard, "Next")
    assert wizard.step == 5
    payload = last(await click(w, wizard, "Allow Everyone"))[1]
    assert "Every member" in payload["content"]
    for group in ["stockpile", "ship", "msupps", "orders", "inventory", "rares"]:
        assert await bot.perms.roles_for(gid, group) == {gid}
    assert await bot.perms.has(w.plain, "inventory")
    await click(w, wizard, "Back")
    assert wizard.step == 4
    await bot.perms.set_roles(gid, "tickets", {gid, w.logi.id})
    await click(w, wizard, "Back")
    await click(w, wizard, "Next")
    await pick(w, wizard, select_of(wizard, discord.ui.RoleSelect), [w.officer])
    assert await bot.perms.roles_for(gid, "tickets") == {gid, w.officer.id}


async def test_setup_post_board_permission_problems(world):
    w = world
    wizard = last(await open_wizard(w))[1]["view"]
    wizard.step = 6
    await wizard.show(make_interaction(w.bot, w.owner, channel=w.channel))
    await pick(w, wizard, select_of(wizard, discord.ui.Select), ["stockpile"])
    w.channel.permissions_for = MagicMock(return_value=discord.Permissions(view_channel=True))
    await pick(w, wizard, select_of(wizard, discord.ui.ChannelSelect), [w.channel])
    interaction = await click(w, wizard, "Post Board")
    payload = last(interaction)[1]
    assert "missing Send Messages, Embed Links" in payload["content"]
    assert w.channel.send.await_count == 0

    w.channel.permissions_for = MagicMock(return_value=discord.Permissions.all())
    w.channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access"))
    interaction = await click(w, wizard, "Post Board")
    assert "Discord refused to let me post" in last(interaction)[1]["content"]
    assert await w.bot.boards.record(w.guild.id, "stockpile") is None

    hidden = SimpleNamespace(id=424242)
    await pick(w, wizard, select_of(wizard, discord.ui.ChannelSelect), [hidden])
    interaction = await click(w, wizard, "Post Board")
    assert "cannot see <#424242>" in last(interaction)[1]["content"]


async def test_setup_requires_admin_everywhere(world):
    w = world
    interaction = await open_wizard(w, w.plain)
    assert "Only bot admins" in last(interaction)[1]["content"]
    wizard = w.admin.SetupWizard(w.bot, w.guild.id, w.plain.id)
    await wizard.send(make_interaction(w.bot, w.plain, channel=w.channel))
    denied = await pick(w, wizard, select_of(wizard, discord.ui.RoleSelect), [w.officer], w.plain)
    assert "Only bot admins" in last(denied)[1]["content"]
    assert await w.bot.settings.get(w.guild.id, keys.ADMIN_ROLE) is None
    denied = await click(w, wizard, "Next", w.plain)
    assert "Only bot admins" in last(denied)[1]["content"]
    assert wizard.step == 0
