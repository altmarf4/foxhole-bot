import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_V1 = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE guild_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE permission_roles (
    guild_id INTEGER NOT NULL,
    grp TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, grp, role_id)
);

CREATE TABLE boards (
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);

CREATE TABLE stockpiles (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    hex TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    store_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    code TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    private INTEGER NOT NULL DEFAULT 0,
    refreshed_by INTEGER,
    refreshed_at INTEGER,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX idx_stockpiles_guild ON stockpiles (guild_id);

CREATE TABLE ships (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    ship_type TEXT NOT NULL,
    squad TEXT NOT NULL DEFAULT '',
    hex TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL,
    refreshed_by INTEGER,
    refreshed_at INTEGER,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX idx_ships_guild ON ships (guild_id);

CREATE TABLE msupps_bases (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    hex TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    msupps INTEGER NOT NULL,
    rate INTEGER NOT NULL,
    measured_at INTEGER NOT NULL,
    measured_by INTEGER,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX idx_msupps_guild ON msupps_bases (guild_id);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    title TEXT NOT NULL,
    hex TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    channel_id INTEGER,
    message_id INTEGER,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    closed_by INTEGER,
    closed_at INTEGER
);
CREATE INDEX idx_orders_guild ON orders (guild_id, status);
CREATE INDEX idx_orders_message ON orders (message_id);

CREATE TABLE order_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    item_code TEXT,
    item_name TEXT NOT NULL,
    target INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_order_lines_order ON order_lines (order_id, position);

CREATE TABLE order_contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    line_id INTEGER NOT NULL REFERENCES order_lines (id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_order_contributions_order ON order_contributions (order_id);

CREATE TABLE inventory_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    stockpile_id TEXT REFERENCES stockpiles (id) ON DELETE CASCADE,
    stock_key TEXT NOT NULL,
    hex TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    store_type TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    faction TEXT,
    game_time INTEGER,
    imported_by INTEGER NOT NULL,
    imported_at INTEGER NOT NULL
);
CREATE INDEX idx_snapshots_guild ON inventory_snapshots (guild_id, stock_key, imported_at);
CREATE INDEX idx_snapshots_stockpile ON inventory_snapshots (stockpile_id, imported_at);

CREATE TABLE inventory_items (
    snapshot_id INTEGER NOT NULL REFERENCES inventory_snapshots (id) ON DELETE CASCADE,
    item_code TEXT NOT NULL,
    item_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    crated INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    per_crate INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (snapshot_id, item_code, crated)
);
CREATE INDEX idx_inventory_items_code ON inventory_items (item_code);

CREATE TABLE entry_access (
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('user', 'role')),
    target_id INTEGER NOT NULL,
    PRIMARY KEY (kind, entry_id, target_type, target_id)
);

CREATE TABLE entry_subscribers (
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (kind, entry_id, user_id)
);

CREATE TABLE entry_alert_roles (
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (kind, entry_id, role_id)
);

CREATE TABLE entry_screenshots (
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data BLOB NOT NULL,
    uploaded_by INTEGER NOT NULL,
    uploaded_at INTEGER NOT NULL,
    PRIMARY KEY (kind, entry_id)
);

CREATE TABLE alert_state (
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    threshold REAL NOT NULL,
    sent_at INTEGER NOT NULL,
    PRIMARY KEY (kind, entry_id, threshold)
);

CREATE TABLE alert_messages (
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX idx_alert_messages_entry ON alert_messages (kind, entry_id);

CREATE TABLE rares_stock (
    guild_id INTEGER PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE rares_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    amount INTEGER NOT NULL,
    old_count INTEGER NOT NULL,
    new_count INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    user_id INTEGER NOT NULL,
    ticket_number INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_rares_log_guild ON rares_log (guild_id, id);

CREATE TABLE ticket_services (
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    cost INTEGER NOT NULL,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE tickets (
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    regiment TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    opened_by INTEGER NOT NULL,
    opened_at INTEGER NOT NULL,
    channel_id INTEGER,
    card_message_id INTEGER,
    assigned_to INTEGER,
    renamed INTEGER NOT NULL DEFAULT 0,
    closed_at INTEGER,
    closed_by INTEGER,
    PRIMARY KEY (guild_id, number)
);
CREATE INDEX idx_tickets_channel ON tickets (channel_id);

CREATE TABLE ticket_items (
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    service TEXT NOT NULL,
    unit_cost INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (guild_id, number, service, unit_cost),
    FOREIGN KEY (guild_id, number) REFERENCES tickets (guild_id, number) ON DELETE CASCADE
);

CREATE TABLE ticket_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (guild_id, number) REFERENCES tickets (guild_id, number) ON DELETE CASCADE
);
CREATE INDEX idx_ticket_payments_ticket ON ticket_payments (guild_id, number);
"""

SCHEMA_V2 = """
CREATE TABLE war_towns (
    map_name TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    hex TEXT NOT NULL,
    town TEXT NOT NULL,
    icon_type INTEGER NOT NULL,
    team TEXT NOT NULL,
    flags INTEGER NOT NULL DEFAULT 0,
    changed_at INTEGER NOT NULL,
    PRIMARY KEY (map_name, x, y)
);

CREATE TABLE war_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    war_id TEXT NOT NULL,
    map_name TEXT NOT NULL,
    hex TEXT NOT NULL,
    town TEXT NOT NULL,
    old_team TEXT NOT NULL,
    new_team TEXT NOT NULL,
    victory INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_war_events_created ON war_events (war_id, created_at);

CREATE TABLE war_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    war_number INTEGER,
    archived_by INTEGER NOT NULL,
    archived_at INTEGER NOT NULL,
    summary TEXT NOT NULL
);
CREATE INDEX idx_war_archives_guild ON war_archives (guild_id, archived_at);

CREATE TABLE logi_runs (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    cargo TEXT NOT NULL DEFAULT '',
    pickup_hex TEXT NOT NULL DEFAULT '',
    pickup_region TEXT NOT NULL DEFAULT '',
    dest_hex TEXT NOT NULL DEFAULT '',
    dest_region TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    claimed_by INTEGER,
    claimed_at INTEGER,
    delivered_by INTEGER,
    delivered_at INTEGER,
    ping_channel_id INTEGER,
    ping_message_id INTEGER,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX idx_logi_runs_guild ON logi_runs (guild_id, status);

CREATE TABLE facility_queue (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    item_code TEXT,
    quantity INTEGER NOT NULL,
    facility TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL,
    status TEXT NOT NULL,
    worker_id INTEGER,
    started_at INTEGER,
    done_by INTEGER,
    done_at INTEGER,
    requested_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX idx_facility_queue_guild ON facility_queue (guild_id, status, position);
"""

MIGRATIONS: list[str] = [SCHEMA_V1, SCHEMA_V2]


class Transaction:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, rows)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row[0] is None:
            return default
        return row[0]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected.")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            await self._conn.execute("PRAGMA journal_mode = WAL")
            await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> None:
        cursor = await self.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        version = row[0]
        for index in range(version, len(MIGRATIONS)):
            log.info("Applying database migration %d", index + 1)
            await self.conn.executescript(f"BEGIN;\n{MIGRATIONS[index]}\nPRAGMA user_version = {index + 1};\nCOMMIT;")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        async with self._lock:
            tx = Transaction(self.conn)
            try:
                yield tx
            except BaseException:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self.transaction() as tx:
            cursor = await tx.execute(sql, params)
            result = cursor.lastrowid if sql.lstrip().upper().startswith("INSERT") else cursor.rowcount
            await cursor.close()
            return result

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        async with self._lock:
            return await Transaction(self.conn).fetchone(sql, params)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        async with self._lock:
            return await Transaction(self.conn).fetchall(sql, params)

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        async with self._lock:
            return await Transaction(self.conn).fetchval(sql, params, default)
