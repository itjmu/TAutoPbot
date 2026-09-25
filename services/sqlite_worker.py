"""Single SQLite writer, batching durable commits outside the asyncio thread."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor


class SQLiteWorker:
    def __init__(self, path):
        self.path = path
        self.connection = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self.queue = asyncio.Queue(maxsize=512)
        self.task = asyncio.create_task(self.run())

    def batch(self, jobs):
        if self.connection is None:
            self.connection = sqlite3.connect(self.path, timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
        conn = self.connection
        results = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            for callback, _ in jobs:
                conn.execute("SAVEPOINT item")
                try:
                    result = callback(conn)
                    conn.execute("RELEASE item")
                    results.append((result, None))
                except Exception as exc:
                    conn.execute("ROLLBACK TO item")
                    conn.execute("RELEASE item")
                    results.append((None, exc))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            results = [(None, exc)] * len(jobs)
        return results

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            job = await self.queue.get()
            if job is None:
                self.queue.task_done()
                break
            await asyncio.sleep(
                0
            )  # collect the current burst before one durable commit
            jobs = [job]
            while len(jobs) < 100 and not self.queue.empty():
                jobs.append(self.queue.get_nowait())
            try:
                results = await loop.run_in_executor(self.executor, self.batch, jobs)
            except Exception as exc:
                results = [(None, exc)] * len(jobs)
            for (_, future), (result, error) in zip(jobs, results):
                if not future.done():
                    if error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
                self.queue.task_done()

    async def call(self, callback):
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((callback, future))
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
