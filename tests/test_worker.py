"""Subprocess smoke tests with no network or production database access."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class WorkerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Job Object lifecycle")
    def test_worker_exit_terminates_descendant_process(self):
        import ctypes
        from ctypes import wintypes

        code = "from services.worker_limits import apply_worker_limits; import subprocess,sys; apply_worker_limits(); child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW); print(child.pid,flush=True)"
        parent = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(parent.returncode, 0, parent.stderr)
        pid = int(parent.stdout.strip())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                kernel.CloseHandle(handle)

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
