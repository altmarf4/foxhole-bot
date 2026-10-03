import re

import discord
import pytest
from discord import app_commands

from fakes import find_button, make_bot, make_channel, make_guild, make_interaction, make_member
from foxbot.core.text import EMBED_DESCRIPTION_LIMIT, EMBED_TOTAL_LIMIT

EXTENSIONS = [
    "foxbot.features.admin",
    "foxbot.features.stockpiles",
    "foxbot.features.moderation",
    "foxbot.features.opsec",
    "foxbot.features.help",
]

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")


@pytest.fixture
async def world():
    bot = await make_bot(EXTENSIONS)
    help_module = bot.extensions["foxbot.features.help"]
    guild = make_guild(owner_id=1)
    member = make_member(guild, name="Reader")
    channel = make_channel(guild, "general")
    yield bot, help_module, member, channel
    await bot.db.close()


def all_text(embed: discord.Embed) -> str:
    return "\n".join(filter(None, [embed.title, embed.description, embed.footer.text if embed.footer else None]))


async def test_help_is_ephemeral_and_paginated(world):
    bot, help_module, member, channel = world
    cog = bot.get_cog("Help")
    interaction = make_interaction(bot, member, channel=channel)
    await cog.help_command.callback(cog, interaction)
    kind, payload = interaction.recorder.last
    assert kind == "send" and payload["ephemeral"]
    first = payload["embed"]
    assert first.title == "Foxhole Logistics Bot - Help"
    paginator = payload["view"]
    total = len(paginator.embeds)
    assert first.footer.text == f"Page 1/{total}"
    click = make_interaction(bot, member, channel=channel)
    await find_button(paginator, "Next").callback(click)
    kind, payload = click.recorder.last
    assert kind == "edit"
    assert payload["embed"].footer.text == f"Page 2/{total}"
    last = make_interaction(bot, member, channel=channel)
    await find_button(paginator, "Last").callback(last)
    assert last.recorder.last[1]["embed"].footer.text == f"Page {total}/{total}"


async def test_help_covers_every_command_and_subcommand(world):
    bot, help_module, _, _ = world
    pages = help_module.build_pages(bot)
    text = "\n".join(all_text(page) for page in pages)
    top_level = bot.tree.get_commands(type=discord.AppCommandType.chat_input)
    assert {c.name for c in top_level} >= {"settings", "permissions", "setup", "purge", "role", "opsec", "help", "stockpile"}
    for command in top_level:
        assert f"**/{command.name}**" in pages[0].description
        assert any(page.title.startswith(f"/{command.name}") for page in pages)
        for leaf in help_module.leaf_commands(command):
            assert f"**/{leaf.qualified_name}**" in text
            assert leaf.description in text
    assert "**/stockpile add** `hex` `town` `name` `code` `storage_type` `[priority]` `[private]`" in text
    assert "**/purge** `amount` `[user]`" in text
    assert "**/role add** `role` `[member]`" in text
    assert "Only shown to members with: Manage Messages." in text
    assert "Only shown to members with: Manage Roles." in text


async def test_help_pages_follow_preferred_order_and_intros(world):
    bot, help_module, _, _ = world
    pages = help_module.build_pages(bot)
    titles = [page.title for page in pages[1:]]
    assert titles == ["/setup", "/stockpile", "/settings", "/permissions", "/purge", "/role", "/opsec", "/help"]
    stockpile_page = pages[2]
    assert stockpile_page.description.startswith(help_module.INTROS["stockpile"])
    rare_intro = help_module.INTROS["rare"]
    assert "Rare Alloys" in rare_intro
    for name in ["stockpile", "ship", "msupps", "inventory", "find", "order", "rare", "ticket", "mine", "settings", "permissions", "setup", "purge", "role", "opsec"]:
        assert name in help_module.INTROS


async def test_help_respects_discord_limits_and_house_rules(world):
    bot, help_module, _, _ = world
    pages = help_module.build_pages(bot)
    for page in pages:
        assert len(page) <= EMBED_TOTAL_LIMIT
        assert len(page.description or "") <= EMBED_DESCRIPTION_LIMIT
        assert len(page.title) <= 256
        assert not EMOJI.search(all_text(page))
        assert re.search(r"\bRA\b", all_text(page)) is None
    for intro in help_module.INTROS.values():
        assert not EMOJI.search(intro)
        assert re.search(r"\bRA\b", intro) is None


async def test_help_is_generated_at_call_time(world):
    bot, help_module, member, channel = world
    group = app_commands.Group(name="ship", description="Track large ships and their squad locks.")

    @group.command(name="add", description="Add a ship.")
    async def add(interaction: discord.Interaction, name: str, notes: str | None = None) -> None:
        pass

    bot.tree.add_command(group)
    pages = help_module.build_pages(bot)
    titles = [page.title for page in pages]
    assert titles.index("/ship") == titles.index("/stockpile") + 1
    ship_page = pages[titles.index("/ship")]
    assert ship_page.description.startswith(help_module.INTROS["ship"])
    assert "**/ship add** `name` `[notes]`" in ship_page.description
    assert "**/ship** - Track large ships" in pages[0].description


async def test_big_group_is_split_across_pages(world):
    bot, help_module, _, _ = world
    group = app_commands.Group(name="bulk", description="A very large group.")
    for index in range(25):
        sub = app_commands.Group(name=f"g{index:02d}", description="Nested group.", parent=group)
        for leaf_index in range(5):

            async def leaf(interaction: discord.Interaction, first_option: str, second_option: str | None = None) -> None:
                pass

            sub.add_command(app_commands.Command(name=f"leaf{leaf_index}", description="x" * 100, callback=leaf))
    bot.tree.add_command(group)
    pages = help_module.build_pages(bot)
    bulk_pages = [page for page in pages if page.title.startswith("/bulk")]
    assert len(bulk_pages) > 1
    assert bulk_pages[0].title == f"/bulk (1/{len(bulk_pages)})"
    joined = "\n".join(page.description for page in bulk_pages)
    assert joined.count("**/bulk g") == 125
    for page in pages:
        assert len(page) <= EMBED_TOTAL_LIMIT
        assert len(page.description) <= EMBED_DESCRIPTION_LIMIT


async def test_opsec_posts_public_three_embed_guide(world):
    bot, _, member, channel = world
    cog = bot.get_cog("Opsec")
    interaction = make_interaction(bot, member, channel=channel)
    await cog.opsec.callback(cog, interaction)
    kind, payload = interaction.recorder.last
    assert kind == "send"
    assert payload["ephemeral"] is False
    embeds = payload["embeds"]
    assert [e.title for e in embeds] == ["OPSEC Warning: Discord Activity Sharing", "How to Turn It Off", "OPSEC Checklist"]
    assert [e.colour.value for e in embeds] == [0xE74C3C, 0xE67E22, 0x2ECC71]
    assert "Activity Privacy" in embeds[1].description
    assert embeds[2].description.count("[ ]") == 3
    assert sum(len(e) for e in embeds) <= EMBED_TOTAL_LIMIT
    for embed in embeds:
        assert not EMOJI.search(all_text(embed))
