import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import Message

from app import content, ui
from app import database as db
from app.features import published_editor as live


class RoundVideoEditorTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def api(self):
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_edit_messages=True)
        )
        return self.bot

    def note(self):
        return Message(
            message_id=12,
            date=0,
            chat=dict(id=42, type="private"),
            from_user=dict(id=42, is_bot=False, first_name="U"),
            video_note=dict(
                file_id="note", file_unique_id="unique", length=240, duration=3
            ),
            forward_origin=dict(
                type="channel",
                date=0,
                chat=dict(id=-10077, type="channel", title="Channel"),
                message_id=456,
            ),
            reply_markup=dict(
                inline_keyboard=[[dict(text="Link", url="https://example.com")]]
            ),
        )

    async def test_round_note_buttons_edit_original_without_caption_or_repost(self):
        bot = self.api()
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ):
            await live.original(self.note(), self.state, bot)
        data = await self.state.get_data()
        self.assertEqual(data["payload"]["content_type"], "video_note")
        controls = str(live.controls(data))
        self.assertNotIn("live:text:", controls)
        self.assertNotIn("live:templates:", controls)
        data["buttons"][0]["text"] = "Updated"
        bot.send_message.reset_mock()
        bot.edit_message_reply_markup.reset_mock()
        await live.apply_edit(bot, 42, data)
        call = bot.edit_message_reply_markup.await_args.kwargs
        self.assertEqual((call["chat_id"], call["message_id"]), (-10077, 456))
        self.assertEqual(call["reply_markup"].inline_keyboard[0][0].text, "Updated")
        bot.edit_message_caption.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_clear_all_direct_and_undo(self):
        bot = self.api()
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ):
            await live.original(self.note(), self.state, bot)
        data = await self.state.get_data()
        await self.state.update_data(unknown_markup=True)
        c = SimpleNamespace(
            data=f"live:clearbuttons:{data['token']}",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
        )
        with patch.object(ui, "edit", new=AsyncMock()) as confirm:
            await live.action(c, self.state, bot)
        confirm.assert_not_awaited()
        cleared = await self.state.get_data()
        self.assertEqual(cleared["buttons"], [])
        self.assertFalse(cleared["unknown_markup"])
        await live.apply_edit(bot, 42, dict(cleared))
        self.assertIsNone(
            bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        )
        c.data = f"live:undo:{data['token']}"
        await live.action(c, self.state, bot)
        self.assertTrue((await self.state.get_data())["buttons"])
        self.assertTrue((await self.state.get_data())["unknown_markup"])

    async def test_legacy_round_note_and_companion_links(self):
        pid = content.create_draft(
            42,
            dict(content_type="video_note", file_id="note", text="Description"),
            [self.cid],
        )
        db.execute(
            "UPDATE post_targets SET payload_json=? WHERE post_id=?",
            (
                json.dumps(
                    dict(content_type="video_note", file_id="note", text="Description")
                ),
                pid,
            ),
        )
        for mid in (101, 102):
            db.execute(
                "INSERT INTO published_messages(post_id,channel_id,telegram_message_id) VALUES(?,?,?)",
                (pid, self.cid, mid),
            )
        _, note = live.saved_original(42, self.cid, 101)
        _, companion = live.saved_original(42, self.cid, 102)
        self.assertEqual((note["content_type"], note["text"]), ("video_note", ""))
        self.assertEqual(
            (companion["content_type"], companion["text"]), ("text", "Description")
        )

    def test_new_note_snapshot_has_no_caption(self):
        snapshot = content.delivered_payload(
            dict(content_type="video_note", file_id="note", text="Description"), 0
        )
        self.assertEqual(snapshot["text"], "")


if __name__ == "__main__":
    unittest.main()
