from unittest.mock import MagicMock

import pytest

from fakes import make_bot, make_guild
from foxbot.bot import EXTENSIONS
from foxbot.core.permissions import GROUPS

KEPT_TABLES = {
    "guild_settings",
    "permission_roles",
    "boards",
    "rares_stock",
    "rares_log",
    "ticket_services",
    "tickets",
    "ticket_items",
    "ticket_payments",
    "war_archives",
}
ENTRY_TABLES = ("entry_access", "entry_subscribers", "entry_alert_roles", "entry_screenshots", "alert_state", "alert_messages")


@pytest.fixture
async def bot():
    bot = await make_bot(list(EXTENSIONS))
    yield bot
    await bot.db.close()


def module(bot, name):
    return bot.extensions[f"foxbot.features.{name}"]


async def guild_tables(bot):
    tables = await bot.db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    found = set()
    for row in tables:
        columns = await bot.db.fetchall(f"PRAGMA table_info({row['name']})")
        if any(column["name"] == "guild_id" for column in columns):
            found.add(row["name"])
    return found


async def test_every_guild_table_is_archived_or_kept(bot):
    war = module(bot, "war")
    archived = {spec.table for spec in war.WAR_TABLES}
    assert set(war.DELETE_ORDER) == archived
    assert not archived & KEPT_TABLES
    assert await guild_tables(bot) == archived | KEPT_TABLES
    for spec in war.WAR_TABLES:
        if spec.module is not None:
            assert spec.module in bot.extensions
            assert war.feature_kind(spec) == bot.extensions[spec.module].KIND


async def seed(bot, guild_id, user_id):
    logi = module(bot, "logi")
    facility = module(bot, "facility")
    route = dict(pickup_hex="", pickup_region="", dest_hex="Deadlands", dest_region="Callahan's Gate")
    kept = await logi.create(bot, guild_id, title="Bmats", cargo="60 Bmats", priority="High", notes="", user_id=user_id, **route)
    cancelled = await logi.create(bot, guild_id, title="Shirts", cargo="20 crates", priority="Low", notes="", user_id=user_id, **route)
    assert await logi.cancel(bot, cancelled, user_id) is not None
    await bot.db.execute("UPDATE logi_runs SET ping_channel_id = 11, ping_message_id = ? WHERE id = ?", (guild_id * 10 + 1, kept["id"]))
    entry = await facility.create(bot, guild_id, item="Tank", item_code=None, quantity=2, facility="", notes="", user_id=user_id)
    assert await facility.start_work(bot, entry, user_id) is not None
    await facility.create(bot, guild_id, item="Shells", item_code=None, quantity=9, facility="", notes="", user_id=user_id)
    for kind, entry_id in (("logi", kept["id"]), ("facility", entry["id"])):
        async with bot.db.transaction() as tx:
            await tx.execute("INSERT INTO entry_access (kind, entry_id, target_type, target_id) VALUES (?, ?, 'user', 7)", (kind, entry_id))
            await tx.execute("INSERT INTO entry_subscribers (kind, entry_id, user_id) VALUES (?, ?, 7)", (kind, entry_id))
            await tx.execute("INSERT INTO entry_alert_roles (kind, entry_id, role_id) VALUES (?, ?, 8)", (kind, entry_id))
            await tx.execute(
                "INSERT INTO entry_screenshots (kind, entry_id, filename, content_type, data, uploaded_by, uploaded_at) "
                "VALUES (?, ?, 'a.png', 'image/png', x'00', 1, 1)",
                (kind, entry_id),
            )
            await tx.execute("INSERT INTO alert_state (kind, entry_id, threshold, sent_at) VALUES (?, ?, 1.0, 1)", (kind, entry_id))
            await tx.execute(
                "INSERT INTO alert_messages (kind, entry_id, channel_id, message_id, created_at) VALUES (?, ?, 12, ?, 1)",
                (kind, entry_id, guild_id * 10 + (2 if kind == "logi" else 3)),
            )


async def entry_rows(bot):
    return {table: await bot.db.fetchval(f"SELECT COUNT(*) FROM {table}", (), 0) for table in ENTRY_TABLES}


async def test_archive_removes_logi_runs_and_facility_queue_with_their_rows(bot):
    war = module(bot, "war")
    ours, theirs = make_guild(), make_guild()
    bot.get_guild = MagicMock(side_effect=lambda gid: {ours.id: ours, theirs.id: theirs}.get(gid))
    await seed(bot, ours.id, 5)
    await seed(bot, theirs.id, 6)
    assert await entry_rows(bot) == {table: 4 for table in ENTRY_TABLES}

    result = await war.archive_guild(bot, ours.id, 127, 5)

    assert result.counts["logi_runs"] == 2 and result.counts["facility_queue"] == 2
    assert set(result.messages) == {(11, ours.id * 10 + 1), (12, ours.id * 10 + 2), (12, ours.id * 10 + 3)}
    for table in ("logi_runs", "facility_queue"):
        assert await bot.db.fetchval(f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (ours.id,)) == 0
        assert await bot.db.fetchval(f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (theirs.id,)) == 2
    assert await entry_rows(bot) == {table: 2 for table in ENTRY_TABLES}
    positions = await bot.db.fetchall("SELECT position FROM facility_queue WHERE guild_id = ? ORDER BY position", (theirs.id,))
    assert [row["position"] for row in positions] == [1, 2]
    assert await bot.db.fetchval("SELECT cancelled_by FROM logi_runs WHERE guild_id = ? AND status = 'cancelled'", (theirs.id,)) == 6


def test_new_features_are_wired_into_shared_panels(bot):
    admin = module(bot, "admin")
    help_module = module(bot, "help")
    mine = module(bot, "mine")
    assert GROUPS["logi"] == "Logi runs" and GROUPS["facility"] == "Facility queue"
    assert {"logi", "facility"} <= set(admin.ACCESS_GROUPS)
    assert admin.ALL_ACCESS_GROUPS == f"all {len(admin.ACCESS_GROUPS)} groups"
    assert "war" in admin.SECTIONS
    assert "logi" in mine.FEATURES_BY_KIND
    assert {"war", "logi", "facility"} <= set(bot.boards.providers)
    titles = {page.title.split(" (")[0]: page for page in help_module.build_pages(bot)}
    for name in ("war", "logi", "facility"):
        assert titles[f"/{name}"].description.startswith(help_module.INTROS[name])
    assert "war alerts" in help_module.INTROS["settings"]
    assert "logi runs" in help_module.INTROS["mine"]
