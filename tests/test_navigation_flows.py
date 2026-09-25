"""Private navigation integrations with an offline Telegram transport."""

import json
import os
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

os.environ["DB_FILE"] = ":memory:"
os.environ["BOT_TOKEN"] = ""
os.environ["ADMIN_ID"] = "12345"

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from app import accounts, content, database, timeutils, ui
from app.features import common, downloads, posts
from app.middleware import Guard
from app.states import GuidedInput


class PanelSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_id = 700

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ not in {"sendMessage", "editMessageText"}:
            return True
        if method.__api_method__ == "sendMessage":
            self.next_id += 1
        return Message(
            message_id=getattr(method, "message_id", None) or self.next_id,
            date=timeutils.now(),
            chat=Chat(id=method.chat_id, type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
            text=method.text,
        )

    async def stream_content(self, *args, **kwargs):
        yield b""


class NavigationFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_db = database.db
        database.db = sqlite3.connect(":memory:")
        database.db.row_factory = sqlite3.Row
        database.init_db()
        self.user = User(id=42, is_bot=False, first_name="Test")
        accounts.ensure_user(self.user)
        self.session = PanelSession()
        self.bot = Bot(token="12345:" + "A" * 35, session=self.session)
        self.state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=12345, chat_id=42, user_id=42)
        )

    async def asyncTearDown(self):
        await self.bot.session.close()
        database.db.close()
        database.db = self.old_db

    def message(self, mid, text, *, authored=False):
        return Message(
            message_id=mid,
            date=timeutils.now(),
            chat=Chat(id=42, type="private"),
            from_user=User(id=12345, is_bot=True, first_name="Bot")
            if authored
            else self.user,
            text=text,
        ).as_(self.bot)

    def callback(self, data, mid=701):
        return CallbackQuery(
            id="callback",
            from_user=self.user,
            chat_instance="offline",
            data=data,
            message=self.message(mid, "Menu", authored=True),
        ).as_(self.bot)

    def calls(self, method):
        return [call for call in self.session.calls if call.__api_method__ == method]

    async def test_start_cancel_and_menu_share_one_panel(self):
        await common.start(self.message(1, "/start"), self.state, self.bot)
        await self.state.set_state(GuidedInput.value)
        await common.cancel(self.message(2, "/cancel"), self.state)
        await common.start(self.message(3, "/menu"), self.state, self.bot)
        self.assertEqual(len(self.calls("sendMessage")), 1)
        self.assertEqual(
            [m.message_id for m in self.calls("editMessageText")], [701, 701]
        )
        self.assertEqual([m.message_id for m in self.calls("deleteMessage")], [1, 2, 3])
        self.assertIsNone(await self.state.get_state())

    async def test_home_exits_wizard_without_leaving_stale_input_state(self):
        await self.state.set_state(GuidedInput.value)
        await self.state.set_data({"kind": "button", "step": "label"})

        async def handler(event, data):
            await common.menu_main(event)

        await Guard()(
            handler, self.callback("menu:main"), {"state": self.state, "bot": self.bot}
        )
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.assertEqual(ui.panel_id(42), 701)

    async def test_progress_menu_does_not_adopt_or_overwrite_progress_message(self):
        await self.state.set_state(GuidedInput.value)
        await downloads.progress_menu(
            self.callback("download:menu", mid=55), self.state
        )
        await downloads.progress_menu(
            self.callback("download:menu", mid=55), self.state
        )
        self.assertEqual(len(self.calls("sendMessage")), 1)
        self.assertEqual([m.message_id for m in self.calls("editMessageText")], [701])
        self.assertIsNone(await self.state.get_state())
        self.assertFalse(self.calls("deleteMessage"))

    async def test_post_preview_cleanup_preserves_panel(self):
        await ui.show_panel(self.bot, 42, "Menu")
        pid = content.create_draft(
            42, {"content_type": "text", "text": "Original post"}
        )
        database.execute(
            "UPDATE posts SET preview_ids=? WHERE id=?", (json.dumps([701, 600]), pid)
        )
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=self.message(900, "Preview", authored=True)),
        ):
            await posts.show_post(self.bot, 42, pid)
        self.assertEqual([m.message_id for m in self.calls("deleteMessage")], [600])
        self.assertEqual(ui.panel_id(42), 701)
        self.assertEqual(
            [m.message_id for m in self.calls("editMessageReplyMarkup")], [900, 701]
        )
        self.assertEqual(len(self.calls("sendMessage")), 1)
        controls = self.calls("editMessageReplyMarkup")[0].reply_markup
        self.assertTrue(all(1 <= len(row) <= 3 for row in controls.inline_keyboard))
        actions = {b.callback_data for row in controls.inline_keyboard for b in row}
        self.assertNotIn(f"p:{pid}:edit", actions)
        self.assertIn(f"p:{pid}:text", actions)
        self.assertIn(f"p:{pid}:replace", actions)
        self.assertEqual(
            json.loads(
                database.one("SELECT preview_ids FROM posts WHERE id=?", (pid,))[0]
            ),
            [900],
        )

    async def test_background_post_notification_does_not_replace_active_menu(self):
        await ui.show_panel(self.bot, 42, "Settings")
        pid = content.create_draft(
            42, {"content_type": "text", "text": "Original post"}
        )
        await posts.show_post(self.bot, 42, pid, False, panel=False)
        self.assertEqual(ui.panel_id(42), 701)
        self.assertEqual(len(self.calls("sendMessage")), 2)
        self.assertFalse(self.calls("editMessageText"))

    async def test_background_target_picker_preserves_menu(self):
        await ui.show_panel(self.bot, 42, "Settings")
        pid = content.create_draft(
            42, {"content_type": "text", "text": "Original post"}
        )
        await ui.send_target_picker(self.bot, 42, pid)
        self.assertEqual(ui.panel_id(42), 701)
        self.assertEqual(len(self.calls("sendMessage")), 2)
        self.assertFalse(self.calls("editMessageText"))

    async def test_interactive_target_picker_reuses_menu(self):
        await ui.show_panel(self.bot, 42, "Menu")
        pid = content.create_draft(
            42, {"content_type": "text", "text": "Original post"}
        )
        await ui.send_target_picker(self.bot, 42, pid, panel=True)
        self.assertEqual(ui.panel_id(42), 701)
        self.assertEqual(len(self.calls("sendMessage")), 1)
        self.assertEqual([m.message_id for m in self.calls("editMessageText")], [701])
