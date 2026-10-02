import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures

from app import content
from app.features import editors, posts


class ButtonCanvasTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def draft(self, buttons=()):
        return content.create_draft(
            42,
            {
                "content_type": "text",
                "text": "Post",
                "buttons_json": json.dumps(list(buttons)),
            },
        )

    def button(self, bid, row):
        return {
            "id": str(bid),
            "type": "url",
            "text": str(bid),
            "url": "https://example.com",
            "row": row,
        }

    def labels(self, pid):
        return [
            [b.text for b in row]
            for row in editors.button_canvas(42, "p", pid).inline_keyboard
            if not any((b.callback_data or "").startswith("bw:done:") for b in row)
        ]

    async def test_incremental_layout_and_save_without_position_prompt(self):
        pid = self.draft()
        self.assertEqual(self.labels(pid), [["+"]])
        c = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            message=SimpleNamespace(),
            bot=self.bot,
        )
        for bid, row, expected in (
            (1, 1, [["1", "+"], ["+"]]),
            (2, 1, [["1", "2", "+"], ["+"]]),
            (3, 2, [["1", "2", "+"], ["3", "+"], ["+"]]),
        ):
            c.data = "bw:color:token:default"
            await self.state.set_data(
                {
                    "token": "token",
                    "step": "color",
                    "scope": "p",
                    "oid": pid,
                    "placement": row,
                    "button": self.button(bid, 1),
                }
            )
            with patch.object(editors, "button_panel", new=AsyncMock()) as panel:
                await editors.button_wizard(c, self.state)
                panel.assert_awaited_once_with(c, "p", pid)
            self.assertEqual(self.labels(pid), expected)
            self.assertIsNone(await self.state.get_state())

    def test_published_and_canvas_do_not_reflow_four_columns_or_long_labels(self):
        buttons = [self.button(i, 1) for i in range(4)]
        buttons[0]["text"] = "Long label " * 4
        pid = self.draft(buttons)
        self.assertEqual(
            [len(r) for r in editors.button_canvas(42, "p", pid).inline_keyboard],
            [5, 1, 3],
        )
        self.assertEqual(
            [
                len(r)
                for r in content.build_published_markup(buttons, pid).inline_keyboard
            ],
            [4],
        )
        self.assertEqual(
            [
                len(r)
                for r in content.build_published_markup(
                    buttons, pid, preview=True
                ).inline_keyboard
            ],
            [4],
        )

    async def test_edit_preserves_position_and_deletion_refreshes_canvas(self):
        pid = self.draft([self.button(1, 1), self.button(2, 1), self.button(3, 2)])
        c = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            data="bw:color:t:default",
        )
        button = self.button(1, 1)
        button["text"] = "Edited"
        await self.state.set_data(
            {
                "token": "t",
                "step": "color",
                "scope": "p",
                "oid": pid,
                "placement": 1,
                "button": button,
            }
        )
        with patch.object(editors, "button_panel", new=AsyncMock()):
            await editors.button_wizard(c, self.state)
            self.assertEqual(self.labels(pid)[0], ["Edited", "2", "+"])
            c.data = f"bw:del:p:{pid}:2"
            await editors.button_wizard(c, self.state)
        self.assertEqual(self.labels(pid), [["Edited", "+"], ["3", "+"], ["+"]])

    def test_more_than_twenty_buttons_and_platform_boundary(self):
        buttons = [self.button(i, i + 1) for i in range(100)]
        content.validate_buttons(buttons)
        pid = self.draft(buttons)
        self.assertEqual(
            sum(map(len, editors.button_canvas(42, "p", pid).inline_keyboard)), 100
        )
        with self.assertRaises(ValueError):
            content.validate_buttons(buttons + [self.button(101, 101)])
        with self.assertRaises(ValueError):
            content.validate_buttons([self.button(i, 1) for i in range(9)])

    async def test_editor_is_attached_to_actual_preview(self):
        pid = self.draft([self.button(1, 1)])
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=100))
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=101)),
        ) as send:
            await posts.show_post(self.bot, 42, pid, button_editor=True)
        markup = send.await_args.args[3]
        self.assertEqual(
            [[b.text for b in r] for r in markup.inline_keyboard],
            [["1", "+"], ["+"], ["✅ Готово", "⬅️ Назад", "🏠 Меню"]],
        )
        self.bot.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(json.loads(content.post_owned(pid, 42)["preview_ids"]), [101])
        with self.assertRaises(ValueError):
            editors.button_document(43, "p", pid, True)
