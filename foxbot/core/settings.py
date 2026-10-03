import copy
import json
from dataclasses import dataclass
from typing import Any

from foxbot import constants
from foxbot.db import Database


@dataclass(frozen=True)
class Setting:
    key: str
    default: Any


ADMIN_ROLE = Setting("admin_role_id", None)
LOGISTICS_ROLE = Setting("logistics_role_id", None)
ALERT_CHANNEL = Setting("alert_channel_id", None)
ALERT_THRESHOLDS = Setting("alert_thresholds", constants.DEFAULT_ALERT_THRESHOLDS)
STORAGE_TYPES = Setting("storage_types", constants.DEFAULT_STORAGE_TYPES)
SHIP_TYPES = Setting("ship_types", constants.DEFAULT_SHIP_TYPES)
TICKET_CATEGORY = Setting("ticket_category_id", None)
TICKET_LOG_CHANNEL = Setting("ticket_log_channel_id", None)
RARES_LOG_CHANNEL = Setting("rares_log_channel_id", None)
RARES_VOICE_CHANNEL = Setting("rares_voice_channel_id", None)
FACTION = Setting("faction", None)
ORDERS_CHANNEL = Setting("orders_channel_id", None)

ALL_SETTINGS = [
    ADMIN_ROLE,
    LOGISTICS_ROLE,
    ALERT_CHANNEL,
    ALERT_THRESHOLDS,
    STORAGE_TYPES,
    SHIP_TYPES,
    TICKET_CATEGORY,
    TICKET_LOG_CHANNEL,
    RARES_LOG_CHANNEL,
    RARES_VOICE_CHANNEL,
    FACTION,
    ORDERS_CHANNEL,
]


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[tuple[int, str], Any] = {}

    async def get(self, guild_id: int, setting: Setting) -> Any:
        cache_key = (guild_id, setting.key)
        if cache_key not in self._cache:
            raw = await self.db.fetchval(
                "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
                (guild_id, setting.key),
            )
            self._cache[cache_key] = json.loads(raw) if raw is not None else copy.deepcopy(setting.default)
        return copy.deepcopy(self._cache[cache_key])

    async def set(self, guild_id: int, setting: Setting, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, setting.key, json.dumps(value)),
        )
        self._cache[(guild_id, setting.key)] = copy.deepcopy(value)

    async def reset(self, guild_id: int, setting: Setting) -> None:
        await self.db.execute(
            "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, setting.key),
        )
        self._cache.pop((guild_id, setting.key), None)

    async def alert_thresholds(self, guild_id: int) -> list[float]:
        values = await self.get(guild_id, ALERT_THRESHOLDS)
        return sorted({float(v) for v in values if float(v) > 0}, reverse=True)
