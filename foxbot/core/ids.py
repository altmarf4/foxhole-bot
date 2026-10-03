import secrets

from foxbot.db import Database

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ID_LENGTH = 6


def generate(length: int = ID_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


async def unique_id(db: Database, table: str) -> str:
    while True:
        candidate = generate()
        exists = await db.fetchval(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,))
        if not exists:
            return candidate
