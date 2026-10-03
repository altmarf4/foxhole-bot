# Foxhole Logistics Bot 2.0 - Specification

The single source of truth for the rebuild. Decisions here were made with the regiment's lead on 2026-10-02/03.

## Delivery phases

| Phase | Scope | Gate |
|---|---|---|
| 1 | Rebuild of every existing feature with the new UX, CSV inventory, orders (replacing production requests), ticket order history, setup wizard, daily backups | Live test on the test bot |
| 2 | War API: `/war status`, live war board, new-war cleanup, town-captured alerts | Live test |
| 3 | Logi run requests, facility queue (simple list) | Live test |

Dropped on purpose: in-game name registration, operations/events, auto-import channel, right-click import, targets/shortage boards, hidden codes, expired-section/auto-delete, hex-filtered boards, staff ticket board, ticket status DMs, auto-close tickets, audit log channel, bulk import.

## House rules

- No emojis anywhere in bot output or code.
- No `#` comments and no docstrings in code. Names carry the meaning.
- English UI text. "Rare Alloys" is always spelled out.
- Confirmations, panels, pickers and errors are ephemeral (only the clicker sees them).
- Ticket channels get exactly one bot message (bare staff-role ping + order card); status changes never post chatter.
- Every button, select and modal re-checks permissions on use.
- All buttons on public messages are persistent (`DynamicItem`), so they work after restarts.

## Architecture

```
foxbot/
  bot.py            FoxBot: services, extension loading, command sync (hash-gated), error handler, main()
  config.py         .env loading (DISCORD_TOKEN, DEV_GUILD_ID, FOXBOT_DATA_DIR, LOG_LEVEL, WAR_API_SHARD)
  constants.py      game constants (lifetimes, priorities, statuses, colours)
  db.py             aiosqlite wrapper, migrations via PRAGMA user_version, Transaction helper
  core/
    settings.py     typed per-guild settings (JSON values, cached)
    permissions.py  groups, admin check, check_group/check_admin helpers
    locations.py    War API hexes/towns, fuzzy resolve, autocomplete choices
    locpicker.py    cascading hex -> town picker (ephemeral) used by every location-based add flow
    boards.py       live board framework (render -> embeds + select + buttons; debounced refresh; DynamicItems)
    alerts.py       timed reminder framework (thresholds, public posts, private DMs, subscribers)
    entries.py      shared entry helpers (access lists, alert roles, screenshots, subscribers, grouping)
    ui.py           reply/edit/deny, BaseView/BaseModal, PickerView, EmbedPaginator, ConfirmView, field builders
    text.py         clipping, chunking, board embed fitting
    timeutil.py, ids.py
  features/         one module (or package) per feature, each with: repository functions, render functions,
                    views/modals, BoardProvider, AlertProvider, Cog, setup()
  resources/        locations.json (War API seed), items.json (item catalog), THIRD_PARTY.md
tests/              pytest + pytest-asyncio, in-memory SQLite, fake discord objects
```

Services hang off the bot: `bot.db`, `bot.settings`, `bot.perms`, `bot.locations`, `bot.boards`, `bot.alerts`, `bot.catalog`.

## Permissions

Groups: `stockpile`, `ship`, `msupps`, `orders`, `rares`, `tickets` (ticket staff), `inventory`.
Admin = server owner, Discord Administrator permission, or the configured admin role. Admins pass every check.
Granting `@everyone` to a group opens it to all members. Configured in the `/permissions` panel with role pickers.
Moderation commands use native Discord permissions (Manage Messages / Manage Roles).

## Locations (all location-based features)

- Board "Add" buttons open an ephemeral cascade: Step 1 hex select(s) - hexes already used by this server first, then "Other hexes A-M" and "Other hexes N-Z"; Step 2 town select filtered to the hex, major towns first with a "More places..." option that switches to minor locations (paged); Step 3 the feature's modal (name, code, ...).
- Slash commands take `hex` and `town` options with autocomplete; `town` is filtered by the `hex` already typed (interaction.namespace). Free text is accepted and fuzzy-resolved.
- Stored as display names (`hex`, `region`).

## People pickers

Discord's native `MentionableSelect` / `UserSelect` / `RoleSelect` (search-as-you-type) everywhere people or roles are chosen.

## Faction

Per-guild setting `faction` (Warden / Colonial / unset), switched by admins each war. Used for item-name disambiguation and (phase 2) capture alerts.

## Common tracker behaviour (stockpiles, ships, msupps)

- One live board per feature, posted by an admin with `/<feature> board`. Board = grouped list (hex > town > entries), a select of the 25 most urgent entries opening a private panel, and buttons.
- Private panel per entry: actions (Refresh, Edit, Screenshot, Reminder Pings, Notify Me / Stop Notifying, Delete, plus feature extras).
- "Last refreshed by X <time>" on cards and board lines (`refreshed_by`, `refreshed_at`).
- Subscribers: "Notify me" adds the user to `entry_subscribers`; they get DM reminders for that entry in addition to the channel post.
- `/mine`: private list of everything the user created or has access to, across features, with timers.
- Reminder thresholds (hours, admin setting, default 12/6/2/1) plus one expiry notice. Crossing several thresholds at once sends one reminder. Refreshing deletes outstanding reminder messages.
- Expired entries stay (marked EXPIRED) until deleted.
- Stockpile codes are shown on the stockpile board.

## Stockpiles

Fields: name, hex, region, storage type (admin list), priority (High/Medium/Low), code, timer 50 h, private flag, access list (users + roles), extra reminder roles, screenshot (stored in DB), refreshed_by/at, created_by/at.
Private stockpiles: never on the public board; listed via the board's "Private Stockpiles" button or `/stockpile private`; visible to creator, admins and the access list; reminders DM everyone with access plus subscribers.
Inventory: latest imported contents shown via an "Inventory" panel button.

## Ships

Fields: name, ship type (admin list), reserved squad, hex, region, parked-at, notes, squad-lock timer 48 h, screenshot, reminder roles, subscribers, refreshed_by/at.

## Msupps

Fields: base name, hex, region, msupps at measurement, consumption per hour, measured_at. Current msupps = max(0, msupps - rate * hours since measurement). Runs out at measured_at + msupps / rate hours. Actions: Add Msupps (+amount), Set Msupps (observed value), Change Rate, Edit, Delete, Notify me. Reminders use the same thresholds against the run-out time.

## Inventory (CSV)

- Input: paste the in-game "Copy to Clipboard" text into a modal (board button / `/inventory import`), or upload a .csv/.tsv/.txt file (`/inventory upload`). Supported formats: in-game clipboard export, FIR TSV, Foxhole Stockpiles app CSV/TSV.
- In-game format: header `<Hex> - <Town> - <Type> - <Name> - X: <x> Y: <y>,<YYYY.MM.DD-HH.MM.SS>`, then `<Item name>[ (Crate)],<qty>` lines, blank lines between groups. Names may use curly quotes. Zero quantities are skipped.
- Item catalog: `resources/items.json`, slimmed from xurxogr/foxhole-stockpiles `data/catalog.json` (MIT): code, display name, locale names, category, faction, quantity per crate.
- Auto-match to a tracked stockpile by hex + town + storage type + name; if none, offer to start tracking it (public/private) or keep it unlinked.
- Each import is a snapshot. Uses: `/find <item>` (autocomplete from catalog; where it is, crates/loose, data age), `/inventory totals` (by category across latest snapshots), change history (diff against the previous snapshot of the same stockpile, shown after import and in the panel), stockpile panel Inventory view, `/inventory export` (CSV file of all latest snapshots).

## Orders (replaces production requests)

- Types: Production, Transport, Scrap. An order has a title, optional destination (location picker), optional notes, and up to 20 lines (item from catalog autocomplete or free text, target amount, done amount).
- Each order is a public card message with persistent controls: a line select, quick buttons +1 / +4 / +9 / Max on the selected line, Custom amount, Edit lines, Close/Reopen, Delete. Contributions are recorded per user; the card shows progress bars and a contributor leaderboard.
- Orders board lists open orders with jump links.

## Rares (regiment stock)

Count + log; board with Add / Remove / Set / Log; optional log channel and live voice-channel name (debounced to respect Discord's 2 renames / 10 min). Ticket payments add to the stock with a log line naming the ticket (negative corrections subtract).

## Tickets

As designed with the regiment: `/ticket open` modal (regiment + optional notes, with ordering instructions as real text), private channel `ticket-<regiment>-<number>` (renamed to include the first ordered service once), one message (staff ping + card + buttons), staff buttons Set Status / Assign / Log Payment, customer buttons Order Service (service + quantity 1-25 with live cost and Confirm, stacking) / Remove Service, Close Ticket (opener or staff, confirm, transcript .txt + summary to the log channel, archive, delete channel). Prices are snapshotted per line. Commands: `/ticket overview`, `/ticket rares` (paid/owed), `/ticket leaderboard` (Rare Alloys spent), `/ticket mine` (customer history). Services managed in `/settings`.

## Admin

- `/setup` wizard: one ephemeral panel to set admin role, logistics role, alerts channel, faction, ticket staff (permissions), and post boards.
- `/settings` panel (admin role, logistics role, alert channel, thresholds, storage types, ship types, faction, ticket services, rares log/voice channels) and `/permissions` panel (role pickers per group).
- Daily SQLite backup (online backup API) to `data/backups/`, keep 14.

## Phase 2 (War API)

`/war status`, live war board, detection of a new war (warId change) with an admin one-click archive, town-captured alerts using dynamic map data and the faction setting.

## Phase 3

Logi run requests (create, claim, deliver, pings), facility queue (simple shared list).
