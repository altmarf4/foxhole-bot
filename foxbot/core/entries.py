from __future__ import annotations

import io
import re
from collections import defaultdict
from typing import Callable, Iterable

import discord

from foxbot.constants import SCREENSHOT_MAX_BYTES
from foxbot.core import timeutil
from foxbot.core.text import md
from foxbot.db import Database, Transaction

INDENT = " "
UNKNOWN_HEX = "Unknown hex"


async def access_for(db: Database, kind: str, entry_id: str) -> tuple[set[int], set[int]]:
    rows = await db.fetchall(
        "SELECT target_type, target_id FROM entry_access WHERE kind = ? AND entry_id = ?",
        (kind, entry_id),
    )
    users = {row["target_id"] for row in rows if row["target_type"] == "user"}
    roles = {row["target_id"] for row in rows if row["target_type"] == "role"}
    return users, roles


async def set_access(db: Database, kind: str, entry_id: str, users: Iterable[int], roles: Iterable[int]) -> None:
    async with db.transaction() as tx:
        await tx.execute("DELETE FROM entry_access WHERE kind = ? AND entry_id = ?", (kind, entry_id))
        await tx.executemany(
            "INSERT INTO entry_access (kind, entry_id, target_type, target_id) VALUES (?, ?, ?, ?)",
            [(kind, entry_id, "user", uid) for uid in set(users)] + [(kind, entry_id, "role", rid) for rid in set(roles)],
        )


async def subscribers(db: Database, kind: str, entry_id: str) -> set[int]:
    rows = await db.fetchall(
        "SELECT user_id FROM entry_subscribers WHERE kind = ? AND entry_id = ?",
        (kind, entry_id),
    )
    return {row["user_id"] for row in rows}


async def is_subscribed(db: Database, kind: str, entry_id: str, user_id: int) -> bool:
    return bool(
        await db.fetchval(
            "SELECT 1 FROM entry_subscribers WHERE kind = ? AND entry_id = ? AND user_id = ?",
            (kind, entry_id, user_id),
        )
    )


async def toggle_subscription(db: Database, kind: str, entry_id: str, user_id: int) -> bool:
    if await is_subscribed(db, kind, entry_id, user_id):
        await db.execute(
            "DELETE FROM entry_subscribers WHERE kind = ? AND entry_id = ? AND user_id = ?",
            (kind, entry_id, user_id),
        )
        return False
    await db.execute(
        "INSERT OR IGNORE INTO entry_subscribers (kind, entry_id, user_id) VALUES (?, ?, ?)",
        (kind, entry_id, user_id),
    )
    return True


async def alert_roles(db: Database, kind: str, entry_id: str) -> list[int]:
    rows = await db.fetchall(
        "SELECT role_id FROM entry_alert_roles WHERE kind = ? AND entry_id = ? ORDER BY role_id",
        (kind, entry_id),
    )
    return [row["role_id"] for row in rows]


async def set_alert_roles(db: Database, kind: str, entry_id: str, role_ids: Iterable[int]) -> None:
    async with db.transaction() as tx:
        await tx.execute("DELETE FROM entry_alert_roles WHERE kind = ? AND entry_id = ?", (kind, entry_id))
        await tx.executemany(
            "INSERT INTO entry_alert_roles (kind, entry_id, role_id) VALUES (?, ?, ?)",
            [(kind, entry_id, rid) for rid in set(role_ids)],
        )


async def screenshot(db: Database, kind: str, entry_id: str) -> dict | None:
    return await db.fetchone(
        "SELECT filename, content_type, data, uploaded_by, uploaded_at FROM entry_screenshots WHERE kind = ? AND entry_id = ?",
        (kind, entry_id),
    )


async def has_screenshot(db: Database, kind: str, entry_id: str) -> bool:
    return bool(
        await db.fetchval(
            "SELECT 1 FROM entry_screenshots WHERE kind = ? AND entry_id = ?",
            (kind, entry_id),
        )
    )


async def save_screenshot(db: Database, kind: str, entry_id: str, attachment: discord.Attachment, user_id: int) -> str | None:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        return "That file is not an image. Upload a PNG, JPG, GIF or WEBP screenshot."
    if attachment.size > SCREENSHOT_MAX_BYTES:
        return f"That image is too large. The limit is {SCREENSHOT_MAX_BYTES // (1024 * 1024)} MB."
    data = await attachment.read()
    extension = content_type.split("/", 1)[1].replace("jpeg", "jpg")
    filename = f"screenshot.{re.sub(r'[^a-z0-9]', '', extension) or 'png'}"
    await db.execute(
        "INSERT INTO entry_screenshots (kind, entry_id, filename, content_type, data, uploaded_by, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (kind, entry_id) DO UPDATE SET "
        "filename = excluded.filename, content_type = excluded.content_type, data = excluded.data, "
        "uploaded_by = excluded.uploaded_by, uploaded_at = excluded.uploaded_at",
        (kind, entry_id, filename, content_type, data, user_id, timeutil.now()),
    )
    return None


async def delete_screenshot(db: Database, kind: str, entry_id: str) -> None:
    await db.execute("DELETE FROM entry_screenshots WHERE kind = ? AND entry_id = ?", (kind, entry_id))


def screenshot_file(row: dict) -> discord.File:
    return discord.File(io.BytesIO(row["data"]), filename=row["filename"])


async def attach_screenshot(db: Database, kind: str, entry_id: str, embed: discord.Embed) -> list[discord.File]:
    row = await screenshot(db, kind, entry_id)
    if row is None:
        return []
    embed.set_image(url=f"attachment://{row['filename']}")
    return [screenshot_file(row)]


async def delete_children(tx: Transaction, kind: str, entry_id: str) -> None:
    for table in ("entry_access", "entry_alert_roles", "entry_screenshots", "entry_subscribers", "alert_state"):
        await tx.execute(f"DELETE FROM {table} WHERE kind = ? AND entry_id = ?", (kind, entry_id))


def location_label(hex_name: str, region: str) -> str:
    if hex_name and region:
        return f"{region}, {hex_name}"
    return hex_name or region or UNKNOWN_HEX


def grouped_lines(
    entries: list[dict],
    line: Callable[[dict], str],
    *,
    sort_key: Callable[[dict], object],
) -> list[str]:
    by_hex: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        by_hex[entry.get("hex") or UNKNOWN_HEX][entry.get("region") or ""].append(entry)
    lines: list[str] = []
    for hex_name in sorted(by_hex, key=lambda h: (h == UNKNOWN_HEX, h.lower())):
        lines.append(f"**{md(hex_name)}**")
        regions = by_hex[hex_name]
        for region in sorted(regions, key=lambda r: (r == "", r.lower())):
            indent = INDENT
            if region:
                lines.append(f"{INDENT}__{md(region)}__")
                indent = INDENT * 2
            for entry in sorted(regions[region], key=sort_key):
                lines.append(f"{indent}{line(entry)}")
    return lines


def refreshed_text(entry: dict) -> str:
    if entry.get("refreshed_by"):
        return f"<@{entry['refreshed_by']}> {timeutil.relative(entry['refreshed_at'])}"
    return f"<@{entry['created_by']}> {timeutil.relative(entry['created_at'])} (added)"


def timer_text(expires_at: int, now: int | None = None) -> str:
    now = now if now is not None else timeutil.now()
    if expires_at <= now:
        return "**EXPIRED**"
    return f"expires {timeutil.relative(expires_at)}"
