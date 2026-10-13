import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_audio_editor as fixtures

from app import content, ui
from app import database as db
from app.features import posts, published_editor


class PostOptionsTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AudioEditorTests.setUp
    tearDown = fixtures.AudioEditorTests.tearDown
    callback = fixtures.AudioEditorTests.callback
    message = fixtures.AudioEditorTests.message
    draft = fixtures.AudioEditorTests.draft

    async def test_next_and_final_controls(self):
        pid = self.draft()
        editing = [
            b.callback_data
            for row in ui.post_controls(pid).inline_keyboard
            for b in row
        ]
        self.assertIn(f"p:{pid}:options", editing)
        self.assertFalse(
            any(
                f"p:{pid}:{action}" in editing
                for action in ("time", "delete", "protect", "publish")
            )
        )
        final = ui.post_controls(
            pid, confirmation=True, protect_content=True, disable_notification=True
        )
        self.assertEqual(
            [[b.callback_data for b in row] for row in final.inline_keyboard],
            [
                [f"p:{pid}:time", f"p:{pid}:delete"],
                [f"p:{pid}:protect", f"p:{pid}:sound"],
                [f"p:{pid}:edit", f"p:{pid}:publish"],
            ],
        )
        self.assertIn("🔕", final.inline_keyboard[1][1].text)
        with (
            patch.object(posts, "show_post", new=AsyncMock()) as preview,
            patch.object(posts, "execute_publish", new=AsyncMock()) as publish,
        ):
            await posts.post_action(
                self.callback(f"p:{pid}:options"), self.state, self.bot
            )
        self.assertTrue(preview.call_args.kwargs["confirmation"])
        self.assertTrue(preview.call_args.kwargs["reuse_preview"])
        publish.assert_not_awaited()

    async def test_next_and_back_reuse_same_message(self):
        for typ in ("text", "photo", "audio", "album"):
            with self.subTest(typ=typ):
                pid = self.draft(typ)
                if typ == "album":
                    db.execute(
                        "UPDATE posts SET media_json=? WHERE id=?",
                        (
                            json.dumps(
                                [
                                    dict(content_type="photo", file_id="a"),
                                    dict(content_type="photo", file_id="b"),
                                ]
                            ),
                            pid,
                        ),
                    )
                    db.execute(
                        "UPDATE posts SET preview_ids='[75,76,77]' WHERE id=?", (pid,)
                    )
                else:
                    db.execute("UPDATE posts SET preview_ids='[77]' WHERE id=?", (pid,))
                self.bot.edit_message_reply_markup.reset_mock()
                with patch.object(content, "send_content", new=AsyncMock()) as send:
                    await posts.post_action(
                        self.callback(f"p:{pid}:options"), self.state, self.bot
                    )
                    self.assertEqual(
                        self.bot.edit_message_reply_markup.call_args.kwargs[
                            "message_id"
                        ],
                        77,
                    )
                    await posts.post_action(
                        self.callback(f"p:{pid}:edit"), self.state, self.bot
                    )
                    send.assert_not_awaited()
                kwargs = self.bot.edit_message_reply_markup.call_args.kwargs
                self.assertEqual(kwargs["message_id"], 77)
                actions = [
                    b.callback_data
                    for row in kwargs["reply_markup"].inline_keyboard
                    for b in row
                ]
                self.assertIn(f"p:{pid}:options", actions)
                self.assertIn(f"p:{pid}:targets", actions)
                self.assertNotIn(f"p:{pid}:publish", actions)

    async def test_protection_and_sound_toggle_in_drafts(self):
        pid = self.draft()
        db.execute("UPDATE post_targets SET payload_json='{}' WHERE post_id=?", (pid,))
        with patch.object(posts, "show_post", new=AsyncMock()) as preview:
            for action, column in (
                ("protect", "protect_content"),
                ("sound", "disable_notification"),
            ):
                await posts.post_action(
                    self.callback(f"p:{pid}:{action}"), self.state, self.bot
                )
                self.assertEqual(content.post_owned(pid, 42)[column], 1)
                self.assertTrue(preview.call_args.kwargs["confirmation"])
                await posts.post_action(
                    self.callback(f"p:{pid}:{action}"), self.state, self.bot
                )
                self.assertEqual(content.post_owned(pid, 42)[column], 0)
        self.assertIsNone(
            db.one("SELECT payload_json FROM post_targets WHERE post_id=?", (pid,))[
                "payload_json"
            ]
        )

    async def test_silent_text_audio_voice_and_album_delivery(self):
        for typ in ("text", "audio", "voice", "album"):
            method = (
                "send_media_group"
                if typ == "album"
                else "send_message"
                if typ == "text"
                else "send_" + typ
            )
            send = AsyncMock(
                return_value=[
                    SimpleNamespace(message_id=1),
                    SimpleNamespace(message_id=2),
                ]
                if typ == "album"
                else SimpleNamespace(message_id=1)
            )
            bot = SimpleNamespace(**{method: send})
            payload = dict(
                content_type=typ,
                file_id="original",
                text="",
                protect_content=True,
                disable_notification=True,
            )
            if typ == "album":
                payload["media_json"] = json.dumps(
                    [
                        dict(content_type="photo", file_id="a"),
                        dict(content_type="photo", file_id="b"),
                    ]
                )
            await content.send_content(bot, 42, payload, message_thread_id=7)
            self.assertTrue(send.call_args.kwargs["disable_notification"])
            self.assertTrue(send.call_args.kwargs["protect_content"])
            self.assertEqual(send.call_args.kwargs["message_thread_id"], 7)

    async def test_published_protection_control_removed(self):
        markup = published_editor.controls(
            dict(token="token", payload=dict(content_type="text"))
        )
        self.assertNotIn(
            "live:protection:token",
            [b.callback_data for row in markup.inline_keyboard for b in row],
        )

    async def test_cancel_pin_and_green_next(self):
        pid = self.draft()
        rows = [
            [b.callback_data for b in row]
            for row in ui.post_controls(pid).inline_keyboard
        ]
        row = next(row for row in rows if f"p:{pid}:options" in row)
        self.assertEqual(row, [f"p:{pid}:skip", f"p:{pid}:pin", f"p:{pid}:options"])
        buttons = [b for row in ui.post_controls(pid).inline_keyboard for b in row]
        self.assertEqual(sum(b.callback_data == f"p:{pid}:skip" for b in buttons), 1)
        self.assertNotIn("menu:posts", [b.callback_data for b in buttons])
        self.assertEqual(
            next(b for b in buttons if b.callback_data == f"p:{pid}:options").style,
            "success",
        )
