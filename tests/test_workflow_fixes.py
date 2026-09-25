"""Offline regressions for message lifecycle, media broadcasts and math captcha."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import Chat, Message, PhotoSize, User

from app import database as db
from app import timeutils, ui
from app.features import admin, conditions
from app.states import Broadcast


class WorkflowFixTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    async def test_menu_moves_below_new_content_and_retires_only_old_menu(self):
        await ui.show_panel(self.bot, 42, "Menu")
        ui.note_message(42, 90)
        self.bot.send_message.return_value = SimpleNamespace(message_id=91)
        await ui.show_panel(self.bot, 42, "Next step")
        self.assertEqual(ui.panel_id(42), 91)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(
            self.bot.edit_message_reply_markup.await_args.kwargs["message_id"], 77
        )
        self.bot.delete_message.assert_not_awaited()

    async def test_forwarded_media_and_album_wait_for_confirmation_then_copy(self):
        self.bot.copy_messages = AsyncMock(
            return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
        )
        await self.state.set_state(Broadcast.content)
        with (
            patch.object(admin, "ADMIN_ID", 42),
            patch.object(ui, "answer", new=AsyncMock()),
            patch.object(ui, "edit", new=AsyncMock()),
        ):
            for mid in (101, 102):
                message = Message(
                    message_id=mid,
                    date=timeutils.now(),
                    chat=Chat(id=42, type="private"),
                    from_user=User(id=42, is_bot=False, first_name="Admin"),
                    photo=[
                        PhotoSize(
                            file_id="file", file_unique_id="unique", width=10, height=10
                        )
                    ],
                    media_group_id="album",
                )
                handler = next(
                    h
                    for h in admin.router.message.handlers
                    if h.callback is admin.broadcast_send
                )
                matched, _ = await handler.check(
                    message, raw_state=Broadcast.content.state
                )
                self.assertTrue(matched)
                await admin.broadcast_send(message, self.state, self.bot)
            self.bot.copy_messages.assert_not_awaited()
            draft = json.loads(db.setting("broadcast_draft:42"))
            c = SimpleNamespace(
                from_user=SimpleNamespace(id=42),
                data="broadcast:send:" + draft["token"],
                answer=AsyncMock(),
            )
            await admin.broadcast_confirm(c, self.state, self.bot)
            self.assertEqual(self.bot.copy_messages.await_count, 4)
            self.assertEqual(
                self.bot.copy_messages.await_args.args[1:], (42, [101, 102])
            )
            with self.assertRaises(ValueError):
                await admin.broadcast_confirm(c, self.state, self.bot)

    async def test_math_condition_requires_correct_per_request_answer(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
        with patch.object(ui, "answer", new=AsyncMock()):
            await conditions.condition_value(
                message,
                self.state,
                self.bot,
                "math_captcha",
                42,
                {"step": "type", "name": "Math", "token": "x"},
            )
        item = db.one("SELECT * FROM condition_items")
        self.assertEqual(item["item_type"], "math_captcha")
        db.execute(
            "UPDATE channels SET condition_id=?,auto_requests=1 WHERE id=?",
            (item["condition_id"], self.cid),
        )
        rid = db.execute(
            "INSERT INTO join_requests(telegram_user_id,channel_id,created_at,updated_at) VALUES(?,?,?,?)",
            (43, self.cid, timeutils.iso(), timeutils.iso()),
        ).lastrowid
        challenge = conditions.request_captcha(rid, item["id"])
        self.assertEqual(conditions.request_captcha(rid, item["id"]), challenge)
        message.from_user.id = 43
        self.bot.approve_chat_join_request = AsyncMock()
        with patch.object(conditions, "supports_requests", return_value=True):
            await conditions.check_request(rid, self.bot, True)
            self.bot.approve_chat_join_request.assert_not_awaited()
            message.text = f"/answer {rid} -1"
            await conditions.join_answer(message, self.bot)
            self.assertFalse(db.one("SELECT is_correct FROM request_answers")[0])
            self.bot.approve_chat_join_request.assert_not_awaited()
            message.text = f"/answer {rid} {challenge['answer']}"
            await conditions.join_answer(message, self.bot)
            self.assertTrue(db.one("SELECT 1 FROM request_answers WHERE is_correct=1"))
            self.bot.approve_chat_join_request.assert_awaited_once_with(-1001, 43)
            message.from_user.id = 44
            with self.assertRaises(ValueError):
                await conditions.join_answer(message, self.bot)
