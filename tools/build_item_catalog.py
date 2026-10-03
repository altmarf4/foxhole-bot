import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "foxbot" / "resources" / "items.json"
FS_CATALOG = "https://raw.githubusercontent.com/xurxogr/foxhole-stockpiles/main/data/catalog.json"
FIR_TREE = "https://api.github.com/repos/GICodeWarrior/fir/git/trees/main?recursive=1"
FIR_RAW = "https://raw.githubusercontent.com/GICodeWarrior/fir/main/{path}"

CATEGORY_NAMES = {
    "EItemCategory::SmallArms": "Small Weapons",
    "EItemCategory::HeavyArms": "Heavy Weapons",
    "EItemCategory::HeavyAmmo": "Heavy Ammunition",
    "EItemCategory::Utility": "Utility",
    "EItemCategory::Supplies": "Resources",
    "EItemCategory::Medical": "Medical",
    "EItemCategory::Uniforms": "Uniforms",
    "EItemCategory::Parts": "Vehicle Parts",
}
FACTIONS = {"EFactionId::Wardens": "warden", "EFactionId::Colonials": "colonial"}


def fetch_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "foxbot-catalog-builder"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_fir_catalog_path() -> str:
    tree = fetch_json(FIR_TREE)["tree"]
    candidates = []
    for entry in tree:
        match = re.fullmatch(r"foxhole/[a-z]+-(\d+)/catalog\.json", entry["path"])
        if match:
            candidates.append((int(match.group(1)), entry["path"]))
    if not candidates:
        raise SystemExit("No versioned FIR catalog found")
    return max(candidates)[1]


def fallback_category(item: dict) -> str:
    if item.get("VehicleProfileType") or item.get("VehicleBuildType"):
        return "Vehicles"
    if item.get("ShippableInfo"):
        return "Shippables"
    return CATEGORY_NAMES.get(str(item.get("ItemCategory")), "Other")


def main() -> None:
    fs_items = {item["CodeName"]: item for item in fetch_json(FS_CATALOG) if item.get("CodeName")}
    fir_path = latest_fir_catalog_path()
    fir_items = {item["CodeName"]: item for item in fetch_json(FIR_RAW.format(path=fir_path)) if item.get("CodeName")}
    items = []
    for code in sorted(set(fs_items) | set(fir_items)):
        fs = fs_items.get(code, {})
        fir = fir_items.get(code, {})
        fig = fir.get("__FIG__") or {}
        if fig and fig.get("is_stockpilable") is False:
            continue
        name = fs.get("DisplayName") or fir.get("DisplayName")
        if not name:
            continue
        per_crate = fig.get("quantity_per_crate") or (fs.get("ItemDynamicData") or {}).get("QuantityPerCrate") or 1
        locales = {k: v for k, v in (fs.get("DisplayNameLocales") or {}).items() if v}
        items.append(
            {
                "code": code,
                "name": name,
                "names": locales,
                "category": fig.get("ui_category") or fallback_category(fs or fir),
                "faction": FACTIONS.get(str(fs.get("FactionVariant") or fir.get("FactionVariant"))),
                "per_crate": int(per_crate),
            }
        )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps({"sources": {"foxhole_stockpiles": FS_CATALOG, "fir": fir_path}, "items": items}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"Wrote {len(items)} items to {OUTPUT} (FIR {fir_path})", file=sys.stderr)


if __name__ == "__main__":
    main()
