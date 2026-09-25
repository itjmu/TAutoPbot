import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import manage


class MaintenanceHealthTests(unittest.TestCase):
    def test_live_backup_includes_wal_and_health_detects_stale_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(
                    "CREATE TABLE runtime_lock(id INTEGER PRIMARY KEY, heartbeat TEXT)"
                )
                writer.execute(
                    "INSERT INTO runtime_lock VALUES(1,?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                writer.commit()
                manage.health(path)
                saved = manage.backup(path)
                with closing(sqlite3.connect(saved)) as snapshot:
                    self.assertEqual(
                        snapshot.execute(
                            "SELECT COUNT(*) FROM runtime_lock"
                        ).fetchone()[0],
                        1,
                    )
                writer.execute(
                    "UPDATE runtime_lock SET heartbeat=?",
                    ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),),
                )
                writer.commit()
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    manage.health(path)
                writer.execute("DELETE FROM runtime_lock")
                writer.commit()
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    manage.health(path)
