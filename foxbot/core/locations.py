from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from discord import app_commands

log = logging.getLogger(__name__)

SEED_PATH = Path(__file__).resolve().parent.parent / "resources" / "locations.json"
REFRESH_INTERVAL = 24 * 3600

HEX_NAME_OVERRIDES = {
    "TheFingersHex": "The Fingers",
    "CallahansPassageHex": "Callahan's Passage",
    "PipersEnclaveHex": "Piper's Enclave",
    "KingsCageHex": "King's Cage",
    "AllodsBightHex": "Allod's Bight",
    "MorgensCrossingHex": "Morgen's Crossing",
    "FishermansRowHex": "Fisherman's Row",
    "LinnMercyHex": "Linn of Mercy",
    "DeadLandsHex": "Deadlands",
    "OarbreakerHex": "Oarbreaker Isles",
    "GodcroftsHex": "Godcrofts",
    "MarbanHollow": "Marban Hollow",
}


def hex_display_name(map_name: str) -> str:
    if map_name in HEX_NAME_OVERRIDES:
        return HEX_NAME_OVERRIDES[map_name]
    base = map_name[:-3] if map_name.endswith("Hex") else map_name
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)


def normalise(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", text.lower())
    if cleaned.startswith("the") and len(cleaned) > 5:
        cleaned = cleaned[3:]
    if cleaned.endswith("hex") and len(cleaned) > 5:
        cleaned = cleaned[:-3]
    return cleaned


def best_match(key: str, candidates: dict):
    if not key:
        return None
    if key in candidates:
        return candidates[key]
    if len(key) >= 4:
        prefixed = [k for k in candidates if k.startswith(key)]
        if len(prefixed) == 1:
            return candidates[prefixed[0]]
    match = difflib.get_close_matches(key, list(candidates), n=1, cutoff=0.8)
    if match:
        return candidates[match[0]]
    if len(key) >= 5:
        scored = sorted(
            ((difflib.SequenceMatcher(None, key, k[: len(key)]).ratio(), k) for k in candidates),
            reverse=True,
        )
        if scored and scored[0][0] >= 0.66 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
            return candidates[scored[0][1]]
    return None


@dataclass(frozen=True)
class Region:
    name: str
    major: bool


@dataclass
class Hex:
    key: str
    name: str
    regions: list[Region] = field(default_factory=list)


@dataclass(frozen=True)
class Location:
    hex: str
    region: str
    known: bool

    def label(self) -> str:
        if self.region and self.hex:
            return f"{self.region}, {self.hex}"
        return self.hex or self.region


class LocationService:
    def __init__(self, cache_path: Path, api_url: str):
        self.cache_path = cache_path
        self.api_url = api_url.rstrip("/")
        self.hexes: dict[str, Hex] = {}
        self.fetched_at = 0.0
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

    def _load_file(self, path: Path) -> bool:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        hexes = {}
        for item in raw.get("hexes", []):
            regions = [Region(r["name"], bool(r.get("major"))) for r in item.get("regions", [])]
            hexes[normalise(item["name"])] = Hex(item["key"], item["name"], regions)
        if not hexes:
            return False
        self.hexes = hexes
        self.fetched_at = float(raw.get("fetched_at", 0))
        return True

    def _dump(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "hexes": [
                {
                    "key": h.key,
                    "name": h.name,
                    "regions": [{"name": r.name, "major": r.major} for r in h.regions],
                }
                for h in sorted(self.hexes.values(), key=lambda h: h.name)
            ],
        }

    async def start(self) -> None:
        if not self._load_file(self.cache_path):
            self._load_file(SEED_PATH)
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self._task = asyncio.create_task(self._refresh_loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._session is not None:
            await self._session.close()

    async def _refresh_loop(self) -> None:
        while True:
            if time.time() - self.fetched_at >= REFRESH_INTERVAL:
                try:
                    await self.refresh()
                except Exception:
                    log.warning("Could not refresh locations from the War API", exc_info=True)
                    await asyncio.sleep(3600)
                    continue
            await asyncio.sleep(3600)

    async def _get_json(self, path: str):
        assert self._session is not None
        async with self._session.get(f"{self.api_url}{path}") as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def refresh(self) -> None:
        map_names = await self._get_json("/worldconquest/maps")
        hexes: dict[str, Hex] = {}
        for map_name in map_names:
            data = await self._get_json(f"/worldconquest/maps/{map_name}/static")
            regions: dict[str, Region] = {}
            for item in data.get("mapTextItems", []):
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                major = item.get("mapMarkerType") == "Major"
                if text not in regions or major:
                    regions[text] = Region(text, major)
            name = hex_display_name(map_name)
            ordered = sorted(regions.values(), key=lambda r: (not r.major, r.name))
            hexes[normalise(name)] = Hex(map_name, name, ordered)
        if not hexes:
            raise ValueError("War API returned no maps")
        self.hexes = hexes
        self.fetched_at = time.time()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._dump(), indent=2), encoding="utf-8")
        log.info("Loaded %d hexes from the War API", len(hexes))

    def hex_names(self) -> list[str]:
        return sorted(h.name for h in self.hexes.values())

    def find_hex(self, text: str) -> Hex | None:
        return best_match(normalise(text), self.hexes)

    def find_region(self, text: str, hex_: Hex | None = None) -> tuple[Hex, Region] | None:
        key = normalise(text)
        if not key:
            return None
        pool = [hex_] if hex_ is not None else list(self.hexes.values())
        candidates: dict[str, tuple[Hex, Region]] = {}
        for h in pool:
            for region in h.regions:
                candidates.setdefault(normalise(region.name), (h, region))
        return best_match(key, candidates)

    def resolve(self, hex_text: str | None, region_text: str | None) -> Location:
        hex_text = (hex_text or "").strip()
        region_text = (region_text or "").strip()
        hex_ = self.find_hex(hex_text) if hex_text else None
        if region_text:
            found = self.find_region(region_text, hex_)
            if found is None and hex_ is None and hex_text:
                found = self.find_region(region_text)
            if found is not None and (hex_ is None or found[0] is hex_):
                return Location(found[0].name, found[1].name, True)
            return Location(hex_.name if hex_ else hex_text, region_text, False)
        if hex_ is not None:
            return Location(hex_.name, "", True)
        return Location(hex_text, "", False)

    def resolve_text(self, text: str) -> Location:
        text = (text or "").strip()
        if not text:
            return Location("", "", False)
        parts = [p.strip() for p in re.split(r"[,/>|]", text) if p.strip()]
        if len(parts) >= 2:
            first, second = parts[0], parts[1]
            if self.find_hex(first) and not self.find_hex(second):
                return self.resolve(first, second)
            return self.resolve(second, first)
        found = self.find_region(text)
        if found is not None:
            return Location(found[0].name, found[1].name, True)
        hex_ = self.find_hex(text)
        if hex_ is not None:
            return Location(hex_.name, "", True)
        return Location("", text, False)

    def hex_choices(self, current: str) -> list[app_commands.Choice[str]]:
        key = normalise(current)
        names = self.hex_names()
        if key:
            names = [n for n in names if key in normalise(n)] or [
                self.hexes[m].name for m in difflib.get_close_matches(key, list(self.hexes), n=10, cutoff=0.5)
            ]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    def location_choices(self, current: str) -> list[app_commands.Choice[str]]:
        key = normalise(current.split(",")[0]) if current else ""
        results: list[str] = []
        for h in sorted(self.hexes.values(), key=lambda h: h.name):
            hex_match = bool(key) and key in normalise(h.name)
            for region in h.regions:
                if not key or hex_match or key in normalise(region.name):
                    if region.major or key:
                        results.append(f"{region.name}, {h.name}")
        if key and not results:
            for name in difflib.get_close_matches(
                key,
                [normalise(r.name) for h in self.hexes.values() for r in h.regions],
                n=10,
                cutoff=0.6,
            ):
                found = self.find_region(name)
                if found:
                    results.append(f"{found[1].name}, {found[0].name}")
        if key:
            results.sort(key=lambda label: (not normalise(label).startswith(key), label))
        choices = [app_commands.Choice(name=label[:100], value=label[:100]) for label in dict.fromkeys(results)]
        if current and current.strip() and not any(c.value == current.strip() for c in choices[:24]):
            choices = choices[:24] + [app_commands.Choice(name=f"Use as typed: {current.strip()}"[:100], value=current.strip()[:100])]
        return choices[:25]

    def region_choices(self, hex_text: str | None, current: str) -> list[app_commands.Choice[str]]:
        key = normalise(current)
        hex_ = self.find_hex(hex_text) if hex_text else None
        pool = [hex_] if hex_ is not None else list(self.hexes.values())
        results: list[tuple[str, str]] = []
        for h in sorted(pool, key=lambda h: h.name):
            for region in h.regions:
                if key and key not in normalise(region.name):
                    continue
                label = region.name if hex_ is not None else f"{region.name} ({h.name})"
                results.append((label, region.name))
        if hex_ is None:
            results.sort(key=lambda item: item[0])
        return [app_commands.Choice(name=label[:100], value=value) for label, value in results[:25]]
