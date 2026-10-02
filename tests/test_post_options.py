import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import PinChatMessage

from app import content, preferences, timeutils, ui
from app import database as db
from app.features import posts
from app.states import PostCreate
from services import post_pins


class PostOptionsTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def draft(self):
        return content.create_draft(
            42, {"content_type": "text", "text": "Post"}, [self.cid]
        )

    def pin(self, pid, **changes):
        values = dict(
            pin_enabled=1, pin_until=timeutils.iso(timeutils.now() + timedelta(hours=2))
        )
        values.update(changes)
        db.execute(
            "UPDATE posts SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
            (*values.values(), pid),
        )

    def mock_rights(self, **rights):
        self.bot.get_chat = AsyncMock(return_value=SimpleNamespace(type="channel"))
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(
                status="administrator", can_post_messages=True, **rights
            )
        )
        self.bot.pin_chat_message = AsyncMock()
        self.bot.unpin_chat_message = AsyncMock()

    async def test_protection_for_text_media_and_album_companion(self):
        markup = content.build_published_markup(
            [dict(id="b", row=1, type="url", text="Link", url="https://example.com")], 1
        )
        for typ in (
            "text",
            "photo",
            "video",
            "animation",
            "document",
            "audio",
            "voice",
            "album",
        ):
            bot = SimpleNamespace(
                **{
                    name: AsyncMock(return_value=SimpleNamespace(message_id=1))
                    for name in (
                        "send_message",
                        "send_photo",
                        "send_video",
                        "send_animation",
                        "send_document",
                        "send_audio",
                        "send_voice",
                    )
                }
            )
            bot.send_media_group = AsyncMock(
                return_value=[
                    SimpleNamespace(message_id=2),
                    SimpleNamespace(message_id=3),
                ]
            )
            data = dict(
                content_type=typ,
                text="Post",
                file_id="file",
                protect_content=True,
                media_json=[
                    dict(content_type="photo", file_id="a"),
                    dict(content_type="photo", file_id="b"),
                ],
            )
            with self.subTest(typ=typ):
                await content.send_content(bot, -1001, data, markup)
                for name, method in vars(bot).items():
                    if method.await_count:
                        self.assertTrue(
                            method.await_args.kwargs["protect_content"], name
                        )
                if typ != "album":
                    self.assertEqual(
                        getattr(
                            bot, "send_message" if typ == "text" else "send_" + typ
                        ).await_args.kwargs["reply_markup"],
                        markup,
                    )
        self.assertIsNone(markup.inline_keyboard[0][0].callback_data)

    async def test_publish_pin_and_expiry_only_unpins_exact_post(self):
        pid = self.draft()
        self.pin(pid)
        self.mock_rights(can_edit_messages=True)
        with patch.object(posts.accounts, "check_referral", new=AsyncMock()):
            self.assertEqual(await posts.execute_publish(pid, self.bot), "published")
            await posts.execute_publish(pid, self.bot)
        self.bot.send_message.assert_awaited_once()
        self.bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-1001, message_id=77, disable_notification=True
        )
        db.execute(
            "UPDATE published_messages SET unpin_at=?",
            (timeutils.iso(timeutils.now() - timedelta(seconds=1)),),
        )
        await post_pins.process(self.bot)
        self.bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-1001, message_id=77
        )
        await post_pins.process(self.bot)
        self.bot.unpin_chat_message.assert_awaited_once()
        self.assertEqual(db.one("SELECT pin_state FROM published_messages")[0], "done")

    async def test_pin_failure_never_republishes_content(self):
        pid = self.draft()
        self.pin(pid)
        self.mock_rights(can_edit_messages=True)
        self.bot.pin_chat_message.side_effect = OSError("connection lost")
        with patch.object(posts.accounts, "check_referral", new=AsyncMock()):
            self.assertEqual(await posts.execute_publish(pid, self.bot), "published")
        self.assertEqual(
            db.one("SELECT pin_state FROM published_messages")[0], "pending"
        )
        db.execute("UPDATE published_messages SET pin_retry_at=NULL")
        self.bot.pin_chat_message.side_effect = None
        await post_pins.process(self.bot)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(
            db.one("SELECT pin_state FROM published_messages")[0], "pinned"
        )

    async def test_pin_rights_for_channels_and_groups(self):
        self.mock_rights(can_edit_messages=False, can_pin_messages=True)
        with self.assertRaises(ValueError):
            await post_pins.check_rights(self.bot, -1001, 42)
        self.bot.get_chat.return_value.type = "supergroup"
        await post_pins.check_rights(self.bot, -1001, 42)
        self.bot.get_chat_member.side_effect = [
            SimpleNamespace(status="administrator", can_pin_messages=False),
            SimpleNamespace(status="creator"),
        ]
        with self.assertRaises(ValueError):
            await post_pins.check_rights(self.bot, -1001, 42)

    async def test_pin_deadline_timezone_and_schedule_validation(self):
        pid = self.draft()
        preferences.save(42, timezone="UTC+05:00")
        future = timeutils.now() + timedelta(days=2)
        local = (future + timedelta(hours=5)).strftime("%d.%m.%Y %H:%M")
        await self.state.set_data(dict(pid=pid))
        await self.state.set_state(PostCreate.pin)
        m = SimpleNamespace(from_user=SimpleNamespace(id=42), text=local)
        with patch.object(posts, "show_post", new=AsyncMock()):
            await posts.edit_post_value(m, self.state, self.bot)
        row = content.post_owned(pid, 42)
        self.assertEqual(
            timeutils.parse_dt(row["pin_until"]),
            future.replace(second=0, microsecond=0),
        )
        with self.assertRaises(ValueError):
            posts.schedule_post(pid, 42, future + timedelta(hours=1))
        db.execute("UPDATE posts SET protect_content=1 WHERE id=?", (pid,))
        posts.schedule_post(pid, 42, future - timedelta(hours=1))
        payload = json.loads(
            db.one("SELECT payload_json FROM post_targets WHERE post_id=?", (pid,))[0]
        )
        self.assertTrue(payload["protect_content"])

    async def test_checkbox_changes_saved_value_and_cancels_schedule(self):
        pid = self.draft()
        posts.schedule_post(pid, 42, timeutils.now() + timedelta(hours=1))
        c = SimpleNamespace(
            data=f"p:{pid}:protect",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        with patch.object(posts, "show_post", new=AsyncMock()):
            await posts.post_action(c, self.state, self.bot)
        row = content.post_owned(pid, 42)
        self.assertEqual(row["protect_content"], 1)
        self.assertEqual(row["status"], "draft")
        self.assertIsNone(
            db.one("SELECT payload_json FROM post_targets WHERE post_id=?", (pid,))[0]
        )
        labels = [
            b.text
            for r in ui.post_controls(
                pid, protect_content=True, pin_enabled=True
            ).inline_keyboard
            for b in r
        ]
        self.assertTrue(
            any(label.startswith("☑") and "Без копий" in label for label in labels)
        )

    async def test_retry_after_and_permanent_pin_error(self):
        pid = self.draft()
        self.pin(pid, pin_until=None)
        self.mock_rights(can_edit_messages=True)
        method = PinChatMessage(chat_id=-1001, message_id=77)
        self.bot.pin_chat_message.side_effect = TelegramRetryAfter(
            method=method, message="wait", retry_after=10
        )
        with patch.object(posts.accounts, "check_referral", new=AsyncMock()):
            await posts.execute_publish(pid, self.bot)
        await post_pins.process(self.bot)
        self.bot.pin_chat_message.assert_awaited_once()
        db.execute("UPDATE published_messages SET pin_retry_at=NULL")
        self.bot.pin_chat_message.side_effect = TelegramBadRequest(
            method=method, message="not enough rights"
        )
        await post_pins.process(self.bot)
        self.assertEqual(
            db.one("SELECT pin_state FROM published_messages")[0], "failed"
        )
        self.assertEqual(content.post_owned(pid, 42)["status"], "published")

    def test_additive_migration_preserves_old_data(self):
        pid = self.draft()
        db.init_extensions()
        db.init_extensions()
        row = content.post_owned(pid, 42)
        self.assertEqual(row["text"], "Post")
        self.assertEqual(row["protect_content"], 0)
        self.assertEqual(row["pin_enabled"], 0)

    async def test_expired_pending_job_unpins_after_restart(self):
        pid = self.draft()
        db.execute(
            "INSERT INTO published_messages(post_id,channel_id,telegram_message_id,pin_state,unpin_at) VALUES(?,?,?,'pending',?)",
            (pid, self.cid, 99, timeutils.iso(timeutils.now() - timedelta(seconds=1))),
        )
        self.mock_rights(can_edit_messages=True)
        await post_pins.process(self.bot)
        self.bot.pin_chat_message.assert_not_awaited()
        self.bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-1001, message_id=99
        )

    async def test_url_only_publication_contains_no_editor_callbacks(self):
        pid = self.draft()
        buttons = [
            dict(id="url", type="url", row=1, text="Site", url="https://example.com")
        ]
        db.execute(
            "UPDATE posts SET buttons_json=? WHERE id=?", (json.dumps(buttons), pid)
        )
        with (
            patch.object(
                posts.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
            ),
            patch.object(posts.accounts, "check_referral", new=AsyncMock()),
        ):
            await posts.execute_publish(pid, self.bot)
        markup = self.bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(len(markup.inline_keyboard[0]), 1)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://example.com")
        self.assertIsNone(markup.inline_keyboard[0][0].callback_data)
        self.assertNotIn("protect_content", self.bot.send_message.await_args.kwargs)
