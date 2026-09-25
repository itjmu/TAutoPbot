"""Offline checks for the private chat's reusable navigation panel."""

import asyncio
import os
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ["DB_FILE"] = ":memory:"
os.environ["BOT_TOKEN"] = ""
os.environ["ADMIN_ID"] = "12345"

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText

from app import database, ui


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_db = database.db
        database.db = sqlite3.connect(":memory:")
        database.db.row_factory = sqlite3.Row
        database.db.execute(
            "CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT)"
        )
        self.bot = SimpleNamespace(
            id=1,
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
            edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
            delete_message=AsyncMock(),
        )

    def tearDown(self):
        database.db.close()
        database.db = self.old_db

    def message(self, uid=42, message_id=50, text="/start", bot_authored=False):
        return SimpleNamespace(
            bot=self.bot,
            chat=SimpleNamespace(id=uid, type="private"),
            from_user=SimpleNamespace(
                id=1 if bot_authored else uid, is_bot=bot_authored
            ),
            message_id=message_id,
            text=text,
            answer=AsyncMock(),
            edit_text=AsyncMock(),
        )

    def remember(self, uid=42, message_id=101):
        database.execute(
            "INSERT OR REPLACE INTO app_settings VALUES(?,?)",
            (f"ui:panel:{uid}", str(message_id)),
        )

    def edit_failure(self, message):
        return TelegramBadRequest(
            method=EditMessageText(chat_id=42, message_id=101, text="Menu"),
            message=message,
        )

    async def test_repeated_answers_reuse_panel_and_persist_message_id(self):
        first = self.message()
        second = self.message(message_id=60, text="Settings")

        await ui.answer(first, "Main menu", reply_markup=ui.main_kb())
        await ui.answer(second, "Settings", reply_markup=ui.back())

        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["chat_id"], 42)
        self.assertEqual(
            self.bot.edit_message_text.await_args.kwargs["message_id"], 101
        )
        self.assertEqual(ui.panel_id(42), 101)
        self.assertEqual(database.setting("ui:panel:42"), "101")
        first.answer.assert_not_awaited()
        second.answer.assert_not_awaited()

    async def test_durable_panel_is_used_without_prior_call_in_process(self):
        self.remember(message_id=77)

        result = await ui.show_panel(self.bot, 42, "Restored menu")

        self.assertEqual(result, 77)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 77)
        self.bot.send_message.assert_not_awaited()

    async def test_users_have_independent_panels(self):
        self.bot.send_message.side_effect = [
            SimpleNamespace(message_id=101),
            SimpleNamespace(message_id=202),
        ]

        await ui.show_panel(self.bot, 42, "First user")
        await ui.show_panel(self.bot, 43, "Second user")
        await ui.show_panel(self.bot, 42, "First user settings")

        self.assertEqual(ui.panel_id(42), 101)
        self.assertEqual(ui.panel_id(43), 202)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["chat_id"], 42)
        self.assertEqual(
            self.bot.edit_message_text.await_args.kwargs["message_id"], 101
        )

    async def test_simultaneous_first_updates_create_only_one_panel(self):
        async def delayed_send(*args, **kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(message_id=101)

        self.bot.send_message.side_effect = delayed_send

        results = await asyncio.gather(
            ui.show_panel(self.bot, 42, "Main menu"),
            ui.show_panel(self.bot, 42, "Settings"),
        )

        self.assertEqual(results, [101, 101])
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_awaited_once()

    async def test_existing_bot_text_can_be_adopted_for_callback(self):
        anchor = self.message(message_id=77, text="Older menu", bot_authored=True)
        callback = SimpleNamespace(
            bot=self.bot, message=anchor, from_user=SimpleNamespace(id=42)
        )

        await ui.edit(callback, "Updated menu", ui.back())

        self.assertEqual(ui.panel_id(42), 77)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 77)
        self.bot.send_message.assert_not_awaited()

    async def test_stored_panel_wins_over_another_text_anchor(self):
        self.remember(message_id=101)
        anchor = self.message(message_id=77, text="Older menu", bot_authored=True)

        result = await ui.show_panel(self.bot, 42, "Main menu", anchor=anchor)

        self.assertEqual(result, 101)
        self.assertEqual(
            self.bot.edit_message_text.await_args.kwargs["message_id"], 101
        )
        anchor.edit_text.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_media_anchor_is_never_edited_or_adopted(self):
        anchor = self.message(message_id=77, text=None, bot_authored=True)

        result = await ui.show_panel(self.bot, 42, "Main menu", anchor=anchor)

        self.assertEqual(result, 101)
        self.assertEqual(ui.panel_id(42), 101)
        self.bot.edit_message_text.assert_not_awaited()
        anchor.edit_text.assert_not_awaited()
        self.bot.delete_message.assert_not_awaited()

    async def test_user_text_anchor_is_never_adopted(self):
        result = await ui.show_panel(self.bot, 42, "Main menu", anchor=self.message())

        self.assertEqual(result, 101)
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_not_awaited()

    async def test_bot_authored_message_uses_private_chat_as_recipient(self):
        message = self.message(bot_authored=True)

        await ui.answer(message, "Next wizard step")

        self.assertEqual(ui.panel_id(42), 50)
        self.assertIsNone(ui.panel_id(1))

    async def test_home_footer_keeps_step_buttons_without_mutating_markup(self):
        markup = ui.kb([[ui.choice("Next", "bw:next")]])
        await ui.show_panel(self.bot, 42, "Wizard", markup)
        sent = self.bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(sent.inline_keyboard[0][0].callback_data, "bw:next")
        self.assertEqual(sent.inline_keyboard[-1][0].callback_data, "menu:main")
        self.assertEqual(len(markup.inline_keyboard), 1)

    async def test_main_and_existing_home_keyboards_are_not_duplicated(self):
        for markup in (ui.main_kb(), ui.back()):
            self.assertEqual(ui.panel_markup(markup), markup)

    async def test_not_modified_does_not_send_duplicate(self):
        self.remember()
        self.bot.edit_message_text.side_effect = self.edit_failure(
            "Bad Request: message is not modified"
        )

        result = await ui.show_panel(self.bot, 42, "Same menu")

        self.assertEqual(result, 101)
        self.assertEqual(ui.panel_id(42), 101)
        self.bot.send_message.assert_not_awaited()

    async def test_deleted_or_uneditable_panel_is_replaced(self):
        for message in ("message to edit not found", "message can't be edited"):
            with self.subTest(message=message):
                self.remember(message_id=77)
                self.bot.send_message.reset_mock()
                self.bot.edit_message_text.side_effect = self.edit_failure(message)

                result = await ui.show_panel(self.bot, 42, "Recovered menu")

                self.assertEqual(result, 101)
                self.assertEqual(ui.panel_id(42), 101)
                self.bot.send_message.assert_awaited_once()

    async def test_parse_and_network_errors_do_not_create_duplicates(self):
        method = EditMessageText(chat_id=42, message_id=101, text="Menu")
        failures = (
            self.edit_failure("Bad Request: can't parse entities"),
            TelegramNetworkError(method=method, message="Connection lost"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.remember()
                self.bot.edit_message_text.side_effect = failure

                with self.assertRaises(type(failure)):
                    await ui.show_panel(self.bot, 42, "Menu")

                self.assertEqual(ui.panel_id(42), 101)
                self.bot.send_message.assert_not_awaited()

    async def test_nonprivate_and_unmounted_messages_keep_answer_behavior(self):
        public = self.message(uid=-1001)
        public.chat.type = "supergroup"
        unmounted = SimpleNamespace(answer=AsyncMock())

        for message in (public, unmounted):
            with self.subTest(message=message):
                await ui.answer(message, "Status", reply_markup=ui.back())

                message.answer.assert_awaited_once()
                self.assertEqual(message.answer.await_args.args[0], "Status")

        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
