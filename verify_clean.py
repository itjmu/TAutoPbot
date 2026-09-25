"""Read-only check of clean DB and pending update count; no polling."""
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from aiogram import Bot

from config import BOT_TOKEN, DB_FILE


async def main():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}
        assert all(counts[name] == 0 for name in ("users", "channels", "posts", "contests", "payments", "premium", "broadcast_jobs", "broadcast_targets", "join_requests"))
    async with Bot(BOT_TOKEN) as bot:
        pending = (await bot.get_webhook_info()).pending_update_count
    result = {"table_counts": counts, "pending_at_check": pending}
    Path("clean_reset_results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
