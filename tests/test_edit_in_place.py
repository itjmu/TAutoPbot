import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Message

from app import content
from app import database as db
from app.features import editors
from app.features import published_editor as live


class EditInPlaceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def api(self):
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_edit_messages=True)
        )
        self.bot.edit_message_media = AsyncMock()
        return self.bot

    async def test_button_panel_updates_existing_preview_with_one_api_request(self):
        pid = content.create_draft(42, {"content_type": "text", "text": "Original"})
        db.execute("UPDATE posts SET preview_ids='[900]' WHERE id=?", (pid,))
        c = SimpleNamespace(from_user=SimpleNamespace(id=42), bot=self.bot)
        with (
            patch.object(content, "send_content", new=AsyncMock()) as send,
            patch.object(editors.ui, "show_panel", new=AsyncMock()) as panel,
        ):
            await editors.button_panel(c, "p", pid)
        send.assert_not_called()
        panel.assert_not_called()
        self.bot.delete_message.assert_not_called()
        self.bot.edit_message_reply_markup.assert_awaited_once()
        call = self.bot.edit_message_reply_markup.await_args.kwargs
        self.assertEqual(call["message_id"], 900)
        self.assertIn(
            f"bw:done:p:{pid}",
            {
                b.callback_data
                for row in call["reply_markup"].inline_keyboard
                for b in row
            },
        )

    async def test_button_input_instructions_remain_on_preview(self):
        pid = content.create_draft(42, {"content_type": "text", "text": "Original"})
        db.execute("UPDATE posts SET preview_ids='[900]' WHERE id=?", (pid,))
        event = SimpleNamespace(bot=self.bot)
        await editors.button_prompt(
            event, 42, "p", pid, "Пришлите ссылку", editors.ui.back(f"bw:panel:p:{pid}")
        )
        self.bot.send_message.assert_not_called()
        self.assertEqual(
            self.bot.edit_message_reply_markup.await_args.kwargs["message_id"], 900
        )

    async def test_forward_edit_and_save_changes_original_id_without_send(self):
        bot = self.api()
        m = Message(
            message_id=12,
            date=0,
            chat={"id": 42, "type": "private"},
            from_user={"id": 42, "is_bot": False, "first_name": "U"},
            text="Old",
            forward_origin={
                "type": "channel",
                "date": 0,
                "chat": {"id": -10077, "type": "channel", "title": "Channel"},
                "message_id": 456,
            },
        )
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ):
            await live.original(m, self.state, bot)
            new = m.model_copy(
                update={"text": "New", "entities": None, "forward_origin": None}
            )
            await live.new_text(new, self.state, bot)
        data = await self.state.get_data()
        bot.send_message.reset_mock()
        await live.apply_edit(bot, 42, data)
        call = bot.edit_message_text.await_args.kwargs
        self.assertEqual(
            (call["chat_id"], call["message_id"], call["text"]), (-10077, 456, "New")
        )
        bot.send_message.assert_not_called()
        self.assertTrue(db.setting("published_edit:-10077:456"))

    async def test_caption_and_media_preserve_entities_and_message_id(self):
        bot = self.api()
        data = {
            "chat_id": -10077,
            "message_id": 456,
            "payload": {
                "content_type": "photo",
                "file_id": "new-photo",
                "text": "Bold",
                "caption_entities_json": json.dumps(
                    [{"type": "bold", "offset": 0, "length": 4}]
                ),
            },
        }
        await live.apply_edit(bot, 42, data)
        call = bot.edit_message_caption.await_args.kwargs
        self.assertEqual(call["caption_entities"][0]["type"], "bold")
        self.assertEqual(call["message_id"], 456)
        data["media_changed"] = True
        await live.apply_edit(bot, 42, data)
        call = bot.edit_message_media.await_args.kwargs
        self.assertEqual(call["media"].media, "new-photo")
        self.assertEqual(call["message_id"], 456)
        bot.send_message.assert_not_called()

    async def test_rights_checked_again_on_save_and_failure_never_republishes(self):
        bot = self.api()
        data = {
            "chat_id": -10077,
            "message_id": 456,
            "payload": {"content_type": "text", "text": "New"},
        }
        bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        with self.assertRaises(ValueError):
            await live.apply_edit(bot, 42, data)
        bot.edit_message_text.assert_not_called()
        self.api()
        bot.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=-10077, message_id=456, text="New"),
            message="message can't be edited",
        )
        with self.assertRaises(TelegramBadRequest):
            await live.apply_edit(bot, 42, data)
        bot.send_message.assert_not_called()

    async def test_hidden_forward_and_stale_apply_rejected(self):
        m = Message(
            message_id=12,
            date=0,
            chat={"id": 42, "type": "private"},
            from_user={"id": 42, "is_bot": False, "first_name": "U"},
            text="Old",
        )
        with self.assertRaises(ValueError):
            await live.original(m, self.state, self.api())
        c = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            data="live:apply:invalid",
            answer=AsyncMock(),
        )
        with self.assertRaises(ValueError):
            await live.action(c, self.state, self.bot)
        self.bot.edit_message_text.assert_not_called()

    async def test_unknown_buttons_require_explicit_confirmation_before_edit(self):
        bot = self.api()
        await self.state.set_data(
            {
                "token": "a",
                "chat_id": -10077,
                "message_id": 456,
                "preview_id": 901,
                "unknown_markup": True,
                "payload": {"content_type": "text", "text": "New"},
            }
        )
        c = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            data="live:apply:a",
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        await live.action(c, self.state, bot)
        bot.edit_message_text.assert_not_called()
        self.assertTrue(c.answer.await_args.kwargs["show_alert"])
        callbacks = {
            b.callback_data
            for row in bot.edit_message_reply_markup.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            for b in row
        }
        self.assertIn("live:confirmed:a", callbacks)
        c.data = "live:confirmed:a"
        with patch.object(live.ui, "answer", new=AsyncMock()):
            await live.action(c, self.state, bot)
        self.assertEqual(bot.edit_message_text.await_args.kwargs["message_id"], 456)
        self.assertEqual(await self.state.get_data(), {})
