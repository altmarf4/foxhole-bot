import discord

EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
EMBEDS_PER_MESSAGE = 10
SELECT_LABEL_LIMIT = 100
SELECT_DESCRIPTION_LIMIT = 100


def clip(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def md(text: str) -> str:
    return discord.utils.escape_markdown(str(text))


def code(text: str) -> str:
    return "`" + str(text).replace("`", "'") + "`"


def plural(count: int, word: str, plural_word: str | None = None) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {plural_word or word + 's'}"


def chunk_lines(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line = clip(line, limit)
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def embed_size(embed: discord.Embed) -> int:
    return len(embed)


def paginate_embeds(
    title: str,
    lines: list[str],
    *,
    colour: int,
    empty: str,
    per_page_chars: int = 3500,
    footer: str | None = None,
) -> list[discord.Embed]:
    if not lines:
        embed = discord.Embed(title=title, description=empty, colour=colour)
        if footer:
            embed.set_footer(text=footer)
        return [embed]
    pages = chunk_lines(lines, per_page_chars)
    embeds = []
    for index, page in enumerate(pages, start=1):
        embed = discord.Embed(title=title, description=page, colour=colour)
        parts = [footer] if footer else []
        if len(pages) > 1:
            parts.append(f"Page {index}/{len(pages)}")
        if parts:
            embed.set_footer(text=" | ".join(parts))
        embeds.append(embed)
    return embeds


def fit_board_embeds(
    title: str,
    lines: list[str],
    *,
    colour: int,
    empty: str,
    footer: str,
    overflow_hint: str,
) -> list[discord.Embed]:
    if not lines:
        embed = discord.Embed(title=title, description=empty, colour=colour)
        embed.set_footer(text=footer)
        return [embed]
    budget = EMBED_TOTAL_LIMIT - len(title) - len(footer) - len(overflow_hint) - 200
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(clip(line, EMBED_DESCRIPTION_LIMIT)) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    hidden = len(lines) - len(kept)
    chunks = chunk_lines(kept, EMBED_DESCRIPTION_LIMIT - 100)[:EMBEDS_PER_MESSAGE]
    embeds = []
    for index, chunk in enumerate(chunks):
        embed = discord.Embed(title=title if index == 0 else None, description=chunk, colour=colour)
        embeds.append(embed)
    tail = footer
    if hidden:
        embeds[-1].description = (embeds[-1].description or "") + f"\n\n*{overflow_hint}*"
        tail = f"{footer} | {hidden} more line(s) not shown"
    embeds[-1].set_footer(text=tail)
    return embeds
