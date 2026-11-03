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


def test_features_are_wired_into_shared_panels(bot):
    admin = module(bot, "admin")
    help_module = module(bot, "help")
    assert "logi" not in GROUPS and "facility" not in GROUPS
    assert admin.ALL_ACCESS_GROUPS == f"all {len(admin.ACCESS_GROUPS)} groups"
    assert "war" in admin.SECTIONS
    assert "war" in bot.boards.providers
    assert not {"logi", "facility"} & set(bot.boards.providers)
    titles = {page.title.split(" (")[0]: page for page in help_module.build_pages(bot)}
    assert titles["/war"].description.startswith(help_module.INTROS["war"])
    assert "war alerts" in help_module.INTROS["settings"]
