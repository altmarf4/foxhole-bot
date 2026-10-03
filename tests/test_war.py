import ast
import asyncio
import copy
import hashlib
import io
import json
import re
import time
import tokenize
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import aiosqlite
import discord
import pytest

from fakes import config, find_button, make_bot, make_channel, make_guild, make_interaction, make_member, next_id
from foxbot.bot import FoxBot
from foxbot.core import settings as keys
from foxbot.core.boards import build_board_view
from foxbot.core.locations import SEED_PATH
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_TOTAL_LIMIT
from foxbot.core.ui import ConfirmView, EmbedPaginator, PickerView
from foxbot.core.warapi import (
    MAPS_PATH,
    WAR_PATH,
    ApiResponse,
    WarApiClient,
    WarApiError,
    dynamic_path,
    report_path,
    static_path,
)
from foxbot.db import Database

BASE = "http://war.test/api"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
DEAD = "DeadLandsHex"
FINGERS = "TheFingersHex"
START_MS = int((time.time() - 3.5 * 86400) * 1000)


class FakeApi:
    def __init__(self):
        self.routes: dict[str, object] = {}
        self.fail: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def etag(data) -> str:
        return '"' + hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest() + '"'

    async def fetch(self, url: str, headers: dict) -> ApiResponse:
        assert url.startswith(BASE)
        path = url[len(BASE):]
        self.calls.append((path, dict(headers)))
        failure = self.fail.get(path)
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, int):
            return ApiResponse(failure)
        if path not in self.routes:
            return ApiResponse(404)
        data = self.routes[path]
        tag = self.etag(data)
        if headers.get("If-None-Match") == tag:
            return ApiResponse(304, None, tag)
        return ApiResponse(200, copy.deepcopy(data), tag)

    def count(self, path: str) -> int:
        return sum(1 for called, _ in self.calls if called == path)


def war_payload(war_id="war-a", number=127, winner="NONE", **extra):
    payload = {
        "warId": war_id,
        "warNumber": number,
        "winner": winner,
        "conquestStartTime": START_MS,
        "conquestEndTime": None,
        "resistanceStartTime": None,
        "scheduledConquestEndTime": None,
        "requiredVictoryTowns": 32,
        "shortRequiredVictoryTowns": 30,
    }
    payload.update(extra)
    return payload


def town(team, x, y, icon=56, flags=0):
    return {"teamId": team, "iconType": icon, "x": x, "y": y, "flags": flags}


def seed_api(api: FakeApi) -> None:
    api.routes[WAR_PATH] = war_payload()
    api.routes[MAPS_PATH] = [DEAD, FINGERS]
    api.routes[static_path(DEAD)] = {
        "mapTextItems": [
            {"text": "Abandoned Ward", "x": 0.43, "y": 0.61, "mapMarkerType": "Major"},
            {"text": "The Pits", "x": 0.6, "y": 0.4, "mapMarkerType": "Major"},
            {"text": "Little Hut", "x": 0.431, "y": 0.6, "mapMarkerType": "Minor"},
        ]
    }
    api.routes[static_path(FINGERS)] = {
        "mapTextItems": [{"text": "Titancall", "x": 0.5, "y": 0.5, "mapMarkerType": "Minor"}]
    }
    set_dead(api, ward="WARDENS", pits="COLONIALS")
    set_fingers(api, "COLONIALS")
    api.routes[report_path(DEAD)] = {"totalEnlistments": 100, "colonialCasualties": 50, "wardenCasualties": 70, "dayOfWar": 30, "version": 1}
    api.routes[report_path(FINGERS)] = {"totalEnlistments": 20, "colonialCasualties": 5, "wardenCasualties": 7, "dayOfWar": 30, "version": 1}


def set_dead(api: FakeApi, *, ward: str, pits: str, ward_icon: int = 56, ward_x: float = 0.43001) -> None:
    api.routes[dynamic_path(DEAD)] = {
        "regionId": 3,
        "scorchedVictoryTowns": 0,
        "mapItems": [
            town(ward, ward_x, 0.6, icon=ward_icon, flags=0x01),
            town(pits, 0.61, 0.41, icon=58, flags=0x01 | 0x20),
            {"teamId": "WARDENS", "iconType": 12, "x": 0.2, "y": 0.2, "flags": 0},
        ],
        "lastUpdated": 1,
        "version": 1,
    }


def set_fingers(api: FakeApi, team: str, flags: int = 0) -> None:
    api.routes[dynamic_path(FINGERS)] = {
        "regionId": 4,
        "scorchedVictoryTowns": 0,
        "mapItems": [town(team, 0.5, 0.52, icon=27, flags=flags)],
        "lastUpdated": 1,
        "version": 1,
    }


def all_text(embed: discord.Embed) -> str:
    parts = [embed.title, embed.description, embed.footer.text if embed.footer else None]
    parts += [f"{field.name} {field.value}" for field in embed.fields]
    return "\n".join(filter(None, parts))


def partial_recorder(bot):
    deleted = []

    def messageable(channel_id):
        target = MagicMock()

        def message(message_id):
            item = MagicMock()

            async def delete():
                deleted.append((channel_id, message_id))

            item.delete = AsyncMock(side_effect=delete)
            return item

        target.get_partial_message.side_effect = message
        return target

    bot.get_partial_messageable = MagicMock(side_effect=messageable)
    return deleted


async def build_world(extensions):
    bot = await make_bot(extensions)
    war = bot.extensions["foxbot.features.war"]
    guild = make_guild(owner_id=1)
    other = make_guild(owner_id=2)
    guilds = {guild.id: guild, other.id: other}
    bot.get_guild = MagicMock(side_effect=lambda gid: guilds.get(gid))
    bot._connection._guilds.update(guilds)
    admin = make_member(guild, name="Admin", administrator=True)
    plain = make_member(guild, name="Plain")
    other_admin = make_member(other, name="Other Admin", administrator=True)
    war_channel = make_channel(guild, "war")
    alert_channel = make_channel(guild, "alerts")
    board_channel = make_channel(guild, "board")
    other_channel = make_channel(other, "other-alerts")
    cog = bot.get_cog("War")
    api = FakeApi()
    seed_api(api)
    cog.service.client = WarApiClient(BASE, fetcher=api.fetch, min_interval=0)
    return SimpleNamespace(
        bot=bot,
        war=war,
        guild=guild,
        other=other,
        admin=admin,
        plain=plain,
        other_admin=other_admin,
        war_channel=war_channel,
        alert_channel=alert_channel,
        board_channel=board_channel,
        other_channel=other_channel,
        cog=cog,
        service=cog.service,
        api=api,
    )


@pytest.fixture
async def world():
    w = await build_world(["foxbot.features.war"])
    yield w
    await w.cog.cog_unload()
    await w.bot.db.close()


async def set_mode(w, mode, guild=None, channel=None):
    guild = guild or w.guild
    await w.bot.settings.set(guild.id, keys.WAR_ALERT_MODE, mode)
    if channel is not None:
        await w.bot.settings.set(guild.id, keys.WAR_ALERT_CHANNEL, channel.id)


async def towns(w):
    rows = await w.bot.db.fetchall("SELECT * FROM war_towns ORDER BY hex, town")
    return [(row["hex"], row["town"], row["team"]) for row in rows]


async def events(w):
    return await w.bot.db.fetchall("SELECT * FROM war_events ORDER BY id")


def sent_everywhere(w):
    return w.war_channel.sent + w.alert_channel.sent + w.board_channel.sent + w.other_channel.sent


async def test_client_uses_etags_and_keeps_cache_on_errors():
    api = FakeApi()
    api.routes["/thing"] = {"a": 1}
    client = WarApiClient(BASE, fetcher=api.fetch, min_interval=0)
    first = await client.get("/thing")
    assert first.changed and first.data == {"a": 1}
    second = await client.get("/thing")
    assert not second.changed and second.data == {"a": 1}
    assert "If-None-Match" not in api.calls[0][1]
    assert api.calls[1][1]["If-None-Match"] == FakeApi.etag({"a": 1})
    assert api.calls[0][1]["User-Agent"].startswith("FoxholeLogisticsBot")
    assert client.not_modified == 1
    api.routes["/thing"] = {"a": 2}
    third = await client.get("/thing")
    assert third.changed and third.data == {"a": 2}
    for failure in (503, 429, aiohttp.ClientConnectionError("down"), asyncio.TimeoutError(), ValueError("bad json")):
        api.fail["/thing"] = failure
        with pytest.raises(WarApiError):
            await client.get("/thing")
        assert client.cached("/thing") == {"a": 2}
        assert client.last_error
    api.fail.clear()
    assert (await client.get("/thing")).changed is False
    assert client.last_error is None
    client.forget()
    assert client.cached("/thing") is None


async def test_client_rejects_304_without_cache_and_empty_bodies():
    async def not_modified(url, headers):
        return ApiResponse(304, None, '"x"')

    client = WarApiClient(BASE, fetcher=not_modified, min_interval=0)
    with pytest.raises(WarApiError):
        await client.get("/x")

    async def empty(url, headers):
        return ApiResponse(200, None, None)

    client = WarApiClient(BASE, fetcher=empty, min_interval=0)
    with pytest.raises(WarApiError):
        await client.get("/x")
    await client.close()


class FakeHttpResponse:
    def __init__(self, status, body=None, etag=None):
        self.status = status
        self.headers = {"ETag": etag} if etag else {}
        self.body = body

    async def json(self, content_type=None):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def get(self, url, headers=None):
        self.requests.append((url, dict(headers or {})))
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


async def test_http_fetch_reads_status_etag_and_json_without_network():
    session = FakeSession(
        [
            FakeHttpResponse(200, {"warId": "a"}, '"v1"'),
            FakeHttpResponse(304, None, '"v1"'),
            FakeHttpResponse(500),
            FakeHttpResponse(200, ValueError("not json")),
        ]
    )
    client = WarApiClient(BASE, min_interval=0)
    client._session = session
    first = await client.get(WAR_PATH)
    assert first.changed and first.data == {"warId": "a"}
    assert session.requests[0][0] == BASE + WAR_PATH
    second = await client.get(WAR_PATH)
    assert not second.changed and second.data == {"warId": "a"}
    assert session.requests[1][1]["If-None-Match"] == '"v1"'
    with pytest.raises(WarApiError):
        await client.get(WAR_PATH)
    with pytest.raises(WarApiError):
        await client.get(WAR_PATH)
    await client.close()
    assert session.closed and client._session is None
    real = WarApiClient(BASE)
    created = real._ensure_session()
    assert created.timeout.total == 20 and created.timeout.connect == 10
    assert real._ensure_session() is created
    await real.close()
    assert created.closed


async def test_client_paces_requests():
    api = FakeApi()
    api.routes["/a"] = [1]
    client = WarApiClient(BASE, fetcher=api.fetch, min_interval=0.05)
    started = time.monotonic()
    for _ in range(3):
        await client.get("/a")
    assert time.monotonic() - started >= 0.09


async def test_first_poll_is_a_silent_baseline(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    outcome = await w.service.poll_once()
    assert outcome.ok and outcome.first_run and not outcome.new_war and not outcome.war_over
    assert outcome.changes == []
    assert await towns(w) == [
        ("Deadlands", "Abandoned Ward", "WARDENS"),
        ("Deadlands", "The Pits", "COLONIALS"),
        ("The Fingers", "Titancall", "COLONIALS"),
    ]
    assert await events(w) == []
    assert sent_everywhere(w) == []
    state = await w.war.load_state(w.bot)
    assert state["war_id"] == "war-a" and state["war_number"] == 127 and state["required_victory_towns"] == 32
    assert state["reports"]["enlistments"] == 120
    assert state["reports"]["wardens"] == 77 and state["reports"]["colonials"] == 55
    assert state["polled_at"]
    row = await w.bot.db.fetchone("SELECT * FROM war_towns WHERE town = 'Abandoned Ward'")
    assert (row["x"], row["y"], row["icon_type"], row["flags"]) == (0.43, 0.6, 56, 1)


async def test_capture_events_are_recorded_and_alerted_in_one_message(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    await w.bot.db.execute(
        "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (?, 'war', ?, ?)",
        (w.guild.id, w.board_channel.id, next_id()),
    )
    await w.service.poll_once()
    w.bot.boards.request_refresh.reset_mock()
    set_dead(w.api, ward="COLONIALS", pits="COLONIALS")
    set_fingers(w.api, "NONE")
    outcome = await w.service.poll_once()
    assert outcome.ok and not outcome.first_run
    assert [(c.town, c.old_team, c.new_team, c.victory) for c in outcome.changes] == [
        ("Abandoned Ward", "WARDENS", "COLONIALS", True),
        ("Titancall", "COLONIALS", "NONE", False),
    ]
    rows = await events(w)
    assert [(r["war_id"], r["hex"], r["town"], r["old_team"], r["new_team"], r["victory"]) for r in rows] == [
        ("war-a", "Deadlands", "Abandoned Ward", "WARDENS", "COLONIALS", 1),
        ("war-a", "The Fingers", "Titancall", "COLONIALS", "NONE", 0),
    ]
    assert len(w.war_channel.sent) == 1
    payload = w.war_channel.sent[0]
    embed = payload["embed"]
    assert payload.get("content") is None and "view" not in payload
    assert payload["allowed_mentions"].roles is False and payload["allowed_mentions"].everyone is False
    assert embed.title == "2 Towns Changed Hands"
    assert "**Deadlands** - Abandoned Ward: Wardens -> Colonials (victory town)" in embed.description
    assert "**The Fingers** - Titancall: Colonials -> Neutral" in embed.description
    assert "we lost" not in embed.description
    assert "War 127" in embed.footer.text
    assert w.other_channel.sent == [] and w.alert_channel.sent == []
    w.bot.boards.request_refresh.assert_called_once_with(w.guild.id, "war")
    changed_at = await w.bot.db.fetchval("SELECT changed_at FROM war_towns WHERE town = 'Abandoned Ward'")
    assert changed_at == rows[0]["created_at"]


async def test_unchanged_maps_are_skipped_with_etags(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    await w.service.poll_once()
    w.bot.boards.request_refresh.reset_mock()
    before = len(w.api.calls)
    outcome = await w.service.poll_once()
    assert outcome.ok and not outcome.changed and outcome.changes == []
    second = w.api.calls[before:]
    assert [path for path, _ in second] == [WAR_PATH, dynamic_path(DEAD), dynamic_path(FINGERS)]
    assert all("If-None-Match" in headers for _, headers in second)
    assert w.service.client.not_modified == 3
    assert w.api.count(static_path(DEAD)) == 1
    assert sent_everywhere(w) == []
    w.bot.boards.request_refresh.assert_not_called()


async def test_ours_mode_only_reports_our_faction(world):
    w = world
    await set_mode(w, "ours", channel=w.war_channel)
    await w.service.poll_once()
    set_dead(w.api, ward="NONE", pits="NONE")
    await w.service.poll_once()
    assert w.war_channel.sent == []
    assert len(await events(w)) == 2

    await w.bot.settings.set(w.guild.id, keys.FACTION, "warden")
    set_dead(w.api, ward="WARDENS", pits="WARDENS")
    set_fingers(w.api, "NONE")
    await w.service.poll_once()
    assert len(w.war_channel.sent) == 1
    embed = w.war_channel.sent[0]["embed"]
    assert embed.description.count("\n") == 1
    assert "Abandoned Ward: Neutral -> Wardens (victory town) - we took it" in embed.description
    assert "The Pits: Neutral -> Wardens (victory town) - we took it" in embed.description
    assert "Titancall" not in embed.description
    assert embed.colour.value == w.war.Colour.SUCCESS

    await w.bot.settings.set(w.guild.id, keys.FACTION, "colonial")
    set_fingers(w.api, "COLONIALS")
    set_dead(w.api, ward="COLONIALS", pits="WARDENS")
    await w.service.poll_once()
    embed = w.war_channel.sent[1]["embed"]
    assert "Titancall: Neutral -> Colonials - we took it" in embed.description
    assert "Abandoned Ward: Wardens -> Colonials (victory town) - we took it" in embed.description
    assert embed.colour.value == w.war.Colour.SUCCESS

    await w.bot.settings.set(w.guild.id, keys.FACTION, "colonial")
    set_fingers(w.api, "WARDENS")
    await w.service.poll_once()
    embed = w.war_channel.sent[2]["embed"]
    assert embed.title == "Town Changed Hands"
    assert "we lost it" in embed.description and embed.colour.value == w.war.Colour.DANGER


async def test_alert_channel_fallback_chain(world):
    w = world
    await set_mode(w, "all")
    await w.service.poll_once()

    async def flip(team):
        set_fingers(w.api, team)
        await w.service.poll_once()

    await flip("NONE")
    assert sent_everywhere(w) == []
    await w.bot.db.execute(
        "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (?, 'war', ?, ?)",
        (w.guild.id, w.board_channel.id, next_id()),
    )
    await flip("WARDENS")
    assert len(w.board_channel.sent) == 1
    await w.bot.settings.set(w.guild.id, keys.ALERT_CHANNEL, w.alert_channel.id)
    await flip("NONE")
    assert len(w.alert_channel.sent) == 1
    await w.bot.settings.set(w.guild.id, keys.WAR_ALERT_CHANNEL, 999_999)
    await flip("COLONIALS")
    assert len(w.alert_channel.sent) == 2
    await w.bot.settings.set(w.guild.id, keys.WAR_ALERT_CHANNEL, w.war_channel.id)
    await flip("NONE")
    assert len(w.war_channel.sent) == 1
    w.war_channel.send = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
    await flip("WARDENS")
    assert len(await events(w)) == 6


async def test_restart_does_not_alert_changes_made_while_offline(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    await w.service.poll_once()
    fresh = w.war.WarService(w.bot, WarApiClient(BASE, fetcher=w.api.fetch, min_interval=0))
    set_fingers(w.api, "WARDENS")
    outcome = await fresh.poll_once()
    assert outcome.ok and not outcome.first_run and outcome.changes == []
    assert ("The Fingers", "Titancall", "WARDENS") in await towns(w)
    assert w.api.count(static_path(FINGERS)) == 1
    set_fingers(w.api, "NONE")
    outcome = await fresh.poll_once()
    assert [(c.old_team, c.new_team) for c in outcome.changes] == [("WARDENS", "NONE")]
    assert len(w.war_channel.sent) == 1


async def test_new_war_resets_towns_and_posts_one_notice_per_guild(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    await w.bot.settings.set(w.other.id, keys.ALERT_CHANNEL, w.other_channel.id)
    await w.service.poll_once()
    set_fingers(w.api, "NONE")
    await w.service.poll_once()
    assert len(await events(w)) == 1
    w.war_channel.sent.clear()

    w.api.routes[WAR_PATH] = war_payload("war-b", 128, conquestStartTime=int(time.time() * 1000))
    w.api.routes[static_path(DEAD)]["mapTextItems"][0]["text"] = "Renamed Ward"
    set_dead(w.api, ward="NONE", pits="WARDENS")
    set_fingers(w.api, "COLONIALS")
    outcome = await w.service.poll_once()
    assert outcome.ok and outcome.new_war and not outcome.first_run
    assert outcome.changes == []
    assert await towns(w) == [
        ("Deadlands", "Renamed Ward", "NONE"),
        ("Deadlands", "The Pits", "WARDENS"),
        ("The Fingers", "Titancall", "COLONIALS"),
    ]
    assert w.api.count(static_path(DEAD)) == 2
    assert len(w.war_channel.sent) == 1 and len(w.other_channel.sent) == 1
    for payload in (w.war_channel.sent[0], w.other_channel.sent[0]):
        assert payload["embed"].title == "A New War Has Started"
        assert "War **128** has started" in payload["embed"].description
        assert "war 127" in payload["embed"].description
        ids = [item.custom_id for item in payload["view"].children]
        assert ids == ["fw:archive:127"]
        assert payload["view"].timeout is None
    state = await w.war.load_state(w.bot)
    assert state["war_id"] == "war-b" and state["previous_war_number"] == 127
    assert state["reports"]["enlistments"] == 120

    set_dead(w.api, ward="WARDENS", pits="WARDENS")
    outcome = await w.service.poll_once()
    assert [(c.town, c.new_team) for c in outcome.changes] == [("Renamed Ward", "WARDENS")]
    rows = await events(w)
    assert [row["war_id"] for row in rows] == ["war-a", "war-b"]
    recent = await w.war.recent_events(w.bot, "war-b", 10)
    assert len(recent) == 1


async def test_first_run_never_announces_and_winner_is_announced_once(world):
    w = world
    await set_mode(w, "off", channel=w.war_channel)
    await w.bot.settings.set(w.other.id, keys.ALERT_CHANNEL, w.other_channel.id)
    w.api.routes[WAR_PATH] = war_payload(winner="WARDENS", conquestEndTime=int(time.time() * 1000))
    outcome = await w.service.poll_once()
    assert outcome.first_run and not outcome.war_over and not outcome.new_war
    assert sent_everywhere(w) == []
    await w.service.poll_once()
    assert sent_everywhere(w) == []

    await w.bot.db.execute("DELETE FROM meta WHERE key = 'war_state'")
    w.api.routes[WAR_PATH] = war_payload()
    await w.service.poll_once()
    w.api.routes[WAR_PATH] = war_payload(winner="COLONIALS", conquestEndTime=int(time.time() * 1000), resistanceStartTime=1)
    outcome = await w.service.poll_once()
    assert outcome.war_over and not outcome.new_war
    for channel in (w.war_channel, w.other_channel):
        assert len(channel.sent) == 1
        embed = channel.sent[0]["embed"]
        assert embed.title == "War 127 Is Over"
        assert "The **Colonials** won war **127**." in embed.description
        assert "It lasted 4 day(s)." in embed.description
    outcome = await w.service.poll_once()
    assert not outcome.war_over
    assert len(w.war_channel.sent) == 1 and len(w.other_channel.sent) == 1
    state = await w.war.load_state(w.bot)
    assert state["winner"] == "COLONIALS" and state["winner_announced"] is True


async def test_api_failures_keep_the_last_data(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    await w.service.poll_once()
    before_state = await w.war.load_state(w.bot)
    before_towns = await towns(w)
    w.api.fail[WAR_PATH] = 503
    outcome = await w.service.poll_once()
    assert not outcome.ok
    assert await w.war.load_state(w.bot) == before_state
    assert await towns(w) == before_towns
    w.api.fail = {dynamic_path(DEAD): aiohttp.ClientConnectionError("reset")}
    set_dead(w.api, ward="NONE", pits="NONE")
    set_fingers(w.api, "WARDENS")
    outcome = await w.service.poll_once()
    assert outcome.ok and outcome.failures == 1
    assert [c.town for c in outcome.changes] == ["Titancall"]
    assert ("Deadlands", "Abandoned Ward", "WARDENS") in await towns(w)
    w.api.fail = {}
    outcome = await w.service.poll_once()
    assert sorted(c.town for c in outcome.changes) == ["Abandoned Ward", "The Pits"]

    w.api.routes[WAR_PATH] = {"warNumber": 1}
    outcome = await w.service.poll_once()
    assert not outcome.ok
    w.api.routes[WAR_PATH] = war_payload()
    w.api.routes[dynamic_path(FINGERS)] = {"mapItems": "broken"}
    w.api.routes[MAPS_PATH] = [DEAD, FINGERS]
    outcome = await w.service.poll_once()
    assert outcome.ok
    assert ("The Fingers", "Titancall", "WARDENS") in await towns(w)


async def test_no_capture_events_before_the_war_starts(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    w.api.routes[WAR_PATH] = war_payload(conquestStartTime=None)
    await w.service.poll_once()
    set_fingers(w.api, "WARDENS")
    outcome = await w.service.poll_once()
    assert outcome.ok and outcome.changes == [] and outcome.changed
    assert ("The Fingers", "Titancall", "WARDENS") in await towns(w)
    embed = await w.war.build_status_embed(w.bot)
    assert "Status: **Not started yet**" in embed.description and "Day **" not in embed.description
    w.api.routes[WAR_PATH] = war_payload()
    set_fingers(w.api, "NONE")
    outcome = await w.service.poll_once()
    assert [(c.old_team, c.new_team) for c in outcome.changes] == [("WARDENS", "NONE")]
    assert len(w.war_channel.sent) == 1


async def test_data_age_is_kept_when_every_map_fails(world):
    w = world
    clock = [1_000_000]
    w.service.clock = lambda: clock[0]
    await w.service.poll_once()
    clock[0] += 300
    w.api.fail = {dynamic_path(DEAD): 500, dynamic_path(FINGERS): 500}
    outcome = await w.service.poll_once()
    assert outcome.ok and outcome.failures == 2
    assert (await w.war.load_state(w.bot))["polled_at"] == 1_000_000
    w.api.fail = {dynamic_path(DEAD): 500}
    clock[0] += 300
    await w.service.poll_once()
    assert (await w.war.load_state(w.bot))["polled_at"] == clock[0]


async def test_poll_survives_unexpected_errors(world):
    w = world
    w.service._apply_map = AsyncMock(side_effect=RuntimeError("boom"))
    outcome = await w.service.poll_once()
    assert not outcome.ok
    assert not w.service._lock.locked()


async def test_reports_are_refreshed_every_30_minutes(world):
    w = world
    clock = [1_000_000]
    w.service.clock = lambda: clock[0]
    await w.service.poll_once()
    assert w.api.count(report_path(DEAD)) == 1
    w.api.routes[report_path(DEAD)] = {"totalEnlistments": 150, "colonialCasualties": 60, "wardenCasualties": 80, "dayOfWar": 31}
    clock[0] += 600
    await w.service.poll_once()
    assert w.api.count(report_path(DEAD)) == 1
    assert (await w.war.load_state(w.bot))["reports"]["enlistments"] == 120
    w.bot.boards.request_refresh.reset_mock()
    await w.bot.db.execute(
        "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (?, 'war', ?, ?)",
        (w.guild.id, w.board_channel.id, next_id()),
    )
    clock[0] += 31 * 60
    outcome = await w.service.poll_once()
    assert outcome.changed
    reports = (await w.war.load_state(w.bot))["reports"]
    assert (reports["enlistments"], reports["wardens"], reports["colonials"]) == (170, 87, 65)
    assert reports["updated_at"] == clock[0]
    w.bot.boards.request_refresh.assert_called_once_with(w.guild.id, "war")

    w.api.fail[report_path(FINGERS)] = 500
    w.api.routes[report_path(DEAD)] = {"totalEnlistments": 151, "colonialCasualties": 60, "wardenCasualties": 80}
    clock[0] += 31 * 60
    await w.service.poll_once()
    assert (await w.war.load_state(w.bot))["reports"]["enlistments"] == 171


async def test_missing_town_names_are_filled_later(world):
    w = world
    w.api.fail[static_path(DEAD)] = 500
    await w.service.poll_once()
    assert ("Deadlands", "", "WARDENS") in await towns(w)
    assert w.api.count(static_path(DEAD)) == 1
    embed = await w.war.hex_embed_for(w.bot, DEAD)
    assert "Unknown town" in embed.description
    w.api.fail.clear()
    await w.service.poll_once()
    assert ("Deadlands", "Abandoned Ward", "WARDENS") in await towns(w)
    assert w.api.count(static_path(DEAD)) == 2
    await w.service.poll_once()
    assert w.api.count(static_path(DEAD)) == 2


async def test_coordinates_are_rounded_and_upgrades_are_not_new_towns(world):
    w = world
    await set_mode(w, "all", channel=w.war_channel)
    await w.service.poll_once()
    set_dead(w.api, ward="WARDENS", pits="COLONIALS", ward_icon=57, ward_x=0.430004)
    outcome = await w.service.poll_once()
    assert outcome.changes == [] and outcome.changed
    rows = await w.bot.db.fetchall("SELECT * FROM war_towns WHERE map_name = ?", (DEAD,))
    assert len(rows) == 2
    assert {row["icon_type"] for row in rows} == {57, 58}
    w.api.routes[dynamic_path(DEAD)]["mapItems"] = w.api.routes[dynamic_path(DEAD)]["mapItems"][1:]
    await w.service.poll_once()
    assert await towns(w) == [("Deadlands", "The Pits", "COLONIALS"), ("The Fingers", "Titancall", "COLONIALS")]
    assert w.war_channel.sent == []


async def test_maps_list_is_cached_and_dropped_maps_are_removed(world):
    w = world
    clock = [1_000_000]
    w.service.clock = lambda: clock[0]
    await w.service.poll_once()
    await w.service.poll_once()
    assert w.api.count(MAPS_PATH) == 1
    w.api.routes[MAPS_PATH] = [DEAD]
    clock[0] += 7 * 3600
    await w.service.poll_once()
    assert w.api.count(MAPS_PATH) == 2
    assert await towns(w) == [("Deadlands", "Abandoned Ward", "WARDENS"), ("Deadlands", "The Pits", "COLONIALS")]


async def test_nearest_name_prefers_major_then_minor():
    from foxbot.features import war

    items = [
        {"text": "Big Town", "x": 0.9, "y": 0.9, "mapMarkerType": "Major"},
        {"text": "Hamlet", "x": 0.1, "y": 0.1, "mapMarkerType": "Minor"},
        {"text": "", "x": 0.1, "y": 0.1, "mapMarkerType": "Major"},
    ]
    assert war.nearest_name(items, 0.1, 0.1) == "Big Town"
    assert war.nearest_name(items[1:], 0.9, 0.9) == "Hamlet"
    assert war.nearest_name([{"text": "Other", "x": 0.2, "y": 0.2}], 0.0, 0.0) == "Other"
    assert war.nearest_name([], 0.5, 0.5) == ""
    assert war.team_of("wardens") == "WARDENS" and war.team_of(None) == "NONE" and war.team_of("x") == "NONE"
    assert war.faction_team("Warden") == "WARDENS" and war.faction_team("colonials") == "COLONIALS"
    assert war.faction_team(None) is None and war.faction_team("both") is None


async def test_board_render_shows_war_overview(world):
    w = world
    await w.service.poll_once()
    set_fingers(w.api, "WARDENS")
    await w.service.poll_once()
    render = await w.bot.boards.providers["war"].render(w.guild)
    assert [(b.action, b.label) for b in render.buttons] == [("captures", "Recent Captures"), ("hexes", "Hex Status")]
    assert not render.options
    embed = render.embeds[0]
    assert embed.title == "War 127"
    assert "Status: **In progress**" in embed.description
    assert "Day **4** of the war" in embed.description
    assert "War API data updated <t:" in embed.description
    assert "**The Fingers** - Titancall: Colonials -> Wardens" in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Victory towns"] == "Wardens: **1** of 32\nColonials: **1** of 32"
    assert fields["Casualties"] == "Wardens: **77**\nColonials: **55**"
    assert fields["Enlistments"] == "**120**"
    assert len(embed) <= EMBED_TOTAL_LIMIT
    assert not EMOJI.search(all_text(embed))
    view = build_board_view("war", render)
    assert {item.custom_id for item in view.children} == {"fb:war:btn:captures", "fb:war:btn:hexes"}
    assert render.embeds[0].footer.text.startswith("Live data from the official War API")


async def test_board_render_without_data_and_with_many_events(world):
    w = world
    render = await w.bot.boards.providers["war"].render(w.guild)
    assert render.embeds[0].description == w.war.NO_DATA_TEXT
    await w.service.poll_once()
    rows = [
        ("war-a", DEAD, "Deadlands", "T" * 200 + str(index), "WARDENS", "COLONIALS", 1, 1_000 + index)
        for index in range(50)
    ]
    async with w.bot.db.transaction() as tx:
        await tx.executemany(
            "INSERT INTO war_events (war_id, map_name, hex, town, old_team, new_team, victory, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    state = await w.war.load_state(w.bot)
    state["polled_at"] = int(time.time()) - 3600
    state["winner"] = "WARDENS"
    state["resistance_start"] = 5
    await w.war.save_state(w.bot, state)
    embed = (await w.bot.boards.providers["war"].render(w.guild)).embeds[0]
    assert len(embed) <= EMBED_TOTAL_LIMIT and len(embed.description) <= EMBED_DESCRIPTION_LIMIT
    assert "Over: the Wardens won (resistance phase)" in embed.description
    assert "could not be reached recently" in embed.description
    assert embed.description.count("(victory town)") <= 10


async def test_board_buttons_show_captures_and_hex_status(world):
    w = world
    await w.service.poll_once()
    for team in ("NONE", "WARDENS", "NONE", "COLONIALS"):
        set_fingers(w.api, team)
        await w.service.poll_once()
    board = w.bot.boards.providers["war"]
    click = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await board.on_button(click, "captures")
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"]
    embed = payload["embed"]
    assert embed.title == "Town Changes - War 127"
    assert embed.description.count("Titancall") == 4
    assert embed.description.index("Neutral -> Colonials") < embed.description.index("Colonials -> Neutral")

    rows = [("war-a", DEAD, "Deadlands", f"Town {index}", "WARDENS", "COLONIALS", 0, 10 + index) for index in range(150)]
    async with w.bot.db.transaction() as tx:
        await tx.executemany(
            "INSERT INTO war_events (war_id, map_name, hex, town, old_team, new_team, victory, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    click = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await board.on_button(click, "captures")
    paginator = click.recorder.last[1]["view"]
    assert isinstance(paginator, EmbedPaginator) and len(paginator.embeds) > 1
    assert all(len(page) <= EMBED_TOTAL_LIMIT for page in paginator.embeds)
    next_click = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await find_button(paginator, "Next").callback(next_click)
    assert next_click.recorder.last[0] == "edit"

    click = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await board.on_button(click, "hexes")
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"]
    picker = payload["view"]
    assert isinstance(picker, PickerView)
    select = next(item for item in picker.children if isinstance(item, discord.ui.Select))
    assert [(o.label, o.value, o.description) for o in select.options] == [
        ("Deadlands", DEAD, "Wardens 1 | Colonials 1 | Neutral 0"),
        ("The Fingers", FINGERS, "Wardens 0 | Colonials 1 | Neutral 0"),
    ]
    select._values = [DEAD]
    pick = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await select.callback(pick)
    kind, payload = pick.recorder.last
    assert kind == "edit" and payload["view"] is picker
    hex_embed = payload["embed"]
    assert hex_embed.title == "Deadlands - Towns"
    assert "**Abandoned Ward** - Wardens (Town Base (tier 1), victory town)" in hex_embed.description
    assert "**The Pits** - Colonials (Town Base (tier 3), victory town)" in hex_embed.description
    assert hex_embed.footer.text.startswith("Wardens 1 | Colonials 1 | Neutral 0")

    click = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await board.on_button(click, "explode")
    assert "no longer supported" in click.recorder.last[1]["content"]
    pick = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await board.on_pick(pick, "x")
    assert pick.recorder.last[1]["ephemeral"]


async def test_hex_status_without_data_and_outsiders(world):
    w = world
    board = w.bot.boards.providers["war"]
    click = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await board.on_button(click, "hexes")
    assert "No town data yet" in click.recorder.last[1]["content"]
    stranger = make_member(make_guild(), name="Stranger")
    for action in ("hexes", "captures"):
        click = make_interaction(w.bot, stranger, channel=w.board_channel)
        click.user = MagicMock(spec=discord.User)
        click.user.id = next_id()
        await board.on_button(click, action)
        assert "only works inside the server" in click.recorder.last[1]["content"]


async def test_status_hex_and_captures_commands(world):
    w = world
    cog = w.cog
    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.status.callback(cog, interaction)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"] and payload["embed"].description == w.war.NO_DATA_TEXT
    await w.service.poll_once()
    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.status.callback(cog, interaction)
    assert interaction.recorder.last[1]["embed"].title == "War 127"

    choices = await cog.hex_autocomplete(make_interaction(w.bot, w.plain), "dead")
    assert [c.value for c in choices] == ["Deadlands"]
    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.hex_command.callback(cog, interaction, "deadlands")
    kind, payload = interaction.recorder.last
    assert payload["ephemeral"] and payload["embed"].title == "Deadlands - Towns"
    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.hex_command.callback(cog, interaction, "Fingers")
    assert "Titancall" in interaction.recorder.last[1]["embed"].description
    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.hex_command.callback(cog, interaction, "Onyx")
    assert interaction.recorder.last[1]["embed"].description == "No town data for this hex yet."
    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.hex_command.callback(cog, interaction, "zzzzzz")
    assert "Unknown hex" in interaction.recorder.last[1]["content"]

    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.captures.callback(cog, interaction)
    kind, payload = interaction.recorder.last
    assert payload["ephemeral"] and "No town has changed hands" in payload["embed"].description


async def test_board_command_is_admin_only(world):
    w = world
    cog = w.cog
    interaction = make_interaction(w.bot, w.plain, channel=w.board_channel)
    await cog.board.callback(cog, interaction)
    assert "Only bot admins" in interaction.recorder.last[1]["content"]
    w.bot.boards.post = AsyncMock()
    interaction = make_interaction(w.bot, w.admin, channel=w.board_channel)
    await cog.board.callback(cog, interaction)
    w.bot.boards.post.assert_awaited_once_with(w.guild, "war", w.board_channel)
    content = interaction.recorder.last[1]["content"]
    assert content.startswith("War board posted.") and "alerts are off" in content
    await set_mode(w, "all")
    interaction = make_interaction(w.bot, w.admin, channel=w.board_channel)
    await cog.board.callback(cog, interaction)
    assert interaction.recorder.last[1]["content"] == "War board posted."


async def test_loop_starts_only_when_ready_and_stops_on_unload(world):
    w = world
    service = w.service
    assert not service._loop.is_running()
    await w.cog.cog_load()
    assert not service._loop.is_running()
    service.poll_once = AsyncMock()
    w.bot.wait_until_ready = AsyncMock()
    await w.cog.on_ready()
    await asyncio.sleep(0.05)
    assert service._loop.is_running()
    service.poll_once.assert_awaited()
    await w.cog.on_ready()
    await w.cog.cog_unload()
    await asyncio.sleep(0.05)
    assert not service._loop.is_running()


async def test_capture_alert_is_clipped_to_embed_limits():
    from foxbot.features import war

    changes = [war.TownChange(DEAD, "Deadlands", "X" * 150 + str(index), "WARDENS", "COLONIALS", True) for index in range(300)]
    embed = war.capture_embed(changes, "WARDENS", {"war_number": 127})
    assert len(embed.description) <= EMBED_DESCRIPTION_LIMIT and len(embed) <= EMBED_TOTAL_LIMIT
    assert "more. Use `/war captures` for the full list." in embed.description
    assert embed.title == "300 Towns Changed Hands"
    assert not EMOJI.search(all_text(embed))
    for text in (war.new_war_embed({"war_number": 128, "previous_war_number": 127}), war.war_over_embed({"winner": "WARDENS"})):
        assert not EMOJI.search(all_text(text))


async def test_dynamic_archive_item_round_trip(world):
    w = world
    item = w.war.ArchiveWarItem(127)
    assert item.item.custom_id == "fw:archive:127" and item.item.label == "Archive Previous War"
    match = w.war.ArchiveWarItem.__discord_ui_compiled_template__.fullmatch("fw:archive:127")
    rebuilt = await w.war.ArchiveWarItem.from_custom_id(make_interaction(w.bot, w.admin), item.item, match)
    assert rebuilt.war_number == 127
    view = w.war.new_war_view({"previous_war_number": None})
    assert [child.custom_id for child in view.children] == ["fw:archive:0"]
    assert w.war.archive_label(None) is None
    assert w.war.archive_label({"war_number": 128, "winner": "NONE"}) == 127
    assert w.war.archive_label({"war_number": 128, "winner": "COLONIALS"}) == 128
    assert w.war.archive_label({"war_number": 1, "winner": "NONE"}) is None


ARCHIVE_EXTENSIONS = [
    "foxbot.features.stockpiles",
    "foxbot.features.ships",
    "foxbot.features.msupps",
    "foxbot.features.orders",
    "foxbot.features.rares",
    "foxbot.features.war",
]


@pytest.fixture
async def archive_world():
    w = await build_world(ARCHIVE_EXTENSIONS)
    yield w
    await w.cog.cog_unload()
    await w.bot.db.close()


async def seed_war_data(bot, guild_id, tag):
    now = int(time.time())
    channel = next_id()
    async with bot.db.transaction() as tx:
        for index in range(2):
            await tx.execute(
                "INSERT INTO stockpiles (id, guild_id, name, hex, store_type, priority, code, expires_at, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 'Deadlands', 'Seaport', 'High', '123456', ?, 1, ?, ?)",
                (f"S{tag}{index}", guild_id, f"Stock {index}", now + 3600, now, now),
            )
        await tx.execute(
            "INSERT INTO ships (id, guild_id, name, ship_type, hex, expires_at, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'Boat', 'Destroyer', 'Deadlands', ?, 1, ?, ?)",
            (f"H{tag}", guild_id, now + 3600, now, now),
        )
        await tx.execute(
            "INSERT INTO msupps_bases (id, guild_id, name, hex, msupps, rate, measured_at, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'Base', 'Deadlands', 100, 5, ?, 1, ?, ?)",
            (f"M{tag}", guild_id, now, now, now),
        )
        for stockpile_id in (f"S{tag}0", None):
            cursor = await tx.execute(
                "INSERT INTO inventory_snapshots (guild_id, stockpile_id, stock_key, source, imported_by, imported_at) VALUES (?, ?, 'key', 'clipboard', 1, ?)",
                (guild_id, stockpile_id, now),
            )
            await tx.execute(
                "INSERT INTO inventory_items (snapshot_id, item_code, item_name, crated, quantity) VALUES (?, 'Cloth', 'Basic Materials', 1, 5)",
                (cursor.lastrowid,),
            )
        await tx.execute(
            "INSERT INTO orders (id, guild_id, order_type, title, status, channel_id, message_id, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'Production', 'Ammo', 'open', ?, ?, 1, ?, ?)",
            (f"O{tag}", guild_id, channel, 5000 + guild_id % 1000, now, now),
        )
        cursor = await tx.execute(
            "INSERT INTO order_lines (order_id, position, item_name, target, done) VALUES (?, 0, 'Shells', 10, 2)", (f"O{tag}",)
        )
        await tx.execute(
            "INSERT INTO order_contributions (order_id, line_id, user_id, amount, created_at) VALUES (?, ?, 1, 2, ?)",
            (f"O{tag}", cursor.lastrowid, now),
        )
        await tx.execute(
            "INSERT INTO logi_runs (id, guild_id, title, priority, status, ping_channel_id, ping_message_id, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'Haul', 'High', 'open', ?, ?, 1, ?, ?)",
            (f"L{tag}", guild_id, channel, 6000 + guild_id % 1000, now, now),
        )
        await tx.execute(
            "INSERT INTO facility_queue (id, guild_id, item, quantity, position, status, requested_by, created_at, updated_at) "
            "VALUES (?, ?, 'Tank', 1, 0, 'queued', 1, ?, ?)",
            (f"F{tag}", guild_id, now, now),
        )
        for kind, entry_id in (("stockpile", f"S{tag}0"), ("ship", f"H{tag}"), ("msupps", f"M{tag}"), ("logi", f"L{tag}")):
            await tx.execute("INSERT INTO entry_access (kind, entry_id, target_type, target_id) VALUES (?, ?, 'user', 7)", (kind, entry_id))
            await tx.execute("INSERT INTO entry_subscribers (kind, entry_id, user_id) VALUES (?, ?, 7)", (kind, entry_id))
            await tx.execute("INSERT INTO entry_alert_roles (kind, entry_id, role_id) VALUES (?, ?, 8)", (kind, entry_id))
            await tx.execute(
                "INSERT INTO entry_screenshots (kind, entry_id, filename, content_type, data, uploaded_by, uploaded_at) VALUES (?, ?, 'a.png', 'image/png', x'00', 1, ?)",
                (kind, entry_id, now),
            )
            await tx.execute("INSERT INTO alert_state (kind, entry_id, threshold, sent_at) VALUES (?, ?, 12.0, ?)", (kind, entry_id, now))
            await tx.execute(
                "INSERT INTO alert_messages (kind, entry_id, channel_id, message_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (kind, entry_id, channel, next_id(), now),
            )
        await tx.execute("INSERT INTO rares_stock (guild_id, count) VALUES (?, 40)", (guild_id,))
        await tx.execute("INSERT INTO ticket_services (guild_id, name, cost) VALUES (?, 'Tank', 50)", (guild_id,))
        await tx.execute(
            "INSERT INTO tickets (guild_id, number, regiment, status, opened_by, opened_at) VALUES (?, 1, 'ABC', 'Open', 1, ?)",
            (guild_id, now),
        )
        await tx.execute(
            "INSERT INTO boards (guild_id, kind, channel_id, message_id) VALUES (?, 'stockpile', ?, ?)",
            (guild_id, channel, next_id()),
        )
    await bot.settings.set(guild_id, keys.FACTION, "warden")
    await bot.perms.set_roles(guild_id, "stockpile", {guild_id})


WAR_TABLES = ["stockpiles", "ships", "msupps_bases", "inventory_snapshots", "orders", "logi_runs", "facility_queue"]


async def guild_counts(bot, guild_id):
    counts = {}
    for table in WAR_TABLES + ["rares_stock", "ticket_services", "tickets", "boards", "guild_settings", "permission_roles"]:
        counts[table] = await bot.db.fetchval(f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (guild_id,), 0)
    return counts


async def generic_rows(bot):
    counts = {}
    for table in ("entry_access", "entry_subscribers", "entry_alert_roles", "entry_screenshots", "alert_state", "alert_messages"):
        counts[table] = await bot.db.fetchval(f"SELECT COUNT(*) FROM {table}", (), 0)
    return counts


async def test_archive_button_cleans_only_this_guild(archive_world):
    w = archive_world
    bot = w.bot
    await seed_war_data(bot, w.guild.id, "A")
    await seed_war_data(bot, w.other.id, "B")
    deleted = partial_recorder(bot)
    before_other = await guild_counts(bot, w.other.id)
    item = w.war.ArchiveWarItem(127)

    click = make_interaction(bot, w.plain, channel=w.war_channel)
    await item.callback(click)
    assert "Only bot admins" in click.recorder.last[1]["content"]
    assert (await guild_counts(bot, w.guild.id))["stockpiles"] == 2

    click = make_interaction(bot, w.admin, channel=w.war_channel)
    await item.callback(click)
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"]
    prompt = payload["content"]
    assert prompt.startswith("**Archive war 127 for this server?**")
    assert "2 stockpiles, 1 ship, 1 msupps base, 2 inventory snapshots, 1 order, 1 logi run, 1 facility queue entry" in prompt
    assert "only lives in memory" in prompt
    confirm = payload["view"]
    assert isinstance(confirm, ConfirmView)

    sneaky = make_interaction(bot, w.plain, channel=w.war_channel)
    await find_button(confirm, "Archive").callback(sneaky)
    assert "Only bot admins" in sneaky.recorder.last[1]["content"]
    assert (await guild_counts(bot, w.guild.id))["stockpiles"] == 2

    press = make_interaction(bot, w.admin, channel=w.war_channel)
    await find_button(confirm, "Archive").callback(press)
    assert press.recorder.kinds() == ["defer", "edit_original"]
    done = press.recorder.last[1]["content"]
    assert done.startswith("War 127 archived for this server.")
    assert "No backup was made" in done
    assert "Deleted: 2 stockpiles, 1 ship, 1 msupps base, 2 inventory snapshots, 1 order, 1 logi run, 1 facility queue entry." in done
    assert press.recorder.last[1]["view"] is None

    after = await guild_counts(bot, w.guild.id)
    assert all(after[table] == 0 for table in WAR_TABLES)
    assert after["rares_stock"] == 1 and after["ticket_services"] == 1 and after["tickets"] == 1
    assert after["boards"] == 1 and after["guild_settings"] == 1 and after["permission_roles"] == 1
    assert await guild_counts(bot, w.other.id) == before_other
    assert await generic_rows(bot) == {name: 4 for name in ("entry_access", "entry_subscribers", "entry_alert_roles", "entry_screenshots", "alert_state", "alert_messages")}
    assert await bot.db.fetchval("SELECT COUNT(*) FROM order_lines", (), 0) == 1
    assert await bot.db.fetchval("SELECT COUNT(*) FROM order_contributions", (), 0) == 1
    assert await bot.db.fetchval("SELECT COUNT(*) FROM inventory_items", (), 0) == 2
    order_message = 5000 + w.guild.id % 1000
    logi_message = 6000 + w.guild.id % 1000
    deleted_ids = {message_id for _, message_id in deleted}
    assert order_message in deleted_ids and logi_message in deleted_ids
    assert len(deleted) == 6
    archives = await bot.db.fetchall("SELECT * FROM war_archives")
    assert len(archives) == 1
    row = archives[0]
    assert (row["guild_id"], row["war_number"], row["archived_by"]) == (w.guild.id, 127, w.admin.id)
    summary = json.loads(row["summary"])
    assert summary["deleted"]["stockpiles"] == 2 and summary["deleted"]["inventory_snapshots"] == 2
    assert summary["backup"] is None
    refreshed = {call.args for call in bot.boards.request_refresh.call_args_list}
    assert refreshed >= {(w.guild.id, kind) for kind in ("stockpile", "ship", "msupps", "orders", "rares", "war")}

    click = make_interaction(bot, w.admin, channel=w.war_channel)
    await item.callback(click)
    prompt = click.recorder.last[1]["content"]
    assert "This server already archived war 127" in prompt
    assert "0 stockpiles, 0 ships" in prompt


async def test_archive_command_labels_the_previous_war(archive_world):
    w = archive_world
    cog = w.cog
    interaction = make_interaction(w.bot, w.plain, channel=w.war_channel)
    await cog.archive.callback(cog, interaction)
    assert "Only bot admins" in interaction.recorder.last[1]["content"]
    interaction = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await cog.archive.callback(cog, interaction)
    assert interaction.recorder.last[1]["content"].startswith("**Archive the previous war for this server?**")
    await w.war.save_state(w.bot, {"war_id": "x", "war_number": 128, "winner": "NONE"})
    interaction = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await cog.archive.callback(cog, interaction)
    assert interaction.recorder.last[1]["content"].startswith("**Archive war 127 for this server?**")
    confirm = interaction.recorder.last[1]["view"]
    cancel = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await find_button(confirm, "Cancel").callback(cancel)
    assert cancel.recorder.last[1]["content"] == "Cancelled."
    assert await w.bot.db.fetchval("SELECT COUNT(*) FROM war_archives", (), 0) == 0


async def test_archive_aborts_when_backup_fails(archive_world, monkeypatch):
    w = archive_world
    await seed_war_data(w.bot, w.guild.id, "A")
    monkeypatch.setattr(w.war, "backup_database", AsyncMock(side_effect=OSError("disk full")))
    click = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await w.war.ArchiveWarItem(127).callback(click)
    confirm = click.recorder.last[1]["view"]
    press = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await find_button(confirm, "Archive").callback(press)
    assert "nothing was deleted" in press.recorder.last[1]["content"]
    assert (await guild_counts(w.bot, w.guild.id))["stockpiles"] == 2
    assert await w.bot.db.fetchval("SELECT COUNT(*) FROM war_archives", (), 0) == 0
    assert not w.war._archive_locks[w.guild.id].locked()


async def test_archive_refuses_while_another_runs(archive_world):
    w = archive_world
    click = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await w.war.ArchiveWarItem(127).callback(click)
    confirm = click.recorder.last[1]["view"]
    lock = w.war._archive_locks[w.guild.id]
    await lock.acquire()
    try:
        press = make_interaction(w.bot, w.admin, channel=w.war_channel)
        await find_button(confirm, "Archive").callback(press)
        assert press.recorder.last[1]["content"] == "An archive is already running for this server."
    finally:
        lock.release()


async def test_archive_writes_a_backup_the_daily_prune_keeps(tmp_path):
    bot = FoxBot(config(tmp_path), extensions=["foxbot.features.war"], database=Database(tmp_path / "foxbot.db"))
    await bot.db.connect()
    bot.locations._load_file(SEED_PATH)
    bot.boards.request_refresh = MagicMock()
    await bot.load_extension("foxbot.features.war")
    war = bot.extensions["foxbot.features.war"]
    try:
        guild = make_guild(owner_id=1)
        await seed_war_data(bot, guild.id, "A")
        result = await war.archive_guild(bot, guild.id, 127, 1)
        assert result.backup is not None and result.backup.exists()
        assert result.backup.parent == tmp_path / "backups"
        assert result.backup.name.startswith(f"war-127-{guild.id}-") and result.backup.suffix == ".db"
        async with aiosqlite.connect(result.backup) as copy_db:
            cursor = await copy_db.execute("SELECT COUNT(*) FROM stockpiles WHERE guild_id = ?", (guild.id,))
            assert (await cursor.fetchone())[0] == 2
        assert await bot.db.fetchval("SELECT COUNT(*) FROM stockpiles", (), 0) == 0
        for day in range(20):
            (tmp_path / "backups" / f"foxbot-2026-01-{day + 1:02d}.db").write_bytes(b"")
        bot.backups.prune()
        assert result.backup.exists()
        assert len(list((tmp_path / "backups").glob("foxbot-*.db"))) == 14
        assert not list((tmp_path / "backups").glob("*.tmp"))
    finally:
        await bot.get_cog("War").cog_unload()
        await bot.db.close()


async def test_reports_keep_per_map_totals_and_skip_home_regions(world):
    w = world
    clock = [1_000_000]
    w.service.clock = lambda: clock[0]
    w.api.routes[MAPS_PATH] = [DEAD, "HomeRegionC", FINGERS, "HomeRegionW"]
    await w.service.poll_once()
    assert w.service.maps == [DEAD, FINGERS]
    assert not any(path.startswith(("/worldconquest/maps/HomeRegion", "/worldconquest/warReport/HomeRegion")) for path, _ in w.api.calls)
    assert (await w.war.load_state(w.bot))["reports"]["enlistments"] == 120

    w.api.fail[report_path(FINGERS)] = 500
    w.api.routes[report_path(DEAD)] = {"totalEnlistments": 500, "colonialCasualties": 60, "wardenCasualties": 80}
    clock[0] += 31 * 60
    outcome = await w.service.poll_once()
    assert outcome.changed
    reports = (await w.war.load_state(w.bot))["reports"]
    assert (reports["enlistments"], reports["wardens"], reports["colonials"]) == (520, 87, 65)
    assert reports["updated_at"] == clock[0]

    fresh = w.war.WarService(w.bot, WarApiClient(BASE, fetcher=w.api.fetch, min_interval=0))
    fresh.clock = lambda: clock[0]
    w.api.routes[report_path(DEAD)] = {"totalEnlistments": 600, "colonialCasualties": 61, "wardenCasualties": 81}
    clock[0] += 31 * 60
    await fresh.poll_once()
    reports = (await w.war.load_state(w.bot))["reports"]
    assert (reports["enlistments"], reports["wardens"], reports["colonials"]) == (620, 88, 66)
    before = w.api.count(report_path(DEAD))
    clock[0] += 300
    await fresh.poll_once()
    assert w.api.count(report_path(DEAD)) == before


async def test_scorched_victory_towns_lower_the_requirement(world):
    w = world
    await w.service.poll_once()
    pits = w.api.routes[dynamic_path(DEAD)]["mapItems"][1]
    pits["teamId"], pits["flags"] = "NONE", 0x01 | 0x10
    await w.service.poll_once()
    fields = {field.name: field.value for field in (await w.war.build_status_embed(w.bot)).fields}
    assert fields["Victory towns"] == (
        "Wardens: **1** of 31\nColonials: **0** of 31\nNeutral: 1\nScorched: 1, so 31 are needed instead of 32"
    )
    assert w.war.victory_text({}, {}, 2) == "Wardens: **0**\nColonials: **0**\nScorched: 2"


async def test_old_notice_button_is_refused(archive_world):
    w = archive_world
    await seed_war_data(w.bot, w.guild.id, "A")
    await w.war.save_state(w.bot, {"war_id": "c", "war_number": 129, "previous_war_number": 128, "winner": "NONE"})
    click = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await w.war.ArchiveWarItem(127).callback(click)
    kind, payload = click.recorder.last
    assert kind == "send" and payload["ephemeral"] and "view" not in payload
    assert "older new war notice" in payload["content"] and "`/war archive`" in payload["content"]
    plain = make_interaction(w.bot, w.plain, channel=w.war_channel)
    await w.war.ArchiveWarItem(127).callback(plain)
    assert "Only bot admins" in plain.recorder.last[1]["content"]
    for number in (128, 0):
        click = make_interaction(w.bot, w.admin, channel=w.war_channel)
        await w.war.ArchiveWarItem(number).callback(click)
        assert isinstance(click.recorder.last[1]["view"], ConfirmView)
    await w.war.save_state(w.bot, {"war_id": "c", "war_number": 129, "previous_war_number": 128, "winner": "NONE"})
    interaction = make_interaction(w.bot, w.admin, channel=w.war_channel)
    await w.cog.archive.callback(w.cog, interaction)
    assert interaction.recorder.last[1]["content"].startswith("**Archive war 128 for this server?**")
    assert (await guild_counts(w.bot, w.guild.id))["stockpiles"] == 2


async def test_second_backup_in_the_same_second_keeps_the_first(tmp_path, monkeypatch):
    bot = FoxBot(config(tmp_path), extensions=["foxbot.features.war"], database=Database(tmp_path / "foxbot.db"))
    await bot.db.connect()
    bot.locations._load_file(SEED_PATH)
    bot.boards.request_refresh = MagicMock()
    await bot.load_extension("foxbot.features.war")
    war = bot.extensions["foxbot.features.war"]
    monkeypatch.setattr(war, "backup_stamp", lambda: "20261003-120000")
    try:
        guild = make_guild(owner_id=1)
        await seed_war_data(bot, guild.id, "A")
        first = await war.archive_guild(bot, guild.id, 127, 1)
        second = await war.archive_guild(bot, guild.id, 127, 1)
        assert first.backup.name == f"war-127-{guild.id}-20261003-120000.db"
        assert second.backup.name == f"war-127-{guild.id}-20261003-120000-2.db"
        async with aiosqlite.connect(first.backup) as copy_db:
            cursor = await copy_db.execute("SELECT COUNT(*) FROM stockpiles WHERE guild_id = ?", (guild.id,))
            assert (await cursor.fetchone())[0] == 2
    finally:
        await bot.get_cog("War").cog_unload()
        await bot.db.close()


def test_source_follows_house_rules():
    root = Path(__file__).resolve().parent.parent
    for relative in ("foxbot/features/war.py", "foxbot/core/warapi.py", "tests/test_war.py"):
        source = (root / relative).read_text(encoding="utf-8")
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        assert not [t for t in tokens if t.type == tokenize.COMMENT], relative
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                assert ast.get_docstring(node) is None, f"{relative}: {getattr(node, 'name', 'module')}"
        assert not EMOJI.search(source), relative
