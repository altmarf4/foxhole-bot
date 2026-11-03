# Foxhole Logistics Bot 2.0 - Specification

The single source of truth for the rebuild. Decisions here were made with the regiment's lead on 2026-10-02/03.

## Delivery phases

| Phase | Scope | Gate |
|---|---|---|
| 1 | Rebuild of every existing feature with the new UX, CSV inventory, orders (replacing production requests), ticket order history, setup wizard, daily backups | Live test on the test bot |
| 2 | War API: `/war status`, live war board, new-war cleanup, town-captured alerts | Live test |
| 3 | Logi run requests, facility queue (simple list) | Removed again on 2026-11-01 at the lead's request |

Dropped on purpose: in-game name registration, operations/events, auto-import channel, right-click import, targets/shortage boards, hidden codes, expired-section/auto-delete, hex-filtered boards, staff ticket board, ticket status DMs, auto-close tickets, audit log channel, bulk import.

## House rules

- No emojis anywhere in bot output or code.
- No `#` comments and no docstrings in code. Names carry the meaning.
- English UI text. The regiment's currency is called "Rares" everywhere (changed from "Rare Alloys" on 2026-11-01). The in-game item keeps its real name "Rare Alloys" in the item catalog so imports still match.
- Confirmations, panels, pickers and errors are ephemeral (only the clicker sees them).
- Ticket channels get exactly one bot message (the ticket ping, if any, + order card); status changes never post chatter.
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
    alerts.py       timed reminder framework (thresholds, DMs to owners/subscribers/roles, channel fallback)
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
Granting `@everyone` to a group opens it to all members. Configured in `/settings` under Permissions with role pickers.
Moderation commands use native Discord permissions (Manage Messages / Manage Roles).

## Locations (all location-based features)

- Board "Add" buttons open an ephemeral cascade: Step 1 one hex select with letter buttons under it (A-H, K-R, S-W; groups never split a first letter and hold at most 25) that switch which hexes the select shows, plus "Type it instead"; Step 2 town select filtered to the hex, major towns first with a "More places..." option that switches to minor locations (paged); Step 3 the feature's modal (name, code, ...).
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
- Subscribers: "Notify me" adds the user to `entry_subscribers`; they get DM reminders for that entry.
- Reminders go by DM (agreed with the regiment on 2026-10-03, channel posts were annoying): to whoever added the entry, whoever last refreshed or measured it, subscribers, members of the entry's reminder roles, and for private stockpiles everyone with access. Each recipient must still be allowed to see the entry. Only when nobody could be DMed is the reminder posted in the alerts channel (or the board's channel) with the logistics and reminder-role pings.
- `/mine`: private list of everything the user created or has access to, across features, with timers.
- Reminder thresholds (hours, admin setting, default 12/6/2/1) plus one expiry notice. Crossing several thresholds at once sends one reminder. Refreshing deletes outstanding reminder messages.
- Expired entries stay (marked EXPIRED) until deleted.
- Stockpile codes are shown on the stockpile board.

## Stockpiles

Fields: name, hex, region, storage type (admin list), priority (High/Medium/Low), code, timer (50 h by default, `/settings` > Timers), private flag, access list (users + roles), extra reminder roles, screenshot (stored in DB), refreshed_by/at, created_by/at.
Private stockpiles: never on the public board; listed via the board's "Private Stockpiles" button or `/stockpile private`; visible to creator, admins and the access list; reminders DM everyone with access plus subscribers.
Inventory: latest imported contents shown via an "Inventory" panel button.

## Ships

Fields: name, ship type (admin list), reserved squad, hex, region, parked-at, notes, squad-lock timer (48 h by default, `/settings` > Timers), screenshot, reminder roles, subscribers, refreshed_by/at.

## Msupps

Fields: base name, hex, region, msupps at measurement, consumption per hour, measured_at. Current msupps = max(0, msupps - rate * hours since measurement). Runs out at measured_at + msupps / rate hours. Actions: Add Msupps (+amount), Set Msupps (observed value), Change Rate, Edit, Delete, Notify me. Reminders use the same thresholds against the run-out time.

## Inventory (CSV)

- Input: paste the in-game "Copy to Clipboard" text into a modal (board button / `/inventory import`), or upload a .csv/.tsv/.txt file (`/inventory upload`). Supported formats: in-game clipboard export, FIR TSV, Foxhole Stockpiles app CSV/TSV.
- In-game format: header `<Hex> - <Town> - <Type> - <Name> - X: <x> Y: <y>,<YYYY.MM.DD-HH.MM.SS>`, then `<Item name>[ (Crate)],<qty>` lines, blank lines between groups. Names may use curly quotes. Zero quantities are skipped.
- Tabs: the Inventory view has three tabs: Inventory (icon grid with green/red change badges and greyed icons for items that ran out, default), Changes (each changed item as before -> after, e.g. `Tripod (crates): 38 -> 30`, split into parts when long) and Map. Without Pillow the Inventory tab shows a text table. A single-stockpile import opens this same view; importing from an open Inventory view updates it in place. Imports with several stockpiles (FIR / app files) get a summary view with the same tabs.
- History: the last 50 imports per stockpile are kept. On every tab: a dropdown of past imports (25 at a time around the shown one) starting at the newest, and Back (newer) / Page x/y / Next (older) buttons. Changes compares the shown import with the one before it.
- Map: the official hex map with a dot at the stockpile, using the export's X/Y (exact) or, without it, the town's War API position (approximate, orange ring); the hex map labels the major towns. A Hex Map / World Map switch shows the whole world (all 53 hexes, labelled) with the hex outlined. Maps are built by `tools/build_maps.py`.
- Totals: `/inventory totals [category]` uses the same look with four tabs: Inventory (icon grid of every stockpile combined), Changes (each stockpile's latest import compared with the one before it, combined), Stockpiles (every stockpile you can see, including ones without imports, with when it was last updated; over 24 hours old and never imported are marked; a dropdown opens its inventory) and Map (world map with numbered markers for every stockpile and a legend).
- Find: `/find` shows a card per stockpile holding the item (item icon, amount, stockpile name, town) with an age badge coloured by freshness (green under 6 hours, white under 24, amber under 3 days, red older; old cards are outlined), the text list with "updated X ago" and an "out of date" mark after 24 hours, a Map tab with every stockpile holding it, and a dropdown to open a stockpile's inventory. Typed text that matches several items lists all of them.
- Stockpile from CSV: `/stockpile add-csv` and the stockpile board's **Add from CSV** button open a form to paste the in-game export plus the access code, priority and visibility. Name, hex, town and storage type come from the export's first line and the contents are imported straight away. If the stockpile is already tracked, only its contents are updated.
- Display: contents, import results and totals include an icon grid styled like the in-game stockpile window (categories in game order, crated icons with the crate marker, vehicles included, quantity under each icon, green/red change badges since the previous import, greyed tiles for items that are gone, a labelled placeholder for unknown items). Icons come from FIR (`resources/icons`, rebuilt with `tools/build_item_icons.py`); rendering uses Pillow and falls back to text if Pillow is missing. `/find` shows the item's icon. Vehicles that the game does not allow in stockpiles (battle tanks, super tanks, trains, large ships) never appear in exports.
- Item catalog: `resources/items.json`, slimmed from xurxogr/foxhole-stockpiles `data/catalog.json` (MIT): code, display name, locale names, category, faction, quantity per crate.
- Auto-match to a tracked stockpile by hex + town + storage type + name; if none, offer to start tracking it (public/private) or keep it unlinked.
- Each import is a snapshot. Uses: `/find <item>` (autocomplete from catalog; where it is, crates/loose, data age), `/inventory totals` (by category across latest snapshots), change history (diff against the previous snapshot of the same stockpile, shown after import and in the panel), stockpile panel Inventory view, `/inventory export` (CSV file of all latest snapshots).

## Orders (replaces production requests)

- Types: Production, Transport, Scrap. An order has a title, optional destination (location picker), optional notes, and up to 20 lines (target amount, done amount).
- Lines (changed 2026-11-01): each line must start with the amount and is shown exactly as typed, with no item catalog matching. The leading number is the progress bar target; a `k` right after it multiplies by 1000 (`50k Scrap` -> 50,000, `1.5k` -> 1,500). Any other suffix is just text (`20c Mortar Shell` -> 20). Editing lines keeps the delivered amount of a line whose text after the amount is unchanged. Older lines saved before this show as `<target> <item>` in the edit form.
- Each order is a public card message with persistent controls: a line select, quick buttons +1 / +4 / +9 / Max on the selected line, Custom amount, Edit lines, Close/Reopen, Delete. Contributions are recorded per user; the card shows progress bars and a contributor leaderboard.
- Orders board lists open orders with jump links.

## Rares (regiment stock)

Count + log; board with Add / Remove / Set / Log; optional log channel and live voice-channel name (debounced to respect Discord's 2 renames / 10 min). Ticket payments add to the stock with a log line naming the ticket (negative corrections subtract).

## Tickets

As designed with the regiment: `/ticket open` modal (regiment + optional notes, with ordering instructions as real text), private channel `ticket-<regiment>-<number>` (renamed to include the first ordered service once), one message (staff ping + card + buttons), staff buttons Set Status / Assign / Log Payment, customer buttons Order Service (service + quantity 1-25 with live cost and Confirm, stacking) / Remove Service, Close Ticket (opener or staff, confirm, transcript .txt + summary to the log channel, archive, delete channel). Prices are snapshotted per line. Commands: `/ticket overview`, `/ticket rares` (paid/owed), `/ticket leaderboard` (Rares spent), `/ticket mine` (customer history). Services managed in `/settings`. Ticket board (`/ticket board` or `/setup`, kind `tickets`): the price list of all services and an **Open Ticket** button that opens the same form as `/ticket open`; it refreshes when services change.
- Ticket ping (changed 2026-11-01): the ticket staff role is no longer pinged automatically. Admins choose who gets pinged for new tickets (roles and/or members, up to 25, `ticket_pings` setting) in `/setup` (step "Ticket staff and pings") and in `/settings` under Roles. The default is nobody. Ping targets are mentioned in the ticket's one message and get view and send access to new ticket channels so the ping reaches them. @everyone is never pinged.

## Admin

- `/setup` wizard: one ephemeral panel to set admin role, logistics role, alerts channel, faction, ticket staff (permissions), and post boards.
- `/settings` is the one admin menu (merged 2026-11-01, rebuilt the same day; `/permissions` was removed). A section select at the top, the footer says "Section x of 11". Dropdowns save as soon as something is picked; removing every value from a dropdown clears that setting. Sections:
  - Overview: roles, channels, faction, war alerts and timers at a glance, plus a "Still to do" list (no admin role, no faction, no alerts channel, groups only admins can use, no ticket services, boards not posted).
  - Roles: admin role, logistics ping role, ticket staff roles (role dropdowns; @everyone is refused for the first two), and who gets pinged for new tickets.
  - Channels: alerts, orders and war alert channels, with a warning when the bot cannot post there.
  - Permissions: a dropdown per group opening the role form, plus Same Roles for All Trackers (replaces the roles of every tracker group).
  - Boards: board and channel dropdowns, Post Board, Remove Board (deletes the message, can be posted again) and Refresh All Boards.
  - Faction & War: Warden / Colonial / Not set buttons and the town change alert mode.
  - Timers: stockpile reserve timer (default 50 h) and ship squad-lock timer (default 48 h), both 1 to 500 hours and applied the next time a timer starts or is refreshed, plus the reminder thresholds; Restore Defaults.
  - Stockpiles & Ships: storage types and ship types (remove dropdowns, Add and Reset buttons).
  - Tickets: services and prices, the ticket category and the ticket log channel (picked from dropdowns; cleared ones are created again automatically).
  - Rares: log channel, voice channel and the status levels (Low up to 50 and Moderate up to 200 by default; Low must be at least 1 and below Moderate).
  - Data & Backups: Back Up Now, the latest backup files and the last archive, and Archive Previous War (the same confirmation as `/war archive`).
- `/setup` stays as the guided first-run wizard and shares the role, channel, faction and board controls with `/settings`.
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
  - Then, for that server only and in one transaction: stockpiles, ships, msupps bases, inventory snapshots and their items, orders with lines and contributions, plus their per-entry rows (access lists, subscribers, reminder roles, screenshots, alert state, alert messages). Reminder messages and order cards are deleted from Discord where possible.
  - Kept: settings, permissions, boards, Rares, tickets and war history. A `war_archives` row records who, when and the counts, every board refreshes, and one server cannot run two archives at once.
  - War number: the notice button (`fw:archive:<number>`) carries the previous war's number and is refused, pointing to `/war archive`, once a newer notice exists. `/war archive` uses the current war's number when a winner is declared, otherwise the current number minus 1.

## Phase 3 (removed)

Logi run requests and the facility queue were built and then removed on 2026-11-01 at the lead's request. Schema v5 drops `logi_runs` and `facility_queue`, their board rows, permission rows and per-entry rows, and queues their posted boards, logi pings and reminder messages in `retired_messages`; the bot deletes those Discord messages once after the next start. The data stays in the backups from before the upgrade.
