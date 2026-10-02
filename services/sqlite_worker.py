"""Single SQLite writer, batching durable commits outside the asyncio thread."""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from threading import RLock


class SQLiteWorker:
    def __init__(self, path, write_gate=None):
        self.path = path
        self.connection = None
        self.write_gate = write_gate if write_gate is not None else RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self.queue = asyncio.Queue(maxsize=512)
        self.task = asyncio.create_task(self.run())

    def batch(self, jobs):
        readonly = jobs[0][2]
        with nullcontext() if readonly else self.write_gate:
            return self._batch(jobs, readonly)

    def _batch(self, jobs, readonly):
        if self.connection is None:
            self.connection = sqlite3.connect(self.path, timeout=0.05)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
        conn = self.connection
        results = []
        try:
            conn.execute(f"PRAGMA query_only={'ON' if readonly else 'OFF'}")
            # Deferred reads never reserve SQLite's sole writer.
            conn.execute("BEGIN")
            for callback, _, _ in jobs:
                conn.execute("SAVEPOINT item")
                try:
                    result = callback(conn)
                    conn.execute("RELEASE item")
                    results.append((result, None))
                except Exception as exc:
                    if isinstance(exc, sqlite3.OperationalError) and (
                        "locked" in str(exc).lower() or "busy" in str(exc).lower()
                    ):
                        raise  # retry the entire batch only after a full rollback
                    conn.execute("ROLLBACK TO item")
                    conn.execute("RELEASE item")
                    results.append((None, exc))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            results = [(None, exc)] * len(jobs)
        finally:
            conn.execute("PRAGMA query_only=OFF")
        return results

    async def run(self):
        loop = asyncio.get_running_loop()
        next_job = None
        while True:
            job = next_job if next_job is not None else await self.queue.get()
            next_job = None
            if job is None:
                self.queue.task_done()
                break
            await asyncio.sleep(
                0
            )  # collect the current burst before one durable commit
            jobs = [job]
            while len(jobs) < 16 and not self.queue.empty():
                candidate = self.queue.get_nowait()
                if candidate is None or candidate[2] != job[2]:
                    next_job = candidate
                    break
                jobs.append(candidate)
            try:
                deadline = time.monotonic() + 30
                while True:
                    results = await loop.run_in_executor(
                        self.executor, self.batch, jobs
                    )
                    busy = any(
                        isinstance(error, sqlite3.OperationalError)
                        and (
                            "locked" in str(error).lower()
                            or "busy" in str(error).lower()
                        )
                        for _, error in results
                    )
                    if not busy or time.monotonic() >= deadline:
                        break
                    # Release the shared write gate between attempts; an external
                    # writer cannot leave event-loop calls waiting for 30 seconds.
                    await asyncio.sleep(0.05)
            except Exception as exc:
                results = [(None, exc)] * len(jobs)
            for (_, future, _), (result, error) in zip(jobs, results):
                if not future.done():
                    if error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
                self.queue.task_done()

    async def call(self, callback, *, readonly=False):
        future = asyncio.get_running_loop().create_future()
        context = copy_context()
        await self.queue.put(
            (lambda conn: context.run(callback, conn), future, readonly)
        )
        return await future

    async def close(self):
        await self.queue.join()
        await self.queue.put(None)
        await self.task
        if self.connection is not None:
            await asyncio.get_running_loop().run_in_executor(
                self.executor, self.connection.close
            )
        self.executor.shutdown(wait=True)
