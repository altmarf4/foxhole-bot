from __future__ import annotations

import hashlib
import json
import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.config import Config, ConfigError, load_config
from foxbot.core.alerts import AlertActionItem, AlertDismissItem, AlertService
from foxbot.core.backups import BackupService
from foxbot.core.boards import BoardButtonItem, BoardSelectItem, BoardService
from foxbot.core.catalog import Catalog
from foxbot.core.locations import LocationService
from foxbot.core.permissions import PermissionService
from foxbot.core.settings import SettingsStore
from foxbot.core.ui import deny, report_error
from foxbot.db import Database
from foxbot.logsetup import setup_logging

log = logging.getLogger("foxbot")

EXTENSIONS = [
    "foxbot.features.admin",
    "foxbot.features.stockpiles",
    "foxbot.features.ships",
    "foxbot.features.msupps",
    "foxbot.features.inventory",
    "foxbot.features.orders",
    "foxbot.features.rares",
    "foxbot.features.tickets",
    "foxbot.features.mine",
    "foxbot.features.moderation",
    "foxbot.features.opsec",
    "foxbot.features.help",
]


class FoxBot(commands.Bot):
    def __init__(self, config: Config, *, extensions: list[str] | None = None, database: Database | None = None):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False),
        )
        self.config = config
        self.extension_names = list(EXTENSIONS if extensions is None else extensions)
        self.db = database or Database(config.database_path)
        self.settings = SettingsStore(self.db)
        self.perms = PermissionService(self.db, self.settings)
        self.locations = LocationService(config.locations_cache_path, config.war_api_url)
        self.catalog = Catalog()
        self.boards = BoardService(self)
        self.alerts = AlertService(self)
        self.backups = BackupService(self, config.data_dir / "backups")
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.locations.start()
        self.add_dynamic_items(BoardButtonItem, BoardSelectItem, AlertActionItem, AlertDismissItem)
        for name in self.extension_names:
            await self.load_extension(name)
        await self.sync_commands()
        self.boards.start()
        self.alerts.start()
        self.backups.start()

    def command_payload(self) -> list[dict]:
        return [command.to_dict(self.tree) for command in self.tree.get_commands()]

    async def sync_commands(self) -> None:
        if self.config.dev_guild_id:
            guild = discord.Object(self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to development guild %s", len(synced), self.config.dev_guild_id)
            return
        digest = hashlib.sha256(json.dumps(self.command_payload(), sort_keys=True, default=str).encode()).hexdigest()
        stored = await self.db.fetchval("SELECT value FROM meta WHERE key = 'command_hash'")
        if stored == digest:
            log.info("Slash commands unchanged; skipping sync")
            return
        synced = await self.tree.sync()
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES ('command_hash', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (digest,),
        )
        log.info("Synced %d global commands (new commands can take a few minutes to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d server(s)", self.user, self.user.id if self.user else "?", len(self.guilds))

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await deny(interaction, f"You need these Discord permissions: {', '.join(error.missing_permissions)}.")
        elif isinstance(error, app_commands.BotMissingPermissions):
            await deny(interaction, f"I am missing these Discord permissions: {', '.join(error.missing_permissions)}.")
        elif isinstance(error, app_commands.NoPrivateMessage):
            await deny(interaction, "This command only works inside a server.")
        elif isinstance(error, app_commands.CheckFailure):
            await deny(interaction, "You cannot use this command.")
        else:
            original = getattr(error, "original", error)
            name = interaction.command.qualified_name if interaction.command else "unknown"
            await report_error(interaction, original, f"/{name}")

    async def close(self) -> None:
        self.backups.stop()
        self.alerts.stop()
        self.boards.stop()
        await self.locations.close()
        await super().close()
        await self.db.close()


def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    setup_logging(config)
    bot = FoxBot(config)
    try:
        bot.run(config.token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord refused the connection because privileged intents are off. Open the Discord Developer Portal, "
            "select the bot, and enable SERVER MEMBERS INTENT and MESSAGE CONTENT INTENT under Bot."
        )
        raise SystemExit(1)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Reset it in the Developer Portal and update DISCORD_TOKEN in .env.")
        raise SystemExit(1)
