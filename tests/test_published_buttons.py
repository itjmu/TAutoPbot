import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import content
from app import database as db
from app.features import editors
from app.features import published_editor as live


class PublishedButtonsTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def session(self, buttons=None):
        data = {
            "token": "abc",
            "chat_id": -10077,
            "message_id": 456,
            "preview_id": 900,
            "payload": {"content_type": "text", "text": "Original"},
            "buttons": buttons or [],
            "markup": None,
        }
        live.store_session(42, data)
        return data

    def callback(self, text=""):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            bot=self.bot,
            message=SimpleNamespace(),
            answer=AsyncMock(),
            data=text,
        )

    def rights(self):
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_edit_messages=True)
        )

    async def test_draft_button_save_reposts_preview_at_bottom(self):
        pid = content.create_draft(42, {"content_type": "text", "text": "Original"})
        db.execute("UPDATE posts SET preview_ids='[900]' WHERE id=?", (pid,))
        editors.save_buttons(
            42,
            "p",
            pid,
            [
                {
                    "id": "b",
                    "type": "url",
                    "text": "Link",
                    "row": 1,
                    "url": "https://example.com",
                }
            ],
        )
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ) as send:
            await editors.button_panel(self.callback(), "p", pid)
        send.assert_awaited_once()
        self.assertEqual(json.loads(content.post_owned(pid, 42)["preview_ids"]), [901])
        self.assertIn(
            ((42, 900), {}),
            [
                (call.args, call.kwargs)
                for call in self.bot.delete_message.await_args_list
            ],
        )

    async def test_shared_button_editor_add_edit_delete_then_save_original_message(
        self,
    ):
        self.rights()
        data = self.session(
            [
                {
                    "id": "old",
                    "type": "url",
                    "text": "Old",
                    "row": 1,
                    "url": "https://example.com",
                }
            ]
        )
        oid = int(data["token"], 16)
        new = {
            "id": "new",
            "type": "alert",
            "text": "Help",
            "alert": "Details",
            "row": 2,
        }
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ) as send:
            await editors.finish_placed_button(
                self.callback(),
                self.state,
                {"scope": "l", "oid": oid, "placement": 2, "button": new},
            )
        send.assert_awaited_once()
        self.assertEqual(live.load_session(42, oid)["preview_id"], 901)
        self.assertEqual(
            [b["id"] for b in editors.button_document(42, "l", oid)], ["old", "new"]
        )
        new["text"] = "Changed"
        editors.save_buttons(42, "l", oid, [new])
        data = live.load_session(42, oid)
        self.bot.send_message.reset_mock()
        await live.apply_edit(self.bot, 42, data)
        call = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual((call["chat_id"], call["message_id"]), (-10077, 456))
        markup = call["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "Changed")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"lb:42:{oid}:new")
        self.bot.send_message.assert_not_called()
        with self.assertRaises(ValueError):
            editors.button_document(42, "l", oid)
        with self.assertRaises(ValueError):
            editors.button_document(43, "l", oid)

    async def test_new_reaction_and_hint_work_only_in_their_publication(self):
        self.rights()
        data = self.session(
            [
                {"id": "r", "type": "reaction", "text": "Like", "row": 1},
                {
                    "id": "h",
                    "type": "alert",
                    "text": "Help",
                    "alert": "Details",
                    "row": 2,
                },
            ]
        )
        oid = int(data["token"], 16)
        await live.apply_edit(self.bot, 42, data)
        c = self.callback(f"lb:42:{oid}:r")
        c.message = SimpleNamespace(chat=SimpleNamespace(id=-10077), message_id=456)
        await live.public_button(c, self.bot)
        await live.public_button(c, self.bot)
        self.assertEqual(db.one("SELECT COUNT(*) FROM edited_reactions")[0], 0)
        self.assertEqual(
            self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0]
            .text,
            "Like 0",
        )
        c.data = f"lb:42:{oid}:h"
        await live.public_button(c, self.bot)
        self.assertEqual(c.answer.await_args.args[0], "Details")
        c.message.message_id = 999
        with self.assertRaises(ValueError):
            await live.public_button(c, self.bot)

    def test_import_preserves_existing_url_style_and_unknown_button(self):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Site", url="https://example.com", style="danger"
                    ),
                    InlineKeyboardButton(text="Other", callback_data="external:opaque"),
                ]
            ]
        )
        buttons = live.import_buttons(markup)
        self.assertEqual([b["type"] for b in buttons], ["url", "raw"])
        buttons[0].update(text="New", url="https://example.org")
        buttons[1]["text"] = "Rename"
        data = self.session(buttons)
        result = live.live_markup(42, data)
        self.assertEqual(result.inline_keyboard[0][0].url, "https://example.org")
        self.assertEqual(result.inline_keyboard[0][0].style, "danger")
        self.assertEqual(result.inline_keyboard[0][1].callback_data, "external:opaque")
        self.assertEqual(result.inline_keyboard[0][1].text, "Rename")
