from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from foxbot.constants import Colour
from foxbot.core import settings as keys
from foxbot.core import timeutil
from foxbot.core.boards import BoardButton, BoardRender
from foxbot.core.locations import hex_display_name, normalise
from foxbot.core.permissions import check_admin, interaction_member
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_FIELD_LIMIT, clip, md, paginate_embeds
from foxbot.core.ui import ConfirmView, EmbedPaginator, PickerView, PickOption, deny, edit, reply, report_error
from foxbot.core.warapi import MAPS_PATH, WAR_PATH, WarApiClient, WarApiError, dynamic_path, report_path, static_path
from foxbot.db import Transaction

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

KIND = "war"
COG_NAME = "War"
STATE_KEY = "war_state"

POLL_MINUTES = 5
REPORT_INTERVAL = 30 * 60
MAPS_INTERVAL = 6 * 3600
STALE_AFTER = 20 * 60
BOARD_EVENTS = 10
CAPTURES_LIMIT = 1000
COORD_DIGITS = 4
DAY_MS = 86_400_000
LINE_LIMIT = 300
ALERT_BUDGET = EMBED_DESCRIPTION_LIMIT - 200
HOME_REGION_PREFIX = "HomeRegion"
REPORT_TOTALS = {"enlistments": "totalEnlistments", "wardens": "wardenCasualties", "colonials": "colonialCasualties"}

TOWN_ICONS = {
    56: "Town Base (tier 1)",
    57: "Town Base (tier 2)",
    58: "Town Base (tier 3)",
    45: "Relic Base (tier 1)",
    46: "Relic Base (tier 2)",
    47: "Relic Base (tier 3)",
    27: "Keep",
}
FLAG_VICTORY = 0x01
FLAG_SCORCHED = 0x10

WARDENS = "WARDENS"
COLONIALS = "COLONIALS"
NEUTRAL = "NONE"
TEAM_NAMES = {WARDENS: "Wardens", COLONIALS: "Colonials", NEUTRAL: "Neutral"}
FACTION_TEAMS = {"warden": WARDENS, "wardens": WARDENS, "colonial": COLONIALS, "colonials": COLONIALS}

ALERT_OFF = "off"
ALERT_OURS = "ours"
ALERT_ALL = "all"
ALERT_MODES = (ALERT_OFF, ALERT_OURS, ALERT_ALL)

WAR_FIELDS = (
    "war_id",
    "war_number",
    "winner",
    "conquest_start",
    "conquest_end",
    "resistance_start",
    "scheduled_end",
    "required_victory_towns",
)

KEPT_TEXT = "Settings, permissions, boards, Rare Alloys and tickets are kept."
NO_DATA_TEXT = "No War API data yet. The bot checks the War API every 5 minutes and this updates on its own."


@dataclass(frozen=True)
class WarTable:
    table: str
    noun: str
    plural_noun: str
    module: str | None = None
    kind: str | None = None


WAR_TABLES = [
    WarTable("stockpiles", "stockpile", "stockpiles", "foxbot.features.stockpiles", "stockpile"),
    WarTable("ships", "ship", "ships", "foxbot.features.ships", "ship"),
    WarTable("msupps_bases", "msupps base", "msupps bases", "foxbot.features.msupps", "msupps"),
    WarTable("inventory_snapshots", "inventory snapshot", "inventory snapshots"),
    WarTable("orders", "order", "orders", "foxbot.features.orders", "orders"),
    WarTable("logi_runs", "logi run", "logi runs", "foxbot.features.logi", "logi"),
    WarTable("facility_queue", "facility queue entry", "facility queue entries", "foxbot.features.facility", "facility"),
]
TABLES_BY_NAME = {spec.table: spec for spec in WAR_TABLES}
DELETE_ORDER = ["inventory_snapshots", "orders", "stockpiles", "ships", "msupps_bases", "logi_runs", "facility_queue"]
ENTRY_TABLES = ("entry_access", "entry_subscribers", "entry_alert_roles", "entry_screenshots", "alert_state", "alert_messages")
CHILD_ROWS = {
    "inventory_snapshots": (
        "DELETE FROM inventory_items WHERE snapshot_id IN (SELECT id FROM inventory_snapshots WHERE guild_id = ?)",
    ),
    "orders": (
        "DELETE FROM order_contributions WHERE order_id IN (SELECT id FROM orders WHERE guild_id = ?)",
        "DELETE FROM order_lines WHERE order_id IN (SELECT id FROM orders WHERE guild_id = ?)",
    ),
}
MESSAGE_COLUMNS = {
    "orders": ("channel_id", "message_id"),
    "logi_runs": ("ping_channel_id", "ping_message_id"),
}

_archive_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass
class TownChange:
    map_name: str
    hex: str
    town: str
    old_team: str
    new_team: str
    victory: bool


@dataclass
class PollOutcome:
    ok: bool = False
    first_run: bool = False
    new_war: bool = False
    war_over: bool = False
    changed: bool = False
    failures: int = 0
    changes: list[TownChange] = field(default_factory=list)


@dataclass
class ArchiveResult:
    war_number: int | None
    backup: Path | None
    counts: dict[str, int]
    messages: list[tuple[int, int]]


def as_int(value: Any, default: int | None = 0) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_ms(value: Any) -> int | None:
    number = as_int(value, None)
    return number if number and number > 0 else None


def team_of(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in TEAM_NAMES else NEUTRAL


def team_name(team: str) -> str:
    return TEAM_NAMES.get(team, TEAM_NAMES[NEUTRAL])


def faction_team(value: Any) -> str | None:
    if not value:
        return None
    return FACTION_TEAMS.get(str(value).strip().casefold())


async def guild_team(bot: FoxBot, guild_id: int) -> str | None:
    return faction_team(await bot.settings.get(guild_id, keys.FACTION))


async def alert_mode(bot: FoxBot, guild_id: int) -> str:
    mode = str(await bot.settings.get(guild_id, keys.WAR_ALERT_MODE) or ALERT_OFF).lower()
    return mode if mode in ALERT_MODES else ALERT_OFF


def has_map_data(name: Any) -> bool:
    return isinstance(name, str) and bool(name) and not name.startswith(HOME_REGION_PREFIX)


def is_town(item: dict) -> bool:
    return as_int(item.get("iconType"), None) in TOWN_ICONS


def coordinate(value: Any) -> float:
    return round(float(value), COORD_DIGITS)


def town_label(town: str) -> str:
    return town or "Unknown town"


def nearest_name(text_items: list[dict], x: float, y: float) -> str:
    for marker in ("Major", "Minor", None):
        candidates = []
        for item in text_items:
            text = str(item.get("text") or "").strip()
            if not text or (marker is not None and item.get("mapMarkerType") != marker):
                continue
            try:
                distance = (float(item["x"]) - x) ** 2 + (float(item["y"]) - y) ** 2
            except (KeyError, TypeError, ValueError):
                continue
            candidates.append((distance, text))
        if candidates:
            return min(candidates)[1]
    return ""


async def load_state(bot: FoxBot) -> dict | None:
    raw = await bot.db.fetchval("SELECT value FROM meta WHERE key = ?", (STATE_KEY,))
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        log.warning("Stored war state is not valid JSON; starting over")
        return None
    return value if isinstance(value, dict) else None


async def save_state(bot: FoxBot, state: dict) -> None:
    await bot.db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (STATE_KEY, json.dumps(state, sort_keys=True)),
    )


async def town_rows(bot: FoxBot, map_name: str) -> list[dict]:
    return await bot.db.fetchall("SELECT * FROM war_towns WHERE map_name = ? ORDER BY town, x, y", (map_name,))


async def victory_counts(bot: FoxBot) -> dict[str, int]:
    rows = await bot.db.fetchall(
        "SELECT team, COUNT(*) AS towns FROM war_towns WHERE flags & ? GROUP BY team",
        (FLAG_VICTORY,),
    )
    return {row["team"]: row["towns"] for row in rows}


async def scorched_victory_towns(bot: FoxBot) -> int:
    return int(
        await bot.db.fetchval(
            "SELECT COUNT(*) FROM war_towns WHERE flags & ? AND flags & ?",
            (FLAG_VICTORY, FLAG_SCORCHED),
            0,
        )
    )


async def recent_events(bot: FoxBot, war_id: str | None, limit: int) -> list[dict]:
    if not war_id:
        return []
    return await bot.db.fetchall(
        "SELECT * FROM war_events WHERE war_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (war_id, limit),
    )


async def hex_summaries(bot: FoxBot) -> list[tuple[str, str, dict[str, int]]]:
    rows = await bot.db.fetchall(
        "SELECT map_name, MIN(hex) AS hex, team, COUNT(*) AS towns FROM war_towns GROUP BY map_name, team"
    )
    grouped: dict[str, tuple[str, dict[str, int]]] = {}
    for row in rows:
        hex_name, counts = grouped.setdefault(row["map_name"], (row["hex"], {}))
        counts[row["team"]] = row["towns"]
    return sorted(((name, hex_name, counts) for name, (hex_name, counts) in grouped.items()), key=lambda item: item[1].lower())


def team_counts_text(counts: dict[str, int]) -> str:
    return " | ".join(f"{team_name(team)} {counts.get(team, 0)}" for team in (WARDENS, COLONIALS, NEUTRAL))


def day_of_war(state: dict, now: int) -> int | None:
    start = as_ms(state.get("conquest_start"))
    if start is None:
        return None
    end = as_ms(state.get("conquest_end")) or now * 1000
    return max(1, (end - start) // DAY_MS + 1)


def phase_text(state: dict) -> str:
    winner = team_of(state.get("winner"))
    if winner != NEUTRAL:
        resistance = " (resistance phase)" if as_ms(state.get("resistance_start")) else ""
        return f"Over: the {team_name(winner)} won{resistance}"
    if as_ms(state.get("conquest_start")) is None:
        return "Not started yet"
    return "In progress"


def war_title(state: dict | None) -> str:
    number = as_int((state or {}).get("war_number"), None)
    return f"War {number}" if number else "War"


def event_line(row: dict) -> str:
    victory = " (victory town)" if row.get("victory") else ""
    text = (
        f"{timeutil.relative(row['created_at'])} **{md(row['hex'])}** - {md(town_label(row['town']))}: "
        f"{team_name(row['old_team'])} -> {team_name(row['new_team'])}{victory}"
    )
    return clip(text, LINE_LIMIT)


def fit_lines(lines: list[str], budget: int, more: str) -> str:
    kept: list[str] = []
    used = 0
    for line in lines:
        line = clip(line, LINE_LIMIT)
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    hidden = len(lines) - len(kept)
    text = "\n".join(kept)
    if hidden:
        text += f"\n...and {hidden} more." + (f" {more}" if more else "")
    return text


def victory_text(state: dict, counts: dict[str, int], scorched: int) -> str:
    required = as_int(state.get("required_victory_towns"), None)
    needed = max(0, required - scorched) if required else None
    target = f" of {needed}" if needed is not None else ""
    rows = [
        f"Wardens: **{counts.get(WARDENS, 0)}**{target}",
        f"Colonials: **{counts.get(COLONIALS, 0)}**{target}",
    ]
    if counts.get(NEUTRAL):
        rows.append(f"Neutral: {counts[NEUTRAL]}")
    if scorched:
        lowered = f", so {needed} are needed instead of {required}" if required else ""
        rows.append(f"Scorched: {scorched}{lowered}")
    return "\n".join(rows)


def reports_fields(state: dict) -> tuple[str, str]:
    reports = state.get("reports") or {}
    if not reports.get("updated_at"):
        return "Not loaded yet.", "Not loaded yet."
    casualties = f"Wardens: **{as_int(reports.get('wardens')):,}**\nColonials: **{as_int(reports.get('colonials')):,}**"
    return casualties, f"**{as_int(reports.get('enlistments')):,}**"


def status_embed(state: dict | None, counts: dict[str, int], scorched: int, events: list[dict], *, now: int | None = None) -> discord.Embed:
    now = now if now is not None else timeutil.now()
    if not state:
        embed = discord.Embed(title="War", description=NO_DATA_TEXT, colour=Colour.MUTED)
        embed.set_footer(text="Live data from the official War API")
        return embed
    lines = [f"Status: **{phase_text(state)}**"]
    day = day_of_war(state, now)
    start = as_ms(state.get("conquest_start"))
    if day is not None and start is not None:
        lines.append(f"Day **{day}** of the war, started {timeutil.full(start // 1000)}")
    polled = as_int(state.get("polled_at"), None)
    if polled:
        lines.append(f"War API data updated {timeutil.relative(polled)}")
        if now - polled > STALE_AFTER:
            lines.append("The War API could not be reached recently, so this shows the last data received.")
    lines.append("")
    lines.append("**Recent town changes**")
    if events:
        lines.append(fit_lines([event_line(row) for row in events], EMBED_DESCRIPTION_LIMIT - 1200, "Press Recent Captures for all of them."))
    else:
        since = as_int(state.get("tracking_since"), None)
        when = f" {timeutil.relative(since)}" if since else ""
        lines.append(f"No town has changed hands since the bot started tracking this war{when}.")
    winner = team_of(state.get("winner"))
    colour = Colour.GOLD if winner != NEUTRAL else Colour.INFO
    embed = discord.Embed(title=war_title(state), description="\n".join(lines), colour=colour)
    casualties, enlistments = reports_fields(state)
    embed.add_field(name="Victory towns", value=clip(victory_text(state, counts, scorched), EMBED_FIELD_LIMIT), inline=True)
    embed.add_field(name="Casualties", value=casualties, inline=True)
    embed.add_field(name="Enlistments", value=enlistments, inline=True)
    embed.set_footer(text="Live data from the official War API | Checked every 5 minutes")
    return embed


async def build_status_embed(bot: FoxBot) -> discord.Embed:
    state = await load_state(bot)
    if not state:
        return status_embed(None, {}, 0, [])
    counts = await victory_counts(bot)
    scorched = await scorched_victory_towns(bot)
    events = await recent_events(bot, state.get("war_id"), BOARD_EVENTS)
    return status_embed(state, counts, scorched, events)


def change_line(change: TownChange, our_team: str | None) -> str:
    victory = " (victory town)" if change.victory else ""
    text = (
        f"**{md(change.hex)}** - {md(town_label(change.town))}: "
        f"{team_name(change.old_team)} -> {team_name(change.new_team)}{victory}"
    )
    if our_team is not None and change.old_team == our_team:
        text += " - we lost it"
    elif our_team is not None and change.new_team == our_team:
        text += " - we took it"
    return text


def alert_colour(changes: list[TownChange], our_team: str | None) -> int:
    if our_team is None:
        return Colour.INFO
    lost = any(change.old_team == our_team for change in changes)
    gained = any(change.new_team == our_team for change in changes)
    if lost and gained:
        return Colour.WARNING
    if lost:
        return Colour.DANGER
    if gained:
        return Colour.SUCCESS
    return Colour.INFO


def capture_embed(changes: list[TownChange], our_team: str | None, state: dict) -> discord.Embed:
    title = "Town Changed Hands" if len(changes) == 1 else f"{len(changes)} Towns Changed Hands"
    description = fit_lines([change_line(change, our_team) for change in changes], ALERT_BUDGET, "Use `/war captures` for the full list.")
    embed = discord.Embed(
        title=title,
        description=description,
        colour=alert_colour(changes, our_team),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.set_footer(text=f"{war_title(state)} | Data from the official War API")
    return embed


def new_war_embed(state: dict) -> discord.Embed:
    number = as_int(state.get("war_number"), None)
    previous = as_int(state.get("previous_war_number"), None)
    started = f"War **{number}** has started." if number else "A new war has started."
    previous_text = f"war {previous}" if previous else "the previous war"
    description = (
        f"{started} Town tracking was reset for the new war.\n\n"
        f"Admins can press **Archive Previous War** to clean up {previous_text}: the bot saves a backup copy of its "
        "database, then deletes this server's stockpiles, ships, msupps bases, inventory snapshots, orders, logi runs "
        f"and facility queue. {KEPT_TEXT}\n\n"
        "Remember to check this server's faction in `/settings` for the new war."
    )
    embed = discord.Embed(title="A New War Has Started", description=description, colour=Colour.INFO)
    embed.set_footer(text="Data from the official War API")
    return embed


def war_over_embed(state: dict) -> discord.Embed:
    winner = team_name(team_of(state.get("winner")))
    number = as_int(state.get("war_number"), None)
    which = f"war **{number}**" if number else "the war"
    lines = [f"The **{winner}** won {which}."]
    day = day_of_war(state, timeutil.now())
    if day is not None:
        lines.append(f"It lasted {day} day(s).")
    lines.append("The bot announces the next war when it starts.")
    embed = discord.Embed(title=f"{war_title(state)} Is Over", description="\n".join(lines), colour=Colour.GOLD)
    embed.set_footer(text="Data from the official War API")
    return embed


async def notice_channel(bot: FoxBot, guild: discord.Guild):
    for setting in (keys.WAR_ALERT_CHANNEL, keys.ALERT_CHANNEL):
        channel_id = await bot.settings.get(guild.id, setting)
        if channel_id:
            channel = guild.get_channel_or_thread(int(channel_id))
            if channel is not None:
                return channel
    board_channel = await bot.boards.channel_id_for(guild.id, KIND)
    if board_channel:
        return guild.get_channel_or_thread(int(board_channel))
    return None


class ArchiveWarItem(discord.ui.DynamicItem[discord.ui.Button], template=r"fw:archive:(?P<war>\d+)"):
    def __init__(self, war_number: int):
        super().__init__(
            discord.ui.Button(
                label="Archive Previous War",
                style=discord.ButtonStyle.danger,
                custom_id=f"fw:archive:{int(war_number)}",
            )
        )
        self.war_number = int(war_number)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["war"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await deny(interaction, "This only works inside the server.")
            return
        try:
            await start_archive(interaction.client, interaction, self.war_number or None, from_notice=True)
        except Exception as error:
            await report_error(interaction, error, "war archive button")


def new_war_view(state: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ArchiveWarItem(as_int(state.get("previous_war_number"), 0) or 0).item)
    return view


class WarService:
    def __init__(self, bot: FoxBot, client: WarApiClient | None = None):
        self.bot = bot
        self.client = client or WarApiClient(bot.config.war_api_url)
        self.clock = timeutil.now
        self.maps: list[str] = []
        self._maps_at = 0
        self._static: dict[str, list[dict]] = {}
        self._static_failed: set[str] = set()
        self._baselined: set[str] = set()
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if not self._loop.is_running():
            self._loop.start()

    async def stop(self) -> None:
        self._loop.cancel()
        await self.client.close()

    @tasks.loop(minutes=POLL_MINUTES)
    async def _loop(self) -> None:
        await self.poll_once()

    @_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def poll_once(self) -> PollOutcome:
        outcome = PollOutcome()
        async with self._lock:
            try:
                await self._poll(outcome)
            except Exception:
                log.exception("War API poll failed")
        return outcome

    async def _poll(self, outcome: PollOutcome) -> None:
        try:
            result = await self.client.get(WAR_PATH)
        except WarApiError as error:
            log.warning("Could not fetch the war state from the War API: %s", error)
            return
        war = result.data if isinstance(result.data, dict) else {}
        war_id = str(war.get("warId") or "").strip()
        if not war_id:
            log.warning("The War API returned a war without an id; skipping this poll")
            return
        now = self.clock()
        self._static_failed.clear()
        previous = await load_state(self.bot) or {}
        outcome.first_run = not previous.get("war_id")
        outcome.new_war = not outcome.first_run and previous["war_id"] != war_id
        if outcome.new_war:
            await self._reset_for_new_war()
            log.info("A new war started: %s (war %s)", war_id, war.get("warNumber"))
        state = self._next_state(previous, war, war_id, outcome)
        if not outcome.first_run and not outcome.new_war and state["winner"] != NEUTRAL and not previous.get("winner_announced"):
            outcome.war_over = True
            state["winner_announced"] = True
        changed = outcome.first_run or outcome.new_war or any(state.get(key) != previous.get(key) for key in WAR_FIELDS)
        maps = await self._map_names(force=outcome.new_war)
        record = state["conquest_start"] is not None
        for map_name in maps:
            try:
                fetched = await self.client.get(dynamic_path(map_name))
            except WarApiError as error:
                outcome.failures += 1
                log.warning("Could not fetch map data for %s: %s", map_name, error)
                continue
            if not fetched.changed and map_name in self._baselined:
                continue
            changes, touched = await self._apply_map(map_name, fetched.data, war_id, now, record=record)
            outcome.changes.extend(changes)
            changed = changed or touched
        if maps and await self._drop_missing_maps(maps):
            changed = True
        if await self._fill_missing_names():
            changed = True
        if await self._refresh_reports(state, maps, now, force=outcome.first_run or outcome.new_war):
            changed = True
        if maps and outcome.failures < len(maps):
            state["polled_at"] = now
        await save_state(self.bot, state)
        outcome.ok = True
        outcome.changed = changed or bool(outcome.changes)
        if outcome.new_war:
            await self._broadcast(new_war_embed(state), new_war_view(state), "new war notice")
        if outcome.war_over:
            await self._broadcast(war_over_embed(state), None, "war over notice")
        if outcome.changes:
            await self._send_alerts(outcome.changes, state)
        if outcome.changed:
            await self._refresh_boards()

    def _next_state(self, previous: dict, war: dict, war_id: str, outcome: PollOutcome) -> dict:
        fresh = outcome.first_run or outcome.new_war
        winner = team_of(war.get("winner"))
        state = {
            "war_id": war_id,
            "war_number": as_int(war.get("warNumber"), None),
            "winner": winner,
            "conquest_start": as_ms(war.get("conquestStartTime")),
            "conquest_end": as_ms(war.get("conquestEndTime")),
            "resistance_start": as_ms(war.get("resistanceStartTime")),
            "scheduled_end": as_ms(war.get("scheduledConquestEndTime")),
            "required_victory_towns": as_int(war.get("requiredVictoryTowns"), None),
            "winner_announced": winner != NEUTRAL if fresh else bool(previous.get("winner_announced")),
            "reports": {} if outcome.new_war else dict(previous.get("reports") or {}),
            "polled_at": previous.get("polled_at"),
            "tracking_since": self.clock() if fresh else previous.get("tracking_since"),
        }
        if outcome.new_war:
            state["previous_war_number"] = as_int(previous.get("war_number"), None)
        else:
            state["previous_war_number"] = previous.get("previous_war_number")
        return state

    async def _reset_for_new_war(self) -> None:
        await self.bot.db.execute("DELETE FROM war_towns")
        self._baselined.clear()
        self._static.clear()
        self._maps_at = 0

    async def _map_names(self, *, force: bool) -> list[str]:
        if self.maps and not force and self.clock() - self._maps_at < MAPS_INTERVAL:
            return self.maps
        try:
            result = await self.client.get(MAPS_PATH)
        except WarApiError as error:
            log.warning("Could not fetch the map list from the War API: %s", error)
            return self.maps
        names = [name for name in result.data if has_map_data(name)] if isinstance(result.data, list) else []
        if names:
            self.maps = list(dict.fromkeys(names))
            self._maps_at = self.clock()
        return self.maps

    async def _static_items(self, map_name: str) -> list[dict] | None:
        if map_name in self._static:
            return self._static[map_name]
        if map_name in self._static_failed:
            return None
        try:
            result = await self.client.get(static_path(map_name))
        except WarApiError as error:
            self._static_failed.add(map_name)
            log.warning("Could not fetch town names for %s: %s", map_name, error)
            return None
        data = result.data if isinstance(result.data, dict) else {}
        items = [item for item in data.get("mapTextItems") or [] if isinstance(item, dict) and str(item.get("text") or "").strip()]
        self._static[map_name] = items
        return items

    async def _apply_map(self, map_name: str, data: Any, war_id: str, now: int, *, record: bool = True) -> tuple[list[TownChange], bool]:
        items = data.get("mapItems") if isinstance(data, dict) else None
        if not isinstance(items, list):
            log.warning("Map data for %s has no map items", map_name)
            return [], False
        current: dict[tuple[float, float], dict] = {}
        for item in items:
            if not isinstance(item, dict) or not is_town(item):
                continue
            try:
                current[(coordinate(item["x"]), coordinate(item["y"]))] = item
            except (KeyError, TypeError, ValueError):
                continue
        existing = {(row["x"], row["y"]): row for row in await town_rows(self.bot, map_name)}
        static: list[dict] | None = None
        if any(not (existing.get(key) or {}).get("town") for key in current):
            static = await self._static_items(map_name)
        hex_name = hex_display_name(map_name)
        baselined = record and map_name in self._baselined
        changes: list[TownChange] = []
        touched = False
        async with self.bot.db.transaction() as tx:
            for key, item in current.items():
                team = team_of(item.get("teamId"))
                flags = as_int(item.get("flags"), 0) or 0
                icon = as_int(item.get("iconType"), 0) or 0
                row = existing.get(key)
                town = row["town"] if row is not None and row["town"] else nearest_name(static or [], *key)
                if row is None:
                    await tx.execute(
                        "INSERT INTO war_towns (map_name, x, y, hex, town, icon_type, team, flags, changed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (map_name, key[0], key[1], hex_name, town, icon, team, flags, now),
                    )
                    touched = True
                    continue
                changed_at = row["changed_at"]
                if row["team"] != team:
                    changed_at = now
                    if baselined:
                        victory = bool((flags | row["flags"]) & FLAG_VICTORY)
                        changes.append(TownChange(map_name, hex_name, town, row["team"], team, victory))
                        await tx.execute(
                            "INSERT INTO war_events (war_id, map_name, hex, town, old_team, new_team, victory, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (war_id, map_name, hex_name, town, row["team"], team, int(victory), now),
                        )
                if (row["hex"], row["town"], row["icon_type"], row["team"], row["flags"], row["changed_at"]) != (
                    hex_name,
                    town,
                    icon,
                    team,
                    flags,
                    changed_at,
                ):
                    await tx.execute(
                        "UPDATE war_towns SET hex = ?, town = ?, icon_type = ?, team = ?, flags = ?, changed_at = ? "
                        "WHERE map_name = ? AND x = ? AND y = ?",
                        (hex_name, town, icon, team, flags, changed_at, map_name, key[0], key[1]),
                    )
                    touched = True
            for key in existing.keys() - current.keys():
                await tx.execute("DELETE FROM war_towns WHERE map_name = ? AND x = ? AND y = ?", (map_name, key[0], key[1]))
                touched = True
        self._baselined.add(map_name)
        return changes, touched

    async def _drop_missing_maps(self, maps: list[str]) -> bool:
        marks = ", ".join("?" for _ in maps)
        removed = await self.bot.db.execute(f"DELETE FROM war_towns WHERE map_name NOT IN ({marks})", tuple(maps))
        return removed > 0

    async def _fill_missing_names(self) -> bool:
        rows = await self.bot.db.fetchall("SELECT map_name, x, y FROM war_towns WHERE town = ''")
        by_map: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_map[row["map_name"]].append(row)
        updates = []
        for map_name, missing in by_map.items():
            static = await self._static_items(map_name)
            if not static:
                continue
            for row in missing:
                name = nearest_name(static, row["x"], row["y"])
                if name:
                    updates.append((name, map_name, row["x"], row["y"]))
        if not updates:
            return False
        async with self.bot.db.transaction() as tx:
            await tx.executemany("UPDATE war_towns SET town = ? WHERE map_name = ? AND x = ? AND y = ?", updates)
        return True

    async def _refresh_reports(self, state: dict, maps: list[str], now: int, *, force: bool) -> bool:
        reports = state.get("reports") or {}
        if not maps:
            return False
        if not force and now - (as_int(reports.get("updated_at"), 0) or 0) < REPORT_INTERVAL:
            return False
        stored = reports.get("by_map")
        by_map = {name: values for name, values in stored.items() if isinstance(values, dict)} if isinstance(stored, dict) else {}
        fetched = 0
        for map_name in maps:
            try:
                result = await self.client.get(report_path(map_name))
            except WarApiError as error:
                log.warning("Could not fetch the war report for %s: %s", map_name, error)
                continue
            if isinstance(result.data, dict):
                by_map[map_name] = {key: as_int(result.data.get(source), 0) or 0 for key, source in REPORT_TOTALS.items()}
                fetched += 1
        if not fetched:
            return False
        known = {name: by_map[name] for name in maps if name in by_map}
        totals = {key: sum(as_int(report.get(key), 0) or 0 for report in known.values()) for key in REPORT_TOTALS}
        changed = any(totals[key] != reports.get(key) for key in REPORT_TOTALS)
        state["reports"] = {**totals, "updated_at": now, "by_map": known}
        return changed

    def guilds(self) -> list[discord.Guild]:
        return list(self.bot.guilds)

    async def _broadcast(self, embed: discord.Embed, view: discord.ui.View | None, what: str) -> None:
        for guild in self.guilds():
            try:
                channel = await notice_channel(self.bot, guild)
                if channel is None:
                    log.info("No channel for the %s in guild %s", what, guild.id)
                    continue
                kwargs: dict[str, Any] = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
                if view is not None:
                    kwargs["view"] = view
                await channel.send(**kwargs)
            except discord.HTTPException:
                log.warning("Could not post the %s in guild %s", what, guild.id, exc_info=True)
            except Exception:
                log.exception("War %s failed in guild %s", what, guild.id)

    async def _send_alerts(self, changes: list[TownChange], state: dict) -> None:
        for guild in self.guilds():
            try:
                await self._alert_guild(guild, changes, state)
            except discord.HTTPException:
                log.warning("Could not post town changes in guild %s", guild.id, exc_info=True)
            except Exception:
                log.exception("Town change alert failed in guild %s", guild.id)

    async def _alert_guild(self, guild: discord.Guild, changes: list[TownChange], state: dict) -> None:
        mode = await alert_mode(self.bot, guild.id)
        if mode == ALERT_OFF:
            return
        our_team = await guild_team(self.bot, guild.id)
        if mode == ALERT_OURS:
            if our_team is None:
                return
            changes = [change for change in changes if our_team in (change.old_team, change.new_team)]
        if not changes:
            return
        channel = await notice_channel(self.bot, guild)
        if channel is None:
            log.info("No channel for town change alerts in guild %s", guild.id)
            return
        await channel.send(embed=capture_embed(changes, our_team, state), allowed_mentions=discord.AllowedMentions.none())

    async def _refresh_boards(self) -> None:
        rows = await self.bot.db.fetchall("SELECT guild_id FROM boards WHERE kind = ?", (KIND,))
        for row in rows:
            self.bot.boards.request_refresh(row["guild_id"], KIND)


def feature_kind(spec: WarTable) -> str | None:
    if spec.kind is None:
        return None
    module = sys.modules.get(spec.module or "")
    return str(getattr(module, "KIND", spec.kind)) if module is not None else spec.kind


def counts_text(counts: dict[str, int]) -> str:
    parts = []
    for spec in WAR_TABLES:
        count = counts.get(spec.table, 0)
        parts.append(f"{count} {spec.noun if count == 1 else spec.plural_noun}")
    return ", ".join(parts)


def archive_label(state: dict | None) -> int | None:
    number = as_int((state or {}).get("war_number"), None)
    if not number:
        return None
    if team_of((state or {}).get("winner")) != NEUTRAL:
        return number
    return number - 1 if number > 1 else None


async def table_columns(tx: Transaction, table: str) -> set[str]:
    rows = await tx.fetchall(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}


async def archive_counts(bot: FoxBot, guild_id: int) -> dict[str, int]:
    counts = {}
    for spec in WAR_TABLES:
        counts[spec.table] = int(await bot.db.fetchval(f"SELECT COUNT(*) FROM {spec.table} WHERE guild_id = ?", (guild_id,), 0))
    return counts


async def last_archive(bot: FoxBot, guild_id: int, war_number: int | None) -> dict | None:
    if war_number is None:
        return None
    return await bot.db.fetchone(
        "SELECT * FROM war_archives WHERE guild_id = ? AND war_number = ? ORDER BY archived_at DESC, id DESC LIMIT 1",
        (guild_id, war_number),
    )


def can_backup(bot: FoxBot) -> bool:
    return bot.db.path != ":memory:"


def backup_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


async def backup_database(bot: FoxBot, guild_id: int, war_number: int | None) -> Path | None:
    if not can_backup(bot):
        return None
    directory = bot.backups.directory
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"war-{war_number or 'unknown'}-{guild_id}-{backup_stamp()}"
    target = directory / f"{stem}.db"
    copy_number = 1
    while target.exists():
        copy_number += 1
        target = directory / f"{stem}-{copy_number}.db"
    temporary = target.with_suffix(".tmp")
    if temporary.exists():
        temporary.unlink()
    async with bot.db.transaction():
        async with aiosqlite.connect(temporary) as destination:
            await bot.db.conn.backup(destination)
    temporary.replace(target)
    log.info("Database backed up to %s before archiving war %s for guild %s", target, war_number, guild_id)
    return target


async def archive_guild(bot: FoxBot, guild_id: int, war_number: int | None, user_id: int) -> ArchiveResult:
    backup = await backup_database(bot, guild_id, war_number)
    counts: dict[str, int] = {}
    messages: list[tuple[int, int]] = []
    async with bot.db.transaction() as tx:
        for table in DELETE_ORDER:
            spec = TABLES_BY_NAME[table]
            columns = await table_columns(tx, table)
            if "guild_id" not in columns:
                counts[table] = 0
                continue
            owned = f"SELECT id FROM {table} WHERE guild_id = ?"
            kind = feature_kind(spec)
            if kind is not None and "id" in columns:
                rows = await tx.fetchall(
                    f"SELECT channel_id, message_id FROM alert_messages WHERE kind = ? AND entry_id IN ({owned})",
                    (kind, guild_id),
                )
                messages.extend((row["channel_id"], row["message_id"]) for row in rows)
                for child in ENTRY_TABLES:
                    cursor = await tx.execute(f"DELETE FROM {child} WHERE kind = ? AND entry_id IN ({owned})", (kind, guild_id))
                    await cursor.close()
            pair = MESSAGE_COLUMNS.get(table)
            if pair is not None and set(pair) <= columns:
                rows = await tx.fetchall(
                    f"SELECT {pair[0]} AS channel_id, {pair[1]} AS message_id FROM {table} "
                    f"WHERE guild_id = ? AND {pair[0]} IS NOT NULL AND {pair[1]} IS NOT NULL",
                    (guild_id,),
                )
                messages.extend((row["channel_id"], row["message_id"]) for row in rows)
            for sql in CHILD_ROWS.get(table, ()):
                cursor = await tx.execute(sql, (guild_id,))
                await cursor.close()
            cursor = await tx.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))
            counts[table] = max(0, cursor.rowcount)
            await cursor.close()
        summary = {
            "war_number": war_number,
            "backup": backup.name if backup is not None else None,
            "deleted": counts,
        }
        cursor = await tx.execute(
            "INSERT INTO war_archives (guild_id, war_number, archived_by, archived_at, summary) VALUES (?, ?, ?, ?, ?)",
            (guild_id, war_number, user_id, timeutil.now(), json.dumps(summary, sort_keys=True)),
        )
        await cursor.close()
    return ArchiveResult(war_number, backup, counts, list(dict.fromkeys(messages)))


async def delete_messages(bot: FoxBot, messages: list[tuple[int, int]]) -> int:
    deleted = 0
    for channel_id, message_id in messages:
        try:
            await bot.get_partial_messageable(channel_id).get_partial_message(message_id).delete()
        except discord.HTTPException:
            continue
        deleted += 1
    return deleted


def refresh_all_boards(bot: FoxBot, guild_id: int) -> None:
    for kind in list(bot.boards.providers):
        bot.boards.request_refresh(guild_id, kind)


def archive_prompt(bot: FoxBot, war_number: int | None, counts: dict[str, int], previous: dict | None) -> str:
    target = f"war {war_number}" if war_number else "the previous war"
    lines = [f"**Archive {target} for this server?**"]
    if can_backup(bot):
        lines.append("A backup copy of the whole database is saved on the bot's host first.")
    else:
        lines.append("No backup can be made because the database only lives in memory.")
    lines.append(f"Then these are deleted: {counts_text(counts)}.")
    lines.append(f"Their reminders and cards are removed too. {KEPT_TEXT}")
    if previous is not None:
        lines.append(f"This server already archived war {war_number} {timeutil.relative(previous['archived_at'])}.")
    lines.append("This cannot be undone from Discord.")
    return "\n".join(lines)


def archive_done_text(result: ArchiveResult) -> str:
    target = f"War {result.war_number}" if result.war_number else "The previous war"
    lines = [f"{target} archived for this server."]
    if result.backup is not None:
        lines.append(f"Backup saved as `{result.backup.name}` on the bot's host.")
    else:
        lines.append("No backup was made because the database only lives in memory.")
    lines.append(f"Deleted: {counts_text(result.counts)}.")
    lines.append(f"{KEPT_TEXT} The boards update in a moment.")
    return "\n".join(lines)


def notice_is_outdated(state: dict | None, war_number: int | None) -> bool:
    latest = as_int((state or {}).get("previous_war_number"), None)
    return bool(war_number and latest and war_number < latest)


async def start_archive(bot: FoxBot, interaction: discord.Interaction, war_number: int | None, *, from_notice: bool = False) -> None:
    if not await check_admin(interaction):
        return
    if from_notice and notice_is_outdated(await load_state(bot), war_number):
        await deny(
            interaction,
            f"This button is on an older new war notice, so it would file this server's current data under war {war_number}. "
            "Use `/war archive` instead.",
        )
        return
    guild_id = interaction.guild_id
    counts = await archive_counts(bot, guild_id)
    previous = await last_archive(bot, guild_id, war_number)

    async def confirmed(confirm_interaction: discord.Interaction) -> None:
        if not await check_admin(confirm_interaction):
            return
        lock = _archive_locks[guild_id]
        if lock.locked():
            await edit(confirm_interaction, content="An archive is already running for this server.", view=None)
            return
        async with lock:
            await confirm_interaction.response.defer()
            try:
                result = await archive_guild(bot, guild_id, war_number, confirm_interaction.user.id)
            except (OSError, aiosqlite.Error):
                log.exception("Archiving war %s failed for guild %s", war_number, guild_id)
                await edit(
                    confirm_interaction,
                    content="The backup or the cleanup failed, so nothing was deleted. The error was logged for the bot host.",
                    view=None,
                )
                return
            await edit(confirm_interaction, content=archive_done_text(result), view=None)
            refresh_all_boards(bot, guild_id)
            await delete_messages(bot, result.messages)

    await reply(
        interaction,
        archive_prompt(bot, war_number, counts, previous),
        view=ConfirmView(owner_id=interaction.user.id, on_confirm=confirmed, confirm_label="Archive"),
    )


async def member_check(interaction: discord.Interaction) -> bool:
    if await interaction_member(interaction) is None:
        await deny(interaction, "This only works inside the server.")
        return False
    return True


async def captures_embeds(bot: FoxBot) -> list[discord.Embed]:
    state = await load_state(bot) or {}
    rows = await recent_events(bot, state.get("war_id"), CAPTURES_LIMIT)
    return paginate_embeds(
        f"Town Changes - {war_title(state)}",
        [event_line(row) for row in rows],
        colour=Colour.INFO,
        empty="No town has changed hands since the bot started tracking this war.",
        per_page_chars=3000,
        footer=f"{len(rows)} change(s), newest first",
    )


async def show_captures(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await member_check(interaction):
        return
    await EmbedPaginator(await captures_embeds(bot), owner_id=interaction.user.id).start(interaction)


def town_line(row: dict) -> str:
    details = [TOWN_ICONS.get(row["icon_type"], "Base")]
    if row["flags"] & FLAG_VICTORY:
        details.append("victory town")
    if row["flags"] & FLAG_SCORCHED:
        details.append("scorched")
    return clip(f"**{md(town_label(row['town']))}** - {team_name(row['team'])} ({', '.join(details)})", LINE_LIMIT)


def hex_embed(map_name: str, rows: list[dict]) -> discord.Embed:
    hex_name = rows[0]["hex"] if rows else hex_display_name(map_name)
    if not rows:
        return discord.Embed(title=f"{hex_name} - Towns", description="No town data for this hex yet.", colour=Colour.MUTED)
    ordered = sorted(rows, key=lambda row: (not row["flags"] & FLAG_VICTORY, town_label(row["town"]).lower()))
    description = fit_lines([town_line(row) for row in ordered], EMBED_DESCRIPTION_LIMIT - 200, "")
    embed = discord.Embed(title=f"{hex_name} - Towns", description=description, colour=Colour.INFO)
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["team"]] += 1
    embed.set_footer(text=f"{team_counts_text(counts)} | Data from the official War API")
    return embed


async def hex_embed_for(bot: FoxBot, map_name: str) -> discord.Embed:
    return hex_embed(map_name, await town_rows(bot, map_name))


async def show_hex_picker(bot: FoxBot, interaction: discord.Interaction) -> None:
    if not await member_check(interaction):
        return
    summaries = await hex_summaries(bot)
    if not summaries:
        await reply(interaction, "No town data yet. The bot checks the War API every 5 minutes.")
        return
    options = [PickOption(map_name, hex_name, team_counts_text(counts)) for map_name, hex_name, counts in summaries]
    view: PickerView | None = None

    async def picked(pick_interaction: discord.Interaction, value: str) -> None:
        if not await member_check(pick_interaction):
            return
        await edit(pick_interaction, content=None, embed=await hex_embed_for(bot, value), view=view)

    view = PickerView(options, picked, owner_id=interaction.user.id, placeholder="Pick a hex...")
    await reply(interaction, f"Pick one of {len(options)} hexes to see who holds its towns.", view=view)


async def resolve_map(bot: FoxBot, text: str) -> str | None:
    found = bot.locations.find_hex(text)
    if found is not None:
        return found.key
    key = normalise(text)
    rows = await bot.db.fetchall("SELECT DISTINCT map_name, hex FROM war_towns")
    for row in rows:
        if key and key in (normalise(row["hex"]), normalise(row["map_name"])):
            return row["map_name"]
    return None


class WarBoard:
    kind = KIND
    title = "War"

    def __init__(self, bot: FoxBot):
        self.bot = bot

    async def render(self, guild: discord.Guild) -> BoardRender:
        return BoardRender(
            embeds=[await build_status_embed(self.bot)],
            buttons=[
                BoardButton("captures", "Recent Captures", discord.ButtonStyle.primary),
                BoardButton("hexes", "Hex Status"),
            ],
        )

    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        if action == "captures":
            await show_captures(self.bot, interaction)
        elif action == "hexes":
            await show_hex_picker(self.bot, interaction)
        else:
            await deny(interaction, "That button is no longer supported.")

    async def on_pick(self, interaction: discord.Interaction, value: str) -> None:
        await deny(interaction, "This board has no selectable entries.")


class War(commands.Cog, name=COG_NAME):
    group = app_commands.Group(name="war", description="The current Foxhole war from the official War API.", guild_only=True)

    def __init__(self, bot: FoxBot):
        self.bot = bot
        self.service = WarService(bot)

    async def cog_load(self) -> None:
        if self.bot.is_ready():
            self.service.start()

    async def cog_unload(self) -> None:
        await self.service.stop()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.service.start()

    @group.command(name="status", description="War number, day, victory towns, casualties and recent captures (only you see it).")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await member_check(interaction):
            return
        await reply(interaction, embed=await build_status_embed(self.bot))

    @group.command(name="hex", description="Who holds each town in a hex (only you see it).")
    @app_commands.describe(hex="The hex (start typing to search)")
    async def hex_command(self, interaction: discord.Interaction, hex: app_commands.Range[str, 1, 60]) -> None:
        if not await member_check(interaction):
            return
        map_name = await resolve_map(self.bot, hex)
        if map_name is None:
            await deny(interaction, "Unknown hex. Pick one from the suggestions.")
            return
        await reply(interaction, embed=await hex_embed_for(self.bot, map_name))

    @hex_command.autocomplete("hex")
    async def hex_autocomplete(self, interaction: discord.Interaction, current: str):
        return self.bot.locations.hex_choices(current)

    @group.command(name="captures", description="Every town that changed hands this war, newest first (only you see it).")
    async def captures(self, interaction: discord.Interaction) -> None:
        await show_captures(self.bot, interaction)

    @group.command(name="board", description="Admin: post the live war board in this channel (moves it if it exists).")
    async def board(self, interaction: discord.Interaction) -> None:
        if not await check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.boards.post(interaction.guild, KIND, interaction.channel)
        except discord.HTTPException as error:
            await reply(interaction, f"Could not post the board here: {error.text or error}")
            return
        mode = await alert_mode(self.bot, interaction.guild_id)
        hint = " Town change alerts are off; turn them on in `/settings` under War." if mode == ALERT_OFF else ""
        await reply(interaction, f"War board posted.{hint}")

    @group.command(name="archive", description="Admin: back up, then delete this server's data from the previous war.")
    async def archive(self, interaction: discord.Interaction) -> None:
        await start_archive(self.bot, interaction, archive_label(await load_state(self.bot)))


async def setup(bot: FoxBot) -> None:
    bot.boards.register(WarBoard(bot))
    bot.add_dynamic_items(ArchiveWarItem)
    await bot.add_cog(War(bot))
