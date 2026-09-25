import csv
import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_planning as fixtures

from app import accounts
from app import database as db
from app.features import user_admin as admin


class UserAdminTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def test_limits_enforce_zero_daily_quota_and_restore_tariff(self):
        original = accounts.channel_limit(42)
        admin.set_limit(42, "channels", 0)
        self.assertFalse(accounts.channel_allowed(42, self.cid))
        admin.set_limit(42, "channels", -1)
        self.assertTrue(accounts.channel_allowed(42, self.cid))
        admin.set_limit(42, "channels", None)
        self.assertEqual(accounts.channel_limit(42), original)
        admin.set_limit(42, "post_create", 1)
        self.assertTrue(accounts.use_daily(42, "post_create"))
        self.assertFalse(accounts.use_daily(42, "post_create"))
        admin.set_limit(42, "download_video", 0)
        self.assertFalse(accounts.use_daily(42, "download_video"))
        admin.set_limit(42, "button_colors", 0)
        self.assertEqual(accounts.button_styles(42), [None])
        with self.assertRaises(ValueError):
            admin.set_limit(42, "button_colors", -1)
        with self.assertRaises(ValueError):
            admin.set_block(admin.ADMIN_ID, True)

    def test_delete_profile_blocks_but_retains_accounting_and_other_users(self):
        admin.delete_profile(42)
        self.assertIsNone(db.one("SELECT 1 FROM users WHERE telegram_id=42"))
        self.assertTrue(accounts.blocked(42))
        self.assertIsNotNone(db.one("SELECT 1 FROM premium WHERE user_id=42"))
        self.assertIsNotNone(db.one("SELECT 1 FROM users WHERE telegram_id=43"))
        admin.set_block(42, False)
        self.assertFalse(accounts.blocked(42))

    async def test_exports_reject_nonadmin_and_group_even_with_admin_id(self):
        for uid, chat_type in ((42, "private"), (admin.ADMIN_ID, "group")):
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=uid),
                message=SimpleNamespace(chat=SimpleNamespace(id=uid, type=chat_type)),
                data="au:export:csv",
                answer=AsyncMock(),
            )
            bot = SimpleNamespace(send_document=AsyncMock())
            with self.assertRaises(ValueError):
                await admin.controls(callback, self.state, bot)
            bot.send_document.assert_not_called()

    async def test_csv_export_only_to_admin_and_preserves_unicode_escapes_formulas(
        self,
    ):
        db.execute(
            "UPDATE users SET first_name=? WHERE telegram_id=42", ('=HYPERLINK("bad")',)
        )
        admin.set_limit(42, "post_create", 0)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=admin.ADMIN_ID),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=admin.ADMIN_ID, type="private")
            ),
            data="au:export:csv",
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(send_document=AsyncMock())
        await admin.controls(callback, self.state, bot)
        args = bot.send_document.await_args.args
        self.assertEqual(args[0], admin.ADMIN_ID)
        rows = list(csv.DictReader(io.StringIO(args[1].data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[0]["first_name"].startswith("'="))
        self.assertIn('"post_create": 0', rows[0]["personal_limits_json"])

    def test_snapshot_restores_committed_wal(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "source.db", Path(folder) / "export.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE data(value)")
                conn.execute("INSERT INTO data VALUES(42)")
                conn.commit()
                admin.snapshot(source, target)
                with closing(sqlite3.connect(target)) as restored:
                    self.assertEqual(
                        restored.execute("SELECT value FROM data").fetchone()[0], 42
                    )

    async def test_forged_mutations_rejected(self):
        for action in ("block:42:1", "erase:42", "limit:42:channels"):
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=43),
                message=SimpleNamespace(chat=SimpleNamespace(id=43, type="private")),
                data="au:" + action,
            )
            with self.assertRaises(ValueError):
                await admin.controls(callback, self.state, self.bot)
        self.assertFalse(accounts.blocked(42))
