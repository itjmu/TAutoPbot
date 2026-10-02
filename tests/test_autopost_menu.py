import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures

from app import database as db
from app import ui
from app.features import multipost, posts


class AutopostMenuTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown
    batch = fixtures.PlanningTests.batch
    post = fixtures.PlanningTests.post

    def callback(self, data="menu:posts"):
        return SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=42), answer=AsyncMock()
        )

    async def test_menu_rows_and_series_draft_exclusion(self):
        self.batch(1)
        with patch.object(ui, "edit", AsyncMock()) as edit:
            await posts.menu_posts(self.callback())
            rows = edit.call_args.args[2].inline_keyboard
            self.assertEqual(
                [[b.callback_data for b in r] for r in rows],
                [
                    ["post:create", "multi:new"],
                    ["live:start", "post:scheduled"],
                    ["menu:main"],
                ],
            )
            pid = self.post()
            await posts.menu_posts(self.callback())
            self.assertEqual(
                edit.call_args.args[2].inline_keyboard[0][0].callback_data,
                f"p:{pid}:preview",
            )
        main = [b.callback_data for row in ui.main_kb().inline_keyboard for b in row]
        self.assertIn("post:create", main)
        self.assertNotIn("menu:multi", main)
        self.assertEqual(
            ui.main_kb().inline_keyboard[0][0].callback_data, "post:create"
        )
        self.assertEqual(ui.main_kb().inline_keyboard[1][0].callback_data, "menu:posts")

    async def test_cancel_deletes_unsent_but_preserves_published(self):
        bid = self.batch(2)
        pids = [i["post_id"] for i in multipost.items(bid)]
        multipost.confirm(bid, 42)
        db.execute("UPDATE posts SET status='published' WHERE id=?", (pids[0],))
        db.execute(
            "INSERT INTO published_messages(post_id,channel_id,telegram_message_id) VALUES(?,?,77)",
            (pids[0], self.cid),
        )
        with self.assertRaises(ValueError):
            multipost.cancel_batch(bid, 43)
        multipost.cancel_batch(bid, 42)
        self.assertIsNone(db.one("SELECT 1 FROM multipost_batches WHERE id=?", (bid,)))
        self.assertFalse(multipost.items(bid))
        self.assertIsNone(db.one("SELECT 1 FROM posts WHERE id=?", (pids[1],)))
        self.assertIsNotNone(
            db.one("SELECT 1 FROM published_messages WHERE post_id=?", (pids[0],))
        )
        self.assertIsNotNone(db.one("SELECT 1 FROM posts WHERE id=?", (pids[0],)))
        with (
            patch.object(ui, "edit", AsyncMock()),
            patch.object(multipost.accounts, "channel_allowed", return_value=True),
        ):
            await multipost.target(
                self.callback(f"multi:target:{self.cid}"), self.state
            )
        self.assertGreater((await self.state.get_data())["batch_id"], bid)

    async def test_combined_list_and_legacy_cancel_cleanup(self):
        active = self.batch(1)
        multipost.confirm(active, 42)
        cancelled = self.batch(1)
        removed_pid = multipost.items(cancelled)[0]["post_id"]
        db.execute(
            "UPDATE multipost_batches SET status='cancelled' WHERE id=?", (cancelled,)
        )
        with patch.object(ui, "edit", AsyncMock()) as edit:
            await posts.post_list(self.callback("post:scheduled"))
            callbacks = [
                b.callback_data
                for row in edit.call_args.args[2].inline_keyboard
                for b in row
            ]
        self.assertIn(f"multi:{active}:open", callbacks)
        self.assertIn(f"p:{multipost.items(active)[0]['post_id']}:preview", callbacks)
        self.assertNotIn(f"multi:{cancelled}:open", callbacks)
        self.assertIsNone(db.one("SELECT 1 FROM posts WHERE id=?", (removed_pid,)))
