import asyncio
from pathlib import Path

import aiosqlite
import discord
import pytest

from fakes import add_role, make_bot, make_guild, make_member
from foxbot.core import settings as keys
from foxbot.core.alerts import crossed_thresholds
from foxbot.core.catalog import Catalog
from foxbot.core.locations import SEED_PATH, LocationService
from foxbot.core.locpicker import group_label, letter_groups
from foxbot.core.text import EMBED_TOTAL_LIMIT, chunk_lines, clip, fit_board_embeds
from foxbot.db import MIGRATIONS, Database


@pytest.fixture
def locations() -> LocationService:
    service = LocationService(Path("/nonexistent"), "http://127.0.0.1:9")
    assert service._load_file(SEED_PATH)
    return service


@pytest.mark.parametrize(
    "text, hex_name, region",
    [
        ("Syrinx Pass", "Piper's Enclave", "Syrinx Pass"),
        ("synrix", "Piper's Enclave", "Syrinx Pass"),
        ("Fingers, Titancall", "The Fingers", "Titancall"),
        ("Titancall, The Fingers", "The Fingers", "Titancall"),
        ("Onyx/The Brink", "Onyx", "The Brink"),
        ("brink", "Onyx", "The Brink"),
        ("Azmar", "Tyrant Foothills", "Azmar"),
        ("deadlands", "Deadlands", ""),
    ],
)
def test_resolve_text(locations, text, hex_name, region):
    location = locations.resolve_text(text)
    assert (location.hex, location.region, location.known) == (hex_name, region, True)


def test_resolve_unknown_keeps_text(locations):
    location = locations.resolve_text("Our secret base")
    assert not location.known
    assert location.region == "Our secret base"


def test_resolve_hex_and_town(locations):
    location = locations.resolve("pipers enclave", "pans villa")
    assert (location.hex, location.region, location.known) == ("Piper's Enclave", "Pan's Villa", True)
    mismatch = locations.resolve("Onyx", "Syrinx Pass")
    assert not mismatch.known


def test_region_choices_filtered_by_hex(locations):
    choices = locations.region_choices("Piper's Enclave", "")
    names = {c.value for c in choices}
    assert "Syrinx Pass" in names and "Pan's Villa" in names
    assert all("(" not in c.name for c in choices)
    assert len(choices) <= 25


def test_hex_choices_limit(locations):
    assert len(locations.hex_choices("")) == 25
    assert [c.value for c in locations.hex_choices("piper")] == ["Piper's Enclave"]


def test_letter_groups_keep_letters_together_and_fit_a_select(locations):
    groups = letter_groups(locations.hex_names())
    assert [group_label(g) for g in groups] == ["A-H", "K-R", "S-W"]
    assert all(len(g) <= 25 for g in groups)
    assert sum(len(g) for g in groups) == len(locations.hex_names())
    crowded = [f"S{i:02d}" for i in range(30)] + ["T1"]
    assert all(len(g) <= 25 for g in letter_groups(crowded))
    assert sum(len(g) for g in letter_groups(crowded)) == 31
    assert letter_groups([]) == []


def test_catalog_resolves_crates_quotes_and_locales():
    catalog = Catalog()
    item, crated = catalog.resolve_line("Argenti r.II Rifle (Crate)")
    assert item.code == "RifleC" and crated
    item, crated = catalog.resolve_line("O’Brien V.101 Freeman")
    assert item.code == "ArmoredCar2LargeW" and not crated
    item, _ = catalog.resolve_line("Basis-Materialien (Kiste)")
    assert item.code == "Cloth"
    assert catalog.lookup("Rare Materials").code == "RareMaterials"
    assert catalog.resolve_line("Totally Unknown Thing (Crate)") == (None, False)


def test_catalog_search():
    catalog = Catalog()
    codes = [item.code for item in catalog.search("loughcaster")]
    assert "RifleW" in codes
    assert len(catalog.choices("")) == 25


def test_crossed_thresholds():
    assert crossed_thresholds([12, 6, 2, 1], 5 * 3600, set()) == [12, 6]
    assert crossed_thresholds([12, 6, 2, 1], 5 * 3600, {12.0}) == [6]
    assert crossed_thresholds([12, 6, 2, 1], 13 * 3600, set()) == []


def test_text_helpers():
    assert clip("abcdef", 5) == "ab..."
    chunks = chunk_lines(["a" * 10] * 10, 25)
    assert all(len(c) <= 25 for c in chunks)
    embeds = fit_board_embeds("T", ["x" * 200] * 100, colour=1, empty="e", footer="f", overflow_hint="more")
    assert sum(len(e) for e in embeds) <= EMBED_TOTAL_LIMIT
    assert "not shown" in embeds[-1].footer.text


async def test_database_migrates_and_settings_roundtrip():
    db = Database(":memory:")
    await db.connect()
    from foxbot.core.settings import SettingsStore

    store = SettingsStore(db)
    assert await store.get(1, keys.ALERT_THRESHOLDS) == [12.0, 6.0, 2.0, 1.0]
    await store.set(1, keys.ALERT_THRESHOLDS, [3, 1, 24])
    assert await store.alert_thresholds(1) == [24.0, 3.0, 1.0]
    store._cache.clear()
    assert await store.get(1, keys.ALERT_THRESHOLDS) == [3, 1, 24]
    version = await db.fetchval("PRAGMA user_version")
    assert version == len(MIGRATIONS)
    await db.close()


async def test_database_from_schema_v4_drops_logi_runs_and_facility_queue(tmp_path):
    path = tmp_path / "old.db"
    raw = await aiosqlite.connect(path)
    await raw.executescript("BEGIN;\n" + "\n".join(MIGRATIONS[:4]) + "\nPRAGMA user_version = 4;\nCOMMIT;")
    await raw.executescript(
        "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (1, 'logi', 10, 100), (1, 'facility', 10, 101), (1, 'orders', 10, 102);"
        "INSERT INTO logi_runs (id, guild_id, title, priority, status, ping_channel_id, ping_message_id, created_by, created_at, updated_at) "
        "VALUES ('A', 1, 'Run', 'High', 'open', 11, 103, 5, 1, 1);"
        "INSERT INTO permission_roles (guild_id, grp, role_id) VALUES (1, 'logi', 7), (1, 'orders', 7);"
        "INSERT INTO entry_subscribers (kind, entry_id, user_id) VALUES ('logi', 'A', 7);"
    )
    await raw.commit()
    await raw.close()
    db = Database(path)
    await db.connect()
    tables = {row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"logi_runs", "facility_queue"} & tables
    retired = await db.fetchall("SELECT channel_id, message_id FROM retired_messages ORDER BY message_id")
    assert [(row["channel_id"], row["message_id"]) for row in retired] == [(10, 100), (10, 101), (11, 103)]
    assert [row["kind"] for row in await db.fetchall("SELECT kind FROM boards")] == ["orders"]
    assert [row["grp"] for row in await db.fetchall("SELECT grp FROM permission_roles")] == ["orders"]
    assert await db.fetchval("SELECT COUNT(*) FROM entry_subscribers") == 0
    assert await db.fetchval("PRAGMA user_version") == len(MIGRATIONS)
    await db.close()


async def test_permissions():
    bot = await make_bot([])
    guild = make_guild(owner_id=1)
    logi = add_role(guild, "Logi")
    owner = make_member(guild, user_id=1)
    plain = make_member(guild)
    logi_member = make_member(guild, roles=[logi])
    admin = make_member(guild, administrator=True)
    assert await bot.perms.has(owner, "stockpile")
    assert await bot.perms.has(admin, "stockpile")
    assert not await bot.perms.has(plain, "stockpile")
    await bot.perms.set_roles(guild.id, "stockpile", {logi.id})
    assert await bot.perms.has(logi_member, "stockpile")
    assert not await bot.perms.has(plain, "stockpile")
    await bot.perms.set_roles(guild.id, "stockpile", {guild.id})
    assert await bot.perms.has(plain, "stockpile")
    admin_role = add_role(guild, "Officer")
    officer = make_member(guild, roles=[admin_role])
    await bot.settings.set(guild.id, keys.ADMIN_ROLE, admin_role.id)
    assert await bot.perms.is_admin(officer)
    await bot.db.close()


async def test_alert_reset_premarks_passed_thresholds():
    bot = await make_bot([])
    now = 1_000_000
    await bot.alerts.reset("stockpile", "ABC123", 5, now + 5 * 3600, now)
    rows = await bot.db.fetchall("SELECT threshold FROM alert_state WHERE entry_id = 'ABC123' ORDER BY threshold DESC")
    assert [r["threshold"] for r in rows] == [12.0, 6.0]
    await bot.db.close()


async def test_command_tree_is_valid_for_all_extensions():
    from foxbot.bot import EXTENSIONS

    available = []
    for name in EXTENSIONS:
        module_path = Path(__file__).resolve().parent.parent / (name.replace(".", "/") + ".py")
        package_path = Path(__file__).resolve().parent.parent / name.replace(".", "/") / "__init__.py"
        if module_path.exists() or package_path.exists():
            available.append(name)
    bot = await make_bot(available)
    payload = bot.command_payload()
    names = [command["name"] for command in payload]
    assert len(names) == len(set(names))

    def walk(options, depth=0):
        for opt in options:
            assert 1 <= len(opt["name"]) <= 32 and opt["name"] == opt["name"].lower()
            assert 1 <= len(opt.get("description", "x")) <= 100, opt["name"]
            if opt.get("options"):
                assert len(opt["options"]) <= 25
                walk(opt["options"], depth + 1)

    walk(payload)
    assert len(payload) <= 100
    await bot.db.close()


async def test_unknown_interaction_is_logged_as_warning(caplog):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from foxbot.core.ui import report_error

    response = MagicMock(status=404, reason="Not Found")
    error = discord.NotFound(response, {"code": 10062, "message": "Unknown interaction"})
    interaction = SimpleNamespace(response=MagicMock(), followup=MagicMock())
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    with caplog.at_level("WARNING", logger="foxbot.core.ui"):
        await report_error(interaction, error, "/settings")
    assert any(record.levelname == "WARNING" and "another copy of the bot" in record.getMessage() for record in caplog.records)
    assert not any(record.levelname == "ERROR" for record in caplog.records)
    interaction.response.send_message.assert_not_awaited()


async def test_command_sync_signature_matches_discord_round_trip():
    import discord as discord_module

    from foxbot.bot import EXTENSIONS, command_signature, discord_payload

    bot = await make_bot(EXTENSIONS)
    local = bot.command_payload()
    remote = discord_payload(
        [
            discord_module.app_commands.AppCommand(
                data=dict(command, id=str(9000 + index), application_id="1", version="1"), state=None
            )
            for index, command in enumerate(local)
        ]
    )
    assert command_signature(remote) == command_signature(local)
    assert command_signature(remote[1:]) != command_signature(local)
    changed = [dict(local[0], description="Something else"), *local[1:]]
    assert command_signature(changed) != command_signature(local)
    await bot.db.close()


def test_instance_lock_blocks_a_second_copy(tmp_path):
    from foxbot.core.instancelock import InstanceLock

    first = InstanceLock(tmp_path / "foxbot.lock")
    second = InstanceLock(tmp_path / "foxbot.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


async def test_board_refresh_skips_unchanged_boards():
    from unittest.mock import AsyncMock, MagicMock

    from fakes import make_channel

    bot = await make_bot(["foxbot.features.stockpiles"])
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    channel = make_channel(guild)
    await bot.boards.post(guild, "stockpile", channel)
    edit = AsyncMock()
    partial = MagicMock()
    partial.get_partial_message.return_value = MagicMock(edit=edit)
    bot.get_partial_messageable = MagicMock(return_value=partial)
    await bot.boards.refresh(guild.id, "stockpile")
    assert edit.await_count == 0
    stockpiles = bot.extensions["foxbot.features.stockpiles"]
    await stockpiles.create(
        bot, guild.id, name="KM1", hex_name="Onyx", region="The Brink", store_type="Seaport",
        priority="High", access_code="123456", private=False, user_id=5,
    )
    await bot.boards.refresh(guild.id, "stockpile")
    await bot.boards.refresh(guild.id, "stockpile")
    assert edit.await_count == 1
    await bot.db.close()
