"""Launch acceptance tests: giveaways, entitlements and safe maintenance."""

import html
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.types import LinkPreviewOptions, Message

import manage
from app import accounts, content, plans, preferences, timeutils, ui
from app import database as db
from app.features import admin, channels, common, contests, editors
from app.middleware import Guard
from app.scheduler import RuntimeLock
from services import contests as service


class LaunchTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown
    contest = fixtures.PlanningTests.contest
    post = fixtures.PlanningTests.post

    async def test_link_with_default_preview_offers_all_actions_and_restores(self):
        message = Message(
            message_id=987,
            date=0,
            chat={"id": 42, "type": "private"},
            from_user={"id": 42, "is_bot": False, "first_name": "User"},
            text="https://vt.tiktok.com/ZSmXc2S7D/",
            link_preview_options=LinkPreviewOptions(),
        )
        await common.offer_incoming(message, self.bot)
        saved = db.one("SELECT * FROM incoming WHERE owner_id=42")
        restored = Message.model_validate_json(saved["message_json"])
        self.assertEqual(restored.text, message.text)
        self.assertEqual(json.loads(saved["links_json"]), [message.text])
        markup = self.bot.send_message.await_args.kwargs["reply_markup"]
        actions = {
            b.callback_data.split(":")[-1]
            for row in markup.inline_keyboard
            for b in row
        }
        self.assertEqual(actions, {"post", "links", "skip", "main"})

    async def test_channel_picker_uses_two_bottom_buttons(self):
        c = SimpleNamespace(
            bot=self.bot,
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(answer=AsyncMock()),
            answer=AsyncMock(),
        )
        with (
            patch.object(
                channels.access, "access_callback", new=AsyncMock(return_value=True)
            ),
            patch.object(ui, "edit", new=AsyncMock()) as edit,
        ):
            await channels.channel_picker(c, self.state)
        keyboard = c.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [b.request_chat.request_id for b in keyboard.keyboard[0]], [701, 702]
        )
        self.assertEqual(len(edit.await_args.args[2].inline_keyboard), 1)
        await channels.dismiss_picker(self.bot, 42)
        self.assertTrue(
            self.bot.send_message.await_args.kwargs["reply_markup"].remove_keyboard
        )
        self.assertFalse(channels.database.setting("channel_setup:42"))

    async def test_shared_private_channel_connects_without_membership_update(self):
        chat = SimpleNamespace(
            id=-100987,
            type="channel",
            title="Private",
            username=None,
            invite_link="https://t.me/+private",
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            chat_shared=SimpleNamespace(chat_id=chat.id),
            answer=AsyncMock(),
        )
        self.bot.get_chat = AsyncMock(return_value=chat)
        with patch.object(
            channels.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
        ):
            await channels.channel_shared(message, self.state, self.bot)
            await channels.channel_shared(message, self.state, self.bot)
        self.assertEqual(
            db.one(
                "SELECT COUNT(*) FROM channels WHERE telegram_chat_id=?", (chat.id,)
            )[0],
            1,
        )
        self.assertIsNone(await self.state.get_state())
        self.assertTrue(
            message.answer.await_args_list[0].kwargs["reply_markup"].remove_keyboard
        )

    async def test_shared_channel_rejects_missing_admin_rights(self):
        chat = SimpleNamespace(
            id=-100987, type="channel", title="Private", username=None
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            chat_shared=SimpleNamespace(chat_id=chat.id),
            answer=AsyncMock(),
        )
        self.bot.get_chat = AsyncMock(return_value=chat)
        with patch.object(
            channels.access, "owner_and_bot_ok", new=AsyncMock(return_value=False)
        ):
            with self.assertRaises(ValueError):
                await channels.channel_shared(message, self.state, self.bot)
        self.assertIsNone(
            db.one("SELECT id FROM channels WHERE telegram_chat_id=?", (chat.id,))
        )

    def callback(self, cid, uid=43):
        return SimpleNamespace(
            data=f"contest:participate:{cid}",
            from_user=SimpleNamespace(
                id=uid,
                username="player",
                first_name="Player",
                last_name=None,
                is_bot=False,
            ),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-1001, type="channel"), message_id=77
            ),
            answer=AsyncMock(),
        )

    async def test_both_creation_flows_require_prize_count_end_and_contact(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=42), answer=AsyncMock())
        c = SimpleNamespace(
            data="",
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        for kind, mode in (("contest", "ranking"), ("raffle", "weighted")):
            await self.state.set_data({"giveaway_type": kind, "launch_v4": True})
            c.data = f"contest:target:{self.cid}"
            with patch.object(
                contests,
                "subscription_link",
                new=AsyncMock(return_value="https://t.me/testchannel"),
            ):
                await contests.target(c, self.state, self.bot)
            for step, value in (
                ("prize_title", "A book"),
                ("post_style", "template"),
                ("winners", "3"),
                ("end", "day"),
                ("contact", "@organizer"),
            ):
                self.assertEqual((await self.state.get_data())["step"], step)
                await contests.accept(message, self.state, self.bot, value)
            data = await self.state.get_data()
            self.assertEqual(data["step"], "review")
            self.assertEqual(data["mode"], mode)
            with patch.object(ui, "edit", new=AsyncMock()):
                await contests.create(c, self.state)
            saved = db.one("SELECT * FROM contests ORDER BY id DESC LIMIT 1")
            self.assertEqual(saved["giveaway_type"], kind)
            self.assertEqual(saved["claim_contact"], "@organizer")
            self.assertEqual(saved["winner_count"], 3)
            self.assertEqual(len(json.loads(saved["subscriptions_json"])), 1)

    async def test_public_subscriptions_join_in_place_and_are_idempotent(self):
        cid = self.contest(
            subscriptions_json=json.dumps(
                [{"chat_id": -1001, "title": "Channel", "url": "https://t.me/channel"}]
            )
        )
        c = self.callback(cid)
        await contests.participate(c, self.bot)
        await contests.participate(c, self.bot)
        self.assertEqual(service.participant_count(cid), 1)
        self.assertTrue(c.answer.await_args.kwargs["show_alert"])
        self.assertIn("участвуете", c.answer.await_args.args[0])
        self.bot.send_message.assert_not_awaited()
        markup = self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        self.assertIn("1", markup.inline_keyboard[-1][0].text)
        self.assertIsNone(markup.inline_keyboard[-1][0].url)

    async def test_missing_subscription_is_an_alert_not_a_private_redirect(self):
        cid = self.contest(
            subscriptions_json=json.dumps(
                [
                    {
                        "chat_id": -1001,
                        "title": "Required channel",
                        "url": "https://t.me/channel",
                    }
                ]
            )
        )
        self.bot.get_chat_member.return_value = SimpleNamespace(status="left")
        c = self.callback(cid)
        await contests.participate(c, self.bot)
        self.assertIn("Required channel", c.answer.await_args.args[0])
        self.assertEqual(service.participant_count(cid), 0)
        self.bot.send_message.assert_not_awaited()

    async def test_captcha_requires_private_completion_before_counting(self):
        cid = self.contest(captcha=1)
        c = self.callback(cid)
        await contests.participate(c, self.bot)
        self.assertEqual(service.participant_count(cid), 0)
        self.assertIn("капчу", c.answer.await_args.args[0])
        db.execute("UPDATE contest_entries SET captcha_passed=1")
        await contests.participate(c, self.bot)
        self.assertEqual(service.participant_count(cid), 1)

    async def test_minimum_referrals_changes_eligible_count(self):
        cid = self.contest(referral_min=1)
        service.register(cid, 42)
        service.register(cid, 43, 42)
        db.execute("UPDATE contest_entries SET base_valid=1")
        self.assertEqual(service.participant_count(cid), 1)
        self.assertEqual(service.referral_count(cid, 42), 1)

    async def test_channel_referrals_are_deduplicated_and_leavers_removed(self):
        cid = self.contest(referral_target="channel", referral_min=1)
        service.register(cid, 42)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute(
            "INSERT INTO contest_invites VALUES(?,?,?)", (cid, 42, "https://t.me/+ref")
        )
        member = SimpleNamespace(
            status="member", user=SimpleNamespace(id=43, is_bot=False)
        )
        left = SimpleNamespace(status="left", user=member.user)
        event = SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            old_chat_member=left,
            new_chat_member=member,
            invite_link=SimpleNamespace(invite_link="https://t.me/+ref"),
        )
        await contests.channel_referral(event, self.bot)
        await contests.channel_referral(event, self.bot)
        self.assertEqual(service.referral_count(cid, 42), 1)
        self.assertEqual(service.participant_count(cid), 1)
        event.old_chat_member, event.new_chat_member = member, left
        await contests.channel_referral(event, self.bot)
        self.assertEqual(service.referral_count(cid, 42), 0)

    async def test_result_edits_same_media_post_and_notifies_creator(self):
        cid = self.contest(
            mode="weighted",
            claim_contact="@claim",
            post_json=json.dumps(
                {"content_type": "photo", "file_id": "file", "text": "Giveaway"}
            ),
        )
        service.register(cid, 43)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute("UPDATE contests SET status='closing' WHERE id=?", (cid,))
        await service.finish(self.bot, service.get(cid))
        await service.deliver(self.bot)
        edit = self.bot.edit_message_caption.await_args.kwargs
        self.assertEqual((edit["chat_id"], edit["message_id"]), (-1001, 77))
        self.assertIn("@claim", edit["caption"])
        self.assertIn("tg://user?id=43", edit["caption"])
        self.assertEqual(edit["reply_markup"].inline_keyboard, [])
        self.assertTrue(
            any(call.args[0] == 42 for call in self.bot.send_message.await_args_list)
        )
        self.assertFalse(
            any(call.args[0] < 0 for call in self.bot.send_message.await_args_list)
        )
        await service.deliver(self.bot)
        self.bot.edit_message_caption.assert_awaited_once()

    async def test_result_edit_retries_without_reroll(self):
        cid = self.contest()
        service.register(cid, 43)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute("UPDATE contests SET status='closing' WHERE id=?", (cid,))
        await service.finish(self.bot, service.get(cid))
        self.bot.edit_message_text.side_effect = [
            TelegramNetworkError(
                method=EditMessageText(text="x", chat_id=-1001, message_id=77),
                message="connection lost",
            ),
            True,
        ]
        await service.deliver(self.bot)
        self.assertEqual(
            db.one("SELECT status FROM contest_outbox WHERE kind='results'")[0],
            "pending",
        )
        with patch.object(service, "choose", side_effect=AssertionError("No reroll")):
            await service.deliver(self.bot)
        self.assertEqual(
            db.one("SELECT status FROM contest_outbox WHERE kind='results'")[0], "sent"
        )

    async def test_limits_and_trials(self):
        for _ in range(5):
            self.assertTrue(accounts.use_daily(42, "download_video"))
        self.assertFalse(accounts.use_daily(42, "download_video"))
        for _ in range(3):
            self.assertTrue(accounts.use_daily(42, "cover"))
        self.assertFalse(accounts.use_daily(42, "cover"))
        self.assertFalse(accounts.start_trial(42))
        self.assertFalse(accounts.has_premium(42))
        accounts.activate_premium(43, 30)
        self.assertTrue(accounts.use_daily(43, "download_video", 20))
        self.assertFalse(accounts.use_daily(43, "download_video"))
        self.assertTrue(accounts.use_daily(43, "cover", 200))
        preferences.save(42, timezone="UTC+12:00")
        self.assertFalse(accounts.use_daily(42, "download_video"))

    async def test_daily_reservation_refund_and_plan_change(self):
        self.assertTrue(accounts.use_daily(42, "download_video", 5))
        accounts.refund_daily(42, "download_video", timeutils.now().date().isoformat())
        self.assertTrue(accounts.use_daily(42, "download_video"))
        plans.set_value("free_download_video", 6, 12345)
        self.assertTrue(accounts.use_daily(42, "download_video"))
        self.assertFalse(accounts.use_daily(42, "download_video"))
        with self.assertRaises(ValueError):
            plans.set_value("premium_button_colors", 4, 12345)

    async def test_plan_settings_reject_non_admin(self):
        c = SimpleNamespace(
            from_user=SimpleNamespace(id=42), data="plan:edit:free_cover"
        )
        with self.assertRaises(ValueError):
            await admin.plan_edit(c, self.state)

    async def test_button_colors_saved_rendered_and_downgraded(self):
        pid = self.post()
        button = {
            "id": "b1",
            "type": "url",
            "text": "Go",
            "url": "https://example.com",
            "row": 1,
            "style": "danger",
        }
        with self.assertRaises(ValueError):
            editors.save_buttons(42, "p", pid, [button])
        accounts.activate_premium(42, 30)
        editors.save_buttons(42, "p", pid, [button])
        markup = content.build_published_markup([button], pid)
        self.assertEqual(markup.inline_keyboard[0][0].style, "danger")
        with db.atomic():
            accounts.premium_change_tx(42, "revoke", None, "test")
        row = db.one("SELECT * FROM channels WHERE id=?", (self.cid,))
        rendered = content.render_payload(content.post_owned(pid, 42), row)
        self.assertIsNone(rendered["buttons"][0]["style"])
        self.assertEqual(accounts.button_styles(42), [None, "primary", "success"])

    async def test_subscription_text_preserves_entities(self):
        cid = self.contest(
            subscription_layout="text",
            subscriptions_json=json.dumps(
                [{"chat_id": -1001, "title": "Channel", "url": "https://t.me/channel"}]
            ),
        )
        post = service.publication(service.get(cid))
        self.assertIn("Channel", post["text"])
        self.assertEqual(post["entities_json"][-1]["url"], "https://t.me/channel")
        self.assertEqual(
            len(service.public_markup(service.get(cid)).inline_keyboard), 1
        )

    async def test_public_handler_allowed_by_middleware(self):
        from aiogram.types import CallbackQuery, Chat, Message, User

        cid = self.contest()
        event = CallbackQuery(
            id="cb",
            chat_instance="chat",
            from_user=User(id=43, is_bot=False, first_name="P"),
            data=f"contest:participate:{cid}",
            message=Message(
                message_id=77, date=timeutils.now(), chat=Chat(id=-1001, type="channel")
            ),
        )
        handler = AsyncMock()
        await Guard()(handler, event, {"bot": self.bot, "state": self.state})
        handler.assert_awaited_once()

    async def test_recovery_requires_original_message_id(self):
        cid = self.contest(status="uncertain", published_ids="[]")
        c = SimpleNamespace(
            data=f"contest:recover:{cid}:sent",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
        )
        with patch.object(ui, "edit", new=AsyncMock()):
            await contests.recover_publication(c, self.state)
        self.assertEqual(service.get(cid)["status"], "uncertain")
        message = SimpleNamespace(
            from_user=c.from_user, forward_origin=None, text="456", answer=AsyncMock()
        )
        await contests.recover_message(message, self.state)
        self.assertEqual(json.loads(service.get(cid)["published_ids"]), [456])
        self.assertEqual(service.get(cid)["status"], "active")

    async def test_twenty_winners_fit_media_caption_with_unicode(self):
        cid = self.contest(
            title="🎁" * 100,
            claim_contact="🎁" * 150,
            winner_count=20,
            prize_kind="physical",
            prize_json=json.dumps({"value": "Contact organizer"}),
            post_json=json.dumps({"content_type": "photo", "file_id": "photo"}),
        )
        for uid in range(100, 120):
            accounts.ensure_user(
                SimpleNamespace(
                    id=uid, username=None, first_name="🎁" * 30, last_name=None
                )
            )
            service.register(cid, uid)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute("UPDATE contests SET status='closing' WHERE id=?", (cid,))
        await service.finish(self.bot, service.get(cid))
        result = json.loads(
            db.one("SELECT payload FROM contest_outbox WHERE kind='results'")[0]
        )["text"]
        self.assertEqual(result.count("tg://user?id="), 20)
        self.assertLessEqual(
            content.utf16len(html.unescape(re.sub(r"<[^>]+>", "", result))), 1024
        )


class MaintenanceTests(unittest.TestCase):
    def test_reset_keeps_backup_and_clears_all_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE old_data(value TEXT)")
            connection.execute("INSERT INTO old_data VALUES('preserve me')")
            connection.commit()
            connection.close()
            lock = RuntimeLock(str(path))
            self.assertTrue(lock.acquire())
            previous = db.db
            db.db = None
            try:
                saved = manage.reset(path)
            finally:
                lock.release()
                db.db = previous
            with closing(sqlite3.connect(saved)) as old:
                self.assertEqual(
                    old.execute("SELECT value FROM old_data").fetchone()[0],
                    "preserve me",
                )
            with closing(sqlite3.connect(path)) as new:
                for table in (
                    "users",
                    "premium",
                    "channels",
                    "posts",
                    "contests",
                    "payments",
                ):
                    self.assertEqual(
                        new.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                    )
            manage.check(path)
