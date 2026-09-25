"""Real file-backed SQLite durability, isolation and event-loop responsiveness."""

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.sqlite_worker import SQLiteWorker


class SQLiteWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_batches_and_failed_callback_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "test.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
            conn.commit()
            worker = SQLiteWorker(path)
            try:

                def broken(db):
                    db.execute("INSERT INTO items VALUES(999,'must rollback')")
                    raise ValueError("test failure")

                results = await asyncio.gather(
                    *(
                        worker.call(
                            lambda db, i=i: (
                                db.execute(
                                    "INSERT INTO items VALUES(?,?)", (i, str(i))
                                ).rowcount
                            )
                        )
                        for i in range(200)
                    ),
                    worker.call(broken),
                    return_exceptions=True,
                )
                self.assertIsInstance(results[-1], ValueError)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 200
                )
                self.assertIsNone(
                    conn.execute("SELECT * FROM items WHERE id=999").fetchone()
                )
            finally:
                await worker.close()
                conn.close()
            with closing(sqlite3.connect(path)) as reopened:
                self.assertEqual(
                    reopened.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    reopened.execute("SELECT COUNT(*) FROM items").fetchone()[0], 200
                )

    async def test_external_lock_does_not_block_event_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "test.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE items(id INTEGER)")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            worker = SQLiteWorker(path)
            try:
                task = asyncio.create_task(
                    worker.call(
                        lambda db: db.execute("INSERT INTO items VALUES(1)").rowcount
                    )
                )
                await asyncio.sleep(0.1)
                self.assertFalse(task.done())
                conn.rollback()  # this line is reached while the worker waits on SQLite
                self.assertEqual(await asyncio.wait_for(task, 3), 1)
            finally:
                conn.rollback()
                await worker.close()
                conn.close()
