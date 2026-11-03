import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "foxbot" / "resources" / "items.json"
OUTPUT = ROOT / "foxbot" / "resources" / "icons"
FIR_TREE = "https://api.github.com/repos/GICodeWarrior/fir/git/trees/main?recursive=1"
FIR_RAW = "https://raw.githubusercontent.com/GICodeWarrior/fir/main/{path}"
ALIASES = {"RareMaterials": "FacilityMaterials12"}
VARIANTS = ("", "-crated")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "foxbot-icon-builder"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def icon_index() -> dict[str, str]:
    tree = json.loads(fetch(FIR_TREE).decode("utf-8"))["tree"]
    versions: dict[str, int] = {}
    for entry in tree:
        match = re.fullmatch(r"foxhole/([a-z]+-(\d+))/icons/.+\.png", entry["path"])
        if match:
            versions[match.group(1)] = int(match.group(2))
    index: dict[str, str] = {}
    for version in sorted(versions, key=versions.get):
        prefix = f"foxhole/{version}/icons/"
        for entry in tree:
            if entry["path"].startswith(prefix) and entry["path"].endswith(".png"):
                index[entry["path"][len(prefix) : -4]] = entry["path"]
    return index


def main() -> None:
    codes = [item["code"] for item in json.loads(CATALOG.read_text(encoding="utf-8"))["items"]]
    index = icon_index()
    jobs: list[tuple[str, str]] = []
    missing: list[str] = []
    for code in codes:
        source = code if code in index else ALIASES.get(code, code)
        if source not in index:
            missing.append(code)
            continue
        for variant in VARIANTS:
            if source + variant in index:
                jobs.append((code + variant, index[source + variant]))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT.glob("*.png"):
        old.unlink()

    def download(job: tuple[str, str]) -> None:
        name, path = job
        (OUTPUT / f"{name}.png").write_bytes(fetch(FIR_RAW.format(path=path)))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(download, jobs))
    print(f"Wrote {len(jobs)} icons to {OUTPUT}", file=sys.stderr)
    if missing:
        print(f"No icon for {len(missing)} item(s): {', '.join(sorted(missing))}", file=sys.stderr)


if __name__ == "__main__":
    main()
