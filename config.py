"""Single configuration source; importing it never starts the bot."""

import os

from dotenv import load_dotenv

if os.getenv("DOWNLOAD_WORKER") != "1":
    load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
DB_FILE = os.getenv("DB_FILE", "bot.db")
FREE_CHANNEL_LIMIT = 10
PREMIUM_CHANNEL_LIMIT = 20
TRIAL_DAYS = 0
PREMIUM_DAILY_ACTIONS = 3
STARS_7_DAYS = int(os.getenv("STARS_7_DAYS", "100"))
STARS_30_DAYS = int(os.getenv("STARS_30_DAYS", "300"))


def validate_config():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env")
    if ADMIN_ID <= 0:
        raise RuntimeError("Set ADMIN_ID in .env")
    if not all(1 <= price <= 100000 for price in (STARS_7_DAYS, STARS_30_DAYS)):
        raise RuntimeError("Stars prices must be between 1 and 100000.")
