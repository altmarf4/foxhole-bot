# Foxhole Logistics Bot 2.0

A Discord bot for a Foxhole regiment: reserved stockpiles (public and private) with expiry reminders, large-ship squad locks, base maintenance supplies, stockpile inventory from the in-game "Copy to Clipboard" export, orders (production / transport / scrap), the regiment's Rare Alloys stock, a customer ticket system priced in Rare Alloys, logi run requests, a shared facility queue, a live war board with town capture alerts, and moderation helpers.

Everything the regiment decided about how it should work is in [SPEC.md](SPEC.md).

## 1. Create the bot in Discord

1. Open https://discord.com/developers/applications and create an application (make a separate one for testing).
2. **Bot** tab: press **Reset Token** and copy the token. Never paste it into a chat, a screenshot or a file you share.
3. On the same tab enable **SERVER MEMBERS INTENT** and **MESSAGE CONTENT INTENT** (needed for role reminders, private-stockpile DMs and ticket transcripts).
4. **OAuth2 > URL Generator**: scopes `bot` and `applications.commands`. Or use this link with your application ID:

   `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=275146599440`

   That permission set is: View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files, Read Message History, Manage Messages (for /purge), Manage Channels (ticket channels), Manage Roles (ticket channel access and /role), Mention All Roles (reminder pings for roles that are not mentionable).
5. In your server, drag the bot's role above any role it should hand out with `/role`.

## 2. Configure

Copy `.env.example` to `.env` and fill it in:

| Key | Meaning |
|---|---|
| `DISCORD_TOKEN` | The bot token (required). |
| `DEV_GUILD_ID` | Optional. Server ID for testing: slash commands update instantly in that server only. Leave empty in production. |
| `WAR_API_SHARD` | `able` (default), `baker` or `charlie`. The official War API shard used for hex and town names, the war board and town capture alerts. |
| `LOG_LEVEL` | `INFO` by default. |
| `FOXBOT_DATA_DIR` | Optional. Where the database, logs and backups live (default `data/` next to this file). |

## 3. Run

Windows: double-click `run.bat` (first run creates `.venv` and installs everything; needs Python 3.11+ from python.org, 3.13 recommended).

Linux / macOS: `./run.sh`

### Running 24/7 on Linux (e.g. a Proxmox container)

```bash
sudo useradd --system --home /opt/foxhole_bot foxbot
sudo cp -r foxhole_bot /opt/foxhole_bot
sudo chown -R foxbot:foxbot /opt/foxhole_bot
sudo -u foxbot sh -c 'cd /opt/foxhole_bot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
sudo cp /opt/foxhole_bot/deploy/foxbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foxbot
journalctl -u foxbot -f
```

## 4. First-time setup in Discord

Run `/setup` as the server owner or an Administrator. It walks through the admin role, the logistics ping role, the alerts channel, the faction, ticket staff, who may use each feature (including logi runs and the facility queue), and posting the live boards. Everything can be changed later with `/settings` and `/permissions`.

Town capture alerts are off until an admin turns them on in `/settings` under War (our faction only, or all changes). When a new war starts the bot posts a notice; set the faction for the new war in `/settings`, and press **Archive Previous War** (or run `/war archive`) to clear the last war's stockpiles, ships, bases, inventories, orders, logi runs and facility queue after a backup.

## Data and backups

- Database: `data/foxbot.db` (SQLite). Logs: `data/logs/`. The bot keeps one backup per day in `data/backups/` (last 14 days).
- Archiving a war first saves `data/backups/war-<number>-<server id>-<time>.db`. These copies are never deleted automatically.
- To restore, stop the bot and copy a backup over `data/foxbot.db`.

## Updating game data

- Hex and town names refresh from the War API automatically once a day. The war board and capture alerts check the War API every 5 minutes.
- After a Foxhole update adds items, run `python tools/build_item_catalog.py` and restart the bot.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Item data credits are in `foxbot/resources/THIRD_PARTY.md`.
