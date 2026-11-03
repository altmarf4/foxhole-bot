import io
import json
import math
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "foxbot" / "resources" / "maps"
LAYOUT_URL = "https://raw.githubusercontent.com/notbadjon/foxhole-hexes/HEAD/hexes.json"
WARAPI_TREE = "https://api.github.com/repos/clapfoot/warapi/git/trees/master?recursive=1"
WARAPI_RAW = "https://raw.githubusercontent.com/clapfoot/warapi/master/{path}"
HEX_WIDTH = 1024
WORLD_HEX_WIDTH = 200
WORLD_PADDING = 16
WEBP_QUALITY = 82
SEAM_OVERLAP = 2


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "foxbot-map-builder"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def hex_center(q: float, p: float, width: float) -> tuple[float, float]:
    height = math.sqrt(3) * width / 2
    return p * 0.75 * width, height * (q + p / 2)


def main() -> None:
    layout = json.loads(fetch(LAYOUT_URL).decode("utf-8"))
    tree = json.loads(fetch(WARAPI_TREE).decode("utf-8"))["tree"]
    images = {
        Path(entry["path"]).stem.casefold(): entry["path"]
        for entry in tree
        if entry["path"].startswith("Images/Maps/") and entry["path"].lower().endswith(".tga")
    }
    jobs: list[tuple[str, str]] = []
    missing: list[str] = []
    for key, entry in layout.items():
        stem = Path(entry.get("file") or f"Map{key}.png").stem.casefold()
        source = images.get(stem) or images.get(f"map{key}".casefold()) or images.get(f"map{key}hex".casefold())
        if source is None:
            missing.append(key)
        else:
            jobs.append((key, source))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT.glob("*.webp"):
        old.unlink()

    def convert(job: tuple[str, str]) -> tuple[str, Image.Image]:
        key, source = job
        with Image.open(io.BytesIO(fetch(WARAPI_RAW.format(path=source)))) as raw:
            image = raw.convert("RGBA")
        if image.width != HEX_WIDTH:
            image = image.resize((HEX_WIDTH, round(image.height * HEX_WIDTH / image.width)), Image.LANCZOS)
        image.save(OUTPUT / f"{key}.webp", "WEBP", quality=WEBP_QUALITY, method=6)
        return key, image

    with ThreadPoolExecutor(max_workers=6) as pool:
        converted = dict(pool.map(convert, jobs))

    world_height = round(math.sqrt(3) * WORLD_HEX_WIDTH / 2)
    centers = {}
    for key in converted:
        entry = layout[key]
        centers[key] = hex_center(float(entry["q"]), float(entry.get("r", entry.get("p"))), WORLD_HEX_WIDTH)
    left = min(x for x, _ in centers.values()) - WORLD_HEX_WIDTH / 2 - WORLD_PADDING
    top = min(y for _, y in centers.values()) - world_height / 2 - WORLD_PADDING
    right = max(x for x, _ in centers.values()) + WORLD_HEX_WIDTH / 2 + WORLD_PADDING
    bottom = max(y for _, y in centers.values()) + world_height / 2 + WORLD_PADDING
    world = Image.new("RGBA", (math.ceil(right - left), math.ceil(bottom - top)), (0, 0, 0, 0))
    placed = {}
    for key, image in converted.items():
        cx, cy = centers[key][0] - left, centers[key][1] - top
        tile = image.resize((WORLD_HEX_WIDTH + SEAM_OVERLAP * 2, world_height + SEAM_OVERLAP * 2), Image.LANCZOS)
        world.alpha_composite(tile, (round(cx - WORLD_HEX_WIDTH / 2) - SEAM_OVERLAP, round(cy - world_height / 2) - SEAM_OVERLAP))
        placed[key] = {"cx": round(cx, 2), "cy": round(cy, 2)}
    world.save(OUTPUT / "world.webp", "WEBP", quality=WEBP_QUALITY, method=6)
    (OUTPUT / "layout.json").write_text(
        json.dumps(
            {
                "hex_width": WORLD_HEX_WIDTH,
                "hex_height": world_height,
                "width": world.width,
                "height": world.height,
                "hexes": dict(sorted(placed.items())),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    total = sum(path.stat().st_size for path in OUTPUT.iterdir())
    print(f"Wrote {len(converted)} hex maps and a {world.width}x{world.height} world map ({total // 1024} KB)", file=sys.stderr)
    if missing:
        print(f"No map image for: {', '.join(sorted(missing))}", file=sys.stderr)


if __name__ == "__main__":
    main()
