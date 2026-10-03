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
    warapi.py       War API client (ETags, at least 0.5 s between requests, cached last response)
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

Groups: `stockpile`, `ship`, `msupps`, `orders`, `rares`, `tickets` (ticket staff), `inventory`, `logi`, `facility`.
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
- `/settings` panel (admin role, logistics role, alert channel, thresholds, storage types, ship types, faction, ticket services, rares log/voice channels, war alerts) and `/permissions` panel (role pickers per group).
- Daily SQLite backup (online backup API) to `data/backups/`, keep 14.

## Phase 2 (War API)

`/war status`, live war board, detection of a new war (warId change) with an admin one-click archive, town-captured alerts using dynamic map data and the faction setting.

As built:

- Commands (group `/war`, server only, replies only the user sees): `/war status` (the board embed), `/war hex <hex>` (hex autocomplete; who holds each town), `/war captures` (every town change this war, newest first, paged), `/war board` (admin, posts the live board), `/war archive` (admin, see Archive).
- War board: kind `war`, title "War". War number, day, status, victory towns per side out of the number needed, casualties, enlistments, the last 10 town changes and the data age. Buttons Recent Captures and Hex Status, no select.
- Polling every 5 minutes from the shard in `WAR_API_SHARD`: the war state, then each map's dynamic data (ETags). The map list refreshes every 6 hours or on a new war; `HomeRegionC` and `HomeRegionW` are skipped (no map data). War reports (casualties, enlistments) refresh every 30 minutes; totals are kept per map, so a map that fails keeps its last values.
- Towns are town bases, relic bases and keeps, named after the nearest Major map label, then Minor, then any label. A town whose name cannot be loaded shows as "Unknown town" until a later poll fills it in.
- Baseline: the first poll after a start or after a new war records each map silently, so changes made while the bot was offline never alert. No capture is recorded before the war's conquest start time.
- Data age: "War API data updated" only advances when at least one map loaded; the board warns after 20 minutes without fresh data.
- Victory towns: flag 0x01 is a victory town, 0x10 scorched. Each scorched victory town lowers the number needed by one; the board shows both numbers.
- Storage: `war_towns` (current owner per town), `war_events` (changes per war), `meta` key `war_state` (JSON), `war_archives`.
- Alerts (`/settings` > War): Off (default), Our faction only, All changes. Our faction only uses the faction setting, adds "we lost it" / "we took it" and colours the message by loss or gain. All changes from one poll are one message.
- New war notice (with the persistent Archive Previous War button) and war over notice are posted in every server. Alerts and notices go to the war alert channel, else the alerts channel, else the war board's channel, and never ping anyone. Nothing is announced on the first poll after a start.
- Archive (new-war cleanup), admin only, after a confirmation that shows the counts:
  - First a backup of the whole database to `data/backups/war-<number>-<server id>-<UTC time>.db` (`-2`, `-3` for repeats in the same second). The daily 14-copy prune does not touch these. If the backup fails, nothing is deleted.
  - Then, for that server only and in one transaction: stockpiles, ships, msupps bases, inventory snapshots and their items, orders with lines and contributions, logi runs and the facility queue, plus their per-entry rows (access lists, subscribers, reminder roles, screenshots, alert state, alert messages). Reminder messages, order cards and logi ping messages are deleted from Discord where possible.
  - Kept: settings, permissions, boards, Rare Alloys, tickets and war history. A `war_archives` row records who, when and the counts, every board refreshes, and one server cannot run two archives at once.
  - War number: the notice button (`fw:archive:<number>`) carries the previous war's number and is refused, pointing to `/war archive`, once a newer notice exists. `/war archive` uses the current war's number when a winner is declared, otherwise the current number minus 1.

## Phase 3

Logi run requests (create, claim, deliver, pings), facility queue (simple shared list).

As built: both use new permission groups, `logi` (Logi runs) and `facility` (Facility queue), set in `/permissions` and in the `/setup` step "Who can use the trackers". Every action on a private panel re-loads the entry, re-checks permission and only applies if the status and the claimer or worker are still what the panel showed; otherwise the panel refreshes and says what changed.

### Logi runs

- Fields: title, cargo, pickup (optional location), destination (location), priority (High/Medium/Low, default Medium), notes, status (open, claimed, delivered, cancelled), who claimed, delivered or cancelled it and when, ping message.
- Commands (group `/logi`, server only): `/logi request title cargo destination_hex [destination_town] [pickup] [priority] [notes]` (location autocomplete, the town filtered by the hex; unknown places are saved as typed with a note), `/logi list`, `/logi history` (delivered or cancelled in the last 7 days, paged), `/logi board` (admin).
- Board: kind `logi`, title "Logi Runs". Open runs grouped by priority, oldest first, then claimed runs with who and since when. Select of 25 (open before claimed, by priority then age). Buttons Request Run (location picker for the destination, then a form with title, cargo, pickup, priority, notes) and History.
- Private panel: Claim, Unclaim, Mark Delivered, Edit (title, cargo, pickup, destination, notes), Cancel Run (confirm with "Cancel Run" / "Keep Run"), Refresh, and a priority select.
- Who: the `logi` group requests, views and claims; the requester and the claimer always see their own run. Unclaim: the claimer or an admin. Mark Delivered: the claimer, the requester or an admin, also straight from open. Edit, priority and cancel: the requester or an admin, while open or claimed.
- Ping: one message per request in the logi board's channel (else the alerts channel) mentioning the logistics role, with persistent Claim and Details buttons. It is removed once the run is claimed, delivered or cancelled and kept current when the run is edited. Unclaiming opens the run again without a second ping. The requester is told whether the ping went out.
- DMs: the requester gets a DM when someone else claims or delivers the run. Cancelling and unclaiming send none.
- `/mine` lists open and claimed runs the user requested or claimed.
- Schema v3 adds `cancelled_by` and `cancelled_at` to `logi_runs`.

### Facility queue

- Fields: item (catalog item or free text, up to 80 characters), quantity (1 to 100,000), facility (optional text), notes, position, status (queued, in progress, done), worker, who marked it done, requester.
- Commands (group `/facility`, server only): `/facility add item quantity [facility] [notes]` (item autocomplete from the catalog for the server's faction; facility autocomplete from recently used facilities, then map locations), `/facility list` (also opens entries done in the last 24 hours), `/facility board` (admin).
- Board: kind `facility`, title "Facility Queue". Numbered list: in progress first (worker and since when), then queued in order, then entries done in the last 24 hours on one line (newest 5 and "and N more"). Select of 25 in that order. Buttons Add (form), Done Today (paged list) and Clear Done (admin, confirmation; permanently deletes done entries).
- Private panel: Start Working, Stop Working, Mark Done, Move Up, Move Down, Move to Top, Back to Queue (undo Mark Done; the entry returns at the top of the queue), Edit, Remove (confirmation), Refresh.
- Order: `position` is gap-free over queued and in-progress entries and rewritten in the same transaction as every change; done entries get 0. Only queued entries move; in-progress entries keep their slots and Stop Working keeps the entry's place.
- Who: any `facility` member starts work and moves entries. Stop Working: the worker or an admin. Mark Done: anyone on a queued entry; on an in-progress entry the worker, the requester or an admin. Edit, Remove and Back to Queue: the requester, the worker, whoever marked it done, or an admin.
- Item matching: item code, exact name, name without "(Crate)", then a cautious whole-word guess (also with a trailing "s" removed). Guesses and unmatched text are reported to the user.
- No pings or DMs, and no `/mine` section yet.
