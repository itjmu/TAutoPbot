import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures

from app import content
from app.features import editors


class BulkButtonsTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def test_pairs_blank_rows_and_links(self):
        buttons = editors.parse_bulk_buttons(
            "One\nhttps://example.com/a|b\nTwo\n@telegram\n\n \nThree\nhttps://example.org",
            [],
            1,
        )
        self.assertEqual([b["row"] for b in buttons], [1, 1, 2])
        self.assertTrue(all(b["type"] == "url" for b in buttons))
        self.assertEqual(buttons[1]["url"], "https://t.me/telegram")

    def test_wrap_preserves_existing_rows(self):
        existing = [
            dict(id=str(i), row=1, type="reaction", text=str(i)) for i in range(7)
        ]
        existing.append(dict(id="last", row=2, type="reaction", text="Last"))
        result = editors.parse_bulk_buttons(
            "\n".join(f"{i}\nhttps://example.com" for i in range(12)), existing, 1
        )
        self.assertEqual([b["row"] for b in result[8:]], [1] + [2] * 8 + [3] * 3)
        self.assertEqual(result[7]["row"], 4)
        self.assertEqual(existing[7]["row"], 2)
        content.validate_buttons(result)

    def test_invalid_input_and_total_limit(self):
        for raw in (
            "",
            "Missing",
            "Label\n\nvalue",
            "a" * 51 + "\nreaction",
            "Name\nalert:",
            "Name\nhttps://",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                editors.parse_bulk_buttons(raw, [], 1)
        with self.assertRaises(ValueError):
            editors.parse_bulk_buttons(
                "\n".join(f"{i}\nhttps://example.com" for i in range(101)), [], 1
            )

    async def test_save_and_invalid_batch_are_atomic(self):
        pid = content.create_draft(42, {"content_type": "text", "text": "Post"})
        await editors.wizard_start(
            self.state, dict(kind="buttons_bulk", scope="p", oid=pid, placement=1)
        )
        m = SimpleNamespace(from_user=SimpleNamespace(id=42), bot=self.bot)
        with self.assertRaises(ValueError):
            await editors.guided_value(
                m, self.state, self.bot, "A\nhttps://example.com\nBroken", 42
            )
        self.assertEqual(editors.button_document(42, "p", pid), [])
        self.assertIsNotNone(await self.state.get_state())
        with patch.object(editors, "button_panel", new=AsyncMock()) as panel:
            await editors.guided_value(
                m, self.state, self.bot, "A\nhttps://example.com\n\nB\n@telegram", 42
            )
            panel.assert_awaited_once_with(m, "p", pid)
        self.assertEqual(
            [b["row"] for b in editors.button_document(42, "p", pid)], [1, 2]
        )
        self.assertIsNone(await self.state.get_state())

    async def test_rejects_subscription_and_wrong_owner(self):
        pid = content.create_draft(42, {"content_type": "text", "text": "Post"})
        await editors.wizard_start(
            self.state, dict(kind="buttons_bulk", scope="p", oid=pid, placement=1)
        )
        m = SimpleNamespace(from_user=SimpleNamespace(id=42), bot=self.bot)
        self.bot.get_chat = AsyncMock(
            return_value=SimpleNamespace(id=-1001, username="telegram", title="Channel")
        )
        raw = "Join\nsubscription:@telegram|Secret"
        with self.assertRaises(ValueError):
            await editors.guided_value(m, self.state, self.bot, raw, 43)
        with self.assertRaises(ValueError):
            await editors.guided_value(m, self.state, self.bot, raw, 42)
        self.assertEqual(editors.button_document(42, "p", pid), [])
        self.bot.get_chat.assert_not_awaited()

    def test_rejects_all_non_link_actions(self):
        for value in (
            "reaction",
            "реакция",
            "Hello",
            "alert:Hello",
            "subscription:@telegram|Secret",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                editors.parse_bulk_buttons("Label\n" + value, [], 1)
