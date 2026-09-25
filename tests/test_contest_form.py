"""Offline questionnaire, draft and shared publication regressions."""

import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures

from app import content, timeutils, ui
from app import database as db
from app.features import contests, posts
from services import contests as service


class ContestFormTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown
    contest = fixtures.PlanningTests.contest

    def callback(self, data):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(chat=SimpleNamespace(id=42)),
            answer=AsyncMock(),
        )

    async def test_form_saves_fields_media_task_and_resumes(self):
        c = self.callback("contest:new")
        with (
            patch.object(ui, "edit", new=AsyncMock()),
            patch.object(ui, "answer", new=AsyncMock()),
        ):
            await contests.new(c, self.state)
            c.data = f"contest:select:{self.cid}"
            await contests.select_channel(c, self.state)
            await contests.selected_channels(c, self.state)
            for step, value, payload in [
                ("prize_title", "Camera", None),
                ("prize_description", "Brand new camera", None),
                ("prize_count", "2", None),
                ("winners", "3", None),
                ("mode", "task", None),
                ("quiz", "2 + 2?\n4", None),
                ("media", "", {"content_type": "animation", "file_id": "gif"}),
            ]:
                await self.state.update_data(step=step)
                await contests.accept(c.message, self.state, self.bot, value, payload)
            saved = json.loads(db.setting("contest_draft:42"))
            self.assertEqual(saved["giveaway_type"], "contest")
            self.assertEqual(saved["winners"], 3)
            self.assertEqual(saved["post"]["content_type"], "animation")
            self.assertIn("2 + 2?", saved["post"]["text"])
            await contests.menu(c, self.state)
            await contests.resume(c, self.state)
            self.assertEqual((await self.state.get_data())["post"], saved["post"])
            await self.state.update_data(step="post")
            await contests.accept(
                c.message,
                self.state,
                self.bot,
                "Custom",
                {"content_type": "text", "text": "Custom"},
            )
            await self.state.update_data(step="winners")
            await contests.accept(c.message, self.state, self.bot, "5")
            self.assertEqual((await self.state.get_data())["post"]["text"], "Custom")
            await contests.create(c, self.state)
            self.assertEqual(
                db.one("SELECT prize_json FROM contests")[0],
                json.dumps({"value": saved["prize"]["value"], "quantity": 2}),
            )
            self.assertFalse(db.setting("contest_draft:42"))

    async def test_multichannel_draw_publishes_and_finishes_once(self):
        cid2 = db.execute(
            "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,bot_is_admin,created_at,updated_at) VALUES(-1002,'Second','channel',42,1,?,?)",
            (timeutils.iso(), timeutils.iso()),
        ).lastrowid
        cid = self.contest(status="scheduled")
        for channel in (self.cid, cid2):
            db.execute(
                "INSERT INTO contest_publications(contest_id,channel_id) VALUES(?,?)",
                (cid, channel),
            )
        send = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=101),
                SimpleNamespace(message_id=202),
            ]
        )
        with (
            patch.object(service.accounts, "channel_allowed", return_value=True),
            patch.object(
                service.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
            ),
            patch.object(content, "send_content", new=send),
            patch.object(service, "deliver", new=AsyncMock()),
        ):
            await service.tick(self.bot)
            await service.tick(self.bot)
        self.assertEqual(send.await_count, 2)
        self.assertEqual([c.args[1] for c in send.await_args_list], [-1001, -1002])
        self.assertEqual(service.get(cid)["status"], "active")
        c = self.callback(f"contest:participate:{cid}")
        c.from_user = SimpleNamespace(
            id=43, is_bot=False, username=None, first_name="Player", last_name=None
        )
        for chat_id, message_id in [(-1001, 101), (-1002, 202)]:
            c.message = SimpleNamespace(
                chat=SimpleNamespace(id=chat_id), message_id=message_id
            )
            await contests.participate(c, self.bot)
        self.assertEqual(service.participant_count(cid), 1)
        db.execute(
            "UPDATE contests SET status='closing',ends_at=? WHERE id=?",
            (timeutils.iso(timeutils.now() - timedelta(seconds=1)), cid),
        )
        await service.finish(self.bot, service.get(cid))
        results = db.all_rows(
            "SELECT recipient,payload FROM contest_outbox WHERE kind='results'"
        )
        self.assertEqual(
            {r["recipient"]: json.loads(r["payload"])["message_id"] for r in results},
            {-1001: 101, -1002: 202},
        )
        self.assertEqual(service.get(cid)["status"], "completed")

    async def test_retry_does_not_duplicate_successful_channel(self):
        cid2 = db.execute(
            "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,bot_is_admin,created_at,updated_at) VALUES(-1002,'Second','channel',42,1,?,?)",
            (timeutils.iso(), timeutils.iso()),
        ).lastrowid
        cid = self.contest(status="scheduled")
        for channel in (self.cid, cid2):
            db.execute(
                "INSERT INTO contest_publications(contest_id,channel_id) VALUES(?,?)",
                (cid, channel),
            )
        send = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=101),
                RuntimeError("connection lost"),
                SimpleNamespace(message_id=202),
            ]
        )
        with (
            patch.object(service.accounts, "channel_allowed", return_value=True),
            patch.object(
                service.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
            ),
            patch.object(content, "send_content", new=send),
            patch.object(service, "deliver", new=AsyncMock()),
            patch.object(ui, "edit", new=AsyncMock()),
        ):
            await service.tick(self.bot)
            self.assertEqual(service.get(cid)["status"], "uncertain")
            await contests.recover_publication(
                self.callback(f"contest:recover:{cid}:retry"), self.state
            )
            await service.tick(self.bot)
        self.assertEqual(
            [call.args[1] for call in send.await_args_list], [-1001, -1002, -1002]
        )
        self.assertEqual(service.get(cid)["status"], "active")

    async def test_post_draft_shortcut_and_completed_contest_hidden(self):
        pid = content.create_draft(42, {"content_type": "text", "text": "Draft"})
        c = self.callback("menu:posts")
        with patch.object(ui, "edit", new=AsyncMock()) as edit:
            await posts.menu_posts(c)
            buttons = edit.await_args.args[2].inline_keyboard
            self.assertIn(
                f"p:{pid}:preview", [b.callback_data for row in buttons for b in row]
            )
            self.contest(status="completed")
            active = self.contest(status="active")
            c.data = "contest:mine:0"
            await contests.directory(c)
            buttons = edit.await_args.args[2].inline_keyboard
            actions = [b.callback_data for row in buttons for b in row]
            self.assertEqual(
                [a for a in actions if a.startswith("contest:manage:")],
                [f"contest:manage:{active}"],
            )
