import codecs
import csv
import io
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
from foxbot.core.catalog import Catalog
from foxbot.core.locations import Location
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_TOTAL_LIMIT
from foxbot.core.ui import EmbedPaginator, PickerView
from foxbot.features.inventory.parsers import (
    APP,
    CLIPBOARD,
    FIR,
    ParseError,
    canonical_store_type,
    decode_upload,
    detect_format,
    parse_app_time,
    parse_bool,
    parse_clipboard_time,
    parse_header,
    parse_int,
    parse_inventory,
    split_crate_suffix,
    stock_key,
)

SAMPLE = (
    "Terminus - Rising Loom - Seaport - Public - X: 0.4577449 Y: 0.6644695,2026.06.23-16.30.38\n"
    "Argenti r.II Rifle (Crate),32\n"
    "Booker Storm Rifle Model 838 (Crate),0\n"
    "7.62mm (Crate),60\n"
    "No.2 Loughcaster (Crate),0\n"
    "Tripod (Crate),38\n"
    "\n"
    "O’Brien V.101 Freeman,2\n"
    "T3 “Xiphos”,0\n"
    "\n"
    "Rare Materials (Crate),5\n"
    "Mortar Shell (Crate),56\n"
)

FIR_TSV = (
    "Stockpile Title\tStockpile Name\tStructure Type\tQuantity\tName\tCrated?\tPer Crate\tTotal\tDescription\tCodeName\n"
    "Loom Port\tPublic\tSeaport\t32\tArgenti r.II Rifle\ttrue\t20\t640\tA \"reliable\" rifle.\tRifleC\n"
    "Loom Port\tPublic\tSeaport\t5\tSoldier Supplies\ttrue\t10\t50\t\tSoldierSupplies\n"
    "KM Depot\tKM1\tStorage Depot\t100\tBasic Materials\tfalse\t100\t100\t\tCloth\n"
    "KM Depot\tKM1\tStorage Depot\t3\tMystery Gadget\ttrue\t4\t12\t\tMysteryGadget\n"
    "KM Depot\tKM1\tStorage Depot\tlots\tBroken Row\tfalse\t1\t1\t\tBroken\n"
)

APP_CSV = (
    "Stockpile Name,Stockpile Type,Code,Crated,Quantity,Confidence,Shard,Ingame Time\n"
    "KM1,Storage Depot,RifleW,True,12,0.98,Able,2026-06-23 16:30:38\n"
    "KM1,Storage Depot,Cloth,False,250,0.99,Able,2026-06-23 16:30:38\n"
    "\"Navy, North\",Seaport,SoldierSupplies,true,4,0.9,Able,\n"
)


def header(hex_name="Terminus", town="Rising Loom", kind="Seaport", name="Public"):
    return f"{hex_name} - {town} - {kind} - {name} - X: 0.45 Y: 0.66,2026.06.23-16.30.38"


@pytest.fixture(scope="module")
def catalog():
    return Catalog()


def test_clipboard_sample(catalog):
    result = parse_inventory(SAMPLE, catalog)
    assert result.source == CLIPBOARD
    assert result.faction == "warden"
    assert result.skipped == 0
    [stock] = result.stocks
    assert (stock.hex, stock.region, stock.store_type, stock.name) == ("Terminus", "Rising Loom", "Seaport", "Public")
    assert stock.game_time == parse_clipboard_time("2026.06.23-16.30.38")
    items = {(item.code, item.crated): item for item in stock.items}
    assert set(items) == {
        ("RifleC", True),
        ("RifleAmmo", True),
        ("Tripod", True),
        ("ArmoredCar2LargeW", False),
        ("RareMaterials", True),
        ("MortarAmmo", True),
    }
    assert items[("RifleC", True)].quantity == 32
    assert items[("RifleC", True)].units == 32 * 20
    assert items[("ArmoredCar2LargeW", False)].units == 2
    assert items[("MortarAmmo", True)].category == "Heavy Weapons"
    assert stock.unknown == []


def test_header_with_dashes_in_name_and_localised_type():
    stock = parse_header("Terminus - Rising Loom - Seehafen - KM - North - X: 0,4577 Y: 0,6644,2026.06.23-16.30.38")
    assert (stock.hex, stock.region, stock.store_type, stock.name) == ("Terminus", "Rising Loom", "Seehafen", "KM - North")
    assert stock.has_header
    assert canonical_store_type("Seehafen", ["Storage Depot", "Seaport"]) == "Seaport"
    assert canonical_store_type("seaport", ["Storage Depot", "Seaport"]) == "Seaport"
    assert canonical_store_type("Bunker  Base", ["Seaport"]) == "Bunker Base"
    assert parse_header("Argenti r.II Rifle (Crate),32") is None
    short = parse_header("Terminus - Rising Loom - X: 0.1 Y: 0.2")
    assert (short.hex, short.region, short.store_type, short.name, short.game_time) == ("Terminus", "Rising Loom", "", "", None)


def test_unknown_items_and_crate_words(catalog):
    text = header() + "\nMystery Box (Crate),4\nBasis-Materialien (Kiste),3\nStrange Thing,2\nOdd Device (Kiste),1\nWeird (Mk II),7\n"
    [stock] = parse_inventory(text, catalog).stocks
    items = {(item.code, item.crated): item for item in stock.items}
    assert items[("unknown:mystery box", True)].name == "Mystery Box"
    assert not items[("unknown:mystery box", True)].known
    assert items[("Cloth", True)].quantity == 3
    assert ("unknown:strange thing", False) in items
    assert ("unknown:odd device", True) in items
    assert ("unknown:weird (mk ii)", False) in items
    assert stock.unknown == ["Mystery Box", "Strange Thing", "Odd Device", "Weird (Mk II)"]
    assert split_crate_suffix("Thing (Caisse)") == ("Thing", True)
    assert split_crate_suffix("Thing (Prototype)") == ("Thing (Prototype)", False)


def test_duplicate_lines_merge(catalog):
    text = header() + "\nTripod (Crate),3\nTripod (Crate),4\nTripod,2\nMystery,1\nmystery,2\n"
    [stock] = parse_inventory(text, catalog).stocks
    items = {(item.code, item.crated): item.quantity for item in stock.items}
    assert items == {("Tripod", True): 7, ("Tripod", False): 2, ("unknown:mystery", False): 3}
    assert stock.unknown == ["Mystery", "mystery"]


def test_faction_inference_and_override(catalog):
    warden = header() + "\nSanitäter Uniform,3\nNo.2 Loughcaster,1\nBooker Storm Rifle Model 838,1\n"
    result = parse_inventory(warden, catalog)
    assert result.faction == "warden"
    assert result.stocks[0].items[0].code == "MedicUniformW"
    forced = parse_inventory(warden, catalog, faction="colonial")
    assert forced.faction == "colonial"
    assert forced.stocks[0].items[0].code == "MedicUniformC"
    colonial = header() + "\nSanitäter Uniform,3\nArgenti r.II Rifle,1\n"
    assert parse_inventory(colonial, catalog).stocks[0].items[0].code == "MedicUniformC"
    neutral = parse_inventory(header() + "\nTripod,1\n", catalog)
    assert neutral.faction is None


def test_multiple_headers_and_skipped_lines(catalog):
    text = header(name="KM1") + "\nTripod,1\nthis line is junk\n\n" + header(name="KM2") + "\nTripod (Crate),2\n"
    result = parse_inventory(text, catalog)
    assert [stock.name for stock in result.stocks] == ["KM1", "KM2"]
    assert result.skipped == 1


def test_headerless_list(catalog):
    result = parse_inventory("Tripod (Crate),2\nBandages,40\n", catalog)
    assert result.source == CLIPBOARD
    [stock] = result.stocks
    assert not stock.has_header
    assert {item.code for item in stock.items} == {"Tripod", "Bandages"}


def test_header_only_is_an_empty_stock(catalog):
    [stock] = parse_inventory(header() + "\nTripod (Crate),0\n", catalog).stocks
    assert stock.items == []


def test_rejects_garbage(catalog):
    with pytest.raises(ParseError):
        parse_inventory("   \n  ", catalog)
    with pytest.raises(ParseError):
        parse_inventory("hello there\nhow are you\nnothing here", catalog)


def test_fir_tsv(catalog):
    assert detect_format(FIR_TSV) == FIR
    result = parse_inventory(FIR_TSV, catalog)
    assert result.source == FIR
    assert result.skipped == 1
    port, depot = result.stocks
    assert (port.name, port.store_type) == ("Public", "Seaport")
    assert {(i.code, i.crated, i.quantity) for i in port.items} == {("RifleC", True, 32), ("SoldierSupplies", True, 5)}
    assert (depot.name, depot.store_type) == ("KM1", "Storage Depot")
    mystery = next(i for i in depot.items if not i.known)
    assert (mystery.code, mystery.crated, mystery.per_crate, mystery.quantity) == ("unknown:mystery gadget", True, 4, 3)
    assert depot.unknown == ["Mystery Gadget"]


def test_app_csv_and_tsv(catalog):
    assert detect_format(APP_CSV) == APP
    result = parse_inventory(APP_CSV, catalog)
    assert result.source == APP
    km, navy = result.stocks
    assert (km.name, km.store_type) == ("KM1", "Storage Depot")
    assert {(i.code, i.crated, i.quantity) for i in km.items} == {("RifleW", True, 12), ("Cloth", False, 250)}
    assert km.game_time == parse_app_time("2026-06-23T16:30:38Z")
    assert navy.name == "Navy, North"
    tsv = APP_CSV.replace(",", "\t").replace('"Navy\t North"', "Navy North")
    tsv_result = parse_inventory(tsv, catalog)
    assert tsv_result.source == APP
    assert [stock.name for stock in tsv_result.stocks] == ["KM1", "Navy North"]


def test_stock_key_normalises():
    assert stock_key("Terminus", "Rising  Loom", "SEAPORT", " Public ") == "terminus|rising loom|seaport|public"
    assert stock_key("A", "B", "C", "O’Brien") == stock_key("a", "b", "c", "O'Brien")


def test_value_helpers():
    assert parse_int(" 32 ") == 32
    assert parse_int("32.0") == 32
    assert parse_int("lots") is None
    assert parse_bool("TRUE") and parse_bool("yes") and not parse_bool("false") and not parse_bool("")
    assert parse_app_time("1782232238") == 1782232238
    assert parse_app_time("1782232238000") == 1782232238
    assert parse_app_time("2026.06.23-16.30.38") == 1782232238
    assert parse_app_time("Day 12, 14:00") is None
    assert parse_clipboard_time("bad") is None


def test_decode_upload():
    text = "Tripod (Crate),2\nO’Brien V.101 Freeman,1\n"
    assert decode_upload(codecs.BOM_UTF8 + text.encode("utf-8")) == text
    assert decode_upload(text.encode("utf-16")) == text
    assert decode_upload("Mörsergranate,3".encode("cp1252")) == "Mörsergranate,3"
    assert decode_upload(b"\x81\x8d,1") == "\x81\x8d,1"


@pytest.fixture
async def world():
    bot = await make_bot(["foxbot.features.stockpiles", "foxbot.features.inventory"])
    inventory = bot.extensions["foxbot.features.inventory"]
    stockpiles = bot.extensions["foxbot.features.stockpiles"]
    guild = make_guild(owner_id=1)
    bot.get_guild = MagicMock(return_value=guild)
    logi = add_role(guild, "Logi")
    clerk_role = add_role(guild, "Clerk")
    await bot.perms.set_roles(guild.id, "stockpile", {logi.id})
    await bot.perms.set_roles(guild.id, "inventory", {logi.id, clerk_role.id})
    member = make_member(guild, name="Logi Guy", roles=[logi])
    clerk = make_member(guild, name="Clerk", roles=[clerk_role])
    outsider = make_member(guild, name="Outsider")
    channel = make_channel(guild, "logi")
    yield SimpleNamespace(
        bot=bot,
        inventory=inventory,
        stockpiles=stockpiles,
        guild=guild,
        member=member,
        clerk=clerk,
        outsider=outsider,
        channel=channel,
        logi=logi,
    )
    await bot.db.close()


async def paste(w, member, text, *, modal=None):
    modal = modal or w.inventory.PasteModal(w.bot, w.guild.id)
    set_label_value(modal.contents, text)
    interaction = make_interaction(w.bot, member, channel=w.channel)
    await modal.on_submit(interaction)
    return interaction


async def add_stockpile(w, member=None, **overrides):
    values = dict(
        name="Public",
        hex_name="Terminus",
        region="Rising Loom",
        store_type="Seaport",
        priority="Medium",
        access_code="123456",
        private=False,
        user_id=(member or w.member).id,
    )
    values.update(overrides)
    return await w.stockpiles.create(w.bot, w.guild.id, **values)


async def snapshots(w):
    return await w.bot.db.fetchall("SELECT * FROM inventory_snapshots ORDER BY id")


def all_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{field.name}\n{field.value}" for field in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


async def test_import_command_opens_modal_within_limits(world):
    w = world
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member, channel=w.channel)
    await cog.import_command.callback(cog, interaction)
    modal = interaction.response.modal
    assert isinstance(modal, w.inventory.PasteModal)
    assert len(modal.children) <= 5
    assert len(modal.title) <= 45
    assert isinstance(modal.help, discord.ui.TextDisplay)
    assert "Copy to Clipboard" in modal.help.content
    assert modal.contents.component.max_length == 4000
    assert modal.contents.component.style == discord.TextStyle.paragraph
    modal.to_dict()


async def test_unmatched_import_offers_actions_and_keep(world):
    w = world
    interaction = await paste(w, w.member, SAMPLE)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    embed = payload["embed"]
    text = all_text(embed)
    assert "Not tracked" in text and "Public" in text
    assert "first import" in text
    assert "6 item types" in text
    assert len(embed) <= EMBED_TOTAL_LIMIT
    view = payload["view"]
    labels = {item.label for item in view.children}
    assert {"Track as stockpile", "Track as private stockpile", "Link to existing", "Keep unlinked"} <= labels
    rows = await snapshots(w)
    assert len(rows) == 1
    snap = rows[0]
    assert snap["stockpile_id"] is None
    assert snap["stock_key"] == "terminus|rising loom|seaport|public"
    assert (snap["source"], snap["faction"], snap["imported_by"]) == ("clipboard", "warden", w.member.id)
    items = await w.bot.db.fetchall("SELECT * FROM inventory_items WHERE snapshot_id = ?", (snap["id"],))
    assert len(items) == 6
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(view, "Keep unlinked").callback(click)
    kind, payload = click.recorder.last
    assert kind == "edit"
    assert "Kept unlinked" in payload["content"]
    assert payload["view"] is None
    assert "kept unlinked" in all_text(payload["embed"])
    w.bot.boards.request_refresh.assert_any_call(w.guild.id, "inventory")


async def test_auto_match_links_and_adopts_history(world):
    w = world
    await paste(w, w.member, SAMPLE)
    row = await add_stockpile(w)
    interaction = await paste(w, w.member, SAMPLE.replace("Tripod (Crate),38", "Tripod (Crate),30"))
    payload = interaction.recorder.last[1]
    text = all_text(payload["embed"])
    assert "**Public** - Rising Loom, Terminus (Seaport)" in text
    assert "Tripod (crates): 38 -> 30 (-8)" in text
    assert payload.get("view") is None
    rows = await snapshots(w)
    assert [r["stockpile_id"] for r in rows] == [row["id"], row["id"]]


async def test_match_ignores_store_type_only_when_unique(world):
    w = world
    row = await add_stockpile(w, store_type="Storage Depot")
    await paste(w, w.member, SAMPLE)
    assert (await snapshots(w))[-1]["stockpile_id"] == row["id"]
    await add_stockpile(w, store_type="Aircraft Depot")
    await paste(w, w.member, SAMPLE)
    assert (await snapshots(w))[-1]["stockpile_id"] is None
    exact = await add_stockpile(w, store_type="Seaport")
    await paste(w, w.member, SAMPLE)
    assert (await snapshots(w))[-1]["stockpile_id"] == exact["id"]


async def test_changes_against_previous_snapshot(world):
    w = world
    await paste(w, w.member, SAMPLE)
    changed = (
        SAMPLE.replace("Argenti r.II Rifle (Crate),32", "Argenti r.II Rifle (Crate),40")
        .replace("Mortar Shell (Crate),56\n", "")
        + "Bandages (Crate),3\n"
    )
    interaction = await paste(w, w.member, changed)
    text = all_text(interaction.recorder.last[1]["embed"])
    assert "1 new, 1 gone, 1 changed" in text
    assert "Mortar Shell (crates): gone (was 56)" in text
    assert "Argenti r.II Rifle (crates): 32 -> 40 (+8)" in text
    assert "Bandages (crates): new, 3" in text
    assert text.index("Mortar Shell") < text.index("Argenti")


async def test_retention_keeps_twenty_per_stock_key(world):
    w = world
    for _ in range(23):
        await paste(w, w.member, SAMPLE)
    rows = await snapshots(w)
    assert len(rows) == 20
    orphans = await w.bot.db.fetchval(
        "SELECT COUNT(*) FROM inventory_items WHERE snapshot_id NOT IN (SELECT id FROM inventory_snapshots)"
    )
    assert orphans == 0


async def test_track_as_stockpile_prefills_and_links(world):
    w = world
    await paste(w, w.member, SAMPLE)
    interaction = await paste(w, w.member, SAMPLE)
    view = interaction.recorder.last[1]["view"]
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(view, "Track as stockpile").callback(click)
    modal = click.response.modal
    assert isinstance(modal, w.stockpiles.StockpileModal)
    assert len(modal.children) <= 5
    assert modal.location == Location("Terminus", "Rising Loom", True)
    assert modal.location_field is None
    assert modal.name_field.component.default == "Public"
    assert [o.value for o in modal.type_field.component.options if o.default] == ["Seaport"]
    assert not modal.private
    set_label_value(modal.code_field, "445566")
    set_label_value(modal.type_field, "Seaport")
    set_label_value(modal.priority_field, "High")
    submit = make_interaction(w.bot, w.member, channel=w.channel)
    await modal.on_submit(submit)
    kind, payload = submit.recorder.last
    assert kind == "edit"
    assert "Now tracking **Public**" in payload["content"]
    created = await w.stockpiles.list_entries(w.bot, w.guild.id, private=False)
    assert len(created) == 1 and created[0]["code"] == "445566"
    assert {r["stockpile_id"] for r in await snapshots(w)} == {created[0]["id"]}
    assert payload["view"] is None
    assert "Public** - Rising Loom, Terminus" in all_text(payload["embed"])


async def test_track_private_opens_private_modal(world):
    w = world
    interaction = await paste(w, w.member, SAMPLE)
    view = interaction.recorder.last[1]["view"]
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(view, "Track as private stockpile").callback(click)
    assert click.response.modal.private


async def test_track_requires_stockpile_permission(world):
    w = world
    interaction = await paste(w, w.clerk, SAMPLE)
    view = interaction.recorder.last[1]["view"]
    click = make_interaction(w.bot, w.clerk, channel=w.channel)
    await find_button(view, "Track as stockpile").callback(click)
    kind, payload = click.recorder.last
    assert kind == "send" and "Stockpiles" in payload["content"]
    assert click.response.modal is None


async def test_link_to_existing_lists_stockpiles_in_hex(world):
    w = world
    target = await add_stockpile(w, name="KM North")
    await add_stockpile(w, name="KM South", region="Thunderbolt")
    await add_stockpile(w, name="Far Away", hex_name="Piper's Enclave", region="Syrinx Pass")
    interaction = await paste(w, w.member, SAMPLE)
    view = interaction.recorder.last[1]["view"]
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(view, "Link to existing").callback(click)
    kind, payload = click.recorder.last
    assert kind == "edit"
    picker = payload["view"]
    assert isinstance(picker, PickerView)
    select = find_select(picker)
    assert sorted(o.label for o in select.options) == ["KM North - Rising Loom, Terminus", "KM South - Thunderbolt, Terminus"]
    select_values(select, [target["id"]])
    pick = make_interaction(w.bot, w.member, channel=w.channel)
    await select.callback(pick)
    kind, payload = pick.recorder.last
    assert kind == "edit" and "Linked to **KM North**" in payload["content"]
    assert (await snapshots(w))[0]["stockpile_id"] == target["id"]
    assert payload["view"] is None
    assert "**KM North** - Rising Loom, Terminus" in all_text(payload["embed"])
    relinked = await paste(w, w.member, SAMPLE.replace("Public", "Elsewhere"))
    assert (await snapshots(w))[-1]["stockpile_id"] is None
    repeat = make_interaction(w.bot, w.member, channel=w.channel)
    await select.callback(repeat)
    assert "already linked" in repeat.recorder.last[1]["content"]
    assert relinked.recorder.last[1]["view"] is not None


async def test_link_picker_falls_back_and_paginates(world):
    w = world
    for index in range(30):
        await add_stockpile(w, name=f"Depot {index:02d}", hex_name="Piper's Enclave", region="Syrinx Pass")
    interaction = await paste(w, w.member, SAMPLE)
    view = interaction.recorder.last[1]["view"]
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(view, "Link to existing").callback(click)
    payload = click.recorder.last[1]
    assert "all are listed" in payload["content"]
    picker = payload["view"]
    assert len(find_select(picker).options) <= 25
    assert find_button(picker, "Next")
    back = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(picker, "Back").callback(back)
    assert back.recorder.last[0] == "edit"
    assert back.recorder.last[1]["view"] is view


async def test_permission_denials(world):
    w = world
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.outsider, channel=w.channel)
    await cog.import_command.callback(cog, interaction)
    assert interaction.response.modal is None
    assert "Inventory" in interaction.recorder.last[1]["content"]
    denied = await paste(w, w.outsider, SAMPLE)
    assert "Inventory" in denied.recorder.last[1]["content"]
    assert await snapshots(w) == []
    totals = make_interaction(w.bot, w.outsider)
    await cog.totals.callback(cog, totals, None)
    assert "Inventory" in totals.recorder.last[1]["content"]
    finder = make_interaction(w.bot, w.outsider)
    await cog.find.callback(cog, finder, "RifleC")
    assert "Inventory" in finder.recorder.last[1]["content"]
    exporter = make_interaction(w.bot, w.outsider)
    await cog.export.callback(cog, exporter)
    assert "file" not in exporter.recorder.last[1]
    result = await paste(w, w.member, SAMPLE)
    view = result.recorder.last[1]["view"]
    await w.bot.perms.set_roles(w.guild.id, "inventory", set())
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(view, "Keep unlinked").callback(click)
    assert click.recorder.last[0] == "send" and "Inventory" in click.recorder.last[1]["content"]
    stranger = make_interaction(w.bot, w.clerk, channel=w.channel)
    assert not await view.interaction_check(stranger)


async def private_world(w):
    secret = await add_stockpile(w, name="Secret", private=True)
    await paste(w, w.member, SAMPLE.replace("Public", "Secret"))
    await paste(w, w.member, header(name="Open") + "\nArgenti r.II Rifle (Crate),3\nTripod,4\n")
    return secret


async def test_private_contents_only_for_access(world):
    w = world
    secret = await private_world(w)
    rows = await snapshots(w)
    assert rows[0]["stockpile_id"] == secret["id"]
    cog = w.bot.get_cog("Inventory")

    clerk_find = make_interaction(w.bot, w.clerk)
    await cog.find.callback(cog, clerk_find, "RifleC")
    text = clerk_find.recorder.last[1]["embed"].description
    assert "Open" in text and "Secret" not in text
    assert "**60** units in 1 stockpile" in text

    owner_find = make_interaction(w.bot, w.member)
    await cog.find.callback(cog, owner_find, "RifleC")
    text = owner_find.recorder.last[1]["embed"].description
    assert "[Private] **Secret**" in text
    assert "**700** units in 2 stockpiles" in text

    clerk_totals = make_interaction(w.bot, w.clerk)
    await cog.totals.callback(cog, clerk_totals, None)
    text = clerk_totals.recorder.last[1]["embed"].description
    assert "Argenti r.II Rifle: 3 crates = **60** units in 1 stockpile" in text
    assert "Mortar Shell" not in text

    clerk_export = make_interaction(w.bot, w.clerk)
    await cog.export.callback(cog, clerk_export)
    data = clerk_export.recorder.last[1]["file"].fp.read().decode("utf-8-sig")
    assert "Secret" not in data and "Open" in data

    await w.inventory.ContentsView(w.bot, w.guild.id, w.clerk.id, stockpile_id=secret["id"]).send(
        denied := make_interaction(w.bot, w.clerk)
    )
    assert "access" in denied.recorder.last[1]["content"]

    choices = await cog.view_autocomplete(make_interaction(w.bot, w.clerk), "")
    assert [c.name for c in choices] == ["Open - Rising Loom, Terminus"]

    render = await w.bot.boards.providers["inventory"].render(w.guild)
    board_text = "\n".join(e.description for e in render.embeds)
    assert "Secret" not in board_text and "Open" in board_text
    assert all("Secret" not in o.label for o in render.options)


async def test_auto_match_skips_private_without_access(world):
    w = world
    await add_stockpile(w, name="Secret", private=True)
    interaction = await paste(w, w.clerk, SAMPLE.replace("Public", "Secret"))
    payload = interaction.recorder.last[1]
    assert (await snapshots(w))[0]["stockpile_id"] is None
    assert "Not tracked" in all_text(payload["embed"])
    click = make_interaction(w.bot, w.clerk, channel=w.channel)
    await find_button(payload["view"], "Link to existing").callback(click)
    assert "no tracked stockpiles" in click.recorder.last[1]["content"].lower()


async def test_find_details_and_resolution(world):
    w = world
    await paste(w, w.member, SAMPLE)
    await paste(w, w.member, header(name="KM2") + "\nArgenti r.II Rifle (Crate),2\nArgenti r.II Rifle,7\nMystery Box,4\n")
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member)
    await cog.find.callback(cog, interaction, "argenti")
    embed = interaction.recorder.last[1]["embed"]
    assert embed.title == "Find: Argenti r.II Rifle"
    lines = embed.description.split("\n")
    assert lines[0] == "**687** units in 2 stockpiles (latest import of each)."
    assert lines[2].startswith("**Public** - Rising Loom, Terminus (Seaport): 32 crates = **640** units, data <t:")
    assert "2 crates + 7 loose = **47** units" in lines[3]
    unknown = make_interaction(w.bot, w.member)
    await cog.find.callback(cog, unknown, "mystery")
    text = unknown.recorder.last[1]["embed"].description
    assert "Mystery Box: **4** loose" in text
    nothing = make_interaction(w.bot, w.member)
    await cog.find.callback(cog, nothing, "Bandages")
    assert "No imported stockpile" in nothing.recorder.last[1]["embed"].description


async def test_find_autocomplete_uses_faction(world):
    w = world
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member)
    choices = await cog.find_autocomplete(interaction, "rifle")
    assert any(c.value == "RifleC" for c in choices)
    await w.bot.settings.set(w.guild.id, keys.FACTION, "warden")
    choices = await cog.find_autocomplete(interaction, "rifle")
    assert not any(c.value == "RifleC" for c in choices)
    assert 0 < len(choices) <= 25


async def test_guild_faction_drives_disambiguation(world):
    w = world
    await w.bot.settings.set(w.guild.id, keys.FACTION, "colonial")
    await paste(w, w.member, header() + "\nSanitäter Uniform,3\nNo.2 Loughcaster,1\nBooker Storm Rifle Model 838,1\n")
    snap = (await snapshots(w))[0]
    assert snap["faction"] == "colonial"
    codes = await w.bot.db.fetchall("SELECT item_code FROM inventory_items WHERE snapshot_id = ?", (snap["id"],))
    assert "MedicUniformC" in {r["item_code"] for r in codes}


async def test_totals_by_category(world):
    w = world
    await paste(w, w.member, SAMPLE)
    await paste(w, w.member, header(name="KM2") + "\nArgenti r.II Rifle (Crate),2\nMystery Box,4\n")
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member)
    await cog.totals.callback(cog, interaction, None)
    text = interaction.recorder.last[1]["embed"].description
    assert text.index("**Small Weapons**") < text.index("**Heavy Weapons**") < text.index("**Unrecognised**")
    assert "Argenti r.II Rifle: 34 crates = **680** units in 2 stockpiles" in text
    filtered = make_interaction(w.bot, w.member)
    choice = discord.app_commands.Choice(name="Utility", value="Utility")
    await cog.totals.callback(cog, filtered, choice)
    embed = filtered.recorder.last[1]["embed"]
    assert embed.title == "Inventory Totals: Utility"
    assert "Tripod" in embed.description and "Argenti" not in embed.description


async def test_totals_paginates_large_inventories(world):
    w = world
    lines = "\n".join(f"Strange Widget Number {index:03d} (Crate),{index + 1}" for index in range(300))
    upload = make_upload("big.txt", (header() + "\n" + lines).encode())
    cog = w.bot.get_cog("Inventory")
    await cog.upload.callback(cog, make_interaction(w.bot, w.member), upload)
    interaction = make_interaction(w.bot, w.member)
    await cog.totals.callback(cog, interaction, None)
    payload = interaction.recorder.last[1]
    paginator = payload["view"]
    assert isinstance(paginator, EmbedPaginator)
    assert len(paginator.embeds) > 1
    assert all(len(e) <= EMBED_TOTAL_LIMIT and len(e.description) <= EMBED_DESCRIPTION_LIMIT for e in paginator.embeds)


async def test_export_csv(world):
    w = world
    row = await add_stockpile(w)
    await paste(w, w.member, SAMPLE.replace("Mortar Shell (Crate),56", "=HYPERLINK(evil) (Crate),1"))
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member)
    await cog.export.callback(cog, interaction)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    upload = payload["file"]
    assert upload.filename.startswith("inventory-") and upload.filename.endswith(".csv")
    data = upload.fp.read().decode("utf-8-sig")
    table = list(csv.reader(io.StringIO(data)))
    assert table[0] == [
        "Stockpile",
        "Hex",
        "Town",
        "Type",
        "Item",
        "Code",
        "Category",
        "Crated",
        "Quantity",
        "Per Crate",
        "Total Units",
        "Imported At",
        "Imported By",
    ]
    rifle = next(r for r in table if r[5] == "RifleC")
    assert rifle[:4] == [row["name"], "Terminus", "Rising Loom", "Seaport"]
    assert rifle[7:11] == ["yes", "32", "20", "640"]
    assert rifle[12] == "Logi Guy"
    evil = next(r for r in table if r[5].startswith("unknown:"))
    assert evil[4].startswith("'=")
    assert len(table) == 7


async def test_export_without_data(world):
    w = world
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member)
    await cog.export.callback(cog, interaction)
    assert "No stockpile contents" in interaction.recorder.last[1]["content"]


def make_upload(filename: str, data: bytes, size: int | None = None):
    attachment = MagicMock(spec=discord.Attachment)
    attachment.filename = filename
    attachment.size = len(data) if size is None else size
    attachment.read = AsyncMock(return_value=data)
    return attachment


async def test_upload_validation(world):
    w = world
    cog = w.bot.get_cog("Inventory")
    wrong = make_interaction(w.bot, w.member)
    await cog.upload.callback(cog, wrong, make_upload("picture.png", b"x"))
    assert ".csv" in wrong.recorder.last[1]["content"]
    big = make_interaction(w.bot, w.member)
    await cog.upload.callback(cog, big, make_upload("export.csv", b"x", size=2 * 1024 * 1024))
    assert "1 MB" in big.recorder.last[1]["content"]
    junk = make_interaction(w.bot, w.member)
    await cog.upload.callback(cog, junk, make_upload("export.txt", b"nothing useful here\nat all"))
    assert junk.recorder.kinds() == ["defer", "followup"]
    assert "does not look like" in junk.recorder.last[1]["content"]
    outsider = make_interaction(w.bot, w.outsider)
    await cog.upload.callback(cog, outsider, make_upload("export.txt", SAMPLE.encode()))
    assert "Inventory" in outsider.recorder.last[1]["content"]
    assert await snapshots(w) == []


async def test_upload_fir_with_several_stockpiles(world):
    w = world
    km1 = await add_stockpile(w, name="KM1", hex_name="Piper's Enclave", region="Syrinx Pass", store_type="Storage Depot")
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member)
    await cog.upload.callback(cog, interaction, make_upload("fir.tsv", codecs.BOM_UTF8 + FIR_TSV.encode()))
    assert interaction.recorder.kinds() == ["defer", "followup"]
    payload = interaction.recorder.last[1]
    assert payload["ephemeral"]
    assert "Imported 2 stockpiles from the FIR export." in payload["content"]
    assert "1 line could not be read" in payload["content"]
    rows = await snapshots(w)
    assert [r["source"] for r in rows] == ["fir", "fir"]
    assert rows[0]["stockpile_id"] is None and rows[1]["stockpile_id"] == km1["id"]
    view = payload["view"]
    assert "(1/2)" in payload["embed"].title
    assert find_button(view, "Track as stockpile")
    click = make_interaction(w.bot, w.member)
    await find_button(view, "Next").callback(click)
    kind, shown = click.recorder.last
    assert kind == "edit" and "(2/2)" in shown["embed"].title
    assert "Mystery Gadget" in all_text(shown["embed"])
    assert not any(getattr(item, "label", "") == "Track as stockpile" for item in view.children)
    back = make_interaction(w.bot, w.member)
    await find_button(view, "Previous").callback(back)
    assert "(1/2)" in back.recorder.last[1]["embed"].title
    track = make_interaction(w.bot, w.member)
    await find_button(view, "Track as stockpile").callback(track)
    assert track.response.modal.name_field.component.default == "Public"


async def test_upload_fir_track_without_location_shows_location_field(world):
    w = world
    cog = w.bot.get_cog("Inventory")
    interaction = make_interaction(w.bot, w.member)
    await cog.upload.callback(cog, interaction, make_upload("fir.tsv", FIR_TSV.encode()))
    view = interaction.recorder.last[1]["view"]
    click = make_interaction(w.bot, w.member)
    await find_button(view, "Track as stockpile").callback(click)
    modal = click.response.modal
    assert modal.location is None and modal.location_field is not None
    assert len(modal.children) <= 5
    set_label_value(modal.location_field, "Rising Loom")
    set_label_value(modal.code_field, "111")
    set_label_value(modal.type_field, "Seaport")
    set_label_value(modal.priority_field, "Low")
    submit = make_interaction(w.bot, w.member)
    await modal.on_submit(submit)
    created = await w.stockpiles.list_entries(w.bot, w.guild.id, private=False)
    assert created[0]["hex"] == "Terminus"
    assert (await snapshots(w))[0]["stockpile_id"] == created[0]["id"]


async def test_import_rejects_headerless_without_stockpile(world):
    w = world
    interaction = await paste(w, w.member, "Tripod (Crate),2\n")
    assert "missing" in interaction.recorder.last[1]["content"]
    assert await snapshots(w) == []


async def test_long_paste_warns_about_cut_off(world):
    w = world
    filler = "\n".join(f"Filler Item {index:04d},{index}" for index in range(400))
    text = (header() + "\n" + filler)[:4000]
    interaction = await paste(w, w.member, text)
    payload = interaction.recorder.last[1]
    assert "4000 character limit" in payload["content"]
    embed = payload["embed"]
    assert len(embed) <= EMBED_TOTAL_LIMIT
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert len(embed.description) <= EMBED_DESCRIPTION_LIMIT


async def test_show_for_stockpile_and_prelinked_import(world):
    w = world
    row = await add_stockpile(w, name="KM North")
    opener = make_interaction(w.bot, w.member, channel=w.channel)
    await w.inventory.show_for_stockpile(w.bot, opener, row)
    kind, payload = opener.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert "No contents have been imported" in payload["embed"].description
    view = payload["view"]
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(view, "Import new contents").callback(click)
    modal = click.response.modal
    assert isinstance(modal, w.inventory.PasteModal)
    assert modal.stockpile_id == row["id"]
    assert "KM North" in modal.help.content
    submitted = await paste(w, w.member, SAMPLE, modal=modal)
    result = submitted.recorder.last[1]
    text = all_text(result["embed"])
    assert "does not match **KM North**" in text
    assert "**KM North** - Rising Loom, Terminus" in text
    rows = await snapshots(w)
    assert rows[-1]["stockpile_id"] == row["id"]
    assert rows[-1]["stock_key"] == "terminus|rising loom|seaport|public"

    headerless = w.inventory.PasteModal(w.bot, w.guild.id, stockpile=row)
    later = await paste(w, w.member, "Tripod (Crate),40\nBandages,10\n", modal=headerless)
    text = all_text(later.recorder.last[1]["embed"])
    assert "Warning" not in text
    assert "Tripod (crates): 38 -> 40 (+2)" in text
    newest = (await snapshots(w))[-1]
    assert newest["stock_key"] == "terminus|rising loom|seaport|km north"

    reopened = make_interaction(w.bot, w.member, channel=w.channel)
    await w.inventory.show_for_stockpile(w.bot, reopened, row)
    embed = reopened.recorder.last[1]["embed"]
    assert embed.title == "Inventory: KM North"
    assert f"Imported by <@{w.member.id}>" in embed.description
    assert "**Utility**" in embed.description and "Tripod: 40 crates = **200** units" in embed.description
    assert "Changes since the previous import" in embed.description


async def test_prelinked_import_rejects_several_stockpiles(world):
    w = world
    row = await add_stockpile(w)
    modal = w.inventory.PasteModal(w.bot, w.guild.id, stockpile=row)
    text = header(name="A") + "\nTripod,1\n" + header(name="B") + "\nTripod,2\n"
    interaction = await paste(w, w.member, text, modal=modal)
    assert "several stockpiles" in interaction.recorder.last[1]["content"]
    await w.stockpiles.delete(w.bot, row)
    gone = await paste(w, w.member, SAMPLE, modal=w.inventory.PasteModal(w.bot, w.guild.id, stockpile=row))
    assert "no longer exists" in gone.recorder.last[1]["content"]


async def test_show_for_stockpile_respects_private_access(world):
    w = world
    secret = await private_world(w)
    denied = make_interaction(w.bot, w.clerk)
    await w.inventory.show_for_stockpile(w.bot, denied, secret)
    assert "access" in denied.recorder.last[1]["content"]
    allowed = make_interaction(w.bot, w.member)
    await w.inventory.show_for_stockpile(w.bot, allowed, secret)
    embed = allowed.recorder.last[1]["embed"]
    assert "[Private] **Secret**" in embed.description


async def test_stockpile_panel_inventory_button(world):
    w = world
    row = await add_stockpile(w)
    await paste(w, w.member, SAMPLE)
    panel = w.stockpiles.StockpilePanel(w.bot, w.guild.id, row["id"], w.member.id)
    await panel.send(make_interaction(w.bot, w.member, channel=w.channel))
    click = make_interaction(w.bot, w.member, channel=w.channel)
    await find_button(panel, "Inventory").callback(click)
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"]
    assert payload["embed"].title == "Inventory: Public"


async def test_contents_view_paginates_and_rechecks(world):
    w = world
    lines = "\n".join(f"Strange Widget Number {index:03d} (Crate),{index + 1}" for index in range(250))
    row = await add_stockpile(w)
    cog = w.bot.get_cog("Inventory")
    await cog.upload.callback(cog, make_interaction(w.bot, w.member), make_upload("big.txt", (SAMPLE + lines).encode()))
    view = w.inventory.ContentsView(w.bot, w.guild.id, w.member.id, stockpile_id=row["id"])
    opener = make_interaction(w.bot, w.member)
    await view.send(opener)
    first = opener.recorder.last[1]["embed"]
    assert len(first) <= EMBED_TOTAL_LIMIT
    nxt = make_interaction(w.bot, w.member)
    await find_button(view, "Next").callback(nxt)
    kind, payload = nxt.recorder.last
    assert kind == "edit" and payload["embed"].description != first.description
    assert len(payload["embed"]) <= EMBED_TOTAL_LIMIT
    await w.stockpiles.delete(w.bot, row)
    after = make_interaction(w.bot, w.member)
    await find_button(view, "Previous").callback(after)
    assert "no longer exists" in after.recorder.last[1]["content"]
    assert await snapshots(w) == []


async def test_view_command_and_unlinked_contents(world):
    w = world
    await paste(w, w.member, SAMPLE)
    cog = w.bot.get_cog("Inventory")
    choices = await cog.view_autocomplete(make_interaction(w.bot, w.member), "rising")
    assert [c.name for c in choices] == ["Public - Rising Loom, Terminus"]
    interaction = make_interaction(w.bot, w.member)
    await cog.view.callback(cog, interaction, choices[0].value)
    embed = interaction.recorder.last[1]["embed"]
    assert "Not linked to a tracked stockpile." in embed.description
    assert "Game clock when copied: 2026-06-23 16:30" in embed.description
    bad = make_interaction(w.bot, w.member)
    await cog.view.callback(cog, bad, "nonsense")
    assert "suggestions" in bad.recorder.last[1]["content"]
    assert await cog.view_autocomplete(make_interaction(w.bot, w.outsider), "") == []


async def test_board_render_buttons_and_pick(world):
    w = world
    row = await add_stockpile(w)
    await paste(w, w.member, SAMPLE)
    await paste(w, w.member, header(name="Loose") + "\nTripod,4\n")
    board = w.bot.boards.providers["inventory"]
    render = await board.render(w.guild)
    assert {b.action for b in render.buttons} == {"import", "find", "totals", "export"}
    assert len(render.options) <= 25
    text = "\n".join(e.description for e in render.embeds)
    assert "**Public** (Seaport) - 6 item types" in text
    assert "**Loose** (Seaport) [not tracked]" in text
    assert sum(len(e) for e in render.embeds) <= EMBED_TOTAL_LIMIT
    values = {o.value for o in render.options}
    assert f"sp:{row['id']}" in values
    pick = make_interaction(w.bot, w.member, channel=w.channel)
    await board.on_pick(pick, f"sp:{row['id']}")
    assert pick.recorder.last[1]["embed"].title == "Inventory: Public"
    outsider_pick = make_interaction(w.bot, w.outsider, channel=w.channel)
    await board.on_pick(outsider_pick, f"sp:{row['id']}")
    assert "Inventory" in outsider_pick.recorder.last[1]["content"]
    importer = make_interaction(w.bot, w.member, channel=w.channel)
    await board.on_button(importer, "import")
    assert isinstance(importer.response.modal, w.inventory.PasteModal)
    finder = make_interaction(w.bot, w.member, channel=w.channel)
    await board.on_button(finder, "find")
    find_modal = finder.response.modal
    assert len(find_modal.children) <= 5
    set_label_value(find_modal.query, "Tripod")
    found = make_interaction(w.bot, w.member, channel=w.channel)
    await find_modal.on_submit(found)
    assert found.recorder.last[1]["embed"].title == "Find: Tripod"
    totals = make_interaction(w.bot, w.member, channel=w.channel)
    await board.on_button(totals, "totals")
    assert totals.recorder.last[1]["embed"].title == "Inventory Totals"
    exporter = make_interaction(w.bot, w.member, channel=w.channel)
    await board.on_button(exporter, "export")
    assert "file" in exporter.recorder.last[1]
    blocked = make_interaction(w.bot, w.outsider, channel=w.channel)
    await board.on_button(blocked, "find")
    assert blocked.response.modal is None


async def test_board_empty_and_admin_post(world):
    w = world
    render = await w.bot.boards.providers["inventory"].render(w.guild)
    assert "No stockpile contents have been imported yet." in render.embeds[0].description
    assert render.options == []
    cog = w.bot.get_cog("Inventory")
    denied = make_interaction(w.bot, w.member, channel=w.channel)
    await cog.board.callback(cog, denied)
    assert "admins" in denied.recorder.last[1]["content"]
    owner = make_member(w.guild, user_id=1, name="Owner")
    posted = make_interaction(w.bot, owner, channel=w.channel)
    await cog.board.callback(cog, posted)
    assert "Inventory board posted." in posted.recorder.last[1]["content"]
    sent_view = w.channel.sent[0]["view"]
    assert {item.custom_id for item in sent_view.children} == {
        "fb:inventory:btn:import",
        "fb:inventory:btn:find",
        "fb:inventory:btn:totals",
        "fb:inventory:btn:export",
    }


async def test_pruned_snapshot_in_result_view(world):
    w = world
    first = await paste(w, w.member, SAMPLE)
    view = first.recorder.last[1]["view"]
    for _ in range(20):
        await paste(w, w.member, SAMPLE)
    click = make_interaction(w.bot, w.member)
    await find_button(view, "Keep unlinked").callback(click)
    assert "no longer exists" in click.recorder.last[1]["content"]


async def test_command_tree_shape(world):
    w = world
    payload = w.bot.command_payload()
    names = {command["name"] for command in payload}
    assert {"inventory", "find"} <= names
    inventory = next(command for command in payload if command["name"] == "inventory")
    assert {o["name"] for o in inventory["options"]} == {"import", "upload", "view", "totals", "export", "board"}
    totals = next(o for o in inventory["options"] if o["name"] == "totals")
    assert len(totals["options"][0]["choices"]) <= 25
    assert timeutil.now() > 0
