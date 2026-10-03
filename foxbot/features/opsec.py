from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from foxbot.constants import Colour
from foxbot.core.ui import reply

if TYPE_CHECKING:
    from foxbot.bot import FoxBot

WARNING_TEXT = (
    "Discord's **Activity Sharing** feature can broadcast what game you're playing - including live details - "
    "to anyone who can see your profile.\n\n"
    "For Foxhole, this can leak **which region or hex you are currently in** to anyone watching, including "
    "enemies who share a server with you. Treat this the same as any other operational security leak."
)

STEPS_TEXT = (
    "1. Open Discord **Settings** (gear icon near your username).\n"
    "2. Go to **Activity Privacy**.\n"
    "3. Find **Activity Sharing** (sometimes called 'Display current activity as a status message').\n"
    "4. Toggle it **off**.\n"
    "5. Repeat this check after any major Discord update - settings occasionally reset."
)

CHECKLIST_TEXT = (
    "[ ] Activity Sharing is toggled **off** in Settings > Activity Privacy\n"
    "[ ] Checked on every device I use Discord on\n"
    "[ ] Reminded a squadmate who might not know about this\n\n"
    "Stay safe out there."
)


def guide_embeds() -> list[discord.Embed]:
    return [
        discord.Embed(title="OPSEC Warning: Discord Activity Sharing", description=WARNING_TEXT, colour=Colour.DANGER),
        discord.Embed(title="How to Turn It Off", description=STEPS_TEXT, colour=Colour.ORANGE),
        discord.Embed(title="OPSEC Checklist", description=CHECKLIST_TEXT, colour=Colour.SUCCESS),
    ]


class Opsec(commands.Cog):
    def __init__(self, bot: FoxBot):
        self.bot = bot

    @app_commands.command(name="opsec", description="Post a guide on stopping Discord from leaking your Foxhole region.")
    async def opsec(self, interaction: discord.Interaction) -> None:
        await reply(interaction, embeds=guide_embeds(), ephemeral=False)


async def setup(bot: FoxBot) -> None:
    await bot.add_cog(Opsec(bot))
