import argparse
import asyncio
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from foxbot.constants import STOCKPILE_LIFETIME_HOURS
from foxbot.core import entries, ids
from foxbot.core.catalog import Catalog
from foxbot.core.locations import SEED_PATH, LocationService
from foxbot.db import Database
from foxbot.features.inventory import save_snapshot
from foxbot.features.inventory.parsers import parse_inventory

KIND = "stockpile"
RECORD_NAME = "test_data.json"
HOUR = 3600
SEED = 20261003
JITTER = 0.03
CATEGORY_ORDER = [
    "Small Weapons",
    "Heavy Weapons",
    "Heavy Ammunition",
    "Utility",
    "Medical",
    "Resources",
    "Uniforms",
    "Vehicles",
    "Shippables",
]

STOCKPILES = [
    ("Test Origin Port", "Origin", "Teichotima", "Seaport", "hub", "High", False, [26, 9, 0.3]),
    ("Test Red River Depot", "Red River", "Cannonsmoke", "Storage Depot", "front", "Medium", False, [30, 2]),
    ("Test Kalokai Port", "Kalokai", "Baccae Ridge", "Seaport", "hub", "Low", False, [10]),
    ("Test Acrithia Port", "Acrithia", "The Brinehold", "Seaport", "hub", "Medium", False, [72, 40]),
    ("Test Great March Depot", "Great March", "Sitaria", "Storage Depot", "front", "High", False, [120]),
    ("Test Westgate Depot", "Westgate", "Westgate Keep", "Storage Depot", "front", "High", False, [48, 24, 4]),
    ("Test Farranac Port", "Farranac Coast", "Pleading Wharf", "Seaport", "hub", "Low", False, [0.8]),
    ("Test Deadlands Depot", "Deadlands", "Callahan's Boot", "Storage Depot", "front", "Medium", False, [20, 6.5]),
    ("Test Ashtown Port", "Ash Fields", "Ashtown", "Seaport", "hub", "Medium", False, [0.25]),
    ("Test Ashtown Depot", "Ash Fields", "Ashtown", "Storage Depot", "front", "Low", True, [60]),
]

HUB_ITEMS = [
    ("Cloth", True, 80, 400),
    ("Wood", True, 40, 250),
    ("Explosive", True, 10, 80),
    ("HeavyExplosive", True, 5, 60),
    ("Components", True, 5, 40),
    ("Metal", True, 20, 150),
    ("Concrete", False, 50, 400),
    ("PipeMaterials", True, 5, 30),
    ("FacilityMaterials9", False, 5, 120),
    ("Diesel", False, 20, 150),
    ("Petrol", False, 10, 100),
    ("RifleAmmo", True, 20, 120),
    ("SMGAmmo", True, 10, 80),
    ("MGAmmo", True, 10, 60),
    ("AssaultRifleAmmo", True, 5, 40),
    ("LightArtilleryAmmo", True, 10, 80),
    ("HeavyArtilleryAmmo", True, 5, 40),
    ("ATAmmo", True, 5, 40),
    ("LightTankAmmo", True, 5, 40),
    ("SoldierSupplies", True, 20, 150),
    ("Bandages", True, 10, 60),
    ("TraumaKit", True, 5, 30),
    ("BloodPlasma", True, 10, 60),
    ("MaterialPlatform", False, 1, 8),
    ("ResourceContainer", False, 1, 6),
    ("ShippingContainer", False, 1, 4),
    ("ConstructionEquipment", False, 1, 3),
    ("Construction", True, 1, 4),
    ("FlatbedTruck", True, 1, 3),
]

FRONT_ITEMS = [
    ("RifleAmmo", True, 10, 80),
    ("SMGAmmo", True, 5, 40),
    ("MGAmmo", True, 5, 40),
    ("ShotgunAmmo", True, 2, 20),
    ("MortarAmmo", True, 5, 40),
    ("MortarAmmoSH", True, 2, 20),
    ("MortarAmmoFL", True, 2, 15),
    ("Mortar", True, 1, 10),
    ("ATRPGAmmo", True, 5, 30),
    ("StickyBomb", True, 2, 20),
    ("GreenAsh", True, 2, 15),
    ("SmokeGrenade", True, 2, 15),
    ("FirstAidKit", True, 5, 30),
    ("Bandages", True, 10, 50),
    ("TraumaKit", True, 2, 20),
    ("BloodPlasma", True, 5, 40),
    ("SoldierSupplies", True, 10, 80),
    ("Tripod", True, 2, 20),
    ("Binoculars", True, 2, 10),
    ("Shovel", True, 2, 15),
    ("SledgeHammer", True, 1, 8),
    ("WorkWrench", True, 1, 6),
    ("WorkHammer", True, 1, 6),
    ("GasMask", True, 2, 15),
    ("GasMaskFilter", True, 5, 30),
    ("SandbagMaterials", True, 5, 40),
    ("BarbedWireMaterials", True, 5, 40),
    ("MetalBeamMaterials", True, 2, 20),
    ("Radio", True, 1, 6),
    ("TankMine", True, 1, 10),
    ("InfantryMine", True, 1, 10),
    ("Cloth", True, 10, 80),
    ("Wood", True, 5, 40),
]

FACTION_WEAPONS = [
    ("Rifle", 10, 60),
    ("RifleAutomatic", 2, 20),
    ("RifleHeavy", 2, 15),
    ("RifleLong", 1, 10),
    ("Grenade", 5, 40),
    ("ATRPG", 2, 15),
    ("ATRPGHeavy", 1, 8),
    ("SoldierUniform", 2, 20),
    ("MedicUniform", 1, 10),
]

FACTION_VEHICLES = [
    ("Truck", 1, 5),
    ("ArmoredCar", 1, 3),
    ("HalfTrack", 1, 3),
    ("FieldMG", 1, 3),
    ("Ambulance", 1, 2),
]


def data_dir() -> Path:
    load_dotenv(ROOT / ".env")
    return Path(os.getenv("FOXBOT_DATA_DIR", "").strip() or ROOT / "data").expanduser().resolve()


def stamp(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).strftime("%Y.%m.%d-%H.%M.%S")


def faction_items(catalog: Catalog, faction: str, bases: list[tuple[str, int, int]]) -> list[tuple[str, bool, int, int]]:
    suffix = "W" if faction == "warden" else "C"
    return [(base + suffix, True, low, high) for base, low, high in bases if catalog.get(base + suffix) is not None]


def first_contents(rng: random.Random, pool: list, extras: list) -> dict[tuple[str, bool], int]:
    contents = {(code, crated): rng.randint(low, high) for code, crated, low, high in pool if rng.random() < 0.75}
    for code, crated, low, high in rng.sample(extras, k=min(len(extras), rng.randint(4, 8))):
        contents[(code, crated)] = rng.randint(low, high)
    for code, _, _, high in rng.sample(pool, k=3):
        contents[(code, False)] = rng.randint(1, max(2, high // 2))
    return contents


def next_contents(rng: random.Random, contents: dict[tuple[str, bool], int], pool: list) -> dict[tuple[str, bool], int]:
    changed = {key: max(1, round(quantity * rng.uniform(0.4, 1.6))) for key, quantity in contents.items() if rng.random() >= 0.12}
    missing = [entry for entry in pool if (entry[0], entry[1]) not in changed]
    for code, crated, low, high in rng.sample(missing, k=min(len(missing), rng.randint(1, 3))):
        changed[(code, crated)] = rng.randint(low, high)
    return changed


def export_text(catalog: Catalog, header: str, contents: dict[tuple[str, bool], int]) -> str:
    groups: dict[str, list[str]] = {}
    for (code, crated), quantity in contents.items():
        item = catalog.get(code)
        groups.setdefault(item.category, []).append(f"{item.name}{' (Crate)' if crated else ''},{quantity}")
    lines = [header]
    for category in sorted(groups, key=lambda name: CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER)):
        lines.extend(sorted(groups[category]))
        lines.append("")
    return "\n".join(lines)


async def only_guild(db: Database) -> int:
    rows = await db.fetchall(
        "SELECT guild_id FROM guild_settings UNION SELECT guild_id FROM stockpiles UNION SELECT guild_id FROM inventory_snapshots"
    )
    guilds = sorted({row["guild_id"] for row in rows})
    if len(guilds) != 1:
        raise SystemExit(f"Pass --guild, the database has these servers: {', '.join(map(str, guilds)) or 'none'}")
    return guilds[0]


async def likely_user(db: Database, guild_id: int) -> int:
    user = await db.fetchval("SELECT created_by FROM stockpiles WHERE guild_id = ? ORDER BY created_at DESC LIMIT 1", (guild_id,))
    if user is None:
        user = await db.fetchval(
            "SELECT imported_by FROM inventory_snapshots WHERE guild_id = ? ORDER BY imported_at DESC LIMIT 1", (guild_id,)
        )
    return int(user or 0)


async def guild_faction(db: Database, guild_id: int) -> str:
    raw = await db.fetchval("SELECT value FROM guild_settings WHERE guild_id = ? AND key = 'faction'", (guild_id,))
    try:
        value = json.loads(raw) if raw else ""
    except ValueError:
        value = raw
    return "warden" if str(value).casefold() == "warden" else "colonial"


def load_locations(folder: Path) -> LocationService:
    locations = LocationService(folder / "locations_cache.json", "")
    if not locations._load_file(locations.cache_path) or not locations.has_positions():
        locations._load_file(SEED_PATH)
    return locations


async def add(folder: Path, record: Path, guild_id: int | None, user_id: int | None) -> None:
    if record.exists():
        raise SystemExit(f"Test data is already in the database ({record}). Run remove first.")
    catalog = Catalog()
    locations = load_locations(folder)
    db = Database(folder / "foxbot.db")
    await db.connect()
    created: list[dict] = []
    try:
        guild_id = guild_id or await only_guild(db)
        user_id = user_id or await likely_user(db, guild_id)
        faction = await guild_faction(db, guild_id)
        weapons = faction_items(catalog, faction, FACTION_WEAPONS)
        vehicles = faction_items(catalog, faction, FACTION_VEHICLES)
        rng = random.Random(SEED)
        now = int(time.time())
        lifetime = STOCKPILE_LIFETIME_HOURS * HOUR
        for number, (name, hex_name, town, store_type, profile, priority, private, ages) in enumerate(STOCKPILES, start=1):
            hex_ = locations.find_hex(hex_name)
            region = next((r for r in hex_.regions if r.name == town), None) if hex_ else None
            if region is None or region.x is None:
                raise SystemExit(f"{town}, {hex_name} has no map position in the location data.")
            x = min(0.98, max(0.02, region.x + rng.uniform(-JITTER, JITTER)))
            y = min(0.98, max(0.02, region.y + rng.uniform(-JITTER, JITTER)))
            refreshed = now - round(rng.uniform(0, 10) * HOUR)
            entry_id = await ids.unique_id(db, "stockpiles")
            await db.execute(
                "INSERT INTO stockpiles (id, guild_id, name, hex, region, store_type, priority, code, expires_at, private, "
                "refreshed_by, refreshed_at, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    guild_id,
                    name,
                    hex_.name,
                    region.name,
                    store_type,
                    priority,
                    f"{number:06d}",
                    refreshed + lifetime,
                    int(private),
                    user_id,
                    refreshed,
                    user_id,
                    now - round((max(ages) + 1) * HOUR),
                    refreshed,
                ),
            )
            created.append({"id": entry_id, "name": name})
            pool = HUB_ITEMS if profile == "hub" else FRONT_ITEMS
            extras = weapons + vehicles if profile == "hub" else weapons
            contents = first_contents(rng, pool, extras)
            for step, age in enumerate(ages):
                if step:
                    contents = next_contents(rng, contents, pool)
                imported_at = now - round(age * HOUR)
                header = f"{hex_.name} - {region.name} - {store_type} - {name} - X: {x:.7f} Y: {y:.7f},{stamp(imported_at)}"
                result = parse_inventory(export_text(catalog, header, contents), catalog, faction)
                stock = result.stocks[0]
                if stock.unknown:
                    raise SystemExit(f"The parser did not know these lines: {', '.join(stock.unknown)}")
                await save_snapshot(
                    SimpleNamespace(db=db),
                    guild_id,
                    stock,
                    source=result.source,
                    faction=result.faction,
                    stockpile_id=entry_id,
                    user_id=user_id,
                    now=imported_at,
                )
            print(f"Added {name} ({region.name}, {hex_.name}) with {len(ages)} import(s)")
    finally:
        if created:
            record.write_text(json.dumps({"guild_id": guild_id, "stockpiles": created}, indent=1), encoding="utf-8")
        await db.close()
    print(f"Done. {len(created)} test stockpiles for server {guild_id}, imported by user {user_id}. Remove them with: remove")


async def remove(folder: Path, record: Path) -> None:
    if not record.exists():
        raise SystemExit("There is no test data to remove.")
    data = json.loads(record.read_text(encoding="utf-8"))
    db = Database(folder / "foxbot.db")
    await db.connect()
    try:
        async with db.transaction() as tx:
            for entry in data["stockpiles"]:
                await tx.execute("DELETE FROM stockpiles WHERE guild_id = ? AND id = ?", (data["guild_id"], entry["id"]))
                await entries.delete_children(tx, KIND, entry["id"])
                await tx.execute("DELETE FROM alert_messages WHERE kind = ? AND entry_id = ?", (KIND, entry["id"]))
    finally:
        await db.close()
    record.unlink()
    print(f"Removed {len(data['stockpiles'])} test stockpiles and all of their imports.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add or remove test stockpiles with in-game export imports.")
    parser.add_argument("action", choices=["add", "remove"])
    parser.add_argument("--guild", type=int, help="Server ID, needed when the database has more than one server")
    parser.add_argument("--user", type=int, help="Discord user ID to show as the creator and importer")
    args = parser.parse_args()
    folder = data_dir()
    record = folder / RECORD_NAME
    if args.action == "add":
        asyncio.run(add(folder, record, args.guild, args.user))
    else:
        asyncio.run(remove(folder, record))


if __name__ == "__main__":
    main()
