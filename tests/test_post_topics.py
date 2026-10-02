import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from app import content, timeutils, ui
from app import database as db
from app.features import posts
from app.states import PostCreate


class PostTopicTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def draft(self):
        db.execute("UPDATE channels SET chat_type='supergroup' WHERE id=?", (self.cid,))
        self.bot.get_chat = AsyncMock(
            return_value=SimpleNamespace(id=-1001, username="group", is_forum=True)
        )
        return content.create_draft(
            42, {"content_type": "text", "text": "Post"}, [self.cid]
        )

    def test_references(self):
        chat = SimpleNamespace(id=-100123, username="group")
        for value in (
            "25",
            "https://t.me/c/123/25",
            "https://t.me/group/25/90",
            "https://t.me/group/90?thread=25",
        ):
            self.assertEqual(posts.parse_topic_reference(value, chat), 25)
        for value in ("0", "1"):
            self.assertIsNone(posts.parse_topic_reference(value, chat))
        for value in (
            "-2",
            "https://t.me/other/25",
            "https://t.me/c/999/25",
            "999999999999",
            "abc",
        ):
            with self.assertRaises(ValueError):
                posts.parse_topic_reference(value, chat)

    async def test_selection_persistence_and_reset(self):
        pid = self.draft()
        await self.state.update_data(post_id=pid, topic_channel_id=self.cid)
        message = SimpleNamespace(text="25", from_user=SimpleNamespace(id=42))
        with patch.object(ui, "send_target_picker", new=AsyncMock()):
            await posts.topic_input(message, self.state, self.bot)
            self.assertEqual(posts.post_target_rows(pid)[0]["message_thread_id"], 25)
            self.assertEqual(await self.state.get_state(), PostCreate.idle.state)
            self.assertIn(f"p:{pid}:topic:{self.cid}", str(ui.target_markup(42, pid)))
            message.text = "0"
            await posts.topic_input(message, self.state, self.bot)
            self.assertIsNone(posts.post_target_rows(pid)[0]["message_thread_id"])

    async def test_schedule_delivery_and_private_preview(self):
        pid = self.draft()
        db.execute(
            "UPDATE post_targets SET message_thread_id=25 WHERE post_id=?", (pid,)
        )
        with patch.object(
            posts.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
        ):
            posts.schedule_post(
                pid, 42, timeutils.parse_dt("2030-01-01T00:00:00+00:00")
            )
            await posts.show_post(self.bot, 42, pid)
            self.assertNotIn(
                "message_thread_id", self.bot.send_message.await_args.kwargs
            )
            await posts.execute_publish(pid, self.bot)
        self.assertEqual(
            self.bot.send_message.await_args.kwargs["message_thread_id"], 25
        )
        self.assertEqual(
            db.one("SELECT status FROM posts WHERE id=?", (pid,))["status"], "published"
        )

    async def test_all_media_and_companions(self):
        for typ in (
            "text",
            "photo",
            "video",
            "animation",
            "document",
            "audio",
            "voice",
            "video_note",
            "album",
        ):
            methods = {
                "send_" + t: AsyncMock(return_value=SimpleNamespace(message_id=1))
                for t in (
                    "message",
                    "photo",
                    "video",
                    "animation",
                    "document",
                    "audio",
                    "voice",
                    "video_note",
                )
            }
            methods["send_media_group"] = AsyncMock(
                return_value=[
                    SimpleNamespace(message_id=2),
                    SimpleNamespace(message_id=3),
                ]
            )
            bot = SimpleNamespace(**methods)
            payload = dict(
                content_type=typ,
                text="Description",
                file_id="file",
                media_json=[
                    dict(content_type="photo", file_id="a"),
                    dict(content_type="photo", file_id="b"),
                ],
            )
            await content.send_content(
                bot,
                -1001,
                payload,
                reply_markup=SimpleNamespace(),
                message_thread_id=25,
            )
            for method in methods.values():
                if method.await_count:
                    self.assertEqual(
                        method.await_args.kwargs["message_thread_id"], 25, typ
                    )

    async def test_nonforum_does_not_send(self):
        pid = self.draft()
        db.execute(
            "UPDATE post_targets SET message_thread_id=25 WHERE post_id=?", (pid,)
        )
        self.bot.get_chat.return_value.is_forum = False
        with patch.object(
            posts.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
        ):
            await posts.execute_publish(pid, self.bot)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(
            db.one("SELECT status FROM posts WHERE id=?", (pid,))["status"], "failed"
        )

    async def test_deleted_topic_never_falls_back(self):
        pid = self.draft()
        db.execute(
            "UPDATE post_targets SET message_thread_id=25 WHERE post_id=?", (pid,)
        )
        self.bot.send_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=-1001, text="Post"),
            message="message thread not found",
        )
        with patch.object(
            posts.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
        ):
            await posts.execute_publish(pid, self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(
            self.bot.send_message.await_args.kwargs["message_thread_id"], 25
        )
        self.assertEqual(
            db.one("SELECT status FROM posts WHERE id=?", (pid,))["status"], "failed"
        )

    async def test_topic_input_rejects_other_owner(self):
        pid = self.draft()
        await self.state.update_data(post_id=pid, topic_channel_id=self.cid)
        with self.assertRaises(ValueError):
            await posts.topic_input(
                SimpleNamespace(text="25", from_user=SimpleNamespace(id=43)),
                self.state,
                self.bot,
            )
        self.assertIsNone(posts.post_target_rows(pid)[0]["message_thread_id"])


if __name__ == "__main__":
    unittest.main()
