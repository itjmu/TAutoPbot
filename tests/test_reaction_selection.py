import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures

from app import content, timeutils
from app import database as db
from app.features import posts
from app.features import published_editor as live
from services.reactions import inherit_edited, toggle


class ReactionSelectionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def buttons(self):
        return [dict(id=bid, type="reaction", text=bid, row=1) for bid in ("yes", "no")]

    def publication(self):
        pid = content.create_draft(42, dict(content_type="text", text="Post"))
        db.execute(
            "INSERT INTO published_messages(post_id,channel_id,telegram_message_id,buttons_json) VALUES(?,?,?,?)",
            (pid, self.cid, 77, json.dumps(self.buttons())),
        )
        return pid

    def callback(self, pid, bid, uid=42, mid=77):
        return SimpleNamespace(
            data=f"action:{pid}:{bid}",
            from_user=SimpleNamespace(id=uid),
            answer=AsyncMock(),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-1001),
                message_id=mid,
                edit_reply_markup=AsyncMock(),
            ),
        )

    def rows(self, pid):
        return [
            (r["user_id"], r["button_id"])
            for r in db.all_rows(
                "SELECT * FROM post_reactions WHERE post_id=? ORDER BY user_id", (pid,)
            )
        ]

    async def test_toggle_switch_and_other_users(self):
        pid = self.publication()
        c = self.callback(pid, "yes")
        await posts.published_action(c, self.bot)
        self.assertEqual(self.rows(pid), [(42, "yes")])
        await posts.published_action(c, self.bot)
        self.assertEqual(self.rows(pid), [])
        await posts.published_action(c, self.bot)
        await posts.published_action(self.callback(pid, "yes", 43), self.bot)
        c.data = f"action:{pid}:no"
        await posts.published_action(c, self.bot)
        self.assertEqual(self.rows(pid), [(42, "no"), (43, "yes")])
        self.assertEqual(
            [
                b.text
                for b in c.message.edit_reply_markup.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0]
            ],
            ["yes 1", "no 1"],
        )
        with self.assertRaises(ValueError):
            await posts.published_action(self.callback(pid, "yes", mid=78), self.bot)
        self.assertEqual(self.rows(pid), [(42, "no"), (43, "yes")])

    async def test_concurrent_users_cannot_overwrite_latest_counts(self):
        pid = self.publication()
        edits = []

        async def edit(reply_markup):
            label = reply_markup.inline_keyboard[0][0].text
            await asyncio.sleep(0.02 if label == "yes 1" else 0)
            edits.append(label)

        callbacks = [self.callback(pid, "yes", uid) for uid in (42, 43)]
        for c in callbacks:
            c.message.edit_reply_markup = edit
        await asyncio.gather(*(posts.published_action(c, self.bot) for c in callbacks))
        self.assertEqual(edits, ["yes 1", "yes 2"])

    def test_legacy_multi_selection_normalized_only_for_acting_user(self):
        pid = self.publication()
        scope = dict(post_id=pid, channel_id=self.cid, telegram_message_id=77)
        for uid, bid in ((42, "yes"), (42, "no"), (43, "yes")):
            db.execute(
                "INSERT INTO post_reactions VALUES(?,?,?,?,?,?)",
                (pid, self.cid, 77, bid, uid, timeutils.iso()),
            )
        toggle("post_reactions", scope, "yes", 42)
        self.assertEqual(self.rows(pid), [(43, "yes")])

    async def test_edit_preserves_voters_and_switches_without_frozen_counts(self):
        pid = self.publication()
        await posts.published_action(self.callback(pid, "yes"), self.bot)
        data = dict(
            token="abc",
            chat_id=-1001,
            message_id=77,
            payload=dict(content_type="text", text="Edited"),
            buttons=[
                dict(b, initial_count=1 if b["id"] == "yes" else 0)
                for b in self.buttons()
            ],
        )
        with patch.object(live, "check_access", new=AsyncMock()):
            await live.apply_edit(self.bot, 42, data)
        oid = int(data["token"], 16)
        c = self.callback(pid, "yes")
        c.data = f"lb:42:{oid}:yes"
        await live.public_button(c, self.bot)
        self.assertEqual(
            live.live_markup(42, live.load_session(42, oid, published=True))
            .inline_keyboard[0][0]
            .text,
            "yes 0",
        )
        c.data = f"lb:42:{oid}:no"
        await live.public_button(c, self.bot)
        edited = live.load_session(42, oid, published=True)
        self.assertEqual(
            [b.text for b in live.live_markup(42, edited).inline_keyboard[0]],
            ["yes 0", "no 1"],
        )
        again = dict(edited, token="def", closed=False)
        with patch.object(live, "check_access", new=AsyncMock()):
            await live.apply_edit(self.bot, 42, again)
        new_id = int(again["token"], 16)
        with self.assertRaises(ValueError):
            await live.public_button(c, self.bot)
        c.data = f"lb:42:{new_id}:no"
        await live.public_button(c, self.bot)
        result = live.live_markup(42, live.load_session(42, new_id, published=True))
        self.assertEqual([b.text for b in result.inline_keyboard[0]], ["yes 0", "no 0"])

    def test_legacy_edited_session_recovers_original_voters(self):
        pid = self.publication()
        db.execute(
            "INSERT INTO post_reactions VALUES(?,?,?,?,?,?)",
            (pid, self.cid, 77, "yes", 42, timeutils.iso()),
        )
        data = dict(
            token="aaa",
            closed=True,
            chat_id=-1001,
            message_id=77,
            buttons=[
                dict(b, initial_count=1 if b["id"] == "yes" else 0)
                for b in self.buttons()
            ],
        )
        inherit_edited(42, data)
        self.assertEqual(data["buttons"][0]["initial_count"], 0)
        toggle(
            "edited_reactions", dict(owner_id=42, session_id=int("aaa", 16)), "yes", 42
        )
        inherit_edited(42, data)
        self.assertEqual(db.one("SELECT COUNT(*) FROM edited_reactions")[0], 0)
