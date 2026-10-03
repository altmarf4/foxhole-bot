from __future__ import annotations

import codecs
import csv
import datetime as dt
import io
import re
from collections import Counter
from dataclasses import dataclass, field

from foxbot.core.catalog import Catalog, Item, name_key

CLIPBOARD = "clipboard"
FIR = "fir"
APP = "app"
SOURCE_LABELS = {
    CLIPBOARD: "in-game Copy to Clipboard text",
    FIR: "FIR export",
    APP: "Foxhole Stockpiles app export",
}

FACTIONS = ("warden", "colonial")
UNKNOWN_PREFIX = "unknown:"
CLIPBOARD_TIME_FORMAT = "%Y.%m.%d-%H.%M.%S"

CRATE_WORDS = {
    "crate",
    "crates",
    "kiste",
    "kisten",
    "caisse",
    "caisses",
    "caixa",
    "caixas",
    "caja",
    "cajas",
    "cassa",
    "casse",
    "skrzynia",
    "ящик",
    "ящики",
    "箱",
    "板条箱",
}

STORE_TYPE_ALIASES = {
    "seehafen": "Seaport",
    "hafen": "Seaport",
    "port": "Seaport",
    "port maritime": "Seaport",
    "porto": "Seaport",
    "porto marítimo": "Seaport",
    "морской порт": "Seaport",
    "порт": "Seaport",
    "港口": "Seaport",
    "lagerhaus": "Storage Depot",
    "lagerdepot": "Storage Depot",
    "depot": "Storage Depot",
    "dépôt": "Storage Depot",
    "dépôt de stockage": "Storage Depot",
    "depósito": "Storage Depot",
    "depósito de armazenamento": "Storage Depot",
    "склад": "Storage Depot",
    "仓库": "Storage Depot",
    "flugzeugdepot": "Aircraft Depot",
    "dépôt d'avions": "Aircraft Depot",
}

HEADER_RE = re.compile(
    r"^(?P<head>.*?)\s*-\s*X:\s*(?P<x>-?\d+(?:[.,]\d+)?)\s+Y:\s*(?P<y>-?\d+(?:[.,]\d+)?)"
    r"\s*(?:,\s*(?P<stamp>\d{4}\.\d{1,2}\.\d{1,2}-\d{1,2}\.\d{1,2}\.\d{1,2}))?\s*,?\s*$",
    re.IGNORECASE,
)
CRATE_SUFFIX_RE = re.compile(r"^(?P<base>.*\S)\s*\((?P<word>[^()]*)\)\s*$")
FULLWIDTH = str.maketrans({"（": "(", "）": ")", "，": ","})

FIR_REQUIRED = {"codename", "quantity", "name"}
FIR_MARKERS = {"stockpile title", "structure type", "crated?"}
APP_REQUIRED = {"stockpile name", "code", "quantity"}


class ParseError(ValueError):
    pass


@dataclass
class RawLine:
    name: str
    quantity: int
    crated: bool | None = None
    code: str | None = None
    per_crate: int | None = None


@dataclass
class ParsedItem:
    code: str
    name: str
    category: str
    crated: bool
    quantity: int
    per_crate: int
    known: bool

    @property
    def units(self) -> int:
        return self.quantity * self.per_crate if self.crated else self.quantity


@dataclass
class ParsedStock:
    hex: str = ""
    region: str = ""
    store_type: str = ""
    name: str = ""
    game_time: int | None = None
    has_header: bool = False
    lines: list[RawLine] = field(default_factory=list)
    items: list[ParsedItem] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    source: str
    faction: str | None
    stocks: list[ParsedStock]
    skipped: int = 0


def normalise(text: str | None) -> str:
    return name_key(text or "")


def stock_key(hex_name: str, region: str, store_type: str, name: str) -> str:
    return "|".join(normalise(part) for part in (hex_name, region, store_type, name))


def parse_int(text: str | None) -> int | None:
    cleaned = str(text or "").strip().replace("\u00a0", "").replace(" ", "")
    match = re.fullmatch(r"(-?\d+)(?:\.0+)?", cleaned)
    return int(match.group(1)) if match else None


def parse_bool(text: str | None) -> bool:
    return str(text or "").strip().casefold() in {"true", "yes", "y", "1", "x", "crated", "wahr", "vrai"}


def parse_clipboard_time(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        moment = dt.datetime.strptime(stamp.strip(), CLIPBOARD_TIME_FORMAT)
    except ValueError:
        return None
    return int(moment.replace(tzinfo=dt.UTC).timestamp())


def parse_app_time(text: str | None) -> int | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    number = parse_int(raw)
    if number is not None:
        if number > 10**12:
            return number // 1000
        return number if number > 10**9 else None
    clipboard = parse_clipboard_time(raw)
    if clipboard is not None:
        return clipboard
    try:
        moment = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp())


def format_game_time(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(int(timestamp), dt.UTC).strftime("%Y-%m-%d %H:%M")


def split_crate_suffix(name: str) -> tuple[str, bool]:
    match = CRATE_SUFFIX_RE.match(name)
    if match and match.group("word").strip().casefold() in CRATE_WORDS:
        return match.group("base").strip(), True
    return name.strip(), False


def canonical_store_type(text: str, known_types: list[str]) -> str:
    key = normalise(text)
    if not key:
        return ""
    for known in known_types:
        if normalise(known) == key:
            return known
    alias = STORE_TYPE_ALIASES.get(key)
    if alias is not None:
        for known in known_types:
            if normalise(known) == normalise(alias):
                return known
        return alias
    return re.sub(r"\s+", " ", text).strip()


def decode_upload(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_header(line: str) -> ParsedStock | None:
    match = HEADER_RE.match(line.strip())
    if match is None:
        return None
    head = match.group("head").strip()
    parts = [part.strip() for part in re.split(r"\s+-\s+", head, maxsplit=3)] if head else []
    parts += [""] * (4 - len(parts))
    return ParsedStock(
        hex=parts[0],
        region=parts[1],
        store_type=parts[2],
        name=parts[3],
        game_time=parse_clipboard_time(match.group("stamp")),
        has_header=True,
    )


def parse_item_line(line: str) -> RawLine | None:
    name, separator, quantity_text = line.translate(FULLWIDTH).rpartition(",")
    if not separator:
        return None
    quantity = parse_int(quantity_text)
    name = name.strip()
    if quantity is None or not name:
        return None
    return RawLine(name=name, quantity=quantity)


def parse_clipboard(text: str) -> tuple[list[ParsedStock], int]:
    stocks: list[ParsedStock] = []
    current: ParsedStock | None = None
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        header = parse_header(line)
        if header is not None:
            current = header
            stocks.append(current)
            continue
        parsed = parse_item_line(line)
        if parsed is None:
            skipped += 1
            continue
        if current is None:
            current = ParsedStock()
            stocks.append(current)
        current.lines.append(parsed)
    return stocks, skipped


def table_delimiter(header_line: str) -> str:
    counts = {delimiter: header_line.count(delimiter) for delimiter in ("\t", ",", ";")}
    if counts["\t"]:
        return "\t"
    return "," if counts[","] >= counts[";"] else ";"


def header_cells(header_line: str) -> set[str]:
    delimiter = table_delimiter(header_line)
    return {cell.strip().strip('"').strip().casefold() for cell in header_line.split(delimiter)}


def read_table(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    delimiter = table_delimiter(lines[0])
    if delimiter == "\t":
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter="\t", quoting=csv.QUOTE_NONE)
    else:
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []
    header = [cell.strip().strip('"').strip().casefold() for cell in rows[0]]
    table = []
    for row in rows[1:]:
        table.append({column: (row[index].strip().strip('"').strip() if index < len(row) else "") for index, column in enumerate(header)})
    return table


def detect_format(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    cells = header_cells(lines[0])
    if FIR_REQUIRED <= cells and cells & FIR_MARKERS:
        return FIR
    if APP_REQUIRED <= cells:
        return APP
    if any(HEADER_RE.match(line) for line in lines):
        return CLIPBOARD
    readable = sum(1 for line in lines if parse_item_line(line) is not None)
    if readable and readable * 2 >= len(lines):
        return CLIPBOARD
    return None


def parse_fir(text: str) -> tuple[list[ParsedStock], int]:
    groups: dict[tuple[str, str, str], ParsedStock] = {}
    skipped = 0
    for row in read_table(text):
        quantity = parse_int(row.get("quantity"))
        name = row.get("name", "")
        code = row.get("codename", "") or None
        if quantity is None or not (name or code):
            skipped += 1
            continue
        title = row.get("stockpile title", "")
        stock_name = row.get("stockpile name", "")
        structure = row.get("structure type", "")
        key = (title, stock_name, structure)
        stock = groups.get(key)
        if stock is None:
            stock = ParsedStock(store_type=structure, name=stock_name or title, has_header=True)
            groups[key] = stock
        stock.lines.append(
            RawLine(
                name=name or code or "",
                quantity=quantity,
                crated=parse_bool(row.get("crated?")),
                code=code,
                per_crate=parse_int(row.get("per crate")),
            )
        )
    return list(groups.values()), skipped


def parse_app(text: str) -> tuple[list[ParsedStock], int]:
    groups: dict[tuple[str, str], ParsedStock] = {}
    skipped = 0
    for row in read_table(text):
        quantity = parse_int(row.get("quantity"))
        code = row.get("code", "")
        if quantity is None or not code:
            skipped += 1
            continue
        key = (row.get("stockpile name", ""), row.get("stockpile type", ""))
        stock = groups.get(key)
        if stock is None:
            stock = ParsedStock(store_type=key[1], name=key[0], has_header=True)
            groups[key] = stock
        game_time = parse_app_time(row.get("ingame time"))
        if game_time is not None and (stock.game_time is None or game_time > stock.game_time):
            stock.game_time = game_time
        stock.lines.append(RawLine(name=code, quantity=quantity, crated=parse_bool(row.get("crated")), code=code))
    return list(groups.values()), skipped


def lookup_line(catalog: Catalog, line: RawLine, faction: str | None) -> tuple[Item | None, bool]:
    if line.code:
        item = catalog.get(line.code)
        if item is not None:
            return item, bool(line.crated)
    display = line.name.translate(FULLWIDTH).strip()
    if line.crated is None:
        return catalog.resolve_line(display, faction)
    item = catalog.lookup(display, faction)
    if item is None:
        item, _ = catalog.resolve_line(display, faction)
    return item, bool(line.crated)


def infer_faction(catalog: Catalog, stocks: list[ParsedStock]) -> str | None:
    counts: Counter[str] = Counter()
    for stock in stocks:
        for line in stock.lines:
            item, _ = lookup_line(catalog, line, None)
            if item is not None and item.faction in FACTIONS:
                counts[item.faction] += 1
    if counts["warden"] > counts["colonial"]:
        return "warden"
    if counts["colonial"] > counts["warden"]:
        return "colonial"
    return None


def resolve_stock(catalog: Catalog, stock: ParsedStock, faction: str | None) -> None:
    merged: dict[tuple[str, bool], ParsedItem] = {}
    unknown: list[str] = []
    for line in stock.lines:
        if line.quantity <= 0:
            continue
        item, crated = lookup_line(catalog, line, faction)
        if item is not None:
            code, name, category, per_crate, known = item.code, item.name, item.category, item.per_crate, True
        else:
            base, suffix_crated = split_crate_suffix(line.name.translate(FULLWIDTH))
            crated = bool(line.crated) if line.crated is not None else suffix_crated
            name = re.sub(r"\s+", " ", base).strip()
            code = UNKNOWN_PREFIX + normalise(name)
            category = ""
            per_crate = line.per_crate if line.per_crate and line.per_crate > 0 else 1
            known = False
            if name not in unknown:
                unknown.append(name)
        key = (code, crated)
        existing = merged.get(key)
        if existing is not None:
            existing.quantity += line.quantity
            continue
        merged[key] = ParsedItem(code, name, category, crated, line.quantity, per_crate, known)
    stock.items = list(merged.values())
    stock.unknown = unknown


def parse_inventory(text: str, catalog: Catalog, faction: str | None = None) -> ParseResult:
    text = (text or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ParseError("There was nothing to import.")
    source = detect_format(text)
    if source is None:
        raise ParseError(
            "That does not look like a stockpile export. In game, open the stockpile, press **Copy to Clipboard** "
            "and paste the whole text. FIR TSV and Foxhole Stockpiles app CSV/TSV exports also work."
        )
    if source == FIR:
        stocks, skipped = parse_fir(text)
    elif source == APP:
        stocks, skipped = parse_app(text)
    else:
        stocks, skipped = parse_clipboard(text)
    if not stocks:
        raise ParseError("No stockpile contents were found in that text.")
    chosen = faction if faction in FACTIONS else infer_faction(catalog, stocks)
    for stock in stocks:
        resolve_stock(catalog, stock, chosen)
    return ParseResult(source=source, faction=chosen, stocks=stocks, skipped=skipped)
