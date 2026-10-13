"""Album previews with an offline Telegram transport and temporary database."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from app import content, database, ui
from app.features import posts


class AlbumPreviewTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def album(self, types):
        items = [
            {
                "content_type": typ,
                "file_id": f"file-{index}",
                "text": "Description" if index == 0 else "",
                "caption_entities_json": (
                    [{"type": "bold", "offset": 0, "length": 11}] if index == 0 else []
                ),
            }
            for index, typ in enumerate(types)
        ]
        return content.create_draft(
            42,
            dict(
                items[0],
                content_type="album",
                media_json=json.dumps(items),
                caption_entities_json=json.dumps(items[0]["caption_entities_json"]),
            ),
        )

    async def test_photo_video_and_mixed_albums_keep_captions_and_controls(self):
        for types in (("photo", "photo"), ("video", "video"), ("photo", "video")):
            with self.subTest(types=types):
                pid = self.album(types)
                self.bot.send_media_group = AsyncMock(
                    return_value=[
                        SimpleNamespace(message_id=101),
                        SimpleNamespace(message_id=102),
                    ]
                )
                self.bot.send_message.reset_mock()
                self.bot.edit_message_reply_markup.reset_mock()
                await posts.show_post(self.bot, 42, pid)
                media = self.bot.send_media_group.await_args.args[1]
                self.assertEqual([item.type for item in media], list(types))
                self.assertEqual([item.media for item in media], ["file-0", "file-1"])
                self.assertEqual(media[0].caption, "Description")
                self.assertEqual(media[0].caption_entities[0].type, "bold")
                self.assertIsNone(media[1].caption)
                self.bot.edit_message_reply_markup.assert_not_awaited()
                self.bot.send_message.assert_awaited_once()
                markup = self.bot.send_message.await_args.kwargs["reply_markup"]
                self.assertTrue(markup.inline_keyboard)
                self.assertEqual(
                    json.loads(content.post_owned(pid, 42)["preview_ids"]),
                    [101, 102, 77],
                )

    async def test_done_callback_sends_controls_and_editor_reuses_companion(self):
        pid = self.album(("photo", "video"))
        self.bot.send_media_group = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=101),
                SimpleNamespace(message_id=102),
            ]
        )
        callback = SimpleNamespace(
            data=f"p:{pid}:toggle:{self.cid}",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            bot=self.bot,
            message=SimpleNamespace(message_id=50),
        )
        with patch.object(ui, "target_picker", new=AsyncMock()):
            await posts.post_action(callback, self.state, self.bot)
        callback.data = f"p:{pid}:preview"
        await posts.post_action(callback, self.state, self.bot)
        markup = self.bot.send_message.await_args.kwargs["reply_markup"]
        actions = {b.callback_data for row in markup.inline_keyboard for b in row}
        for action in ("text", "replace", "buttons", "options"):
            self.assertIn(f"p:{pid}:{action}", actions)
        self.assertEqual(
            json.loads(content.post_owned(pid, 42)["preview_ids"]), [101, 102, 77]
        )
        await posts.show_post(self.bot, 42, pid, button_editor=True, reuse_preview=True)
        self.bot.send_media_group.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(
            self.bot.edit_message_reply_markup.await_args.kwargs["message_id"], 77
        )

    async def test_companion_failure_preserves_album_and_shows_fallback_controls(self):
        pid = self.album(("video", "video"))
        self.bot.send_media_group = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=101),
                SimpleNamespace(message_id=102),
            ]
        )
        self.bot.send_message.side_effect = [
            TelegramBadRequest(
                method=SendMessage(chat_id=42, text="Controls"),
                message="Bad Request: rejected",
            ),
            SimpleNamespace(message_id=103),
        ]
        await posts.show_post(self.bot, 42, pid)
        self.assertEqual(
            json.loads(content.post_owned(pid, 42)["preview_ids"]), [101, 102]
        )
        markup = self.bot.send_message.await_args_list[-1].kwargs["reply_markup"]
        self.assertIn(
            f"p:{pid}:options",
            {b.callback_data for row in markup.inline_keyboard for b in row},
        )

    async def test_legacy_album_preview_rebuilds_with_button_editor(self):
        pid = self.album(("photo", "video"))
        database.execute("UPDATE posts SET preview_ids=? WHERE id=?", ("[90, 91]", pid))
        self.bot.send_media_group = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=101),
                SimpleNamespace(message_id=102),
            ]
        )
        await posts.show_post(self.bot, 42, pid, button_editor=True, reuse_preview=True)
        self.bot.send_media_group.assert_awaited_once()
        markup = self.bot.send_message.await_args.kwargs["reply_markup"]
        self.assertIn(
            f"bw:done:p:{pid}",
            {b.callback_data for row in markup.inline_keyboard for b in row},
        )
        self.assertEqual(
            json.loads(content.post_owned(pid, 42)["preview_ids"]), [101, 102, 77]
        )
