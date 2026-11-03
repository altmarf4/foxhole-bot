import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        f"This bot needs Python 3.11 or newer, but it was started with Python {sys.version.split()[0]}. "
        "Install Python 3.12 or 3.13, delete the .venv folder and start the bot again."
    )

from foxbot.bot import main

main()
