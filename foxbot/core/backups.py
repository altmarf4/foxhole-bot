from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
from discord.ext import tasks

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

log = logging.getLogger(__name__)

KEEP = 14


class BackupService:
    def __init__(self, bot: FoxBot, directory: Path):
        self.bot = bot
        self.directory = directory

    def start(self) -> None:
        if not self._loop.is_running():
            self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    def path_for(self, day: dt.date) -> Path:
        return self.directory / f"foxbot-{day.isoformat()}.db"

    async def backup_now(self, day: dt.date | None = None) -> Path:
        day = day or dt.date.today()
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(day)
        temporary = target.with_suffix(".tmp")
        if temporary.exists():
            temporary.unlink()
        async with self.bot.db.transaction():
            async with aiosqlite.connect(temporary) as destination:
                await self.bot.db.conn.backup(destination)
        temporary.replace(target)
        self.prune()
        log.info("Database backed up to %s", target)
        return target

    def prune(self) -> None:
        backups = sorted(self.directory.glob("foxbot-*.db"))
        for old in backups[:-KEEP]:
            try:
                old.unlink()
            except OSError:
                log.warning("Could not delete old backup %s", old)

    @tasks.loop(hours=1)
    async def _loop(self) -> None:
        if self.bot.db.path == ":memory:":
            return
        if self.path_for(dt.date.today()).exists():
            return
        try:
            await self.backup_now()
        except Exception:
            log.exception("Database backup failed")

    @_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(30)
