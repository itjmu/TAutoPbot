"""Synthetic queue and durable SQLite measurements, with no Telegram requests."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.methods import GetChatMember

from services.jobs import BackgroundJobs
from services.sqlite_worker import SQLiteWorker
from services.telegram_rate import TelegramRateLimit, background_traffic


async def priority(legacy):
    limiter = TelegramRateLimit()
    limiter.next_request = time.monotonic() + 0.03
    jobs = BackgroundJobs()

    async def request(bot, method):
        return True

    async def work():
        token = background_traffic.set(False) if legacy else None
        try:
            await asyncio.gather(
                *(
                    limiter(request, None, GetChatMember(chat_id=i, user_id=1))
                    for i in range(12)
                )
            )
        finally:
            if token is not None:
                background_traffic.reset(token)

    task = jobs.start(1, work, "load probe")
    await asyncio.sleep(0.01)
    started = time.monotonic()
    await limiter(request, None, GetChatMember(chat_id=100, user_id=1))
    elapsed = time.monotonic() - started
    await task
    await jobs.close()
    return round(elapsed * 1000, 2)


async def main():
    result = {"mode": "synthetic API queue and real temporary SQLite; Windows"}
    result["menu_wait_legacy_ms"] = await priority(True)
    result["menu_wait_fixed_ms"] = await priority(False)
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "load.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY)")
        conn.commit()
        worker = SQLiteWorker(path)
        lag = []
        finished = asyncio.Event()

        async def heartbeat():
            while not finished.is_set():
                started = time.monotonic()
                await asyncio.sleep(0.01)
                lag.append(max(0, time.monotonic() - started - 0.01))

        pulse = asyncio.create_task(heartbeat())
        started = time.monotonic()
        try:
            await asyncio.gather(
                *(
                    worker.call(
                        lambda db, i=i: (
                            db.execute("INSERT INTO items VALUES(?)", (i,)).rowcount
                        )
                    )
                    for i in range(1000)
                )
            )
            result["sqlite_1000_writes_seconds"] = round(time.monotonic() - started, 3)
            result["sqlite_rows"] = conn.execute(
                "SELECT COUNT(*) FROM items"
            ).fetchone()[0]
            result["sqlite_integrity"] = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        finally:
            finished.set()
            await pulse
            await worker.close()
            conn.close()
        result["loop_max_lag_ms"] = round(max(lag, default=0) * 1000, 2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
