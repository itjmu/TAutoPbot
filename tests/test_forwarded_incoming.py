import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import Message

from app import database as db
from app import ui
from app.features import common


class ForwardedIncomingTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    async def test_forward_variants_new_and_already_saved_create_post(self):
        origins = [
            dict(
                type="channel",
                chat=dict(id=-1001, type="channel", title="Source"),
                message_id=12,
            ),
            dict(
                type="chat",
                sender_chat=dict(id=-1002, type="supergroup", title="Group"),
            ),
            dict(
                type="user", sender_user=dict(id=50, is_bot=False, first_name="Sender")
            ),
            dict(type="hidden_user", sender_user_name="Hidden"),
        ]
        for origin in origins:
            for legacy in (False, True):
                with self.subTest(kind=origin["type"], legacy=legacy):
                    message = Message.model_validate(
                        dict(
                            message_id=7,
                            date=1790798333,
                            chat=dict(id=42, type="private"),
                            from_user=dict(id=42, is_bot=False, first_name="Owner"),
                            forward_origin=dict(origin, date=1790798333),
                            text="Forwarded text",
                            entities=[dict(type="bold", offset=0, length=9)],
                        )
                    )
                    with patch.object(ui, "show_panel", AsyncMock()):
                        await common.offer_incoming(message, self.bot)
                    row = db.one("SELECT * FROM incoming ORDER BY id DESC LIMIT 1")
                    self.assertEqual(
                        json.loads(row["message_json"])["forward_origin"]["type"],
                        origin["type"],
                    )
                    if legacy:
                        db.execute(
                            "UPDATE incoming SET message_json=? WHERE id=?",
                            (
                                message.model_dump_json(
                                    exclude_none=True, exclude_defaults=True
                                ),
                                row["id"],
                            ),
                        )
                    callback = SimpleNamespace(
                        data=f"in:{row['id']}:post",
                        from_user=SimpleNamespace(id=42),
                        answer=AsyncMock(),
                    )
                    with (
                        patch.object(ui, "send_target_picker", AsyncMock()),
                        patch.object(common.accounts, "use_daily", return_value=True),
                    ):
                        await common.incoming_action(callback, self.bot)
                        await common.incoming_action(callback, self.bot)
                    saved = db.one(
                        "SELECT p.* FROM posts p JOIN incoming i ON p.id=i.post_id WHERE i.id=?",
                        (row["id"],),
                    )
                    self.assertEqual(saved["text"], "Forwarded text")
                    self.assertEqual(
                        json.loads(saved["entities_json"])[0]["type"], "bold"
                    )
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM posts")["n"], 8)
