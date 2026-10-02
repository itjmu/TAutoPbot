"""Track in-flight updates/jobs so shutdown drains them before closing resources."""

import asyncio
import json
import time
from collections import deque
from functools import wraps

from aiogram import BaseMiddleware

from app import database, timeutils
from services.telegram_rate import background_traffic


class RuntimeTasks(BaseMiddleware):
    def __init__(self):
        self.tasks = set()
        self.update_durations = deque(maxlen=1024)
        self.job_durations = {}
        self.last_health = None
        self.limiter = None

    async def __call__(self, handler, event, data):
        task = asyncio.current_task()
        started = time.monotonic()
        self.tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self.update_durations.append(time.monotonic() - started)
            self.tasks.discard(task)

    def job(self, function):
        @wraps(function)
        async def run(*args, **kwargs):
            task = asyncio.current_task()
            self.tasks.add(task)
            token = background_traffic.set(True)
            started = time.monotonic()
            try:
                return await function(*args, **kwargs)
            finally:
                self.job_durations[function.__name__] = round(
                    time.monotonic() - started, 3
                )
                background_traffic.reset(token)
                self.tasks.discard(task)

        return run

    async def health(self, heartbeat):
        from services import contests
        from services.jobs import download_jobs

        now = time.monotonic()
        period_lag = max(0, now - self.last_health - 20) if self.last_health else 0
        self.last_health = now
        await heartbeat()
        started = time.monotonic()
        await asyncio.sleep(0.05)
        samples = sorted(self.update_durations)
        metrics = {
            "timestamp": timeutils.iso(),
            "inflight_updates_jobs": len(self.tasks),
            "sqlite_queue": database._worker.queue.qsize() if database._worker else 0,
            "telegram_queue": len(self.limiter.pending) if self.limiter else 0,
            "downloads_active": len(download_jobs.tasks),
            "contests_closing": len(contests._closing_tasks),
            "handler_p95_ms": round(
                samples[min(len(samples) - 1, int(len(samples) * 0.95))] * 1000, 2
            )
            if samples
            else None,
            "loop_lag_ms": round(max(0, time.monotonic() - started - 0.05) * 1000, 2),
            "heartbeat_period_lag_seconds": round(period_lag, 3),
            "job_last_seconds": self.job_durations.copy(),
        }
        await database.async_call(
            lambda conn: (
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings(key,value) VALUES('runtime_metrics',?)",
                    (json.dumps(metrics),),
                ).rowcount
            )
        )

    async def close(self):
        tasks = set(self.tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=25)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
