"""Local synthetic SQLite/FSM load only; never uses the working database or Telegram."""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["DB_FILE"] = ":memory:"
os.environ["BOT_TOKEN"] = ""
os.environ["ADMIN_ID"] = "12345"

from aiogram.fsm.storage.base import StorageKey
from app import accounts, database as db
from app.storage import SQLiteStorage


async def main():
    with tempfile.TemporaryDirectory(prefix="tauto-audit-") as folder:
        path = str(Path(folder) / "load.db")
        db.connect(path)
        db.init_db()
        store = SQLiteStorage()
        stamps = []
        stop = False

        async def heartbeat():
            while not stop:
                stamps.append(time.perf_counter())
                await asyncio.sleep(0.01)

        async def user(uid):
            accounts.ensure_user(SimpleNamespace(id=uid, username=None, first_name="Synthetic", last_name=None))
            key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
            await store.set_state(key, "audit")
            await store.set_data(key, {"text": "x" * 300, "step": "review"})

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.02)
        started = time.perf_counter()
        await asyncio.gather(*(user(uid) for uid in range(1, 2001)))
        elapsed = time.perf_counter() - started
        await asyncio.sleep(0.03)
        stop = True
        await beat
        result = {"kind": "local synthetic DB/FSM burst; no network", "users": 2000,
                  "seconds": round(elapsed, 3), "users_per_second": round(2000 / elapsed, 1),
                  "heartbeat_max_gap_seconds": round(max(b-a for a,b in zip(stamps,stamps[1:])),3),
                  "saved_users": db.one("SELECT COUNT(*) FROM users")[0],
                  "saved_fsm_rows": db.one("SELECT COUNT(*) FROM fsm_state")[0]}
        other = sqlite3.connect(path)
        other.execute("BEGIN IMMEDIATE")
        db.db.execute("PRAGMA busy_timeout=250")
        started = time.perf_counter()
        try:
            db.execute("UPDATE users SET first_name='blocked' WHERE telegram_id=1")
        except sqlite3.OperationalError:
            result["synchronous_lock_block_seconds_at_250ms_timeout"] = round(time.perf_counter()-started,3)
        finally:
            other.rollback()
            other.close()
            db.db.close()
            db.db=None
        Path("audit_load.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        print(json.dumps(result,indent=2))


if __name__ == "__main__":
    asyncio.run(main())
