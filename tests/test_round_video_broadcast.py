import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import Message

from app import content, ui
from app import database as db
from app.features import admin
from services import broadcasts


class RoundVideoBroadcastTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    async def test_round_video_draft_render_send_and_partial_failure(self):
        message = Message.model_validate(
            dict(
                message_id=1,
                date=0,
                chat=dict(id=42, type="private"),
                video_note=dict(
                    file_id="round", file_unique_id="unique", length=240, duration=8
                ),
            )
        )
        payload = content.message_payload(message)
        self.assertEqual(payload["content_type"], "video_note")
        pid = content.create_draft(42, payload, [self.cid])
        row = db.one("SELECT * FROM posts WHERE id=?", (pid,))
        data = content.render_payload(
            row, {"id": self.cid, "default_template_id": None}
        )
        self.bot.send_video_note = AsyncMock(
            return_value=SimpleNamespace(message_id=80)
        )
        markup = ui.kb([[ui.choice("Link", "demo")]])
        await content.send_content(self.bot, 42, data, markup)
        self.assertEqual(
            self.bot.send_video_note.await_args.kwargs["reply_markup"], markup
        )
        self.assertNotIn("caption", self.bot.send_video_note.await_args.kwargs)
        data.update(
            text="Description",
            caption_entities_json=[dict(type="bold", offset=0, length=11)],
            protect_content=True,
        )
        sent = await content.send_content(self.bot, 42, data, markup)
        self.assertEqual(len(sent), 2)
        self.assertTrue(self.bot.send_video_note.await_args.kwargs["protect_content"])
        self.assertTrue(self.bot.send_message.await_args.kwargs["protect_content"])
        self.assertEqual(
            self.bot.send_message.await_args.kwargs["entities"][0]["type"], "bold"
        )
        self.bot.send_message.side_effect = RuntimeError("connection lost")
        with self.assertRaises(content.PartialAlbumError) as failure:
            await content.send_content(self.bot, 42, data, markup)
        self.assertEqual(failure.exception.messages[0].message_id, 80)

    async def test_broadcast_forward_copy_and_albums(self):
        self.bot.forward_message = AsyncMock(
            return_value=SimpleNamespace(message_id=91)
        )
        self.bot.forward_messages = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=91),
                SimpleNamespace(message_id=92),
            ]
        )
        self.bot.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=91))
        self.bot.copy_messages = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=91),
                SimpleNamespace(message_id=92),
            ]
        )
        for forward in (True, False):
            for ids in ([1], [2, 1]):
                with self.subTest(forward=forward, ids=ids):
                    draft = dict(
                        token=f"{forward}-{len(ids)}",
                        chat_id=42,
                        ids=ids,
                        forward=forward,
                    )
                    jid = broadcasts.enqueue(42, draft)
                    await broadcasts.tick(self.bot)
                    method = getattr(
                        self.bot,
                        ("forward" if forward else "copy")
                        + ("_messages" if len(ids) > 1 else "_message"),
                    )
                    self.assertEqual(method.await_count, 4)
                    if len(ids) > 1:
                        self.assertEqual(method.await_args.args[2], [1, 2])
                    self.assertEqual(
                        db.one("SELECT status FROM broadcast_jobs WHERE id=?", (jid,))[
                            0
                        ],
                        "completed",
                    )
                    await broadcasts.tick(self.bot)
                    self.assertEqual(method.await_count, 4)

    async def test_admin_intake_remembers_forward_mode(self):
        message = Message.model_validate(
            dict(
                message_id=1,
                date=0,
                chat=dict(id=42, type="private"),
                from_user=dict(id=42, is_bot=False, first_name="Admin"),
                text="Original",
                forward_origin=dict(
                    type="hidden_user", sender_user_name="Source", date=0
                ),
            )
        )
        with (
            patch.object(admin, "ADMIN_ID", 42),
            patch.object(ui, "answer", AsyncMock()),
        ):
            await admin.broadcast_send(message, self.state, self.bot)
        self.assertTrue(json.loads(db.setting("broadcast_draft:42"))["forward"])
