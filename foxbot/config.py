import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

WAR_API_SHARDS = {
    "able": "https://war-service-live.foxholeservices.com/api",
    "baker": "https://war-service-live-2.foxholeservices.com/api",
    "charlie": "https://war-service-live-3.foxholeservices.com/api",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    token: str
    data_dir: Path
    dev_guild_id: int | None
    log_level: str
    war_api_url: str

    @property
    def database_path(self) -> Path:
        return self.data_dir / "foxbot.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def locations_cache_path(self) -> Path:
        return self.data_dir / "locations_cache.json"


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ConfigError(f"{name} must be a numeric Discord ID, got {raw!r}.")
    return int(raw)


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and put your bot token in it."
        )
    data_dir = Path(os.getenv("FOXBOT_DATA_DIR", "").strip() or PROJECT_ROOT / "data")
    shard = os.getenv("WAR_API_SHARD", "able").strip().lower()
    if shard not in WAR_API_SHARDS:
        raise ConfigError(f"WAR_API_SHARD must be one of {', '.join(WAR_API_SHARDS)}, got {shard!r}.")
    return Config(
        token=token,
        data_dir=data_dir.expanduser().resolve(),
        dev_guild_id=_optional_int("DEV_GUILD_ID"),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        war_api_url=WAR_API_SHARDS[shard],
    )
