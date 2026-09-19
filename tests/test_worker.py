"""Subprocess smoke tests with no network or production database access."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class WorkerTests(unittest.TestCase):
    def test_os_lock_releases_immediately_after_process_exit(self):
        from app.scheduler import RuntimeLock

        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "bot.db")
            first = RuntimeLock(path)
            second = RuntimeLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()
            code = "from app.scheduler import RuntimeLock; import os,sys; lock=RuntimeLock(sys.argv[1]); assert lock.acquire(); os._exit(0)"
            result = subprocess.run(
                [sys.executable, "-c", code, path], capture_output=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(first.acquire())
            first.release()

    def test_worker_rejects_invalid_url_without_opening_database(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "must-not-exist.db"
            env = dict(
                os.environ,
                DB_FILE=str(db_path),
                BOT_TOKEN="",
                ADMIN_ID="0",
                PYTHONIOENCODING="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-m", "app.download_worker"],
                input=json.dumps(
                    {"action": "inspect", "url": "file:///private", "folder": folder}
                ),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
                env=env,
                cwd=Path(__file__).resolve().parents[1],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["ok"])
            self.assertFalse(db_path.exists())

    def test_importing_application_does_not_open_database(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "must-not-exist.db"
            env = dict(
                os.environ,
                DB_FILE=str(db_path),
                BOT_TOKEN="",
                ADMIN_ID="0",
                PYTHONIOENCODING="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from app.application import main; from app.database import db; assert db is None",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
                env=env,
                cwd=Path(__file__).resolve().parents[1],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(db_path.exists())
