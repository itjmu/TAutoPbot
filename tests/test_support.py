"""Support relay tests with an offline Telegram transport and temporary database."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_audio_editor as fixtures
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import ForwardMessage

from app import database as db
from app import ui
from app.features import common, support


class SupportTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AudioEditorTests.setUp
    tearDown = fixtures.AudioEditorTests.tearDown
    message = fixtures.AudioEditorTests.message
    callback = fixtures.AudioEditorTests.callback

    def relay_bot(self):
        self.bot.forward_message = AsyncMock(
            return_value=SimpleNamespace(message_id=80)
        )
        self.bot.forward_messages = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=80),
                SimpleNamespace(message_id=81),
            ]
        )
        self.bot.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=90))
        self.bot.copy_messages = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        )

    async def test_help_and_command_open_same_section(self):
        with patch.object(ui, "edit", new=AsyncMock()) as edit:
            await common.help_menu(self.callback("settings:help"))
            markup = edit.call_args.args[2]
            self.assertIn(
                "support:write",
                [b.callback_data for row in markup.inline_keyboard for b in row],
            )
            await support.open_callback(self.callback("support:write"), self.state)
        self.assertEqual(await self.state.get_state(), support.Support.writing.state)
        with patch.object(ui, "answer", new=AsyncMock()):
            await support.open_command(self.message(text="/paysupport"), self.state)
        self.assertEqual(await self.state.get_state(), support.Support.writing.state)
        self.assertEqual(
            [
                [b.callback_data for b in row]
                for row in support.navigation().inline_keyboard
            ],
            [["settings:help", "menu:main"]],
        )

    async def test_supported_types_are_forwarded_without_rebuilding_content(self):
        self.relay_bot()
        cases = [
            dict(text="Formatted", entities=[dict(type="bold", offset=0, length=9)]),
            dict(photo=[dict(file_id="f", file_unique_id="u", width=10, height=10)]),
            dict(
                video=dict(
                    file_id="f", file_unique_id="u", width=10, height=10, duration=2
                )
            ),
            dict(audio=dict(file_id="f", file_unique_id="u", duration=2)),
            dict(voice=dict(file_id="f", file_unique_id="u", duration=2)),
            dict(
                video_note=dict(file_id="f", file_unique_id="u", duration=2, length=10)
            ),
            dict(document=dict(file_id="f", file_unique_id="u")),
            dict(
                animation=dict(
                    file_id="f", file_unique_id="u", width=10, height=10, duration=2
                )
            ),
            dict(
                sticker=dict(
                    file_id="f",
                    file_unique_id="u",
                    type="regular",
                    width=10,
                    height=10,
                    is_animated=False,
                    is_video=False,
                )
            ),
            dict(contact=dict(phone_number="123", first_name="Test")),
            dict(location=dict(latitude=1, longitude=2)),
            dict(
                venue=dict(
                    location=dict(latitude=1, longitude=2),
                    title="Place",
                    address="Street",
                )
            ),
            dict(
                poll=dict(
                    id="p",
                    question="Q",
                    options=[dict(text="A", voter_count=0, persistent_id="a")],
                    total_voter_count=0,
                    is_closed=False,
                    is_anonymous=True,
                    type="regular",
                    allows_multiple_answers=False,
                    allows_revoting=False,
                    members_only=False,
                )
            ),
            dict(dice=dict(emoji="🎲", value=1)),
            dict(
                text="Forwarded",
                forward_origin=dict(
                    type="hidden_user", sender_user_name="Original", date=1
                ),
            ),
        ]
        await self.state.set_state(support.Support.writing)
        for values in cases:
            with self.subTest(kind=next(iter(values))):
                await support.receive(self.message(**values), self.state, self.bot)
                self.bot.forward_message.assert_awaited_with(support.ADMIN_ID, 42, 50)
                self.assertEqual(
                    await self.state.get_state(), support.Support.writing.state
                )
        self.bot.copy_message.assert_not_awaited()
        requests = db.all_rows(
            "SELECT value FROM app_settings WHERE key LIKE 'support:request:%'"
        )
        self.assertEqual(len(requests), len(cases))
        self.assertTrue(all(json.loads(r["value"])["user_id"] == 42 for r in requests))

    async def test_reply_is_bound_to_admin_and_original_control(self):
        self.relay_bot()
        support.save("support:request:token", dict(user_id=42, control_id=50))
        callback = self.callback("support:reply:token")
        await support.reply_callback(callback, self.state)
        self.assertIsNone(await self.state.get_state())
        callback.from_user.id = support.ADMIN_ID
        with patch.object(ui, "answer", new=AsyncMock()):
            await support.reply_callback(callback, self.state)
        self.assertEqual(await self.state.get_state(), support.Support.replying.state)
        message = self.message(text="Answer").model_copy(
            update={
                "from_user": self.message().from_user.model_copy(
                    update={"id": support.ADMIN_ID}
                ),
                "chat": self.message().chat.model_copy(update={"id": support.ADMIN_ID}),
            }
        )
        await support.receive(message, self.state, self.bot)
        self.bot.copy_message.assert_awaited_once_with(42, support.ADMIN_ID, 50)
        self.assertIsNone(await self.state.get_state())
        callback.message = self.message().model_copy(update={"message_id": 51})
        await support.reply_callback(callback, self.state)
        self.assertIsNone(await self.state.get_state())

    async def test_failures_and_unsupported_messages_do_not_claim_success(self):
        self.relay_bot()
        await self.state.set_state(support.Support.writing)
        with patch("aiogram.types.Message.answer", new=AsyncMock()):
            await support.receive(
                self.message(text="Protected", has_protected_content=True),
                self.state,
                self.bot,
            )
            await support.receive(
                self.message(new_chat_title="Service"), self.state, self.bot
            )
        self.bot.forward_message.assert_not_awaited()
        self.bot.forward_message.side_effect = TelegramBadRequest(
            method=ForwardMessage(
                chat_id=support.ADMIN_ID, from_chat_id=42, message_id=50
            ),
            message="Cannot forward",
        )
        await support.receive(self.message(text="Failure"), self.state, self.bot)
        self.assertEqual(await self.state.get_state(), support.Support.writing.state)
        self.assertFalse(
            any("✅" in call.args[1] for call in self.bot.send_message.await_args_list)
        )

    async def test_album_grouping_and_persistent_reply_target(self):
        self.relay_bot()
        for replying in (False, True):
            await self.state.set_state(
                support.Support.replying if replying else support.Support.writing
            )
            await self.state.update_data(support_user=43)
            sender = support.ADMIN_ID if replying else 42
            message = self.message(
                photo=[dict(file_id="f", file_unique_id="u", width=10, height=10)],
                media_group_id=str(replying),
            )
            message = message.model_copy(
                update={
                    "chat": message.chat.model_copy(update={"id": sender}),
                    "from_user": message.from_user.model_copy(update={"id": sender}),
                }
            )
            await support.receive(message, self.state, self.bot)
            second = message.model_copy(update={"message_id": 51})
            self.assertTrue(await support.PendingAlbum()(second))
            await support.album_part(second)
            record = json.loads(db.setting(support.album_key(message)))
            record["ready"] = 0
            support.save(support.album_key(message), record)
            await support.flush_albums(self.bot)
            self.assertFalse(db.setting(support.album_key(message)))
            if replying:
                self.bot.copy_messages.assert_awaited_once_with(43, sender, [50, 51])
                self.assertIsNone(await self.state.get_state())
            else:
                self.bot.forward_messages.assert_awaited_once_with(
                    support.ADMIN_ID, sender, [50, 51]
                )
                self.assertEqual(
                    await self.state.get_state(), support.Support.writing.state
                )

    async def test_delivered_reply_does_not_repeat_when_receipt_fails(self):
        self.relay_bot()
        self.bot.send_message.side_effect = TelegramBadRequest(
            method=ForwardMessage(
                chat_id=42, from_chat_id=support.ADMIN_ID, message_id=50
            ),
            message="Receipt unavailable",
        )
        await self.state.set_state(support.Support.replying)
        await self.state.update_data(support_user=43)
        message = self.message(text="Answer")
        message = message.model_copy(
            update={
                "from_user": message.from_user.model_copy(
                    update={"id": support.ADMIN_ID}
                ),
                "chat": message.chat.model_copy(update={"id": support.ADMIN_ID}),
            }
        )
        await support.receive(message, self.state, self.bot)
        self.bot.copy_message.assert_awaited_once()
        self.assertIsNone(await self.state.get_state())
