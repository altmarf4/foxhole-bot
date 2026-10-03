from unittest.mock import MagicMock

import pytest

from fakes import add_role, find_select, make_bot, make_channel, make_guild, make_interaction, make_member, select_values
from foxbot.core import entries, timeutil
from foxbot.core.text import EMBED_TOTAL_LIMIT
from foxbot.core.ui import PickerView

ALL = [
    "foxbot.features.stockpiles",
    "foxbot.features.ships",
    "foxbot.features.msupps",
    "foxbot.features.mine",
]


async def build(extensions):
    bot = await make_bot(extensions)
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    logi = add_role(guild, "Logi")
    navy = add_role(guild, "Navy")
    engineers = add_role(guild, "Engineers")
    officers = add_role(guild, "Officers")
    await bot.perms.set_roles(guild.id, "stockpile", {logi.id})
    await bot.perms.set_roles(guild.id, "ship", {navy.id})
    await bot.perms.set_roles(guild.id, "msupps", {engineers.id})
    member = make_member(guild, name="Me", roles=[logi, navy, engineers, officers])
    other = make_member(guild, name="Other", roles=[logi, navy, engineers])
    channel = make_channel(guild, "logi")
    return bot, guild, member, other, channel, officers


@pytest.fixture
async def world():
    bot, guild, member, other, channel, officers = await build(ALL)
    yield bot, guild, member, other, channel, officers
    await bot.db.close()


def module(bot, name):
    return bot.extensions[f"foxbot.features.{name}"]


async def stockpile(bot, guild, user, **overrides):
    values = dict(
        name="KM1",
        hex_name="Piper's Enclave",
        region="Pan's Villa",
        store_type="Storage Depot",
        priority="Medium",
        access_code="861781",
        private=False,
        user_id=user.id,
    )
    values.update(overrides)
    return await module(bot, "stockpiles").create(bot, guild.id, **values)


async def ship(bot, guild, user, **overrides):
    values = dict(
        name="HMS Fox",
        ship_type="Destroyer",
        squad="Alpha",
        hex_name="Piper's Enclave",
        region="Syrinx Pass",
        parked_at="",
        notes="",
        user_id=user.id,
    )
    values.update(overrides)
    return await module(bot, "ships").create(bot, guild.id, **values)


async def base(bot, guild, user, **overrides):
    values = dict(name="Villa BB", hex_name="Piper's Enclave", region="Pan's Villa", msupps=1000, rate=50, user_id=user.id)
    values.update(overrides)
    return await module(bot, "msupps").create(bot, guild.id, **values)


async def run_mine(bot, member, channel):
    cog = bot.get_cog("Mine")
    interaction = make_interaction(bot, member, channel=channel)
    await cog.mine.callback(cog, interaction)
    return interaction.recorder.last


async def test_mine_lists_everything_the_member_is_involved_in(world):
    bot, guild, member, other, channel, officers = world
    added = await stockpile(bot, guild, member, name="Mine Added")
    refreshed = await stockpile(bot, guild, other, name="Other Refreshed", access_code="111111")
    await module(bot, "stockpiles").refresh_timer(bot, refreshed, member.id)
    shared = await stockpile(bot, guild, other, name="Shared Private", private=True, access_code="222222")
    await entries.set_access(bot.db, "stockpile", shared["id"], set(), {officers.id})
    await stockpile(bot, guild, other, name="Hidden Private", private=True, access_code="333333")
    await stockpile(bot, guild, other, name="Unrelated", access_code="444444")
    followed = await ship(bot, guild, other, name="Followed Ship")
    await entries.toggle_subscription(bot.db, "ship", followed["id"], member.id)
    await ship(bot, guild, other, name="Other Ship")
    measured = await base(bot, guild, other, name="Measured Base")
    await module(bot, "msupps").add_msupps(bot, measured, 100, member.id)
    own_base = await base(bot, guild, member, name="Own Base", rate=0)

    kind, payload = await run_mine(bot, member, channel)
    assert kind == "send" and payload["ephemeral"]
    assert payload["content"] == "You are involved in 6 tracked entries."
    embeds = payload["embeds"]
    assert [e.title for e in embeds] == ["Stockpiles (3)", "Ships (1)", "Msupps (2)"]
    stock_text = embeds[0].description
    assert "Mine Added" in stock_text and "(added)" in stock_text
    assert "Other Refreshed" in stock_text and "(refreshed)" in stock_text
    assert "[Private] **Shared Private**" in stock_text and "(access)" in stock_text
    assert "`222222`" in stock_text
    assert "Hidden Private" not in stock_text and "Unrelated" not in stock_text
    assert "expires <t:" in stock_text
    assert "Followed Ship" in embeds[1].description and "(notified)" in embeds[1].description
    assert "Other Ship" not in embeds[1].description
    assert "Measured Base" in embeds[2].description and "(measured)" in embeds[2].description
    assert "runs out <t:" in embeds[2].description
    assert "Own Base" in embeds[2].description and "rate not set" in embeds[2].description
    assert sum(len(e) for e in embeds) <= EMBED_TOTAL_LIMIT

    view = payload["view"]
    assert isinstance(view, PickerView)
    values = [o.value for o in view.options]
    assert len(values) == 6
    assert set(values) == {
        f"stockpile:{added['id']}",
        f"stockpile:{refreshed['id']}",
        f"stockpile:{shared['id']}",
        f"ship:{followed['id']}",
        f"msupps:{measured['id']}",
        f"msupps:{own_base['id']}",
    }
    assert values[-1] == f"msupps:{own_base['id']}"
    labels = {o.value: o.label for o in view.options}
    assert labels[f"ship:{followed['id']}"] == "Ship: Followed Ship - Syrinx Pass, Piper's Enclave"
    assert labels[f"msupps:{measured['id']}"].startswith("Base: Measured Base")

    select = find_select(view)
    for value, panel_class in (
        (f"stockpile:{shared['id']}", module(bot, "stockpiles").StockpilePanel),
        (f"ship:{followed['id']}", module(bot, "ships").ShipPanel),
        (f"msupps:{measured['id']}", module(bot, "msupps").MsuppsPanel),
    ):
        select_values(select, [value])
        pick = make_interaction(bot, member, channel=channel)
        await select.callback(pick)
        kind, opened = pick.recorder.last
        assert kind == "send" and opened["ephemeral"] and isinstance(opened["view"], panel_class)


async def test_mine_filters_by_current_permission(world):
    bot, guild, member, other, channel, officers = world
    await ship(bot, guild, member, name="Lost Access Ship")
    shared = await stockpile(bot, guild, other, name="Was Shared", private=True)
    await entries.set_access(bot.db, "stockpile", shared["id"], {member.id}, set())
    await entries.toggle_subscription(bot.db, "stockpile", shared["id"], member.id)
    await base(bot, guild, member, name="My Base")
    await bot.perms.set_roles(guild.id, "ship", set())
    await entries.set_access(bot.db, "stockpile", shared["id"], set(), set())
    kind, payload = await run_mine(bot, member, channel)
    assert [e.title for e in payload["embeds"]] == ["Msupps (1)"]
    assert "Lost Access Ship" not in payload["embeds"][0].description


async def test_admin_does_not_get_every_private_stockpile(world):
    bot, guild, _, other, channel, _ = world
    admin = make_member(guild, name="Admin", administrator=True)
    await stockpile(bot, guild, other, name="Secret", private=True)
    kind, payload = await run_mine(bot, admin, channel)
    assert "Nothing to show yet" in payload["content"]
    assert "view" not in payload


async def test_mine_empty(world):
    bot, guild, member, other, channel, _ = world
    await stockpile(bot, guild, other)
    kind, payload = await run_mine(bot, member, channel)
    assert kind == "send" and payload["ephemeral"]
    assert "Nothing to show yet" in payload["content"]


async def test_picked_entry_rechecks_permission(world):
    bot, guild, member, other, channel, _ = world
    row = await ship(bot, guild, member)
    kind, payload = await run_mine(bot, member, channel)
    select = find_select(payload["view"])
    await bot.perms.set_roles(guild.id, "ship", set())
    select_values(select, [f"ship:{row['id']}"])
    pick = make_interaction(bot, member, channel=channel)
    await select.callback(pick)
    assert "permission" in pick.recorder.last[1]["content"]
    await module(bot, "ships").delete(bot, row)
    await bot.perms.set_roles(guild.id, "ship", {guild.id})
    gone = make_interaction(bot, member, channel=channel)
    await select.callback(gone)
    assert "no longer exists" in gone.recorder.last[1]["content"]


async def test_many_entries_page_and_fit(world):
    bot, guild, member, other, channel, _ = world
    for index in range(30):
        await stockpile(bot, guild, member, name=f"Stockpile {index:02d} " + "s" * 50, access_code=f"{index:06d}")
    for index in range(40):
        await ship(bot, guild, member, name=f"Ship {index:02d} " + "w" * 50, parked_at="p" * 80)
    for index in range(3):
        await base(bot, guild, member, name=f"Base {index}")
    kind, payload = await run_mine(bot, member, channel)
    embeds = payload["embeds"]
    assert len(embeds) == 3 and len(embeds) <= 10
    assert sum(len(e) for e in embeds) <= EMBED_TOTAL_LIMIT
    assert all(len(e.description) <= 4096 for e in embeds)
    assert "Use the picker below." in embeds[0].description
    assert "Use the picker below." in embeds[1].description
    assert "Use the picker below." not in embeds[2].description
    view = payload["view"]
    assert len(view.options) == 73
    assert view.page_count == 3
    select = find_select(view)
    assert len(select.options) == 25
    assert all(len(o.label) <= 100 and len(o.description or "") <= 100 for o in select.options)
    next_button = next(item for item in view.children if getattr(item, "label", None) == "Next")
    await next_button.callback(make_interaction(bot, member, channel=channel))
    assert view.page == 1
    assert find_select(view).options[0].value == view.options[25].value


async def test_works_without_stockpiles_loaded():
    bot, guild, member, other, channel, _ = await build(["foxbot.features.ships", "foxbot.features.mine"])
    try:
        now = timeutil.now()
        await bot.db.execute(
            "INSERT INTO stockpiles (id, guild_id, name, hex, region, store_type, priority, code, expires_at, private, "
            "created_by, created_at, updated_at) VALUES ('AAAAAA', ?, 'Orphan', 'Onyx', '', 'Seaport', 'Low', '1', ?, 0, ?, ?, ?)",
            (guild.id, now + 3600, member.id, now, now),
        )
        row = await ship(bot, guild, member)
        kind, payload = await run_mine(bot, member, channel)
        assert [e.title for e in payload["embeds"]] == ["Ships (1)"]
        assert [o.value for o in payload["view"].options] == [f"ship:{row['id']}"]
        mine = bot.extensions["foxbot.features.mine"]
        missing = make_interaction(bot, member, channel=channel)
        await mine.open_entry(bot, missing, "stockpile:AAAAAA")
        assert "not available" in missing.recorder.last[1]["content"]
        broken = make_interaction(bot, member, channel=channel)
        await mine.open_entry(bot, broken, "garbage")
        assert "not available" in broken.recorder.last[1]["content"]
    finally:
        await bot.db.close()
