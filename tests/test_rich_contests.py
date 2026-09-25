"""Formatting survives saved contests, media and Telegram template conversion."""

import json
import unittest
from unittest.mock import AsyncMock, patch

import test_contest_form as fixtures
from aiogram.types import MessageEntity

from app import content, ui
from app import database as db
from app.features import contests
from services import contests as service


class RichContestTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ContestFormTests.setUp
    tearDown = fixtures.ContestFormTests.tearDown
    callback = fixtures.ContestFormTests.callback

    async def test_compact_panel_and_sections(self):
        c = self.callback("contest:new")
        with (
            patch.object(ui, "edit", new=AsyncMock()) as edit,
            patch.object(ui, "answer", new=AsyncMock()) as answer,
        ):
            await contests.new(c, self.state)
            c.data = f"contest:select:{self.cid}"
            await contests.select_channel(c, self.state)
            await contests.selected_channels(c, self.state)
            self.assertEqual((await self.state.get_data())["step"], "prize_title")
            await contests.accept(c.message, self.state, self.bot, "Camera")
            rows = answer.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual(sum(map(len, rows)), 7)
            for section in ("prize", "rules", "dates", "post"):
                c.data = f"contest:section:{section}"
                await contests.form_section(c, self.state)
                self.assertEqual(
                    edit.await_args.args[2].inline_keyboard[-1][0].callback_data,
                    "contest:panel",
                )

    async def test_saved_form_preserves_formatted_fields_in_media_publication(self):
        c = self.callback("contest:new")
        with (
            patch.object(ui, "edit", new=AsyncMock()),
            patch.object(ui, "answer", new=AsyncMock()),
        ):
            await contests.new(c, self.state)
            c.data = f"contest:select:{self.cid}"
            await contests.select_channel(c, self.state)
            await contests.selected_channels(c, self.state)
            for field, raw, entity in [
                (
                    "prize_title",
                    "  🎁 Camera  ",
                    MessageEntity(type="bold", offset=2, length=9),
                ),
                (
                    "prize_description",
                    "Details",
                    MessageEntity(type="expandable_blockquote", offset=0, length=7),
                ),
                (
                    "quiz",
                    "Question\nSecret",
                    MessageEntity(type="italic", offset=0, length=15),
                ),
            ]:
                c.message.entities = [entity]
                await self.state.update_data(step=field)
                await contests.accept(c.message, self.state, self.bot, raw)
            await self.state.update_data(step="media")
            await contests.accept(
                c.message,
                self.state,
                self.bot,
                "",
                {"content_type": "photo", "file_id": "photo"},
            )
            await contests.menu(c, self.state)
            await contests.resume(c, self.state)
            await contests.create(c, self.state)
        row = db.one("SELECT * FROM contests")
        post = service.publication(row)
        encoded = post["text"].encode("utf-16-le")
        fragments = {
            e["type"]: encoded[
                e["offset"] * 2 : (e["offset"] + e["length"]) * 2
            ].decode("utf-16-le")
            for e in post["caption_entities_json"]
        }
        self.assertEqual(fragments["bold"], "🎁 Camera")
        self.assertEqual(fragments["expandable_blockquote"], "Details")
        self.assertEqual(fragments["italic"], "Question")
        self.assertNotIn("Secret", json.dumps(post))

    def test_template_telegram_formats(self):
        raw = '<blockquote expandable>Quote</blockquote><tg-emoji emoji-id="123">😀</tg-emoji><pre><code class="language-python">print(1)</code></pre><a href="tg://user?id=42">Name</a>'
        text, entities = content.parse_template_html(raw)
        self.assertEqual(text, "Quote😀print(1)Name")
        by_type = {e["type"]: e for e in entities}
        self.assertEqual(by_type["custom_emoji"]["length"], 2)
        self.assertEqual(by_type["pre"]["language"], "python")
        self.assertNotIn("code", by_type)
        self.assertIn("expandable_blockquote", by_type)
        self.assertEqual(by_type["text_mention"]["user"]["id"], 42)
