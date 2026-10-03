from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from discord import app_commands

CATALOG_PATH = Path(__file__).resolve().parent.parent / "resources" / "items.json"

QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'", "`": "'", "“": '"', "”": '"', "„": '"', "″": '"'})
PREFERRED_CODES = {"rarematerials": "RareMaterials"}
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
    "Aircraft Parts",
    "Other",
]


def name_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(QUOTES)).strip().casefold()


def search_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.translate(QUOTES).casefold())


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    category: str
    faction: str | None
    per_crate: int
    names: dict[str, str] = field(default_factory=dict, hash=False, compare=False)


class Catalog:
    def __init__(self, path: Path = CATALOG_PATH):
        self.items: dict[str, Item] = {}
        self._by_name: dict[str, list[Item]] = {}
        self._search: dict[str, Item] = {}
        self.load(path)

    def load(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.items.clear()
        self._by_name.clear()
        self._search.clear()
        for entry in raw["items"]:
            item = Item(
                code=entry["code"],
                name=entry["name"],
                category=entry.get("category") or "Other",
                faction=entry.get("faction"),
                per_crate=max(1, int(entry.get("per_crate") or 1)),
                names=dict(entry.get("names") or {}),
            )
            self.items[item.code] = item
            for label in {item.name, *item.names.values()}:
                bucket = self._by_name.setdefault(name_key(label), [])
                if item not in bucket:
                    bucket.append(item)
            self._search.setdefault(search_key(item.name), item)

    def get(self, code: str | None) -> Item | None:
        return self.items.get(code) if code else None

    def lookup(self, display: str, faction: str | None = None) -> Item | None:
        candidates = self._by_name.get(name_key(display), [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        preferred = PREFERRED_CODES.get(search_key(display))
        if preferred and preferred in self.items:
            return self.items[preferred]
        if faction:
            for item in candidates:
                if item.faction == faction:
                    return item
        neutral = [item for item in candidates if item.faction is None]
        return (neutral or candidates)[0]

    def resolve_line(self, display: str, faction: str | None = None) -> tuple[Item | None, bool]:
        item = self.lookup(display, faction)
        if item is not None:
            return item, False
        match = re.match(r"^(?P<base>.*\S)\s*\([^()]*\)\s*$", display)
        if match:
            item = self.lookup(match.group("base"), faction)
            if item is not None:
                return item, True
        return None, False

    def search(self, query: str, faction: str | None = None, limit: int = 25) -> list[Item]:
        key = search_key(query)
        pool = [item for item in self.items.values() if faction is None or item.faction in (None, faction)]
        if not key:
            return sorted(pool, key=lambda i: (CATEGORY_ORDER.index(i.category) if i.category in CATEGORY_ORDER else 99, i.name))[:limit]
        starts = [i for i in pool if search_key(i.name).startswith(key) or i.code.casefold().startswith(key)]
        contains = [i for i in pool if i not in starts and (key in search_key(i.name) or any(key in search_key(n) for n in i.names.values()))]
        results = sorted(starts, key=lambda i: i.name) + sorted(contains, key=lambda i: i.name)
        if len(results) < limit:
            keys = {search_key(i.name): i for i in pool}
            for match in difflib.get_close_matches(key, list(keys), n=limit, cutoff=0.6):
                if keys[match] not in results:
                    results.append(keys[match])
        return results[:limit]

    def choices(self, query: str, faction: str | None = None) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=f"{item.name} ({item.category})"[:100], value=item.code)
            for item in self.search(query, faction)
        ]
